# Cloudflare scheduler

Use [worker.js](worker.js) as the complete replacement for the existing Worker.
This repository does not have Cloudflare deployment credentials; a GitHub push
does not deploy the Worker or change its Cron Triggers.

## Dashboard setup

1. Open the existing Worker, replace its JavaScript with `worker.js`, then deploy.
2. Keep its existing `GITHUB_TOKEN` secret. The token must have Actions read/write
   access to both `SSalKim/CDS` and `SSalKim/CDS-Public-Charts`. For a fine-grained
   token, add the new repository to its selected-repository list.
3. Under Settings / Trigger Events / Cron Triggers, keep only these two schedules:

| Target | Cron (UTC) | GitHub workflow |
| --- | --- | --- |
| Public charts | `3,13,23,33,43,53 * * * *` | `SSalKim/CDS-Public-Charts`, `public_charts.yml` |
| Existing VTG | `9,19,29,39,49,59 * * * *` | `SSalKim/CDS`, `vtg.yml` |

The former GDM 03-minute slot now runs public charts. Remove the former DMDW
05-minute trigger. This code ignores 05-minute events even if that old trigger
has not yet been removed. Existing GDM/DMDW repositories are not modified.
`wrangler.example.jsonc` shows the same schedules for CLI users; replace its
placeholder with the existing Worker's real name before using it.

Cloudflare cron uses UTC. Trigger changes can take up to 15 minutes to propagate.
See [Cloudflare Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/).

## Production behavior

- VTG keeps its original periodic dispatch behavior; the worker does not select
  a VTG cycle or alter VTG's own scheduling logic.
- Public charts receive `dry_run=false`, `runner_mode=auto`, `model=all`,
  `max_leads=2`, `force=false`. Cycle and hour are deliberately unspecified.
- Public charts are skipped if any run is in progress, queued, waiting, pending
  or requested. Each status is filtered server-side, avoiding history truncation.
- A failed or invalid status lookup prevents a dispatch. GET requests retry
  once on transport/server failures; dispatch POSTs are never retried because
  an ambiguous timeout may already have started a run.
- The GitHub concurrency group remains the final in-repository safeguard. Two
  simultaneous scheduler invocations can still race between lookup and dispatch;
  this is not a distributed lock or a cross-repository runner reservation.
- GitHub responses and tokens are not echoed to logs. HTTP 204 means accepted,
  not that chart production has completed.

## Optional manual HTTP dispatch

`GET /` is a read-only health page. Unlike the supplied previous Worker, a public
GET URL can no longer start an authenticated GitHub workflow.

Manual dispatch is disabled unless you add a separate `MANUAL_TRIGGER_TOKEN`
Worker secret. It is not the GitHub token. Scheduled execution does not need it.
Then send a POST with `Authorization: Bearer <MANUAL_TRIGGER_TOKEN>`:

```text
POST https://<existing-worker-host>/?target=charts
POST https://<existing-worker-host>/?target=vtg
POST https://<existing-worker-host>/?target=both
```

`cds` remains an alias for VTG; `all`/`both` now mean VTG plus public charts.
Old `gdm`, `gdm-check`, `dmdw` requests return HTTP 410 and never call GitHub.

## Offline tests

```text
node --test tests/test_worker.mjs
```

The tests use mocked HTTP only and do not dispatch real GitHub workflows.
