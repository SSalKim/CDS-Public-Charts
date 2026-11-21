"""Bounded image IO; reservations and publication callbacks stay on the caller."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import deque
from threading import local
import time


class UploadBudgetExceeded(OverflowError):
    def __init__(self, size):
        super().__init__("Image budget reached; publish retention cleanup before continuing")
        self.size = size


def _hash_file(path):
    import hashlib
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk); size += len(chunk)
    return digest.hexdigest(), size


class UploadPool:
    def __init__(self, store, workers=1, *, mirror=None, r2_only=False):
        if workers not in (1, 2, 3, 4):
            raise ValueError("Use one to four upload workers")
        self.store, self.workers = store, workers
        # Optional R2 copy made on the same IO thread, after the Drive upload succeeded.
        self.mirror = mirror
        # With r2_only the image goes to R2 alone; Drive is used only if that upload fails.
        self.r2_only = r2_only
        self.drive_fallbacks = 0
        self.local = local()
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chart-upload") if workers > 1 else None

    def upload(self, item):
        if self.r2_only and self.mirror is not None:
            from . import r2, policy
            sha256, size = _hash_file(item["path"])
            name = self.mirror.mirror(item["path"], item["key"], sha256)
            if name:
                model, cycle = policy.KEY_PATTERN.fullmatch(item["key"]).groups()[:2]
                return {"file_id": r2.r2_file_id(name), "url": r2.public_url(model, cycle, name), "size": size,
                        "sha256": sha256, "key": item["key"], "r2": name, "storage_account": r2.R2_ACCOUNT}
            self.drive_fallbacks += 1
            return self._drive(item)
        info = self._drive(item)
        if self.mirror is not None:
            name = self.mirror.mirror(item["path"], item["key"], info["sha256"])
            if name:
                info = {**info, "r2": name}
        return info

    def _drive(self, item):
        if self.executor is None:
            info = self.store.upload_image(item["path"], item["key"])
        else:
            if not hasattr(self.local, "store"):
                self.local.store = self.store.new_upload_worker()
            info = self.local.store.upload_image(item["path"], item["key"])
        return info

    def run(self, items, reserve, release, record):
        """Drain successful in-flight uploads even when another image fails."""
        if self.executor is None:
            for item in items:
                reserve(item)
                try:
                    info = self.upload(item)
                finally:
                    release(item)
                record(item, info)
            return
        remaining, pending, failure = list(reversed(items)), {}, None
        try:
            while remaining or pending:
                while remaining and len(pending) < self.workers and failure is None:
                    item = remaining[-1]
                    try:
                        reserve(item)
                    except UploadBudgetExceeded as exception:
                        if not pending:
                            failure = exception
                        break
                    remaining.pop()
                    pending[self.executor.submit(self.upload, item)] = item
                if not pending:
                    break
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    item = pending.pop(future)
                    release(item)
                    try:
                        record(item, future.result())
                    except Exception as exception:
                        failure = failure or exception
                if failure:
                    remaining.clear()
            if failure:
                raise failure
        finally:
            for future in pending:
                future.cancel()
            if pending:
                wait(pending)

    def close(self):
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)


class StreamingUploads:
    """One coordinator feeds all leads into the same bounded upload pool."""

    def __init__(self, pool, reserve, release, record):
        self.pool, self.reserve, self.release, self.record = pool, reserve, release, record
        self.waiting, self.pending = deque(), {}
        self.failure = None

    def enqueue(self, items):
        self.waiting.extend(items)

    def __bool__(self):
        return bool(self.waiting or self.pending)

    def upload(self, item):
        started = time.monotonic()
        return self.pool.upload(item), round(time.monotonic() - started, 3)

    def poll(self, *, block=False):
        if self.failure:
            self.waiting.clear()
        # Keep the IO threads fed while the coordinator saves/publishes a manifest.
        while self.waiting and len(self.pending) < self.pool.workers * 8 and self.failure is None:
            item = self.waiting[0]
            try:
                self.reserve(item)
            except Exception as exception:
                self.failure = exception
                self.waiting.clear()
                break
            self.waiting.popleft()
            if self.pool.executor is None:
                try:
                    info, seconds = self.upload(item)
                    self.record({**item, "upload_seconds": seconds}, info)
                except Exception as exception:
                    self.failure = exception
                finally:
                    self.release(item)
                break
            self.pending[self.pool.executor.submit(self.upload, item)] = item
        if self.pending:
            done, _ = wait(self.pending, timeout=.2 if block else 0, return_when=FIRST_COMPLETED)
            for future in done:
                item = self.pending.pop(future)
                self.release(item)
                try:
                    info, seconds = future.result()
                    self.record({**item, "upload_seconds": seconds}, info)
                except Exception as exception:
                    self.failure = self.failure or exception
        if self.failure and not self.pending:
            raise self.failure

    def drain(self):
        while self:
            self.poll(block=True)
