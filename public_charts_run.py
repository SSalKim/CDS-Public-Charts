"""Bounded public-model chart production with isolated Drive publication."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from threading import local

from public_charts import policy
from public_charts.dates import parse_cycle
from public_charts.parallel import LeadQueue, child_path, cleanup_raw, cleanup_staging, execute_leads, model_workspace
from public_charts_publish import publish_manifest
from public_charts.uploads import UploadPool, UploadBudgetExceeded, StreamingUploads
from public_charts.resources import (LaptopLease, ResourceSampler, available_memory, windows_available_memory,
                                     cpu_capacity, worker_count, WorkerMemoryBudget)

ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "data/public_charts_manifest.json"


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def manifest_bytes(payload):
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

def current_render_keys(rows):
    return {key for key, row in rows.items() if row.get("render_version") == policy.render_version(row["cycle"],row["model"],row.get("product"),row.get("domain"))}


def resolve_plan(models, cycle=None, requested_hours=None, now=None, start_cycle=None):
    now = now or datetime.now(timezone.utc)
    if start_cycle:
        parsed = parse_cycle(start_cycle)
        if parsed.strftime("%Y%m%d%H") != start_cycle or parsed.hour % 6:
            raise ValueError("Start cycle must be a valid six-hour UTC cycle")
        if cycle and cycle < start_cycle:
            raise ValueError("Requested cycle precedes the configured production start")
    result = []
    for model in models:
        depth = policy.RETENTION[model] if start_cycle else 2
        cycles = [cycle] if cycle else policy.cycle_candidates(model, now - policy.source_check_delay(model), depth)
        floor = policy.start_cycle(model, start_cycle)
        cycles = [selected for selected in cycles if selected >= floor and policy.retention_current(model,selected,now)]
        for selected in cycles:
            if not policy.supports_cycle(model, selected) and len(models)>1:
                continue
            for hour in requested_hours if requested_hours is not None else policy.hours(model, selected):
                policy.expected_keys(model, selected, hour)
                result.append({"model": model, "cycle": selected, "hour": hour})
    if not cycle:
        order = {model: index for index, model in enumerate(models)}
        result.sort(key=lambda target: (order[target["model"]], -int(target["cycle"]), target["hour"]))
    return result


def recover_rows(inventory):
    rows = {}
    for item in inventory:
        props = item.get("appProperties", {})
        if props.get("kind") != "image":
            continue
        match = policy.KEY_PATTERN.fullmatch(props.get("key", ""))
        if not match:
            raise ValueError("Unrecognized managed image; automatic cleanup refused")
        model, cycle, hour, product, domain = match.groups()
        entry_key = policy.key(model, cycle, int(hour), product, domain)
        if entry_key not in policy.expected_keys(model, cycle, int(hour)):
            raise ValueError("Unexpected managed product; automatic cleanup refused")
        file_id = item["id"]
        sha = props.get("sha256", "")
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("Managed image lacks a valid input-independent content hash")
        row = {"model": model, "cycle": cycle, "hour": int(hour), "product": product, "domain": domain,
               "file_id": file_id, "size": int(item["size"]), "sha256": sha,
               "url": f"https://drive.usercontent.google.com/download?id={file_id}&export=view&authuser=0"}
        rows.setdefault(entry_key, row)
    return rows


def plan_cleanup(rows, inventory, budget, reserve_bytes=0, *, account_budgets=None):
    if reserve_bytes >= budget:
        raise ValueError("One incoming image exceeds the image budget")
    kept, _ = policy.retain(rows, budget - reserve_bytes, account_budgets=account_budgets)
    retained = {row["file_id"] for row in kept}
    candidates = {item["id"] for item in inventory if item.get("appProperties", {}).get("kind") == "image"}
    candidates.update(row["file_id"] for row in rows)
    return kept, sorted(candidates - retained)


def finalize_cleanup(store, manifest, plan, published_payload, *, grace_seconds=600, now=None,
                     max_files=900, workers=3, max_seconds=90, clock=time.monotonic):
    if workers not in (1, 2, 3) or max_files < 0 or max_seconds < 0:
        raise ValueError("Invalid bounded cleanup limits")
    started = clock()
    if plan.get("namespace") != "cds-public-charts-v1" or plan.get("folder_id") != store.folder_id:
        raise ValueError("Cleanup target does not match the isolated chart folder")
    if vars(store).get("storage_folders") != plan.get("storage_folders"):
        raise ValueError("Cleanup account targets differ from the published plan")
    digest = hashlib.sha256(manifest_bytes(manifest)).hexdigest()
    if digest != plan.get("manifest_sha256") or manifest != published_payload:
        raise ValueError("The exact retained manifest must be published before cleanup")
    if store.read_json("manifest.json", {}) != manifest:
        raise ValueError("Drive manifest changed; stale cleanup refused")
    retained = {row["file_id"] for row in manifest["charts"]}
    pending = list(plan.get("delete_ids", []))
    if len(pending) != len(set(pending)) or retained.intersection(pending):
        raise ValueError("Cleanup plan references retained or duplicate files")
    inventory = {item["id"]: item for item in store.inventory()}
    if any(file_id in inventory and inventory[file_id].get("appProperties", {}).get("kind") != "image" for file_id in pending):
        raise ValueError("Only generated image files can be removed")
    state = store.read_json("state.json", {})
    deadlines = state.get("pending_delete_after", {})
    timestamp = now if now is not None else datetime.now(timezone.utc).timestamp()
    remaining, eligible = {}, []
    for file_id in pending:
        if file_id not in inventory:
            continue
        deadline = deadlines.get(file_id, timestamp + grace_seconds)
        remaining[file_id] = deadline
        if timestamp >= deadline:
            eligible.append(file_id)
    clients = local()

    def remove(file_id):
        if workers == 1:
            client = store
        else:
            if not hasattr(clients, "store"):
                clients.store = store.new_upload_worker()
            client = clients.store
        client.delete_managed(file_id)

    removed, submitted, failure = 0, 0, None
    candidates = iter(eligible)
    # The publication/ownership/ETag checks remain mandatory; only independent IO overlaps.
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chart-cleanup") as executor:
            pending_deletes = {}
            while True:
                while (len(pending_deletes) < workers and submitted < max_files and failure is None
                       and clock()-started < max_seconds):
                    file_id = next(candidates, None)
                    if file_id is None:
                        break
                    pending_deletes[executor.submit(remove, file_id)] = file_id
                    submitted += 1
                if not pending_deletes:
                    break
                completed, _ = wait(pending_deletes, return_when=FIRST_COMPLETED)
                for future in completed:
                    file_id = pending_deletes.pop(future)
                    try:
                        future.result()
                        remaining.pop(file_id)
                        removed += 1
                    except Exception as error:
                        failure = failure or error
    finally:
        state["pending_delete_after"] = remaining
        state["pending_delete_ids"] = []
        store.write_json("state.json", state)
        print(json.dumps({"event":"cleanup_finished", "removed":removed, "remaining":len(remaining),
                          "eligible":len(eligible), "workers":workers,
                          "seconds":round(clock()-started,3), "failed":failure is not None}), flush=True)
    if failure:
        raise failure
    return removed


def verify_public_url(url):
    import requests
    # Do not reuse the authenticated Drive session for this check.
    for attempt in range(3):
        try:
            with requests.get(url, headers={"Range": "bytes=0-15"}, stream=True, timeout=(10, 20)) as response:
                response.raise_for_status()
                header = next(response.iter_content(chunk_size=16), b"")
                if header[:4] != b"RIFF" or header[8:12] != b"WEBP":
                    raise ValueError("Anonymous Drive URL did not return a WebP image")
                return
        except requests.HTTPError as error:
            if attempt == 2 or error.response is None or error.response.status_code not in (403,404,429,500,502,503,504):
                raise
        except (requests.ConnectionError, requests.Timeout, ValueError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)


def run(args, store=None):
    plan = resolve_plan(args.models, args.cycle, args.hours, start_cycle=args.start_cycle)
    if args.dry_run:
        print(json.dumps({"dry_run": True, "retention": policy.RETENTION,
                          "products": "nine seasonal products; Asia surfce only; Korea rain/snow layouts",
                          "candidates": plan, "max_leads_per_model": args.max_leads,
                          "workers": args.workers, "time_budget_minutes": getattr(args, "time_budget_minutes", 0),
                          "laptop_guard_configured": bool(os.environ.get("CDS_VTG_RUNNER_ROOT")),
                          "incremental_publication": args.publish_incremental}, indent=2))
        return 0
    work = Path(args.workdir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    if work == ROOT or work == work.parent:
        raise ValueError("A dedicated work directory is required")
    local = args.local
    if local:
        output = work / "preview"
        output.mkdir(parents=True, exist_ok=True)
        state = read_json(output / "manifest.json", {"charts": []})
        inventory = []
    else:
        from public_charts.storage import production_store
        store = store or production_store()
        store.verify_account_and_folder()
        state = store.read_json("state.json", {"schema_version": 1, "rows": []})
        if state.get("schema_version") != 1:
            raise ValueError("Unknown private production state version")
        if state.get("storage_layout") == "balanced-v1" and not hasattr(store, "storage_folders"):
            raise ValueError("Balanced chart state requires the two-account coordinator")
        if state.get("storage_layout") == "balanced-v1" and state.get("storage_folders") != store.storage_folders:
            raise ValueError("Stored account targets differ from the configured chart folders")
        inventory = store.inventory()
        if hasattr(store, "prime_inventory"):
            store.prime_inventory(inventory)
    rows = {policy.row_key(row): row for row in state.get("rows", state.get("charts", []))}
    if not local:
        recover_rows(inventory)
        valid_ids = {item["id"] for item in inventory}
        rows = {key: row for key, row in rows.items() if row.get("file_id") in valid_ids}
        if hasattr(store, "storage_folders"):
            for row in rows.values():
                row["storage_account"] = store.account_for_file(row["file_id"])
        # Uncommitted uploads are not public state. A retry reuses and verifies
        # them through upload_image rather than trusting their metadata alone.
    fresh = current_render_keys(rows)
    if not args.cycle:
        planned={(item["model"],item["cycle"]) for item in plan}
        for model in args.models:
            previous=sorted({row["cycle"] for row in rows.values() if row["model"]==model},reverse=True)[:policy.RETENTION[model]]
            for cycle in previous:
                if ((model,cycle) not in planned and cycle >= policy.start_cycle(model,args.start_cycle)
                        and policy.source_check_due(cycle, model=model) and policy.retention_current(model,cycle)):
                    plan.extend({"model":model,"cycle":cycle,"hour":hour} for hour in policy.hours(model, cycle))
        order = {model: index for index, model in enumerate(args.models)}
        plan.sort(key=lambda target: (order[target["model"]], -int(target["cycle"]), target["hour"]))
    image_bytes = policy.unique_bytes(rows.values())
    if not local:
        image_bytes = sum(int(item.get("size", 0)) for item in inventory
                          if item.get("appProperties", {}).get("kind") == "image")
    accounts = tuple(store.storage_folders) if not local and hasattr(store, "storage_folders") else ("primary",)
    account_bytes = {account: sum(int(item.get("size", 0)) for item in inventory
                                 if item.get("appProperties", {}).get("kind") == "image"
                                 and item.get("storage_account", "primary") == account) for account in accounts}
    pending_by_account, reserve_by_account = dict.fromkeys(accounts, 0), dict.fromkeys(accounts, 0)
    minutes = getattr(args, "time_budget_minutes", 0)
    cap = max(len(policy.hours(model))*policy.RETENTION[model] for model in args.models) if minutes else args.max_leads
    public_before = (state if local else store.read_json("manifest.json", {}))
    published = {policy.row_key(row) for row in public_before.get("charts", [])}
    queue = LeadQueue(plan, fresh, cap, args.force, published=published)
    counts = queue.counts
    for model in args.models:
        counts.setdefault(model, 0)
    errors = []
    verified_cycles = set(state.get("verified_cycles", []))
    budget_blocked = False
    reserve_bytes = 0
    before_ids = {item["id"] for item in inventory} | {row["file_id"] for row in rows.values()}
    upload_workers = getattr(args, "upload_workers", 1)
    uploader = UploadPool(store, upload_workers) if not local else None
    pending_bytes = 0
    publication_dirty = False
    publication_metrics, pending_aliases, uploading_keys = {}, [], set()
    last_publication = 0.
    publication_interval = getattr(args, "publish_interval_seconds", 15)
    remote = None
    source_monitor = None
    from public_charts.kma_gate import KmaReleaseGate
    kma_gate=KmaReleaseGate() if not local else None

    def save_state():
        if local:
            write_json(output / "manifest.json", policy.public_manifest(list(rows.values())))
        else:
            store.write_json("state.json", {"schema_version": 1, "pipeline_version": policy.PIPELINE_VERSION,
                             "rows": list(rows.values()), "errors": errors[-40:],
                             "verified_cycles": sorted(verified_cycles),
                             **({"storage_layout": "balanced-v1", "storage_folders": store.storage_folders}
                                if len(accounts) > 1 else {}),
                             "pending_delete_after": state.get("pending_delete_after", {})})

    def snapshot():
        nonlocal last_publication, publication_dirty
        if publication_dirty:
            save_state()
        account_budgets = {account: args.max_bytes - reserve_by_account[account] for account in accounts}
        kept, delete_ids = plan_cleanup(list(rows.values()), inventory, args.max_bytes, reserve_bytes,
                                       account_budgets=account_budgets if len(accounts) > 1 else None)
        manifest = policy.public_manifest(kept)
        previous = read_json(args.manifest, {})
        if previous.get("charts") == manifest["charts"] and previous.get("models") == manifest["models"]:
            manifest = previous
        store.write_json("manifest.json", manifest)
        write_json(args.manifest, manifest)
        write_json(work / "cleanup-plan.json", {"namespace": "cds-public-charts-v1", "folder_id": store.folder_id,
                   "manifest_sha256": hashlib.sha256(manifest_bytes(manifest)).hexdigest(), "delete_ids": delete_ids,
                   **({"storage_folders": store.storage_folders} if len(accounts) > 1 else {})})
        if args.publish_incremental:
            publish_manifest(ROOT)
        last_publication = time.monotonic()
        publication_dirty = False
        now = datetime.now(timezone.utc)
        retained_keys = {policy.row_key(row) for row in manifest["charts"]}
        queue.mark_published(retained_keys)
        for entry_key, item in list(publication_metrics.items()):
            if entry_key not in retained_keys:
                continue
            event = {"event": "image_published", **item["target"], "product": item["product"],
                     "domain": item["domain"], "published_at": now.isoformat(),
                     "upload_seconds": item.get("upload_seconds", 0)}
            for source, label in (("observed_at", "ready_to_publish_seconds"),
                                  ("created_at", "encoded_to_publish_seconds"),
                                  ("source_last_modified_at", "source_last_modified_age_seconds")):
                if item.get(source):
                    event[label] = round(max(0, (now - datetime.fromisoformat(item[source])).total_seconds()), 3)
            event["observed_ready_over_15m"] = event.get("ready_to_publish_seconds", 0) > 900
            print(json.dumps(event), flush=True)
        for entry_key in retained_keys:
            publication_metrics.pop(entry_key,None)
        return kept, delete_ids

    class BudgetReached(Exception):
        pass

    def record(item, info):
        nonlocal image_bytes, publication_dirty
        model, cycle, hour = (item["target"][key] for key in ("model", "cycle", "hour"))
        if not local:
            verification_key = f"{model}/{cycle}"
            if len(accounts) > 1:
                verification_key += "/" + info["storage_account"]
            if verification_key not in verified_cycles:
                verify_public_url(info["url"])
                verified_cycles.add(verification_key)
            if info["file_id"] not in before_ids:
                image_bytes += int(info["size"])
                account_bytes[info.get("storage_account", "primary")] += int(info["size"])
                before_ids.add(info["file_id"])
        rows[item["key"]] = {"model": model, "cycle": cycle, "hour": hour,
                             "product": item["product"], "domain": item["domain"],
                             "render_version": policy.render_version(cycle,model,item["product"],item["domain"]),
                             **{key: info[key] for key in ("file_id", "url", "size", "sha256")},
                             **({"storage_account": info["storage_account"]} if "storage_account" in info else {})}
        fresh.add(item["key"])
        uploading_keys.discard(item["key"])
        publication_metrics[item["key"]] = item
        publication_dirty = True

    def reserve(item):
        nonlocal pending_bytes
        account = store.account_for_key(item["key"]) if len(accounts) > 1 else "primary"
        if account_bytes[account] + pending_by_account[account] + item["size"] > args.max_bytes:
            exception = UploadBudgetExceeded(item["size"])
            exception.storage_account = account
            raise exception
        pending_bytes += item["size"]
        pending_by_account[account] += item["size"]

    def release(item):
        nonlocal pending_bytes
        pending_bytes -= item["size"]
        account = store.account_for_key(item["key"]) if len(accounts) > 1 else "primary"
        pending_by_account[account] -= item["size"]

    stream = StreamingUploads(uploader, reserve, release, record) if uploader else None

    def resolve_aliases():
        for item in list(pending_aliases):
            target = item["target"]
            source = policy.key(target["model"], target["cycle"], target["hour"], *item["alias_of"])
            if source in rows and source not in uploading_keys:
                record(item, rows[source])
                pending_aliases.remove(item)

    def tick(*, block=False):
        nonlocal reserve_bytes, budget_blocked, publication_dirty
        if stream:
            try:
                stream.poll(block=block)
            except UploadBudgetExceeded as exception:
                budget_blocked = True
                reserve_bytes = max(reserve_bytes, exception.size)
                account = getattr(exception, "storage_account", "primary")
                reserve_by_account[account] = max(reserve_by_account[account], exception.size)
                errors.append({"status": "budget", "error": str(exception)})
                raise BudgetReached from None
        if remote:
            remote.tick()
        resolve_aliases()
        if kma_gate and kma_gate.poll(rows):
            publication_dirty=True
        if not local and publication_dirty and time.monotonic() - last_publication >= publication_interval:
            snapshot()

    def consume(target, staged):
        model, cycle, hour = target["model"], target["cycle"], target["hour"]

        try:
            images, aliases, seen = [], [], set()
            for artifact in staged["artifacts"]:
                product, domain = artifact["product"], artifact["domain"]
                entry_key = policy.key(model, cycle, hour, product, domain)
                if entry_key not in policy.expected_keys(model, cycle, hour) or entry_key in seen:
                    raise ValueError("Unexpected renderer output")
                seen.add(entry_key)
                item = {**artifact, "key": entry_key, "target": target}
                if artifact["alias_of"]:
                    aliases.append(item)
                else:
                    path = Path(artifact["path"])
                    directory = child_path(model_workspace(work, model), "staged", cycle, f"{hour:03}")
                    if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or path.suffix != ".webp":
                        raise ValueError("Unsafe staged image")
                    images.append({**item, "path": path, "size": path.stat().st_size})
            if local:
                for item in images:
                    path = item["path"]
                    destination = output / "images" / model / path.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
                    record(item, {"file_id": item["key"], "url": destination.relative_to(output).as_posix(),
                                  "size": item["size"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            else:
                uploading_keys.update(item["key"] for item in images)
                stream.enqueue(images)
            pending_aliases.extend(aliases)
            tick()
            while stream and len(stream.waiting) + len(stream.pending) > args.workers * 24:
                tick(block=True)
        except BaseException:
            save_state()
            raise
        if staged.get("partial"):
            return
        error = staged["error"]
        if error:
            errors.append({**target, "status": "pending" if error["pending"] else "error", "error": error["type"],
                           **{key: value for key, value in error.items() if key not in {"type", "pending"}}})
            print(json.dumps(errors[-1]), flush=True)
            if args.fail_fast and not error["pending"]:
                raise RuntimeError("Renderer failed: " + error["type"])
        else:
            print(json.dumps({**target, **staged["result"], "render_seconds": staged["render_seconds"],
                              "event": "render_finished", "prefetch_seconds": staged.get("prefetch_seconds", 0),
                              "publication_pending": bool(uploading_keys or publication_dirty),
                              "upload_workers": upload_workers,
                              "worker_peak_rss_mib":staged.get("worker_peak_rss_mib"),
                              "renderer_pid": staged["pid"]}), flush=True)

    def started(target, active):
        print(json.dumps({**target, "event": "render_started", "active_workers": active,
                          "work_class":"refresh" if queue.priority(target)[0] else "operational"}), flush=True)

    lease, sampler = LaptopLease(), ResourceSampler()
    memory_budget = WorkerMemoryBudget(args.workers)
    acquired = lease.acquire()
    deadline = time.monotonic() + minutes*60 if minutes else None
    stop_reason = None
    previous_cycles = {(row["model"], row["cycle"]) for row in rows.values()}
    if minutes and not local and getattr(args,"source_monitor",False):
        from public_charts.monitor import SourceMonitor
        source_monitor=SourceMonitor(queue,work)
        source_monitor.start()

    def can_schedule():
        nonlocal stop_reason
        if stop_reason is not None:
            return False
        if not acquired:
            stop_reason = "shared_laptop_lock"
            return False
        if deadline is not None and time.monotonic() >= deadline+90:
            stop_reason = "time_budget"
            return False
        return True

    def refresh_plan():
        planned = resolve_plan(args.models, args.cycle, args.hours, start_cycle=args.start_cycle)
        cycles = {(item["model"], item["cycle"]) for item in planned}
        for model, cycle in previous_cycles - cycles:
            if cycle >= policy.start_cycle(model,args.start_cycle) and policy.source_check_due(cycle, model=model) and policy.retention_current(model,cycle):
                planned.extend({"model": model, "cycle": cycle, "hour": hour} for hour in policy.hours(model, cycle))
        order = {model: index for index, model in enumerate(args.models)}
        planned.sort(key=lambda target: (order[target["model"]], -int(target["cycle"]), target["hour"]))
        print(json.dumps({"event": "plan_refreshed", "candidates": len(planned)}), flush=True)
        return planned

    if getattr(args,"remote_helper",False) and not local:
        from public_charts.artifacts import ArtifactError, GitHubArtifacts
        from public_charts.remote import RemoteCoordinator
        try:
            session = os.environ["GITHUB_RUN_ID"]+"-"+os.environ.get("GITHUB_RUN_ATTEMPT","1")
            code_sha = subprocess.check_output(["git","-c",f"safe.directory={ROOT.as_posix()}","rev-parse","HEAD"],
                                               cwd=ROOT,text=True).strip()
            remote = RemoteCoordinator(GitHubArtifacts(),session,code_sha,queue,work,consume,minutes=minutes,
                                       can_assign=lambda:not budget_blocked and acquired and
                                       (deadline is None or time.monotonic()<deadline))
            remote.start()
        except (ArtifactError,KeyError,ValueError,OSError,subprocess.SubprocessError) as error:
            print(json.dumps({"event":"helper_unavailable","error":type(error).__name__}),flush=True)
            remote = None

    print(json.dumps({"event": "runtime_profile", "workers": args.workers, "upload_workers": upload_workers,
                      "prepare_workers":0 if local else 2,"prepared_queue_limit":args.workers+(0 if local else 2),
                      "heavy_workers":memory_budget.snapshot()["heavy_workers"],
                      "resource_policy":"measured_worker_memory",
                      "cpu_capacity": cpu_capacity(), "available_memory_mib": round(available_memory()/1024**2, 1) if available_memory() is not None else None,
                      "windows_available_memory_mib": round(windows_available_memory()/1024**2,1) if windows_available_memory() is not None else None,
                      "time_budget_minutes": minutes, "laptop_guard": lease.root is not None,
                      "same_model_parallelism": True, "source_refresh_seconds": getattr(args,"refresh_seconds",60),
                      "coordinator": "github", "helper_enabled": bool(remote and not remote.disabled),
                      "source_monitor_enabled":bool(source_monitor),"source_probe_interval_seconds":60,
                      "source_probe_workers":2,"source_probe_lookahead":4,
                      "automatic_cycle_check_delay_hours": policy.SOURCE_CHECK_DELAY.total_seconds()/3600,
                      "cycle_check_delay_hours_by_model": {model: policy.source_check_delay(model).total_seconds()/3600
                                                            for model in args.models},
                      "publication_interval_seconds": publication_interval, "streaming_images": not local,
                      "latency_policy":"20261118-pipeline-v2","seasonal_upper_download":True,
                      "scheduling_policy":"operational-first-newest-ready-cycle",
                      "partial_field_cache":True,"unstarted_lease_rebalance":True,
                      "cleanup_workers":3,"cleanup_submission_budget_seconds":90,
                      "current_precipitation_first":True,"prepared_cache_max_mib":512,
                      "prepared_cache_ttl_hours":24,"batch_drain_seconds":90}), flush=True)
    sampler.start()
    failure = None
    try:
        execute_leads(queue, work, args.workers, consume, force=args.force,
                      raw_override=args.cache_root, started=started, can_schedule=can_schedule,
                      refresh=refresh_plan if minutes else None,
                      refresh_seconds=getattr(args, "refresh_seconds", 60),
                      idle_seconds=max(120, minutes * 60), streaming=not local, prefetch=not local,
                      progress=lambda event: print(json.dumps(event), flush=True), on_idle=tick,
                      cleanup_staged=False,keep_alive=remote.keep_alive if remote else None,
                      can_prepare=lambda:deadline is None or time.monotonic()<deadline-90,
                      memory_budget=memory_budget)
    except BudgetReached:
        pass
    except BaseException as exception:
        failure = exception
    finally:
        if source_monitor:
            source_monitor.stop.set()
        if remote:
            remote.close()
        try:
            while stream:
                tick(block=True)
        except BudgetReached:
            pass
        except BaseException as exception:
            failure = failure or exception
        if uploader:
            uploader.close()
        if source_monitor:
            source_monitor.close()
        if kma_gate:
            kma_gate.close()
            kma_gate.poll(rows)
        cleanup_staging(work, args.models)
        resolve_aliases()
        lease.close()
        resource_usage = {**sampler.finish(),**memory_budget.summary()}
        save_state()
        if not local:
            kept, delete_ids = snapshot()
    if failure is not None:
        print(json.dumps({"event": "production_failed", "error": type(failure).__name__,
                          "chart_key": getattr(failure, "chart_key", None),
                          "http_status": getattr(failure, "http_status", None),
                          "reason": getattr(failure, "reason", None),
                          "transport_error_type": getattr(failure, "error_type", None),
                          "rendered_leads": counts, **resource_usage}), flush=True)
        raise failure
    if local:
        manifest = policy.public_manifest(list(rows.values()))
        write_json(output / "manifest.json", manifest)
        cards = "".join(f'<figure><figcaption>{html.escape(policy.row_key(row))}</figcaption>'
                        f'<img style="max-width:100%" src="{html.escape(row["url"])}"></figure>'
                        for row in rows.values())
        (output / "index.html").write_text('<!doctype html><meta charset="utf-8"><title>Public chart preview</title>' + cards, encoding="utf-8")
        print(f"Local preview: {output / 'index.html'}")
        return int(any(row["status"] == "error" for row in errors))
    print(json.dumps({"rendered_leads": counts, "retained_bytes": policy.unique_bytes(kept),
                      "retained_bytes_by_account": {account: policy.unique_bytes(row for row in kept
                          if row.get("storage_account", "primary") == account) for account in accounts},
                      "pending_delete_files": len(delete_ids), "budget_wait": budget_blocked,
                      "stop_reason": stop_reason, **resource_usage, "errors": errors[-40:]}))
    return int(any(row["status"] == "error" for row in errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    from public_charts.storage import balanced_enabled
    default_models = [model for model in policy.RETENTION if model != "icon" or balanced_enabled()]
    parser.add_argument("--models", nargs="+", choices=policy.RETENTION, default=default_models)
    parser.add_argument("--cycle")
    parser.add_argument("--start-cycle", help="Never produce initialization cycles before this UTC YYYYMMDDHH")
    parser.add_argument("--hours", nargs="+", type=int)
    parser.add_argument("--max-leads", type=int, default=2)
    parser.add_argument("--workers", choices=("auto", "1", "2", "3", "4"), default="auto",
                        help="Separate renderers, capped by available CPU and memory")
    parser.add_argument("--upload-workers", type=int, choices=(1, 2, 3, 4), default=3)
    parser.add_argument("--time-budget-minutes", type=float, default=0,
                        help="Continuously refresh ready work until the dispatch budget expires")
    parser.add_argument("--publish-incremental", action="store_true", help="Push each completed lead's verified manifest")
    parser.add_argument("--publish-interval-seconds", type=float, default=15,
                        help="Coalesce completed-image URL publication without waiting for an entire lead")
    parser.add_argument("--refresh-seconds", type=float, default=60)
    parser.add_argument("--no-source-monitor",dest="source_monitor",action="store_false",default=True,
                        help="Use validated preparation directly without background discovery")
    parser.add_argument("--max-bytes", type=int, default=policy.MAX_IMAGE_BYTES)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--workdir", type=Path, default=ROOT / "outputs/public-charts-work")
    parser.add_argument("--cache-root", help="Only for explicit local preview; production scratch is isolated")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--remote-helper",action="store_true",help="GitHub-only coordinator with an optional compute helper")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--check-drive", action="store_true")
    parser.add_argument("--finalize-cleanup", action="store_true")
    args = parser.parse_args()
    args.workers = worker_count(args.workers, local=args.local)
    if not 1 <= args.publish_interval_seconds <= 60 or not 5 <= args.refresh_seconds <= 120:
        parser.error("Publication must use 1-60 seconds and source-plan refresh 5-120 seconds")
    if not 0 <= args.time_budget_minutes <= 30 or args.time_budget_minutes and (args.force or args.local):
        parser.error("Continuous production allows 0-30 minutes; force and local mode require a bounded batch")
    if args.cache_root and args.workers != 1 or args.local and args.publish_incremental:
        parser.error("A local cache override requires one worker; local mode cannot publish to Git")
    if args.remote_helper and (args.local or args.force or not args.time_budget_minutes):
        parser.error("A compute helper requires non-forced continuous production")
    if args.max_leads < 1 or args.max_bytes < 1 or args.cache_root and not args.local:
        parser.error("Invalid batch/budget; cache-root is local-only")
    if args.check_drive or args.finalize_cleanup:
        from public_charts.storage import production_store
        store = production_store()
        if args.check_drive:
            store.verify_account_and_folder()
            print("Dedicated Drive account and owned folder verified; no files changed.")
            if hasattr(store, "quota_summary"):
                print(json.dumps({"storage_quota": store.quota_summary()}))
            return 0
        published = subprocess.check_output(["git", "-c", f"safe.directory={ROOT.as_posix()}", "show",
                                             "origin/main:data/public_charts_manifest.json"], cwd=ROOT)
        count = finalize_cleanup(store, read_json(args.manifest, {}),
                                 read_json(args.workdir / "cleanup-plan.json", {}), json.loads(published))
        print(f"Permanently removed {count} superseded managed images after publication.")
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
