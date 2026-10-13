# Interface responsiveness

The browser loads pipeline summaries before diagnostics, Git status, and result
freshness checks. Run and tool forms fetch their full metadata when opened.
Looking up one pipeline inspects only that checkout; it does not rediscover the
whole workspace. Source scans skip excluded directories before entering them.

Display metadata and QC summaries have a 15-second browser cache. Concurrent
requests for the same display data share a request. Successful POST requests and
observed session-status changes invalidate that cache. **Refresh status** fetches
a fresh overview. Launch plans, scientific gates, and file authorization never
use the display cache.

Results use a bounded, in-memory inventory with a 15-second lifetime. Paging and
filtering reuse that inventory, showing at most 60 files per page. Filters search
the entire scanned inventory, not just the current page. Files appear independently
of QC parsing. **Refresh** rescans outputs and reloads QC immediately. Changes to
sources, attachment changes, and local process exits invalidate result inventories;
nested changes made outside OmicsANG are picked up on expiry or Refresh. Result
sources are reauthorized on every page request and files are authorized again when
opened. Cached inventories are never used for provenance or execution.

Result and QC scans classify files by name before touching them, so large
non-displayed outputs (BAM, FASTQ, bigWig, ...) cost one directory entry each
rather than a path resolution and `stat`. Only displayable files, and only the
file names a QC parser reads, are verified as regular files inside their source.

An inventory is limited to 4,000 recognized outputs and a two-second scan, with
additional per-source bounds. A partial inventory is labelled explicitly; attach a
narrower output directory when a large result tree exceeds the bounds. Diagnostic
scans also report when their file, entry, or time limits are reached. These are
browsing limits, not scientific validation results.

Code diagnostics skip per-line checks a file cannot trigger (for example, a file
without any shell-risk command), and detect local CPU capacity once per scan.

Session polling waits until its preceding requests finish. Visible tabs poll every
2.5 seconds while sessions or queued runs are active and every 15 seconds when
idle. Hidden tabs schedule no polls; returning to a tab refreshes immediately.
An already-running request is allowed to finish when the tab becomes hidden.
Terminals whose sessions have ended are released (xterm buffer and WebSocket)
after the six most recently viewed; the focused terminal and live sessions are
never released. Reopening a released session re-attaches to it.

For regression checks, run:

```bash
python -m unittest discover -s tests -p test_ui_performance.py
node --check benchtop/web/ui-performance.js
node --check benchtop/web/app.js
```

These use disposable fixtures and mocked browser clocks. To assess the deployed
workspace, compare cold page load, repeated pipeline switches, first Results load,
and subsequent result pages in the browser's Network and Performance panels.
Report cold and warm timings separately; synthetic microbenchmarks are not
end-to-end browser latency measurements.

## Automatic local allocation and monitoring

New local runs automatically share the detected CPU and memory budget. Detection
accounts for CPU affinity, Linux cgroup limits, and available memory. About 10%
of CPU capacity and RAM is held back for the desktop (with up to a 2 GB minimum
RAM reserve). Saved user limits can reduce this budget. The default is two
concurrent pipelines; change **Parallel pipelines** in the queue controls.

**Auto allocate** divides the budget between those slots and checks available
memory again before the launch plan is reviewed. The reviewed CPU and memory
allocation is recorded in the immutable run plan and remains fixed while queued
or running. Disable Auto allocate to set a smaller or larger reservation. Queued
runs start automatically when their CPU and RAM reservations fit, a slot is free,
and sufficient system memory is available. The controller checks every five
seconds and after a run exits. Two runs of the same pipeline checkout stay
serialized to avoid conflicting output writes. Older runs without memory
reservations run alone.

The system monitor shows sampled system CPU load, available/used RAM, reserved
pipeline capacity, and CPU cores/RSS memory used by each active process tree.
The first CPU sample shows a sampling message. RSS includes shared pages in each
process, so summing it can overestimate unique physical memory. Monitoring is
read-only and uses Linux `/proc`; actual usage and reservations are shown
separately. Visible active tabs refresh approximately every 2.5 seconds.

CPU scheduling uses reserved cores rather than instantaneous CPU percentages.
Memory admission checks both reservations and current system availability.
Allocations are passed to Snakemake as `--cores` and `--resources mem_mb=...`.
These are scheduler budgets, not operating-system memory limits: workflows must
declare accurate rule resources, and untracked external programs can still
consume capacity after launch. See [Snakemake resources](https://snakemake.readthedocs.io/en/latest/snakefiles/rules.html#resources)
and [Linux cgroup v2 limits](https://cdn.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).
Slurm continues to use its own submission resource settings.

Resource regression tests use tiny disposable fixtures and simulated jobs:

```bash
python -m pytest tests/test_local_resources.py tests/test_scheduler_durability.py
```
