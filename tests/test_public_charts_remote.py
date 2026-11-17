import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from PIL import Image

from public_charts import policy
from public_charts.artifacts import ArtifactError, GitHubArtifacts, unpack
from public_charts.parallel import LeadQueue
from public_charts.remote import (RemoteCoordinator, SCHEMA, identity, import_bundle, pack_bundle)
from public_charts_action import action_args

SESSION, SHA = "12345-1", "a"*40


def target(model="gfs",hour=0):
    return {"model":model,"cycle":"2026111612","hour":hour}


def message(kind, **fields):
    return {"schema":SCHEMA,"session":SESSION,"code_sha":SHA,"kind":kind,**fields}


class FakeArtifacts:
    def __init__(self):
        self.created, self.entries, self.payloads, self.controls = 0, [], {}, []
        self.cancelled, self.dispatched = [], []

    def upload(self,name,payload,files=None):
        self.created += 1
        self.controls.append(copy.deepcopy(payload))
        return self.created

    def dispatch_helper(self,*args):
        self.dispatched.append(args)

    def helper_run(self,session):
        return {"id":42}

    def list(self,run_id,prefix):
        return list(self.entries)

    def read(self,item):
        return copy.deepcopy(self.payloads[item["id"]])

    def delete(self,artifact_id):
        self.entries = [item for item in self.entries if item["id"] != artifact_id]

    def cancel(self,run_id):
        self.cancelled.append(run_id)

    def push(self,payload,files=None):
        item = {"id":len(self.payloads)+100,"name":f"chart-result-{SESSION}-{len(self.payloads):06}"}
        self.entries.append(item)
        self.payloads[item["id"]] = (payload,files or {})


class RemoteQueueTests(unittest.TestCase):
    def test_two_render_pools_claim_eight_distinct_leads_across_refresh(self):
        targets = [target(hour=h) for h in range(0,24,3)]
        queue = LeadQueue(targets,set(),100)
        queue.model_parallel_limit = 4
        local, remote = set(), set()
        for _ in range(4):
            local.add(identity(queue.next(local)))
        queue.refresh(targets)
        for _ in range(4):
            remote.add(identity(queue.next(remote)))
        self.assertFalse(local & remote)
        self.assertEqual(len(local | remote),8)

    def test_readiness_probe_is_shared_across_both_pools(self):
        queue = LeadQueue([target(),target(hour=3)],set(),100)
        queue.readiness_gate = True
        queue.model_parallel_limit = 4
        self.assertEqual(queue.next(set(),preparing=True),target())
        self.assertIsNone(queue.next(set(),preparing=True))
        queue.ready_cycles.add(("gfs","2026111612"))
        self.assertEqual(queue.next(set(),preparing=True),target(hour=3))

    def test_empty_helper_queue_accepts_a_new_model_after_refresh(self):
        queue = LeadQueue([],set(),100)
        queue.refresh([target()])
        self.assertEqual(queue.next(set()),target())


class RemoteCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = 100.
        self.transport, self.received, self.events = FakeArtifacts(), [], []
        self.queue = LeadQueue([target(),target("aifs")],set(),100)
        self.queue.model_parallel_limit = 4
        self.broker = RemoteCoordinator(self.transport,SESSION,SHA,self.queue,self.temp.name,
                                       lambda target,staged:self.received.append((target,staged)),
                                       clock=lambda:self.clock,emit=self.events.append)
        self.broker.start()

    def heartbeat(self,**extra):
        self.transport.push(message("heartbeat",generated_at=self.clock,capacity=2,cmc_capacity=1,active=[],**extra))
        self.broker.tick()

    def test_offline_helper_leases_are_reclaimed_without_publishing(self):
        self.heartbeat()
        self.assertEqual(len(self.broker.leases),2)
        self.clock += 91
        self.broker.tick()
        self.assertTrue(self.broker.disabled)
        self.assertFalse(self.broker.leases)
        self.assertFalse(self.received)
        self.assertEqual({identity(self.queue.next(set())),identity(self.queue.next(set()))},
                         {identity(target()),identity(target("aifs"))})
        self.assertEqual(self.transport.cancelled,[42])

    def test_never_started_helper_does_not_reserve_local_work(self):
        self.clock += 181
        self.broker.tick()
        self.assertTrue(self.broker.disabled)
        self.assertFalse(self.queue.issued)
        self.assertIsNotNone(self.queue.next(set()))

    def test_stale_session_is_rejected_without_consuming_output(self):
        self.transport.push({**message("heartbeat",capacity=2),"session":"999-1"})
        for _ in range(3):
            self.broker.tick()
            self.clock += 6
        self.assertTrue(self.broker.disabled)
        self.assertFalse(self.received)

    def test_stopped_helper_returns_unfinished_claims(self):
        self.heartbeat()
        self.clock += 6
        self.heartbeat(stopped=True)
        self.assertTrue(self.broker.disabled)
        self.assertFalse(self.queue.issued)

    def test_no_assignments_or_callbacks_after_coordinator_close(self):
        self.heartbeat()
        self.broker.close()
        self.assertTrue(self.transport.controls[-1]["stop"])
        self.clock += 6
        self.broker.tick()
        self.assertFalse(self.received)

    def test_partial_results_publish_before_final_receipt_releases_claim(self):
        self.heartbeat()
        token, lease = next(iter(self.broker.leases.items()))
        path = Path(self.temp.name)/"sample.webp"
        Image.new("RGB",(910,660),"white").save(path,"WEBP",lossless=True)
        staged = {"artifacts":[{"product":"gph500","domain":"asia","alias_of":None,"path":str(path)}],
                  "partial":True,"error":None,"result":{}}
        bundle, paths = pack_bundle(token,lease["target"],staged)
        self.transport.push(message("bundle",**{k:v for k,v in bundle.items() if k != "kind"}),
                            {name:Path(file).read_bytes() for name,file in paths.items()})
        self.clock += 6
        self.broker.tick()
        self.assertEqual(len(self.received),1)
        self.assertTrue(self.received[0][1]["partial"])
        self.assertIn(token,self.broker.leases)
        self.queue.existing.update(policy.expected_keys(*identity(lease["target"])))
        self.transport.push(message("bundle",lease=token,target=lease["target"],artifacts=[],partial=False,
                                    error=None,result={"complete":True}))
        self.clock += 6
        self.broker.tick()
        self.assertEqual(len(self.received),2)
        self.assertNotIn(token,self.broker.leases)
        self.assertNotIn(identity(lease["target"]),self.queue.issued)

    def test_expired_lease_is_reclaimed_and_late_output_cannot_publish(self):
        self.heartbeat()
        token, lease = next(iter(self.broker.leases.items()))
        self.clock += 601
        self.transport.push(message("heartbeat",generated_at=self.clock,capacity=2,cmc_capacity=1,
                                    active=list(self.broker.leases)))
        self.broker.tick()
        self.assertNotIn(token,self.broker.leases)
        self.assertFalse(self.broker.disabled)
        self.transport.push(message("bundle",lease=token,target=lease["target"],artifacts=[],partial=False,
                                    error=None,result={"complete":True}))
        self.clock += 6
        self.broker.tick()
        self.assertFalse(self.received)
        self.assertIsNotNone(self.queue.next(set()))

    def test_nonfinite_heartbeat_is_rejected(self):
        self.transport.push(message("heartbeat",generated_at=float("nan"),capacity=2,cmc_capacity=1,active=[]))
        self.broker.tick()
        self.assertIsNone(self.broker.last_heartbeat)
        self.assertFalse(self.queue.issued)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = self.root/"sample.webp"
        Image.new("RGB",(910,660),"white").save(path,"WEBP",lossless=True)
        self.staged = {"artifacts":[{"product":"gph500","domain":"asia","alias_of":None,"path":str(path)}],
                       "result":{"complete":True},"error":None}
        self.lease = {"id":"b"*32,"target":target()}
        self.bundle, paths = pack_bundle(self.lease["id"],target(),self.staged)
        self.files = {name:Path(path).read_bytes() for name,path in paths.items()}

    def test_verified_bytes_are_staged_in_the_coordinators_existing_upload_path(self):
        result = import_bundle(self.bundle,self.files,self.lease,self.root)
        self.assertEqual(Path(result["artifacts"][0]["path"]).read_bytes(),next(iter(self.files.values())))
        self.assertIn("/staged/",Path(result["artifacts"][0]["path"]).as_posix())

    def test_corrupt_hash_wrong_task_or_extra_file_is_rejected(self):
        broken = copy.deepcopy(self.bundle)
        broken["artifacts"][0]["sha256"] = "0"*64
        for payload, files, lease in ((broken,self.files,self.lease),
                                     (self.bundle,self.files,{**self.lease,"target":target(hour=3)}),
                                     (self.bundle,{**self.files,"extra.webp":b"bad"},self.lease)):
            with self.subTest(payload=payload["target"]),self.assertRaises(ValueError):
                import_bundle(payload,files,lease,self.root)

    def test_cross_forecast_alias_is_rejected(self):
        payload = copy.deepcopy(self.bundle)
        payload["artifacts"][0]["alias_of"] = ["gph500","elsewhere"]
        with self.assertRaises(ValueError):
            import_bundle(payload,{},self.lease,self.root)

    def test_final_korea_dimensions_are_accepted_but_untrimmed_canvas_is_rejected(self):
        for size in ((634,725),(640,730)):
            with self.subTest(size=size):
                path = self.root/"korea.webp"
                Image.new("RGB",size,"white").save(path,"WEBP",lossless=True)
                staged = {**self.staged,"artifacts":[{"product":"gph500","domain":"hkor","alias_of":None,"path":str(path)}]}
                bundle, paths = pack_bundle(self.lease["id"],target(),staged)
                files = {name:Path(file).read_bytes() for name,file in paths.items()}
                if size == (634,725):
                    result = import_bundle(bundle,files,self.lease,self.root)
                    with Image.open(result["artifacts"][0]["path"]) as image:
                        self.assertEqual(image.size,size)
                else:
                    with self.assertRaisesRegex(ValueError,"Unexpected helper image dimensions"):
                        import_bundle(bundle,files,self.lease,self.root)


