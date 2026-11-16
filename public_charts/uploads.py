"""Bounded image IO; reservations and publication callbacks stay on the caller."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import deque
from threading import local
import time


class UploadBudgetExceeded(OverflowError):
    def __init__(self, size):
        super().__init__("Image budget reached; publish retention cleanup before continuing")
        self.size = size


class UploadPool:
    def __init__(self, store, workers=1):
        if workers not in (1, 2, 3, 4):
            raise ValueError("Use one to four upload workers")
        self.store, self.workers = store, workers
        self.local = local()
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chart-upload") if workers > 1 else None

    def upload(self, item):
        if self.executor is None:
            return self.store.upload_image(item["path"], item["key"])
        if not hasattr(self.local, "store"):
            self.local.store = self.store.new_upload_worker()
        return self.local.store.upload_image(item["path"], item["key"])

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
        while self.waiting and len(self.pending) < self.pool.workers and self.failure is None:
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
