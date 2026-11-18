"""Cheap, bounded source discovery independent of the two calculation pools."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
from threading import Event, Thread
import time
from urllib.parse import urlsplit

import requests
from urllib3.util.retry import Retry

from . import policy
from .sources import (AIGFS_BASES, CANADA_BASES, ECMWF_BASES, GFS_BASES, NotReady,
                      atomic_json, choose_ncep, ncep_index)

INTERVAL_SECONDS = 60
MAX_CHECKS_PER_MINUTE = 60
LOOKAHEAD = 4


def identity(target):
    return target["model"],target["cycle"],target["hour"]


def probe_sources(target):
    model,cycle,hour = identity(target)
    policy.expected_keys(model,cycle,hour)
    if model in {"aifs","ecmwf"}:
        family="ifs" if model == "ecmwf" else "aifs-single"
        path=f"{cycle[:8]}/{cycle[8:]}z/{family}/0p25/oper/{cycle}0000-{hour}h-oper-fc.index"
        return [(f"{base}/{path}","ecmwf") for base in ECMWF_BASES]
    if model == "gfs":
        path=f"gfs.{cycle[:8]}/{cycle[8:]}/atmos/gfs.t{cycle[8:]}z.pgrb2.0p25.f{hour:03}.idx"
        return [(f"{base}/{path}","ncep") for base in GFS_BASES]
    if model == "aigfs":
        path=f"aigfs.{cycle[:8]}/{cycle[8:]}/model/atmos/grib2/aigfs.t{cycle[8:]}z.pres.f{hour:03}.grib2.idx"
        return [(f"{base}/{path}","ncep") for base in AIGFS_BASES]
    if model == "cmc":
        path=(f"{cycle[:8]}/WXO-DD/model_gdps/15km/{cycle[8:]}/{hour:03}/"
              f"{cycle[:8]}T{cycle[8:]}Z_MSC_GDPS_Pressure_MSL_LatLon0.15_PT{hour:03}H.grib2")
        return [(f"{base}/{path}","metadata") for base in CANADA_BASES]
    if model=="ukmo":
        from .ukmo import source_url
        return [(source_url(cycle,hour,"pressure_at_mean_sea_level"),"metadata")]
    from .icon import source_urls
    return [(url,"metadata") for url in source_urls(cycle,hour,"FI",500)]


def _check_index(payload, kind):
    text=payload.decode("utf-8")
    if kind == "ncep":
        choose_ncep(ncep_index(text),{"geopotential_500"})
    else:
        entries=[json.loads(line) for line in text.splitlines() if line.strip()]
        if not any(item.get("param") in {"z","gh"} and item.get("levtype") == "pl"
                   and str(item.get("levelist")) == "500" and int(item.get("_offset",-1)) >= 0
                   and int(item.get("_length",0)) > 0 for item in entries):
            raise NotReady("Representative upper field is not indexed yet")


def probe_source(target):
    """Observe representative inputs only; full GRIB validation remains in collect."""
    deadline=time.monotonic()+20
    failures, retry_after, missing, rate_limited = [], 60, False, False
    with requests.Session() as session:
        session.headers["User-Agent"]="CDS-public-source-monitor/1.0"
        for url,kind in probe_sources(target):
            remaining=deadline-time.monotonic()
            if remaining <= 0:
                break
            started=datetime.now(timezone.utc).isoformat()
            try:
                with session.request("HEAD" if kind == "metadata" else "GET",url,stream=True,
                                     allow_redirects=False,timeout=(min(3,remaining),min(3,remaining))) as response:
                    detected=datetime.now(timezone.utc).isoformat()
                    if response.status_code in {404,410}:
                        print(json.dumps({"event":"source_not_ready",**target,
                                          "source_id":hashlib.sha256(url.encode()).hexdigest(),
                                          "source_host":urlsplit(url).hostname,"checked_at":detected,
                                          "http_status":response.status_code}),flush=True)
                        missing=True
                        continue
                    if response.status_code == 429:
                        rate_limited=True
                        try:
                            retry_after=max(60,Retry().get_retry_after(response) or 60)
                        except (ValueError,TypeError):
                            retry_after=60
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise ValueError("Unexpected source probe response")
                    if kind != "metadata":
                        chunks,size=[],0
                        while True:
                            if time.monotonic() >= deadline:
                                raise ValueError("Source inventory probe exceeds its budget")
                            chunk=response.raw.read1(65536,decode_content=True) if response.raw is not None else response.content
                            if not chunk:
                                break
                            size+=len(chunk)
                            if size > 4*1024**2 or time.monotonic() >= deadline:
                                raise ValueError("Source inventory probe exceeds its budget")
                            chunks.append(chunk)
                            if response.raw is None:
                                break
                        _check_index(b"".join(chunks),kind)
                    elif response.headers.get("Content-Length") == "0":
                        raise NotReady("Empty representative source")
                    return {"available":True,"source_id":hashlib.sha256(url.encode()).hexdigest(),
                            "source_host":urlsplit(url).hostname,"probe_kind":kind,
                            "request_started_at":started,"detected_at":detected,
                            "last_modified":response.headers.get("Last-Modified"),"retry_after":60}
            except NotReady:
                missing=True
            except (requests.RequestException,ValueError,UnicodeError,TypeError) as error:
                failures.append(type(error).__name__)
    # Monitor faults must never permanently stop the validated production path.
    return {"available":False if missing or rate_limited else None,"retry_after":retry_after,
            "error":failures[-1] if failures else None}


class SourceMonitor:
    def __init__(self, queue, work, *, probe=probe_source, emit=None, clock=time.monotonic):
        self.queue,self.probe,self.clock=queue,probe,clock
        self.emit=emit or (lambda item: print(json.dumps(item),flush=True))
        self.path=Path(work)/"raw-cache/prepared/source-monitor.json"
        self.stop=Event()
        self.thread=None
        self.due={}
        self.check_started={}
        self.observations={}
        try:
            cached=json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(cached,dict):
                raise ValueError("Invalid source observation cache")
            for key,value in cached.items():
                stamp=datetime.fromisoformat(value["detected_at"])
                age=(datetime.now(timezone.utc)-stamp).total_seconds()
                if 0 <= age <= 48*3600 and len(key) == 64:
                    self.observations[key]=value
        except (OSError,ValueError,TypeError,KeyError):
            self.observations={}

    def accept(self,target,result):
        key=identity(target)
        now=self.clock()
        self.due[key]=max(now,self.check_started.get(key,now)+result.get("retry_after",60))
        status=result.get("available")
        if status is not False:
            self.queue.source_observed(target)
        event={"event":"source_probe","scope":"representative_input",**target,
               "available":status,**{k:v for k,v in result.items() if k not in {"available"}}}
        if status is True:
            source_id=result["source_id"]
            prior=self.observations.get(source_id)
            if prior is None:
                self.observations[source_id]={k:result.get(k) for k in ("detected_at","last_modified")}
                event["event"]="source_detected"
            else:
                event["first_detected_at"]=prior["detected_at"]
            try:
                age=(datetime.fromisoformat(result["detected_at"])-parsedate_to_datetime(result["last_modified"])).total_seconds()
                event["modified_to_detected_seconds"]=round(age,3)
            except (ValueError,TypeError,KeyError,OverflowError):
                pass
        self.emit(event)

    def _run(self):
        recent,pending=deque(),{}
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix="chart-source-probe") as executor:
            while not self.stop.is_set():
                for future,target in list(pending.items()):
                    if not future.done():
                        continue
                    pending.pop(future)
                    try:
                        result=future.result()
                    except Exception as error:
                        result={"available":None,"retry_after":60,"error":type(error).__name__}
                    self.accept(target,result)
                now=self.clock()
                while recent and now-recent[0] >= 60:
                    recent.popleft()
                active={identity(target) for target in pending.values()}
                for target in self.queue.monitor_targets(LOOKAHEAD):
                    if len(pending) >= 2 or len(recent) >= MAX_CHECKS_PER_MINUTE:
                        break
                    key=identity(target)
                    if key in active or now < self.due.get(key,0):
                        continue
                    pending[executor.submit(self.probe,dict(target))]=target
                    self.check_started[key]=now
                    active.add(key)
                    recent.append(now)
                    self.emit({"event":"source_probe_started",**target})
                self.stop.wait(.2)
            for future,target in pending.items():
                try:
                    self.accept(target,future.result())
                except Exception as error:
                    self.emit({"event":"source_probe_shutdown_error","error":type(error).__name__})

    def start(self):
        self.queue.source_monitor=True
        def guarded():
            try:
                self._run()
            except Exception as error:
                self.queue.source_monitor=False
                self.emit({"event":"source_monitor_failed","error":type(error).__name__,
                           "fallback":"validated_prepare"})
        self.thread=Thread(target=guarded,name="chart-source-monitor")
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join()
        # Bound persistence independently of the regional-array cache budget.
        kept=dict(sorted(self.observations.items(),key=lambda item:item[1]["detected_at"])[-4096:])
        try:
            atomic_json(self.path,kept)
        except OSError as error:
            self.emit({"event":"source_monitor_cache_failed","error":type(error).__name__})
