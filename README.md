# CDS Public Charts

Dedicated production repository for AIFS, GFS, AIGFS and CMC GDPS charts.
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
- Production is serialized in one concurrency group. Never enable the old CDS
  producer and this producer together: both use the same Drive state.
- The renderer and model schedules are unchanged. Completed products are reused;
  no existing Drive images need to be copied or regenerated for the migration.

## Cloudflare Setup

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
    "max_leads": 2,
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

`auto` uses an online, idle Linux runner labeled `cds-vtg` registered with **this
repository**; otherwise it uses `ubuntu-24.04`. The existing CDS laptop registration
does not automatically become available here. Until a laptop registration is
explicitly added, the new repository uses GitHub-hosted runners. Do not start two
runner services on one laptop without coordinating CPU use and VTG priority.

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
