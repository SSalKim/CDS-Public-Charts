import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import threading
from queue import Empty, Queue
import unittest
from unittest.mock import patch

from PIL import Image

from public_charts import policy
from public_charts.parallel import LeadQueue, child_path, cleanup_raw, execute_leads, stage_lead
from public_charts.resources import WorkerMemoryBudget
from public_charts.sources import NotReady
from public_charts_publish import MANIFEST, publish_manifest
from public_charts_run import run
from test_public_charts_publish import git, make_repository

CYCLE = "2026111512"


def target(model="aifs", hour=0, cycle=CYCLE):
    return {"model": model, "cycle": cycle, "hour": hour}


def sample_renderer(model, cycle, hour, root, existing, emit, *, force=False, checkpoint_root=None):
    from public_charts.producer import encode
    root = Path(root)
    image = Image.new("RGB", (64, 48), "white")
    for x in range(32):
        for y in range(48):
            image.putpixel((x, y), (x * 7, y * 5, 80))
    path = root / "images" / f"{model}-{hour}.webp"
    encode(image, path)
    emit("surfce", "hkor", path)
    path.unlink()
    emit("acptot", "hkor", None, alias_of=("surfce", "hkor"))
    checkpoint = Path(checkpoint_root) / cycle / model / "sample.txt"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text(str(hour), encoding="utf-8")
    return {"complete": True}


def synchronized_renderer(model, cycle, hour, root, existing, emit, **kwargs):
    work = Path(kwargs["checkpoint_root"]).parents[1]
    if model == "gfs":
        (work / "gfs-started").touch()
        waiting = work / "first-published"
    else:
        waiting = work / "gfs-started"
    deadline = time.monotonic() + 45
    while not waiting.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Other renderer/coordinator did not progress")
        time.sleep(0.02)
    result = sample_renderer(model, cycle, hour, root, existing, emit, **kwargs)
    if model == "gfs":
        (work / "gfs-finished").touch()
    result["secret_visible"] = any(name in os.environ for name in (
        "PUBLIC_CHARTS_DRIVE_REFRESH_TOKEN", "PUBLIC_CHARTS_DRIVE2_REFRESH_TOKEN"))
    return result


def ahead_renderer(model, cycle, hour, root, existing, emit, **kwargs):
    result = sample_renderer(model, cycle, hour, root, existing, emit, **kwargs)
    if hour == 6:
        (Path(kwargs["checkpoint_root"]).parents[1] / "next-rendered").touch()
    return result


def every_product_renderer(model, cycle, hour, root, existing, emit, **kwargs):
    from public_charts.producer import encode
    path = Path(root) / "images/sample.webp"
    image = Image.new("RGB", (32, 32), "white")
    for x in range(16):
        for y in range(32):
            image.putpixel((x, y), (0, 0, 0))
    encode(image, path)
    for key in sorted(policy.expected_keys(model, cycle, hour) - existing):
        product, domain = key.split("/")[-2:]
        emit(product, domain, path)
    path.unlink()
    return {"complete": True}


def diagnostic_renderer(model, cycle, hour, root, existing, emit, **kwargs):
    from public_charts.parallel import _EVENTS
    _EVENTS.put(("diagnostic", {"model": model, "cycle": cycle, "hour": hour},
                 {"event": "product_started", "product": "gph500", "domain": "asia"}))
    return every_product_renderer(model, cycle, hour, root, existing, emit, **kwargs)


def concurrent_cmc_renderer(model, cycle, hour, root, existing, emit, *, checkpoint_root=None, **kwargs):
    import numpy as np
    from public_charts.extended_sources import ExtraFields

    work = Path(checkpoint_root).parents[1]
    marker = Path(root) / "fields" / f"{hour}.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    (work / f"cmc-started-{hour}").touch()
    deadline = time.monotonic() + 30
    expected_file=work / "expected-renderers"
    expected=int(expected_file.read_text()) if expected_file.exists() else 3
    while len(list(work.glob("cmc-started-*"))) < expected:
        if time.monotonic() > deadline:
            raise TimeoutError("CMC leads did not start concurrently")
        time.sleep(.02)
    extra = ExtraFields(root, model, cycle)
    extra.checkpoints = Path(checkpoint_root) / cycle / model
    with extra.checkpoint_lock():
        total = extra.checkpoint("sample.npz")
        value = int(total["values"][0]) if total else 0
        time.sleep(.1)
        extra.save_checkpoint("sample.npz", {"values": np.array([value + hour])})
    if not marker.exists():
        raise RuntimeError("Another lead removed this lead's scratch files")
    return every_product_renderer(model, cycle, hour, root, existing, emit, **kwargs)


