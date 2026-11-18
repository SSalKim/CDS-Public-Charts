# Production Latency Policy

The `20261117-v1` policy keeps the chart catalog, styling and scientific calculations unchanged.

- Production downloads 300 hPa in November-April and 200 hPa in May-October. General-purpose collection still supports both.
- Every source transfer has one elapsed-time budget across retries. Base preparation has a 180-second budget; each winter diagnostic phase has 300 seconds. Blocking socket reads also have short timeouts.
- Two consecutive ICON feed transport failures demote that feed for five minutes in the renderer process. Both exact-cycle feeds remain available, including recovery probes after cooldown. Data and range validation are never bypassed.
- Validated partial source downloads remain available during retries in the current batch. Completed regional arrays survive batch changes through a SHA-256-checked cache with identity validation, a 24-hour TTL and a 512 MiB post-pruning cap per runner. These derived caches use Actions cache storage, not either chart Drive. Raw GRIB scratch directories are still removed after the batch.
- New local preparations stop 90 seconds before the batch deadline. Ready local work can start until 90 seconds after the deadline. In-flight work remains bounded by source and diagnostic budgets.
- Asia surface charts and Korean current-interval winter charts publish before total-accumulation calculations. Snow depth still sums interval-specific snow-ratio diagnostics, never a single final ratio applied to total rain.
- Shared cumulative locks cover checkpoint commits only, not network transfers. Independent interval snow diagnostics are cached separately. Prefix sums retain the original ordering and formulas.
- A helper lease initially lasts ten minutes. New verified images or increasing progress counters can extend it by six minutes from the latest progress, capped at twenty minutes and the coordinator session limit. Host heartbeats alone cannot extend a lease. Revoked work stops cooperatively at transfer, diagnostic and image-publication boundaries.
- `source_received` records the locally observed successful receipt, source host, forecast identity, byte count and transfer duration. It is not proof of the supplier's first publication time. `prefetch_ready` reports whether regional inputs were reused. Window and total diagnostics have separate readiness events.
- Helper transport errors include a sanitized operation and HTTP status when available; credentials, signed URLs and response bodies are not logged.

Deployment does not invalidate already published charts. Ongoing jobs finish on their checked-out revision; subsequent jobs use the new revision.

## Ready-Work Rollover And Transport Update

The `20261118-rollover-v3` update removes the separate runner-routing job and
overlaps verified reads of the two independent Drive accounts. Pinned Python
environments and Node artifact modules can be reused on a persistent runner;
their signatures include the interpreter/runtime identity and dependency pins.
GitHub-hosted machines remain ephemeral and still install their isolated runtime.

Normal automatic production opts into one ready-operational-backlog extension
near its preparation cutoff. A normal 20-minute dispatch can continue to at most
30 minutes; explicit manual budgets and chosen-cycle jobs are not extended.
The helper admission deadline is updated with the coordinator; existing lease
hard limits, the helper's 28-minute process limit and workflow timeouts still apply.

ICON uses its exact-cycle static compressed feed first, retaining v1 fallback.
The per-request limit is 15 seconds, including transfers, with 5/8-second socket
limits and no repeat on the same URL in that call. A five-minute failure marker
is shared among processes on that runner; success on another variable does not
erase it. Already verified fields remain cached. One failed helper preparation
can retry on the same lease; another failure or lost heartbeat releases it through
the existing recovery path. Local failed preparations have a bounded three-minute
local preference. This is not a cross-runner field-cache sharing mechanism.

The 90-second cleanup submission window remains in place while its backlog is
large; this update does not pretend that deletion or hosted-runner startup is free.
Fewer unnecessary rollovers reduce how often those costs interrupt new inputs.
