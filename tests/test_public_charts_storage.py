import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from public_charts import policy
from public_charts.storage import ChartDrivePool, ROUTES, production_store
from public_charts_drive import ChartDriveError, ChartDriveStore
from public_charts_run import finalize_cleanup, manifest_bytes, run


class MemoryStore:
    def __init__(self, name):
        self.name, self.folder_id, self.account_email = name, name + "-folder", name + "@example.invalid"
        self.data, self.files, self.uploads, self.deleted = {}, [], [], []

    def verify_account_and_folder(self):
        pass

    def inventory(self):
        return list(self.files)

    def prime_inventory(self, items):
        self.primed = list(items)

    def new_upload_worker(self):
        return self

    def read_json(self, name, default=None):
        return self.data.get(name, default)

    def write_json(self, name, payload):
        self.data[name] = json.loads(json.dumps(payload))

    def upload_image(self, path, key):
        data = Path(path).read_bytes()
        file_id = f"{self.name}-{len(self.files)}"
        digest = hashlib.sha256(data).hexdigest()
        self.uploads.append(key)
        self.files.append({"id": file_id, "size": len(data),
                           "appProperties": {"kind": "image", "key": key, "sha256": digest}})
        return {"file_id": file_id, "size": len(data), "sha256": digest,
                "url": f"https://drive.usercontent.google.com/download?id={file_id}"}

    def delete_managed(self, file_id):
        self.deleted.append(file_id)


