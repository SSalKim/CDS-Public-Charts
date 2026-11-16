from threading import Barrier, Event, Lock, get_ident
import time
import unittest

from public_charts.uploads import UploadPool, UploadBudgetExceeded, StreamingUploads


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
    def test_bounded_uploads_keep_running_while_coordinator_is_publishing(self):
        gate, completed, lock = Event(), Event(), Lock()
        counts = {"active": 0, "peak": 0, "finished": 0}
        parent, reserved, released, recorded = get_ident(), [], [], []

        class IndependentWorker:
            def new_upload_worker(self):
                return self

            def upload_image(self, path, key):
                with lock:
                    counts["active"] += 1
                    counts["peak"] = max(counts["peak"], counts["active"])
                if not gate.wait(10):
                    raise TimeoutError("Coordinator did not begin publication")
                time.sleep(.01)
                with lock:
                    counts["active"] -= 1
                    counts["finished"] += 1
                    if counts["finished"] == 24:
                        completed.set()
                return {"key": key}

        pool = UploadPool(IndependentWorker(), 3)
        self.addCleanup(pool.close)
        self.addCleanup(gate.set)
        def record(item, info):
            self.assertEqual(get_ident(), parent)
            recorded.append(info["key"])
        stream = StreamingUploads(pool, lambda item: reserved.append(item["key"]),
                                  lambda item: released.append(item["key"]), record)
        stream.enqueue([{"key": str(i), "path": str(i)} for i in range(27)])
        stream.poll()
        self.assertEqual(len(reserved), 24)
        self.assertEqual(len(stream.waiting), 3)
        gate.set()
        self.assertTrue(completed.wait(10))
        self.assertEqual(recorded, [])
        stream.drain()
        self.assertEqual(counts["peak"], 3)
        self.assertEqual(len(recorded), 27)
        self.assertEqual(set(reserved), set(released))

    def test_streaming_uses_all_upload_slots_across_different_leads(self):
        store, recorded, parent = Store(3), [], get_ident()
        pool = UploadPool(store, 3)
        self.addCleanup(pool.close)
        def record(item, info):
            self.assertEqual(get_ident(), parent)
            self.assertGreater(item["upload_seconds"], 0)
            recorded.append(info["key"])
        stream = StreamingUploads(pool, lambda item: None, lambda item: None, record)
        for i in range(3):
            stream.enqueue([{"key": str(i), "path": str(i)}])
            stream.poll()
        stream.drain()
        self.assertEqual(store.maximum, 3)
        self.assertEqual(set(recorded), {"0", "1", "2"})

    def test_streaming_failure_drains_verified_inflight_images(self):
        store, recorded, released = Store(3, fail="1"), [], []
        pool = UploadPool(store, 3)
        self.addCleanup(pool.close)
        stream = StreamingUploads(pool, lambda item: None, lambda item: released.append(item["key"]),
                                  lambda item, info: recorded.append(info["key"]))
        stream.enqueue([{"key": str(i), "path": str(i)} for i in range(3)])
        with self.assertRaisesRegex(RuntimeError, "upload failed"):
            stream.drain()
        self.assertEqual(set(recorded), {"0", "2"})
        self.assertEqual(set(released), {"0", "1", "2"})
        self.assertEqual(store.active, 0)

    def test_streaming_budget_does_not_discard_inflight_successes(self):
        store, recorded = Store(2), []
        pool = UploadPool(store, 3)
        self.addCleanup(pool.close)
        used = {"bytes": 0, "reserved": 0}
        def reserve(item):
            if used["bytes"] + used["reserved"] + 10 > 20:
                raise UploadBudgetExceeded(10)
            used["reserved"] += 10
        def record(item, info):
            used["bytes"] += 10
            recorded.append(info["key"])
        stream = StreamingUploads(pool, reserve,
                                  lambda item: used.update(reserved=used["reserved"]-10), record)
        stream.enqueue([{"key": str(i), "path": str(i)} for i in range(3)])
        with self.assertRaises(UploadBudgetExceeded):
            stream.drain()
        self.assertEqual(used, {"bytes": 20, "reserved": 0})
        self.assertEqual(len(recorded), 2)

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