def streaming_renderer(model, cycle, hour, root, existing, emit, **kwargs):
    work = Path(kwargs["checkpoint_root"]).parents[1]
    result = sample_renderer(model, cycle, hour, root, existing, emit, **kwargs)
    deadline = time.monotonic() + 30
    while not (work / "image-public").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Completed image waited for the whole lead")
        time.sleep(.02)
    (work / "lead-finished").touch()
    return result


def lookahead_prepare(item, work):
    work = Path(work)
    if item["hour"] == 6:
        deadline = time.monotonic() + 30
        while not (work / "current-rendering").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Rendering did not overlap lookahead")
            time.sleep(.02)
        (work / "next-inputs-ready").touch()
    return {"error": None, "inputs": {"seconds": .1}}


def prepare_then_stop(item, work):
    (Path(work) / "memory-guard").touch()
    return {"error": None, "inputs": {"seconds": .1}}


def lookahead_renderer(model, cycle, hour, root, existing, emit, **kwargs):
    work = Path(kwargs["checkpoint_root"]).parents[1]
    if hour == 0:
        (work / "current-rendering").touch()
        deadline = time.monotonic() + 30
        while not (work / "next-inputs-ready").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Next input was not prefetched while rendering")
            time.sleep(.02)
    return every_product_renderer(model, cycle, hour, root, existing, emit, **kwargs)


