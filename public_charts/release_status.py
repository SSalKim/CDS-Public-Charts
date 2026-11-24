"""Record when KMA publishes the charts the viewer shows straight from KMA.

The viewer's timetable cannot read KMA response headers from the browser, so a background
thread reads the Last-Modified time of each recent run's first and last lead image and keeps
``status/releases.json`` on R2 current. Runs already known to be complete are not asked again.
While a run is being published, ``latest`` holds the furthest lead out so far, found by a binary
search over leads every model publishes (12-hourly, 6-hourly for KIM LDAPS); KMA publishes leads
in order, so a few image requests per run and check are enough.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import threading
import time

import requests

from .kma_gate import image_header_valid

STATUS_KEY = "status/releases.json"
STATUS_CACHE = "public, max-age=30"
SCHEMA = 1
BASE = "https://data.kma.go.kr/CHT"
# Seconds between checks, and between rewrites of an unchanged file (the viewer treats a file
# older than 20 minutes as stale and falls back to its own checks).
INTERVAL_SECONDS = 120
REFRESH_SECONDS = 600
LOOKBACK_HOURS = 36
# KMA's earliest chart arrives about 3 h after the cycle time.
MIN_AGE_HOURS = 2
PROBE_WORKERS = 4
# model: (KMA folder, file name, {cycle hour: (first lead, last lead)}, progress step). The leads
# are the ones the viewer shows from KMA: UKUM 156/168 h and ECMWF 252-360 h come from R2 instead.
# The progress step divides every lead KMA publishes for that chart, so each probed lead exists.
STREAMS = {
    "kim_gdps": ("KIMG", "kim_gdps_erly_asia_gph500_ft06_pa4_s{hour:03}_{cycle}.png",
                 {0: (0, 288), 6: (0, 84), 12: (0, 288), 18: (0, 84)}, 12),
    "kim_rdps": ("KIMR1", "kim_rdps_erly_asia_gph500_s{hour:03}_{cycle}.png",
                 {0: (0, 120), 6: (0, 72), 12: (0, 120), 18: (0, 72)}, 12),
    "kim_ldps": ("KIML", "kim_ldps_erly_hkor_gph500_s{hour:03}_{cycle}.png",
                 {0: (0, 48), 6: (0, 48), 12: (0, 48), 18: (0, 48)}, 6),
    "ukmo": ("GDPS", "ukum_erly_asia_gph500_s{hour:03}_{cycle}.png", {0: (0, 144), 12: (0, 144)}, 12),
    "ecmwf": ("ECMW", "ecmw_lc40_asia_gph500_s{hour:03}_{cycle}.gif", {0: (0, 240), 12: (0, 240)}, 12),
}


def utcnow():
    return datetime.now(timezone.utc)


def image_url(model, cycle, hour):
    folder, template, *_ = STREAMS[model]
    return f"{BASE}/{folder}/{cycle[:6]}/{cycle[6:8]}/{template.format(hour=hour, cycle=cycle)}"


def release_time(url, *, session=requests, now=utcnow):
    """ISO time the image appeared (its Last-Modified), or None while it is missing or unreadable."""
    try:
        with session.get(url, headers={"Range": "bytes=0-63", "User-Agent": "CDS-KMA-release-times/1.0"},
                         stream=True, timeout=(3, 5), allow_redirects=False) as response:
            if response.status_code not in (200, 206) or not image_header_valid(response.raw.read(64)):
                return None
            value = response.headers.get("Last-Modified")
    except requests.RequestException:
        return None
    try:
        moment = parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        moment = None
    return (moment.astimezone(timezone.utc) if moment else now()).isoformat()


def recent_runs(now, *, lookback_hours=LOOKBACK_HOURS, min_age_hours=MIN_AGE_HOURS):
    """(run key, model, cycle, first, last) for each stream's cycles old enough to have started."""
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=lookback_hours)
    result = []
    for model, (_, _, cycles, _) in STREAMS.items():
        moment = start
        while moment <= now - timedelta(hours=min_age_hours):
            if moment.hour in cycles:
                cycle = moment.strftime("%Y%m%d%H")
                first, last = cycles[moment.hour]
                result.append((f"{model}/{cycle}", model, cycle, first, last))
            moment += timedelta(hours=1)
    return result


