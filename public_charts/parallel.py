"""Bounded renderer processes; only the caller owns Drive and Git publication."""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gc
import faulthandler
import multiprocessing
import os
from pathlib import Path
import shutil
import time
import traceback
from queue import Queue, Empty, Full
from threading import Event, Thread
from urllib.parse import urlsplit

from . import policy
from .resources import available_memory, cmc_worker_count

_EVENTS = _CANCELLED = None


def child_path(root, *parts):
    root = Path(root).resolve()
    target = root.joinpath(*parts)
    if target.is_symlink() or not target.resolve().is_relative_to(root) or target.resolve() == root:
        raise ValueError("Unsafe renderer scratch path")
    return target


def cleanup_raw(work):
    root = child_path(work, "raw-cache")
    for name in ("grib-cache", "fields", "extended-fields", "images"):
        target = child_path(root, name)
        if target.exists():
            shutil.rmtree(target)


def model_workspace(work, model):
    if model not in policy.RETENTION:
        raise ValueError("Unknown renderer model")
    return child_path(work, "render-workers", model)


def initialize_renderer(events=None, cancelled=None):
    global _EVENTS, _CANCELLED
    _EVENTS, _CANCELLED = events, cancelled
    if events is not None:
        events.cancel_join_thread()
    for name in list(os.environ):
        if name.startswith(("PUBLIC_CHARTS_DRIVE_", "PUBLIC_CHARTS_DRIVE2_", "PUBLIC_CHARTS_ICON_DRIVE_")) or name in {"GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN"}:
            os.environ.pop(name, None)
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    if os.environ.get("MPLCONFIGDIR"):
        os.environ["MPLCONFIGDIR"] = str(Path(os.environ["MPLCONFIGDIR"]) / str(os.getpid()))


def lead_workspace(work, target):
    return child_path(model_workspace(work, target["model"]), "leads", target["cycle"], f'{target["hour"]:03}')


def cleanup_staging(work, models):
    for model in models:
        for name in ("staged", "leads"):
            scratch = child_path(model_workspace(work, model), name)
            if scratch.exists():
                shutil.rmtree(scratch)


def input_status(info):
    modified = []
    for source in info.get("sources", []):
        try:
            stamp = parsedate_to_datetime(source.get("last_modified") or "")
            if stamp.tzinfo is not None:
                modified.append(stamp.astimezone(timezone.utc))
        except (ValueError, TypeError, OverflowError):
            pass
    return {"observed_at": info["observed_at"], "seconds": info.get("seconds", 0),
            "source_last_modified_at": max(modified).isoformat() if modified else None}


def error_record(exception):
    from .sources import NotReady
    error = {"type": type(exception).__name__, "pending": isinstance(exception, NotReady)}
    if not error["pending"]:
        frames = traceback.extract_tb(exception.__traceback__)
        error["location"] = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                             for frame in frames if Path(frame.filename).parent.name == "public_charts"][-3:]
        response = getattr(exception, "response", None)
        if response is not None:
            error.update(http_status=response.status_code, source_host=urlsplit(response.url or "").hostname)
    return error


def prepare_lead(target, work):
    """Bounded disk-backed lookahead; no image allocation or Drive credentials."""
    from .sources import collect
    root = child_path(lead_workspace(work, target), "raw-cache")
    started = time.monotonic()
    watch = _EVENTS is not None
    if watch:
        faulthandler.dump_traceback_later(180,repeat=True)
    try:
        arrays, metadata, statistics = collect(target["model"], target["cycle"], target["hour"], root,
                                                include_precipitation=False)
        del arrays
        status = input_status({"observed_at": datetime.now(timezone.utc).isoformat(),
                               "seconds": round(time.monotonic() - started, 3),
                               "sources": metadata.get("sources", [])})
        return {"error": None, "inputs": status, "requests": statistics.get("requests", 0),
                "bytes": statistics.get("bytes", 0)}
    except Exception as exception:
        cleanup_raw(lead_workspace(work, target))
        return {"error": error_record(exception)}
    finally:
        if watch:
            faulthandler.cancel_dump_traceback_later()
        gc.collect()


