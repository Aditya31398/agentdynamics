"""Incremental refresh (#5) must give exactly what a full rebuild gives.

An incremental refresh re-scores and rewrites only the tasks whose inputs changed. That is only safe if
"inputs" is complete, and several of them cross task boundaries: a new trace in a conversation changes
the *previous* task's rework flag; a subagent run changes its parent's cost; a grade changes a task no
new data touched; the clock turns "in progress" into "unknown"; and every task is scored against its
type's baseline, which new traffic moves.

So each scenario here is a seeded random sequence of ingests, updates, deletions, grades and clock
advances, refreshed incrementally after every step. At checkpoints the store is compared, every column
of every task, event and baseline row, against a full rebuild from the same sources.
"""
import json
import os
import random
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentdynamics import analysis  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402

WORKFLOWS = ["support", "billing", "research"]
PROMPTS = ["Where is my refund?", "Update my address", "still not working", "Cancel the order",
           "no, that's wrong", "Thanks, that worked"]


class Scenario:
    def __init__(self, seed, tmp):
        self.rng = random.Random(seed)
        self.clock = 1_900_000_000.0            # fixed and injectable: outcomes depend on "now"
        self.eng = Engine(os.path.join(tmp, "data"), None)
        self.eng._clock = lambda: self.clock
        self.runs = {}                          # run id -> payload, for updates and subagents
        self.n = 0

    def run(self, rid=None, **over):
        rng = self.rng
        self.n += 1
        rid = rid or f"r{self.n}"
        ts = self.clock - rng.choice([30, 200, 900, 5000])       # some inside the 600 s "in progress" window
        steps = [{"kind": "prompt", "ts": ts, "text": rng.choice(PROMPTS)}]
        t = ts
        for _ in range(rng.randint(1, 3)):
            steps.append({"kind": "llm", "ts": t, "end_ts": t + 1, "model": "claude-sonnet-5",
                          "input_tokens": rng.randint(100, 5000), "output_tokens": rng.randint(10, 800),
                          "stop_reason": "end_turn"})
            t += 1.5
            if rng.random() < 0.7:
                steps.append({"kind": "tool", "ts": t, "end_ts": t + 0.2, "name": rng.choice(["kb", "db", "http"]),
                              "is_error": rng.random() < 0.15, "input": {"q": rng.randint(1, 9)}})
                t += 0.3
        if rng.random() < 0.15:
            steps.append({"kind": "notice", "name": "outcome", "ts": t,
                          "outcome": rng.choice(["completed", "failed", "rework"]), "text": "graded in run"})
        p = {"id": rid, "project": "inc", "workflow": rng.choice(WORKFLOWS), "steps": steps,
             "thread_id": rng.choice([None, "th-a", "th-a", "th-b"]),
             "status": "error" if rng.random() < 0.1 else "ok", "complete": rng.random() > 0.2}
        if rng.random() < 0.2:
            p["feedback"] = [{"key": "user", "score": rng.choice([0.1, 0.9])}]
        p.update(over)
        self.runs[rid] = p
        self.eng.ingest(p)
        return rid

    def step(self):
        rng, e = self.rng, self.eng
        op = rng.random()
        if op < 0.45 or not self.runs:
            self.run()
        elif op < 0.55:                                          # a subagent: changes its (clean) parent
            parent = rng.choice(sorted(self.runs))
            self.run(parent_id=parent)
        elif op < 0.65:                                          # an existing run is updated
            self.run(rid=rng.choice(sorted(self.runs)))
        elif op < 0.72:                                          # a run is deleted at its source
            rid = rng.choice(sorted(self.runs))
            path = os.path.join(e.runs_dir, f"{rid}.json")
            if os.path.exists(path):
                os.remove(path)
            self.runs.pop(rid)
        elif op < 0.82:                                          # a grade on a task no data touched
            rid = rng.choice(sorted(self.runs))
            e.grade(f"{rid}#0", rng.choice(["completed", "failed", "rework"]), "graded later")
        elif op < 0.86:
            rid = rng.choice(sorted(self.runs))
            e.ungrade(f"{rid}#0")
        elif op < 0.93:                                          # a late span on an existing OTLP trace
            self.otlp(rng.randint(1, 4))
        else:                                                    # time passes: "in progress" expires
            self.clock += rng.choice([300, 700, 4000])
        e.refresh()

    def otlp(self, trace_no):
        rng = self.rng
        tid = f"{trace_no:032x}"
        ts = self.clock - 800
        sid = f"{rng.randint(1, 10**12):016x}"
        body = {"resourceSpans": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "inc-otlp"}}]},
            "scopeSpans": [{"spans": [{
                "traceId": tid, "spanId": sid, "name": "chat", "status": {},
                "startTimeUnixNano": str(int(ts * 1e9)), "endTimeUnixNano": str(int((ts + 1) * 1e9)),
                "attributes": [
                    {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                    {"key": "gen_ai.request.model", "value": {"stringValue": "claude-haiku-4-5"}},
                    {"key": "gen_ai.usage.input_tokens", "value": {"intValue": str(rng.randint(100, 3000))}},
                    {"key": "gen_ai.usage.output_tokens", "value": {"intValue": str(rng.randint(10, 300))}}]}]}]}]}
        self.eng.ingest_otlp(json.dumps(body).encode(), "application/json")

    def snapshot(self):
        con = self.eng.con

        def table(q):
            return [dict(r) for r in con.execute(q)]
        meta = {r["k"]: json.loads(r["v"]) for r in con.execute("SELECT k, v FROM meta") if r["k"] != "refreshed"}
        return {"tasks": table("SELECT * FROM tasks ORDER BY id"),
                "events": table("SELECT * FROM events ORDER BY id"),
                "baselines": table("SELECT * FROM baselines ORDER BY task_type"),
                "meta": meta}


class IncrementalEqualsFullTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_same(self, inc, full, where):
        self.assertEqual(len(inc["tasks"]), len(full["tasks"]), f"{where}: task count")
        for a, b in zip(inc["tasks"], full["tasks"]):
            if a != b:
                diff = {k: (a[k], b[k]) for k in a if a[k] != b.get(k)}
                self.fail(f"{where}: task {a['id']} differs from a full rebuild (incremental, full): {diff}")
        self.assertEqual(inc["events"], full["events"], f"{where}: events")
        self.assertEqual(inc["baselines"], full["baselines"], f"{where}: baselines")
        self.assertEqual(inc["meta"], full["meta"], f"{where}: insights")

    def test_random_histories(self):
        rescored = total = 0
        for seed in range(12):
            with self.subTest(seed=seed):
                sc = Scenario(seed, os.path.join(self.tmp, f"s{seed}"))
                try:
                    sc.eng.refresh(force=True)
                    for i in range(1, 121):
                        sc.step()
                        rescored += sc.eng.stats.get("tasks_rescored", 0)
                        total += sc.eng.con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                        if i % 30 == 0:
                            inc = sc.snapshot()
                            sc.eng.refresh(force=True)                      # a full rebuild from sources
                            self.assert_same(inc, sc.snapshot(), f"seed {seed}, step {i}")
                finally:
                    sc.eng.con.close()
        # and it has to actually be incremental: most refreshes re-score a small part of the store
        self.assertLess(rescored / total, 0.5, f"re-scored {rescored} of {total} task-refreshes")


class OnlyNewTrafficIsScoredTest(unittest.TestCase):
    """The point of #5, asserted as a count rather than a timing, so it cannot flake: into a store of
    several hundred tasks, 10 new ones are re-scored and nothing else is -- unless the type has grown
    past its next baseline step, when the whole type is re-scored once."""

    def test_new_tasks_only_until_the_baseline_steps(self):
        from agentdynamics.analysis import baseline_sample_size as m
        n0 = next(n for n in range(500, 2000) if m(n) == m(n + 10) and m(n + 10) < m(n + 60))
        tmp = tempfile.mkdtemp()
        eng = Engine(os.path.join(tmp, "data"), None)
        try:
            eng.ingest_otlp(otlp_traces(0, n0), "application/json")
            time.sleep(2.1)                  # outside the 2 s span-reassembly window (see bench/bench.py)
            eng.refresh(force=True)
            eng.ingest_otlp(otlp_traces(n0, 10), "application/json")
            eng.refresh()
            self.assertEqual(eng.stats["tasks_rescored"], 10, "only the new tasks")
            # grow past the next step: the type's baseline moves, so every task of it is re-scored once
            time.sleep(2.1)
            eng.ingest_otlp(otlp_traces(n0 + 10, 50), "application/json")
            eng.refresh()
            self.assertGreater(eng.stats["tasks_rescored"], 50)
            self.assertEqual(eng.stats["tasks_rescored"],
                             eng.con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        finally:
            eng.con.close()
            shutil.rmtree(tmp, ignore_errors=True)


def otlp_traces(first, n):
    """n OTLP traces of one workflow, each a single model call with a varying cost."""
    now = time.time() - 3600
    spans = []
    for i in range(first, first + n):
        tid = f"{i + 1:032x}"
        ts = now + i
        spans.append({"traceId": tid, "spanId": f"{i + 1:016x}", "name": "support", "status": {},
                      "startTimeUnixNano": str(int(ts * 1e9)), "endTimeUnixNano": str(int((ts + 2) * 1e9)),
                      "attributes": [
                          {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                          {"key": "gen_ai.agent.name", "value": {"stringValue": "support"}},
                          {"key": "gen_ai.request.model", "value": {"stringValue": "claude-sonnet-5"}},
                          {"key": "gen_ai.usage.input_tokens", "value": {"intValue": str(500 + (i * 37) % 3000)}},
                          {"key": "gen_ai.usage.output_tokens", "value": {"intValue": str(50 + (i * 11) % 400)}}]})
    return json.dumps({"resourceSpans": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "inc"}}]}, "scopeSpans": [{"spans": spans}]}]}).encode()


class SettledFieldsTest(unittest.TestCase):
    def test_every_field_the_settle_phase_writes_is_in_the_signature(self):
        """A field finalize writes before scoring but leaves out of SETTLED_FIELDS is served stale by
        incremental refreshes. Diff a task's fields before and after the settle phase on a history that
        exercises threads, subagents, grades and feedback, and require every changed one be listed."""
        tmp = tempfile.mkdtemp()
        try:
            sc = Scenario(3, tmp)
            for _ in range(60):
                sc.step()
            runs = list(sc.eng._runs.values())
            fresh = {r["id"]: analysis.run_tasks(r) for r in runs}
            before = {t["id"]: dict(t) for ts in fresh.values() for t in ts}
            scored = {"scores", "score", "apdex", "cost_vs_baseline", "duration_vs_baseline", "failed", "_sdk_grade"}
            analysis.finalize(runs, fresh, now=sc.clock, grades={"r1#0": {"outcome": "failed"}})
            written = set()
            for ts in fresh.values():
                for t in ts:
                    written |= {k for k, v in t.items() if before[t["id"]].get(k, object()) != v}
            missing = written - scored - set(analysis.SETTLED_FIELDS)
            self.assertEqual(missing, set(), "finalize writes these but SETTLED_FIELDS leaves them out")
            sc.eng.con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
