from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

import numpy as np
import requests

from public_charts import icon,policy
from public_charts.parallel import LeadQueue
from public_charts.resources import BatchWindow
from public_charts.storage import ChartDrivePool
from public_charts.remote import RemoteCoordinator,identity
from public_charts_runtime import install_runtime,install_transport
from public_charts_action import action_args
from test_public_charts_storage import MemoryStore
from test_public_charts_remote import FakeArtifacts,SESSION,SHA,message


TARGET={"model":"icon","cycle":"2026111812","hour":72}


class BatchWindowTests(unittest.TestCase):
    def test_only_automatic_production_opts_in_to_adaptive_rollover(self):
        env={"CHART_WORK":"work","DRY_RUN":"false","TIME_BUDGET_INPUT":"20"}
        self.assertIn("--adaptive-rollover",action_args("coordinator",env))
        for field,value in (("MINUTES_INPUT","20"),("CYCLE_INPUT","2026111812"),("DRY_RUN","true")):
            self.assertNotIn("--adaptive-rollover",action_args("coordinator",{**env,field:value}))

    def test_only_ready_operational_work_extends_once_to_thirty_minutes(self):
        clock=[100.]
        window=BatchWindow(20,adaptive=True,clock=lambda:clock[0])
        clock[0]=1209
        self.assertEqual(window.extend(True),0)
        clock[0]=1210
        self.assertEqual(window.extend(False),0)
        self.assertEqual(window.extend(True),600)
        self.assertEqual(window.deadline,1900)
        clock[0]=1810
        self.assertEqual(window.extend(True),0)

    def test_short_manual_and_expired_windows_never_extend(self):
        for minutes,adaptive in ((1,True),(20,False),(30,True),(0,True)):
            clock=[0.]
            window=BatchWindow(minutes,adaptive=adaptive,clock=lambda:clock[0])
            clock[0]=max(0,minutes*60-1)
            self.assertEqual(window.extend(True),0)
        window=BatchWindow(20,adaptive=True,clock=lambda:1201)
        with patch.object(window,"clock",return_value=window.deadline+1):
            self.assertEqual(window.extend(True),0)


class RetryAffinityTests(unittest.TestCase):
    def test_icon_12z_repair_invalidates_only_total_precipitation_composites(self):
        for product in ("surfce","acptot","acptot1","acrain","acrain1","gph500"):
            row={"model":"icon","cycle":"2026111812","hour":24,"product":product,"domain":"hkor",
                 "render_version":4 if product=="gph500" else 10}
            self.assertEqual(policy.render_is_current(row),product not in {"acrain","acrain1"})
        row={"model":"icon","cycle":"2026111806","hour":24,"product":"acrain","domain":"hkor","render_version":8}
        self.assertTrue(policy.render_is_current(row))

    def test_existing_icon_12z_keys_remain_current_without_rerender(self):
        for product,domain in (("gph500","asia"),("gph500","hkor"),("acptot","hkor"),("acrain","hkor")):
            row={"model":"icon","cycle":"2026111812","hour":0,"product":product,"domain":domain}
            row["render_version"]=policy.render_version(row["cycle"],row["model"],product,domain)
            self.assertTrue(policy.render_is_current(row))

    def test_local_partial_input_stays_local_for_a_bounded_retry(self):
        queue=LeadQueue([TARGET],set(),100)
        with patch("public_charts.parallel.time.monotonic",return_value=100) as clock:
            queue.prefer_local_retry(TARGET)
            self.assertIsNone(queue.next(set(),remote=True))
            self.assertEqual(queue.next(set()),TARGET)
            queue.reclaim(TARGET,unstarted=True)
            clock.return_value=281
            self.assertEqual(queue.next(set(),remote=True),TARGET)

    def test_one_helper_retry_reuses_its_lease_and_then_releases_normally(self):
        with tempfile.TemporaryDirectory() as root:
            clock=[100.]
            queue=LeadQueue([TARGET],set(),100)
            transport=FakeArtifacts()
            events=[]
            broker=RemoteCoordinator(transport,SESSION,SHA,queue,root,lambda *args:None,
                                     clock=lambda:clock[0],emit=events.append)
            broker.start()
            transport.push(message("heartbeat",generated_at=100,capacity=1,active=[]))
            broker.tick()
            token,lease=next(iter(broker.leases.items()))
            error={"type":"SourceDeferred","pending":True,"reason":"source_transfer_budget"}
            for attempt in range(2):
                clock[0]+=6
                transport.push(message("bundle",lease=token,target=TARGET,artifacts=[],partial=False,error=error))
                broker.tick()
                if attempt==0:
                    self.assertTrue(broker.leases[token]["source_retry"])
                    self.assertIn(identity(TARGET),queue.issued)
                else:
                    self.assertFalse(broker.leases)
                    self.assertNotIn(identity(TARGET),queue.issued)
            self.assertEqual(sum(e["event"]=="helper_source_retry" for e in events),1)


