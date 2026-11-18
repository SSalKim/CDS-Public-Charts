import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from matplotlib import pyplot as plt

from public_charts import plotter
from public_charts import policy
from public_charts.chart_style import PUBLIC_PROFILE,SNOW_THRESHOLD,precipitation_threshold,winter_season
from public_charts.diagnostics import surface_snow_possible
from public_charts.extended_sources import ExtraFields,cached_snow_step,snow_step,winter_fields
from public_charts.sources import Decoded


class SnowRegionTests(unittest.TestCase):
    def test_rain_cutoff_and_winter_layout_have_distinct_october_boundaries(self):
        for month in range(1,13):
            cycle=f"2026{month:02}1800"
            with self.subTest(month=month):
                self.assertEqual(precipitation_threshold(cycle),.5 if 5<=month<=10 else .25)
                winter=month<=4 or month>=10
                self.assertEqual(winter_season(cycle),winter)
                for model in ("aifs","gfs","ecmwf","cmc","icon","ukmo"):
                    self.assertEqual(policy.winter(model,cycle),winter)
                self.assertFalse(policy.winter("aigfs",cycle))
                self.assertEqual(SNOW_THRESHOLD,.25)

    def test_revision_refreshes_only_supported_winter_snow_composites_and_aliases(self):
        for model in ("aifs","gfs","cmc","ecmwf","icon","ukmo"):
            for cycle in ("2026111712","2026111800"):
                for product in ("surfce","acptot","acptot1","acrain","acrain1"):
                    with self.subTest(model=model,cycle=cycle,product=product):
                        self.assertEqual(policy.render_version(cycle,model,product,"hkor"),9 if model=="ukmo" else 8)
            self.assertNotEqual(policy.render_version("2026071800",model,"acptot","hkor"),8)
            self.assertNotEqual(policy.render_version("2026111800",model,"gph500","hkor"),8)
            self.assertNotEqual(policy.render_version("2026111800",model,"surfce","asia"),8)
        self.assertNotEqual(policy.render_version("2026111800","aigfs","acptot","hkor"),8)

    def test_all_native_and_gfs_steps_use_strict_cutoff_and_fix_legacy_flags(self):
        latitude,longitude=np.array([35.,36.]),np.array([127.,128.])
        snow=np.array([[.02,.25],[.251,1.]])
        expected=np.array([[False,False],[True,True]])
        ones=np.ones_like(snow)
        surface={"latitude":latitude,"longitude":longitude,"temperature_2m":ones*268.,
                 "relative_humidity_2m":ones*90,"surface_pressure":ones*101000}
        def get(hour,key,**kwargs):
            values=ones*260 if key=="temperature_850" else snow
            return Decoded(values,latitude,longitude,{"startStep":0,"endStep":3})
        for model in ("aifs","ecmwf","cmc","icon","gfs"):
            with self.subTest(model=model),tempfile.TemporaryDirectory() as root:
                extra=ExtraFields(root,model,"2026111800")
                with patch.object(extra,"prefetch"),patch.object(extra,"get",side_effect=get), \
                     patch("public_charts.extended_sources.near_surface_fields",return_value=surface), \
                     patch("public_charts.extended_sources.collect_rainfall",return_value=(ones,Decoded(ones,latitude,longitude,{}),[])), \
                     patch("public_charts.extended_sources.gfs_sampled_snow",return_value=Decoded(snow,latitude,longitude,{})):
                    step=snow_step(extra,3,3)
                np.testing.assert_array_equal(step["snow_expected"],expected)
                legacy={**step,"snow_expected":np.ones_like(snow)}
                with patch.object(extra,"checkpoint",return_value=legacy), \
                     patch("public_charts.extended_sources.snow_step",side_effect=AssertionError("Use cached SWE")):
                    cached=cached_snow_step(extra,3,3)
                np.testing.assert_array_equal(cached["snow_expected"],expected)

    def test_exact_cutoff_is_excluded_without_quantizing_the_continuous_swe(self):
        values=np.array([0.,.01,.249,.25,np.nextafter(.25,np.inf),.6,100.])
        original=values.copy()
        display=plotter.snow_display_values(values)
        np.testing.assert_array_equal(display>.25,values>.25)
        self.assertLess(display[3],.25)
        np.testing.assert_array_equal(display[[0,1,2,4,5,6]],values[[0,1,2,4,5,6]])
        np.testing.assert_array_equal(values,original)

    def test_occurrence_conditions_are_or_not_and_and_invalid_inputs_fail(self):
        temperature=np.array([-1.,2.,0.,4.,0.,0.,np.nan])
        humidity=np.array([90.,50.,50.,95.,101.,80.,80.])
        rain=np.array([1.,1.,1.,1.,1.,0.,1.])
        np.testing.assert_array_equal(surface_snow_possible(rain,temperature,humidity),
                                      [True,True,True,False,False,False,False])

    def test_interval_and_total_hatch_paths_match_snow_shading_paths(self):
        latitude,longitude=np.linspace(29,45,25),np.linspace(115,138,31)
        x,y=np.meshgrid(longitude,latitude)
        snow=.6*np.exp(-((x-127)**2+(y-38)**2)/6)
        snow[8:11,12:15]=.25
        total=.6*np.exp(-((x-125)**2+(y-40)**2)/9)
        valid=np.ones_like(snow,dtype=bool)
        valid[15,15]=False
        arrays={"latitude":latitude,"longitude":longitude,"pressure_msl":101000+10*y,
                "precipitation":np.ones_like(snow),"snowfall_water_equivalent":snow,
                "total_snowfall_water_equivalent":total,"snow_expected":np.ones_like(snow),
                "geopotential_925":(810+5*(y-38))*9.80665,"geopotential_850":(1500+5*(y-38))*9.80665,
                "geopotential_1000":(210+0*y)*9.80665,"temperature_925":273.15+y-38,
                "wet_bulb_2m":1.2+.5*(y-38),"wind_u_925":5+0*x,"wind_v_925":5+0*y}
        close=plt.close
        paths={}
        def capture(figure):
            if hasattr(figure,"axes"):
                for artist in figure.axes[0].collections:
                    if artist.get_gid() in {"snow-area-hatch","shaded-field"}:
                        paths[artist.get_gid()]=[p.deepcopy() for p in artist.get_paths()]
            close(figure)
        for family,key in (("acptot","snowfall_water_equivalent"),("acrain","total_snowfall_water_equivalent")):
            with self.subTest(family=family):
                masks={key:valid}
                with patch("cartopy.mpl.geoaxes.GeoAxes.add_feature"), \
                     patch.object(plotter,"korean_admin_boundaries",return_value=[]), \
                     patch.object(plotter.plt,"close",side_effect=capture):
                    plotter.render(arrays,"2026111800",24,family,"hkor",None,
                                   accumulation=arrays["precipitation"],precipitation_field="precipitation",
                                   profile=PUBLIC_PROFILE,field_masks=masks,return_frame=True)
                    hatch=paths["snow-area-hatch"][0]
                    plotter.render(arrays,"2026111800",24,family+"_snow","hkor",None,
                                   profile=PUBLIC_PROFILE,field_masks=masks,return_frame=True)
                    shaded=paths["shaded-field"][0]
                self.assertGreater(len(hatch.vertices),0)
                np.testing.assert_array_equal(hatch.vertices,shaded.vertices)
                np.testing.assert_array_equal(hatch.codes,shaded.codes)

    def test_window_mask_is_not_the_last_steps_flag_and_cache_is_corrected(self):
        ones=np.ones((2,2))
        latitude,longitude=np.array([35.,36.]),np.array([127.,128.])
        base={"latitude":latitude,"longitude":longitude,"precipitation":ones}
        base.update({key:ones for key in ("pressure_msl","terrain_height","geopotential_925",
                     "geopotential_850","temperature_925","temperature_850","wind_u_925","wind_v_925")})
        def step(extra,end,cadence):
            snow=np.array([[.13,.125],[0.,.3]]) if end==3 else np.array([[.13,.125],[0.,0.]])
            return {"latitude":latitude,"longitude":longitude,"temperature_2m":ones*273.15,
                    "relative_humidity_2m":ones*80,"surface_pressure":ones*101000,"snow_ratio":ones,
                    "snow_expected":np.zeros((2,2)),"snowfall_water_equivalent":snow,"snowfall_depth_diagnostic":snow}
        with tempfile.TemporaryDirectory() as root:
            extra=ExtraFields(root,"icon","2026111800")
            with patch("public_charts.extended_sources.snow_step",side_effect=step), \
                 patch.object(extra,"get",return_value=Decoded(ones,latitude,longitude,{})):
                fields,_=winter_fields(extra,base,6,6,cumulative=False)
            expected=np.array([[True,False],[False,True]])
            np.testing.assert_array_equal(fields["snow_expected"],expected)
            with patch.object(extra,"get",side_effect=AssertionError("Cache should avoid raw downloads")):
                cached,_=winter_fields(extra,base,6,6,cumulative=False)
            np.testing.assert_array_equal(cached["snow_expected"],expected)


if __name__=="__main__":
    unittest.main()
