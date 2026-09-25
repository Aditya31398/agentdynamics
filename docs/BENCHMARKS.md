# Benchmarks

How the engine's cost grows with the size of the store, measured by [`bench/bench.py`](../bench/bench.py).
Reproduce with:

```bash
python bench/bench.py --sizes 1000,10000              # both ingest paths
python bench/bench.py --sizes 100000 --sources otlp
python bench/bench.py --check                         # the CI gate
```

Three numbers per size:

- **ingest**: pushing N traced requests in. The SDK path writes one JSON file per run; the span path
  (OTLP here, and the same code serves LangSmith, Langfuse and log ingestion) upserts into SQLite.
- **full refresh**: a cold rebuild of every derived table from the sources, as after an upgrade.
- **incremental refresh**: the steady state. 1% more traffic arrives into a store already holding N
  tasks, and the console catches up. The analyzer runs this continuously, so it is the number that
  decides how far behind the console runs.

## Results

Python 3.12, Windows 11, one laptop CPU. Each request: a prompt, 2–3 model calls and 3–5 tool calls.

| path | tasks | ingest | full refresh | incremental (+1%) | on disk |
|---|---:|---:|---:|---:|---:|
| span (OTLP) | 1,000 | 0.4 s | 0.6 s | 0.10 s | 7 MB |
| span (OTLP) | 10,000 | 4.2 s | 5.5 s | 1.1 s | 72 MB |
| span (OTLP) | 100,000 | 39 s | 60 s | **13.5 s** | 715 MB |
| SDK | 1,000 | 1.2 s | 10 s (9.5 s opening files) | 0.31 s | 3 MB |
| SDK | 10,000 | 12 s | 98 s (94 s opening files) | 3.0 s | 27 MB |

What these say:

- **Everything grows linearly now.** Roughly 10–12× the time for 10× the data, in every column.
- **The incremental refresh is the problem.** At 100k tasks, absorbing 1,000 new ones takes 13.5 s,
  almost all of it re-finalizing and rewriting the other 99,000. The console runs that far behind, and
  the analyzer is never idle. Extrapolated, 1M tasks is about 2 minutes per update. This is
  [weak area 1](../CLAUDE.md) with a number on it, and what [#5](https://github.com/Aditya31398/agentdynamics/issues/5)
  (incremental finalize) is for.
- **On this machine the SDK path is dominated by the OS, not the engine.** Opening each run file cost
  about 9 ms, most likely real-time antivirus scanning newly written files. The CI runner confirms it
  (Python 3.12, Linux, GitHub's `ubuntu-latest`), with the same code:

  | 1,000 tasks | SDK full refresh | of which opening files | span full refresh | incremental (SDK / span) |
  |---|---:|---:|---:|---:|
  | Windows laptop, antivirus on | 10.05 s | 9.5 s | 0.60 s | 0.31 s / 0.10 s |
  | Linux CI runner | 0.37 s | 0.09 s | 0.31 s | 0.07 s / 0.06 s |

  File opens were about 100× cheaper on Linux, and there the two ingest paths cost about the same. The
  benchmark reports file-scan time separately so the OS isn't mistaken for the engine.

## The quadratic this found

The first run never finished at 10,000 tasks. A profile showed nearly all the time in one indexed
lookup: fetching a trace's spans cost 6 ms each. `spans_raw` has primary key `(source, span_id)` and an
index on `(source, trace_id)`, but with no `ANALYZE` statistics — which is every fresh install — SQLite
answered `WHERE source=? AND trace_id=?` by walking the primary key on `source` alone, visiting every span
of that source once per trace. A full refresh was O(n²):

| span path, full refresh | before | after |
|---|---:|---:|
| 1,000 traces | 6.8 s | 0.6 s |
| 4,000 traces | 81.5 s | 2.3 s |
| 10,000 traces | didn't finish in 10 min | 5.5 s |

The "what changed since the last refresh" query had the same trap: a full table scan on every incremental
refresh instead of a search on the `updated` index. Both queries now name their index (`INDEXED BY`), so
the plan no longer depends on statistics. `tests/test_core.py` checks both plans on every push.

## The CI gate

Absolute times move a lot between machines. Here, opening files alone differed by roughly two orders of
magnitude from what a Linux runner would show. So the `scaling` CI job doesn't compare against stored
numbers. It compares the engine with itself: time at 4,000 tasks divided by time at 1,000, both measured
in one process on one runner, where machine speed cancels out.

| | growth, 1k → 4k | limit |
|---|---:|---:|
| linear | 4× | |
| today, span path, full refresh | 4.0× | 6× |
| today, span path, incremental | 4.9× | 6× |
| the quadratic above | 14.9× | 6× — fails |

The gate catches cost that grows faster than the data, not a constant-factor slowdown. That trade is
deliberate: a flat 20% slower is annoying, and quadratic is an outage. When #5 makes the incremental
refresh independent of store size, its limit should come down to hold it there.
