# `slurm_job_core_usage_total` semantics & the SMT caveat

Notes for anyone consuming the CPU metric (e.g. building an efficiency dashboard).

## What it emits

`slurm_job_core_usage_total{user,account,slurmjobid,core="N"}` is a **counter, one
series per CPU id in the job's cgroup cpuset** (`cpuset.cpus.effective`), value =
per-core CPU time in **nanoseconds**.

- cgroup v1: read directly from `cpuacct.usage_percpu` (already per-core, ns).
- cgroup v2: there is no per-core breakdown, so the collector reads `cpu.stat`
  `usage_usec`, converts to ns (`× 1000`), and divides evenly across the cpuset. The
  **sum** across a job's cores is the true total CPU time; individual per-core values
  are synthetic. (eBPF could provide real per-core in the future.)

## Consumer caveat: logical CPUs vs cores vs `NumCPUs`

On an SMT node where Slurm allocates whole cores, a job that requested **1 CPU** has
**both hyperthreads** of a physical core in its cpuset. Therefore:

- `count(slurm_job_core_usage_total)` counts **logical CPUs (hardware threads)**, which
  can be **2× the Slurm `NumCPUs`** the user actually requested/was billed for.
  (`scontrol show job -d` shows the split: `NumCPUs=1` vs `CPU_IDs=0-1`.)
- A **single-threaded** process only keeps ~1 of those threads busy. So a naive
  "used ÷ allocated-series" efficiency reads **~50%** for a perfectly-sized 1-CPU
  serial job.

When building an efficiency view, normalise against **billed CPUs**, not the thread
count. This exporter emits both:

- `slurm_job_core_usage{...,core="N"}` — one series per **cpuset thread** (above).
  `count()` of it is the logical-CPU count, up to 2× `NumCPUs`.
- `slurm_job_cpus_alloc{user,account,slurmjobid}` — Slurm **`NumCPUs`** (billed CPUs)
  per node (see next section).

Utilisation = `busy_threads / slurm_job_cpus_alloc` reads ~100% for a fully-used serial
job **and** for a `--hint=nomultithread` MPI job (whose cpuset is 2× its billed CPUs),
instead of the ~50% floor you get dividing by the thread count. The Duke CS/OIT dashboards
were re-based onto `slurm_job_cpus_alloc` for exactly this reason, with a fallback to the
thread count on nodes still running an exporter that predates the metric
(`sum by(...)(slurm_job_cpus_alloc) or count by(...)(slurm_job_core_usage_total)`).

The second hyperthread is still a *real, usable* resource (worth ~1×–2× depending on the
workload's instruction-level parallelism — latency-bound code approaches 2×,
throughput/memory-bound code ~1×), so a job legitimately using both siblings can read
slightly above 100%; that is a bonus, not over-subscription.

## `slurm_job_cpus_alloc`: billed CPUs (the right denominator)

`slurm_job_cpus_alloc{user,account,slurmjobid}` = Slurm **`NumCPUs`** for the job on this
node — the CPUs the user requested and is billed for.

Why a separate metric: the cgroup only exposes the **cpuset** (hardware threads). On SMT
nodes that hand out whole cores (e.g. `TaskPlugin=...,task/affinity`), a job's cpuset holds
**both** hyperthreads of every allocated core — up to 2× `NumCPUs`. `NumCPUs` is only
knowable from Slurm, so the collector shells out to `scontrol show job <id>`, parses
`NumCPUs`/`NumNodes`, and emits `NumCPUs / NumNodes` (exact for homogeneous jobs).

The value is fixed for a job's lifetime, so it is **cached per jobid** — one `scontrol`
call per new job, not per scrape — and the cache is pruned each scrape to jobs still on the
node. (`SLURM_CPUS_ON_NODE` would have been free but is **not** present in
`/proc/<pid>/environ` — Slurm sets it after exec — so unlike `SLURM_JOB_ACCOUNT` it can't
be read from a job process; verified on DCC.)

## rate() gotcha for the counter

`rate(slurm_job_core_usage_total[$window])` normalises the increase over the **full**
window, so a series younger than `$window` (recent job, or newly-scraped node) reads
far below its true rate. For a "current utilisation" panel over a long window, use a
short rate averaged over the window instead:
`avg_over_time(rate(slurm_job_core_usage_total[5m])[$window:])`.
