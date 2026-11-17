import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from public_charts.extended_sources import DiagnosticDeferred, ExtraFields, winter_fields
from public_charts.sources import Decoded


class WorkBudgetTests(unittest.TestCase):
    def test_expired_budget_does_not_download_another_field(self):
        with tempfile.TemporaryDirectory() as directory:
            extra = ExtraFields(directory, "icon", "2026111612", deadline=10)
            with patch("public_charts.extended_sources.time.monotonic", return_value=11), \
                    patch.object(extra.provider, "fetch") as fetch:
                with self.assertRaises(DiagnosticDeferred) as error:
                    extra.get(108, "temperature_2m")
                self.assertEqual(error.exception.reason, "cumulative_work_budget")
                fetch.assert_not_called()

    def test_lock_wait_budget_does_not_release_another_writers_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = ExtraFields(directory, "icon", "2026111612")
            waiting = ExtraFields(directory, "icon", "2026111612", deadline=10)
            with writer.checkpoint_lock(), patch("public_charts.extended_sources.time.monotonic", return_value=11):
                with self.assertRaises(DiagnosticDeferred) as error:
                    with waiting.checkpoint_lock():
                        self.fail("An expired reader must not enter the writer section")
                self.assertEqual(error.exception.reason, "checkpoint_wait_budget")
            waiting.deadline = None
            with waiting.checkpoint_lock():
                pass

    def test_completed_precipitation_prefix_survives_budget_and_resumes_exactly(self):
        field = Decoded(np.ones((2, 2)), np.array([30., 31.]), np.array([120., 121.]), {})
        with tempfile.TemporaryDirectory() as directory:
            extra = ExtraFields(directory, "icon", "2026111612", deadline=10)
            with patch("public_charts.extended_sources.time.monotonic", return_value=0) as clock:
                def collect(*args):
                    clock.return_value = 11
                    return field.values, field, []
                with patch("public_charts.extended_sources.collect_rainfall", side_effect=collect):
                    with self.assertRaises(DiagnosticDeferred):
                        extra.total_rain(6)
            self.assertIsNotNone(extra.checkpoint("rain/003.npz"))
            self.assertIsNone(extra.checkpoint("rain/006.npz"))
            extra.deadline = None
            with patch("public_charts.extended_sources.collect_rainfall", return_value=(field.values, field, [])) as collect:
                total = extra.total_rain(6)
            self.assertEqual(collect.call_count, 1)
            self.assertEqual(collect.call_args.args[2], 6)
            np.testing.assert_array_equal(total.values, np.full((2,2), 2.))

    def test_snow_prefix_resumes_without_recounting_a_completed_interval(self):
        ones = np.ones((2,2))
        lat, lon = np.array([30., 31.]), np.array([120., 121.])
        step = {"latitude": lat, "longitude": lon, "temperature_2m": ones*273.15,
                "relative_humidity_2m": ones*80, "surface_pressure": ones*101000,
                "snow_ratio": ones*10, "snow_expected": ones,
                "snowfall_water_equivalent": ones, "snowfall_depth_diagnostic": ones}
        base = {"latitude": lat, "longitude": lon, "precipitation": ones}
        base.update({key: ones for key in ("pressure_msl", "terrain_height", "geopotential_925",
                    "geopotential_850", "temperature_925", "temperature_850", "wind_u_925", "wind_v_925")})
        with tempfile.TemporaryDirectory() as directory:
            extra = ExtraFields(directory, "icon", "2026111612", deadline=10)
            with patch("public_charts.extended_sources.time.monotonic", return_value=0) as clock:
                def calculate(*args):
                    clock.return_value = 11
                    return dict(step)
                with patch("public_charts.extended_sources.snow_step", side_effect=calculate):
                    with self.assertRaises(DiagnosticDeferred):
                        winter_fields(extra, base, 6, 3)
            self.assertIsNotNone(extra.checkpoint("snow/003.npz"))
            extra.deadline = None
            with patch("public_charts.extended_sources.snow_step", return_value=dict(step)) as calculate, \
                    patch.object(extra, "get", return_value=Decoded(ones, lat, lon, {})):
                result, _ = winter_fields(extra, base, 6, 3)
            self.assertEqual(calculate.call_count, 1)
            self.assertEqual(calculate.call_args.args[1], 6)
            np.testing.assert_array_equal(result["snowfall_water_equivalent"], ones)
            np.testing.assert_array_equal(result["total_snowfall_water_equivalent"], ones*2)
            np.testing.assert_array_equal(result["total_snowfall_depth_diagnostic"], ones*2)


if __name__ == "__main__":
    unittest.main()
