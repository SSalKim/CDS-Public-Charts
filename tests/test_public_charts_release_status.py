import json
import struct
import unittest
from datetime import datetime, timezone

from public_charts import r2
from public_charts import release_status as rs

NOW = datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1200, 900) + b"\x00" * 40


class FakeClient:
    def __init__(self, payload=None):
        self.payload, self.writes = payload, []

    def get(self, key):
        self.assertKey(key)
        return self.payload

    def put(self, key, body, *, cache_control):
        self.assertKey(key)
        self.writes.append(json.loads(body))
        self.payload = body

    @staticmethod
    def assertKey(key):
        assert key == rs.STATUS_KEY


class FakeProbe:
    def __init__(self, times):
        self.times, self.calls = times, []

    def __call__(self, url):
        self.calls.append(url)
        return self.times.get(url)


class Clock:
    def __init__(self):
        self.value = 0.

    def __call__(self):
        return self.value


class FakeResponse:
    def __init__(self, status, body, headers):
        self.status_code, self.headers = status, headers
        self.raw = type("Raw", (), {"read": lambda _self, size: body[:size]})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, url, **kwargs):
        return self.response


def status(probe, client=None, clock=None):
    return rs.ReleaseStatus(client or FakeClient(), probe=probe, wall=lambda: NOW, clock=clock or Clock(),
                            emit=lambda item: None)


class ReleaseStatusTest(unittest.TestCase):
    def test_recent_runs_skip_cycles_too_young_to_have_started(self):
        keys = {key for key, *_ in rs.recent_runs(NOW)}
        self.assertIn("kim_gdps/2026112306", keys)
        self.assertNotIn("kim_gdps/2026112312", keys)
        self.assertIn("ukmo/2026112300", keys)
        self.assertNotIn("ukmo/2026112306", keys)
        self.assertNotIn("kim_gdps/2026112118", keys)

    def test_complete_and_started_runs_get_their_times(self):
        probe = FakeProbe({
            rs.image_url("kim_gdps", "2026112300", 288): "2026-11-23T05:49:40+00:00",
            rs.image_url("kim_gdps", "2026112300", 0): "2026-11-23T03:20:54+00:00",
            rs.image_url("kim_gdps", "2026112306", 0): "2026-11-23T09:16:00+00:00",
        })
        tracker = status(probe)
        self.assertTrue(tracker.check())
        self.assertEqual(tracker.runs["kim_gdps/2026112300"],
                         {"first": "2026-11-23T03:20:54+00:00", "last": "2026-11-23T05:49:40+00:00", "leads": [0, 288]})
        self.assertEqual(tracker.runs["kim_gdps/2026112306"],
                         {"first": "2026-11-23T09:16:00+00:00", "latest": 0, "leads": [0, 84]})
        self.assertNotIn("kim_rdps/2026112306", tracker.runs)

    def test_a_running_run_reports_its_furthest_lead_with_few_requests(self):
        run = "2026112300"
        times = {rs.image_url("kim_gdps", run, hour): f"2026-11-23T04:{hour % 60:02}:00+00:00"
                 for hour in range(0, 151, 3)}
        probe = FakeProbe(times)
        tracker = status(probe)
        tracker.check()
        self.assertEqual(tracker.runs["kim_gdps/" + run]["latest"], 144)
        asked = [url for url in probe.calls if f"_{run}." in url and "kim_gdps" in url]
        self.assertLessEqual(len(asked), 2 + 5, "last, first and a binary search over 24 leads")
        for hour in range(153, 241, 3):
            times[rs.image_url("kim_gdps", run, hour)] = "2026-11-23T05:00:00+00:00"
        probe.calls.clear()
        tracker.check()
        self.assertEqual(tracker.runs["kim_gdps/" + run]["latest"], 240)
        searched = [url for url in probe.calls if f"_{run}." in url and "kim_gdps" in url]
        self.assertFalse(any(f"_s{hour:03}_" in url for url in searched for hour in range(12, 144, 12)),
                         "the search starts above the lead already seen")
        times[rs.image_url("kim_gdps", run, 288)] = "2026-11-23T05:49:40+00:00"
        tracker.check()
        self.assertNotIn("latest", tracker.runs["kim_gdps/" + run])
        self.assertEqual(tracker.runs["kim_gdps/" + run]["last"], "2026-11-23T05:49:40+00:00")

    def test_finished_runs_are_not_asked_again(self):
        probe = FakeProbe({rs.image_url("ecmwf", "2026112300", 240): "2026-11-23T06:58:22+00:00",
                           rs.image_url("ecmwf", "2026112300", 0): "2026-11-23T05:51:25+00:00"})
        tracker = status(probe)
        tracker.check()
        probe.calls.clear()
        self.assertFalse(tracker.check())
        self.assertNotIn(rs.image_url("ecmwf", "2026112300", 240), probe.calls)
        self.assertNotIn(rs.image_url("ecmwf", "2026112300", 0), probe.calls)

    def test_load_keeps_recent_runs_and_drops_old_ones(self):
        published = {"schema": 1, "generated_at": "x", "runs": {
            "ukmo/2026112300": {"first": "a", "last": "b", "leads": [0, 144]},
            "ukmo/2026112000": {"first": "a", "last": "b", "leads": [0, 144]}}}
        tracker = status(FakeProbe({}), client=FakeClient(json.dumps(published).encode()))
        tracker.load()
        self.assertEqual(set(tracker.runs), {"ukmo/2026112300"})

    def test_tick_writes_on_change_and_on_refresh_only(self):
        client, clock = FakeClient(), Clock()
        probe = FakeProbe({rs.image_url("kim_ldps", "2026112300", 48): "2026-11-23T04:47:18+00:00",
                           rs.image_url("kim_ldps", "2026112300", 0): "2026-11-23T03:52:25+00:00"})
        tracker = status(probe, client=client, clock=clock)
        tracker.tick()
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.writes[0]["runs"]["kim_ldps/2026112300"]["last"], "2026-11-23T04:47:18+00:00")
        self.assertEqual(client.writes[0]["schema"], 1)
        clock.value = 100
        tracker.tick()
        self.assertEqual(len(client.writes), 1)
        clock.value = rs.REFRESH_SECONDS + 1
        tracker.tick()
        self.assertEqual(len(client.writes), 2)

    def test_release_time_reads_last_modified_of_a_real_image(self):
        ok = FakeSession(FakeResponse(206, PNG, {"Last-Modified": "Mon, 23 Nov 2026 03:20:54 GMT"}))
        self.assertEqual(rs.release_time("u", session=ok), "2026-11-23T03:20:54+00:00")
        page = FakeSession(FakeResponse(200, b"<html>not found</html>" + b" " * 60, {}))
        self.assertIsNone(rs.release_time("u", session=page))
        missing = FakeSession(FakeResponse(404, b"", {}))
        self.assertIsNone(rs.release_time("u", session=missing))
        no_header = FakeSession(FakeResponse(206, PNG, {}))
        self.assertEqual(rs.release_time("u", session=no_header, now=lambda: NOW), NOW.isoformat())

    def test_status_key_is_an_allowed_r2_object(self):
        self.assertTrue(r2.KEY_PATTERN.fullmatch(rs.STATUS_KEY))


if __name__ == "__main__":
    unittest.main()
