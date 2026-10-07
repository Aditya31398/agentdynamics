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
from agentdynamics.server import Api  # noqa: E402
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
        elif op < 0.72 and not e._runs_in_store:                 # a run is deleted at its source (a file)
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
        return snapshot(self.eng)


def snapshot(eng):
    con = eng.con

    def table(q):
        return [dict(r) for r in con.execute(q)]
    meta = {r["k"]: json.loads(r["v"]) for r in con.execute("SELECT k, v FROM meta") if r["k"] != "refreshed"}
    return {"tasks": table("SELECT * FROM tasks ORDER BY id"),
            "events": table("SELECT * FROM events ORDER BY id"),
            "baselines": table("SELECT * FROM baselines ORDER BY task_type"),
            "meta": meta,
            # computed by the API from the stored tasks, so it must not depend on how they got there
            "insights": Api(eng).process({"days": "", "sub": "1"})["insights"]}


class SameAsRebuild:
    def assert_same(self, inc, full, where):
        self.assertEqual(len(inc["tasks"]), len(full["tasks"]), f"{where}: task count")
        for a, b in zip(inc["tasks"], full["tasks"]):
            if a != b:
                diff = {k: (a[k], b[k]) for k in a if a[k] != b.get(k)}
                self.fail(f"{where}: task {a['id']} differs from a full rebuild (incremental, full): {diff}")
        self.assertEqual(inc["events"], full["events"], f"{where}: events")
        self.assertEqual(inc["baselines"], full["baselines"], f"{where}: baselines")
        self.assertEqual(inc["meta"], full["meta"], f"{where}: meta")
        self.assertEqual(inc["insights"], full["insights"], f"{where}: insights")