def stage_lead(target, work, existing, force=False, raw_override=None, renderer=None, preparation=None):
    """Keep validated WebPs until the coordinator uploads them; never write Drive."""
    from .producer import produce_lead
    from .sources import NotReady

    model, cycle, hour = target["model"], target["cycle"], target["hour"]
    expected = policy.expected_keys(model, cycle, hour)
    model_root = model_workspace(work, model)
    workspace = lead_workspace(work, target) if not raw_override else model_root
    raw = Path(raw_override) if raw_override else child_path(workspace, "raw-cache")
    checkpoints = Path(raw_override) / "checkpoints" if raw_override else child_path(work, "raw-cache", "checkpoints")
    staged = child_path(model_root, "staged", cycle, f"{hour:03}")
    staged.mkdir(parents=True, exist_ok=True)
    artifacts = []
    started = time.monotonic()
    inputs = {}

    def send(kind, payload):
        if _EVENTS is None:
            return
        while _CANCELLED is None or not _CANCELLED.is_set():
            try:
                _EVENTS.put((kind, target, payload), timeout=.2)
                return
            except Full:
                pass
        raise RuntimeError("Chart publication stopped")

    def progress(phase, info):
        if phase not in {"base", "surface"}:
            send("diagnostic", {"event": phase, **info})
            return
        status = preparation["inputs"] if phase == "base" and preparation else input_status(info)
        inputs[phase] = status
        send("inputs", {"phase": phase, **status})

    def emit(product, domain, path, alias_of=None):
        if policy.key(model, cycle, hour, product, domain) not in expected:
            raise ValueError("Unexpected staged product")
        phase = "base" if product in policy.products(cycle) else "surface"
        artifact = {"product": product, "domain": domain, "alias_of": alias_of,
                    "created_at": datetime.now(timezone.utc).isoformat(), **inputs.get(phase, {})}
        if alias_of:
            artifact["path"] = None
        else:
            source = Path(path)
            destination = child_path(staged, source.name)
            if source.suffix != ".webp" or source.is_symlink():
                raise ValueError("Unexpected staged image")
            shutil.copyfile(source, destination)
            artifact["path"] = str(destination)
        artifacts.append(artifact)
        send("artifact", artifact)

    result, error = {}, None
    watch = renderer is None and _EVENTS is not None
    if watch:
        faulthandler.dump_traceback_later(180,repeat=True)
    try:
        kwargs = {"force": force, "checkpoint_root": checkpoints}
        if renderer is None:
            kwargs["progress"] = progress
        result = (renderer or produce_lead)(model, cycle, hour, raw, set(existing), emit, **kwargs)
        if result.get("pending") and not result.get("complete"):
            error = {"type": "NotReady", "pending": True, "phase": "surface"}
            if result.get("pending_reason"):
                error["reason"] = result["pending_reason"]
    except Exception as exception:
        error = error_record(exception)
    finally:
        if watch:
            faulthandler.cancel_dump_traceback_later()
        if not raw_override:
            cleanup_raw(workspace)
        gc.collect()
    return {"artifacts": artifacts, "result": result, "error": error,
            "render_seconds": round(time.monotonic() - started, 3), "pid": os.getpid(),
            "prefetch_seconds": preparation["inputs"]["seconds"] if preparation else 0}


