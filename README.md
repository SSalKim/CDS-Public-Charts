# CDS Public Charts

Dedicated production repository for AIFS, GFS, AIGFS, CMC GDPS and ECMWF IFS off-cycle charts.
The [CDS viewer](https://ssalkim.github.io/CDS/) and VTG remain in
[SSalKim/CDS](https://github.com/SSalKim/CDS).

- Images: existing, separate chart Google Drive account and folder.
- Public index: `data/public_charts_manifest.json`; refreshed without Pages builds.
- Scheduling: Cloudflare calls `public_charts.yml` every ten minutes.
  There is intentionally no GitHub-native cron in this repository.
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
  CMC keeps its memory-dependent rendering cap: two concurrent CMC leads
  on the laptop with about 6.4 GiB available, three at 8 GiB, or four at 12 GiB.
  Each lead has
  separate raw scratch files. Shared cumulative rain/snow checkpoints are protected
  by a per-cycle process lock; only prefix calculation is serialized, not rendering.
  Only the coordinator uploads to Drive, updates manifests, publishes Git commits,
  and plans cleanup. Completed images stream to the coordinator before the whole
  lead finishes. One shared queue fills three independent upload slots across leads.
  Verified URLs are coalesced into publications every 15 seconds (plus the final
  flush); the first image is published immediately. Aliases wait for their source
  image's verification, and staged files remain until all uploads drain.
- Rendering defaults to `auto`: up to four processes, limited by effective CPU
  and available memory (including WSL/container limits). Windows host headroom is
  also sampled: new scheduling stops below 1 GiB free on Windows or 512 MiB in Linux.
  Already running work is drained, not killed. Set
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
- The dedicated laptop label is `cds-public-charts`. Routing checks both this
  repository and the original CDS runner status. A busy VTG runner causes
  GitHub-hosted routing. On the shared laptop, chart production also holds a
  host lock. VTG routing waits up to three minutes for that shared laptop, then
  uses GitHub-hosted; it never interrupts an active chart batch.
  The original VTG automation also takes the host lock before rendering.
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
second safeguard, not a cross-repository lock. Offset the ten-minute Cloudflare
schedule from VTG, for example minute 03/13/23/33/43/53 UTC.

## Runner Routing

`auto` uses the online, idle `LAPTOP-WOOJIN-CHARTS` runner labeled
`cds-public-charts` in this repository, only when the original CDS VTG runner is
also idle. Otherwise it uses `ubuntu-24.04`. Both registrations share a physical
host lock. VTG waits up to three minutes for that laptop before falling back;
CHART falls back immediately when it is busy and does not yield an active render.

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
available, with 3-5 minutes preferred. This is not a proven SLA. Cloudflare still
starts jobs every ten minutes; source outages, cold cumulative prefixes, startup
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

`--refresh-seconds` defaults to 60 and `--publish-interval-seconds` to 15. Cloudflare
configuration is unchanged. `Test public chart pipeline` runs offline regressions
on Linux when code changes; it has no Drive secrets and changes no operational data.
