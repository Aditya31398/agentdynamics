"""Ingest and refresh benchmarks: how the engine's cost grows with the size of the store.

    python bench/bench.py                       # 1k and 10k tasks, both ingest paths
    python bench/bench.py --sizes 1000,10000,100000
    python bench/bench.py --check               # the CI gate: how cost *grows*, at 1k and 4k

CLAUDE.md used to say the engine was "fine to about 100k tasks". Nobody had measured that. This does:

  ingest        pushing N runs in: the SDK path writes a JSON file per run; the span path (OTLP) upserts
                into SQLite
  full refresh  a cold rebuild of every derived table from the sources
  incremental   the steady state: 1% more traffic arrives into a store that already holds N tasks, and
                the console catches up. This is the number weak area 1 is about -- if it grows with N, a
                busy deployment spends its time re-analysing history

Times are wall-clock seconds on the machine running it, and absolute numbers move a lot between
machines: on a Windows box with real-time antivirus, opening the SDK path's run files cost 9 ms each and
was 95% of a full rebuild. So the regression gate doesn't compare against a stored baseline. It compares
the engine with itself: the time at 4x the size divided by the time at 1x, both measured in one process
on one machine, where machine speed cancels out. Linear growth is ~4x. The O(n^2) trace lookup this
benchmark found (see store.trace_spans) was ~16x. The gate catches growth, not a constant-factor slowdown;
that trade is deliberate, since a flat 20% slower is annoying and quadratic is an outage.
"""
import argparse
import json
import os
import platform
import random
import shutil
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agentdynamics.engine import Engine  # noqa: E402

# check: size -> 4x size. Linear is 4.0; the limit leaves room for noise and still fails quadratic (~16).
# incremental_refresh is linear today (weak area 1, issue #5); when #5 lands it should be ~1, and this
# limit should come down to hold it there.
CHECK_SIZES = (1000, 4000)
GROWTH_LIMIT = {"full_refresh": 6.0, "incremental_refresh": 6.0}
WORKFLOWS = ["support", "billing", "research", "triage", "refunds"]
MODELS = ["claude-sonnet-5", "claude-haiku-4-5", "claude-opus-5"]
TOOLS = ["kb.search", "orders.lookup", "payments.status", "email.send", "fs.read"]


def calibrate():
    """A fixed CPU workload in the same interpreter: json round trips, dict churn and sorting --
    roughly the mix the engine does. Returns seconds; best of three to shed scheduler noise."""
    best = None
    for _ in range(3):
        t0 = time.perf_counter()
        rng = random.Random(1)
        for i in range(4000):
            d = {"id": i, "steps": [{"k": rng.random(), "n": str(j)} for j in range(12)]}
            s = json.dumps(d)
            d2 = json.loads(s)
            sorted(d2["steps"], key=lambda x: x["k"])
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return best


def sdk_run(i, rng, t0):
    """One traced request in the generic SDK format: a prompt, 2-3 model calls, 3-5 tool calls."""
    ts = t0 + i * 7.0
    wf = WORKFLOWS[i % len(WORKFLOWS)]
    steps = [{"kind": "prompt", "ts": ts, "text": f"{wf} request {i}"},
             {"kind": "span", "ts": ts, "end_ts": ts + 6, "span_kind": "node", "name": "plan", "node": "plan"}]
    t = ts
    for j in range(rng.randint(2, 3)):
        steps.append({"kind": "llm", "ts": t, "end_ts": t + 1.2, "model": MODELS[(i + j) % 3],
                      "input_tokens": rng.randint(400, 4000), "output_tokens": rng.randint(50, 600),
                      "cache_read": rng.choice([0, 0, 1200]), "stop_reason": "tool_use" if j else "end_turn"})
        t += 1.3
        for _ in range(rng.randint(1, 2)):
            steps.append({"kind": "tool", "ts": t, "end_ts": t + 0.3, "name": rng.choice(TOOLS),
                          "input": {"q": f"item-{rng.randint(1, 50)}"}, "is_error": rng.random() < 0.05})
            t += 0.4
    return {"id": f"bench-{i}", "project": "bench", "workflow": wf, "environment": "production",
            "status": "error" if rng.random() < 0.08 else "ok", "complete": True, "steps": steps}