class ArtifactTests(unittest.TestCase):
    @staticmethod
    def archive(names):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream,"w") as archive:
            for name,content in names:
                archive.writestr(name,content)
        return stream.getvalue()

    def test_archive_paths_and_undeclared_structure_are_rejected(self):
        for filename in ("../leak.webp","/absolute.webp","nested/payload.webp",".env"):
            payload = self.archive([("manifest.json","{}"),(filename,b"x")])
            with self.subTest(filename=filename),self.assertRaises(ArtifactError):
                unpack(payload)

    def test_github_token_is_not_forwarded_to_signed_download(self):
        payload = self.archive([("manifest.json",'{"kind":"heartbeat"}')])
        transport = GitHubArtifacts("owner/repo","private-token")
        location = "https://results.blob.core.windows.net/path?signature=private"
        response = Mock(headers={"Location":location})
        download = Mock(status_code=200)
        download.iter_content.return_value = [payload]
        context = Mock()
        context.__enter__ = Mock(return_value=download)
        context.__exit__ = Mock(return_value=False)
        with patch.object(transport,"api",return_value=response),patch("public_charts.artifacts.requests.get",return_value=context) as get:
            result, files = transport.read({"id":42,"digest":"sha256:"+hashlib.sha256(payload).hexdigest()})
        self.assertEqual(result,{"kind":"heartbeat"})
        self.assertEqual(files,{})
        self.assertNotIn("headers",get.call_args.kwargs)

    def test_wrong_download_host_is_rejected_before_network_call(self):
        transport = GitHubArtifacts("owner/repo","private-token")
        with patch.object(transport,"api",return_value=Mock(headers={"Location":"https://127.0.0.1/private"})), \
                patch("public_charts.artifacts.requests.get") as get,self.assertRaises(ArtifactError):
            transport.read({"id":42})
        get.assert_not_called()

    def test_laptop_transport_probe_selects_the_real_self_hosted_runner(self):
        transport = GitHubArtifacts("owner/repo","private-token")
        with patch.object(transport,"api") as api:
            transport.dispatch_helper(SESSION,SHA,True,"laptop")
        self.assertEqual(api.call_args.kwargs["json"]["inputs"]["smoke_runner"],"laptop")
        self.assertEqual(api.call_args.kwargs["json"]["inputs"]["smoke"],"true")

    def test_malformed_api_artifact_entries_fail_closed(self):
        transport = GitHubArtifacts("owner/repo","private-token")
        for data in ({"artifacts":[None]}, {"artifacts":[{"name":"chart-control-test"}]}, []):
            with self.subTest(data=data),patch.object(transport,"api",return_value=Mock(json=lambda:data)), \
                    self.assertRaises(ArtifactError):
                transport.list(123,"chart-control-")

    def test_archive_decoder_failure_is_a_recoverable_transport_error(self):
        payload = self.archive([("manifest.json","{}")])
        with patch("public_charts.artifacts.zipfile.ZipFile.read",side_effect=RuntimeError("bad archive")), \
                self.assertRaises(ArtifactError):
            unpack(payload)


