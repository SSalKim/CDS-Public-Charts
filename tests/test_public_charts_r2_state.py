import gzip
import json
import os
import unittest
from unittest.mock import patch

from public_charts import r2
from public_charts_publish import git_manifest_enabled, publish_manifest
import public_charts_r2_compare as compare

ENV = {"PUBLIC_CHARTS_R2_ACCOUNT_ID": "a" * 32, "PUBLIC_CHARTS_R2_STATE_ACCESS_KEY_ID": "k",
       "PUBLIC_CHARTS_R2_STATE_SECRET_ACCESS_KEY": "s", "PUBLIC_CHARTS_R2_STATE_BUCKET": "cds-public-charts-state"}


class Drive:
    folder_id = "folder"

    def __init__(self):
        self.data, self.storage_folders = {}, {"primary": "p", "secondary": "s"}

    def read_json(self, name, default=None):
        return self.data.get(name, default)

    def write_json(self, name, payload):
        self.data[name] = payload

    def account_for_key(self, key):
        return "primary"


class Client:
    def __init__(self, fail=False):
        self.objects, self.fail = {}, fail

    def put(self, key, body, **kwargs):
        if self.fail:
            raise r2.R2Error("boom", operation="put", http_status=503)
        assert r2.KEY_PATTERN.fullmatch(key) and kwargs["max_bytes"] == r2.STATE_MAX_BYTES
        self.objects[key] = body

    def get(self, key, **kwargs):
        if self.fail:
            raise r2.R2Error("boom", operation="get", http_status=503)
        return self.objects.get(key)


class StateStoreTests(unittest.TestCase):
    def test_modes_and_credentials(self):
        self.assertEqual(r2.state_mode({}), "off")
        self.assertEqual(r2.state_mode({**ENV, "PUBLIC_CHARTS_R2_STATE": "Dual"}), "dual")
        with self.assertRaises(ValueError):
            r2.state_mode({"PUBLIC_CHARTS_R2_STATE": "only"})
        with self.assertRaises(ValueError):
            r2.state_mode({**ENV, "PUBLIC_CHARTS_R2_STATE": "both"})
        drive = Drive()
        self.assertIs(r2.wrap_state_store(drive, {}), drive)

    def test_dual_writes_both_compressed_and_reads_r2_first(self):
        drive, client, events = Drive(), Client(), []
        store = r2.StateMirroredStore(drive, client, "dual", emit=events.append, anonymous_get=lambda key: 400)
        store.write_json("state.json", {"rows": [1, 2]})
        self.assertEqual(drive.data["state.json"], {"rows": [1, 2]})
        self.assertEqual(json.loads(gzip.decompress(client.objects["state/state.json.gz"])), {"rows": [1, 2]})
        drive.data["state.json"] = {"rows": ["stale"]}
        self.assertEqual(store.read_json("state.json"), {"rows": [1, 2]})
        self.assertEqual(events, [{"event": "r2_state_private", "anonymous_status": 400, "private": True}])
        self.assertEqual(store.storage_folders, drive.storage_folders, "Cleanup checks see the Drive layout")
        self.assertEqual(vars(store)["storage_folders"], drive.storage_folders)
        self.assertEqual(store.account_for_key("x"), "primary")

    def test_only_mode_leaves_drive_alone_and_migrates_once(self):
        drive, client = Drive(), Client()
        drive.data["manifest.json"] = {"charts": ["old"]}
        store = r2.StateMirroredStore(drive, client, "only", emit=lambda item: None)
        self.assertEqual(store.read_json("manifest.json"), {"charts": ["old"]}, "First read falls back to Drive")
        store.write_json("manifest.json", {"charts": ["new"]})
        self.assertEqual(drive.data["manifest.json"], {"charts": ["old"]})
        self.assertEqual(store.read_json("manifest.json"), {"charts": ["new"]})

    def test_failures(self):
        from public_charts_drive import ChartDriveError
        events = []
        dual = r2.StateMirroredStore(Drive(), Client(fail=True), "dual", emit=events.append)
        dual.write_json("state.json", {"rows": []})
        self.assertEqual(dual.read_json("state.json"), {"rows": []}, "Dual mode keeps working from Drive")
        self.assertEqual([e["event"] for e in events], ["r2_state_write_failed", "r2_state_read_failed"])
        only = r2.StateMirroredStore(Drive(), Client(fail=True), "only", emit=lambda item: None)
        with self.assertRaises(ChartDriveError) as caught:
            only.write_json("state.json", {})
        self.assertTrue(caught.exception.transient, "A mid-run state write is retried at the next interval")
        with self.assertRaises(ValueError):
            r2.state_key("token.json")


class GitSwitchTests(unittest.TestCase):
    def test_git_publication_can_be_turned_off(self):
        self.assertTrue(git_manifest_enabled({}))
        self.assertFalse(git_manifest_enabled({"PUBLIC_CHARTS_GIT_MANIFEST": "Off"}))
        with patch.dict(os.environ, {"PUBLIC_CHARTS_GIT_MANIFEST": "off"}), patch("subprocess.run") as run:
            self.assertIsNone(publish_manifest("."))
        run.assert_not_called()

    def test_r2_check_works_without_git_history(self):
        index = {"schema": r2.SCHEMA, "generated_at": "2026-11-21T00:00:00+00:00", "runs": {}}

        class Response:
            status_code = 200
            headers = {"Cache-Control": r2.INDEX_CACHE, "cf-cache-status": "HIT", "Access-Control-Allow-Origin": "https://o"}
            def json(self):
                return index

        class Session:
            headers = {}
            def get(self, *args, **kwargs):
                return Response()

        from datetime import datetime, timezone
        report = compare.run_checks("https://b/", "https://o", None, session=Session(),
                                    now=datetime(2026, 11, 21, 0, 10, tzinfo=timezone.utc))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["facts"]["git_manifests_checked"], "off")


if __name__ == "__main__":
    unittest.main()
