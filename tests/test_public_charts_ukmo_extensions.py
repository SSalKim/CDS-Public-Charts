from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from public_charts import policy
from public_charts.kma_gate import gate_key,KmaReleaseGate
from public_charts.ukmo import (RainDiagnostics,WinterDiagnostics,collect_ukmo,diagnostic_ends,
                               native_interval,produce_ukmo,source_url)
from public_charts_run import current_render_keys


NOW=datetime(2026,11,18,12,tzinfo=timezone.utc)


def row(hour=156,product="gph500",domain="asia",cycle="2026111800"):
    return {"model":"ukmo","cycle":cycle,"hour":hour,"product":product,"domain":domain,
            "file_id":f"{cycle}-{hour}-{product}-{domain}","size":100,"sha256":"a"*64,
            "url":"https://drive.usercontent.google.com/download?id=test"}


def base():
    ones=np.ones((2,2))
    return {"latitude":np.array([35.,36.]),"longitude":np.array([127.,128.]),
            "pressure_msl":ones*101000,"terrain_height":ones*0,"temperature_2m":ones*268,
            "relative_humidity_2m":ones*90,"surface_pressure":ones*100000,
            "valid_925":ones.astype(bool),"valid_850":ones.astype(bool)}


class UkmoExtensionPolicyTests(unittest.TestCase):
    def test_marker_revision_refreshes_ukmo_only(self):
        self.assertEqual(policy.render_version("2026111800","gfs"),4)
        self.assertEqual(policy.render_version("2026111800","ukmo"),5)
        rows={"old":{**row(),"render_version":4},"new":{**row(),"render_version":5},
              "gfs":{**row(),"model":"gfs","render_version":4}}
        self.assertEqual(current_render_keys(rows),{"new","gfs"})

    def test_diagnostic_layout_revision_refreshes_only_snow_composites(self):
        self.assertEqual(policy.render_version("2026111800","gfs","acptot","hkor"),8)
        self.assertEqual(policy.render_version("2026111800","ukmo","acrain1","hkor"),8)
        self.assertEqual(policy.render_version("2026111800","gfs","surfce","asia"),4)
        self.assertEqual(policy.render_version("2026111800","gfs","gph500","hkor"),4)
        self.assertEqual(policy.render_version("2026111800","aifs","acptot","hkor"),8)

    def test_full_catalog_only_at_new_cycles_or_extended_leads(self):
        for cycle in ("2026111806","2026111818","2027051806"):
            self.assertEqual(policy.hours("ukmo",cycle),tuple(range(0,70,3)))
            self.assertEqual(len(policy.expected_keys("ukmo",cycle,0)),20)
            self.assertEqual(len(policy.expected_keys("ukmo",cycle,69)),20)
        for hour in (156,168):
            self.assertEqual(len(policy.expected_keys("ukmo","2026111800",hour)),20)
        self.assertEqual(len(policy.expected_keys("ukmo","2026111800",144)),4)
        self.assertEqual(policy.hours("ukmo","2027051800"),(156,168))
        for cycle,hour in (("2026111806",72),("2026111800",150),("2026111800",162)):
            with self.assertRaises(ValueError): policy.expected_keys("ukmo",cycle,hour)

    def test_extended_rows_require_144_confirmation_but_off_cycles_do_not(self):
        entry=row()
        self.assertEqual(policy.public_manifest([entry],now=NOW)["charts"],[])
        entry["kma_confirmed_at"]=NOW.isoformat()
        self.assertEqual(policy.public_manifest([entry],now=NOW)["charts"],[])
        entry["kma_reference_hour"]=144
        self.assertEqual(len(policy.public_manifest([entry],now=NOW)["charts"]),1)
        off=row(69,cycle="2026111806")
        self.assertEqual(len(policy.public_manifest([off],now=NOW)["charts"]),1)
        self.assertIsNone(gate_key(off))
        self.assertEqual(policy.public_manifest([off],now=NOW+timedelta(days=3))["charts"],[])

    def test_gate_reuses_144_per_product_and_area_for_both_extensions(self):
        self.assertEqual(gate_key(row()),("2026111800",144,"gph500","asia"))
        self.assertEqual(gate_key(row(168)),gate_key(row()))
        self.assertNotEqual(gate_key(row(domain="hkor")),gate_key(row()))
        self.assertEqual(gate_key(row(product="gph300")),("2026111800",144,"gph200","asia"))
        self.assertEqual(gate_key(row(product="acptot1",domain="hkor")),("2026111800",144,"acptot"))
        gate=KmaReleaseGate()
        gate.confirmed[gate_key(row())]=NOW.isoformat()
        rows={i:row(h) for i,h in enumerate((156,168))}
        self.assertTrue(gate.poll(rows));gate.close()
        self.assertTrue(all(r["kma_reference_hour"]==144 for r in rows.values()))
        self.assertEqual(len(policy.public_manifest(list(rows.values()),now=NOW)["charts"]),2)

    def test_native_cadence_has_no_fabricated_147_or_153_inputs(self):
        self.assertEqual(diagnostic_ends(144,168),[150,156,162,168])
        self.assertEqual(native_interval(150),6)
        self.assertIn("PT0168H00M",source_url("2026111800",168,"pressure_at_mean_sea_level"))
        self.assertIn("PT0069H00M",source_url("2026111806",69,"pressure_at_mean_sea_level"))
        for cycle,hour in (("2026111806",72),("2026111800",147),("2026111800",169)):
            with self.assertRaises(ValueError): source_url(cycle,hour,"pressure_at_mean_sea_level")


