import hashlib
from pathlib import Path
import tempfile
import threading
import unittest

from public_charts import policy, r2
from public_charts.uploads import UploadPool

URL = "https://drive.usercontent.google.com/download?id={}&export=view&authuser=0"
CYCLE = "2026112012"


class FakeClient:
    def __init__(self, fail=False):
        self.fail, self.puts = fail, []

    def put(self, key, body, *, cache_control, content_type="application/json"):
        if self.fail:
            raise r2.R2Error("boom", operation="put", http_status=503)
        self.puts.append((key, body, cache_control, content_type))


class DriveStore:
    def __init__(self):
        self.uploads = []

    def upload_image(self, path, key):
        data = Path(path).read_bytes()
        self.uploads.append(key)
        return {"file_id": "F" + str(len(self.uploads)), "url": URL.format("F" + str(len(self.uploads))),
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "key": key}

    def new_upload_worker(self):
        return self


def chart(tmp, key, data=b"RIFF....WEBPdata"):
    path = Path(tmp) / (key.replace("/", "_") + ".webp")
    path.write_bytes(data)
    return {"key": key, "path": path, "size": len(data)}


class ImageMirrorTests(unittest.TestCase):
    def test_name_and_key_follow_the_planned_layout(self):
        sha = hashlib.sha256(b"x").hexdigest()
        name = r2.image_name("gfs", CYCLE, 42, "acrain1", "hkor", sha)
        self.assertEqual(name, f"gfs_hkor_acrain1_s042_{CYCLE}_{sha[:16]}.webp")
        self.assertEqual(r2.image_key("gfs", CYCLE, name), f"charts/gfs/{CYCLE}/{name}")
        self.assertTrue(r2.KEY_PATTERN.fullmatch(r2.image_key("gfs", CYCLE, name)))
        self.assertFalse(r2.KEY_PATTERN.fullmatch("charts/gfs/../secret.webp"))

    def test_upload_worker_copies_to_r2_after_drive_and_names_the_object(self):
        client = FakeClient()
        mirror = r2.ImageMirror(lambda: client, emit=lambda item: None)
        with tempfile.TemporaryDirectory() as tmp:
            item = chart(tmp, policy.key("gfs", CYCLE, 42, "acptot", "hkor"))
            info = UploadPool(DriveStore(), 1, mirror=mirror).upload(item)
        key, body, cache, content_type = client.puts[0]
        self.assertEqual(key, f"charts/gfs/{CYCLE}/{info['r2']}")
        self.assertEqual((body, cache, content_type), (b"RIFF....WEBPdata", r2.IMMUTABLE_CACHE, "image/webp"))
        self.assertEqual((mirror.writes, mirror.failures, mirror.bytes), (1, 0, len(body)))

    def test_a_failed_copy_keeps_only_the_drive_url(self):
        events = []
        mirror = r2.ImageMirror(lambda: FakeClient(fail=True), emit=events.append)
        with tempfile.TemporaryDirectory() as tmp:
            info = UploadPool(DriveStore(), 1, mirror=mirror).upload(chart(tmp, policy.key("gfs", CYCLE, 42, "acptot", "hkor")))
        self.assertNotIn("r2", info)
        self.assertEqual(info["file_id"], "F1")
        self.assertEqual(events[0]["event"], "r2_image_failed")
        self.assertEqual((mirror.writes, mirror.failures), (0, 1))

    def test_each_upload_thread_gets_its_own_client(self):
        made = []
        def factory():
            made.append(threading.get_ident())
            return FakeClient()
        mirror = r2.ImageMirror(factory, emit=lambda item: None)
        with tempfile.TemporaryDirectory() as tmp:
            items = [chart(tmp, policy.key("gfs", CYCLE, hour, "gph500", "asia")) for hour in (0, 3, 6, 9, 12, 15)]
            pool = UploadPool(DriveStore(), 3, mirror=mirror)
            recorded = []
            pool.run(items, lambda item: None, lambda item: None, lambda item, info: recorded.append(info))
            pool.close()
        self.assertEqual(len(recorded), 6)
        self.assertTrue(all(info.get("r2") for info in recorded))
        self.assertEqual(len(made), len(set(made)), "No client is shared between threads")

    def test_modes(self):
        self.assertEqual(r2.images_mode({}), "off")
        self.assertEqual(r2.images_mode({"PUBLIC_CHARTS_R2_IMAGES": " Dual "}), "dual")
        with self.assertRaises(ValueError):
            r2.images_mode({"PUBLIC_CHARTS_R2_IMAGES": "only"})


class ManifestFieldTests(unittest.TestCase):
    def rows(self, **extra):
        sha = "a" * 64
        base = {"model": "gfs", "cycle": CYCLE, "hour": 42, "product": "acptot", "domain": "hkor", "file_id": "F1",
                "url": URL.format("F1"), "size": 10, "sha256": sha, "render_version": 312}
        return [{**base, **extra}, {**base, "product": "surfce", **extra}]

    def test_public_manifest_and_fragment_carry_the_r2_name_for_aliases_too(self):
        name = f"gfs_hkor_acptot_s042_{CYCLE}_{'a' * 16}.webp"
        manifest = policy.public_manifest(self.rows(r2=name))
        self.assertTrue(all(row["r2"] == name for row in manifest["charts"]))
        _, fragments = r2.split_manifest(manifest)
        items = fragments[f"gfs/{CYCLE}"]["charts"]
        self.assertEqual([item[4] for item in items], [{"r2": name}, {"r2": name}],
                         "The surfce alias points at the acptot object it reuses")

    def test_rows_without_a_copy_stay_drive_only(self):
        manifest = policy.public_manifest(self.rows())
        self.assertNotIn("r2", manifest["charts"][0])
        _, fragments = r2.split_manifest(manifest)
        self.assertEqual(len(fragments[f"gfs/{CYCLE}"]["charts"][0]), 4)

    def test_names_from_another_run_are_rejected(self):
        manifest = policy.public_manifest(self.rows(r2=f"icon_hkor_acptot_s042_{CYCLE}_{'a' * 16}.webp"))
        with self.assertRaises(ValueError):
            r2.split_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
