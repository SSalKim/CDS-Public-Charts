"""One GitHub publisher leases independent leads to a credential-free helper."""
from datetime import datetime
import hashlib
import io
import json
import math
from pathlib import Path
import re
import subprocess
import time
import uuid

from PIL import Image

from . import policy
from .artifacts import ArtifactError, validate_session
from .chart_style import public_image_size
from .parallel import child_path, model_workspace

SCHEMA = 1
HEARTBEAT_SECONDS = 30
LOST_HELPER_SECONDS = 90
STARTUP_SECONDS = 180
LEASE_SECONDS = 600
PROGRESS_GRACE_SECONDS = 360
MAX_LEASE_SECONDS = 1200
POLL_SECONDS = 5


def identity(target):
    return target["model"], target["cycle"], target["hour"]


def target_keys(target):
    if (not isinstance(target, dict) or set(target) != {"model", "cycle", "hour"}
            or type(target["hour"]) is not int):
        raise ValueError("Invalid remote chart target")
    return policy.expected_keys(*identity(target))


def validate_message(message, session, code_sha):
    if (not isinstance(message, dict) or message.get("schema") != SCHEMA
            or message.get("session") != session or message.get("code_sha") != code_sha):
        raise ValueError("Stale or incompatible helper message")


def image_size(product, domain, model, cycle):
    if (product in {"acptot", "acrain", "acptot1", "acrain1"} and domain == "hkor"
            and policy.winter(model,cycle)):
        return (600, 2000) if product.endswith("1") else (1750, 660)
    return public_image_size(domain)


def pack_bundle(lease, target, staged):
    allowed, artifacts, files = target_keys(target), [], {}
    for item in staged["artifacts"]:
        key = policy.key(*identity(target), item["product"], item["domain"])
        if key not in allowed:
            raise ValueError("Unexpected helper product")
        record = {name: item[name] for name in ("product", "domain", "alias_of")}
        for name in ("created_at", "observed_at", "source_last_modified_at"):
            if item.get(name):
                record[name] = item[name]
        if item["alias_of"]:
            if policy.key(*identity(target), *item["alias_of"]) not in allowed:
                raise ValueError("Unexpected helper alias")
        else:
            path = Path(item["path"])
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            filename = digest + ".webp"
            record.update(filename=filename, sha256=digest, size=len(data))
            files[filename] = path
        artifacts.append(record)
    message = {"kind": "bundle", "lease": lease, "target": target, "artifacts": artifacts,
               "partial": bool(staged.get("partial")), "error": staged.get("error"),
               "result": {k: v for k, v in staged.get("result", {}).items()
                          if k in {"complete", "pending", "pending_reason", "plot_seconds", "encode_seconds"}},
               "render_seconds": staged.get("render_seconds", 0), "prefetch_seconds": staged.get("prefetch_seconds", 0)}
    return message, files


def import_bundle(message, files, lease, work):
    target = lease["target"]
    allowed = target_keys(target)
    if message.get("target") != target or message.get("lease") != lease["id"]:
        raise ValueError("Helper result does not match its lease")
    records = message.get("artifacts")
    if not isinstance(records, list) or len(records) > len(allowed):
        raise ValueError("Invalid helper artifact list")
    seen, used, artifacts = set(), set(), []
    directory = child_path(model_workspace(work,target["model"]), "staged", target["cycle"], f"{target['hour']:03}")
    for item in records:
        key = policy.key(*identity(target), item["product"], item["domain"])
        if key not in allowed or key in seen:
            raise ValueError("Unexpected or duplicate helper output")
        seen.add(key)
        record = {"product": item["product"], "domain": item["domain"], "alias_of": item.get("alias_of"), "path": None}
        for name in ("created_at", "observed_at", "source_last_modified_at"):
            if item.get(name):
                if datetime.fromisoformat(item[name]).tzinfo is None:
                    raise ValueError("Helper timing requires a timezone")
                record[name] = item[name]
        if record["alias_of"]:
            if policy.key(*identity(target), *record["alias_of"]) not in allowed:
                raise ValueError("Unexpected helper alias")
        else:
            filename, digest = item.get("filename"), item.get("sha256")
            if not isinstance(digest,str) or not re.fullmatch(r"[a-f0-9]{64}",digest) or filename != digest+".webp":
                raise ValueError("Invalid helper content hash")
            data = files.get(filename)
            if (data is None or len(data) != item.get("size") or hashlib.sha256(data).hexdigest() != digest
                    or data[:4] != b"RIFF" or data[8:12] != b"WEBP"):
                raise ValueError("Helper image integrity failure")
            with Image.open(io.BytesIO(data)) as image:
                expected = image_size(item["product"],item["domain"],target["model"],target["cycle"])
                if image.size != expected or image.format != "WEBP":
                    raise ValueError(f"Unexpected helper image dimensions: {image.size} != {expected}")
                image.verify()
            directory.mkdir(parents=True,exist_ok=True)
            path = child_path(directory,hashlib.sha256(key.encode()).hexdigest()+".webp")
            path.write_bytes(data)
            record["path"] = str(path)
            used.add(filename)
        artifacts.append(record)
    if set(files) != used:
        raise ValueError("Undeclared files in helper bundle")
    error = message.get("error")
    if error is not None and (not isinstance(error,dict) or not isinstance(error.get("type"),str)
                              or type(error.get("pending")) is not bool):
        raise ValueError("Invalid helper error record")
    return {"artifacts": artifacts, "result": message.get("result",{}), "error": error,
            "partial": bool(message.get("partial")), "pid": None,
            "render_seconds": message.get("render_seconds",0), "prefetch_seconds": message.get("prefetch_seconds",0)}


