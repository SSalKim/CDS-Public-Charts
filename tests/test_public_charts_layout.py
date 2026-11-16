import unittest

import matplotlib
matplotlib.use("Agg")
from matplotlib import pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextPath
import cartopy.crs as ccrs

from public_charts.chart_style import MAPS, ASIA_KIM_MAP, PUBLIC_PROFILE, heading_style, map_spec
from public_charts.plotter import tracked_title, fit_heading


class LayoutTests(unittest.TestCase):
    def test_cutover_is_by_initial_cycle_not_valid_time(self):
        for cycle in ("2026111512", "2026111518", "2026111600"):
            self.assertIs(map_spec("asia", cycle, PUBLIC_PROFILE), MAPS["asia"])
        self.assertIs(map_spec("asia", "2026111606", PUBLIC_PROFILE), ASIA_KIM_MAP)
        self.assertIs(map_spec("asia", "2026111700", PUBLIC_PROFILE), ASIA_KIM_MAP)

    def test_korea_and_local_weathermesh_maps_are_unchanged(self):
        self.assertIs(map_spec("hkor", "2026111606", PUBLIC_PROFILE), MAPS["hkor"])
        self.assertIs(map_spec("asia", "2026111606"), MAPS["asia"])
        self.assertEqual(heading_style("hkor", "2026111606", PUBLIC_PROFILE),
                         heading_style("hkor", "2026111600", PUBLIC_PROFILE))

    def test_asia_title_and_model_sizes_are_exact_not_scaled_twice(self):
        self.assertEqual(heading_style("asia", "2026111606", PUBLIC_PROFILE),
                         {"title": 12., "model": 11.5, "tracking": -.12})
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


if __name__ == "__main__":
    unittest.main()
