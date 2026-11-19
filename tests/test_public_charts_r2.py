from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from public_charts import r2
from public_charts_publish import MANIFEST, PublicationError, publish_r2

URL = "https://drive.usercontent.google.com/download?id={}&export=view&authuser=0"


def row(model, cycle, hour, product, domain, file_id, **extra):
    return {"model": model, "cycle": cycle, "hour": hour, "product": product, "domain": domain,
            "file_id": file_id, "url": URL.format(file_id), "size": 10, "sha256": "0" * 64,
            "render_version": 7, **extra}


def manifest(*rows):
    return {"schema_version": 1, "generated_at": "2026-11-19T00:00:00+00:00",
            "models": {"gfs": {"title": "GFS", "published": True, "retention_cycles": 9}},
            "charts": list(rows)}


class FakeClient:
    def __init__(self, objects=None, fail=False):
        self.objects, self.fail, self.puts = dict(objects or {}), fail, []
        self.writes = self.reads = 0

    def put(self, key, body, *, cache_control, content_type="application/json"):
        if self.fail:
            raise r2.R2Error("boom", operation="put", http_status=503)
        self.writes += 1
        self.puts.append((key, cache_control))
        self.objects[key] = body

    def get(self, key):
        self.reads += 1
        return self.objects.get(key)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeResponse:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content


class FakeSession:
    def __init__(self, statuses):
        self.statuses, self.calls = list(statuses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs["headers"]))
        return FakeResponse(self.statuses.pop(0), b"{}")