class QueueTests(unittest.TestCase):
    def test_unseen_cycle_has_one_readiness_probe_instead_of_filling_every_slot(self):
        queue = LeadQueue([target("gfs", h) for h in (0, 3, 6)] + [target("cmc")], set(), 10)
        queue.model_parallel_limit = 4
        queue.readiness_gate = True
        self.assertEqual(queue.next(set(), preparing=True), target("gfs", 0))
        self.assertEqual(queue.next({("gfs", CYCLE, 0)}, preparing=True), target("cmc"))
        self.assertIsNone(queue.next({("gfs", CYCLE, 0), ("cmc", CYCLE, 0)}, preparing=True))
        queue.refresh([target("gfs", h) for h in (0, 3, 6)])
        queue.ready_cycles.add(("gfs", CYCLE))
        self.assertEqual(queue.next({("gfs", CYCLE, 0)}, preparing=True), target("gfs", 3))

    def test_same_model_can_borrow_all_idle_worker_slots(self):
        queue = LeadQueue([target("gfs", h) for h in (0, 3, 6, 9, 12)], set(), 10)
        queue.model_parallel_limit = 4
        active = set()
        for hour in (0, 3, 6, 9):
            self.assertEqual(queue.next(active), target("gfs", hour))
            active.add(("gfs", CYCLE, hour))
        self.assertIsNone(queue.next(active))

    def test_late_surface_input_does_not_block_the_next_upper_air_lead(self):
        queue = LeadQueue([target("gfs", 24), target("gfs", 27)], set(), 10)
        queue.finished(target("gfs", 24), {"type": "NotReady", "pending": True, "phase": "surface"})
        self.assertEqual(queue.next(set()), target("gfs", 27))
        self.assertEqual(queue.counts["gfs"], 0)

    def test_model_round_robin_and_same_model_serialization(self):
        queue = LeadQueue([target("aifs", 0), target("aifs", 6), target("gfs", 0)], set(), 2)
        self.assertEqual(queue.next(set()), target("aifs", 0))
        self.assertEqual(queue.next({"aifs"}), target("gfs", 0))
        self.assertIsNone(queue.next({"aifs", "gfs"}))
        self.assertEqual(queue.next({"gfs"}), target("aifs", 6))

    def test_not_ready_zero_blocks_only_that_cycle(self):
        queue = LeadQueue([target(), target(hour=6), target(cycle="2026111506"), target("gfs")], set(), 2)
        first = queue.next(set())
        queue.finished(first, {"type": "NotReady", "pending": True})
        self.assertEqual(queue.next(set()), target("gfs"))
        self.assertEqual(queue.next(set()), target(cycle="2026111506"))

    def test_skip_fresh_and_stop_at_successful_lead_quota(self):
        fresh = policy.expected_keys("aifs", CYCLE, 0)
        queue = LeadQueue([target(), target(hour=6), target(hour=12)], fresh, 1)
        item = queue.next(set())
        self.assertEqual(item["hour"], 6)
        queue.finished(item, None)
        self.assertIsNone(queue.next(set()))

    def test_two_missing_nonzero_leads_block_cycle(self):
        queue = LeadQueue([target(hour=6), target(hour=12), target(hour=18)], set(), 2)
        for _ in range(2):
            item = queue.next(set())
            queue.finished(item, {"type": "NotReady", "pending": True})
        self.assertIsNone(queue.next(set()))

    def test_distinct_real_errors_do_not_block_a_healthy_following_lead(self):
        queue = LeadQueue([target(hour=72), target(hour=78), target(hour=84),
                           target(cycle="2026111506")], set(), 2)
        error = {"type": "ValueError", "pending": False}
        for _ in range(2):
            queue.finished(queue.next(set()), error)
        self.assertEqual(queue.next(set()), target(hour=84))
        self.assertEqual(queue.counts["aifs"], 0)

    def test_same_failed_lead_is_quarantined_without_blocking_its_cycle(self):
        targets=[target(hour=0),target(hour=6)]
        queue=LeadQueue(targets,set(),10)
        error={"type":"IndexError","pending":False}
        for _ in range(2):
            queue.finished(targets[0],error)
        queue.refresh(targets)
        self.assertEqual(queue.next(set()),targets[1])

    def test_three_distinct_broken_leads_still_protect_the_bad_cycle(self):
        queue=LeadQueue([target(hour=h) for h in (0,6,12,18)]+[target("gfs")],set(),10)
        for hour in (0,6,12):
            queue.finished(target(hour=hour),{"type":"ValueError","pending":False})
        self.assertEqual(queue.next(set()),target("gfs"))

    def test_monitor_mode_defers_missing_lead_not_the_whole_cycle(self):
        targets=[target(hour=h) for h in (0,6)]
        queue=LeadQueue(targets,set(),10)
        queue.source_monitor=True
        for item in targets:
            queue.source_observed(item)
        queue.finished(targets[0],{"type":"NotReady","pending":True})
        self.assertEqual(queue.next(set()),targets[1])

    def test_pending_cycle_backoff_survives_plan_refresh_and_expires(self):
        targets = [target(), target(hour=6), target(cycle="2026111506")]
        with patch("public_charts.parallel.time.monotonic", return_value=100) as clock:
            queue = LeadQueue(targets, set(), 10)
            queue.finished(queue.next(set()), {"type": "NotReady", "pending": True})
            clock.return_value = 220
            queue.refresh(targets)
            self.assertEqual(queue.next(set()), target(cycle="2026111506"))
            self.assertIsNone(queue.next(set()))
            clock.return_value = 701
            queue.refresh(targets)
            self.assertEqual(queue.next(set()), target())

    def test_cmc_uses_spare_slots_without_repeating_active_leads_after_refresh(self):
        targets = [target("cmc", h) for h in (72, 75, 78, 81)]
        queue = LeadQueue(targets, set(), 10)
        queue.model_parallel_limit = 3
        active = set()
        for expected in targets[:3]:
            queue.refresh(targets)
            self.assertEqual(queue.next(active), expected)
            active.add(("cmc", CYCLE, expected["hour"]))
        self.assertIsNone(queue.next(active))
        active.remove(("cmc", CYCLE, 72))
        queue.finished(targets[0], None)
        queue.refresh(targets)
        queue.existing.update(policy.expected_keys("cmc", CYCLE, 72))
        self.assertEqual(queue.next(active), targets[3])

    def test_parallel_cmc_respects_total_quota_and_other_model_rotation(self):
        queue = LeadQueue([target("cmc", h) for h in (72, 75, 78)] + [target("gfs")], set(), 2)
        queue.model_parallel_limit = 4
        self.assertEqual(queue.next(set()), target("cmc", 72))
        active = {("cmc", CYCLE, 72)}
        self.assertEqual(queue.next(active), target("gfs"))
        active.add(("gfs", CYCLE, 0))
        self.assertEqual(queue.next(active), target("cmc", 75))
        active.add(("cmc", CYCLE, 75))
        self.assertIsNone(queue.next(active))


