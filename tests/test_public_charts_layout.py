import unittest
from unittest.mock import patch
import numpy as np

import matplotlib
matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextPath
import cartopy.crs as ccrs

from public_charts.chart_style import (MAPS, ASIA_KIM_MAP, KOREA_KIM_MAP, GRID_BOUNDS,
                                       PUBLIC_PROFILE, heading_style, map_spec, source_note_alpha)
from public_charts.plotter import tracked_title, fit_heading
from public_charts import plotter


class LayoutTests(unittest.TestCase):
    def test_cutover_is_by_initial_cycle_not_valid_time(self):
        for cycle in ("2026111512", "2026111518", "2026111600"):
            self.assertIs(map_spec("asia", cycle, PUBLIC_PROFILE), MAPS["asia"])
        self.assertIs(map_spec("asia", "2026111606", PUBLIC_PROFILE), ASIA_KIM_MAP)
        self.assertIs(map_spec("asia", "2026111700", PUBLIC_PROFILE), ASIA_KIM_MAP)

    def test_korea_cutover_preserves_old_cycles_and_local_weathermesh(self):
        for cycle in ("2026111512", "2026111518", "2026111600"):
            self.assertIs(map_spec("hkor", cycle, PUBLIC_PROFILE), MAPS["hkor"])
        for cycle in ("2026111606", "2026111700"):
            self.assertIs(map_spec("hkor", cycle, PUBLIC_PROFILE), KOREA_KIM_MAP)
        self.assertIs(map_spec("hkor", "2026111606"), MAPS["hkor"])
        self.assertIs(map_spec("asia", "2026111606"), MAPS["asia"])
        self.assertEqual(heading_style("hkor", "2026111606", PUBLIC_PROFILE),
                         heading_style("hkor", "2026111600", PUBLIC_PROFILE))

    def test_revised_korea_viewport_stays_inside_available_grid(self):
        spec = KOREA_KIM_MAP
        globe = ccrs.Globe(ellipse=None, semimajor_axis=spec["radius"], semiminor_axis=spec["radius"])
        projection = ccrs.LambertConformal(central_longitude=126, central_latitude=38,
                                         standard_parallels=(30, 60), globe=globe)
        x = np.linspace(*spec["xlim"], 50)
        y = np.linspace(*spec["ylim"], 50)
        xx, yy = np.meshgrid(x, y)
        points = ccrs.PlateCarree().transform_points(projection, xx, yy)
        south, north, west, east = GRID_BOUNDS["hkor"]
        self.assertGreater(points[:, :, 0].min(), west)
        self.assertLess(points[:, :, 0].max(), east)
        self.assertGreater(points[:, :, 1].min(), south)
        self.assertLess(points[:, :, 1].max(), north)

    def test_asia_title_and_model_sizes_are_exact_not_scaled_twice(self):
        self.assertEqual(heading_style("asia", "2026111606", PUBLIC_PROFILE),
                         {"title": 12., "model": 11.5, "tracking": -.12, "width_scale": .96})
        self.assertAlmostEqual(heading_style("asia", "2026111600", PUBLIC_PROFILE)["title"], 13.635)

    def test_tracking_does_not_shrink_glyph_height(self):
        prop = FontProperties(family="DejaVu Serif", weight="bold", size=12)
        normal = TextPath((0, 0), "MSLP(hPa) and Accumulated Precipitation(mm)", prop=prop).get_extents()
        tracked = tracked_title("MSLP(hPa) and Accumulated Precipitation(mm)", prop, -.12).get_extents()
        self.assertAlmostEqual(normal.height, tracked.height)
        self.assertLess(tracked.width, normal.width)
        self.assertGreater(tracked.width, normal.width * .95)

    def test_long_title_still_fits_before_model_name(self):
        fig = plt.figure(figsize=(10, 7.282), dpi=160)
        try:
            heading = fig.text(.001, .98, "500hPa GPH(m), Temp(C), Relative Vorticity(E-5/s)",
                               size=12, family="DejaVu Serif", weight="bold", va="bottom")
            fit_heading(fig, heading, .66, tracking=-.12)
            patch = fig.artists[-1]
            self.assertFalse(heading.get_visible())
            self.assertLessEqual(patch.get_window_extent().x1, .66 * fig.bbox.width + .01)
            self.assertAlmostEqual(heading.get_fontsize(), 12)
        finally:
            plt.close(fig)

    def test_mindoro_is_inside_and_sri_lanka_west_coast_is_clipped(self):
        spec = ASIA_KIM_MAP
        globe = ccrs.Globe(ellipse=None, semimajor_axis=spec["radius"], semiminor_axis=spec["radius"])
        projection = ccrs.LambertConformal(central_longitude=126, central_latitude=38,
                                         standard_parallels=(30, 60), globe=globe)
        x, y = projection.transform_point(121, 13, ccrs.PlateCarree())
        self.assertTrue(spec["xlim"][0] < x < spec["xlim"][1])
        self.assertTrue(spec["ylim"][0] < y < spec["ylim"][1])
        west, _ = projection.transform_point(79.7, 8, ccrs.PlateCarree())
        east, _ = projection.transform_point(81.7, 8, ccrs.PlateCarree())
        self.assertLess(west, spec["xlim"][0])
        self.assertGreater(east, spec["xlim"][0])

    def test_model_compression_preserves_height_and_right_alignment(self):
        fig = plt.figure(figsize=(10, 7.282), dpi=160)
        try:
            model = fig.text(.94, .98, "ECMWF AIFS-Single (N320)", size=11.5,
                             family="DejaVu Serif", weight="bold", ha="right", va="bottom")
            model.set_gid("model-title")
            original = model.get_window_extent(fig.canvas.get_renderer())
            self.assertEqual(fit_heading(fig, model, .94, width_scale=.96), .96)
            patch = fig.artists[-1]
            self.assertEqual(patch.get_gid(), "compressed-model-title")
            self.assertAlmostEqual(patch.get_window_extent().x1, original.x1)
            self.assertAlmostEqual(model.get_fontsize(), 11.5)
        finally:
            plt.close(fig)

    def test_note_transparency_changes_only_for_new_public_cycles(self):
        self.assertEqual(source_note_alpha("2026111600", PUBLIC_PROFILE), .9)
        self.assertEqual(source_note_alpha("2026111606", PUBLIC_PROFILE), .65)
        self.assertEqual(source_note_alpha("2026111606"), .9)

    def test_asia_model_names_share_the_existing_gfs_baseline_and_right_edge(self):
        from public_charts.policy import TITLES
        fig = plt.figure(figsize=(10, 7.282), dpi=160)
        try:
            def draw(name, reference=None):
                model = fig.text(.945, .975, name, size=11.5, family="DejaVu Serif",
                                 weight="bold", ha="right", va="bottom", color="blue")
                model.set_gid("model-title")
                fit_heading(fig, model, .945, width_scale=.96, baseline_reference=reference)
                return fig.artists[-1]
            reference = TITLES["gfs"]
            old_gfs = draw(reference)
            baseline = old_gfs.get_transform().transform((0, 0))[1]
            for name in TITLES.values():
                with self.subTest(name=name):
                    aligned = draw(name, reference)
                    self.assertAlmostEqual(aligned.get_transform().transform((0, 0))[1], baseline)
                    self.assertAlmostEqual(aligned.get_window_extent().x1, old_gfs.get_window_extent().x1)
            np.testing.assert_allclose(draw(reference, reference).get_transform().get_matrix(),
                                       old_gfs.get_transform().get_matrix())
        finally:
            plt.close(fig)

    def test_interval_snow_panel_has_integer_maximum_annotation(self):
        latitude, longitude = np.linspace(29, 45, 161), np.linspace(115, 138, 231)
        x, y = np.meshgrid(longitude, latitude)
        arrays = {"latitude": latitude, "longitude": longitude,
                  "snowfall_water_equivalent": 4 * np.exp(-((x-127)**2+(y-38)**2)),
                  "geopotential_925": (810+5*(y-38))*9.80665,
                  "geopotential_850": (1500+5*(y-38))*9.80665,
                  "geopotential_1000": (210+0*y)*9.80665,
                  "temperature_925": 273.15+y-38, "wet_bulb_2m": 1.2+.5*(y-38),
                  "wind_u_925": 5+0*x, "wind_v_925": 5+0*y}
        labels = []
        close = plt.close
        def capture(figure):
            if hasattr(figure, "axes"):
                labels.extend(text.get_text() for axes in figure.axes for text in axes.texts
                              if text.get_gid() == "precipitation-extreme")
            close(figure)
        with patch("cartopy.mpl.geoaxes.GeoAxes.add_feature"), \
             patch.object(plotter, "korean_admin_boundaries", return_value=[]), \
             patch.object(plotter.plt, "close", side_effect=capture):
            plotter.render(arrays, "2026111606", 24, "acptot_snow", "hkor", None,
                           profile=PUBLIC_PROFILE, return_frame=True)
        self.assertIn("4", labels)

    def test_ukmo_diagnostic_label_is_above_snow_threshold_without_extra_source_line(self):
        latitude,longitude=np.linspace(29,45,41),np.linspace(115,138,47)
        x,y=np.meshgrid(longitude,latitude)
        snow=4*np.exp(-((x-127)**2+(y-38)**2))
        arrays={"latitude":latitude,"longitude":longitude,"pressure_msl":101000+10*y,
                "precipitation":snow,"snowfall_water_equivalent":snow,"snowfall_depth_diagnostic":snow,
                "total_snowfall_water_equivalent":snow,"total_snowfall_depth_diagnostic":snow,
                "geopotential_925":(810+5*(y-38))*9.80665,"geopotential_850":(1500+5*(y-38))*9.80665,
                "geopotential_1000":(210+0*y)*9.80665,"temperature_925":273.15+y-38,
                "wet_bulb_2m":1.2+.5*(y-38),"wind_u_925":5+0*x,"wind_v_925":5+0*y}
        close=plt.close
        for product in ("acptot_snow","acptot_snowdepth","acrain_snow","acrain_snowdepth","acptot"):
            with self.subTest(product=product):
                def capture(figure):
                    if hasattr(figure,"axes"):
                        texts={text.get_gid():text for text in figure.texts}
                        threshold=texts["rain-threshold-note"]
                        if product.endswith(("snow","snowdepth")):
                            label=texts["diagnostic-snow-note"]
                            self.assertEqual(label.get_text(),"Diagnostic")
                            renderer=figure.canvas.get_renderer()
                            self.assertGreaterEqual(label.get_window_extent(renderer).y0,threshold.get_window_extent(renderer).y1)
                            self.assertIn("Snow > 0.25",threshold.get_text())
                            self.assertGreater(threshold.get_window_extent(renderer).y0,0)
                        else:
                            self.assertNotIn("diagnostic-snow-note",texts)
                        source=next(t for ax in figure.axes for t in ax.texts if t.get_gid()=="source-note")
                        self.assertEqual(source.get_text(),"Plotted by WooJin Kim\nData sourced from Met Office")
                    close(figure)
                with patch("cartopy.mpl.geoaxes.GeoAxes.add_feature"), \
                     patch.object(plotter,"korean_admin_boundaries",return_value=[]), \
                     patch.object(plotter.plt,"close",side_effect=capture):
                    plotter.render(arrays,"2026111800",54,product,"hkor",None,accumulation=snow,
                                   profile=PUBLIC_PROFILE,model_id="ukmo",precipitation_field="precipitation",
                                   source_note="Data sourced from Met Office",diagnostic_label="Diagnostic",return_frame=True)

    def test_diagnostic_label_does_not_move_across_forecast_dates(self):
        latitude,longitude=np.linspace(29,45,15),np.linspace(115,138,21)
        x,y=np.meshgrid(longitude,latitude)
        snow=np.zeros_like(x)
        arrays={"latitude":latitude,"longitude":longitude,"pressure_msl":101000+10*y,
                "precipitation":snow,"total_snowfall_water_equivalent":snow}
        positions=[]
        close=plt.close
        def capture(figure):
            if hasattr(figure,"texts"):
                label=next(t for t in figure.texts if t.get_gid()=="diagnostic-snow-note")
                positions.append(tuple(label.get_position()))
            close(figure)
        with patch("cartopy.mpl.geoaxes.GeoAxes.add_feature"), \
             patch.object(plotter,"korean_admin_boundaries",return_value=[]), \
             patch.object(plotter.plt,"close",side_effect=capture):
            for hour in (0,3,24,84,120,240):
                plotter.render(arrays,"2026113000",hour,"acrain_snow","hkor",None,
                               profile=PUBLIC_PROFILE,model_id="gfs",diagnostic_label="Diagnostic",return_frame=True)
        self.assertEqual(len(positions),6)
        self.assertEqual(len(set(positions)),1)


if __name__ == "__main__":
    unittest.main()
