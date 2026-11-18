# Throughput pipeline, 2026-11-18

The GitHub coordinator remains the sole owner of Drive state, manifests,
retention and deletion. A missing or stopped helper returns work to GitHub.
No grid resolution, scientific formula, product or image dimensions change.

## Compute

- Each node has at most four render processes and two preparation processes.
  Both pools share one physical-memory budget; these are caps, not guaranteed
  concurrent allocations. Completed preparations wait on disk, with at most
  one renderer-pool-sized queue of prepared leads, with up to two extra disk
  slots reserved for newly available operational work behind prepared refreshes.
- First-publication and incomplete operational sets precede already-public
  image refreshes. Within each class, newest ready cycles precede older ones;
  equal-priority models retain fair rotation. Missing inputs never block a
  ready candidate. The discovery monitor, both compute nodes and the prepared
  render queue use the same publication-aware ordering. In-flight calculations
  are not interrupted just because higher-priority inputs arrive.
- Per-phase, per-workload measured peaks reserve 125% headroom, with a 1 GiB
  minimum per new task. Linux retains 1 GiB and Windows retains 1.5 GiB.
  There are no fixed CMC/ICON concurrency caps. Running tasks are not killed
  just because admission capacity drops.
- On a capacity drop, an unstarted helper lease becomes eligible for return
  after 45 seconds. It remains reserved until the helper acknowledges release.
  Lease selection and release acknowledgement share a lock. A task that started
  before the revocation arrived stays on its current node.

## Inputs

- ICON persists each validated, remapped native variable separately, including
  source timestamps. Keys include initialization, lead, parameter, pressure
  level, domain, spacing and remapping version. SHA-256 and coordinate checks
  are mandatory before reuse. A partially available set never marks a lead ready.
- These regional fields and completed input bundles share the existing 512 MiB
  cache budget and 24-hour TTL. Partial fields can use at most 256 MiB; fields
  already represented in a completed bundle are evicted first.
- Actions cache format is `public-charts-input-v2`; both nodes restore and save
  prepared bundles and partial fields. A first v2 run starts with a cold cache.

## Cleanup

- Exact published-manifest, namespace, account, image ownership, grace-period
  and conditional ETag deletion checks remain intact.
- Up to three independent IO clients delete in parallel. Stop submitting after
  90 seconds or 900 files; already-started network requests are drained, so this
  is not a strict 90-second wall-clock limit.
- Preserve original deletion deadlines for unattempted/failed files. Save the
  retry state before surfacing a deletion error. Track `cleanup_finished` totals;
  persistent growth of eligible backlog requires more maintenance capacity,
  not silently skipping retention safeguards.

## Operational checks

Monitor `prefetch_started`, `prefetch_ready` (including `field_cache_hits`),
`worker_memory`, `helper_unstarted_release_requested`,
`helper_unstarted_reclaimed`, and `cleanup_finished`.

Compare source discovery to assignment, input readiness to first publication,
full-lead publication, memory pauses, duplicate transfers and eligible deletion
backlog. Evaluate the 15-minute publication objective from those observations;
passing tests alone is not evidence of a measured production speedup.
