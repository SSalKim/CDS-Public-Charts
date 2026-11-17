const GITHUB_API = "https://api.github.com";
const OWNER = "SSalKim";
const REF = "main";
const USER_AGENT = "cds-cloudflare-scheduler";
const SCHEDULER_VERSION = "20261117-charts-1min-v2";
const ACTIVE_STATUSES = ["in_progress", "queued", "waiting", "pending", "requested"];
// Verified empty benchmark record; GitHub rejects both cancel APIs for this run.
const EMPTY_QUEUED_CHART_RUN_IDS = new Set(["43883299059"]);
const RETIRED_TARGETS = new Set(["gdm", "gdm-check", "dmdw"]);
const TARGETS = {
  vtg: { repo: "CDS", workflow: "vtg.yml" },
  charts: {
    repo: "CDS-Public-Charts",
    workflow: "public_charts.yml",
    inputs: { dry_run: false, runner_mode: "auto", model: "all", max_leads: "2", force: false }
  }
};
const CRONS = {
  charts: "*/1 * * * *",
  vtg: "9,19,29,39,49,59 * * * *"
};
const LEGACY_CHARTS_CRON = "3,13,23,33,43,53 * * * *";
const MINUTE_CHART_CRONS = new Set([CRONS.charts, "* * * * *", "0-59 * * * *", "0-59/1 * * * *"]);

export function createDispatcher(fetcher = fetch) {
  async function githubRequest(path, token, method = "GET", body) {
    // A timed-out POST may already have been accepted: never retry dispatches.
    const attempts = method === "GET" ? 2 : 1;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 8000);
      try {
        const response = await fetcher(`${GITHUB_API}${path}`, {
          method,
          headers: {
            Authorization: `Bearer ${token}`,
            Accept: "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT
          },
          signal: controller.signal,
          ...(body === undefined ? {} : { body: JSON.stringify(body) })
        });
        const text = await response.text();
        if (response.status >= 500 && attempt + 1 < attempts) continue;
        if (!response.ok) return { ok: false, status: response.status };
        if (method === "POST") return { ok: response.status === 204, status: response.status };
        return { ok: true, status: response.status, data: JSON.parse(text) };
      } catch {
        if (attempt + 1 === attempts) return { ok: false, status: 0 };
      } finally {
        clearTimeout(timer);
      }
    }
    return { ok: false, status: 0 };
  }

  async function runTarget(target, env) {
    const config = TARGETS[target];
    if (!config) return { ok: false, target, skipped: true, reason: "Unknown target" };
    if (!env.GITHUB_TOKEN) {
      return { ok: false, target, skipped: true, reason: "Missing GITHUB_TOKEN secret" };
    }
    const base = `/repos/${OWNER}/${config.repo}/actions/workflows/${config.workflow}`;
    if (target === "charts") {
      // Filter server-side so long-queued runs cannot hide behind completed runs.
      for (const status of ACTIVE_STATUSES) {
        const pageSize = status === "queued" ? EMPTY_QUEUED_CHART_RUN_IDS.size + 1 : 1;
        const check = await githubRequest(`${base}/runs?branch=${REF}&status=${status}&per_page=${pageSize}`, env.GITHUB_TOKEN);
        if (!check.ok || !Array.isArray(check.data?.workflow_runs)) {
          return { ok: false, target, skipped: true, status: check.status,
            reason: "Workflow status unavailable; dispatch skipped" };
        }
        const active = check.data.workflow_runs.find(run =>
          status !== "queued" || !EMPTY_QUEUED_CHART_RUN_IDS.has(String(run.id)));
        if (active) {
          return { ok: true, target, skipped: true, run_id: active.id, run_status: active.status,
            reason: "Chart production is already active" };
        }
      }
    }
    const response = await githubRequest(`${base}/dispatches`, env.GITHUB_TOKEN, "POST",
      { ref: REF, ...(config.inputs ? { inputs: config.inputs } : {}) });
    return { ok: response.ok, target, skipped: false, status: response.status,
      reason: response.ok ? "Dispatch accepted" : "GitHub dispatch failed" };
  }

  async function handleFetch(request, env) {
    const target = new URL(request.url).searchParams.get("target");
    if (RETIRED_TARGETS.has(target)) {
      return Response.json({ ok: false, target, skipped: true, reason: "Target retired" }, { status: 410 });
    }
    if (!target && request.method === "GET") {
      return Response.json({ ok: true, service: USER_AGENT, version: SCHEDULER_VERSION, crons: CRONS,
        allowedTargets: ["vtg", "cds", "charts", "both", "all"] });
    }
    const targets = target === "all" || target === "both" ? ["vtg", "charts"]
      : target === "cds" || target === "vtg" ? ["vtg"] : target === "charts" ? ["charts"] : [];
    if (!targets.length) return Response.json({ ok: false, reason: "Unknown target" }, { status: 400 });
    if (request.method !== "POST") {
      return Response.json({ ok: false, reason: "Manual dispatch requires authenticated POST" },
        { status: 405, headers: { Allow: "POST" } });
    }
    // Scheduled execution needs no HTTP trigger secret. Public URLs never dispatch.
    if (!env.MANUAL_TRIGGER_TOKEN) {
      return Response.json({ ok: false, reason: "Manual dispatch is disabled" }, { status: 503 });
    }
    if (request.headers.get("Authorization") !== `Bearer ${env.MANUAL_TRIGGER_TOKEN}`) {
      return Response.json({ ok: false, reason: "Unauthorized" }, { status: 401 });
    }
    const results = [];
    for (const item of targets) results.push(await runTarget(item, env));
    const ok = results.every(result => result.ok);
    return Response.json({ ok, results }, { status: ok ? 200 : 502 });
  }

  function scheduled(event, env, ctx) {
    // The two triggers overlap at VTG minutes; route by trigger, not minute.
    const cron = String(event.cron || "").trim().replace(/\s+/g, " ");
    const target = MINUTE_CHART_CRONS.has(cron) || cron === LEGACY_CHARTS_CRON ? "charts"
      : cron === CRONS.vtg ? "vtg" : null;
    if (!target) {
      console.warn(JSON.stringify({ event: "scheduler_skipped", version: SCHEDULER_VERSION,
        cron: event.cron, ok: false, skipped: true, reason: "Unrecognized cron expression" }));
      return;
    }
    ctx.waitUntil(runTarget(target, env).then(result => {
      console.log(JSON.stringify({ event: "scheduler_result", version: SCHEDULER_VERSION, cron: event.cron, ...result }));
      if (!result.ok) {
        const status = result.status === 0 ? "network/response error"
          : Number.isInteger(result.status) ? `HTTP ${result.status}` : "no HTTP request sent";
        throw new Error(`GitHub scheduler request failed [${result.target}] ${status}: ${result.reason}`);
      }
    }));
  }

  return { fetch: handleFetch, scheduled, runTarget };
}

const dispatcher = createDispatcher();
export default { fetch: dispatcher.fetch, scheduled: dispatcher.scheduled };