class StagingTests(unittest.TestCase):
    def test_live_prefetch_rechecks_pending_inputs_after_one_minute(self):
        queue = LeadQueue([target()],set(),2)
        with tempfile.TemporaryDirectory() as directory:
            execute_leads(queue,Path(directory),1,lambda *_:None,prefetch=True,can_schedule=lambda:False)
        self.assertEqual(queue.retry_seconds,60)
        self.assertTrue(queue.readiness_gate)

    def test_product_progress_is_forwarded_without_becoming_an_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            progress, received = [], []
            execute_leads(LeadQueue([target()], set(), 1), directory, 2,
                          lambda item, staged: received.extend(staged["artifacts"]),
                          renderer=diagnostic_renderer, streaming=True, progress=progress.append)
            self.assertTrue(received)
            self.assertEqual(progress, [{**target(), "event": "product_started", "product": "gph500", "domain": "asia"}])

    def test_cumulative_budget_defers_only_surface_and_keeps_its_reason(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("public_charts.producer.produce_lead", return_value={"complete": False,
                      "pending": "cumulative_work_budget", "pending_reason": "cumulative_work_budget"}):
            staged = stage_lead(target(), directory, set())
            self.assertEqual(staged["error"], {"type": "NotReady", "pending": True,
                                              "phase": "surface", "reason": "cumulative_work_budget"})

    def test_completion_queued_during_idle_publication_is_not_lost(self):
        class TimeoutOnce(Queue):
            timed_out = False
            def get(self, block=True, timeout=None):
                if timeout is not None and not self.timed_out:
                    self.timed_out = True
                    raise Empty
                return super().get(block, timeout)
        def publish_while_scheduler_finishes():
            for thread in threading.enumerate():
                if thread.name == "chart-render-scheduler":
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
        with tempfile.TemporaryDirectory() as directory, patch("public_charts.parallel.Queue", TimeoutOnce):
            execute_leads(LeadQueue([], set(), 1), directory, 2, lambda *args: None,
                          on_idle=publish_while_scheduler_finishes)

    def test_error_queued_during_idle_publication_keeps_its_original_cause(self):
        class TimeoutOnce(Queue):
            timed_out = False
            def get(self, block=True, timeout=None):
                if timeout is not None and not self.timed_out:
                    self.timed_out = True
                    raise Empty
                return super().get(block, timeout)
        def publish_while_scheduler_finishes():
            for thread in threading.enumerate():
                if thread.name == "chart-render-scheduler":
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
        def broken_guard():
            raise ValueError("original scheduler failure")
        with tempfile.TemporaryDirectory() as directory, patch("public_charts.parallel.Queue", TimeoutOnce), \
                self.assertRaisesRegex(ValueError, "original scheduler failure"):
            execute_leads(LeadQueue([], set(), 1), directory, 2, lambda *args: None,
                          on_idle=publish_while_scheduler_finishes, can_schedule=broken_guard)

    def test_image_is_delivered_before_its_renderer_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            work, partials, finals = Path(directory), [], []
            def consume(item, staged):
                if staged.get("partial"):
                    if not partials:
                        self.assertFalse((work / "lead-finished").exists())
                    self.assertTrue(Path(staged["artifacts"][0]["path"]).is_file())
                    partials.extend(staged["artifacts"])
                    (work / "image-public").touch()
                else:
                    self.assertIsNone(staged["error"])
                    finals.append(staged)
            execute_leads(LeadQueue([target()], set(), 1), work, 1, consume,
                          renderer=streaming_renderer, streaming=True)
            self.assertEqual(len(partials), 2)
            self.assertEqual(len(finals), 1)
            self.assertEqual(finals[0]["artifacts"], [])

    def test_next_lead_is_prefetched_while_current_lead_is_rendered(self):
        with tempfile.TemporaryDirectory() as directory:
            received = []
            execute_leads(LeadQueue([target(hour=h) for h in (0, 6)], set(), 2), directory, 2,
                          lambda item, staged: received.append((item["hour"], staged["error"])),
                          renderer=lookahead_renderer, prefetch=True, prefetcher=lookahead_prepare)
            self.assertEqual(sorted(received), [(0, None), (6, None)])

    def test_gfs_leads_use_isolated_raw_caches_and_one_locked_prefix(self):
        from public_charts.extended_sources import ExtraFields
        with tempfile.TemporaryDirectory() as directory:
            work, received = Path(directory), []
            (work / "expected-renderers").write_text("2")
            execute_leads(LeadQueue([target("gfs", h) for h in (3, 6)], set(), 10), work, 2,
                          lambda item, staged: received.append(staged["error"]), renderer=concurrent_cmc_renderer)
            self.assertEqual(received, [None, None])
            extra = ExtraFields(work, "gfs", CYCLE)
            extra.checkpoints = work / "raw-cache/checkpoints" / CYCLE / "gfs"
            self.assertEqual(extra.checkpoint("sample.npz")["values"].tolist(), [9])

    def test_cmc_backlog_can_fill_all_four_slots_and_preserves_shared_checkpoints(self):
        from public_charts.extended_sources import ExtraFields
        with tempfile.TemporaryDirectory() as directory:
            work, received, starts = Path(directory), [], []
            (work / "expected-renderers").write_text("4")
            pending = [target(model) for model in ("aifs", "gfs", "aigfs")]
            queue = LeadQueue(pending + [target("cmc", h) for h in (72, 75, 78, 81)], set(), 10)
            for item in pending:
                queue.finished(item, {"type": "NotReady", "pending": True})

            def consume(item, staged):
                self.assertIsNone(staged["error"])
                self.assertEqual(item["model"], "cmc")
                self.assertTrue(all(Path(row["path"]).exists() for row in staged["artifacts"]))
                received.append(staged["pid"])

            budget=WorkerMemoryBudget(4,memory_reader=lambda:12*1024**3,host_reader=lambda:None)
            execute_leads(queue, work, 4, consume, renderer=concurrent_cmc_renderer,
                          started=lambda item, active: starts.append(active),memory_budget=budget)
            self.assertEqual(starts, [1, 2, 3, 4])
            self.assertEqual(len(set(received)), 4)
            extra = ExtraFields(work, "cmc", CYCLE)
            extra.checkpoints = work / "raw-cache/checkpoints" / CYCLE / "cmc"
            self.assertEqual(extra.checkpoint("sample.npz")["values"].tolist(), [306])
            self.assertFalse((work / "render-workers/cmc/leads").exists())

    def test_memory_headroom_limits_concurrency_without_a_model_specific_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            work, starts, received=Path(directory), [], []
            (work / "expected-renderers").write_text("2")
            queue=LeadQueue([target("cmc",h) for h in (72,75,78)],set(),10)
            def consume(item,staged):
                self.assertIsNone(staged["error"])
                received.append(item["hour"])
            budget=WorkerMemoryBudget(3,memory_reader=lambda:3*1024**3,host_reader=lambda:None,
                                       rss_reader=lambda pid:None)
            execute_leads(queue,work,3,consume,renderer=concurrent_cmc_renderer,
                          started=lambda item,active:starts.append(active),memory_budget=budget)
            self.assertEqual(sorted(received),[72,75,78])
            self.assertEqual(max(starts),2)

    def test_paused_pool_resumes_queued_work_when_memory_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            recovered, received, idle = threading.Event(), [], []
            budget=WorkerMemoryBudget(2,memory_reader=lambda:(8 if recovered.is_set() else .5)*1024**3,
                                       host_reader=lambda:None)
            def on_idle():
                idle.append(True)
                recovered.set()
            execute_leads(LeadQueue([target()],set(),1),directory,2,
                          lambda item,staged:received.append(staged),renderer=every_product_renderer,
                          on_idle=on_idle,memory_budget=budget)
            self.assertTrue(idle)
            self.assertEqual(len(received),1)
            self.assertIsNone(received[0]["error"])
            self.assertEqual(budget.summary()["memory_pause_count"],1)
            self.assertFalse(budget.tasks)

    def test_checkpoint_lock_releases_after_error(self):
        from public_charts.extended_sources import ExtraFields
        with tempfile.TemporaryDirectory() as directory:
            extra = ExtraFields(directory, "cmc", CYCLE)
            with self.assertRaisesRegex(RuntimeError, "test"):
                with extra.checkpoint_lock():
                    raise RuntimeError("test")
            with extra.checkpoint_lock():
                self.assertTrue((extra.checkpoints / ".writer.lock").exists())

    def test_continuous_refresh_adds_new_lead_without_repeating_prepared_lead(self):
        with tempfile.TemporaryDirectory() as directory:
            hours, refreshed = [], []

            def refresh():
                refreshed.append(True)
                return [target(), target(hour=6)]

            execute_leads(LeadQueue([target()], set(), 10), directory, 4,
                          lambda item, staged: hours.append(item["hour"]), renderer=every_product_renderer,
                          refresh=refresh, refresh_seconds=.1, idle_seconds=.2)
            self.assertEqual(sorted(hours), [0, 6])
            self.assertTrue(refreshed)

    def test_stop_condition_drains_started_work_without_submitting_more(self):
        with tempfile.TemporaryDirectory() as directory:
            stop, hours = threading.Event(), []
            execute_leads(LeadQueue([target(hour=h) for h in (0, 6, 12, 18)], set(), 10), directory, 4,
                          lambda item, staged: hours.append(item["hour"]), renderer=every_product_renderer,
                          started=lambda item, active: stop.set(), can_schedule=lambda: not stop.is_set())
            self.assertEqual(hours, [0])

    def test_healthy_helper_can_receive_work_after_the_idle_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            hours=[]
            started=time.monotonic()
            def refresh():
                return [target()] if time.monotonic()-started >= .5 else []
            def healthy():
                return not hours and time.monotonic()-started < 30
            execute_leads(LeadQueue([],set(),1),directory,1,
                          lambda item,staged:hours.append((item["hour"],staged["error"])),
                          renderer=every_product_renderer,refresh=refresh,refresh_seconds=.2,
                          idle_seconds=.01,keep_alive=healthy,can_schedule=healthy)
            self.assertEqual(hours,[(0,None)])

    def test_stop_after_prefetch_does_not_start_a_memory_heavy_renderer(self):
        with tempfile.TemporaryDirectory() as directory:
            work, starts, received = Path(directory), [], []
            execute_leads(LeadQueue([target()], set(), 1), work, 1,
                          lambda *args: received.append(args), renderer=every_product_renderer,
                          prefetch=True, prefetcher=prepare_then_stop,
                          started=lambda *args: starts.append(args),
                          can_schedule=lambda: not (work / "memory-guard").exists())
            self.assertEqual(starts, [])
            self.assertEqual(received, [])

    def test_next_lead_renders_while_coordinator_uploads_previous_lead(self):
        with tempfile.TemporaryDirectory() as directory:
            work, parent, hours = Path(directory), threading.get_ident(), []

            def consume(item, staged):
                self.assertEqual(threading.get_ident(), parent)
                self.assertIsNone(staged["error"])
                if item["hour"] == 0:
                    deadline = time.monotonic() + 30
                    while not (work / "next-rendered").exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue((work / "next-rendered").exists())
                hours.append(item["hour"])

            execute_leads(LeadQueue([target(), target(hour=6)], set(), 2), work, 2, consume,
                          renderer=ahead_renderer)
            self.assertEqual(sorted(hours), [0, 6])

    def test_validated_image_survives_producer_deletion_and_checkpoint_is_shared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = stage_lead(target(), root, set(), renderer=sample_renderer)
            self.assertIsNone(result["error"])
            self.assertTrue(Path(result["artifacts"][0]["path"]).is_file())
            self.assertEqual(result["artifacts"][1]["alias_of"], ("surfce", "hkor"))
            self.assertFalse((root / "render-workers/aifs/raw-cache/images").exists())
            self.assertTrue((root / "raw-cache/checkpoints" / CYCLE / "aifs/sample.txt").is_file())

    def test_partial_artifacts_are_retained_on_pending_input(self):
        def partial(*args, **kwargs):
            sample_renderer(*args, **kwargs)
            raise NotReady("Upstream pending")

        with tempfile.TemporaryDirectory() as directory:
            result = stage_lead(target(), directory, set(), renderer=partial)
            self.assertEqual(result["error"], {"type": "NotReady", "pending": True})
            self.assertEqual(len(result["artifacts"]), 2)

    def test_cleanup_is_scoped_and_preserves_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("fields", "images", "checkpoints"):
                (root / "raw-cache" / name).mkdir(parents=True)
            cleanup_raw(root)
            self.assertTrue((root / "raw-cache/checkpoints").is_dir())
            self.assertFalse((root / "raw-cache/fields").exists())
            with self.assertRaises(ValueError):
                child_path(root, "..", "outside")

    def test_spawn_has_two_renderers_but_git_publisher_is_parent_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout, remote = make_repository(root)
            work = root / "work"
            work.mkdir()
            parent = os.getpid()
            received, snapshots, pixels = [], [], []

            def consume(item, staged):
                self.assertEqual(os.getpid(), parent)
                self.assertIsNone(staged["error"])
                self.assertFalse(staged["result"]["secret_visible"])
                self.assertNotEqual(staged["pid"], parent)
                received.append(staged["pid"])
                with Image.open(staged["artifacts"][0]["path"]) as image:
                    pixels.append(image.convert("RGB").tobytes())
                snapshots.append(item["model"])
                (checkout / MANIFEST).write_text(json.dumps({"charts": snapshots}), encoding="utf-8")
                publish_manifest(checkout)
                self.assertEqual(json.loads(git(remote, "show", f"main:{MANIFEST}"))["charts"], snapshots)
                if len(received) == 1:
                    self.assertFalse((work / "gfs-finished").exists())
                    (work / "first-published").touch()

            with patch.dict(os.environ, {"PUBLIC_CHARTS_DRIVE_REFRESH_TOKEN": "test-not-a-real-token",
                                         "PUBLIC_CHARTS_DRIVE2_REFRESH_TOKEN": "test-not-a-real-token"}):
                execute_leads(LeadQueue([target(), target("gfs")], set(), 1), work, 2, consume,
                              renderer=synchronized_renderer)
            self.assertEqual(len(set(received)), 2)
            self.assertEqual(snapshots, ["aifs", "gfs"])
            self.assertEqual(pixels[0], pixels[1])
            self.assertFalse((work / "render-workers/aifs/staged").exists())
            self.assertTrue((work / "raw-cache/checkpoints" / CYCLE / "aifs/sample.txt").exists())
            serial = stage_lead(target(), work, set(), renderer=sample_renderer)
            with Image.open(serial["artifacts"][0]["path"]) as image:
                self.assertEqual(image.convert("RGB").tobytes(), pixels[0])


class FakeStore:
    folder_id = "test-folder"

    def __init__(self):
        self.parent = os.getpid()
        self.data, self.uploads = {}, []

    def verify_account_and_folder(self):
        pass

    def read_json(self, name, default):
        return self.data.get(name, default)

    def inventory(self):
        return []

    def write_json(self, name, value):
        assert os.getpid() == self.parent
        self.data[name] = json.loads(json.dumps(value))

    def upload_image(self, path, key):
        assert os.getpid() == self.parent
        self.uploads.append(key)
        return {"file_id": key, "url": "https://example.invalid/chart.webp",
                "size": Path(path).stat().st_size, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}


class CoordinatorTests(unittest.TestCase):
    def test_first_verified_image_is_public_while_its_lead_is_still_running(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=argparse.Namespace(models=["aifs"],cycle=CYCLE,hours=[0],start_cycle=None,dry_run=False,
                                    workdir=root/"work",local=False,max_leads=1,force=False,workers=1,
                                    cache_root=None,max_bytes=1_000_000,manifest=root/"manifest.json",
                                    publish_incremental=True,fail_fast=True)
            store, published=FakeStore(),[]
            def execute(*positional,**kwargs):
                return execute_leads(*positional,**kwargs,renderer=streaming_renderer)
            def publish(_):
                value=json.loads(args.manifest.read_text())
                if not published:
                    self.assertFalse((args.workdir/"lead-finished").exists())
                    self.assertGreater(len(value["charts"]),0)
                    (args.workdir/"image-public").touch()
                published.append(value)
            with patch("public_charts_run.execute_leads",side_effect=execute), \
                 patch("public_charts_run.verify_public_url"), \
                 patch("public_charts_run.publish_manifest",side_effect=publish):
                self.assertEqual(run(args,store),0)
            self.assertTrue((args.workdir/"lead-finished").exists())
            self.assertEqual(len(published[-1]["charts"]),2)

    def test_publishes_each_lead_and_preserves_aliases_without_worker_drive_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(models=["aifs", "gfs"], cycle=CYCLE, hours=[0],
                                      start_cycle=None, dry_run=False, workdir=root / "work", local=False,
                                      max_leads=1, force=False, workers=1, cache_root=None,
                                      max_bytes=1_000_000, manifest=root / "manifest.json",
                                      publish_incremental=True, fail_fast=True)
            store, published = FakeStore(), []
            real_execute = execute_leads

            def execute(*args, **kwargs):
                return real_execute(*args, **kwargs, renderer=sample_renderer)

            def publish(_):
                value = json.loads(args.manifest.read_text(encoding="utf-8"))
                self.assertEqual(value, store.data["manifest.json"])
                published.append(len(value["charts"]))

            with patch("public_charts_run.execute_leads", side_effect=execute), \
                 patch("public_charts_run.verify_public_url") as verify, \
                 patch("public_charts_run.publish_manifest", side_effect=publish):
                self.assertEqual(run(args, store), 0)
            self.assertGreaterEqual(len(published), 2)
            self.assertIn(published[0], (1, 2))
            self.assertEqual(published[-1], 4)
            self.assertEqual(len(store.uploads), 2)
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(len({row["file_id"] for row in store.data["state.json"]["rows"]}), 2)
            self.assertEqual(json.loads((args.workdir / "cleanup-plan.json").read_text())["delete_ids"], [])

    def test_upload_failure_keeps_verified_rows_only_and_blocks_failed_file_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(models=["aifs", "gfs"], cycle=CYCLE, hours=[0],
                                      start_cycle=None, dry_run=False, workdir=root / "work", local=False,
                                      max_leads=1, force=False, workers=1, cache_root=None,
                                      max_bytes=1_000_000, manifest=root / "manifest.json",
                                      publish_incremental=True, fail_fast=True)
            store = FakeStore()
            original_upload = store.upload_image

            def upload(path, key):
                if key.startswith("gfs/"):
                    raise RuntimeError("Simulated failed upload")
                return original_upload(path, key)

            def execute(*args, **kwargs):
                return execute_leads(*args, **kwargs, renderer=sample_renderer)

            with patch("public_charts_run.execute_leads", side_effect=execute), \
                 patch("public_charts_run.verify_public_url"), \
                 patch("public_charts_run.publish_manifest"), \
                 patch.object(store, "upload_image", side_effect=upload):
                with self.assertRaisesRegex(RuntimeError, "failed upload"):
                    run(args, store)
            self.assertEqual({row["model"] for row in store.data["manifest.json"]["charts"]}, {"aifs"})
            self.assertEqual(len(store.data["state.json"]["rows"]), 2)
            self.assertEqual(json.loads((args.workdir / "cleanup-plan.json").read_text())["delete_ids"], [])


if __name__ == "__main__":
    unittest.main()
