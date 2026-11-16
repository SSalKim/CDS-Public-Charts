"""Bounded renderer processes; only the caller owns Drive and Git publication."""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
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


def initialize_renderer():
    for name in list(os.environ):
        if name.startswith("PUBLIC_CHARTS_DRIVE_") or name in {"GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN"}:
            os.environ.pop(name, None)
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    if os.environ.get("MPLCONFIGDIR"):
        os.environ["MPLCONFIGDIR"] = str(Path(os.environ["MPLCONFIGDIR"]) / str(os.getpid()))


def stage_lead(target, work, existing, force=False, raw_override=None, renderer=None):
    """Keep validated WebPs until the coordinator uploads them; never write Drive."""
    from .producer import produce_lead
    from .sources import NotReady

    model, cycle, hour = target["model"], target["cycle"], target["hour"]
    expected = policy.expected_keys(model, cycle, hour)
    workspace = model_workspace(work, model)
    raw = Path(raw_override) if raw_override else child_path(workspace, "raw-cache")
    checkpoints = Path(raw_override) / "checkpoints" if raw_override else child_path(work, "raw-cache", "checkpoints")
    staged = child_path(workspace, "staged", cycle, f"{hour:03}")
    staged.mkdir(parents=True, exist_ok=True)
    artifacts = []
    started = time.monotonic()

    def emit(product, domain, path, alias_of=None):
        if policy.key(model, cycle, hour, product, domain) not in expected:
            raise ValueError("Unexpected staged product")
        artifact = {"product": product, "domain": domain, "alias_of": alias_of}
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

    result, error = {}, None
    try:
        result = (renderer or produce_lead)(model, cycle, hour, raw, set(existing), emit,
                                           force=force, checkpoint_root=checkpoints)
    except Exception as exception:
        error = {"type": type(exception).__name__, "pending": isinstance(exception, NotReady)}
        if not error["pending"]:
            frames = traceback.extract_tb(exception.__traceback__)
            error["location"] = [{"file": Path(frame.filename).name, "line": frame.lineno,
                                   "function": frame.name} for frame in frames
                                  if Path(frame.filename).parent.name == "public_charts"][-3:]
            response = getattr(exception, "response", None)
            if response is not None:
                error["http_status"] = response.status_code
                error["source_host"] = urlsplit(response.url or "").hostname
    finally:
        if not raw_override:
            cleanup_raw(workspace)
    return {"artifacts": artifacts, "result": result, "error": error,
            "render_seconds": round(time.monotonic() - started, 3), "pid": os.getpid()}


class LeadQueue:
    """One active lead per model protects cumulative checkpoint ordering."""

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

    def next(self, active):
        if self.attempts >= self.max_leads * len(self.queues) * 4:
            return None
        for _ in range(len(self.order)):
            model = self.order[0]
            self.order.rotate(-1)
            if (model in active or self.counts[model] >= self.max_leads
                    or self.model_attempts[model] >= self.max_leads * 4):
                continue
            while self.queues[model]:
                target = self.queues[model].popleft()
                cycle, hour = target["cycle"], target["hour"]
                if (model, cycle) in self.blocked:
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
            self.misses[key] = self.misses.get(key, 0) + 1
            if target["hour"] == 0 or self.misses[key] >= 2:
                self.blocked.add(key)
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
        self.blocked = {key for key in self.blocked if self.failures.get(key, 0) >= 2}
        self.misses.clear()

    def rendered(self, target, staged):
        self.existing.update(policy.key(target["model"], target["cycle"], target["hour"],
                                        item["product"], item["domain"]) for item in staged["artifacts"])


def execute_leads(queue, work, workers, consume, *, force=False, raw_override=None,
                  renderer=None, started=None, refresh=None, refresh_seconds=120,
                  can_schedule=None, idle_seconds=120):
    """consume runs only on the main process, including after renderer failures."""
    if workers not in (1, 2, 3, 4) or workers > 1 and raw_override:
        raise ValueError("Use one to four workers; a local cache override requires one")
    cleanup_models = set(queue.queues)
    try:
        if workers == 1 and not refresh:
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
                with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                         initializer=initialize_renderer) as executor:
                    pending, active = {}, set()
                    next_refresh = time.monotonic() + refresh_seconds
                    last_progress = time.monotonic()
                    try:
                        while not stop.is_set():
                            scheduling = can_schedule is None or can_schedule()
                            if refresh and scheduling and time.monotonic() >= next_refresh:
                                queue.refresh(refresh())
                                cleanup_models.update(queue.queues)
                                next_refresh = time.monotonic() + refresh_seconds
                            while scheduling and (can_schedule is None or can_schedule()) and len(pending) < workers and (target := queue.next(active)) is not None:
                                known = policy.expected_keys(target["model"], target["cycle"], target["hour"]) & queue.existing
                                future = executor.submit(stage_lead, target, work, known, force, None, renderer)
                                pending[future] = target
                                active.add(target["model"])
                                if started:
                                    started(target, len(pending))
                            if not pending:
                                if not refresh or not scheduling or time.monotonic() - last_progress >= idle_seconds:
                                    break
                                stop.wait(.2)
                                continue
                            completed, _ = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                            for future in completed:
                                target = pending.pop(future)
                                active.remove(target["model"])
                                result = future.result()
                                queue.rendered(target, result)
                                queue.finished(target, result["error"])
                                if result["artifacts"]:
                                    last_progress = time.monotonic()
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
                    if not scheduler.is_alive():
                        raise RuntimeError("Renderer scheduler exited without a result")
                    continue
                if kind == "done":
                    break
                if kind == "error":
                    raise target
                consume(target, result)
        finally:
            stop.set()
            scheduler.join()
    finally:
        # Executor shutdown has joined running renderers before staged files vanish.
        for model in cleanup_models:
            staged = child_path(model_workspace(work, model), "staged")
            if staged.exists():
                shutil.rmtree(staged)
