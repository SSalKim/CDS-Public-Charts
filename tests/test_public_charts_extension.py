import tempfile
from datetime import datetime, timezone
import unittest
from unittest import mock

import numpy as np

from public_charts import extended_sources, policy
from public_charts.sources import Decoded, forecast_hours, native_steps


class ExtensionScheduleTest(unittest.TestCase):
    def test_gfs_aifs_aigfs_reach_360_h_from_the_cutover(self):
        for model in ("gfs", "aifs", "aigfs"):
            self.assertEqual(policy.hours(model, "2026112206")[-1], 240, model)
            hours = policy.hours(model, "2026112212")
            self.assertEqual(hours[-1], 360, model)
            self.assertEqual(hours[-3:], (336, 348, 360))
            self.assertEqual(forecast_hours(model)[-1], 360)

    def test_ecmwf_00_12z_add_only_the_leads_after_kma(self):
        self.assertFalse(policy.supports_cycle("ecmwf", "2026112200"))
        self.assertTrue(policy.supports_cycle("ecmwf", "2026112212"))
        self.assertTrue(policy.supports_cycle("ecmwf", "2026112306"))
        self.assertEqual(policy.hours("ecmwf", "2026112212"), tuple(range(252, 361, 12)))
        self.assertEqual(policy.hours("ecmwf", "2026112218")[-1], 144)
        for hour in policy.hours("ecmwf", "2026112300"):
            self.assertEqual(len(policy.expected_keys("ecmwf", "2026112300", hour)), 20)

    def test_ecmwf_candidates_add_00_12z_only_from_the_cutover(self):
        self.assertEqual(policy.cycle_candidates("ecmwf", datetime(2026, 11, 22, 11, tzinfo=timezone.utc), 3),
                         ["2026112206", "2026112118", "2026112106"])
        self.assertEqual(policy.cycle_candidates("ecmwf", datetime(2026, 11, 23, 1, tzinfo=timezone.utc), 3),
                         ["2026112300", "2026112218", "2026112212"])
        self.assertEqual(policy.cycle_candidates("cmc", datetime(2026, 11, 23, 8, tzinfo=timezone.utc)),
                         ["2026112300", "2026112212"])

    def test_ecmwf_cycles_every_six_hours_with_a_similar_retention(self):
        self.assertEqual(policy.CYCLE_STEP["ecmwf"], 6)
        self.assertEqual(policy.RETENTION["ecmwf"] * policy.CYCLE_STEP["ecmwf"], 54)

    def test_native_steps_follow_the_ecmwf_six_hour_output_after_144_h(self):
        steps = native_steps("ecmwf", 252)
        self.assertEqual(steps[:3], [0, 3, 6])
        self.assertEqual(steps[48:51], [144, 150, 156])
        self.assertEqual(steps[-1], 252)
        self.assertNotIn(243, steps)
        self.assertEqual(native_steps("gfs", 360)[-2:], [357, 360])
        self.assertEqual(native_steps("aifs", 360)[-2:], [354, 360])


class ExtensionRainfallTest(unittest.TestCase):
    def test_total_rain_asks_for_native_ecmwf_intervals(self):
        calls = []
        lat = np.array([30.0, 31.0])
        lon = np.array([120.0, 121.0])

        def fake_collect(provider, cycle, end, interval):
            calls.append((end, interval))
            return np.full((2, 2), float(interval)), Decoded(np.zeros((2, 2)), lat, lon, {}), {}

        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(extended_sources, "collect_rainfall", fake_collect), \
                mock.patch.object(extended_sources, "regional", lambda field: field):
            extra = extended_sources.ExtraFields(root, "ecmwf", "2026112212")
            total = extra.total_rain(252)
        self.assertEqual(calls[47], (144, 3))
        self.assertEqual(calls[48], (150, 6))
        self.assertEqual(calls[-1], (252, 6))
        self.assertEqual(sum(interval for _, interval in calls), 252)
        np.testing.assert_allclose(total.values, 252)
        self.assertEqual(total.metadata["endStep"], 252)

    def test_total_rain_rejects_leads_off_the_native_cadence(self):
        with tempfile.TemporaryDirectory() as root:
            extra = extended_sources.ExtraFields(root, "ecmwf", "2026112212")
            with self.assertRaises(ValueError):
                extra.total_rain(147)


if __name__ == "__main__":
    unittest.main()
