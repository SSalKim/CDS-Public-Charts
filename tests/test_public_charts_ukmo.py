from concurrent.futures import Future
import argparse
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from public_charts import policy
from public_charts.kma_gate import KmaReleaseGate,image_header_valid
from public_charts.ukmo import WinterDiagnostics,bounded_swe,estimate_surface_pressure,source_url,PROFILE_LEVELS
from public_charts.sources import Decoded
from public_charts.extended_sources import ExtraFields,gfs_sampled_snow,snow_checkpoint_name
from public_charts_run import resolve_plan,run


NOW=datetime(2026,11,18,3,tzinfo=timezone.utc)


def managed(product="acptot",cycle="2026111800",confirmed=False):
    result={"model":"ukmo","cycle":cycle,"hour":3,"product":product,"domain":"hkor",
            "size":100,"file_id":product+cycle,"url":"https://drive.usercontent.google.com/download?id="+product+cycle,"sha256":"a"*64}
    if confirmed: result["kma_confirmed_at"]=NOW.isoformat()
    return result


class UkmoPolicyTests(unittest.TestCase):
    def test_winter_only_original_cycles_and_korea_precipitation_only(self):
        self.assertEqual(len(policy.expected_keys("ukmo","2026111800",54)),4)
        self.assertEqual(policy.TITLES["ukmo"],"UKMO (UM N1280 L70)")
        self.assertEqual(policy.hours("ukmo")[-1],144)
        for cycle in ("2026111806","2026111818","2027051800","2027091812"):
            self.assertFalse(policy.supports_cycle("ukmo",cycle))
        for product,domain in (("surfce","asia"),("gph500","hkor"),("acptot","asia")):
            with self.assertRaises(ValueError): policy.key("ukmo","2026111800",54,product,domain)

    def test_cutover_does_not_backfill_older_cycles(self):
        plan=resolve_plan(["ukmo"],now=NOW,start_cycle="2026111512",requested_hours=[0])
        self.assertEqual(plan,[{"model":"ukmo","cycle":"2026111800","hour":0}])
        self.assertEqual(policy.render_version("2026111718"),3)
        self.assertEqual(policy.render_version("2026111800"),4)

    def test_unconfirmed_upload_is_not_public_and_confirmation_is_product_specific(self):
        rows=[managed(),managed("acrain",confirmed=True)]
        manifest=policy.public_manifest(rows,now=NOW)
        self.assertEqual([r["product"] for r in manifest["charts"]],["acrain"])
        self.assertIn("kma_confirmed_at",manifest["charts"][0])

    def test_48h_expiry_removes_public_row_and_allows_file_cleanup(self):
        row=managed(confirmed=True)
        self.assertEqual(len(policy.public_manifest([row],now=NOW)["charts"]),1)
        expiry=datetime(2026,11,20,0,tzinfo=timezone.utc)
        self.assertEqual(len(policy.public_manifest([row],now=expiry)["charts"]),1)
        self.assertEqual(policy.public_manifest([row],now=expiry+timedelta(seconds=1))["charts"],[])
        kept,delete=policy.retain([row],now=expiry+timedelta(seconds=1))
        self.assertEqual(kept,[])
        self.assertEqual(delete,[row["file_id"]])

    def test_future_ukmo_rows_cannot_be_released(self):
        self.assertEqual(policy.public_manifest([managed(cycle="2026111812",confirmed=True)],now=NOW)["charts"],[])

    def test_completed_private_images_wait_and_publish_without_rerendering(self):
        class Store:
            folder_id="isolated-test-folder"
            def verify_account_and_folder(self): pass
            def read_json(self,name,default): return self.data.get(name,default)
            def write_json(self,name,value): self.data[name]=json.loads(json.dumps(value))
            def inventory(self):
                return [{"id":r["file_id"],"size":r["size"],"appProperties":{"kind":"image","key":policy.row_key(r),"sha256":r["sha256"]}}
                        for r in self.data["state.json"]["rows"]]
        class Gate:
            release=False
            def poll(self,rows):
                changed=False
                for row in rows.values():
                    if self.release and row["product"] in {"acptot","acptot1"} and not row.get("kma_confirmed_at"):
                        row["kma_confirmed_at"]=NOW.isoformat();changed=True
                return changed
            def close(self): pass
        store=Store();gate=Gate()
        store.data={"state.json":{"schema_version":1,"rows":[{**managed(p),"render_version":4} for p in ("acptot","acptot1","acrain","acrain1")]}}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            args=argparse.Namespace(models=["ukmo"],cycle="2026111800",hours=[3],start_cycle=None,dry_run=False,
                workdir=root/"work",local=False,max_leads=1,force=False,workers=1,cache_root=None,
                max_bytes=1_000_000,manifest=root/"manifest.json",publish_incremental=True,fail_fast=True)
            with patch("public_charts_run.resolve_plan",return_value=[]),patch("public_charts_run.publish_manifest"), \
                 patch("public_charts.kma_gate.KmaReleaseGate",return_value=gate), \
                 patch("public_charts_run.execute_leads",side_effect=lambda *a,**k:k["on_idle"]()), \
                 patch("public_charts.policy.datetime") as clock:
                clock.now.return_value=NOW
                self.assertEqual(run(args,store),0)
                self.assertEqual(len(store.data["state.json"]["rows"]),4)
                self.assertEqual(store.data["manifest.json"]["charts"],[])
                gate.release=True
                self.assertEqual(run(args,store),0)
                self.assertEqual({r["product"] for r in store.data["manifest.json"]["charts"]},{"acptot","acptot1"})


