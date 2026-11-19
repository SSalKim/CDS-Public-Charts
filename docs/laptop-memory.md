# Laptop helper memory policy

The GitHub coordinator still owns publication and can produce all charts without
the laptop. Disk layout, cleanup and memory changes here apply to the dedicated
laptop helper. No Windows, WSL, swap, worker-count or VTG settings are required.

- The helper keeps prepared inputs, partial fields and checkpoints on local
  disk under the runner tool cache, separate from job scratch space. It tries
  remote restore only for empty caches and does not save them remotely on the
  laptop. Regional inputs retain the 24-hour / 512 MiB budget; checkpoints have
  an additional 2 GiB budget applied at pruning. A diagnostics revision change
  invalidates the local checkpoint tree. Actual payloads are validated before
  use, not reread just to check their storage budget.
- A later laptop job can clean up old cache-save Node processes only if the
  repository, executable, workspace, entry point and run attempt match and the
  GitHub API confirms the run is completed. PID file descriptors prevent
  signaling a reused PID. Active or unverifiable jobs are skipped.
- Ready candidates advertised by the coordinator are advisory. The coordinator
  can start them immediately. The helper requests a best-effort reclaim of up
  to 512 MiB of cold prepared or partial fields, at most once per 120 seconds,
  only if Windows RAM is the limiting resource. It does not delete the files
  or touch active cycles, checkpoints, maps, or grid weights. A hint does not
  grant capacity; the helper samples actual host and guest memory again.
- Admission still reserves 1.5 GiB in Windows and 1 GiB in WSL independently.
  With three eligible observations per phase/model/season/lead band, a
  conservative estimate can use the observed high-water RSS plus margin
  instead of the 1 GiB floor. These profiles are versioned and stay on the
  laptop. Below the normal 1 GiB headroom, only one specific lead can be
  offered, and its prepare and render estimates must both fit.
- A laptop lead blocked for 30 seconds due to memory before preparation or
  render goes back to GitHub without a source-unavailable backoff.

On first rollout, check `helper_runtime` for `laptop_learned_headroom`, compare
`laptop_cache_reclaim` before/after Windows values, and check useful helper
bundles, fallback timing, peak RAM, swap and the 15-minute publication rate.
Cache hints do not guarantee that WSL returns memory to Windows. Do not lower
the host reserve or broaden reclaim to all WSL caches based on hint counts.
The existing GitHub-only runner selection is the operational fallback.