class SignatureTests(unittest.TestCase):
    def test_matches_the_published_aws_get_object_example(self):
        headers = r2.sigv4_headers(
            "GET", "examplebucket.s3.amazonaws.com", "/test.txt", {"Range": "bytes=0-9"},
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            region="us-east-1", now=datetime(2013, 5, 24, tzinfo=timezone.utc))
        self.assertEqual(headers["authorization"],
                         "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
                         "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
                         "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


class ClientTests(unittest.TestCase):
    def client(self, statuses):
        session = FakeSession(statuses)
        return r2.R2Client("a" * 32, "key", "secret", "cds-charts", session=session, sleep=lambda _: None), session

    def test_retries_transient_failures_and_counts_writes(self):
        client, session = self.client([503, 200])
        client.put(r2.INDEX_KEY, b"{}", cache_control=r2.INDEX_CACHE)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(client.writes, 2)
        method, url, headers = session.calls[-1]
        self.assertEqual(url, f"https://{'a' * 32}.r2.cloudflarestorage.com/cds-charts/manifest/index.json")
        self.assertEqual(headers["cache-control"], r2.INDEX_CACHE)
        self.assertIn("cache-control;content-type;host", headers["authorization"])

    def test_permission_errors_fail_immediately(self):
        client, session = self.client([403])
        with self.assertRaises(r2.R2Error) as caught:
            client.put(r2.INDEX_KEY, b"{}", cache_control=r2.INDEX_CACHE)
        self.assertEqual(caught.exception.details["http_status"], 403)
        self.assertEqual(len(session.calls), 1)

    def test_missing_index_reads_as_none(self):
        client, _ = self.client([404])
        self.assertIsNone(client.get(r2.INDEX_KEY))

    def test_rejects_keys_outside_the_manifest_prefix(self):
        client, session = self.client([200])
        for key in ("charts/x.webp", "manifest/../state.json", "manifest/runs/gfs/2026111900.json"):
            with self.assertRaises(r2.R2Error):
                client.put(key, b"{}", cache_control=r2.INDEX_CACHE)
        self.assertEqual(session.calls, [])


class SplitTests(unittest.TestCase):
    def test_fragments_are_compact_sorted_and_keep_ukmo_confirmation(self):
        models, fragments = r2.split_manifest(manifest(
            row("gfs", "2026111900", 6, "gph500", "hkor", "B"),
            row("gfs", "2026111900", 0, "gph500", "asia", "A"),
            row("ukmo", "2026111900", 0, "acptot", "hkor", "C",
                kma_confirmed_at="2026-11-19T06:00:00+00:00", kma_reference_hour=0)))
        self.assertEqual(set(fragments), {"gfs/2026111900", "ukmo/2026111900"})
        gfs = fragments["gfs/2026111900"]
        self.assertEqual(gfs["charts"], [[0, "asia", "gph500", "A"], [6, "hkor", "gph500", "B"]])
        self.assertEqual(gfs["images"], "drive")
        self.assertEqual(gfs["leads"][:3], [0, 3, 6])
        self.assertEqual(gfs["leads"][-1], 240)
        self.assertEqual(fragments["ukmo/2026111900"]["charts"][0][4],
                         {"kma_confirmed_at": "2026-11-19T06:00:00+00:00", "kma_reference_hour": 0})
        self.assertEqual(models["gfs"]["title"], "GFS")

    def test_revision_is_stable_and_content_addressed(self):
        first = r2.split_manifest(manifest(row("gfs", "2026111900", 0, "gph500", "asia", "A")))[1]
        again = r2.split_manifest(manifest(row("gfs", "2026111900", 0, "gph500", "asia", "A")))[1]
        other = r2.split_manifest(manifest(row("gfs", "2026111900", 0, "gph500", "asia", "Z")))[1]
        self.assertEqual(r2.revision(first["gfs/2026111900"]), r2.revision(again["gfs/2026111900"]))
        self.assertNotEqual(r2.revision(first["gfs/2026111900"]), r2.revision(other["gfs/2026111900"]))
        self.assertRegex(r2.revision(first["gfs/2026111900"]), r"\A[a-f0-9]{16}\Z")

    def test_rejects_rows_whose_url_cannot_be_rebuilt(self):
        bad = row("gfs", "2026111900", 0, "gph500", "asia", "A")
        bad["url"] = "https://example.invalid/A"
        with self.assertRaises(ValueError):
            r2.split_manifest(manifest(bad))

    def test_rejects_duplicate_chart_identities(self):
        with self.assertRaises(ValueError):
            r2.split_manifest(manifest(row("gfs", "2026111900", 0, "gph500", "asia", "A"),
                                       row("gfs", "2026111900", 0, "gph500", "asia", "B")))


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.client, self.clock, self.events = FakeClient(), Clock(), []
        self.publisher = r2.R2Publisher(self.client, clock=self.clock, emit=self.events.append,
                                        wall=lambda: datetime(2026, 11, 19, 6, tzinfo=timezone.utc))
        self.base = [row("gfs", "2026111900", 0, "gph500", "asia", "A"),
                     row("gfs", "2026111906", 0, "gph500", "asia", "B")]

    def index(self):
        return json.loads(self.client.objects[r2.INDEX_KEY])

    def test_fragments_are_uploaded_before_the_index_with_cache_headers(self):
        self.assertEqual(self.publisher.publish(manifest(*self.base)), "published")
        keys = [key for key, _ in self.client.puts]
        self.assertEqual(keys[-1], r2.INDEX_KEY)
        self.assertEqual(len(keys), 3)
        self.assertTrue(all(cache == r2.IMMUTABLE_CACHE for _, cache in self.client.puts[:-1]))
        self.assertEqual(self.client.puts[-1][1], r2.INDEX_CACHE)
        index = self.index()
        self.assertEqual(index["generated_at"], "2026-11-19T06:00:00+00:00")
        for run, item in index["runs"].items():
            fragment = json.loads(self.client.objects[r2.fragment_key(run, item["rev"])])
            self.assertEqual(item["count"], len(fragment["charts"]))

    def test_only_changed_runs_are_rewritten(self):
        self.publisher.publish(manifest(*self.base))
        self.clock.now += 31
        self.assertEqual(self.publisher.publish(manifest(*self.base)), "unchanged")
        self.client.puts.clear()
        changed = self.base + [row("gfs", "2026111906", 3, "gph500", "asia", "C")]
        self.assertEqual(self.publisher.publish(manifest(*changed)), "published")
        self.assertEqual([key.startswith("manifest/runs/gfs/2026111906.") for key, _ in self.client.puts],
                         [True, False])

    def test_index_writes_are_coalesced_and_flushed_when_due(self):
        self.publisher.publish(manifest(*self.base))
        self.clock.now += 10
        changed = self.base + [row("gfs", "2026111906", 3, "gph500", "asia", "C")]
        self.assertEqual(self.publisher.publish(manifest(*changed)), "deferred")
        self.assertFalse(self.publisher.due())
        self.clock.now += 25
        self.assertTrue(self.publisher.due())
        self.assertEqual(self.publisher.publish(self.publisher.latest), "published")
        self.assertEqual(self.index()["runs"]["gfs/2026111906"]["count"], 2)
        self.assertFalse(self.publisher.due())

    def test_force_bypasses_coalescing_for_the_final_publication(self):
        self.publisher.publish(manifest(*self.base))
        self.clock.now += 1
        changed = self.base + [row("gfs", "2026111906", 3, "gph500", "asia", "C")]
        self.assertEqual(self.publisher.publish(manifest(*changed), force=True), "published")

    def test_unchanged_index_is_refreshed_as_a_clock(self):
        self.publisher.publish(manifest(*self.base))
        self.clock.now += r2.INDEX_REFRESH_SECONDS
        self.assertTrue(self.publisher.due())
        before = self.client.writes
        self.assertEqual(self.publisher.publish(self.publisher.latest), "published")
        self.assertEqual(self.client.writes, before + 1, "Only the index is rewritten")

    def test_existing_fragments_are_not_uploaded_again_by_a_new_process(self):
        self.publisher.publish(manifest(*self.base))
        fresh = r2.R2Publisher(self.client, clock=self.clock, emit=self.events.append)
        before = self.client.writes
        self.assertEqual(fresh.publish(manifest(*self.base)), "published")
        self.assertEqual(self.client.writes, before + 1)
        self.assertEqual(self.client.puts[-1][0], r2.INDEX_KEY)

    def test_expired_runs_leave_the_index(self):
        self.publisher.publish(manifest(*self.base))
        self.clock.now += 31
        self.publisher.publish(manifest(self.base[1]))
        self.assertEqual(list(self.index()["runs"]), ["gfs/2026111906"])

    def test_safe_publish_reports_failures_without_raising(self):
        publisher = r2.R2Publisher(FakeClient(fail=True), clock=self.clock, emit=self.events.append)
        events = []
        self.assertEqual(r2.safe_publish(publisher, manifest(*self.base), emit=events.append), "failed")
        self.assertEqual(events[0]["event"], "r2_publish_failed")
        self.assertEqual(events[0]["http_status"], 503)
        self.assertTrue(publisher.dirty)


class ModeTests(unittest.TestCase):
    CONFIG = {"PUBLIC_CHARTS_R2_ACCOUNT_ID": "a" * 32, "PUBLIC_CHARTS_R2_ACCESS_KEY_ID": "k",
              "PUBLIC_CHARTS_R2_SECRET_ACCESS_KEY": "s", "PUBLIC_CHARTS_R2_BUCKET": "cds-charts"}

    def test_defaults(self):
        self.assertEqual(r2.mode({}), "off")
        self.assertEqual(r2.mode({**{key: "" for key in self.CONFIG}, "PUBLIC_CHARTS_R2_MODE": ""}), "off")
        self.assertEqual(r2.mode(self.CONFIG), "shadow")
        self.assertEqual(r2.mode({**self.CONFIG, "PUBLIC_CHARTS_R2_MODE": "off"}), "off")
        self.assertEqual(r2.mode({**self.CONFIG, "PUBLIC_CHARTS_R2_MODE": "Required"}), "required")

    def test_invalid_or_incomplete_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            r2.mode({**self.CONFIG, "PUBLIC_CHARTS_R2_MODE": "maybe"})
        with self.assertRaises(ValueError):
            r2.mode({"PUBLIC_CHARTS_R2_MODE": "shadow"})


class FinalPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / MANIFEST).parent.mkdir(parents=True)
        (self.root / MANIFEST).write_text(json.dumps(manifest(row("gfs", "2026111900", 0, "gph500", "asia", "A"))),
                                          encoding="utf-8")

    def publisher(self, fail):
        return r2.R2Publisher(FakeClient(fail=fail), emit=lambda item: None)

    def test_off_mode_does_nothing(self):
        self.assertEqual(publish_r2(self.root, env={}), "off")

    def test_required_mode_blocks_cleanup_on_failure(self):
        env = {**ModeTests.CONFIG, "PUBLIC_CHARTS_R2_MODE": "required"}
        with self.assertRaises(PublicationError):
            publish_r2(self.root, env=env, publisher=self.publisher(True))
        self.assertEqual(publish_r2(self.root, env=env, publisher=self.publisher(False)), "published")

    def test_shadow_mode_reports_but_does_not_block(self):
        self.assertEqual(publish_r2(self.root, env=ModeTests.CONFIG, publisher=self.publisher(True)), "failed")


if __name__ == "__main__":
    unittest.main()