class GateTests(unittest.TestCase):
    def test_html_empty_and_small_error_images_are_rejected(self):
        valid=b"\x89PNG\r\n\x1a\n"+b"\0\0\0\rIHDR"+struct.pack(">II",600,730)
        self.assertTrue(image_header_valid(valid))
        self.assertFalse(image_header_valid(b"<html>200 OK</html>"))
        self.assertFalse(image_header_valid(valid[:16]+struct.pack(">II",20,20)))

    def test_gate_is_nonblocking_deduplicates_layouts_and_does_not_release_other_product(self):
        gate=KmaReleaseGate(clock=lambda:0)
        gate.pool.shutdown()
        class Pool:
            def __init__(self): self.jobs=[]
            def submit(self,fn,key):
                future=Future();self.jobs.append((key,future));return future
            def shutdown(self,**kwargs): pass
        gate.pool=Pool()
        rows={i:managed(p) for i,p in enumerate(("acptot","acptot1","acrain","acrain1"))}
        self.assertFalse(gate.poll(rows))
        self.assertEqual(len(gate.pool.jobs),2)
        for key,future in gate.pool.jobs: future.set_result(NOW.isoformat() if key[2]=="acptot" else None)
        self.assertTrue(gate.poll(rows))
        self.assertTrue(rows[0].get("kma_confirmed_at"))
        self.assertTrue(rows[1].get("kma_confirmed_at"))
        self.assertNotIn("kma_confirmed_at",rows[2])
        self.assertFalse(gate.poll(rows))
        gate.close()


class FakeNative:
    def __init__(self): self.calls=[]
    def value(self,hour,token,unit,**kwargs):
        self.calls.append((hour,token))
        if token=="snowfall_rate": return np.full((2,2),.002/3600)
        if token.startswith("snowfall_rate_from_convection"): return np.zeros((2,2))
        if token.startswith("precipitation_accumulation"): return np.full((2,2),.001*kwargs["duration"])
        if token=="temperature_at_screen_level": return np.full((2,2),268.)
        if token=="relative_humidity_at_screen_level": return np.full((2,2),.9)
        raise AssertionError(token)
    def field(self,hour,token,levels,**kwargs):
        return {"value_850":np.full((2,2),260.),"flag_850":np.array([[0,0],[0,1]])},{}


def base():
    ones=np.ones((2,2))
    return {"latitude":np.array([35.,36.]),"longitude":np.array([127.,128.]),"pressure_msl":ones*101000,
            "temperature_2m":ones*268.,"relative_humidity_2m":ones*90,"surface_pressure":ones*100000,
            "valid_925":ones.astype(bool),"valid_850":ones.astype(bool)}


