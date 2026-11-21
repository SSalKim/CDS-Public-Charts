import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from public_charts import policy, r2
from public_charts.parallel import execute_leads
from public_charts.uploads import UploadPool
from public_charts_run import plan_cleanup, run
from test_public_charts_parallel import CYCLE, FakeStore, sample_renderer

NAME = f"gfs_hkor_acptot_s042_{CYCLE}_{'a' * 16}.webp"


class Client:
    def __init__(self, fail=False):
        self.objects, self.fail, self.writes, self.reads = {}, fail, 0, 0

    def put(self, key, body, **kwargs):
        if self.fail and key.startswith("charts/"):
            raise r2.R2Error("boom", operation="put", http_status=503)
        self.writes += 1
        self.objects[key] = body

    def get(self, key):
        self.reads += 1
        return self.objects.get(key)


class DriveStore(FakeStore):
    def upload_image(self, path, key):
        record = super().upload_image(path, key)
        file_id = hashlib.sha256(key.encode()).hexdigest()[:20]
        return {**record, "file_id": file_id, "url": r2.DRIVE_URL.format(file_id)}


def produce(root, store, client, hours=(0,)):
    args = argparse.Namespace(models=["gfs"], cycle=CYCLE, hours=list(hours), start_cycle=None, dry_run=False,
                              workdir=root / "work", local=False, max_leads=1, force=False, workers=1, cache_root=None,
                              max_bytes=1_000_000, manifest=root / "manifest.json", publish_incremental=True, fail_fast=True)
    publisher = r2.R2Publisher(client, emit=lambda item: None)
    def execute(*positional, **kwargs):
        return execute_leads(*positional, **kwargs, renderer=sample_renderer)
    with patch("public_charts_run.execute_leads", side_effect=execute), patch("public_charts_run.verify_public_url"), \
            patch("public_charts_run.publish_manifest"), \
            patch("public_charts.r2.publisher_from_env", return_value=("required", publisher)), \
            patch("public_charts.r2.client_from_env", return_value=client), \
            patch.dict(os.environ, {"PUBLIC_CHARTS_R2_IMAGES": "only"}):
        status = run(args, store)
    return status, json.loads(args.manifest.read_text(encoding="utf-8"))


class R2OnlyRunTests(unittest.TestCase):
    def test_images_go_to_r2_alone_and_survive_the_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root, store, client = Path(directory), DriveStore(), Client()
            status, manifest = produce(root, store, client)
            self.assertEqual(status, 0)
            self.assertEqual(store.uploads, [], "Nothing is uploaded to Drive")
            self.assertTrue(manifest["charts"])
            for row in manifest["charts"]:
                self.assertTrue(row["file_id"].startswith(r2.R2_ID_PREFIX))
                self.assertEqual(row["url"], r2.public_url(row["model"], row["cycle"], row["r2"]))
                self.assertIn(r2.image_key(row["model"], row["cycle"], row["r2"]), client.objects)
            state = store.data["state.json"]["rows"]
            self.assertTrue(all(row["storage_account"] == r2.R2_ACCOUNT for row in state))
            plan = json.loads((root / "work" / "cleanup-plan.json").read_text(encoding="utf-8"))
            self.assertFalse([i for i in plan["delete_ids"] if i.startswith(r2.R2_ID_PREFIX)])
            _, fragments = r2.split_manifest(manifest)
            self.assertTrue(all(item[3].startswith(r2.R2_ID_PREFIX) for f in fragments.values() for item in f["charts"]))

            # The Drive inventory stays empty, yet the R2 rows remain published.
            status, again = produce(root, store, client)
            self.assertEqual(status, 0)
            self.assertEqual({r["file_id"] for r in again["charts"]}, {r["file_id"] for r in manifest["charts"]})

    def test_a_failed_r2_upload_falls_back_to_drive(self):
        with tempfile.TemporaryDirectory() as directory:
            root, store = Path(directory), DriveStore()
            status, manifest = produce(root, store, Client(fail=True))
            self.assertEqual(status, 0)
            self.assertTrue(store.uploads, "Drive receives the images R2 refused")
            self.assertTrue(all(row["url"] == r2.DRIVE_URL.format(row["file_id"]) and "r2" not in row
                                for row in manifest["charts"]))


class R2OnlyUnitTests(unittest.TestCase):
    def test_upload_worker_skips_drive(self):
        client = Client()
        pool = UploadPool(DriveStore(), 1, mirror=r2.ImageMirror(lambda: client, emit=lambda item: None), r2_only=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.webp"; path.write_bytes(b"RIFF....WEBPdata")
            info = pool.upload({"key": policy.key("gfs", CYCLE, 42, "acptot", "hkor"), "path": path, "size": 16})
        self.assertEqual(info["storage_account"], r2.R2_ACCOUNT)
        self.assertEqual(info["file_id"], r2.r2_file_id(info["r2"]))
        self.assertEqual(pool.store.uploads, [])
        self.assertEqual(pool.drive_fallbacks, 0)

    def test_manifest_rows_must_be_consistent(self):
        row = {"model": "gfs", "cycle": CYCLE, "hour": 42, "product": "acptot", "domain": "hkor", "size": 10, "sha256": "a" * 64,
               "r2": NAME, "file_id": r2.r2_file_id(NAME), "url": r2.public_url("gfs", CYCLE, NAME), "render_version": 7}
        manifest = policy.public_manifest([row])
        _, fragments = r2.split_manifest(manifest)
        self.assertEqual(fragments[f"gfs/{CYCLE}"]["charts"][0][3], r2.r2_file_id(NAME))
        for broken in ({"url": r2.DRIVE_URL.format("x")}, {"file_id": "r2_other"}):
            with self.assertRaises(ValueError):
                r2.split_manifest(policy.public_manifest([{**row, **broken}]))

    def test_cleanup_never_lists_r2_objects_as_drive_files(self):
        from datetime import datetime, timedelta
        latest = datetime.strptime(CYCLE, "%Y%m%d%H")
        cycles = [(latest - timedelta(hours=6 * n)).strftime("%Y%m%d%H") for n in range(policy.RETENTION["gfs"] + 2)]
        rows = [{"model": "gfs", "cycle": c, "hour": 0, "product": "gph500", "domain": "asia", "size": 10,
                 "sha256": "b" * 64, "file_id": f"r2_{c}", "storage_account": r2.R2_ACCOUNT} for c in cycles]
        with patch("public_charts.policy.retention_current", return_value=True):
            kept, delete_ids = plan_cleanup(rows, [], 1_000_000, account_budgets={"primary": 1_000_000, r2.R2_ACCOUNT: r2.R2_MAX_BYTES})
        self.assertEqual(len(kept), policy.RETENTION["gfs"], "Old R2 cycles leave the manifest")
        self.assertEqual(delete_ids, [], "but are never queued for Drive deletion")


if __name__ == "__main__":
    unittest.main()
