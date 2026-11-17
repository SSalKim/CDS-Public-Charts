from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import requests

from public_charts import input_cache, policy, producer
from public_charts.extended_sources import ExtraFields, winter_fields
from public_charts.icon import native, source_urls
from public_charts.sources import Decoded, HttpCache, NotReady, Provider, SourceDeferred, required_fields
import test_public_charts_pipeline as pipeline_tests
import test_public_charts_remote as remote_tests

message=remote_tests.message


class SourceBudgetTests(unittest.TestCase):
    def test_revoked_worker_stops_before_another_network_request(self):
        from public_charts.parallel import check_task_cancelled
        with tempfile.TemporaryDirectory() as directory, patch("public_charts.parallel._TASK_FLAGS",[1]), \
                patch("public_charts.parallel._TASK_SLOT",0), patch("public_charts.parallel._CANCELLED",None), \
                patch("public_charts.sources.check_cancelled",check_task_cancelled):
            client=HttpCache(directory)
            client.session=Mock()
            with self.assertRaises(SourceDeferred) as error:
                client.get("https://example.invalid/data.grib2")
            self.assertEqual(error.exception.reason,"task_revoked")
            client.session.get.assert_not_called()
    def test_read_budget_covers_all_retries_not_each_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            client=HttpCache(directory)
            client.session=Mock()
            client.session.get.side_effect=requests.ReadTimeout("slow")
            with patch("public_charts.sources.time.monotonic",side_effect=[0,0,50,50]), \
                    patch("public_charts.sources.time.sleep"), self.assertRaises(SourceDeferred):
                client.get("https://example.invalid/file.grib2")
            self.assertEqual(client.session.get.call_count,1)

    def test_all_transient_mirrors_defer_without_hiding_auth_or_bad_data(self):
        for error, expected in [(requests.ReadTimeout(),NotReady),(ValueError("bad cycle"),ValueError)]:
            with self.assertRaises(expected):
                Provider._mirrors(["a","b"],Mock(side_effect=error))
        response=requests.Response();response.status_code=403
        with self.assertRaises(requests.HTTPError):
            Provider._mirrors(["a","b"],Mock(side_effect=requests.HTTPError(response=response)))

    def test_production_collects_one_seasonal_upper_level(self):
        from public_charts.parallel import prepare_lead
        for cycle, upper in [("2026111706",300),("2027041706",300),("2027051706",200),("2026101706",200)]:
            wanted=required_fields("icon",upper)
            self.assertIn(f"temperature_{upper}",wanted)
            self.assertNotIn(f'temperature_{500-upper}',wanted)
            with tempfile.TemporaryDirectory() as directory, patch("public_charts.sources.collect",return_value=({}, {}, {})) as collect:
                prepare_lead({"model":"icon","cycle":cycle,"hour":0},directory)
                self.assertEqual(collect.call_args.kwargs["upper_level"],upper)
        self.assertTrue({"temperature_200","temperature_300"} <= required_fields())

    def test_icon_unhealthy_feed_is_temporarily_demoted_and_reprobed(self):
        health={}
        client=Mock()
        def fetch(url,**kwargs):
            if "/v1/" in url:
                raise requests.ReadTimeout()
            return b"complete"
        client.get.side_effect=fetch
        with patch("public_charts.icon._FEED_FAILURES",health), patch("public_charts.icon.time.monotonic",return_value=0) as clock, \
                patch("public_charts.icon.read_native",return_value=(np.ones(1),{})):
            for hour in (0,3,6):
                native(client,"2026111706",hour,"PMSL")
            self.assertEqual(sum("/v1/" in call.args[0] for call in client.get.call_args_list),2)
            clock.return_value=301
            native(client,"2026111706",9,"PMSL")
            self.assertIn("/v1/",client.get.call_args_list[-2].args[0])


