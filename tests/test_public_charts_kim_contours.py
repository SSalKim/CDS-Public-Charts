import collections
import unittest
from unittest.mock import patch

import numpy as np

from public_charts import policy
from public_charts.chart_style import PUBLIC_PROFILE, contour_steps

OLD, NEW = "2026111906", policy.KMA_STYLE_CUTOVER


def bold_levels(domain, level, first, last, kma=True):
    steps = contour_steps(domain, level, PUBLIC_PROFILE, kma=kma)
    levels = np.arange(first, last + 1, steps["height"])
    return [int(value) for value in levels if (value - steps["height_anchor"]) % steps["height_major"] == 0]


class KimBoldLineTests(unittest.TestCase):
    def test_lower_level_heights_are_bold_every_fourth_contour_from_kim_anchors(self):
        for domain in ("asia", "hkor"):
            self.assertEqual(bold_levels(domain, 850, 1260, 1620), [1260, 1380, 1500, 1620])
            self.assertEqual(bold_levels(domain, 925, 570, 930), [570, 690, 810, 930])
            self.assertEqual(bold_levels(domain, 700, 2760, 3120), [2760, 2880, 3000, 3120])

    def test_korea_500_hpa_matches_kim_every_fourth_line(self):
        self.assertEqual(bold_levels("hkor", 500, 5220, 5580), [5220, 5340, 5460, 5580])
        steps = contour_steps("hkor", 500, PUBLIC_PROFILE, kma=True)
        self.assertEqual((steps["temperature"] * 4, steps["temperature_major"], steps["temperature_anchor"]), (4, 4, 0))

    def test_levels_already_matching_kim_are_unchanged(self):
        for domain, level in (("asia", 500), ("asia", 300), ("hkor", 300), ("asia", 200)):
            self.assertEqual(contour_steps(domain, level, PUBLIC_PROFILE, kma=True),
                             contour_steps(domain, level, PUBLIC_PROFILE, kma=False), (domain, level))
        self.assertEqual(bold_levels("asia", 500, 5100, 5580), [5100, 5340, 5580])
        self.assertEqual(bold_levels("asia", 300, 8640, 9120), [8640, 8880, 9120])

    def test_legacy_cycles_keep_their_previous_anchors(self):
        self.assertEqual(bold_levels("asia", 850, 1260, 1620, kma=False), [1320, 1440, 1560])
        self.assertEqual(bold_levels("hkor", 700, 2760, 3120, kma=False), [2820, 2940, 3060])


def upper_fields(level=500):
    latitude, longitude = np.arange(25, 50.01, .25), np.arange(110, 145.01, .25)
    lat, lon = np.meshgrid(latitude, longitude, indexing="ij")
    return {"latitude": latitude, "longitude": longitude, "terrain_height": np.zeros_like(lat),
            f"geopotential_{level}": (5500 + 4 * (lat - 37) ** 2 - 60 * np.sin(np.deg2rad(lon * 4))) * 9.80665,
            f"temperature_{level}": 253 - .6 * (lat - 37) + 2 * np.cos(np.deg2rad(lon * 5)),
            f"wind_u_{level}": 20 + 0 * lat, f"wind_v_{level}": 5 + 0 * lat}


class DrawingOrderAndSparseCentreTests(unittest.TestCase):
    def render(self, product, domain, arrays, cycle):
        import cartopy.mpl.geoaxes as geoaxes
        from public_charts import plotter
        calls = []
        original = geoaxes.GeoAxes.contour
        def spy(self, *args, **kwargs):
            calls.append((kwargs.get("colors"), kwargs.get("zorder")))
            return original(self, *args, **kwargs)
        annotations = []
        original_annotate = plotter.plt.Axes.annotate
        def note(self, text, *args, **kwargs):
            if str(kwargs.get("gid", "")).startswith("center-"):
                annotations.append(text)
            return original_annotate(self, text, *args, **kwargs)
        with patch.object(geoaxes.GeoAxes, "contour", spy), patch.object(plotter.plt.Axes, "annotate", note):
            plotter.render(arrays, cycle, 24, product, domain, None, profile=PUBLIC_PROFILE, model_id="gfs",
                           pressure_only=product == "surfce", return_frame=True)
        return calls, annotations

    def test_isotherms_are_drawn_below_height_contours(self):
        calls, _ = self.render("gph500", "hkor", upper_fields(), NEW)
        heights = [z for color, z in calls if color == "#0000ff"]
        temperatures = [z for color, z in calls if color == "red"]
        self.assertTrue(heights and temperatures)
        self.assertLess(max(temperatures), min(heights))
        legacy, _ = self.render("gph500", "hkor", upper_fields(), OLD)
        self.assertEqual({z for color, z in legacy if color == "red"}, {5})

    def test_korea_isotherm_numbers_every_two_degrees(self):
        from public_charts import plotter
        original = plotter.plt.Axes.clabel

        def isotherm_labels(cycle):
            seen = []
            def spy(self, contour_set, *args, **kwargs):
                seen.append([float(level) for level in kwargs.get("levels", args[0] if args else [])])
                return original(self, contour_set, *args, **kwargs)
            with patch.object(plotter.plt.Axes, "clabel", spy):
                self.render("gph500", "hkor", upper_fields(), cycle)
            # Isotherms are the only labelled set with sub-zero levels at 500 hPa.
            return [levels for levels in seen if levels and max(levels) <= 50 and min(levels) < 0]

        new = isotherm_labels(NEW)
        self.assertTrue(new and all(level % 2 == 0 for levels in new for level in levels))
        old = isotherm_labels(OLD)
        self.assertTrue(any(level % 2 for levels in old for level in levels), "Legacy cycles number every isotherm")

    def test_an_empty_region_gets_a_weaker_centre(self):
        latitude, longitude = np.arange(-5, 85.01, .5), np.arange(20, 220.01, .5)
        lat, lon = np.meshgrid(latitude, longitude, indexing="ij")
        strong = 14 * np.exp(-(((lat - 35) / 6) ** 2 + ((lon - 125) / 8) ** 2))
        weak = -.7 * np.exp(-(((lat - 55) / 5) ** 2 + ((lon - 60) / 6) ** 2))  # below the normal prominence
        arrays = {"latitude": latitude, "longitude": longitude, "terrain_height": np.zeros_like(lat),
                  "pressure_msl": (1012 + strong + weak) * 100}
        _, shown = self.render("surfce", "asia", arrays, NEW)
        counts = collections.Counter(text for text in shown if text in ("H", "L"))
        self.assertEqual(counts["H"], 1)
        self.assertEqual(counts["L"], 1, "The isolated weak low is shown because nothing else is nearby")


if __name__ == "__main__":
    unittest.main()
