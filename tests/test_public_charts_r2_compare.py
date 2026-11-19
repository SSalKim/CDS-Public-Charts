from datetime import datetime, timedelta, timezone
import unittest

from public_charts import r2
from public_charts_r2_compare import run_checks

BASE = "https://cds4.example.test/"
ORIGIN = "https://ssalkim.github.io"
NOW = datetime(2026, 11, 19, 9, tzinfo=timezone.utc)
URL = "https://drive.usercontent.google.com/download?id={}&export=view&authuser=0"


def manifest(*ids):
    return {"models": {"gfs": {"title": "GFS", "published": True, "retention_cycles": 9}},
            "charts": [{"model": "gfs", "cycle": "2026111900", "hour": hour, "product": "gph500", "domain": "asia",
                        "file_id": file_id, "url": URL.format(file_id)} for hour, file_id in ids]}


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}

    def json(self):
        return self.body


class FakeR2:
    """Serves what the coordinator would publish for ``source``, with adjustable headers."""

    def __init__(self, source, *, generated_at=NOW - timedelta(minutes=2), cdn="HIT", cors=ORIGIN):
        models, fragments = r2.split_manifest(source)
        revisions = {run: r2.revision(f) for run, f in fragments.items()}
        index = r2.build_index(models, revisions, {run: len(f["charts"]) for run, f in fragments.items()},
                               generated_at.isoformat())
        self.objects = {r2.INDEX_KEY: (index, r2.INDEX_CACHE)}
        self.objects.update({r2.fragment_key(run, revisions[run]): (fragment, r2.IMMUTABLE_CACHE)
                             for run, fragment in fragments.items()})
        self.headers = {"cf-cache-status": cdn, **({"Access-Control-Allow-Origin": cors} if cors else {})}
        self.headers_seen = []

    def get(self, url, headers=None, timeout=None):
        self.headers_seen.append(headers or {})
        key = url[len(BASE):]
        if key not in self.objects:
            return Response(404)
        body, cache = self.objects[key]
        return Response(200, body, {"Cache-Control": cache, **self.headers})


def history(*manifests):
    return [(f"{index:040x}", f"2026-11-19T08:{59 - index:02}:00+00:00", item) for index, item in enumerate(manifests)]


class CompareTests(unittest.TestCase):
    def check(self, server, git):
        return run_checks(BASE, ORIGIN, git, session=server, now=NOW)

    def test_r2_matching_the_newest_git_manifest_passes(self):
        newest = manifest((0, "A"), (3, "B"))
        report = self.check(FakeR2(newest), history(newest, manifest((0, "A"))))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["facts"]["commits_behind"], 0)
        self.assertEqual(report["facts"]["fragments_checked"], 1)
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["facts"]["git_manifests_checked"], 2)

    def test_r2_lagging_one_publication_is_not_a_failure(self):
        older = manifest((0, "A"))
        report = self.check(FakeR2(older), history(manifest((0, "A"), (3, "B")), older))
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["facts"]["commits_behind"], 1)

    def test_content_that_never_existed_in_git_fails_with_details(self):
        report = self.check(FakeR2(manifest((0, "X"))), history(manifest((0, "A")), manifest((0, "B"))))
        self.assertFalse(report["ok"])
        self.assertIn("matches none of the last 2", report["problems"][0])
        self.assertEqual(report["facts"]["difference_from_latest_git"]["different"], ["gfs/2026111900"])

    def test_corrupt_or_missing_fragments_fail(self):
        source = manifest((0, "A"))
        server = FakeR2(source)
        key = next(key for key in server.objects if key != r2.INDEX_KEY)
        body, cache = server.objects[key]
        server.objects[key] = ({**body, "charts": [[0, "asia", "gph500", "Z"]]}, cache)
        self.assertIn("revision mismatch", " ".join(self.check(server, history(source))["problems"]))
        del server.objects[key]
        self.assertIn("HTTP 404", " ".join(self.check(server, history(source))["problems"]))

    def test_viewer_origin_must_be_allowed(self):
        source = manifest((0, "A"))
        server = FakeR2(source, cors=None)
        report = self.check(server, history(source))
        self.assertFalse(report["ok"])
        self.assertIn("CORS", report["problems"][0])
        self.assertEqual(server.headers_seen[0], {"Origin": ORIGIN})

    def test_uncached_or_stale_index_only_warns(self):
        source = manifest((0, "A"))
        report = self.check(FakeR2(source, cdn="DYNAMIC", generated_at=NOW - timedelta(minutes=60)), history(source))
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["warnings"]), 2)

    def test_missing_index_fails(self):
        server = FakeR2(manifest((0, "A")))
        del server.objects[r2.INDEX_KEY]
        report = self.check(server, history(manifest((0, "A"))))
        self.assertFalse(report["ok"])
        self.assertIn("HTTP 404", report["problems"][0])


if __name__ == "__main__":
    unittest.main()
