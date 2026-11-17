"""Exercise two real Actions jobs and artifact transport without any Drive writes."""
import json
import os
from pathlib import Path
import subprocess
import time

from public_charts import policy
from public_charts.artifacts import GitHubArtifacts
from public_charts.parallel import LeadQueue, stage_lead
from public_charts.remote import RemoteCoordinator, identity
from public_charts_helper import smoke_renderer


def main():
    if any(name.startswith("PUBLIC_CHARTS_DRIVE") for name in os.environ):
        raise RuntimeError("Transport verification must not receive Drive credentials")
    session = os.environ["GITHUB_RUN_ID"]+"-"+os.environ.get("GITHUB_RUN_ATTEMPT","1")
    sha = subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
    work = Path(os.environ["RUNNER_TEMP"]) / "chart-remote-verification"
    targets = [{"model":model,"cycle":"2026111612","hour":0} for model in ("aifs","gfs","cmc","icon")]
    existing = set().union(*(policy.expected_keys(*identity(target)) - {policy.key(*identity(target),"gph500","asia")}
                             for target in targets))
    queue = LeadQueue(targets,existing,100)
    queue.model_parallel_limit = 4
    local = queue.next(set())
    completed, remote_count = set(), []

    def consume(target, staged):
        if staged["error"]:
            raise RuntimeError("Helper verification returned an error")
        for item in staged["artifacts"]:
            if not Path(item["path"]).is_file():
                raise RuntimeError("Helper verification image missing")
        if not staged.get("partial"):
            completed.add(identity(target))
            remote_count.append(identity(target))

    helper_runner = os.environ.get("VERIFY_HELPER_RUNNER","github-hosted")
    broker = RemoteCoordinator(GitHubArtifacts(),session,sha,queue,work,consume,minutes=5,smoke=True,
                               smoke_runner=helper_runner)
    broker.start()
    deadline = time.time()+360
    local_done = False
    try:
        while len(completed) < len(targets) and time.time() < deadline:
            broker.tick()
            if broker.disabled:
                raise RuntimeError("Remote helper verification disabled")
            if broker.leases and not local_done:
                staged = stage_lead(local,work,existing,renderer=smoke_renderer)
                if staged["error"]:
                    raise RuntimeError("Local verification renderer failed")
                queue.rendered(local,staged)
                queue.finished(local,None)
                completed.add(identity(local))
                local_done = True
            time.sleep(1)
        if len(completed) != 4 or not local_done or len(set(remote_count)) != 3:
            raise RuntimeError("Two-job verification did not finish distinct leads")
        print(json.dumps({"event":"two_runner_verified","local_leads":1,"remote_leads":3,
                          "drive_writes":0,"session":session,"helper_runner":helper_runner}),flush=True)
    finally:
        broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
