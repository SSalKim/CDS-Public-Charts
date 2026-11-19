"""Check that the public R2 manifest matches a recent Git manifest (read-only, no credentials).

The coordinator writes the Git manifest first and mirrors the same manifest to R2, with
R2 index writes coalesced to 30 seconds. So the live R2 index should always equal the
split of some recent Git manifest commit, usually the newest one. Comparing against the
commit history avoids false alarms from the few seconds between the two writes.

Checks:
- the R2 index equals ``r2.split_manifest`` of one of the last ``--depth`` Git manifests
- every listed fragment exists, hashes to its revision, and holds ``count`` charts
- index and fragment cache headers, CDN caching, and CORS for the viewer origin
- the index is not stale (``generated_at`` older than ``--max-age-minutes``)

Exit status 1 means a real inconsistency that blocks switching readers to R2.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import requests

from public_charts import r2

ROOT = Path(__file__).resolve().parent
MANIFEST = "data/public_charts_manifest.json"


def git(*args, root=ROOT):
    return subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}", *args], cwd=root,
                          capture_output=True, check=True, timeout=300).stdout


def git_history(depth, fetch=True, root=ROOT):
    """Yield ``(sha, committed_at, manifest)`` newest first for recent manifest commits."""
    ref = "origin/main"
    if fetch:
        git("fetch", "--quiet", f"--depth={depth}", "origin", "main", root=root)
        ref = "FETCH_HEAD"
    log = git("log", f"-{depth}", "--format=%H%x09%cI", ref, "--", MANIFEST, root=root).decode()
    for line in log.splitlines():
        sha, committed = line.split("\t")
        yield sha, committed, json.loads(git("show", f"{sha}:{MANIFEST}", root=root))


def content(index):
    return {"models": index.get("models"), "pipeline_version": index.get("pipeline_version"),
            "runs": {run: {"rev": item.get("rev"), "count": item.get("count")}
                     for run, item in (index.get("runs") or {}).items()}}


def expected_content(manifest):
    models, fragments = r2.split_manifest(manifest)
    return content(r2.build_index(models, {run: r2.revision(f) for run, f in fragments.items()},
                                  {run: len(f["charts"]) for run, f in fragments.items()}, None))


def match_history(index, history):
    """Return ``(position, sha, committed_at)`` of the Git manifest R2 mirrors, or None."""
    live = content(index)
    for position, (sha, committed, manifest) in enumerate(history):
        if expected_content(manifest) == live:
            return position, sha, committed
    return None


def describe_difference(index, manifest):
    live, expected = content(index)["runs"], expected_content(manifest)["runs"]
    return {"only_in_git": sorted(set(expected) - set(live)), "only_in_r2": sorted(set(live) - set(expected)),
            "different": sorted(run for run in set(live) & set(expected) if live[run] != expected[run]),
            "models_equal": content(index)["models"] == expected_content(manifest)["models"]}


def check_fragments(session, base, index, workers=8):
    def one(item):
        run, entry = item
        url = base + r2.fragment_key(run, entry["rev"])
        response = session.get(url, timeout=30)
        if response.status_code != 200:
            return run, f"HTTP {response.status_code}", None
        fragment = response.json()
        if r2.revision(fragment) != entry["rev"]:
            return run, "revision mismatch", None
        if len(fragment.get("charts", [])) != entry["count"]:
            return run, "count mismatch", None
        return run, None, response.headers.get("Cache-Control", "")
    with ThreadPoolExecutor(workers) as pool:
        return list(pool.map(one, sorted(index["runs"].items())))


def run_checks(base, origin, history, *, session=None, now=None, max_age_minutes=45):
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", "cds-public-charts-r2-compare/1.0")
    now = now or datetime.now(timezone.utc)
    problems, warnings, facts = [], [], {}
    response = session.get(base + r2.INDEX_KEY, headers={"Origin": origin}, timeout=30)
    facts["index_status"] = response.status_code
    if response.status_code != 200:
        return {"ok": False, "problems": [f"R2 index unavailable: HTTP {response.status_code}"], "warnings": [], "facts": facts}
    index = response.json()
    facts.update(cache_control=response.headers.get("Cache-Control"), cdn=response.headers.get("cf-cache-status"),
                 cors=response.headers.get("Access-Control-Allow-Origin"), runs=len(index.get("runs", {})),
                 generated_at=index.get("generated_at"))
    if index.get("schema") != r2.SCHEMA:
        problems.append(f"Unexpected index schema {index.get('schema')}")
    if facts["cache_control"] != r2.INDEX_CACHE:
        problems.append(f"Index Cache-Control is {facts['cache_control']!r}")
    if facts["cors"] not in (origin, "*"):
        problems.append(f"CORS does not allow {origin} (got {facts['cors']!r})")
    if facts["cdn"] in (None, "DYNAMIC", "BYPASS"):
        warnings.append(f"Index is not cached at the edge (cf-cache-status={facts['cdn']}); check the Cache Rule")
    try:
        age = (now - datetime.fromisoformat(index["generated_at"])).total_seconds() / 60
        facts["index_age_minutes"] = round(age, 1)
        if age > max_age_minutes:
            warnings.append(f"Index is {age:.0f} minutes old; is a coordinator running with R2 enabled?")
    except (KeyError, TypeError, ValueError):
        problems.append("Index has no valid generated_at")
    history = list(history)
    facts["git_manifests_checked"] = len(history)
    matched = match_history(index, history)
    if matched is None:
        problems.append(f"R2 index matches none of the last {len(history)} Git manifests")
        if history:
            facts["difference_from_latest_git"] = describe_difference(index, history[0][2])
    else:
        position, sha, committed = matched
        facts.update(matched_git_commit=sha[:12], matched_git_committed_at=committed, commits_behind=position)
        if position > 3:
            warnings.append(f"R2 mirrors a Git manifest {position} commits behind the newest")
    results = check_fragments(session, base, index)
    broken = [(run, error) for run, error, _ in results if error]
    if broken:
        problems.append(f"{len(broken)} fragment(s) invalid: " + ", ".join(f"{run} ({error})" for run, error in broken[:10]))
    caches = {cache for _, error, cache in results if not error}
    if caches - {r2.IMMUTABLE_CACHE}:
        problems.append(f"Fragment Cache-Control values: {sorted(caches)}")
    facts["fragments_checked"] = len(results)
    return {"ok": not problems, "problems": problems, "warnings": warnings, "facts": facts}


def markdown(report, base):
    lines = [f"## R2 manifest check: {'OK' if report['ok'] else 'FAILED'}", "", f"Base: `{base}`", ""]
    lines += [f"- ❌ {item}" for item in report["problems"]]
    lines += [f"- ⚠️ {item}" for item in report["warnings"]]
    lines += ["", "| Fact | Value |", "| --- | --- |"]
    lines += [f"| {key} | `{json.dumps(value) if isinstance(value, (dict, list)) else value}` |"
              for key, value in report["facts"].items()]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.environ.get("PUBLIC_CHARTS_R2_PUBLIC_BASE", "https://cds4.uzo1o1.party/"))
    parser.add_argument("--origin", default="https://ssalkim.github.io")
    parser.add_argument("--depth", type=int, default=40, help="Recent Git manifest commits to match against")
    parser.add_argument("--no-fetch", dest="fetch", action="store_false", help="Use the local origin/main")
    parser.add_argument("--max-age-minutes", type=float, default=45,
                        help="Between production runs the index is only rewritten when a run ends")
    args = parser.parse_args()
    base = args.base if args.base.endswith("/") else args.base + "/"
    report = run_checks(base, args.origin, git_history(args.depth, args.fetch), max_age_minutes=args.max_age_minutes)
    print(json.dumps({"event": "r2_compare", **report}, ensure_ascii=False), flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(markdown(report, base))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