class ActionRoleTests(unittest.TestCase):
    def test_korea_and_both_winter_composites_pass_the_transport_contract(self):
        from public_charts_helper import smoke_renderer
        from public_charts.remote import image_size
        probes = (("gph500","asia"),("gph500","hkor"),("acptot","hkor"),("acptot1","hkor"))
        existing = policy.expected_keys(*identity(target())) - {policy.key(*identity(target()),*item) for item in probes}
        with tempfile.TemporaryDirectory() as directory:
            outputs=[]
            smoke_renderer(*identity(target()),directory,existing,lambda *args:outputs.append(args))
            self.assertEqual(len(outputs),4)
            expected = {(product,domain):image_size(product,domain,"gfs",target()["cycle"]) for product,domain in probes}
            self.assertEqual(expected["gph500","hkor"],(634,725))
            self.assertEqual(expected["acptot","hkor"],(1750,660))
            self.assertEqual(expected["acptot1","hkor"],(600,2000))
            for product,domain,path in outputs:
                with Image.open(path) as image:
                    self.assertEqual(image.size,expected[product,domain])
    def test_stopped_helper_reports_zero_cmc_capacity(self):
        from public_charts_helper import ComputeHelper
        transport = FakeArtifacts()
        helper = ComputeHelper(transport,SESSION,SHA,"unused",4)
        helper.heartbeat(stopped=True)
        self.assertEqual(transport.controls[-1]["capacity"],0)
        self.assertEqual(transport.controls[-1]["cmc_capacity"],0)

    def test_transport_probe_passes_the_real_lossless_encoder(self):
        from public_charts_helper import smoke_renderer
        with tempfile.TemporaryDirectory() as directory:
            existing = policy.expected_keys(*identity(target())) - {policy.key(*identity(target()),"gph500","asia")}
            outputs = []
            smoke_renderer(*identity(target()),directory,existing,lambda *args:outputs.append(args))
            self.assertEqual(len(outputs),1)
            with Image.open(outputs[0][2]) as image:
                self.assertEqual(image.size,(910,660))
                self.assertEqual(image.getpixel((0,0)),(0,80,180))

    def test_only_non_forced_continuous_production_adds_a_helper(self):
        env = {"CHART_WORK":"/tmp/charts","DRY_RUN":"false","HELPER_AVAILABLE":"true"}
        self.assertIn("--remote-helper",action_args("coordinator",env))
        for changed in ({"DRY_RUN":"true"},{"FORCE_INPUT":"true"},{"CYCLE_INPUT":"2026111612"},
                        {"HELPER_AVAILABLE":"false"}):
            self.assertNotIn("--remote-helper",action_args("coordinator",{**env,**changed}))

    def test_helper_has_no_drive_or_publication_arguments(self):
        args = action_args("helper",{"CHART_WORK":"/tmp/helper","HELPER_SESSION":SESSION,"HELPER_CODE_SHA":SHA})
        self.assertEqual(args[0],"public_charts_helper.py")
        self.assertNotIn("--publish-incremental",args)
        workflow = (Path(__file__).resolve().parents[1]/".github/workflows/public_charts_helper.yml").read_text()
        self.assertNotIn("secrets.",workflow)
        self.assertNotIn("public_charts_run.py",workflow)
        self.assertIn("sparse-checkout-cone-mode: false",workflow)
        for name in ("public_charts_action.py","public_charts_helper.py","public_charts_runtime.py"):
            self.assertIn("/"+name,workflow)
            self.assertIn("test -f "+name,workflow)


if __name__ == "__main__":
    unittest.main()