class Native:
    def __init__(self,*args,**kwargs): self.records=[];self.requests=self.bytes=0;self.calls=[]
    def field(self,hour,token,levels=(),**kwargs):
        self.calls.append((hour,token,levels,kwargs))
        data={"latitude":np.array([35.,36.]),"longitude":np.array([127.,128.])}
        for level in levels:
            value=(8000*np.log(1020/level) if token.startswith("geopotential") else .9 if token.startswith("relative")
                   else 270. if token.startswith("temperature") else 90. if token.startswith("wind_direction") else 10.)
            data[f"value_{level}"]=np.full((2,2),value)
            data[f"flag_{level}"]=np.zeros((2,2),dtype=np.int8)
        return data,{}
    def value(self,hour,token,unit,**kwargs):
        self.calls.append((hour,token,(),kwargs))
        if token=="height_of_orography": value=0.
        elif token=="pressure_at_mean_sea_level": value=101000.
        elif token.startswith("temperature"): value=268.
        elif token.startswith("relative_humidity"): value=.9
        elif token=="snowfall_rate": value=.002/3600
        elif token.startswith("snowfall_rate_from_convection"): value=0.
        elif token.startswith("precipitation_accumulation"): value=.001*kwargs["duration"]
        else: raise AssertionError(token)
        return np.full((2,2),value)


class UkmoExtensionInputTests(unittest.TestCase):
    def test_full_collector_supplies_the_common_renderer_contract_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as root,patch("public_charts.ukmo.NativeReader",Native):
            data,meta,_=collect_ukmo("2026111806",69,root)
            for level in (300,500,700,850,925):
                self.assertIn(f"temperature_{level}",data)
                np.testing.assert_allclose(data[f"wind_u_{level}"],-10)
            for level in (700,850,925):
                self.assertTrue((data[f"specific_humidity_{level}"]>0).all())
            self.assertNotIn("geopotential_200",data)
            self.assertEqual(meta["collector_version"],2)
            with patch("public_charts.ukmo.NativeReader",side_effect=AssertionError("Cached")):
                self.assertEqual(collect_ukmo("2026111806",69,root)[2]["requests"],0)

    def test_six_hour_snow_and_rain_steps_are_summed_without_double_counting(self):
        with tempfile.TemporaryDirectory() as root:
            rain=RainDiagnostics(root,"2026111800",base());rain.native=Native()
            np.testing.assert_allclose(rain.window(144,156),12)
            self.assertEqual([h for h,token,*_ in rain.native.calls if token.startswith("precipitation")],[150,156])
            snow=WinterDiagnostics(root,"2026111800",base(),156);snow.native=Native()
            with patch("public_charts.ukmo.snow_ratio",return_value=(np.ones((2,2)),np.ones((2,2)),{})) as ratio:
                step=snow.step(150)
                self.assertEqual(ratio.call_args.kwargs["interval_hours"],6)
            np.testing.assert_allclose(step["rain"],6)
            np.testing.assert_allclose(step["swe"],6)
            self.assertNotIn(147,[call[0] for call in snow.native.calls])

    def test_full_zero_lead_produces_every_key_with_winter_alias_and_correct_layouts(self):
        with tempfile.TemporaryDirectory() as root:
            entries={};shapes={}
            def picture(*args,**kwargs):
                a=np.tile(np.array([0,255],dtype=np.uint8),(16,8));return Image.fromarray(a).convert("RGB")
            def publish(product,domain,path,alias_of=None):
                entries[policy.key("ukmo","2026111806",0,product,domain)]=alias_of
                if path:
                    with Image.open(path) as image: shapes[(product,domain)]=image.size
            with patch("public_charts.ukmo.collect_ukmo",return_value=(base(),{"sources":[]},{})), \
                 patch("public_charts.ukmo.RainDiagnostics.window",return_value=np.zeros((2,2))), \
                 patch("public_charts.ukmo.WinterDiagnostics.fields",return_value=(base(),np.zeros((2,2)),{})), \
                 patch("public_charts.plotter.render",side_effect=picture):
                result=produce_ukmo("2026111806",0,root,set(),publish)
            self.assertTrue(result["complete"])
            self.assertEqual(set(entries),policy.expected_keys("ukmo","2026111806",0))
            self.assertEqual(shapes[("acptot","hkor")],(1750,660))
            self.assertEqual(shapes[("acrain1","hkor")],(600,2000))
            self.assertEqual(entries[policy.key("ukmo","2026111806",0,"surfce","hkor")],("acptot","hkor"))


if __name__=="__main__": unittest.main()
