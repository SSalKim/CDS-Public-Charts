"""Build argv without shell interpolation; Actions supplies scoped runtime tokens."""
import os
from pathlib import Path
import sys


def action_args(role, env):
    work = env["CHART_WORK"]
    if role == "helper":
        result = ["public_charts_helper.py","--workdir",work,"--session",env["HELPER_SESSION"],
                  "--code-sha",env["HELPER_CODE_SHA"]]
        if env.get("HELPER_SMOKE") == "true":
            result.append("--smoke")
        return result
    if role != "coordinator":
        raise ValueError("Unexpected Actions role")
    result = ["public_charts_run.py","--workdir",work,"--max-leads",env.get("LIMIT_INPUT","2"),
              "--workers",env.get("WORKERS_INPUT","auto"),"--upload-workers",env.get("UPLOAD_WORKERS_INPUT","3")]
    dry = env.get("DRY_RUN","true") == "true"
    result.append("--dry-run" if dry else "--publish-incremental")
    if env.get("MODEL_INPUT","all") != "all":
        result.extend(("--models",env["MODEL_INPUT"]))
    for name, option in (("CYCLE_INPUT","--cycle"),("START_CYCLE_INPUT","--start-cycle")):
        if env.get(name):
            result.extend((option,env[name]))
    if env.get("HOURS_INPUT"):
        result.extend(["--hours",*env["HOURS_INPUT"].replace(","," ").split()])
    minutes = float(env.get("MINUTES_INPUT","0") or 0)
    force = env.get("FORCE_INPUT") == "true"
    if not minutes and not env.get("CYCLE_INPUT") and not env.get("HOURS_INPUT") and not force:
        minutes = float(env.get("TIME_BUDGET_INPUT","20"))
    if minutes:
        result.extend(("--time-budget-minutes",str(minutes)))
    if force:
        result.append("--force")
    if not dry and not force and minutes and env.get("HELPER_AVAILABLE") == "true":
        result.append("--remote-helper")
    return result


if __name__ == "__main__":
    args = action_args(sys.argv[1],os.environ)
    script = Path(__file__).resolve().parent / args[0]
    os.execv(sys.executable,[sys.executable,str(script),*args[1:]])
