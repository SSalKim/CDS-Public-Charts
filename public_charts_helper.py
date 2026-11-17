"""Optional compute-only runner. This process never authenticates to Drive."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import time

from public_charts.artifacts import ArtifactError, GitHubArtifacts, validate_session
from public_charts.parallel import LeadQueue, execute_leads
from public_charts.remote import (HEARTBEAT_SECONDS, POLL_SECONDS, SCHEMA, identity,
                                  image_size, pack_bundle, target_keys, validate_message)
from public_charts.resources import LaptopLease, ResourceSampler, available_memory, cmc_worker_count, windows_available_memory, worker_count


def smoke_renderer(model, cycle, hour, root, existing, publish, **kwargs):
    """Transport verification only; never used for real forecast publication."""
    from PIL import Image
    from public_charts import policy
    from public_charts.producer import encode
    for key in sorted(policy.expected_keys(model,cycle,hour)-existing):
        product, domain = key.split("/")[-2:]
        path = Path(root) / "images" / (product+"-"+domain+".webp")
        image = Image.new("RGB",image_size(product,domain,model,cycle),"white")
        image.paste((0,80,180),(0,0,128,128))
        encode(image,path)
        publish(product,domain,path)
    return {"complete":True}


class HelperQueue(LeadQueue):
    def __init__(self, runtime):
        super().__init__([],set(),10000)
        self.runtime = runtime

    def next(self, active, **kwargs):
        target = super().next(active,**kwargs)
        if target is not None:
            lease = next((lease for lease in self.runtime.control.get("leases",[])
                          if lease["target"] == target),None)
            if lease is None or not self.runtime.valid_lease(lease):
                self.reclaim(target)
                return None
            self.runtime.tokens[identity(target)] = lease["id"]
        return target


class ComputeHelper:
    def __init__(self, transport, session, code_sha, work, workers, *, smoke=False):
        self.transport, self.session, self.code_sha = transport, validate_session(session), code_sha
        self.work, self.workers, self.smoke = Path(work), workers, smoke
        self.parent_id = int(session.split("-")[0])
        self.control, self.tokens, self.waiting = {}, {}, {}
        self.queue = HelperQueue(self)
        self.sequence, self.next_poll, self.last_heartbeat = 0, 0, 0
        self.started, self.latest_control, self.low_memory = time.time(), "", False
        self.transport_failures = 0

    def valid_lease(self, lease):
        return (not self.control.get("stop") and time.time() < self.control.get("valid_until",0)
                and time.time() < lease["expires_at"] and time.time() < self.control.get("deadline",0))

    def can_schedule(self):
        if self.transport.created >= 350 or self.low_memory or time.time()-self.started >= 28*60:
            return False
        free, host = available_memory(), windows_available_memory()
        if ((free is not None and free < 512*1024**2) or (host is not None and host < 1024**3)):
            self.low_memory = True
            return False
        if not self.control:
            return time.time()-self.started < 180
        return not self.control.get("stop") and time.time() < self.control["valid_until"] and time.time() < self.control["deadline"]

    def upload(self, message, files=None):
        payload = {"schema":SCHEMA,"session":self.session,"code_sha":self.code_sha,**message}
        name = f"chart-result-{self.session}-{self.sequence:06}"
        self.transport.upload(name,payload,files)
        self.sequence += 1

    def heartbeat(self, stopped=False):
        capacity = 0 if stopped or self.low_memory else self.workers
        self.upload({"kind":"heartbeat","generated_at":time.time(),"capacity":capacity,
                     "cmc_capacity":min(capacity,cmc_worker_count(self.workers)),
                     "active":list(self.tokens.values()),"stopped":stopped})
        self.last_heartbeat = time.time()

    def targets(self):
        return [lease["target"] for lease in self.control.get("leases",[]) if self.valid_lease(lease)]

    def poll_control(self):
        entries = self.transport.list(self.parent_id,f"chart-control-{self.session}-")
        if not entries:
            return
        latest = max(entries,key=lambda item:item["name"])
        if latest["name"] == self.latest_control:
            return
        message, files = self.transport.read(latest)
        validate_message(message,self.session,self.code_sha)
        if files or message.get("kind") != "control" or not isinstance(message.get("leases"),list) or len(message["leases"]) > 4:
            raise ValueError("Invalid coordinator control")
        times = [message.get(key) for key in ("generated_at","valid_until","deadline")]
        if (type(message.get("stop")) is not bool
                or any(type(value) not in (int,float) or not math.isfinite(value) for value in times)
                or times[0] > time.time()+30 or not times[0] <= times[1] <= times[0]+120):
            raise ValueError("Invalid coordinator control deadline")
        tokens, targets = set(), set()
        for lease in message["leases"]:
            allowed = target_keys(lease["target"])
            if (not isinstance(lease.get("id"),str) or not re.fullmatch(r"[a-f0-9]{32}",lease["id"])
                    or lease["id"] in tokens or identity(lease["target"]) in targets
                    or not isinstance(lease.get("existing"),list) or not set(lease["existing"]) <= allowed
                    or type(lease.get("expires_at")) not in (int,float) or not math.isfinite(lease["expires_at"])
                    or lease["expires_at"] > message["deadline"]):
                raise ValueError("Invalid coordinator lease")
            tokens.add(lease["id"])
            targets.add(identity(lease["target"]))
        self.control, self.latest_control = message, latest["name"]
        with self.queue.lock:
            for lease in message["leases"]:
                self.queue.existing.update(lease["existing"])

    def flush(self, key):
        target, staged, at = self.waiting[key]
        token = self.tokens.get(key)
        lease = next((lease for lease in self.control.get("leases",[]) if lease["id"] == token),None)
        if lease is not None and self.valid_lease(lease):
            message, files = pack_bundle(token,target,staged)
            self.upload(message,files)
        self.waiting.pop(key)
        if not staged.get("partial"):
            self.tokens.pop(key,None)

    def consume(self, target, staged):
        key = identity(target)
        if key in self.waiting:
            previous = self.waiting[key][1]
            staged = {**staged,"artifacts":previous["artifacts"]+staged["artifacts"]}
        self.waiting[key] = (target,staged,time.time())
        if len(staged["artifacts"]) >= 4 or not staged.get("partial"):
            self.flush(key)

    def tick(self):
        now = time.time()
        if now < self.next_poll:
            return
        self.next_poll = now+POLL_SECONDS
        try:
            self.poll_control()
            if not self.control:
                return
            for key, (_,_,at) in list(self.waiting.items()):
                if now-at >= 10:
                    self.flush(key)
            if now-self.last_heartbeat >= HEARTBEAT_SECONDS:
                self.heartbeat()
            self.transport_failures = 0
        except (ArtifactError,ValueError,KeyError,TypeError,OSError) as error:
            self.transport_failures += 1
            print(json.dumps({"event":"helper_transport_retry","error":type(error).__name__,"attempt":self.transport_failures}),flush=True)
            if self.transport_failures >= 3:
                self.control = {"stop":True}

    def run(self):
        lease, sampler = LaptopLease(), ResourceSampler()
        if not lease.acquire():
            return 0
        sampler.start()
        try:
            self.tick()
            print(json.dumps({"event":"helper_runtime","workers":self.workers,"session":self.session,"smoke":self.smoke}),flush=True)
            execute_leads(self.queue,self.work,self.workers,self.consume,streaming=True,
                          prefetch=not self.smoke, renderer=smoke_renderer if self.smoke else None,
                          refresh=self.targets,refresh_seconds=5,idle_seconds=180,
                          can_schedule=self.can_schedule,on_idle=self.tick,
                          progress=lambda event:print(json.dumps(event),flush=True))
            for key in list(self.waiting):
                self.flush(key)
            if self.control:
                self.heartbeat(stopped=True)
        finally:
            lease.close()
            print(json.dumps({"event":"helper_resources",**sampler.finish()}),flush=True)
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session",required=True)
    parser.add_argument("--code-sha",required=True)
    parser.add_argument("--workdir",type=Path,required=True)
    parser.add_argument("--smoke",action="store_true")
    args = parser.parse_args()
    if any(name.startswith("PUBLIC_CHARTS_DRIVE") for name in os.environ):
        raise RuntimeError("Compute helper must not receive Drive credentials")
    workers = 2 if args.smoke else worker_count()
    return ComputeHelper(GitHubArtifacts(),args.session,args.code_sha,args.workdir,workers,smoke=args.smoke).run()


if __name__ == "__main__":
    raise SystemExit(main())