class LeadQueue:
    """Fair model rotation, with isolated CMC leads filling spare render slots."""

    def __init__(self, targets, existing, max_leads, force=False):
        self.queues = {}
        for target in targets:
            self.queues.setdefault(target["model"], deque()).append(target)
        self.order = deque(self.queues)
        self.counts = dict.fromkeys(self.queues, 0)
        self.existing, self.max_leads, self.force = existing, max_leads, force
        self.blocked, self.misses = set(), {}
        self.failures, self.model_attempts = {}, dict.fromkeys(self.queues, 0)
        self.attempts = 0
        self.retry_after = {}
        self.cmc_parallel_limit = 3
        self.model_parallel_limit = 1
        self.retry_seconds = 600
        self.readiness_gate = False
        self.ready_cycles = {(key.split("/")[0], key.split("/")[1]) for key in existing}

    def next(self, active, *, preparing=False):
        if self.attempts >= self.max_leads * len(self.queues) * 4:
            return None
        for _ in range(len(self.order)):
            model = self.order[0]
            self.order.rotate(-1)
            active_count = sum(item == model if isinstance(item,str) else item[0] == model for item in active)
            limit = self.cmc_parallel_limit if model == "cmc" else self.model_parallel_limit
            if preparing:
                limit += 1
            if (active_count >= limit or self.counts[model] + active_count >= self.max_leads
                    or self.model_attempts[model] >= self.max_leads * 4):
                continue
            for _ in range(len(self.queues[model])):
                target = self.queues[model].popleft()
                cycle, hour = target["cycle"], target["hour"]
                if (self.readiness_gate and (model, cycle) not in self.ready_cycles
                        and any(not isinstance(item, str) and item[:2] == (model, cycle) for item in active)):
                    self.queues[model].append(target)
                    continue
                if (model, cycle) in self.blocked or (model, cycle, hour) in active:
                    continue
                if max(self.retry_after.get((model,cycle),0),
                       self.retry_after.get((model,cycle,hour),0)) > time.monotonic():
                    self.queues[model].append(target)
                    continue
                if not self.force and policy.expected_keys(model, cycle, hour) <= self.existing:
                    continue
                self.attempts += 1
                self.model_attempts[model] += 1
                return target
        return None

    def finished(self, target, error):
        model, cycle = target["model"], target["cycle"]
        if not error:
            self.counts[model] += 1
        elif error["pending"]:
            key = model, cycle
            if error.get("phase") == "surface":
                self.retry_after[model,cycle,target["hour"]] = time.monotonic() + self.retry_seconds
                return
            self.misses[key] = self.misses.get(key, 0) + 1
            if target["hour"] == 0 or self.misses[key] >= 2:
                self.retry_after[key] = time.monotonic() + self.retry_seconds
        else:
            key = model, cycle
            self.failures[key] = self.failures.get(key, 0) + 1
            if self.failures[key] >= 2:
                self.blocked.add(key)

    def refresh(self, targets):
        self.queues = {}
        for target in targets:
            self.queues.setdefault(target["model"], deque()).append(target)
        self.order = deque(self.queues)
        now = time.monotonic()
        self.retry_after = {key:deadline for key,deadline in self.retry_after.items() if deadline > now}
        self.misses = {key:count for key,count in self.misses.items() if key in self.retry_after}

    def rendered(self, target, staged):
        self.existing.update(policy.key(target["model"], target["cycle"], target["hour"],
                                        item["product"], item["domain"]) for item in staged["artifacts"])