class UkmoDiagnosticsTests(unittest.TestCase):
    def test_each_native_interval_is_bounded_before_accumulating(self):
        with tempfile.TemporaryDirectory() as root:
            diag=WinterDiagnostics(root,"2026111800",base(),6)
            diag.native=FakeNative()
            step=diag.step(3)
            np.testing.assert_allclose(step["swe"],3.)
            np.testing.assert_allclose(step["raw_swe"],6.)
            self.assertFalse(step["sr_valid"][1,1])
            result=diag.prefix(6)
            np.testing.assert_allclose(result["swe"],6.)
            diag.native.calls.clear()
            np.testing.assert_allclose(diag.prefix(6)["swe"],6.)
            self.assertEqual(diag.native.calls,[])

    def test_after_f54_uses_one_native_3h_window(self):
        with tempfile.TemporaryDirectory() as root:
            diag=WinterDiagnostics(root,"2026111800",base(),57)
            diag.native=FakeNative()
            result=diag.step(57)
            np.testing.assert_allclose(result["rain"],3.)
            np.testing.assert_allclose(result["swe"],3.)
            self.assertNotIn((56,"snowfall_rate"),diag.native.calls)

    def test_zero_hour_is_unshaded_without_precipitation_download(self):
        with tempfile.TemporaryDirectory() as root:
            diag=WinterDiagnostics(root,"2026111800",base(),0)
            diag.native=FakeNative()
            fields,total,masks=diag.fields(0,cumulative=True)
            self.assertEqual(diag.native.calls,[])
            self.assertFalse(total.any())
            self.assertFalse(fields["total_snowfall_water_equivalent"].any())

    def test_invalid_amounts_are_not_silently_clipped(self):
        with self.assertRaises(ValueError): bounded_swe(np.array([-1.]),np.array([2.]))
        with self.assertRaises(ValueError): bounded_swe(np.array([1.]),np.array([np.nan]))

    def test_pressure_profile_extrapolation_and_native_url(self):
        z={f"value_{p}":np.full((2,2),8000*np.log(1020/p)) for p in PROFILE_LEVELS}
        terrain=np.array([[0.,400.],[1200.,2500.]])
        np.testing.assert_allclose(estimate_surface_pressure(z,terrain),102000*np.exp(-terrain/8000),rtol=1e-12)
        self.assertIn("20261120T0600Z-PT0054H00M",source_url("2026111800",54,"snowfall_rate"))
        with self.assertRaises(ValueError): source_url("2026111800",55,"snowfall_rate")


class GfsHourlyTests(unittest.TestCase):
    def test_phase_is_applied_to_each_hour_not_the_three_hour_total(self):
        grid=np.array([35.,36.]);lon=np.array([127.,128.]);one=np.ones((2,2))
        with tempfile.TemporaryDirectory() as root:
            extra=ExtraFields(root,"gfs","2026111800")
            def rainfall(provider,cycle,end,duration): return one*end,Decoded(one,grid,lon,{}),[]
            def phase(end,key): return Decoded(one*(end==1),grid,lon,{})
            with patch("public_charts.extended_sources.collect_rainfall",side_effect=rainfall) as collect,patch.object(extra,"get",side_effect=phase):
                result=gfs_sampled_snow(extra,3,3,grid,lon)
                np.testing.assert_allclose(result.values,1.)
                self.assertEqual([call.args[3] for call in collect.call_args_list],[1,1,1])
                gfs_sampled_snow(extra,3,3,grid,lon)
                self.assertEqual(collect.call_count,3)

    def test_native_cadence_after_f120_and_cache_isolation(self):
        grid=np.array([35.,36.]);lon=np.array([127.,128.]);one=np.ones((2,2))
        with tempfile.TemporaryDirectory() as root:
            extra=ExtraFields(root,"gfs","2026111800")
            with patch("public_charts.extended_sources.collect_rainfall",return_value=(one,Decoded(one,grid,lon,{}),[])) as collect,patch.object(extra,"get",return_value=Decoded(one,grid,lon,{})):
                gfs_sampled_snow(extra,123,6,grid,lon)
                self.assertEqual([c.args[3] for c in collect.call_args_list],[1,1,1,3])
            self.assertEqual(snow_checkpoint_name(extra,"snow/003.npz"),"gfs-hourly-v1/snow/003.npz")
            extra.cycle="2026111718"
            self.assertEqual(snow_checkpoint_name(extra,"snow/003.npz"),"snow/003.npz")


if __name__=="__main__": unittest.main()
