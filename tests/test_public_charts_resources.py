import unittest
from unittest.mock import patch

from public_charts.resources import worker_count, WorkerMemoryBudget, LaptopLease, ResourceSampler
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

    def test_observed_laptop_headroom_caps_only_the_total_pool(self):
        free=int(6.4*1024**3)
        workers=worker_count(memory=free,cpus=16)
        self.assertEqual(workers,3)
        self.assertEqual(worker_count("4",memory=free,cpus=16),3)

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


class MemoryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.free, self.host, self.now = 12*1024**3, None, 100.
        self.rss = {}
        self.budget = WorkerMemoryBudget(4,memory_reader=lambda:self.free,host_reader=lambda:self.host,
                                         rss_reader=lambda pid:self.rss.get(pid),clock=lambda:self.now)

    @staticmethod
    def target(model="gfs",hour=0):
        return {"model":model,"cycle":"2026111700","hour":hour}

    def test_any_model_can_fill_four_slots_when_memory_is_available(self):
        for model in ("cmc","icon","gfs","aifs"):
            with self.subTest(model=model):
                for hour in range(4):
                    self.assertTrue(self.budget.can_start())
                    self.budget.start("render",self.target(model,hour))
                self.assertFalse(self.budget.can_start())
                for hour in range(4):
                    self.budget.finish("render",self.target(model,hour))

    def test_actual_heavy_workers_pause_new_work_until_headroom_recovers(self):
        for pid,model in ((10,"aifs"),(11,"gfs")):
            item=self.target(model)
            self.budget.start("render",item)
            self.budget.observe("render",item,pid)
            self.rss[pid]=2*1024**3
        self.free=int(3.5*1024**3)
        self.now+=2
        self.assertFalse(self.budget.can_start())
        self.assertEqual(self.budget.snapshot()["heavy_active_workers"],2)
        self.assertEqual(self.budget.summary()["memory_pause_count"],1)
        self.free=10*1024**3
        self.now+=12
        self.assertTrue(self.budget.can_start())
        self.assertEqual(self.budget.summary()["memory_pause_seconds"],12.)
        self.assertEqual(len(self.budget.tasks),2)

    def test_low_windows_memory_can_pause_despite_free_wsl_allowance(self):
        self.host=1024**3
        self.assertFalse(self.budget.can_start())
        self.host=8*1024**3
        self.now+=2
        self.assertTrue(self.budget.can_start())

    def test_unstarted_jobs_reserve_memory_before_child_pid_arrives(self):
        self.free=3*1024**3
        self.assertTrue(self.budget.can_start())
        self.budget.start("prepare",self.target(hour=0))
        self.assertTrue(self.budget.can_start())
        self.budget.start("prepare",self.target(hour=3))
        self.assertFalse(self.budget.can_start())

    def test_finished_jobs_record_peak_and_ignore_late_worker_start(self):
        item=self.target("icon")
        self.budget.start("render",item)
        self.budget.observe("render",item,10)
        self.rss[10]=2*1024**3
        usage=self.budget.finish("render",item)
        self.assertEqual(usage["worker_peak_rss_mib"],2048.)
        self.assertTrue(usage["heavy_worker"])
        self.budget.observe("render",item,10)
        self.assertFalse(self.budget.tasks)
        self.assertEqual(self.budget.snapshot()["worker_reserve_mib"],2560.)

    def test_recent_light_work_eventually_releases_old_peak_reservation(self):
        self.budget.recent_peaks.append(3*1024**3)
        for hour in range(8):
            item=self.target(hour=hour)
            self.rss[10]=256*1024**2
            self.budget.start("render",item)
            self.budget.observe("render",item,10)
            self.budget.finish("render",item)
        self.assertEqual(self.budget.snapshot()["worker_reserve_mib"],1024.)

    def test_missing_metrics_preserve_the_existing_worker_cap(self):
        self.free=None
        self.assertEqual(self.budget.snapshot()["capacity"],4)

    def test_large_preparation_does_not_inflate_a_known_light_render_profile(self):
        item=self.target("icon")
        self.budget.start("prepare",item)
        self.budget.observe("prepare",item,10)
        self.rss[10]=3*1024**3
        self.budget.finish("prepare",item)
        self.assertEqual(self.budget.estimate("prepare",item),int(3.75*1024**3))
        self.assertEqual(self.budget.estimate("render",self.target("gfs")),1024**3)
        self.host=3*1024**3
        self.assertGreaterEqual(self.budget.snapshot(force=True)["capacity"],1)
        self.assertTrue(self.budget.can_start("render",self.target("gfs")))
        self.assertFalse(self.budget.can_start("prepare",item))

    def test_dedicated_prepare_slot_still_shares_the_physical_memory_budget(self):
        self.budget.configure_pipeline(2,1)
        for hour in (0,3):
            self.budget.start("render",self.target(hour=hour))
        self.assertFalse(self.budget.can_start("render",self.target(hour=6)))
        self.assertEqual(self.budget.summary()["memory_pause_count"],0)
        self.assertTrue(self.budget.can_start("prepare",self.target(hour=6)))
        self.host=2*1024**3
        self.now+=2
        self.assertFalse(self.budget.can_start("prepare",self.target(hour=6)))


if __name__ == "__main__":
    unittest.main()
