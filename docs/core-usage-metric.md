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

When building an efficiency view, either:

1. measure utilisation against Slurm `NumCPUs` (physical/billed CPUs), **or**
2. label the metric as **thread / logical-CPU utilisation** and treat ~50% as the
   expected floor for serial jobs — since the second hyperthread is a *real, usable*
   resource (worth ~1×–2× depending on the workload's instruction-level parallelism;
   latency-bound code approaches 2×, throughput/memory-bound code ~1×).

The Duke CS/OIT dashboards take approach (2): they count threads and relabel the panels
as "thread utilisation" rather than dividing the hyperthread away.

## rate() gotcha for the counter

`rate(slurm_job_core_usage_total[$window])` normalises the increase over the **full**
window, so a series younger than `$window` (recent job, or newly-scraped node) reads
far below its true rate. For a "current utilisation" panel over a long window, use a
short rate averaged over the window instead:
`avg_over_time(rate(slurm_job_core_usage_total[5m])[$window:])`.
