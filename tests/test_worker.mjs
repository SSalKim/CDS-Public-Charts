import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../cloudflare/worker.js", import.meta.url), "utf8");
const { createDispatcher } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const env = { GITHUB_TOKEN: "private-github-token", MANUAL_TRIGGER_TOKEN: "private-trigger-token" };

function fixture(handler = () => null) {
  const calls = [];
  const dispatcher = createDispatcher(async (url, options) => {
    calls.push({ url, ...options });
    const response = handler(url, options, calls.length);
    return response ?? (options.method === "POST" ? new Response(null, { status: 204 })
      : Response.json({ workflow_runs: [] }));
  });
  return { dispatcher, calls };
}

test("chart dispatch uses the new repo, real production and no fixed cycle", async () => {
  const { dispatcher, calls } = fixture();
  const result = await dispatcher.runTarget("charts", env);
  assert.equal(result.ok, true);
  assert.equal(calls.filter(call => call.method === "GET").length, 5);
  const post = calls.at(-1);
  assert.match(post.url, /SSalKim\/CDS-Public-Charts\/actions\/workflows\/public_charts.yml\/dispatches$/);
  assert.deepEqual(JSON.parse(post.body), { ref: "main", inputs: {
    dry_run: false, runner_mode: "auto", model: "all", max_leads: 2, force: false
  } });
  assert.equal(post.headers["Content-Type"], "application/json");
});

for (const status of ["in_progress", "queued", "waiting", "pending", "requested"]) {
  test(`chart ${status} run prevents duplicate dispatch`, async () => {
    const { dispatcher, calls } = fixture(url => new URL(url).searchParams.get("status") === status
      ? Response.json({ workflow_runs: [{ id: 42, status }] }) : null);
    const result = await dispatcher.runTarget("charts", env);
    assert.equal(result.ok, true);
    assert.equal(result.skipped, true);
    assert.equal(result.run_status, status);
    assert(!calls.some(call => call.method === "POST"));
  });
}

for (const code of [401, 403, 404, 429, 500, 503]) {
  test(`status HTTP ${code} fails closed`, async () => {
    const { dispatcher, calls } = fixture(() => new Response("private upstream error", { status: code }));
    const result = await dispatcher.runTarget("charts", env);
    assert.equal(result.ok, false);
    assert.equal(result.skipped, true);
    assert.equal(calls.length, code >= 500 ? 2 : 1);
    assert(!calls.some(call => call.method === "POST"));
    assert(!JSON.stringify(result).includes("private"));
  });
}

test("malformed JSON and transport errors never trigger charts", async () => {
  for (const failure of [() => new Response("not json"), () => Response.json({}),
    () => { throw new Error("private transport detail"); }]) {
    const { dispatcher, calls } = fixture(failure);
    const result = await dispatcher.runTarget("charts", env);
    assert.equal(result.ok, false);
    assert(!calls.some(call => call.method === "POST"));
    assert(!JSON.stringify(result).includes("private"));
  }
});

test("one transient GET failure can recover", async () => {
  const { dispatcher, calls } = fixture((url, options, count) => count === 1
    ? new Response("Unavailable", { status: 503 }) : null);
  assert.equal((await dispatcher.runTarget("charts", env)).ok, true);
  assert.equal(calls.length, 7);
});

test("VTG keeps original CDS dispatch without new busy filtering", async () => {
  const { dispatcher, calls } = fixture();
  assert.equal((await dispatcher.runTarget("vtg", env)).ok, true);
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /SSalKim\/CDS\/actions\/workflows\/vtg.yml\/dispatches$/);
  assert.deepEqual(JSON.parse(calls[0].body), { ref: "main" });
});

test("failed or ambiguous POSTs are never retried", async () => {
  for (const failure of [() => new Response("private", { status: 503 }),
    () => { throw new Error("private"); }]) {
    const { dispatcher, calls } = fixture(failure);
    const result = await dispatcher.runTarget("vtg", env);
    assert.equal(result.ok, false);
    assert.equal(calls.length, 1);
    assert(!JSON.stringify(result).includes("private"));
  }
});

test("every daily cron slot routes correctly and the retired slot is inert", async () => {
  for (let minute = 0; minute < 60; minute += 1) {
    const { dispatcher, calls } = fixture();
    const pending = [];
    dispatcher.scheduled({ scheduledTime: Date.UTC(2026, 10, 16, 23, minute), cron: "test" },
      env, { waitUntil: task => pending.push(task) });
    await Promise.all(pending);
    const posts = calls.filter(call => call.method === "POST");
    assert.equal(posts.length, [3, 9].includes(minute % 10) ? 1 : 0);
    if (posts.length) assert(posts[0].url.includes(minute % 10 === 3 ? "/CDS-Public-Charts/" : "/CDS/"));
    assert(!calls.some(call => call.url.includes("/GDM/") || call.url.includes("dmdw")));
  }
});

test("scheduled errors are visible without credentials in logs", async () => {
  const { dispatcher } = fixture(() => new Response("private", { status: 403 }));
  const pending = [];
  dispatcher.scheduled({ scheduledTime: Date.UTC(2026, 10, 16, 0, 3), cron: "test" },
    env, { waitUntil: task => pending.push(task) });
  await assert.rejects(pending[0], /GitHub scheduler request failed/);
});

test("health endpoint and retired manual URLs never call GitHub", async () => {
  const { dispatcher, calls } = fixture();
  assert.equal((await dispatcher.fetch(new Request("https://worker.example/"), env)).status, 200);
  for (const target of ["gdm", "gdm-check", "dmdw"]) {
    assert.equal((await dispatcher.fetch(new Request(`https://worker.example/?target=${target}`), env)).status, 410);
  }
  assert.equal(calls.length, 0);
});

test("manual dispatch requires POST and a separate secret", async () => {
  const { dispatcher, calls } = fixture();
  const url = "https://worker.example/?target=charts";
  assert.equal((await dispatcher.fetch(new Request(url), env)).status, 405);
  assert.equal((await dispatcher.fetch(new Request(url, { method: "POST" }), { GITHUB_TOKEN: env.GITHUB_TOKEN })).status, 503);
  assert.equal((await dispatcher.fetch(new Request(url, { method: "POST" }), env)).status, 401);
  assert.equal(calls.length, 0);
});

test("manual cds alias and both/all route only to the two active workflows", async () => {
  for (const [target, count] of [["cds", 1], ["vtg", 1], ["charts", 1], ["both", 2], ["all", 2]]) {
    const { dispatcher, calls } = fixture();
    const request = new Request(`https://worker.example/?target=${target}`, {
      method: "POST", headers: { Authorization: `Bearer ${env.MANUAL_TRIGGER_TOKEN}` }
    });
    assert.equal((await dispatcher.fetch(request, env)).status, 200);
    assert.equal(calls.filter(call => call.method === "POST").length, count);
    assert(!JSON.stringify(calls).includes("/GDM/"));
  }
});

test("missing GitHub token and unknown targets never make requests", async () => {
  const { dispatcher, calls } = fixture();
  assert.equal((await dispatcher.runTarget("charts", {})).ok, false);
  assert.equal((await dispatcher.runTarget("vtg", {})).ok, false);
  assert.equal((await dispatcher.runTarget("gdm", env)).ok, false);
  assert.equal((await dispatcher.fetch(new Request("https://worker.example/?target=unknown"), env)).status, 400);
  assert.equal(calls.length, 0);
});