class IncrementalEqualsFullTest(SameAsRebuild, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

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


class BaselineReuseTest(SameAsRebuild, unittest.TestCase):
    """A baseline is re-computed only when its sample could have changed (analysis.finalize). Below 21
    tasks a type's baseline uses all of them, so any arrival changes the sample size and forces the
    re-computation: the random histories above never reach the reuse path. Here each kind of change
    is made to a type large enough that its sample size holds still, and must equal a full rebuild.
    Percentiles shrug off most changes to a sample, so the fixture is built for them to show: cost rises
    with start time, and each change moves the median. (Two first drafts passed with the check removed.)"""

    def setUp(self):
        m = analysis.baseline_sample_size
        n0 = next(n for n in range(60, 400) if m(n - 1) == m(n) == m(n + 1))   # a size that holds for +-1
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = 1_900_000_000.0
        self.eng = Engine(os.path.join(self.tmp, "data"), None)
        self.eng._clock = lambda: self.clock
        self.addCleanup(self.eng.con.close)
        for i in range(n0 - 2):         # with the parent's two tasks, the type holds n0
            # cost rises with start time, so any change in which tasks form the sample moves its percentiles
            self.eng.ingest(self.payload(f"r{i:03}", self.clock - 90000 + i * 60, tokens=1000 + i * 10))
        # a parent with two tasks that spawns a subagent from the first; the subagent starts after the
        # second, so without the spawn step it would roll up into the second instead
        self.eng.ingest(self.payload("parent", self.clock - 80000, tokens=4000, spawn="S1", second=True))
        self.eng.ingest(self.payload("agent-S1", self.clock - 79000, parent_id="parent"))
        self.eng.refresh(force=True)

    def payload(self, rid, ts, tokens=1000, spawn=None, second=False, **over):
        def turn(t, text):
            return [{"kind": "prompt", "ts": t, "text": text},
                    {"kind": "llm", "ts": t, "end_ts": t + 2, "model": "claude-sonnet-5", "input_tokens": tokens,
                     "output_tokens": 100, "stop_reason": "end_turn"}]
        steps = turn(ts, "Where is my refund?")
        if spawn:
            steps.append({"kind": "tool", "ts": ts + 2, "end_ts": ts + 3, "name": "Task", "subagent_id": spawn})
        if second:
            steps += turn(ts + 100, "And the other order?")
        p = {"id": rid, "project": "inc", "workflow": "support", "steps": steps, "status": "ok", "complete": True}
        p.update(over)
        return p

    def check(self, what):
        inc = snapshot(self.eng)
        self.eng.refresh(force=True)
        self.assert_same(inc, snapshot(self.eng), what)

    def test_a_later_task_reuses_the_baseline(self):
        self.eng.ingest(self.payload("late", self.clock - 10))
        self.eng.refresh()
        self.check("a task after the sample")

    def test_a_backfill_into_the_sample(self):
        # cheaper than every task in the sample, and it pushes out the dearest: the median moves
        self.eng.ingest(self.payload("early", self.clock - 200000, tokens=100))
        self.eng.refresh()
        self.check("a task earlier than the sample")

    @unittest.skipIf(os.environ.get("AGENTDYNAMICS_DB_URL"), "SDK runs are files only in a SQLite store")
    def test_a_task_in_the_sample_goes(self):
        os.remove(os.path.join(self.eng.runs_dir, "r000.json"))
        self.eng.ingest(self.payload("late", self.clock - 10))      # and one arrives, so the size holds
        self.eng.refresh()
        self.check("a sample task deleted")

    def test_a_task_in_the_sample_changes(self):
        self.eng.ingest(self.payload("r001", self.clock - 90000 + 60, tokens=90000))
        self.eng.refresh()
        self.check("a sample task updated")

    def test_a_subagent_changes_a_sample_task_cost(self):
        self.eng.ingest(self.payload("agent-late", self.clock - 89000, parent_id="r002", tokens=90000))
        self.eng.refresh()
        self.check("a subagent of a sample task")

    def test_a_parent_drops_its_spawn(self):
        self.eng.ingest(self.payload("parent", self.clock - 80000, tokens=4000, second=True))
        self.eng.refresh()
        self.check("a spawn step removed")


class MultiDayScenario(Scenario):
    """Traffic over 20 days, two models and two releases: tasks are scored against recent baselines (the 14 days
    before their own, per type, model and release), whose windows must be kept and dropped exactly right."""

    def run(self, rid=None, **over):
        rng = self.rng
        day = rng.randint(0, 20)
        over.setdefault("version", rng.choice([None, "v1", "v2"]))
        rid = super().run(rid, **over)
        p = self.runs[rid]
        shift = day * 86400
        model = rng.choice(["claude-sonnet-5", "claude-haiku-4-5"])
        for st in p["steps"]:
            st["ts"] -= shift
            if "end_ts" in st:
                st["end_ts"] -= shift
            if st["kind"] == "llm":
                st["model"] = model
        p["workflow"] = "support" if rng.random() < 0.8 else "billing"
        self.eng.ingest(p)
        return rid


class RecentBaselinesTest(SameAsRebuild, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_random_multi_day_histories(self):
        windows = 0
        for seed in range(4):
            with self.subTest(seed=seed):
                sc = MultiDayScenario(seed, os.path.join(self.tmp, f"m{seed}"))
                try:
                    sc.eng.refresh(force=True)
                    for i in range(1, 181):
                        sc.step()
                        if i % 45 == 0:
                            inc = sc.snapshot()
                            sc.eng.refresh(force=True)
                            self.assert_same(inc, sc.snapshot(), f"seed {seed}, step {i}")
                    windows += sc.eng.con.execute(
                        "SELECT COUNT(*) FROM tasks WHERE baseline LIKE '%last 14 days%'").fetchone()[0]
                finally:
                    sc.eng.con.close()
        self.assertGreater(windows, 50, "the histories have to reach recent baselines, or this proves nothing")

    def engine(self):
        clock = 1_900_000_000.0
        eng = Engine(os.path.join(self.tmp, "data"), None)
        eng._clock = lambda: clock
        self.addCleanup(eng.con.close)
        return eng, clock

    def payload(self, rid, ts, model="claude-sonnet-5", tokens=1000, version=None):
        p = {"id": rid, "project": "p", "workflow": "support", "status": "ok", "complete": True, "steps": [
            {"kind": "prompt", "ts": ts, "text": "Where is my refund?"},
            {"kind": "llm", "ts": ts, "end_ts": ts + 2, "model": model, "input_tokens": tokens, "output_tokens": 100}]}
        if version:
            p["version"] = version
        return p

    def task(self, eng, rid):
        row = eng.con.execute("SELECT cost_vs_baseline, baseline FROM tasks WHERE id = ?", (f"{rid}#0",)).fetchone()
        return row[0], json.loads(row[1])

    def test_a_model_change_is_compared_with_its_own_model(self):
        eng, now = self.engine()
        for i in range(30):          # three weeks ago and before: the cheap model
            eng.ingest(self.payload(f"old{i}", now - 40 * 86400 + i * 3600, "claude-haiku-4-5", 1000))
        for i in range(30):          # the last 13 days: the dear one, at ten times the tokens
            eng.ingest(self.payload(f"new{i}", now - 13 * 86400 + i * 36000, "claude-opus-5", 10000))
        eng.ingest(self.payload("today", now, "claude-opus-5", 10000))
        eng.refresh(force=True)
        ratio, b = self.task(eng, "today")
        self.assertEqual(b["basis"], "support · claude-opus-5, last 14 days")
        self.assertAlmostEqual(ratio, 1.0, places=2, msg="normal for this model, not 10x the old one")
        self.assertGreaterEqual(b["n"], 10)
        self.assertTrue(0 <= b["cost_rank"] <= 1)
        ratio, b = self.task(eng, "old0")
        self.assertEqual(b["basis"], "support, all history", "nothing recent before it: the type's history")

    def test_a_new_release_starts_from_the_broader_baseline(self):
        eng, now = self.engine()
        for i in range(20):
            eng.ingest(self.payload(f"v1-{i}", now - 10 * 86400 + i * 3600, version="v1", tokens=1000 + i))
        for i in range(3):
            eng.ingest(self.payload(f"v2-{i}", now - 2 * 86400 + i * 3600, version="v2"))
        eng.ingest(self.payload("v2-today", now, version="v2"))
        eng.ingest(self.payload("v1-today", now, version="v1"))
        eng.refresh(force=True)
        self.assertEqual(self.task(eng, "v2-today")[1]["basis"], "support · claude-sonnet-5, last 14 days",
                         "three v2 tasks aren't a baseline: the type and model's, across releases")
        self.assertEqual(self.task(eng, "v1-today")[1]["basis"], "support · claude-sonnet-5 · v1, last 14 days")

    def test_a_late_task_that_moves_no_figure_still_counts(self):
        """Identical tasks: a backfilled one changes no percentile of the window it lands in, only its size --
        which the task page shows. An incremental refresh has to rewrite it all the same."""
        eng, now = self.engine()
        for i in range(12):
            eng.ingest(self.payload(f"same{i}", now - 5 * 86400 + i * 3600))
        eng.ingest(self.payload("today", now))
        eng.refresh(force=True)
        self.assertEqual(self.task(eng, "today")[1]["n"], 12)
        eng.ingest(self.payload("backfill", now - 3 * 86400))
        eng.refresh()
        inc = snapshot(eng)
        self.assertEqual(self.task(eng, "today")[1]["n"], 13)
        eng.refresh(force=True)
        self.assert_same(inc, snapshot(eng), "a backfill that moves no percentile")

    def test_a_cost_rank_is_where_the_cost_falls_in_the_sample(self):
        q = [float(i) for i in range(1, 22)]                  # 21 quantiles: 1 .. 21
        self.assertEqual(analysis.cost_rank(q, 11.0), 0.5)
        self.assertEqual(analysis.cost_rank(q, 11.5), 0.525)
        self.assertEqual((analysis.cost_rank(q, 0.5), analysis.cost_rank(q, 99.0)), (0.0, 1.0))
        self.assertIsNone(analysis.cost_rank([], 3.0))

    def test_old_behaviour_ages_out(self):
        """The history baseline is anchored to the earliest tasks: after an agent got ten times cheaper, every task
        stayed at 0.1x "normal" for as long as the old ones were held. A recent baseline forgets them."""
        eng, now = self.engine()
        for i in range(60):          # the long history: most of what is held
            eng.ingest(self.payload(f"dear{i}", now - 60 * 86400 + i * 3600, tokens=20000))
        for i in range(40):
            eng.ingest(self.payload(f"cheap{i}", now - 14 * 86400 + i * 25000, tokens=2000))
        eng.ingest(self.payload("today", now, tokens=2000))
        eng.refresh(force=True)
        ratio, b = self.task(eng, "today")
        self.assertAlmostEqual(ratio, 1.0, places=2)
        hist = json.loads(eng.con.execute("SELECT data FROM baselines WHERE task_type = 'support'").fetchone()[0])
        self.assertGreater(hist["cost_p50"], 2 * b["cost_p50"], "the history baseline still says otherwise")


class TimeSettlesOutcomesTest(unittest.TestCase):
    """An "in progress" task settles as time passes. A refresh with no new traffic returned early, so on a
    quiet install it stayed "in progress" until something else arrived -- while a rebuild said "unknown".
    The random histories found it only on the Postgres run, whose different sequence of steps ended on a
    stretch of time with no traffic."""

    def test_in_progress_settles_without_traffic(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        clock = [1_900_000_000.0]
        eng = Engine(os.path.join(tmp, "data"), None)
        self.addCleanup(eng.con.close)
        eng._clock = lambda: clock[0]
        eng.ingest({"id": "r1", "project": "p", "workflow": "w", "complete": False, "status": "ok", "steps": [
            {"kind": "prompt", "ts": clock[0] - 100, "text": "hi"},
            {"kind": "llm", "ts": clock[0] - 100, "end_ts": clock[0] - 99, "model": "claude-sonnet-5",
             "input_tokens": 10, "output_tokens": 5}]})
        eng.refresh(force=True)
        q = "SELECT outcome FROM tasks WHERE id = 'r1#0'"
        self.assertEqual(eng.con.execute(q).fetchone()[0], "in progress")
        clock[0] += 60
        self.assertFalse(eng.refresh(), "nothing is due yet")
        clock[0] += analysis.IN_PROGRESS_S
        self.assertTrue(eng.refresh(), "the outcome is due to settle")
        self.assertEqual(eng.con.execute(q).fetchone()[0], "unknown")
        self.assertFalse(eng.refresh(), "and once settled, nothing more is due")


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
    """n OTLP traces of one workflow, each a single model call with a varying cost -- all yesterday, around noon
    UTC: tasks on two days would be scored against a recent baseline from the first, which new traffic on the
    second day doesn't move."""
    now = (int(time.time() // 86400) - 1) * 86400 + 43200
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
            # written while scoring, from inputs the scoring signature holds (the chosen baseline among them)
            scored = {"scores", "score", "apdex", "cost_vs_baseline", "duration_vs_baseline", "failed", "_sdk_grade",
                      "baseline", "apdex_basis"}
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
