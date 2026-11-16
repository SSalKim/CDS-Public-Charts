import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from public_charts import policy
from public_charts.parallel import LeadQueue, child_path, cleanup_raw, execute_leads, stage_lead
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
    work = Path(root).parents[2]
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
    result["secret_visible"] = "PUBLIC_CHARTS_DRIVE_REFRESH_TOKEN" in os.environ
    return result


def ahead_renderer(model, cycle, hour, root, existing, emit, **kwargs):
    result = sample_renderer(model, cycle, hour, root, existing, emit, **kwargs)
    if hour == 6:
        (Path(root).parents[2] / "next-rendered").touch()
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


def concurrent_cmc_renderer(model, cycle, hour, root, existing, emit, *, checkpoint_root=None, **kwargs):
    import numpy as np
    from public_charts.extended_sources import ExtraFields

    work = Path(checkpoint_root).parents[1]
    marker = Path(root) / "fields" / f"{hour}.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    (work / f"cmc-started-{hour}").touch()
    deadline = time.monotonic() + 30
    while len(list(work.glob("cmc-started-*"))) < 3:
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


class QueueTests(unittest.TestCase):
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

    def test_repeated_real_errors_stop_scanning_bad_cycle_but_remain_errors(self):
        queue = LeadQueue([target(hour=72), target(hour=78), target(hour=84),
                           target(cycle="2026111506")], set(), 2)
        error = {"type": "ValueError", "pending": False}
        for _ in range(2):
            queue.finished(queue.next(set()), error)
        self.assertEqual(queue.next(set()), target(cycle="2026111506"))
        self.assertEqual(queue.counts["aifs"], 0)

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
        self.assertEqual(queue.next(set()), target("cmc", 72))
        active = {("cmc", CYCLE, 72)}
        self.assertEqual(queue.next(active), target("gfs"))
        active.add(("gfs", CYCLE, 0))
        self.assertEqual(queue.next(active), target("cmc", 75))
        active.add(("cmc", CYCLE, 75))
        self.assertIsNone(queue.next(active))


class StagingTests(unittest.TestCase):
    def test_cmc_backlog_fills_three_slots_and_preserves_shared_checkpoints(self):
        from public_charts.extended_sources import ExtraFields
        with tempfile.TemporaryDirectory() as directory:
            work, received, starts = Path(directory), [], []
            pending = [target(model) for model in ("aifs", "gfs", "aigfs")]
            queue = LeadQueue(pending + [target("cmc", h) for h in (72, 75, 78)], set(), 10)
            for item in pending:
                queue.finished(item, {"type": "NotReady", "pending": True})

            def consume(item, staged):
                self.assertIsNone(staged["error"])
                self.assertEqual(item["model"], "cmc")
                self.assertTrue(all(Path(row["path"]).exists() for row in staged["artifacts"]))
                received.append(staged["pid"])

            with patch("public_charts.parallel.available_memory", return_value=6*1024**3):
                execute_leads(queue, work, 4, consume, renderer=concurrent_cmc_renderer,
                              started=lambda item, active: starts.append(active))
            self.assertEqual(starts, [1, 2, 3])
            self.assertEqual(len(set(received)), 3)
            extra = ExtraFields(work, "cmc", CYCLE)
            extra.checkpoints = work / "raw-cache/checkpoints" / CYCLE / "cmc"
            self.assertEqual(extra.checkpoint("sample.npz")["values"].tolist(), [225])
            self.assertFalse((work / "render-workers/cmc/leads").exists())

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
            self.assertEqual(hours, [0, 6])
            self.assertTrue(refreshed)

    def test_stop_condition_drains_started_work_without_submitting_more(self):
        with tempfile.TemporaryDirectory() as directory:
            stop, hours = threading.Event(), []
            execute_leads(LeadQueue([target(hour=h) for h in (0, 6, 12, 18)], set(), 10), directory, 4,
                          lambda item, staged: hours.append(item["hour"]), renderer=every_product_renderer,
                          started=lambda item, active: stop.set(), can_schedule=lambda: not stop.is_set())
            self.assertEqual(hours, [0])

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
            self.assertEqual(hours, [0, 6])

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

            with patch.dict(os.environ, {"PUBLIC_CHARTS_DRIVE_REFRESH_TOKEN": "test-not-a-real-token"}):
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
            self.assertEqual(published, [2, 4, 4])
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