def row(model, cycle, size, account, file_id=None):
    return {"model": model, "cycle": cycle, "hour": 0, "product": "gph500", "domain": "asia",
            "storage_account": account, "file_id": file_id or f"{model}-{cycle}", "size": size,
            "sha256": "a" * 64, "url": "https://drive.usercontent.google.com/download?id=example"}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.stores = {name: MemoryStore(name) for name in ("primary", "secondary")}
        self.pool = ChartDrivePool(self.stores)

    def test_balanced_routes_and_legacy_ownership_are_independent(self):
        self.assertEqual({model for model, account in ROUTES.items() if account == "primary"},
                         {"gfs", "cmc", "ecmwf", "ukmo"})
        self.assertEqual(self.pool.account_for_key("aifs/2026111612/000/gph500/asia"), "secondary")
        self.stores["primary"].files = [{"id": "legacy-aifs", "appProperties": {"kind": "image"}}]
        inventory = self.pool.inventory()
        self.pool.prime_inventory(inventory)
        self.assertEqual(self.pool.account_for_file("legacy-aifs"), "primary")
        self.assertEqual(len(self.stores["primary"].primed), 1)
        self.assertEqual(self.stores["secondary"].primed, [])

    def test_same_account_or_folder_is_rejected(self):
        for field in ("folder_id", "account_email"):
            with self.subTest(field=field):
                original = getattr(self.stores["secondary"], field)
                setattr(self.stores["secondary"], field, getattr(self.stores["primary"], field))
                with self.assertRaises(ChartDriveError):
                    ChartDrivePool(self.stores)
                setattr(self.stores["secondary"], field, original)

    def test_no_secondary_configuration_fallback(self):
        with patch.dict(os.environ, {"PUBLIC_CHARTS_STORAGE_LAYOUT": "balanced-v1"}, clear=True), \
                patch.object(ChartDriveStore, "from_environment", side_effect=[self.stores["primary"],
                     ChartDriveError("Missing dedicated settings")]) as create:
            with self.assertRaisesRegex(ChartDriveError, "Missing"):
                production_store()
            self.assertEqual(create.call_args_list[-1].args, ("PUBLIC_CHARTS_DRIVE2_",))

    def test_legacy_workflow_without_layout_remains_single_account(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(ChartDriveStore, "from_environment", return_value=self.stores["primary"]) as create:
            self.assertIs(production_store(), self.stores["primary"])
            create.assert_called_once_with()

    def test_deletion_requires_inventory_and_correct_owner(self):
        with self.assertRaises(ChartDriveError):
            self.pool.delete_managed("unknown")
        self.stores["secondary"].files = [{"id": "snow-image"}]
        self.pool.inventory()
        self.pool.delete_managed("snow-image")
        self.assertEqual(self.stores["primary"].deleted, [])
        self.assertEqual(self.stores["secondary"].deleted, ["snow-image"])

    def test_duplicate_file_identity_across_accounts_is_rejected(self):
        for store in self.stores.values():
            store.files = [{"id": "unexpected-shared-id"}]
        with self.assertRaises(ChartDriveError):
            self.pool.inventory()

    def test_two_budgets_are_not_one_fourteen_gigabyte_pool(self):
        rows = [row("gfs", "2026111612", 80, "primary"), row("icon", "2026111612", 80, "secondary")]
        kept, deleted = policy.retain(rows, 100, account_budgets={"primary": 100, "secondary": 100})
        self.assertEqual(len(kept), 2)
        self.assertEqual(deleted, [])
        rows[0]["size"] = 101
        with self.assertRaisesRegex(ValueError, "Latest"):
            policy.retain(rows, 1000, account_budgets={"primary": 100, "secondary": 1000})

    def test_account_pressure_never_evicts_a_models_newest_cycle(self):
        rows = [row("aifs", "2026111606", 20, "primary"),
                row("aifs", "2026111612", 101, "secondary")]
        with self.assertRaisesRegex(ValueError, "Latest"):
            policy.retain(rows, account_budgets={"primary": 1000, "secondary": 100})

    def test_aliases_are_charged_once_and_account_ids_are_private(self):
        first = row("icon", "2026111612", 80, "secondary", "same")
        second = {**first, "product": "gph300"}
        kept, _ = policy.retain([first, second], account_budgets={"primary": 1, "secondary": 80})
        self.assertEqual(policy.unique_bytes(kept), 80)
        self.assertNotIn("storage_account", policy.public_manifest(kept)["charts"][0])

    def test_cleanup_refuses_changed_second_folder(self):
        manifest = policy.public_manifest([row("icon", "2026111612", 80, "secondary")])
        plan = {"namespace": "cds-public-charts-v1", "folder_id": self.pool.folder_id,
                "storage_folders": {**self.pool.storage_folders, "secondary": "wrong-folder"},
                "manifest_sha256": hashlib.sha256(manifest_bytes(manifest)).hexdigest(), "delete_ids": []}
        with self.assertRaisesRegex(ValueError, "targets"):
            finalize_cleanup(self.pool, manifest, plan, manifest)

    def test_coordinator_uploads_each_model_to_its_account_with_one_master_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(models=["gfs", "icon"], cycle="2026111612", hours=[0], start_cycle=None,
                dry_run=False, workdir=root / "work", local=False, max_leads=1, force=False, workers=1,
                cache_root=None, max_bytes=100, manifest=root / "manifest.json", publish_incremental=False,
                fail_fast=True, upload_workers=1)
            def execute(queue, work, workers, consume, **kwargs):
                for model in args.models:
                    target = {"model": model, "cycle": args.cycle, "hour": 0}
                    path = work / "render-workers" / model / "staged" / args.cycle / "000" / "map.webp"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"x" * 80)
                    consume(target, {"artifacts": [{"product": "gph500", "domain": "asia",
                        "alias_of": None, "path": str(path)}], "partial": True})
            with patch("public_charts_run.execute_leads", side_effect=execute), \
                    patch("public_charts_run.verify_public_url"):
                self.assertEqual(run(args, self.pool), 0)
            state = self.stores["primary"].data["state.json"]
            self.assertEqual({item["storage_account"] for item in state["rows"]}, {"primary", "secondary"})
            self.assertEqual(len(self.stores["primary"].data["manifest.json"]["charts"]), 2)
            self.assertEqual(self.stores["secondary"].data, {})
            self.assertTrue(self.stores["secondary"].uploads[0].startswith("icon/"))
            self.assertEqual(set(state["verified_cycles"]),
                             {"gfs/2026111612/primary", "icon/2026111612/secondary"})
            self.pool.storage_folders["secondary"] = "changed-target"
            with self.assertRaisesRegex(ValueError, "Stored account targets"):
                run(args, self.pool)