def otlp_batch(first, n, rng, t0):
    """n traces in OTLP JSON with GenAI semantic conventions: an agent span, model calls, tool calls."""
    spans = []
    for i in range(first, first + n):
        tid = f"{i:032x}"
        ts = t0 + i * 7.0
        root = f"{i:015x}0"
        spans.append(_span(tid, root, None, "invoke_agent", ts, ts + 6,
                           {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": WORKFLOWS[i % 5]}))
        for j in range(rng.randint(2, 3)):
            spans.append(_span(tid, f"{i:015x}{j + 1}", root, "chat", ts + j * 1.5, ts + j * 1.5 + 1.2,
                               {"gen_ai.operation.name": "chat", "gen_ai.request.model": MODELS[(i + j) % 3],
                                "gen_ai.usage.input_tokens": rng.randint(400, 4000),
                                "gen_ai.usage.output_tokens": rng.randint(50, 600)}))
        spans.append(_span(tid, f"{i:015x}9", root, "execute_tool", ts + 4, ts + 4.3,
                           {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": rng.choice(TOOLS)}))
    return json.dumps({"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "bench"}}]},
        "scopeSpans": [{"spans": spans}]}]}).encode()


def _span(tid, sid, parent, name, s, e, attrs):
    a = [{"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}}
         for k, v in attrs.items()]
    return {"traceId": tid, "spanId": sid, "parentSpanId": parent or "", "name": name, "status": {},
            "startTimeUnixNano": str(int(s * 1e9)), "endTimeUnixNano": str(int(e * 1e9)), "attributes": a}


def ingest(eng, source, first, n, rng, t0):
    if source == "sdk":
        for i in range(first, first + n):
            eng.ingest(sdk_run(i, rng, t0))
    else:
        step = 500
        for i in range(first, first + n, step):
            eng.ingest_otlp(otlp_batch(i, min(step, first + n - i), rng, t0), "application/json")


def bench_one(source, n):
    tmp = tempfile.mkdtemp(prefix=f"adbench-{source}-")
    rng = random.Random(42)
    t0 = time.time() - 20 * 86400          # inside the default 30-day window, spread over ~20 days at 10k
    try:
        eng = Engine(os.path.join(tmp, "data"), None)
        start = time.perf_counter()
        ingest(eng, source, 0, n, rng, t0)
        ingest_s = time.perf_counter() - start
        # Span reassembly re-reads anything updated in the 2 s before the previous refresh (a guard
        # against spans committed mid-refresh). Refreshing straight after the bulk load would put the
        # whole load inside that window, and the incremental number would measure the burst, not the
        # store. Live traffic arrives spread out; wait the window out to measure the steady state.
        time.sleep(2.1)

        # Opening the SDK path's run files is disk and OS work, not analysis. On a machine with
        # real-time antivirus it can dominate (9 ms per open, measured on Windows), so report it apart.
        scan = {"s": 0.0}
        orig_scan = eng._scan_files

        def timed_scan():
            t = time.perf_counter()
            try:
                return orig_scan()
            finally:
                scan["s"] += time.perf_counter() - t
        eng._scan_files = timed_scan
        start = time.perf_counter()
        eng.refresh(force=True)
        full_s = time.perf_counter() - start
        full_scan_s = scan["s"]
        tasks = eng.con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

        extra = max(1, n // 100)
        ingest(eng, source, n, extra, rng, t0)
        start = time.perf_counter()
        eng.refresh()
        inc_s = time.perf_counter() - start

        eng.con.close()
        db = os.path.join(tmp, "data", "agentdynamics.db")
        size = sum(os.path.getsize(db + suf) for suf in ("", "-wal") if os.path.exists(db + suf))
        return {"source": source, "size": n, "tasks": tasks,
                "ingest": round(ingest_s, 3), "ingest_per_s": round(n / ingest_s) if ingest_s else None,
                "full_refresh": round(full_s, 3), "full_file_scan": round(full_scan_s, 3),
                "incremental_refresh": round(inc_s, 3),
                "incremental_added": extra, "db_mb": round(size / 1e6, 1)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sizes", default="1000,10000")
    ap.add_argument("--sources", default="sdk,otlp")
    ap.add_argument("--check", action="store_true",
                    help=f"fail if cost grows faster than linearly between {CHECK_SIZES[0]} and {CHECK_SIZES[1]} tasks")
    ap.add_argument("--json", help="also write the results here")
    a = ap.parse_args()
    sizes = list(CHECK_SIZES) if a.check else [int(x) for x in a.sizes.split(",")]

    cal = calibrate()
    print(f"python {platform.python_version()} on {platform.system()} {platform.machine()}; "
          f"calibration {cal * 1000:.0f} ms (a fixed CPU workload, for comparing machines by eye)")
    print(f"{'source':<6} {'tasks':>8} {'ingest s':>9} {'runs/s':>8} {'full s':>8} {'of which files':>15} "
          f"{'incr s':>8} {'db MB':>7}")
    results = []
    for n in sizes:
        for src in a.sources.split(","):
            r = bench_one(src, n)
            results.append(r)
            print(f"{src:<6} {r['tasks']:>8} {r['ingest']:>9.2f} {r['ingest_per_s']:>8} "
                  f"{r['full_refresh']:>8.2f} {r['full_file_scan']:>15.2f} {r['incremental_refresh']:>8.3f} "
                  f"{r['db_mb']:>7.1f}", flush=True)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"python": platform.python_version(), "platform": f"{platform.system()} {platform.machine()}",
                       "calibration_s": round(cal, 4), "results": results}, f, indent=2)
    return check(results) if a.check else 0


def check(results):
    """Growth between the two sizes, per source: time(4n) / time(n). Machine speed cancels out."""
    small, big = CHECK_SIZES
    by = {(r["source"], r["size"]): r for r in results}
    failed = []
    print(f"growth from {small} to {big} tasks ({big // small}x the data; linear is {big / small:.1f}x):")
    for src in sorted({r["source"] for r in results}):
        a, b = by.get((src, small)), by.get((src, big))
        if not (a and b):
            continue
        for k, limit in GROWTH_LIMIT.items():
            # the file-scan part of a full refresh is the OS, not the engine; judge the engine
            ta = a[k] - (a["full_file_scan"] if k == "full_refresh" else 0)
            tb = b[k] - (b["full_file_scan"] if k == "full_refresh" else 0)
            g = tb / ta if ta > 0 else 0.0
            ok = g <= limit
            print(f"  {src:<5} {k:<20} {g:5.1f}x  (limit {limit:.0f}x)  {'ok' if ok else 'TOO FAST-GROWING'}")
            if not ok:
                failed.append(f"{src}/{k}")
    if failed:
        print(f"FAIL: cost grows faster than linearly: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