class PreparedCacheTests(unittest.TestCase):
    def test_identity_hash_ttl_and_size_limit(self):
        target={"model":"gfs","cycle":"2026111706","hour":3}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            raw=root/"raw"
            fields=raw/"fields"/target["cycle"]/target["model"]/"003"
            fields.mkdir(parents=True)
            np.savez_compressed(fields/"fields.npz",values=np.ones((2,2)))
            (fields/"metadata.json").write_text(json.dumps(target))
            with patch("public_charts.input_cache.time.time",return_value=100):
                input_cache.save(root,target,raw)
                self.assertTrue(input_cache.restore(root,target,root/"next"))
                saved=input_cache.location(root,target)
                self.assertFalse(input_cache.valid(saved,{**target,"hour":6}))
                self.assertFalse(input_cache.valid(saved,target,100+24*3600+1))
                (saved/"fields.npz").write_bytes(b"broken")
                self.assertFalse(input_cache.valid(saved,target))
                input_cache.prune(root,max_bytes=0)
                self.assertFalse(saved.exists())


class ProgressLeaseTests(unittest.TestCase):
    setUp=remote_tests.RemoteCoordinatorTests.setUp
    heartbeat=remote_tests.RemoteCoordinatorTests.heartbeat
    def test_real_progress_renews_lease_but_replayed_progress_does_not(self):
        self.heartbeat()
        token,lease=next(iter(self.broker.leases.items()))
        initial=lease["expires_at"]
        self.clock=initial-30
        self.transport.push(message("heartbeat",generated_at=self.clock,capacity=2,active=[token],
                                    progress={token:{"sequence":1,"at":self.clock}}))
        self.broker.tick()
        renewed=lease["expires_at"]
        self.assertGreater(renewed,initial)
        self.clock+=60
        self.transport.push(message("heartbeat",generated_at=self.clock,capacity=2,active=[token],
                                    progress={token:{"sequence":1,"at":self.clock}}))
        self.broker.tick()
        self.assertEqual(lease["expires_at"],renewed)


class DiagnosticSeparationTests(unittest.TestCase):
    setUp=pipeline_tests.ProductionTests.setUp
    picture=staticmethod(pipeline_tests.ProductionTests.picture)
    def test_current_precipitation_is_published_even_when_total_is_deferred(self):
        entries=[]
        fields={**self.base,"precipitation":np.ones((2,2))}
        def winter(extra,base,hour,interval,*,cumulative=True):
            self.assertFalse(cumulative)
            return base,{}
        with patch.object(producer,"collect",return_value=(fields,{},{})), \
                patch.object(producer,"winter_fields",side_effect=winter), \
                patch.object(ExtraFields,"total_rain",side_effect=SourceDeferred()), \
                patch.object(producer,"render",side_effect=self.picture):
            result=producer.produce_lead("gfs","2026111706",6,self.root,set(),
                                        lambda product,*args,**kwargs:entries.append(product))
        self.assertFalse(result["complete"])
        self.assertTrue({"acptot","acptot1","surfce"} <= set(entries))
        self.assertNotIn("acrain",entries)

    def test_window_only_matches_cumulative_window_without_scanning_history(self):
        ones=np.ones((2,2))
        lat,lon=self.base["latitude"],self.base["longitude"]
        base={**self.base,"precipitation":ones}
        base.update({key:ones for key in ("geopotential_925","geopotential_850","temperature_925","temperature_850","wind_u_925","wind_v_925")})
        step={"latitude":lat,"longitude":lon,"temperature_2m":ones*273.15,"relative_humidity_2m":ones*80,
              "surface_pressure":ones*101000,"snow_ratio":ones*10,"snow_expected":ones,
              "snowfall_water_equivalent":ones,"snowfall_depth_diagnostic":ones}
        full=ExtraFields(self.root/"full","icon","2026111706")
        window=ExtraFields(self.root/"window","icon","2026111706")
        def get(*args,**kwargs):
            return Decoded(ones,lat,lon,{})
        with patch("public_charts.extended_sources.snow_step",side_effect=lambda *args:dict(step)) as calculate, \
                patch.object(full,"get",side_effect=get), patch.object(window,"get",side_effect=get):
            current,_=winter_fields(window,base,12,3,cumulative=False)
            self.assertEqual(calculate.call_count,1)
            reference,_=winter_fields(full,base,12,3)
        for key in ("snowfall_water_equivalent","snowfall_depth_diagnostic","wet_bulb_2m"):
            np.testing.assert_array_equal(current[key],reference[key])
        np.testing.assert_array_equal(reference["total_snowfall_depth_diagnostic"],ones*4)


if __name__ == "__main__":
    unittest.main()
