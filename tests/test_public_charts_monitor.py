import json
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

import requests

from public_charts.monitor import SourceMonitor, probe_source, probe_sources
from public_charts.parallel import LeadQueue
from public_charts.parallel import execute_leads
from public_charts import policy
from test_public_charts_parallel import every_product_renderer


def target(model="gfs",hour=0):
    return {"model":model,"cycle":"2026111712","hour":hour}


def response(status=200,content=b""):
    result=requests.Response()
    result.status_code=status
    result._content=content
    result._content_consumed=True
    result.headers["Last-Modified"]="Tue, 17 Nov 2026 15:00:00 GMT"
    return result


class ProbeTests(unittest.TestCase):
    def test_each_model_uses_existing_exact_cycle_sources(self):
        for model in ("aifs","ecmwf","gfs","aigfs","cmc","icon"):
            item=target(model,24)
            if model == "ecmwf":
                item["cycle"]="2026111718"
            urls=probe_sources(item)
            self.assertTrue(urls)
            self.assertTrue(all(url.startswith("https://") for url,_ in urls))
            if model != "icon":
                self.assertTrue(all("20261117" in url for url,_ in urls))
            if model in {"aifs","ecmwf","gfs","aigfs"}:
                self.assertTrue(all(kind != "metadata" for _,kind in urls))
            else:
                self.assertTrue(all(kind == "metadata" for _,kind in urls))

    def test_index_probe_does_not_download_grib_or_decode_fields(self):
        payload=b"1:0:d=2026111712:HGT:500 mb:anl:\n2:100:d=2026111712:TMP:500 mb:anl:\n"
        session=MagicMock()
        session.request.return_value.__enter__.return_value=response(content=payload)
        with patch("public_charts.monitor.requests.Session") as factory:
            factory.return_value.__enter__.return_value=session
            result=probe_source(target())
        self.assertTrue(result["available"])
        self.assertEqual(result["probe_kind"],"ncep")
        self.assertEqual(session.request.call_args.args[0],"GET")
        self.assertTrue(session.request.call_args.args[1].endswith(".idx"))
        self.assertEqual(len(result["source_id"]),64)
        self.assertNotIn("url",result)

    def test_metadata_probe_does_not_consume_the_large_body(self):
        received=response()
        received.raw=Mock()
        session=MagicMock()
        session.request.return_value.__enter__.return_value=received
        with patch("public_charts.monitor.requests.Session") as factory:
            factory.return_value.__enter__.return_value=session
            result=probe_source(target("icon"))
        self.assertTrue(result["available"])
        self.assertEqual(session.request.call_args.args[0],"HEAD")
        received.raw.read1.assert_not_called()

    def test_missing_is_pending_and_transport_or_parser_failure_can_fall_back(self):
        for received,expected in ((response(404),False),(response(503),None),(response(content=b"invalid"),False)):
            with self.subTest(status=received.status_code),patch("public_charts.monitor.requests.Session") as factory:
                session=factory.return_value.__enter__.return_value
                session.request.return_value.__enter__.return_value=received
                self.assertIs(probe_source(target())["available"],expected)

    def test_rate_limit_backoff_is_honored_without_admitting_a_download(self):
        received=response(429)
        received.headers["Retry-After"]="3600"
        with patch("public_charts.monitor.requests.Session") as factory:
            factory.return_value.__enter__.return_value.request.return_value.__enter__.return_value=received
            result=probe_source(target())
        self.assertIs(result["available"],False)
        self.assertEqual(result["retry_after"],3600)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.queue=LeadQueue([target(hour=h) for h in (0,3,6,9,12)],set(),100)
        self.queue.model_parallel_limit=4
        self.events=[]
        self.monitor=SourceMonitor(self.queue,self.directory.name,emit=self.events.append)

    def observation(self):
        return {"available":True,"source_id":"a"*64,"source_host":"example.invalid",
                "detected_at":"2026-11-17T15:01:00+00:00","last_modified":"Tue, 17 Nov 2026 15:00:00 GMT"}

    def test_observed_representative_input_is_not_marked_as_validated_or_complete(self):
        self.queue.source_monitor=True
        self.assertIsNone(self.queue.next(set()))
        self.monitor.accept(target(),self.observation())
        self.assertEqual(self.queue.next(set()),target())
        self.assertFalse(self.queue.existing)
        self.assertFalse(self.queue.ready_cycles)
        self.assertEqual(self.events[-1]["modified_to_detected_seconds"],60)

    def test_failed_probe_is_not_a_permanent_production_gate(self):
        self.queue.source_monitor=True
        self.monitor.accept(target(),{"available":None,"error":"ReadTimeout"})
        self.assertEqual(self.queue.next(set()),target())

    def test_missing_source_waits_but_does_not_hide_another_ready_lead(self):
        self.queue.source_monitor=True
        self.monitor.accept(target(),{"available":False})
        self.monitor.accept(target(hour=3),self.observation())
        self.assertEqual(self.queue.next(set()),target(hour=3))

    def test_discovery_continues_while_all_four_calculators_are_occupied(self):
        self.queue.issued.update(("gfs","2026111712",h) for h in (0,3,6,9))
        seen=Event()
        def probe(item):
            self.assertEqual(item["hour"],12)
            seen.set()
            return self.observation()
        self.monitor.probe=probe
        self.monitor.start()
        try:
            self.assertTrue(seen.wait(3))
            deadline=time.monotonic()+3
            while not self.events or self.events[-1].get("event") != "source_detected":
                self.assertLess(time.monotonic(),deadline)
                time.sleep(.01)
            self.assertEqual(len(self.queue.issued),4)
        finally:
            self.monitor.close()

    def test_monitor_crash_releases_the_gate_to_the_validated_path(self):
        with patch.object(self.queue,"monitor_targets",side_effect=RuntimeError("test")):
            self.monitor.start()
            self.monitor.thread.join(3)
        self.assertFalse(self.monitor.thread.is_alive())
        self.assertFalse(self.queue.source_monitor)
        self.assertIsNotNone(self.queue.next(set()))

    def test_github_only_pool_drains_the_discovery_ready_queue(self):
        items=[target(hour=0),target(hour=3),target("aifs",0),target("aifs",6)]
        queue=LeadQueue(items,set(),100)
        monitor=SourceMonitor(queue,self.directory.name,probe=lambda item:self.observation(),emit=self.events.append)
        published=[]
        def consume(item,staged):
            published.extend(policy.key(item["model"],item["cycle"],item["hour"],r["product"],r["domain"])
                             for r in staged["artifacts"])
        monitor.start()
        try:
            execute_leads(queue,self.directory.name,2,consume,renderer=every_product_renderer,
                          streaming=True,refresh=lambda:items,refresh_seconds=.1,idle_seconds=1)
        finally:
            monitor.close()
        expected=set().union(*(policy.expected_keys(item["model"],item["cycle"],item["hour"]) for item in items))
        self.assertEqual(set(published),expected)
        self.assertEqual(len(published),len(expected))
        self.assertFalse(queue.issued)

    def test_detection_history_is_reused_only_for_metrics_not_admission(self):
        result=self.observation()
        from datetime import datetime,timezone
        result["detected_at"]=datetime.now(timezone.utc).isoformat()
        self.monitor.accept(target(),result)
        self.monitor.close()
        queue=LeadQueue([target()],set(),10)
        restored=SourceMonitor(queue,self.directory.name,emit=self.events.append)
        self.assertFalse(queue.source_ready)
        restored.accept(target(),result)
        self.assertEqual(self.events[-1]["event"],"source_probe")
        self.assertIn("first_detected_at",self.events[-1])

    def test_cache_failure_does_not_fail_production_shutdown(self):
        with patch("public_charts.monitor.atomic_json",side_effect=OSError("test")):
            self.monitor.close()
        self.assertEqual(self.events[-1]["event"],"source_monitor_cache_failed")

    def test_all_explicit_cache_actions_have_short_bounds(self):
        root=Path(__file__).resolve().parents[1]
        for name in ("public_charts.yml","public_charts_helper.yml"):
            source=(root/".github/workflows"/name).read_text()
            self.assertNotIn("uses: actions/cache@",source)
            import re
            for block in re.split(r"\n      - ",source):
                if "uses: actions/cache/" in block:
                    self.assertRegex(block,r"timeout-minutes: [12]\b")
                    self.assertIn("continue-on-error: true",block)
