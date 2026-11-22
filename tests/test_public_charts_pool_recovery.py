import os
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures.process import BrokenProcessPool
from unittest.mock import patch

from public_charts.parallel import LeadQueue, execute_leads
from test_public_charts_parallel import sample_renderer, target


def crash_once_renderer(model, cycle, hour, root, existing, emit, **kwargs):
    work = Path(kwargs["checkpoint_root"]).parents[1]
    if hour == 6 and not (work / "crashed").exists():
        (work / "crashed").touch()
        os._exit(3)
    return sample_renderer(model, cycle, hour, root, existing, emit, **kwargs)


def always_crash_renderer(model, cycle, hour, root, existing, emit, **kwargs):
    os._exit(3)


def slow_renderer(model, cycle, hour, root, existing, emit, **kwargs):
    time.sleep(1.2)
    return sample_renderer(model, cycle, hour, root, existing, emit, **kwargs)


def crash_once_prepare(item, work):
    if not (Path(work) / "prepare-crashed").exists():
        (Path(work) / "prepare-crashed").touch()
        os._exit(4)
    return {"error": None, "inputs": {"seconds": .1}}


class PoolRecoveryTests(unittest.TestCase):
    def test_dead_renderer_rebuilds_the_pool_and_retries_its_lead(self):
        with tempfile.TemporaryDirectory() as directory:
            received, progress = [], []
            execute_leads(LeadQueue([target(hour=h) for h in (0, 6)], set(), 2), directory, 2,
                          lambda item, staged: received.append((item["hour"], staged["error"])),
                          renderer=crash_once_renderer, progress=progress.append)
            self.assertEqual(sorted(received), [(0, None), (6, None)])
            broken = [event for event in progress if event["event"] == "worker_pool_broken"]
            self.assertEqual(len(broken), 1)
            self.assertEqual((broken[0]["pool"], broken[0]["restart"]), ("render", 1))
            self.assertIn(target(hour=6), broken[0]["requeued"])
            self.assertIn(3, [worker["exitcode"] for worker in broken[0]["workers"]])

    def test_repeated_pool_failures_still_stop_the_run(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("public_charts.parallel.MAX_POOL_RESTARTS", 1), \
                self.assertRaisesRegex(BrokenProcessPool, "render pool broke 2 times"):
            execute_leads(LeadQueue([target()], set(), 1), directory, 2, lambda *_: None,
                          renderer=always_crash_renderer)

    def test_dead_preparation_worker_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            received, progress = [], []
            execute_leads(LeadQueue([target()], set(), 1), directory, 2,
                          lambda item, staged: received.append((item["hour"], staged["error"])),
                          renderer=sample_renderer, prefetch=True, prefetcher=crash_once_prepare,
                          progress=progress.append)
            self.assertEqual(received, [(0, None)])
            self.assertEqual([event["pool"] for event in progress if event["event"] == "worker_pool_broken"],
                             ["prepare"])

    def test_slow_tasks_are_reported_by_the_coordinator(self):
        with tempfile.TemporaryDirectory() as directory, patch("public_charts.parallel.SLOW_TASK_SECONDS", .4):
            progress = []
            execute_leads(LeadQueue([target()], set(), 1), directory, 2, lambda *_: None,
                          renderer=slow_renderer, progress=progress.append)
            slow = [event for event in progress if event["event"] == "task_slow"]
            self.assertTrue(slow)
            self.assertEqual((slow[0]["phase"], slow[0]["hour"]), ("render", 0))
            self.assertGreaterEqual(slow[0]["seconds"], .4)


if __name__ == "__main__":
    unittest.main()