class RemoteCoordinator:
    def __init__(self, transport, session, code_sha, queue, work, consume, *, minutes=20, clock=time.time,
                 emit=None, can_assign=None, smoke=False, smoke_runner="github-hosted"):
        self.transport, self.session, self.code_sha = transport, validate_session(session), code_sha
        self.queue, self.work, self.consume = queue, work, consume
        self.clock, self.emit = clock, emit or (lambda item: print(json.dumps(item),flush=True))
        self.can_assign = can_assign or (lambda: True)
        self.started, self.deadline = clock(), clock()+minutes*60
        self.leases, self.seen = {}, set()
        self.progress_sequences = {}
        self.helper_id, self.last_heartbeat = None, None
        self.capacity, self.heavy_capacity, self.reported_active = 0, 0, set()
        self.next_poll, self.last_control, self.sequence, self.errors = 0, 0, 0, 0
        self.disabled, self.polling, self.smoke = False, False, smoke
        self.smoke_runner = smoke_runner

    def publish_control(self, stop=False):
        now = self.clock()
        message = {"schema":SCHEMA,"session":self.session,"code_sha":self.code_sha,"kind":"control",
                   "generated_at":now,"valid_until":now+120,"deadline":self.deadline+480,
                   "stop":stop,"leases":list(self.leases.values())}
        self.transport.upload(f"chart-control-{self.session}-{self.sequence:06}",message)
        self.sequence += 1
        self.last_control = now

    def start(self):
        try:
            self.publish_control()
            self.transport.dispatch_helper(self.session,self.code_sha,self.smoke,self.smoke_runner)
            self.emit({"event":"helper_requested","session":self.session})
        except (ArtifactError,ValueError,OSError,subprocess.SubprocessError) as error:
            self.disable(type(error).__name__)

    def disable(self, reason):
        if self.disabled:
            return
        self.disabled = True
        for lease in self.leases.values():
            self.queue.reclaim(lease["target"])
        self.leases.clear()
        self.emit({"event":"helper_disabled","reason":reason,"session":self.session})
        try:
            self.publish_control(stop=True)
            if self.helper_id:
                self.transport.cancel(self.helper_id)
        except (ArtifactError,OSError,ValueError,subprocess.SubprocessError):
            pass

    def tick(self):
        now = self.clock()
        if self.disabled or self.polling or now < self.next_poll:
            return
        self.next_poll, self.polling = now+POLL_SECONDS, True
        changed = False
        try:
            run = self.transport.helper_run(self.session) if self.helper_id is None else None
            if run:
                self.helper_id = int(run["id"])
            if self.helper_id:
                entries = self.transport.list(self.helper_id,f"chart-result-{self.session}-")
                for item in sorted(entries,key=lambda item:item["name"]):
                    if item["id"] in self.seen:
                        continue
                    message, files = self.transport.read(item)
                    validate_message(message,self.session,self.code_sha)
                    if message.get("kind") == "heartbeat":
                        if files or type(message.get("capacity")) is not int or not 0 <= message["capacity"] <= 4:
                            raise ValueError("Invalid helper capacity")
                        at = float(message.get("generated_at",0))
                        active, heavy_capacity = message.get("active",[]), message.get("heavy_capacity",message["capacity"])
                        if (not math.isfinite(at) or at > now+30 or at < self.started-30
                                or type(heavy_capacity) is not int or not 0 <= heavy_capacity <= message["capacity"]
                                or not isinstance(active,list) or len(active) > 4
                                or any(not isinstance(token,str) or not re.fullmatch(r"[a-f0-9]{32}",token) for token in active)):
                            raise ValueError("Invalid helper heartbeat time")
                        self.last_heartbeat = max(self.last_heartbeat or 0,min(now,at))
                        self.capacity = message["capacity"]
                        self.heavy_capacity = heavy_capacity
                        self.reported_active = set(active)
                        progress = message.get("progress",{})
                        if not isinstance(progress,dict) or not set(progress) <= set(active):
                            raise ValueError("Invalid helper progress")
                        for token, item in progress.items():
                            if (not isinstance(item,dict) or type(item.get("sequence")) is not int
                                    or item["sequence"] < 1 or type(item.get("at")) not in (int,float)
                                    or not math.isfinite(item["at"]) or not self.started-30 <= item["at"] <= now+30):
                                raise ValueError("Invalid helper progress time")
                            lease = self.leases.get(token)
                            if lease is not None and item["sequence"] > self.progress_sequences.get(token,0):
                                self.progress_sequences[token] = item["sequence"]
                                if now <= lease["expires_at"] and now-item["at"] <= LOST_HELPER_SECONDS:
                                    lease["expires_at"] = min(max(lease["expires_at"],item["at"]+PROGRESS_GRACE_SECONDS),
                                                              lease["hard_expires_at"])
                                    changed = True
                        if message.get("stopped"):
                            self.capacity = 0
                            for lease in self.leases.values():
                                self.queue.reclaim(lease["target"])
                            self.leases.clear()
                            self.disabled = True
                            self.emit({"event":"helper_stopped","session":self.session})
                    elif message.get("kind") == "bundle":
                        lease = self.leases.get(message.get("lease"))
                        if lease is not None:
                            staged = import_bundle(message,files,lease,self.work)
                            if staged["artifacts"] and now <= lease["expires_at"]:
                                lease["expires_at"] = min(max(lease["expires_at"],now+PROGRESS_GRACE_SECONDS),lease["hard_expires_at"])
                                changed = True
                            self.queue.rendered(lease["target"],staged)
                            if (not staged["partial"] and staged["error"] is None
                                    and not target_keys(lease["target"]) <= set(self.queue.known(lease["target"]))):
                                staged["error"] = {"type":"NotReady","pending":True,"phase":"surface",
                                                   "reason":"helper_incomplete_output"}
                            self.consume(lease["target"],staged)
                            self.emit({"event":"helper_bundle_received",**lease["target"],"lease":lease["id"],
                                       "images":len(staged["artifacts"]),"partial":staged["partial"]})
                            if not staged["partial"]:
                                self.queue.finished(lease["target"],staged["error"])
                                self.leases.pop(lease["id"])
                    else:
                        raise ValueError("Invalid helper message kind")
                    self.seen.add(item["id"])
                    try:
                        self.transport.delete(item["id"])
                    except ArtifactError:
                        pass
            if self.disabled:
                return
            if self.last_heartbeat is None and now-self.started > STARTUP_SECONDS:
                self.disable("helper_start_timeout")
                return
            if self.last_heartbeat is not None and now-self.last_heartbeat > LOST_HELPER_SECONDS:
                self.disable("helper_heartbeat_lost")
                return
            for token, lease in list(self.leases.items()):
                if now > lease["expires_at"]:
                    self.queue.reclaim(lease["target"])
                    self.leases.pop(token)
                    self.emit({"event":"helper_lease_expired",**lease["target"]})
                    changed = True
            orphaned = len(self.reported_active-set(self.leases))
            while (self.last_heartbeat is not None and self.can_assign() and now < self.deadline
                   and len(self.leases)+orphaned < self.capacity):
                active = {identity(lease["target"]) for lease in self.leases.values()}
                target = self.queue.next(active)
                if target is None:
                    break
                token = uuid.uuid4().hex
                self.leases[token] = {"id":token,"target":target,"existing":self.queue.known(target),
                                      "expires_at":min(now+LEASE_SECONDS,self.deadline+480),
                                      "hard_expires_at":min(now+MAX_LEASE_SECONDS,self.deadline+480)}
                self.emit({"event":"helper_lead_assigned",**target,"lease":token})
                changed = True
            if changed or now-self.last_control >= HEARTBEAT_SECONDS:
                self.publish_control()
            self.errors = 0
        except (ArtifactError,ValueError,KeyError,TypeError,OSError,subprocess.SubprocessError) as error:
            self.errors += 1
            self.emit({"event":"helper_channel_retry","error":type(error).__name__,"attempt":self.errors,
                       **(error.details if isinstance(error,ArtifactError) else {})})
            if isinstance(error,ValueError) and str(error).startswith("Unexpected helper image dimensions:"):
                self.emit({"event":"helper_image_rejected","reason":str(error)})
            if self.errors >= 3:
                self.disable("helper_channel_failed")
        finally:
            self.polling = False

    def keep_alive(self):
        return not self.disabled and (bool(self.leases) or
                self.last_heartbeat is None and self.clock()-self.started < STARTUP_SECONDS)

    def close(self):
        self.disabled = True
        try:
            self.publish_control(stop=True)
        except (ArtifactError,OSError,ValueError,subprocess.SubprocessError):
            pass
