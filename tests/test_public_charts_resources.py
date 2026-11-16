import unittest
from unittest.mock import patch

from public_charts.resources import worker_count, cmc_worker_count, LaptopLease, ResourceSampler
from public_charts_runner import choose


class ResourceTests(unittest.TestCase):
    def test_four_cpus_and_sufficient_memory_use_four_workers(self):
        self.assertEqual(worker_count(memory=12*1024**3, cpus=4), 4)
        self.assertEqual(worker_count(memory=8*1024**3, cpus=16), 4)

    def test_wsl_memory_or_cpu_limits_reduce_parallelism(self):
        self.assertEqual(worker_count(memory=6*1024**3, cpus=16), 3)
        self.assertEqual(worker_count(memory=4*1024**3, cpus=16), 2)
        self.assertEqual(worker_count(memory=3*1024**3, cpus=16), 2)
        self.assertEqual(worker_count(memory=1*1024**3, cpus=16), 1)
        self.assertEqual(worker_count("4", memory=10*1024**3, cpus=2), 2)
        self.assertEqual(worker_count(local=True), 1)

    def test_observed_laptop_headroom_allows_three_total_but_only_two_cmc(self):
        free=int(6.4*1024**3)
        workers=worker_count(memory=free,cpus=16)
        self.assertEqual(workers,3)
        self.assertEqual(cmc_worker_count(workers,memory=free),2)
        self.assertEqual(worker_count("4",memory=free,cpus=16),3)

    def test_cmc_memory_limits_leave_room_for_large_native_grids(self):
        for gib,expected in ((2,1),(6,2),(10,3),(14,4)):
            self.assertEqual(cmc_worker_count(4,memory=gib*1024**3),expected)
        self.assertEqual(cmc_worker_count(1,memory=14*1024**3),1)

    def test_dedicated_chart_runner_is_selected_only_when_vtg_is_idle(self):
        chart = {"runners": [{"name": "LAPTOP-WOOJIN-CHARTS", "status": "online", "busy": False,
                              "labels": [{"name": name} for name in ("Linux", "cds-public-charts")]}]}
        vtg = {"runners": [{"busy": True, "labels": [{"name": "cds-vtg"}]}]}
        self.assertEqual(choose(chart, vtg), "ubuntu-24.04")
        vtg["runners"][0]["busy"] = False
        self.assertEqual(choose(chart, vtg), ["self-hosted", "linux", "cds-public-charts"])

    @patch.dict("os.environ", {}, clear=True)
    def test_github_hosted_needs_no_laptop_lock(self):
        lease = LaptopLease()
        self.assertTrue(lease.acquire())
        self.assertFalse(lease.vtg_busy())
        lease.close()

    def test_sampler_always_joins(self):
        sampler = ResourceSampler()
        sampler.start()
        values = sampler.finish()
        self.assertFalse(sampler.thread.is_alive())
        self.assertIn("peak_process_tree_rss_mib", values)

    @patch("public_charts.resources.windows_available_memory", return_value=4*1024**3)
    @patch("public_charts.resources.available_memory", return_value=9*1024**3)
    def test_sampler_keeps_windows_physical_headroom_separate_from_wsl_allowance(self, *_):
        sampler=ResourceSampler()
        sampler.sample()
        values=sampler.finish()
        self.assertEqual(values["lowest_windows_available_memory_mib"],4096.)
        self.assertEqual(values["lowest_available_memory_mib"],9216.)


if __name__ == "__main__":
    unittest.main()