class SharedFeedHealthTests(unittest.TestCase):
    def test_static_feed_first_and_failed_feed_is_shared_with_other_processes(self):
        with tempfile.TemporaryDirectory() as root,patch.dict(os.environ,{"PUBLIC_CHARTS_FIELD_CACHE_DIR":root}), \
                patch("public_charts.icon._FEED_FAILURES",{}),patch("public_charts.icon.time.time",return_value=100) as clock, \
                patch("public_charts.icon.read_native",return_value=(np.ones(1),{})):
            first=Mock()
            first.get.side_effect=[requests.ReadTimeout(),b"good"]
            icon.native(first,"2026111812",72,"FI",500)
            self.assertNotIn("/v1/",first.get.call_args_list[0].args[0])
            with patch("public_charts.icon._FEED_FAILURES",{}):
                second=Mock()
                second.get.return_value=b"good"
                icon.native(second,"2026111812",75,"FI",500)
                self.assertIn("/v1/",second.get.call_args.args[0])
            clock.return_value=401
            third=Mock()
            third.get.return_value=b"good"
            icon.native(third,"2026111812",78,"FI",500)
            self.assertNotIn("/v1/",third.get.call_args.args[0])


class DriveReadTests(unittest.TestCase):
    def test_both_inventories_overlap_and_preserve_deterministic_account_order(self):
        stores={name:MemoryStore(name) for name in ("primary","secondary")}
        pool=ChartDrivePool(stores)
        gate=threading.Barrier(2)
        def inventory(name):
            gate.wait(timeout=5)
            return [{"id":name+"-id"}]
        with patch.object(stores["primary"],"inventory",side_effect=lambda:inventory("primary")), \
                patch.object(stores["secondary"],"inventory",side_effect=lambda:inventory("secondary")):
            rows=pool.inventory()
        self.assertEqual([r["storage_account"] for r in rows],["primary","secondary"])
        self.assertEqual(pool.account_for_file("secondary-id"),"secondary")

    def test_inventory_failure_never_publishes_partial_ownership(self):
        stores={name:MemoryStore(name) for name in ("primary","secondary")}
        pool=ChartDrivePool(stores)
        stores["primary"].files=[{"id":"one"}]
        with patch.object(stores["secondary"],"inventory",side_effect=RuntimeError("incomplete")):
            with self.assertRaisesRegex(RuntimeError,"incomplete"):
                pool.inventory()
        self.assertFalse(pool._file_accounts)


class RuntimeReuseTests(unittest.TestCase):
    def test_python_reuse_checks_versions_and_reinstalls_an_invalid_marker(self):
        calls=[]
        def execute(args,**kwargs):
            calls.append(args)
            if args[1:3]==["-m","venv"]:
                python=Path(args[-1])/("Scripts/python.exe" if os.name=="nt" else "bin/python")
                python.parent.mkdir(parents=True)
                python.touch()
            return subprocess.CompletedProcess(args,0,stdout="",stderr="")
        with tempfile.TemporaryDirectory() as root,patch("public_charts_runtime.subprocess.run",side_effect=execute), \
                patch.dict(os.environ,{"GITHUB_PATH":str(Path(root)/"github-path")}):
            python=install_runtime(root)
            self.assertEqual(sum("pip" in c for c in calls),1)
            install_runtime(root)
            self.assertEqual(sum("pip" in c for c in calls),1)
            (python.parent.parent/".charts-ready").write_text("wrong")
            install_runtime(root)
            self.assertEqual(sum("pip" in c for c in calls),2)

    def test_node_modules_are_reused_outside_the_checkout_and_invalidated_by_node_version(self):
        calls=[]
        version=["v24.0.0"]
        def execute(args,**kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args,0,stdout=version[0] if args==["node","--version"] else "",stderr="")
        with tempfile.TemporaryDirectory() as root,patch("public_charts_runtime.subprocess.run",side_effect=execute), \
                patch.dict(os.environ,{"GITHUB_ENV":str(Path(root)/"github-env")}):
            first=install_transport(root)
            self.assertTrue(first.is_relative_to(root))
            install_transport(root)
            self.assertEqual(sum(c[0]=="npm" for c in calls),1)
            version[0]="v24.1.0"
            self.assertNotEqual(install_transport(root),first)
            self.assertEqual(sum(c[0]=="npm" for c in calls),2)
            self.assertIn("PUBLIC_CHARTS_TRANSPORT_SCRIPT=",(Path(root)/"github-env").read_text())
            self.assertEqual((first/"upload.cjs").read_bytes(),
                             (Path(__file__).resolve().parents[1]/"public_charts/transport/upload.cjs").read_bytes())


if __name__=="__main__":
    unittest.main()