def execute_leads(queue, work, workers, consume, *, force=False, raw_override=None,
                  renderer=None, started=None, refresh=None, refresh_seconds=120,
                  can_schedule=None, idle_seconds=120, streaming=False, prefetch=False,
                  prefetcher=None, progress=None, on_idle=None, cleanup_staged=True):
    """consume runs only on the main process, including after renderer failures."""
    if workers not in (1, 2, 3, 4) or workers > 1 and raw_override:
        raise ValueError("Use one to four workers; a local cache override requires one")
    if renderer is not None and prefetcher is None:
        prefetch = False
    cleanup_models = set(queue.queues)
    memory = available_memory()
    queue.cmc_parallel_limit = cmc_worker_count(workers, memory)
    queue.model_parallel_limit = workers
    if prefetch:
        queue.retry_seconds = 120
        queue.readiness_gate = True
    context = multiprocessing.get_context("spawn")
    events = context.Queue(maxsize=workers * 8) if streaming else None
    cancelled = context.Event()
    try:
        if workers == 1 and not refresh and not streaming and not prefetch:
            while (target := queue.next(set())) is not None:
                if can_schedule and not can_schedule():
                    break
                if started:
                    started(target, 1)
                result = stage_lead(target, work, queue.existing, force, raw_override, renderer)
                consume(target, result)
                queue.finished(target, result["error"])
            return
        queue.existing = set(queue.existing)
        ready, stop = Queue(maxsize=workers), Event()

        def deliver(value):
            while not stop.is_set():
                try:
                    ready.put(value, timeout=.2)
                    return
                except Full:
                    pass

        def schedule():
            try:
                with ProcessPoolExecutor(max_workers=workers, mp_context=context,
                                         initializer=initialize_renderer,
                                         initargs=(events, cancelled)) as executor:
                    pending, active, prepared, announced, finished = {}, set(), deque(), set(), set()
                    next_refresh = time.monotonic() + refresh_seconds
                    last_progress = time.monotonic()

                    def identity(target):
                        return target["model"], target["cycle"], target["hour"]

                    def announce(target, artifacts):
                        fresh = []
                        for item in artifacts:
                            key = (*identity(target), item["product"], item["domain"])
                            if key not in announced:
                                announced.add(key)
                                fresh.append(item)
                        return fresh

                    def drain_events():
                        nonlocal last_progress
                        batches = {}
                        if events is None:
                            return
                        for _ in range(workers * 16):
                            try:
                                kind, target, payload = events.get_nowait()
                            except Empty:
                                break
                            if identity(target) in finished:
                                continue
                            if kind == "inputs":
                                deliver(("progress", target, {"event": "inputs_ready", **target, **payload}))
                            elif kind == "diagnostic":
                                deliver(("progress", target, {**target, **payload}))
                            else:
                                fresh = announce(target, [payload])
                                if fresh:
                                    batches.setdefault(identity(target), (target, []))[1].extend(fresh)
                        for target, artifacts in batches.values():
                            last_progress = time.monotonic()
                            staged = {"artifacts": artifacts, "result": {}, "error": None, "partial": True}
                            queue.rendered(target, staged)
                            deliver(("lead", target, staged))

                    def submit_render(target, preparation=None):
                        known = policy.expected_keys(target["model"], target["cycle"], target["hour"]) & queue.existing
                        future = executor.submit(stage_lead, target, work, known, force, None, renderer, preparation)
                        pending[future] = ("render", target)
                        if started:
                            started(target, len(pending))

                    try:
                        while not stop.is_set():
                            drain_events()
                            scheduling = can_schedule is None or can_schedule()
                            if refresh and scheduling and time.monotonic() >= next_refresh:
                                queue.refresh(refresh())
                                cleanup_models.update(queue.queues)
                                next_refresh = time.monotonic() + refresh_seconds
                            for _ in range(len(prepared)):
                                target, preparation = prepared.popleft()
                                limit = queue.cmc_parallel_limit if target["model"] == "cmc" else workers
                                running = sum(kind == "render" and item["model"] == target["model"]
                                              for kind, item in pending.values())
                                if (scheduling and (can_schedule is None or can_schedule())
                                        and len(pending) < workers and running < limit):
                                    submit_render(target, preparation)
                                else:
                                    prepared.append((target, preparation))
                            while (scheduling and (can_schedule is None or can_schedule()) and len(pending) < workers
                                   and (target := queue.next(active, preparing=prefetch)) is not None):
                                active.add(identity(target))
                                finished.discard(identity(target))
                                if prefetch:
                                    future = executor.submit(prefetcher or prepare_lead, target, work)
                                    pending[future] = ("prepare", target)
                                    deliver(("progress", target, {"event": "prefetch_started", **target}))
                                else:
                                    submit_render(target)
                            if not pending:
                                if not scheduling or (not prepared and
                                        (not refresh or time.monotonic() - last_progress >= idle_seconds)):
                                    break
                                stop.wait(.2)
                                continue
                            completed, _ = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                            drain_events()
                            for future in completed:
                                kind, target = pending.pop(future)
                                result = future.result()
                                if kind == "prepare" and not result["error"]:
                                    queue.ready_cycles.add((target["model"], target["cycle"]))
                                    prepared.append((target, result))
                                    last_progress = time.monotonic()
                                    deliver(("progress", target, {"event": "prefetch_ready", **target,
                                                                  **result.get("inputs", {})}))
                                    continue
                                active.remove(identity(target))
                                if kind == "prepare":
                                    result = {"artifacts": [], "result": {}, "error": result["error"],
                                              "render_seconds": 0, "pid": None}
                                queue.rendered(target, result)
                                queue.finished(target, result["error"])
                                if result["artifacts"]:
                                    last_progress = time.monotonic()
                                result["artifacts"] = announce(target, result["artifacts"])
                                finished.add(identity(target))
                                deliver(("lead", target, result))
                    finally:
                        for future in pending:
                            future.cancel()
            except BaseException as exception:
                deliver(("error", exception, None))
            finally:
                deliver(("done", None, None))

        scheduler = Thread(target=schedule, name="chart-render-scheduler")
        scheduler.start()
        try:
            while True:
                try:
                    kind, target, result = ready.get(timeout=.5)
                except Empty:
                    if on_idle:
                        on_idle()
                    if scheduler.is_alive():
                        continue
                    # Publication may have outlasted a scheduler that queued its final event.
                    try:
                        kind, target, result = ready.get_nowait()
                    except Empty:
                        raise RuntimeError("Renderer scheduler exited without a result") from None
                if kind == "done":
                    break
                if kind == "error":
                    raise target
                if kind == "progress":
                    if progress:
                        progress(result)
                else:
                    consume(target, result)
                if on_idle:
                    on_idle()
        finally:
            stop.set()
            cancelled.set()
            scheduler.join()
    finally:
        if events is not None:
            events.close()
            events.cancel_join_thread()
        # Executor shutdown has joined running renderers before staged files vanish.
        if cleanup_staged:
            cleanup_staging(work, cleanup_models)
