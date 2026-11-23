import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from public_charts_drive import ChartDriveError, ChartDriveStore
from public_charts.storage import DetachedDriveStore, drive_detached, production_store
from public_charts.r2 import is_r2_row
from public_charts_run import finalize_cleanup
from test_public_charts_r2_only import Client, produce

STATE_ENV = {"PUBLIC_CHARTS_R2_ACCOUNT_ID": "a", "PUBLIC_CHARTS_R2_STATE_ACCESS_KEY_ID": "k",
             "PUBLIC_CHARTS_R2_STATE_SECRET_ACCESS_KEY": "s", "PUBLIC_CHARTS_R2_STATE_BUCKET": "b"}


class MemoryStateStore(DetachedDriveStore):
    """The detached store behind the R2 state wrapper, with R2 replaced by a dict."""

    def __init__(self, files=None):
        super().__init__()
        self.files = dict(files or {})

    def read_json(self, name, default=None):
        return json.loads(json.dumps(self.files.get(name, default)))

    def write_json(self, name, payload):
        self.files[name] = json.loads(json.dumps(payload))


class DriveDetachedTests(unittest.TestCase):
    def test_r2_only_images_and_state_never_contact_drive(self):
        env = {**STATE_ENV, "PUBLIC_CHARTS_R2_IMAGES": "only", "PUBLIC_CHARTS_R2_STATE": "only",
               "PUBLIC_CHARTS_STORAGE_LAYOUT": "balanced-v1"}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(ChartDriveStore, "from_environment", side_effect=AssertionError("Drive contacted")), \
                patch("public_charts.r2.wrap_state_store", side_effect=lambda store: store):
            self.assertIsInstance(production_store(), DetachedDriveStore)

    def test_drive_stays_attached_until_both_images_and_state_are_r2_only(self):
        for images, state in (("dual", "only"), ("only", "dual"), ("only", "off")):
            with self.subTest(images=images, state=state):
                env = {**STATE_ENV, "PUBLIC_CHARTS_R2_IMAGES": images, "PUBLIC_CHARTS_R2_STATE": state}
                self.assertFalse(drive_detached(env))

    def test_detached_store_refuses_every_drive_operation(self):
        store = DetachedDriveStore()
        self.assertEqual(store.inventory(), [])
        for name in ("read_json", "write_json", "upload_image", "new_upload_worker", "delete_managed",
                     "account_for_key"):
            with self.subTest(name=name), self.assertRaises(ChartDriveError):
                getattr(store, name)("x", "y")

    def test_balanced_state_runs_detached_and_cleanup_touches_nothing(self):
        drive_row = {"model": "gfs", "cycle": "2026111500", "hour": 0, "product": "surfce", "domain": "asia",
                     "file_id": "drivefile0001", "url": "https://drive.google.com/x", "size": 10, "sha256": "0" * 64}
        state = {"schema_version": 1, "rows": [drive_row], "storage_layout": "balanced-v1",
                 "storage_folders": {"primary": "p", "secondary": "s"}}
        with tempfile.TemporaryDirectory() as directory:
            root, store = Path(directory), MemoryStateStore({"state.json": state})
            status, manifest = produce(root, store, Client())
            self.assertEqual(status, 0)
            self.assertTrue(manifest["charts"])
            self.assertTrue(all(is_r2_row(row) for row in manifest["charts"]))
            saved = store.files["state.json"]
            self.assertNotIn("storage_layout", saved)
            self.assertNotIn("drivefile0001", {row["file_id"] for row in saved["rows"]})
            plan = json.loads((root / "work" / "cleanup-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(finalize_cleanup(store, manifest, plan, manifest), 0)


if __name__ == "__main__":
    unittest.main()