class ReleaseStatus:
    def __init__(self, client, *, probe=release_time, wall=utcnow, clock=time.monotonic, emit=None,
                 interval=INTERVAL_SECONDS, refresh=REFRESH_SECONDS):
        self.client, self.probe, self.wall, self.clock = client, probe, wall, clock
        self.emit = emit or (lambda item: print(json.dumps(item), flush=True))
        self.interval, self.refresh = interval, refresh
        self.runs = {}
        self.last_write = None
        self.stop = threading.Event()
        self.thread = None

    @classmethod
    def from_env(cls, env=None, **kwargs):
        from . import r2
        return cls(r2.client_from_env(env), **kwargs)

    def load(self):
        """Start from the published file so finished runs are not asked again after a restart."""
        payload = self.client.get(STATUS_KEY)
        if not payload:
            return
        status = json.loads(payload)
        if status.get("schema") != SCHEMA or not isinstance(status.get("runs"), dict):
            return
        known = {key for key, *_ in recent_runs(self.wall())}
        for key, entry in status["runs"].items():
            if key in known and isinstance(entry, dict):
                self.runs[key] = {name: entry[name] for name in ("first", "last", "latest", "leads") if name in entry}

    def check(self):
        """Ask KMA about every unfinished recent run; return True when anything changed."""
        runs = recent_runs(self.wall())
        known = {key for key, *_ in runs}
        changed = any(key not in known for key in self.runs)
        self.runs = {key: entry for key, entry in self.runs.items() if key in known}
        pending = [run for run in runs if not self.runs.get(run[0], {}).get("last")]

        def ask(run):
            key, model, cycle, first, last = run
            entry = dict(self.runs.get(key, {}))
            finished = self.probe(image_url(model, cycle, last))
            if finished:
                entry["last"] = finished
                entry.pop("latest", None)
            if not entry.get("first"):
                started = self.probe(image_url(model, cycle, first))
                if started:
                    entry["first"] = started
            if entry.get("first") and not entry.get("last"):
                entry["latest"] = self.latest_lead(model, cycle, first, last, entry.get("latest", first))
            if entry:
                entry["leads"] = [first, last]
            return key, entry

        with ThreadPoolExecutor(max_workers=PROBE_WORKERS, thread_name_prefix="kma-release-times") as pool:
            for key, entry in pool.map(ask, pending):
                if entry and entry != self.runs.get(key):
                    self.runs[key] = entry
                    changed = True
        return changed

    def latest_lead(self, model, cycle, first, last, known):
        """The furthest lead out that exists, searching only above ``known`` (already seen)."""
        step = STREAMS[model][3]
        rungs = sorted({known} | {hour for hour in range(first, last, step) if hour > known})
        low, high = 0, len(rungs) - 1
        while low < high:
            middle = (low + high + 1) // 2
            if self.probe(image_url(model, cycle, rungs[middle])):
                low = middle
            else:
                high = middle - 1
        return rungs[low]

    def write(self):
        body = json.dumps({"schema": SCHEMA, "generated_at": self.wall().isoformat(),
                           "runs": dict(sorted(self.runs.items()))},
                          separators=(",", ":"), sort_keys=True).encode()
        self.client.put(STATUS_KEY, body, cache_control=STATUS_CACHE)
        self.last_write = self.clock()
        self.emit({"event": "kma_release_status_published", "runs": len(self.runs),
                   "complete": sum(1 for entry in self.runs.values() if entry.get("last")), "bytes": len(body)})

    def tick(self):
        changed = self.check()
        if changed or self.last_write is None or self.clock() - self.last_write >= self.refresh:
            self.write()

    def _loop(self):
        try:
            self.load()
        except Exception as error:
            self.emit({"event": "kma_release_status_failed", "stage": "load", "error": type(error).__name__})
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as error:
                self.emit({"event": "kma_release_status_failed", "stage": "tick", "error": type(error).__name__,
                           "message": str(error)[:200]})
            self.stop.wait(self.interval)

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="kma-release-status", daemon=True)
        self.thread.start()
        return self

    def close(self, timeout=15):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=timeout)
