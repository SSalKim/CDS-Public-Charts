from datetime import datetime, timezone
import time
import unittest

from public_charts import policy
from public_charts.kma_gate import KmaReleaseGate, gate_key, kma_image

NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)


def row(hour=252, product="gph500", domain="asia", cycle="2026112300"):
    return {"model": "ecmwf", "cycle": cycle, "hour": hour, "product": product, "domain": domain,
            "file_id": f"r2_{cycle}{hour}{product}{domain}", "size": 100, "sha256": "a" * 64,
            "url": "https://cds4.uzo1o1.party/x"}


class EcmwfKmaGateTests(unittest.TestCase):
    def test_extension_leads_wait_for_kma_240_of_the_same_chart(self):
        self.assertEqual(gate_key(row()), ("ecmwf", "2026112300", 240, "gph500", "asia"))
        self.assertEqual(gate_key(row(360)), gate_key(row()))
        self.assertEqual(gate_key(row(product="gph300"))[3], "gph200")
        self.assertEqual(gate_key(row(product="surfce", domain="hkor"))[3:], ("acptot", "hkor"))
        self.assertEqual(gate_key(row(product="acrain1", domain="hkor"))[3:], ("acrain", "hkor"))
        self.assertEqual(gate_key(row(cycle="2026112312"))[1:3], ("2026112312", 240))

    def test_six_hourly_runs_are_not_gated(self):
        for hour in (0, 144):
            entry = row(hour, cycle="2026112306")
            self.assertIsNone(gate_key(entry))
            self.assertEqual(len(policy.public_manifest([entry], now=NOW)["charts"]), 1)

    def test_kma_filenames_match_the_viewer_catalog(self):
        self.assertEqual(kma_image(gate_key(row())), ("ECMW", "ecmw_lc40_asia_gph500_s240_2026112300.gif"))
        self.assertEqual(kma_image(gate_key(row(product="acptot", domain="hkor"))),
                         ("ECMW", "ecmw_lc20_hkor_acptot_s240_2026112300.gif"))
        ukmo = {**row(156), "model": "ukmo"}
        self.assertEqual(kma_image(gate_key(ukmo)), ("GDPS", "ukum_erly_asia_gph500_s144_2026112300.png"))

    def test_unconfirmed_extension_rows_stay_private(self):
        entry = row()
        self.assertEqual(policy.public_manifest([entry], now=NOW)["charts"], [])
        entry["kma_confirmed_at"] = NOW.isoformat()
        self.assertEqual(policy.public_manifest([entry], now=NOW)["charts"], [])
        entry["kma_reference_hour"] = 240
        charts = policy.public_manifest([entry], now=NOW)["charts"]
        self.assertEqual(len(charts), 1)
        self.assertEqual(charts[0]["kma_reference_hour"], 240)

    def test_gate_probes_kma_240_once_per_chart_and_confirms_every_lead(self):
        probed = []
        def probe(key):
            probed.append(key)
            return NOW.isoformat()
        gate = KmaReleaseGate(probe=probe, clock=lambda: 0)
        rows = {hour: row(hour) for hour in (252, 264, 360)}
        rows["hkor"] = row(252, "acptot", "hkor")
        gate.poll(rows)
        deadline = time.monotonic() + 10
        while gate.pending and time.monotonic() < deadline:
            time.sleep(.02)
            if all(future.done() for future in gate.pending):
                break
        self.assertTrue(gate.poll(rows))
        gate.close()
        self.assertEqual(sorted(probed), [("ecmwf", "2026112300", 240, "acptot", "hkor"),
                                          ("ecmwf", "2026112300", 240, "gph500", "asia")])
        self.assertTrue(all(entry["kma_reference_hour"] == 240 for entry in rows.values()))
        self.assertEqual(len(policy.public_manifest(list(rows.values()), now=NOW)["charts"]), 4)


if __name__ == "__main__":
    unittest.main()
