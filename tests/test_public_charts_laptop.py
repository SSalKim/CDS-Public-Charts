from collections import deque
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from public_charts import input_cache, laptop
from public_charts.resources import WorkerMemoryBudget
from public_charts_runtime import prepare_laptop_checkpoints, prune_checkpoints


MIB = 1024**2
TARGET = {"model":"gfs","cycle":"2026111812","hour":48}


class LaptopMemoryTests(unittest.TestCase):
    def setUp(self):
        self.host = 2430*MIB
        self.samples = {}
        self.budget = WorkerMemoryBudget(4,memory_reader=lambda:8420*MIB,
                                         host_reader=lambda:self.host,laptop_profiles=self.samples)

    def learn(self, phase, mib=500):
        key = self.budget.profile_key(phase,TARGET)
        self.samples[key] = deque([mib*MIB]*3,maxlen=8)

    def test_short_light_lead_needs_both_profiles_and_does_not_unlock_other_leads(self):
        self.assertEqual(self.budget.snapshot()["capacity"],0)
        self.assertTrue(self.budget.host_pressured([TARGET]))
        self.learn("prepare")
        self.assertFalse(self.budget.trial_fits(TARGET))
        self.learn("render")
        self.assertTrue(self.budget.trial_fits(TARGET))
        self.assertFalse(self.budget.trial_fits({**TARGET,"hour":132}))
        self.assertFalse(self.budget.host_pressured([TARGET]))
        self.budget.start("prepare",TARGET)
        self.assertFalse(self.budget.can_start("render",TARGET))

    def test_hints_cannot_increase_capacity_without_real_host_headroom(self):
        advise = Mock(return_value=256*MIB)
        refresh = Mock()
        control = laptop.LaptopReclaim("unused",self.budget,clock=lambda:10,advise=advise,host_refresh=refresh)
        with patch("builtins.print"):
            control.maybe_start([],[])
            self.assertIsNone(control.thread)
            control.maybe_start([TARGET],[TARGET["cycle"]])
            control.thread.join(2)
            control.maybe_start([TARGET],[])
        advise.assert_called_once_with("unused",frozenset([TARGET["cycle"]]))
        refresh.assert_called_once()
        self.assertEqual(self.budget.snapshot(force=True)["capacity"],0)
        self.host = 2900*MIB
        self.assertEqual(self.budget.snapshot(force=True)["capacity"],1)
        self.host = None
        self.assertEqual(self.budget.snapshot(force=True)["capacity"],0)

    def test_admission_rechecks_host_even_when_previous_snapshot_had_headroom(self):
        self.host = 4096*MIB
        refresh = Mock(return_value=1800*MIB)
        self.budget.host_refresh = refresh
        self.assertGreater(self.budget.snapshot()["capacity"],0)
        self.assertFalse(self.budget.can_start("prepare",TARGET))
        refresh.assert_called_once()
        self.assertEqual(self.budget.snapshot()["windows_available_memory_mib"],1800.)


