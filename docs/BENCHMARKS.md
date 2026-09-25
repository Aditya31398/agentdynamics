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
"+1%" absorbs 1% more traffic; "+100" absorbs a fixed 100 tasks, so it shows cost against store size.

| path | tasks | ingest | full refresh | +1% (re-scored) | +100 (re-scored) | on disk |
|---|---:|---:|---:|---:|---:|---:|
| span (OTLP) | 1,000 | 0.5 s | 0.6 s | 0.02 s (10) | 0.23 s (1,110) | 8 MB |
| span (OTLP) | 10,000 | 4.6 s | 6.1 s | 0.40 s (100) | 0.33 s (100) | 72 MB |
| span (OTLP) | 100,000 | 41 s | 66 s | **4.2 s** (1,000) | **2.9 s** (100) | 716 MB |
| SDK | 1,000 | 1.2 s | 10 s (10.0 s opening files) | 0.18 s (10) | 1.3 s (1,110) | 3 MB |
| SDK | 10,000 | 12 s | 101 s (95 s opening files) | 2.0 s (100) | 1.9 s (100) | 28 MB |

At 1,000 tasks, 100 new ones are a 10% jump, past the baseline's 5% step, so the whole type is
re-scored once; that is the design, and why that row re-scores 1,110.

What these say:

- **Everything grows linearly.** Roughly 10× the time for 10× the data in the rebuild columns.
- **Only new traffic is scored and written.** The re-scored counts are exactly the new tasks at 10k and
  100k. What still grows with the store is a light pass over every task on each refresh — outcomes,
  conversation threads, grades, baselines, insights — at about 30 µs a task: 2.9 s to absorb 100 tasks
  at 100,000. Making that pass incremental too needs streaming quantiles and incremental aggregates.
- **On this machine the SDK path is dominated by the OS, not the engine.** Opening each run file cost
  about 9 ms, most likely real-time antivirus scanning newly written files. The CI runner confirms it
  (Python 3.12, Linux, GitHub's `ubuntu-latest`), with the same code:

  | 1,000 tasks | SDK full refresh | of which opening files | span full refresh | incremental (SDK / span) |
  |---|---:|---:|---:|---:|
  | Windows laptop, antivirus on | 10.05 s | 9.5 s | 0.60 s | 0.31 s / 0.10 s |
  | Linux CI runner | 0.37 s | 0.09 s | 0.31 s | 0.07 s / 0.06 s |

  File opens were about 100× cheaper on Linux, and there the two ingest paths cost about the same. The
  benchmark reports file-scan time separately so the OS isn't mistaken for the engine.

## Incremental refresh (#5)

Before #5, every refresh re-scored every task and rewrote the whole `tasks` table, so absorbing new
traffic cost as much as the store was large:

| span path, +1% | before | after | re-scored after |
|---|---:|---:|---:|
| 10,000 tasks | 1.1 s | 0.40 s | 100 |
| 20,000 tasks | 5.65 s | 0.66 s | 200 |
| 100,000 tasks | 13.5 s | 4.2 s | 1,000 |

Two things made that possible. A task is re-scored only when something it depends on changed — its
own run, a field the cross-task pass writes (outcome, grade, thread successor, subagent cost), or the
two baseline figures scoring reads — and only changed rows are written. And baselines no longer move
with every arrival: each type's is computed from its earliest M tasks, where M advances in 5% steps.
The first version rounded baselines to three significant figures instead; at 100,000 tasks one type's
median crossed a rounding boundary and 21,000 tasks were re-scored for 1,000 new ones. Rounding can
also oscillate on a boundary. The stepped sample can't, and costs about 20 re-scores per new task
over time.

`tests/test_incremental.py` holds it to exactness: an incrementally maintained store must equal a full
rebuild, every column of every row, over randomized histories. That test also found a bug that predated
#5 — a conversation's earlier task kept a follow-up that had since been deleted — see the CHANGELOG.

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
| span path, full refresh | 3.9× | 6× |
| span path, +100 tasks | 0.7× | 6× |
| the quadratic above | 14.9× | 6× — fails |

The gate catches cost that grows faster than the data, not a constant-factor slowdown. That trade is
deliberate: a flat 20% slower is annoying, and quadratic is an outage. The +100 figure is flat at these
sizes because fixed overhead hides the per-task pass; it is linear at scale (see above). Whether scoring
is limited to new traffic isn't left to timing at all: `tests/test_incremental.py` asserts the count.
