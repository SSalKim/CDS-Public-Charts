"""Bounded image IO; reservations and publication callbacks stay on the caller."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import local


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
