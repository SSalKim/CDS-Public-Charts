from threading import Barrier, Lock, get_ident
import time
import unittest

from public_charts.uploads import UploadPool, UploadBudgetExceeded


class Worker:
    def __init__(self, parent):
        self.parent = parent

    def upload_image(self, path, key):
        with self.parent.lock:
            self.parent.active += 1
            self.parent.maximum = max(self.parent.maximum, self.parent.active)
        try:
            self.parent.barrier.wait(timeout=10)
            time.sleep(.01)
            if key == self.parent.fail:
                raise RuntimeError("upload failed")
            return {"key": key}
        finally:
            with self.parent.lock:
                self.parent.active -= 1


class Store:
    def __init__(self, workers, fail=None):
        self.barrier, self.lock = Barrier(workers), Lock()
        self.active = self.maximum = 0
        self.threads, self.fail = [], fail

    def new_upload_worker(self):
        self.threads.append(get_ident())
        return Worker(self)


class UploadTests(unittest.TestCase):
    def test_three_independent_upload_threads_but_callbacks_stay_on_coordinator(self):
        store, parent, recorded = Store(3), get_ident(), []
        pool = UploadPool(store, 3)
        self.addCleanup(pool.close)
        items = [{"key": str(i), "path": str(i)} for i in range(6)]

        def record(item, info):
            self.assertEqual(get_ident(), parent)
            self.assertEqual(item["key"], info["key"])
            recorded.append(item["key"])

        pool.run(items, lambda item: None, lambda item: None, record)
        self.assertEqual(store.maximum, 3)
        self.assertEqual(len(set(store.threads)), 3)
        self.assertEqual(set(recorded), {str(i) for i in range(6)})

    def test_one_failure_still_records_other_inflight_successes(self):
        store, recorded = Store(3, fail="1"), []
        pool = UploadPool(store, 3)
        self.addCleanup(pool.close)
        with self.assertRaisesRegex(RuntimeError, "upload failed"):
            pool.run([{"key": str(i), "path": str(i)} for i in range(3)], lambda item: None,
                     lambda item: None, lambda item, info: recorded.append(info["key"]))
        self.assertEqual(set(recorded), {"0", "2"})
        self.assertEqual(store.active, 0)

    def test_budget_reservations_never_oversubscribe_and_preserve_completed_files(self):
        store, recorded = Store(2), []
        pool = UploadPool(store, 3)
        self.addCleanup(pool.close)
        amounts = {"used": 0, "reserved": 0}

        def reserve(item):
            if amounts["used"] + amounts["reserved"] + 10 > 20:
                raise UploadBudgetExceeded(10)
            amounts["reserved"] += 10

        def release(item):
            amounts["reserved"] -= 10

        def record(item, info):
            amounts["used"] += 10
            recorded.append(info["key"])

        with self.assertRaises(UploadBudgetExceeded):
            pool.run([{"key": str(i), "path": str(i)} for i in range(3)], reserve, release, record)
        self.assertEqual(len(recorded), 2)
        self.assertEqual(amounts, {"used": 20, "reserved": 0})


if __name__ == "__main__":
    unittest.main()
