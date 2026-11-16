#!/usr/bin/env bash
set -euo pipefail
unset RUNNER_TRACKING_ID
charts="$HOME/actions-runner-cds-public-charts"
vtg="$HOME/actions-runner/cds-vtg/actions-runner"
logs="$HOME/.cache/cds-wsl-maintenance"
mkdir -p "$logs"
test -f "$charts/start-charts.sh"
test -f "$vtg/run.sh"
if ! pgrep -f "^$charts/bin/Runner.Listener" >/dev/null; then
    nohup bash "$charts/start-charts.sh" >"$logs/charts.log" 2>&1 </dev/null &
fi
if ! pgrep -f "^$vtg/bin/Runner.Listener" >/dev/null; then
    (cd "$vtg" && nohup ./run.sh >"$logs/vtg.log" 2>&1 </dev/null &)
fi
sleep 15
pgrep -f "^$charts/bin/Runner.Listener" >/dev/null
pgrep -f "^$vtg/bin/Runner.Listener" >/dev/null
awk '/^MemTotal:|^MemAvailable:/{print}' /proc/meminfo
