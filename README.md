# CDS Public Charts

Dedicated production repository for AIFS, GFS, AIGFS, CMC GDPS and ECMWF IFS off-cycle charts.
The [CDS viewer](https://ssalkim.github.io/CDS/) and VTG remain in
[SSalKim/CDS](https://github.com/SSalKim/CDS).

- Images: existing, separate chart Google Drive account and folder.
- New images use owned model subfolders (`UKMO`, `ECMWF`, `AIFS`, `GFS`,
  `AIGFS`, `CMC`, `ICON`) on the model's existing assigned Drive. Filenames are
  `model_domain_product_sHHH_YYYYMMDDHH_digest.webp`, for example
  `gfs_asia_gph500_s024_2026111812_<sha256-prefix>.webp`.
  Existing root-level images keep their names, IDs and URLs; matching bytes are
  reused without relocation. Inventory and conditional expiry cleanup include
  both layouts. Private `state.json` and `manifest.json` remain at the root.
- Public index: `data/public_charts_manifest.json`; refreshed without Pages builds.
- Scheduling: Cloudflare checks `public_charts.yml` every minute and dispatches
  only if no active or queued production run exists (Worker deployment required).
  There is intentionally no GitHub-native cron in this repository.
- Automatic raw-source checks wait until initialization time + three hours:
  00Z from 12:00 KST, 06Z from 18:00 KST, 12Z from 00:00 KST the next day,
  and 18Z from 06:00 KST. The window applies to initial planning, continuous
  refresh and restoration of incomplete cycles. Older eligible unfinished work
  continues; Cloudflare itself is not paused. Explicit manual cycle requests
  can override the delay. These are earliest check times, not guaranteed arrivals.
- ICON starts checks at +2h30 instead: 11:30, 17:30, 23:30 and 05:30 KST.
  Its observed initial 12Z/18Z files were available around +2h39; the other
  models retain the +3h gate.
- Cumulative rain/snow preparation has a cooperative five-minute work budget,
  including checkpoint-lock waits. Completed prefixes remain verified and are
  resumed on the next attempt; no incomplete accumulation is plotted.
  `surface_phase` and `product_started`/`product_finished` identify slow stages.
  Active renderer tasks emit thread stacks after three minutes for diagnosis.
- Winter precipitation hatching uses the same continuous SWE field, validity mask,
  and strict `>0.25 kg/m2` cutoff as the corresponding snowfall panel, for both
  interval `acptot` and total `acrain`. Exactly 0.25 is excluded; a separately
  interpolated categorical boundary or the last subinterval's flag is not used.
  Hatching and snowfall shading/edges share the exact same precipitation clip
  path, so both remain inside the matching interval or cumulative rain footprint.
  Rain uses strict `>0.5 mm` in May-October and `>0.25 mm` in November-April;
  winter triptychs are October-April. Source snowfall amounts are not filtered
  or smoothed to implement this display constraint.
  Native SWE (AIFS/ECMWF/CMC/ICON) and native GFS phase sampling are unchanged.
- UKMO integrates native large-scale and convective snowfall rates, retaining the
  existing same-interval precipitation cap without an additional surface T/RH
  filter on SWE. The strict `>0.25 kg/m2` display cutoff is shared by all models.
  Surface occurrence conditions remain part of SR depth conversion, not SWE.
  `ukmo-winter-v3-native-swe` isolates step and cumulative checkpoints from the
  superseded exp3 caches. The shared precipitation boundary (render revision 10)
  starts at initialization `2026111812`. Existing winter Korea composites and
  aliases from earlier cycles are accepted at their published revisions and are
  not rebuilt for these changes. Missing older products may still be completed;
  nothing already published is rolled back or deleted for a style update.
- UKMO Asia longitude wrapping starts at initialization `2026111812`. Native
  -180..180 coordinates are converted to 0..360 and the 20..180 / 180..220 slabs
  are read separately, with values and terrain flags reordered together. The
  native, collected-field and Asia precipitation caches are versioned to exclude
  earlier truncated grids. Render revision 11 applies only to new UKMO Asia
  charts; earlier Asia images, Korea and other models are unchanged.
- Automatic initial and refreshed plans keep private historical cycles inside
  each model's existing retention window. Deleted, retired cycles are never
  revived as missing operational work; explicit manual cycle requests still work.
  Post-publication cleanup uses up to 12 bounded I/O threads, prioritizing larger
  expired images within the same 90-second submission and 900-file limits.
  Ownership, folder, ETag, exact-manifest and cached-link grace checks are unchanged.
- Automatic production may defer rollover once when already-ready operational
  work remains near the normal deadline: the total dispatch budget is capped at
  30 minutes. Explicit manual budgets, dry runs, forced runs and selected-cycle
  jobs do not opt in. In-flight transfers and renderer drain limits remain bounded.
- Helper selection runs on the GitHub coordinator without a separate route job.
  Both Drive inventories are still freshly verified but their independent reads
  overlap. Python environments and artifact transport modules are version-checked
  and reused outside the checkout on persistent runners; GitHub ephemeral runners
  retain their normal package-download caches.
- ICON prefers the exact-cycle static compressed feed, with the v1 endpoint as
  fallback. Each request uses one 15-second elapsed budget and a failed feed's
  five-minute cooldown is shared among processes on the same runner. A transfer
  deferral gets one bounded same-helper retry; local preparation keeps a short
  local retry preference. Loss of the helper still reclaims its leases normally.
  This changes transport/scheduling only, not forecast calculations or render
  versions; already-published ICON 12Z files are not rebuilt.
- `PUBLIC_CHARTS_ENABLED=true` enables production; `false` blocks production
  dispatches while still allowing a non-mutating dry run.
- `PUBLIC_CHARTS_START_CYCLE=2026111512` preserves the production start boundary.
- Only five `PUBLIC_CHARTS_DRIVE_*` repository Secrets are required.
- Managed image storage is capped at 14,000,000,000 bytes (14 decimal GB),
  including uploads in flight and expired images awaiting deletion. Retention
  remains nine cycles for AIFS/GFS/AIGFS and five for CMC; the byte cap can
  still evict older cycles early. Metadata and other account usage need
  separate space within the dedicated Drive account's quota.
- ECMWF IFS production starts at `2026111606`, only at 06/18 UTC. Official 0.25-degree
  GRIB data are used for both surface and pressure levels, with the same nine
  seasonal products and five retained cycles. The lead schedule is 000-084 by 3h,
  090/096, then 108-144 by 12h; precipitation uses each displayed interval.
  CDS keeps one ECMWF button: 00/12 UTC still uses the existing KMA image archive,
  while 06/18 UTC resolves only the generated Drive manifest. No Open-Meteo or
  0.1-degree data are mixed into these products.
- Production is serialized in one concurrency group. Never enable the old CDS
  producer and this producer together: both use the same Drive state.
- Inside that one job, isolated workers share download preparation and rendering.
  Any model can borrow idle slots for different forecast leads; models rotate fairly
  and each model's newest cycle is prioritized before historical backfill.
  There are no CMC/ICON-specific concurrency limits. Each lead has
  separate raw scratch files. Shared cumulative rain/snow checkpoints are protected
  by a per-cycle process lock; only prefix calculation is serialized, not rendering.
  Only the coordinator uploads to Drive, updates manifests, publishes Git commits,
  and plans cleanup. Completed images stream to the coordinator before the whole
  lead finishes. One shared queue fills three independent upload slots across leads.
  Verified URLs are coalesced into publications every 15 seconds (plus the final
  flush); the first image is published immediately. Aliases wait for their source
  image's verification, and staged files remain until all uploads drain.
  The upload executor also accepts a bounded lookahead of eight reserved files
  per IO thread, so those same three threads keep transferring while the
  coordinator saves and publishes the manifest. Network concurrency is unchanged;
  only the coordinator reserves bytes, records results and publishes URLs.
- Rendering defaults to `auto`: up to four processes, limited by effective CPU
  and available memory (including WSL/container limits). Windows host headroom is
  also sampled. Model-independent admission reserves 1 GiB free in Linux and
  1.5 GiB on Windows, plus growth room for every active worker. New starts pause
  under memory pressure and resume when headroom recovers; running work is not
  killed. The per-worker reservation is at least 1 GiB or 125% of the largest
  measured peak among active and the last eight completed tasks, whichever is
  greater. Peaks are sampled, not an OS-enforced hard memory limit. Set
  `PUBLIC_CHARTS_WORKERS=1` to restore sequential rendering.
  Total renderer caps are four at 8 GiB available, three at 6 GiB, two below that,
  and one below 3 GiB. Explicit worker requests cannot bypass these limits.
  Renderers collect retired figure reference cycles after every image and lead
  to prevent large NumPy-backed plots from lingering between tasks.
- A bounded renderer scheduler keeps working while the coordinator uploads.
  `PUBLIC_CHARTS_UPLOAD_WORKERS` controls independent image-upload sessions
  (default `3`; `1` restores sequential uploads). Only the coordinator changes
  state, manifests, aliases, budget reservations, cleanup plans, and Git.
  Small images use one multipart upload; MD5/size, ownership and public-reader
  verification remain mandatory. Larger images retain resumable uploads.
  Multipart creation uses [pre-generated Drive IDs](https://developers.google.com/workspace/drive/api/guides/manage-uploads#use_a_pre-generated_id_to_upload_files):
  transient upload failures retry the same bytes and ID, at most three attempts,
  without creating duplicate files after a lost response. A conflict still requires
  the existing content, owner, folder and chart-key checks. Transient reads and
  anonymous public-URL propagation checks also have bounded retries; persistent
  permission, quota, integrity or network failures remain visible errors.
- Automatic dispatches keep processing for a 20-minute dispatch budget instead
  of stopping after two leads per model. `PUBLIC_CHARTS_TIME_BUDGET_MINUTES`
  changes that budget (maximum 30). Plans refresh every 60 seconds so a newly
  available cycle can enter the queue. A continuous run waits for inputs until its
  budget, and
  in-flight work is drained and published after the dispatch deadline.
- A missing +000h, or two missing nonzero leads, pauses that model/cycle for two
  minutes during lookahead. An unseen cycle gets one readiness probe at a time,
  so absent data cannot occupy all four workers. Late rain postpones only that
  lead's surface products, not subsequent upper-air leads.
- Disk-backed lookahead prepares upcoming leads while ready leads render within
  the same four-process budget. Each renderer fetches rain/winter inputs on one
  background thread while drawing upper-air charts. Same-hour surface variables
  share inventory lookups. Checkpoint format, native grids, winter equations and
  lossless WebP encoding remain unchanged.
- Raw-source failover validates the same cycle, lead and fields on each mirror.
  AIFS uses AWS, Google Cloud, then ECMWF; GFS (including native sflux) uses AWS
  then NOMADS; AIGFS uses NOMADS then AWS. CMC uses dated Datamart paths followed
  by HPFX. Inventories and byte offsets are reloaded from each selected mirror;
  offsets from one host are never reused on another. Missing data stays pending,
  but corrupt data or permission errors are not hidden if every mirror fails.
- GitHub-hosted `ubuntu-24.04` always coordinates production and exclusively
  owns Drive uploads, public manifests, quota checks and deletion. When
  `PUBLIC_CHARTS_DUAL_COMPUTE_ENABLED=true`, an idle dedicated laptop labeled
  `cds-public-charts` may run a separate compute-only workflow. Routing checks
  both repositories; a busy VTG runner prevents adding that helper. Both laptop
  registrations retain their shared host lock. VTG waits up to three minutes
  for the laptop before using GitHub-hosted, without interrupting chart work.
- `run_minutes` is an optional manual validation input; explicit cycle/hour
  batches still use their lead limit unless a positive budget is supplied.
  No Cloudflare Worker changes are required for these defaults.
- The renderer and model schedules are unchanged. Completed products are reused;
  no existing Drive images need to be copied or regenerated for the migration.

## Cloudflare Setup

The complete replacement [Worker script](cloudflare/worker.js),
[Cron Triggers and setup guide](cloudflare/README.md), and example Wrangler config
are included in `cloudflare/`. The old GDM and DMDW dispatches are removed.

Allow the existing Cloudflare GitHub token to access **SSalKim/CDS-Public-Charts**.
A fine-grained token needs repository **Actions: read and write** permission.
Store it as a Worker secret, not in source or browser code.

Dispatch endpoint:

```text
POST https://api.github.com/repos/SSalKim/CDS-Public-Charts/actions/workflows/public_charts.yml/dispatches
```

Headers:

```text
Authorization: Bearer <WORKER_SECRET>
Accept: application/vnd.github+json
Content-Type: application/json
User-Agent: CDS-Cloudflare-Scheduler
X-GitHub-Api-Version: 2022-11-28
```

Body:

```json
{
  "ref": "main",
  "inputs": {
    "dry_run": false,
    "runner_mode": "auto",
    "model": "all",
    "max_leads": "2",
    "force": false
  }
}
```

Leave `cycle` and `hours` unset so the producer checks recent incomplete cycles.
`dry_run` defaults to **true** in the manual UI: send **false** for production.
A successful dispatch returns HTTP 204; it does not mean rendering has finished.

Before dispatching, query this workflow's runs and skip a tick if a run has an
active status (`queued`, `in_progress`, `waiting`, `pending`, or `requested`):

```text
GET https://api.github.com/repos/SSalKim/CDS-Public-Charts/actions/workflows/public_charts.yml/runs?per_page=100
```

Check GitHub responses, use bounded retries for transient errors and avoid
dispatching if the status check fails. The workflow's concurrency group is a
second safeguard, not a cross-repository lock. Use the every-minute chart trigger
and keep VTG's 09/19/29/39/49/59 UTC trigger unchanged. Deploy the matching Worker;
its routing uses the trigger expression because both schedules overlap.

## Runner Routing

`auto` keeps the coordinator on `ubuntu-24.04` and adds an online, idle
`LAPTOP-WOOJIN-CHARTS` compute helper when VTG is also idle and the dual-compute
variable is enabled. `github-hosted` explicitly disables the helper. The laptop
receives no Drive credentials, state files or deletion authority. Each machine
uses its own resource-limited pool of up to four renderers. A single queue leases
distinct model/cycle/lead tasks dynamically rather than fixing models to hosts.

Control messages and small verified WebP batches use private, one-day Actions
artifacts. Results are imported into the coordinator's normal upload queue after
session, revision, target, dimensions and SHA-256 checks. Consumed results are
deleted immediately. A 90-second heartbeat timeout or 180-second startup timeout
returns unfinished tasks to GitHub; expired leases reject late results. This
separate helper workflow never blocks final publication if the laptop goes away.
Healthy coordinator control keeps the helper waiting through empty queues and
`NotReady` responses for the batch lifetime; there is no three-minute idle exit.
The startup timeout, stale-control checks and lost-heartbeat recovery still apply.
The coordinator stops new assignments at its normal time budget and allows up to
eight additional minutes for active helper tasks. No new Cloudflare settings or
Google credentials are required. `verify_compute.yml` exercises transport without
Drive writes; select `helper_runner=laptop` to test the real self-hosted workspace.
The helper declares its complete required checkout list and checks its entry
points before installation, so a reused sparse checkout cannot omit new scripts.

After a laptop/WSL restart, run `~/actions-runner-cds-public-charts/start-charts.sh`
in a separate WSL terminal and keep it running. This is not an OS autostart
service. Start the original VTG listener as before. Both repositories must be
readable by `RUNNER_STATUS_TOKEN` with Administration Read permission.

## Verification

`Verify chart Drive connection` authenticates and checks the existing folder
without uploading, deleting or editing any Drive files. Use this before enabling
production. A production dry run does not need credentials or download grids.

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-public-charts.txt
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe public_charts_run.py --dry-run --start-cycle 2026111512
```

See [setup and retention details](docs/public-charts-setup.md).
`vtg_drive_archive.py` is the shared Drive client snapshot, not a VTG workflow.

## Latency Target And Measurements

The target is publication within 15 minutes of the necessary inputs becoming
available, with 3-5 minutes preferred. This is not a proven SLA. The one-minute
Cloudflare trigger reduces idle gaps after deployment; source outages, cold cumulative prefixes, startup
and bursts larger than capacity can add latency. CDS needs its next manifest check
(20 seconds while viewing external models). There is no independent monitor of
true first availability across all source mirrors.

`prefetch_started`, `prefetch_ready`, `inputs_ready`, `render_finished` and
`image_published` separate discovery, preparation, plotting/encoding and publication.
`ready_to_publish_seconds` starts at our successful input observation;
`source_last_modified_age_seconds` is only a selected-source timestamp proxy, not
proof of first availability. `observed_ready_over_15m` flags observed-ready delays
over 900 seconds. Check source history separately before claiming the target was
met. Runtime summaries include peak process-tree RSS and minimum free memory on
Linux and Windows.
`heavy_workers` replaces the former CMC-specific counter. It is the current
admission estimate, not extra processes; workers using at least 1 GiB RSS count
as `heavy_active_workers`. `worker_memory`, `worker_memory_paused/resumed` and
`memory_pause_count/seconds` record per-task peaks and recoverable scheduling
pauses separately on each compute node. Only that node's memory gates its pool.

`--refresh-seconds` defaults to 60 and `--publish-interval-seconds` to 15. Live
pending-source retries use 60 seconds, subject to worker capacity. The matching
Worker JavaScript and chart Cron Trigger must both be deployed for the one-minute
wake-up policy. `Test public chart pipeline` runs offline regressions
on Linux when code changes; it has no Drive secrets and changes no operational data.