class LaptopCacheTests(unittest.TestCase):
    def test_memory_snapshot_excludes_unrelated_proc_fields(self):
        with patch.object(Path,"read_text",return_value="Cached: 2048 kB\nAnonPages: 1024 kB\nOther: 55 kB\n"):
            self.assertEqual(laptop.memory_state(),{"cached_mib":2.0,"anonpages_mib":1.0})

    def test_only_cold_unprotected_inputs_are_hinted_within_budget(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            now = 100000
            items = [("prepared/gfs/2026111712/003/fields.npz",0,7),
                     ("partial-fields/2026111712/003/item.npz",10,8),
                     ("prepared/gfs/2026111812/003/fields.npz",0,9),
                     ("prepared/icon/2026111712/003/fields.npz",now,10),
                     ("checkpoints/2026111712/gfs/item.npz",0,11)]
            for relative,stamp,size in items:
                path = root/"raw-cache"/relative
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(b"x"*size)
                os.utime(path,(stamp,stamp))
            with (patch.object(os,"posix_fadvise",create=True) as advise,
                  patch.object(os,"POSIX_FADV_DONTNEED",4,create=True),
                  patch.object(os,"O_NOFOLLOW",0,create=True),
                  patch.object(os,"O_NONBLOCK",0,create=True)):
                self.assertEqual(laptop.advise_cold_files(root,{"2026111812"},now=now,max_bytes=12),7)
            advise.assert_called_once()
            self.assertTrue(all((root/"raw-cache"/relative).is_file() for relative,_,_ in items))

    def test_checkpoint_revision_and_budget_preserve_other_local_cache(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            prepare_laptop_checkpoints(root,"a"*64)
            checkpoint = root/"raw-cache/checkpoints"
            for cycle in ("2026111712","2026111812"):
                path = checkpoint/cycle/"gfs/rain/003.npz"
                path.parent.mkdir(parents=True)
                path.write_bytes(b"12345678")
            prune_checkpoints(checkpoint,max_bytes=8)
            self.assertFalse((checkpoint/"2026111712").exists())
            prepare_laptop_checkpoints(root,"a"*64)
            self.assertTrue((checkpoint/"2026111812").exists())
            with patch.dict(os.environ,{"CHART_CACHE_WORK":str(root)}):
                self.assertTrue(input_cache.location(root/"scratch",TARGET).is_relative_to(root/"raw-cache"))
            profiles = laptop.profile_file(root)
            laptop.save_profiles(profiles,{"prepare:gfs:1:300:short":[500*MIB]*3})
            self.assertEqual(len(laptop.load_profiles(profiles)),1)
            prepare_laptop_checkpoints(root,"b"*64)
            self.assertFalse(checkpoint.exists())
            self.assertTrue(profiles.exists())

    def test_pruning_does_not_warm_payloads_and_restore_still_rejects_corruption(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cached = input_cache.location(root,TARGET)
            cached.mkdir(parents=True)
            (cached/"fields.npz").write_bytes(b"wrong bytes")
            (cached/"metadata.json").write_text(json.dumps({
                **TARGET,"cached_at":123,"prepared_sha256":"0"*64}))
            read_bytes = Path.read_bytes
            def guarded(path):
                if path.name == "fields.npz":
                    self.fail("prune read a chart payload")
                return read_bytes(path)
            with patch.object(Path,"read_bytes",guarded):
                input_cache.prune(root,now=123,verify_hash=False)
            self.assertTrue((cached/"fields.npz").exists())
            self.assertFalse(input_cache.valid(cached,TARGET,now=123))


class LaptopProcessTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(os,"getuid"),"Linux process identity validation")
    def test_cache_saver_identity_rejects_current_foreign_or_different_entrypoint(self):
        with TemporaryDirectory() as directory:
            runner = Path(directory)/"runner"
            workspace = runner/"_work/CDS-Public-Charts/CDS-Public-Charts"
            workspace.mkdir(parents=True)
            exe = runner/"externals/node24/bin/node"
            exe.parent.mkdir(parents=True)
            exe.touch()
            script = runner/"_work/_actions/actions/cache/v4/dist/save-only/index.js"
            script.parent.mkdir(parents=True)
            script.touch()
            process = Path(directory)/"proc/123"
            process.mkdir(parents=True)
            (process/"exe").symlink_to(exe)
            (process/"cwd").symlink_to(workspace)
            (process/"cmdline").write_bytes(os.fsencode(exe)+b"\0"+os.fsencode(script)+b"\0")
            metadata = b"GITHUB_RUN_ID=1\0GITHUB_RUN_ATTEMPT=1\0GITHUB_REPOSITORY=SSalKim/CDS-Public-Charts\0"
            (process/"environ").write_bytes(metadata)
            self.assertEqual(laptop.cache_saver_run(process,workspace,"SSalKim/CDS-Public-Charts","2"),("1",1))
            self.assertIsNone(laptop.cache_saver_run(process,workspace,"SSalKim/CDS-Public-Charts","1"))
            self.assertIsNone(laptop.cache_saver_run(process,workspace,"SSalKim/VTG","2"))
            (process/"cmdline").write_bytes(os.fsencode(exe)+b"\0different.js\0")
            self.assertIsNone(laptop.cache_saver_run(process,workspace,"SSalKim/CDS-Public-Charts","2"))

    def test_signals_only_verified_completed_run_and_same_pinned_pid(self):
        with TemporaryDirectory() as directory:
            workspace = Path(directory)/"runner/_work/CDS-Public-Charts/CDS-Public-Charts"
            workspace.mkdir(parents=True)
            proc = Mock()
            proc.iterdir.return_value = [Path("100"),Path("200"),Path("300")]
            env = {"RUNNER_ENVIRONMENT":"self-hosted","GITHUB_WORKSPACE":str(workspace),
                   "GITHUB_REPOSITORY":"SSalKim/CDS-Public-Charts","GH_TOKEN":"unused","GITHUB_RUN_ID":"99"}
            verify = Mock(side_effect=[True,False])
            poll = Mock()
            poll.poll.return_value = [(20,1)]
            with (patch.object(laptop,"cache_saver_run",side_effect=[
                    ("10",1),("10",1),("20",1),("20",1),("30",1),None]),
                  patch.object(os,"pidfd_open",side_effect=[20,21,22],create=True),
                  patch.object(laptop.signal,"pidfd_send_signal",create=True) as send,
                  patch.object(laptop.select,"poll",return_value=poll,create=True),
                  patch.object(laptop.select,"POLLIN",1,create=True),
                  patch.object(os,"close"),patch("builtins.print")):
                laptop.reap_cache_savers(proc=proc,env=env,verify=verify)
            self.assertEqual(verify.call_count,2)
            send.assert_called_once_with(20,laptop.signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
