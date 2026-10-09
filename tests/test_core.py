"""End-to-end tests on a synthetic Claude Code transcript + SDK run."""
import contextlib
import http.client
import io
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentdynamics import analysis, pricing  # noqa: E402
from agentdynamics.collectors import claude_code, generic  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

T0 = "2026-09-01T10:%02d:%02dZ"


class Builder:
    def __init__(self, sid="sess-1"):
        self.lines, self.sid, self.t, self.n = [], sid, 0, 0

    def ts(self):
        self.t += 2
        return T0 % divmod(self.t, 60)

    def user(self, text, **kw):
        self.lines.append({"type": "user", "sessionId": self.sid, "cwd": "E:\\proj", "timestamp": self.ts(),
                           "message": {"role": "user", "content": text}, **kw})

    def assistant(self, blocks, stop="tool_use", usage=None):
        self.n += 1
        mid = f"msg_{self.n}"
        usage = usage or {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000,
                          "cache_creation_input_tokens": 200}
        for b in blocks:  # Claude Code writes one line per content block, repeating usage
            self.lines.append({"type": "assistant", "sessionId": self.sid, "timestamp": self.ts(),
                               "message": {"id": mid, "model": "claude-opus-5", "role": "assistant", "content": [b],
                                           "stop_reason": stop, "usage": usage}})

    def tool(self, tid, name, inp, result="ok", error=False):
        self.assistant([{"type": "thinking", "thinking": ""}, {"type": "tool_use", "id": tid, "name": name, "input": inp}])
        self.lines.append({"type": "user", "sessionId": self.sid, "timestamp": self.ts(),
                           "message": {"role": "user", "content": [
                               {"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": error}]}})

    def write(self, path):
        with open(path, "w", encoding="utf-8") as f:
            for line in self.lines:
                f.write(json.dumps(line) + "\n")


def build_fixture(root):
    proj = os.path.join(root, "E--proj")
    os.makedirs(os.path.join(proj, "sess-1", "subagents"))
    b = Builder()
    b.user("Fix the failing login test")
    b.tool("t1", "Read", {"file_path": "app/login.py"})
    b.tool("t2", "Read", {"file_path": "app/login.py"})            # redundant read
    b.tool("t3", "Grep", {"pattern": "token"})
    b.tool("t4", "Grep", {"pattern": "token"})                     # duplicate call
    b.tool("t5", "Edit", {"file_path": "app/login.py", "old_string": "a", "new_string": "b"})
    b.tool("t6", "Bash", {"command": "pytest -q"}, "1 passed")       # verification after the edit
    b.tool("t7", "Agent", {"description": "review", "prompt": "review it"})
    b.lines[-1]["toolUseResult"] = {"agentId": "abc123", "totalTokens": 500}
    b.assistant([{"type": "text", "text": "Fixed and tests pass."}], stop="end_turn")
    b.user("still not working")                                    # correction -> previous task is rework
    for i in range(3):
        b.tool(f"e{i}", "Bash", {"command": f"cmd --try {i}"}, "boom", error=True)
    b.user("[Request interrupted by user]")
    b.lines.append({"type": "custom-title", "customTitle": "Login fix", "sessionId": "sess-1"})
    b.write(os.path.join(proj, "sess-1.jsonl"))

    s = Builder("sess-1")
    s.user("review it", isSidechain=True)
    s.tool("s1", "Read", {"file_path": "app/login.py"})
    s.assistant([{"type": "text", "text": "Looks good"}], stop="end_turn")
    s.write(os.path.join(proj, "sess-1", "subagents", "agent-abc123.jsonl"))


class CoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.root = os.path.join(cls.tmp, "projects")
        build_fixture(cls.root)
        cls.runs = [claude_code.parse_file(p, cls.root) for p in claude_code.discover(cls.root)]
        cls.tasks, cls.base, cls.events = analysis.analyze(cls.runs, now=0)
        cls.by_id = {t["id"]: t for t in cls.tasks}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def main_run(self):
        return next(r for r in self.runs if not r["is_subagent"])

    def test_usage_counted_once_per_message(self):
        llm = [s for s in self.main_run()["steps"] if s["kind"] == "llm"]
        self.assertEqual(len(llm), 11)  # 10 tool turns + 1 text turn, despite 2 lines per message
        self.assertEqual(llm[0]["output_tokens"], 100)
        self.assertAlmostEqual(llm[0]["cost"], pricing.cost("claude-opus-5", 10, 100, 1000, 200))

    def test_each_current_model_has_its_own_list_price(self):
        """Prices match by longest prefix, so a point release missing from the table silently took an older
        model's price: claude-opus-5-5 was billed as claude-opus-5."""
        for model, (inp, out, read) in {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-opus-5": (5.0, 25.0, 0.5),
                                        "claude-sonnet-5-5": (2.0, 10.0, 0.20), "claude-fable-5-1": (10.0, 50.0, 0.25),
                                        "claude-haiku-4-5": (1.0, 5.0, 0.1)}.items():
            r = pricing.rates(model)
            self.assertEqual((r["input"], r["output"]), (inp, out), model)
            self.assertAlmostEqual(r["cache_read"], read, msg=model)
        self.assertIsNone(pricing.rates("gpt-5"), "unknown models are unpriced, never guessed")
        self.assertAlmostEqual(pricing.rates("claude-mythos-5-1")["cache_read"], 0.25, msg="0.025x, like Claude Fable 5.1")

    def test_an_entry_prices_its_own_model_and_never_a_later_version(self):
        """A table entry used to price every id it prefixed, so a release missing from the table took the price of
        the one before it. Now only a snapshot date or a provider's version tag may follow an entry's id."""
        for model, key in {"claude-opus-4-1-20250805": "claude-opus-4-1", "claude-opus-4-20250514": "claude-opus-4",
                           "claude-sonnet-4-5-20250929": "claude-sonnet-4-5", "claude-sonnet-4-6": "claude-sonnet-4-6",
                           "us.anthropic.claude-sonnet-4-5-20250929-v1:0": "claude-sonnet-4-5",
                           "anthropic.claude-opus-5-5": "claude-opus-5-5", "claude-opus-4-5@20251101": "claude-opus-4-5",
                           "anthropic/claude-opus-4.5": "claude-opus-4-5", "claude-haiku-4-5-20251001": "claude-haiku-4-5",
                           "claude-opus-4-6[1m]": "claude-opus-4-6"}.items():
            self.assertEqual(pricing.entry(model), key, model)
        for model in ("claude-opus-5-6", "claude-sonnet-4-7", "claude-haiku-4-6", "claude-opus-4-9-20270101", "claude-fable-6"):
            self.assertIsNone(pricing.rates(model), f"{model}: a version the table doesn't know is unpriced")
        self.assertEqual(pricing.rates("claude-opus-4-1")["input"], 15.0, "not Opus 4's entry by accident")

    def test_batch_fast_and_us_inference_change_the_price(self):
        base = pricing.cost("claude-opus-5-5", 1000, 200, 3000, 100, 400)
        self.assertAlmostEqual(base, (1000 * 4 + 200 * 20 + 3000 * 0.2 + 100 * 5 + 400 * 8) / 1e6)
        self.assertAlmostEqual(pricing.cost("claude-opus-5-5", 1000, 200, 3000, 100, 400, "batch"), base * 0.5)
        self.assertAlmostEqual(pricing.cost("claude-opus-5-5", 1000, 200, 3000, 100, 400, speed="fast"), base * 2)
        self.assertAlmostEqual(pricing.cost("claude-opus-5-5", 1000, 200, 3000, 100, 400, "batch", None, "us"), base * 0.55)
        self.assertAlmostEqual(pricing.cost("claude-opus-5-5", 1000, 200, 3000, 100, 400, "standard", "standard", "global"), base)

    def test_segmentation_and_types(self):
        t0, t1 = self.by_id["sess-1#0"], self.by_id["sess-1#1"]
        self.assertEqual(t0["task_type"], "bugfix")
        self.assertEqual(t0["tool_calls"], 7)
        self.assertEqual(self.main_run()["title"], "Login fix")
        self.assertEqual(t1["prompt"], "still not working")

    def test_waste_detection(self):
        t0 = self.by_id["sess-1#0"]
        self.assertEqual(t0["redundant_reads"], 1)
        self.assertEqual(t0["duplicate_calls"], 1)
        self.assertGreater(t0["waste_cost"], 0)

    def test_verification(self):
        t0 = self.by_id["sess-1#0"]
        self.assertEqual(t0["code_changed"], 1)
        self.assertEqual(t0["verified"], 1)

    def test_phases(self):
        self.assertEqual(self.by_id["sess-1#0"]["phase_calls"],
                         {"explore": 4, "edit": 1, "verify": 1, "delegate": 1})

    def test_outcomes_and_events(self):
        self.assertEqual(self.by_id["sess-1#0"]["outcome"], "rework")
        t1 = self.by_id["sess-1#1"]
        self.assertEqual(t1["outcome"], "interrupted")
        self.assertEqual(t1["max_error_streak"], 3)
        rules = {e["rule_id"] for e in self.events if e["task_id"] == t1["id"]}
        self.assertIn("error_streak", rules)
        self.assertIn("interrupted", rules)

    def test_subagent_rollup(self):
        t0 = self.by_id["sess-1#0"]
        self.assertEqual(t0["subagents"], 1)
        self.assertGreater(t0["subagent_cost"], 0)
        sub = next(t for t in self.tasks if t["is_subagent"])
        self.assertEqual(sub["parent_task_id"], "sess-1#0")

    def test_scores_bounded(self):
        for t in self.tasks:
            for v in (t.get("scores") or {}).values():
                if v is not None:
                    self.assertTrue(0 <= v <= 100)

    def test_task_typing(self):
        c = analysis.classify_task
        self.assertEqual(c("human", "Go ahead and create a dashboard, also think of how to test it"), "feature/build")
        self.assertEqual(c("human", "What is the equivalent tool?"), "question/research")
        self.assertEqual(c("human", "continue"), "follow-up")
        self.assertEqual(c("scheduled", "anything"), "scheduled job")

    def test_task_types_say_how_they_were_decided(self):
        """A workflow name is a fact; a keyword match is a guess. The label has to say which."""
        d = analysis.classify_task_detail
        self.assertEqual(d("human", "Fix the failing login test"), ("bugfix", "keywords", "fix"))
        self.assertEqual(d("human", "continue"), ("follow-up", "follow-up", None))
        self.assertEqual(d("command", "/review"), ("slash command", "prompt kind", None))
        self.assertEqual(d("human", "Hello there"), ("other", "unmatched", None))
        # the transcript fixture: typed by keyword, with the matched word kept
        t = next(x for x in self.tasks if x["prompt"].startswith("Fix the failing login test"))
        self.assertEqual((t["task_type_source"], t["task_type_match"]), ("keywords", "fix"))
        # a traced run: its workflow name wins over any keyword in the prompt
        run = generic.normalize({"id": "typed", "workflow": "refund_triage", "steps": [
            {"kind": "prompt", "ts": 1, "text": "Fix my broken refund"},
            {"kind": "llm", "ts": 1, "end_ts": 2, "model": "claude-opus-5", "input_tokens": 10, "output_tokens": 5}]})
        t = analysis.run_tasks(run)[0]
        self.assertEqual((t["task_type"], t["task_type_source"], t["task_type_match"]),
                         ("refund_triage", "workflow", None))

    def test_generic_normalize(self):
        run = generic.normalize({"id": "r1", "agent": "bot", "steps": [
            {"kind": "prompt", "ts": 1, "text": "hi"},
            {"kind": "llm", "ts": 1, "end_ts": 2, "model": "claude-sonnet-5", "input_tokens": 1000, "output_tokens": 100},
            {"kind": "tool", "ts": 2, "end_ts": 3, "name": "Read", "input": {"file_path": "x"}}]})
        self.assertAlmostEqual(run["steps"][1]["cost"], (1000 * 2 + 100 * 10) / 1e6)
        self.assertEqual(run["steps"][2]["phase"], "explore")
        self.assertEqual(run["steps"][1]["tool_calls"], 1)

    def test_engine_and_api(self):
        eng = Engine(os.path.join(self.tmp, "data"), self.root)
        try:
            eng.refresh(force=True)
            eng.ingest({"id": "sdk-1", "agent": "bot", "steps": [
                {"kind": "prompt", "ts": 1, "text": "Summarize"},
                {"kind": "llm", "ts": 1, "end_ts": 2, "model": "claude-opus-5", "input_tokens": 100,
                 "output_tokens": 10, "stop_reason": "end_turn"}]})
            eng.refresh()
            api = Api(eng)
            self.assertEqual(api.overview({})["kpis"]["tasks"], 3)
            self.assertTrue(api.flowmap({})["nodes"])
            self.assertTrue(api.tools({})["tools"])
            self.assertIsNotNone(api.task("sess-1#0"))
            self.assertTrue(api.process({})["insights"])
            self.assertEqual(api.compare({"dim": "project", "a": "E--proj", "b": "bot"})["b"]["tasks"], 1)
            rows = api.analytics({"group": "outcome", "metrics": "tasks,cost"})["rows"]
            self.assertEqual(sum(r["tasks"] for r in rows), 3)
            # Task Types says how each label was decided, and shows the words when it was a guess
            by_type = {t["type"]: t for t in api.types({})["types"]}
            self.assertEqual(set(by_type["bugfix"]["typed_by"]), {"keywords"})   # all guessed, none stated
            self.assertIn("fix", by_type["bugfix"]["top_matches"])
        finally:
            eng.con.close()

    def test_span_lookups_use_their_indexes(self):
        """With no ANALYZE statistics (every fresh install) SQLite answered the per-trace lookup by
        walking the primary key on `source` alone -- every span of the source, once per trace. A full
        refresh was O(n^2): 6 s at 1k traces, 81 s at 4k. And "updated > ?" scanned the whole table on
        every incremental refresh. This captures the SQL the store really runs and checks each plan is
        an index search, so the regression is caught here, not only by bench/bench.py --check."""
        from agentdynamics import store
        con = store.connect(os.path.join(self.tmp, "plans.db"))
        try:
            seen = []
            con.set_trace_callback(seen.append)
            store.trace_spans(con, "otlp", "t1")
            store.spans_for_traces(con, [("otlp", "t1"), ("otlp", "t2")])     # the batched form refresh uses
            store.traces_updated_since(con, 0)
            con.set_trace_callback(None)
            checked = 0
            for sql in seen:
                where = sql.split("WHERE", 1)[-1]
                index = ("spans_trace" if "trace_id =" in where or "trace_id=" in where or "trace_id IN" in where
                         else "spans_updated" if "updated >" in where else None)
                if index is None or "spans_raw" not in sql:
                    continue
                # the trace callback inlines parameters, so this is the statement exactly as it ran
                plan = " ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql))
                self.assertTrue(plan.startswith(f"SEARCH spans_raw USING INDEX {index}"), f"{sql!r} -> {plan}")
                checked += 1
            self.assertEqual(checked, 3, seen)
            # rolling up a day before retention purges it reads that day's tasks by start time
            seen.clear()
            con.set_trace_callback(seen.append)
            store.freeze_days(con, [("2026-01-01", 1767225600, 1767312000)], "2026-01-01", 1767312000)
            con.set_trace_callback(None)
            sql = next(s for s in seen if s.startswith("INSERT OR REPLACE INTO rollup_daily"))
            plan = " ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql))
            self.assertIn("SEARCH tasks USING INDEX tasks_started", plan)
        finally:
            con.close()

    def test_an_slo_over_no_tasks_is_unknown_not_an_error(self):
        """A filter that matches nothing -- a project with no traffic yet, or one a scoped key can't
        see -- made every ">=" objective raise, and /api/slos return a 500."""
        from agentdynamics import slo
        for op, metric, target in ((">=", "success_rate", 0.95), ("<=", "p95_duration", 60)):
            with self.subTest(op=op):
                r = slo.evaluate([], {"id": "x", "name": "x", "metric": metric, "op": op, "target": target,
                                      "window_days": 7})
                self.assertEqual((r["value"], r["met"], r["status"]), (None, None, "unknown"))

    @unittest.skipIf(os.environ.get("AGENTDYNAMICS_DB_URL"), "SDK runs are files only in a SQLite store")
    def test_refresh_survives_a_missing_runs_dir(self):
        """Removing <data>/runs used to raise FileNotFoundError out of every later refresh, while
        /healthz went on reporting 'ok' from the last successful timestamp."""
        eng = Engine(os.path.join(self.tmp, "data2"), self.root)
        try:
            eng.ingest({"id": "sdk-1", "agent": "bot", "steps": [
                {"kind": "llm", "ts": 1, "end_ts": 2, "model": "claude-opus-5",
                 "input_tokens": 100, "output_tokens": 10, "stop_reason": "end_turn"}]})
            eng.refresh(force=True)
            before = Api(eng).overview({})["kpis"]["tasks"]

            shutil.rmtree(eng.runs_dir)
            eng.refresh()                    # the background loop's call: must not raise
            self.assertTrue(os.path.isdir(eng.runs_dir), "the runs dir should be recreated")
            self.assertEqual(Api(eng).healthz()["status"], "ok")
            # A directory we cannot read is not a statement that every run in it was deleted;
            # a blinking mount must not empty the console. Deleting one file still removes it.
            self.assertEqual(Api(eng).overview({})["kpis"]["tasks"], before)
        finally:
            eng.con.close()

    @unittest.skipIf(os.environ.get("AGENTDYNAMICS_DB_URL"), "SDK runs are files only in a SQLite store")
    def test_deleting_one_run_file_still_removes_it(self):
        """The guard above must not turn into "file deletions are ignored"."""
        eng = Engine(os.path.join(self.tmp, "data4"), None)
        try:
            eng.ingest({"id": "sdk-9", "agent": "bot", "steps": [
                {"kind": "llm", "ts": 1, "end_ts": 2, "model": "claude-opus-5",
                 "input_tokens": 100, "output_tokens": 10, "stop_reason": "end_turn"}]})
            eng.refresh(force=True)
            self.assertEqual(Api(eng).overview({})["kpis"]["tasks"], 1)
            for fn in os.listdir(eng.runs_dir):
                os.remove(os.path.join(eng.runs_dir, fn))
            eng.refresh()
            self.assertEqual(Api(eng).overview({})["kpis"]["tasks"], 0)
        finally:
            eng.con.close()

    def test_healthz_reports_a_failing_refresh_loop(self):
        """A frozen last_refresh must not read as healthy."""
        eng = Engine(os.path.join(self.tmp, "data3"), self.root)
        try:
            eng.refresh(force=True)
            api = Api(eng)
            self.assertEqual(api.healthz()["status"], "ok")

            eng.failed_refreshes, eng.last_refresh_error = 3, "OSError: disk gone"
            h = api.healthz()
            self.assertEqual(h["status"], "degraded")
            self.assertEqual(h["failed_refreshes"], 3)
            self.assertIn("disk gone", h["last_refresh_error"])
            self.assertIn("agentdynamics_refresh_failures", api.prometheus())

            eng.refresh(force=True)          # a success clears it
            self.assertEqual(api.healthz()["status"], "ok")
        finally:
            eng.con.close()


class TimeAndAttributionTest(unittest.TestCase):
    """Agent time from the intervals steps covered, and each tool call's cost as writing it plus carrying its result.

    The old rules: agent time summed gaps between timestamps, each capped at 5 minutes, so a 20-minute build
    counted 5 and a person approving for 4 counted 4; a turn's whole cost was split evenly over the calls it
    issued, so a 40 kB file read cost the same as a 40-byte one."""

    T0 = 1_790_000_000

    def task(self, steps):
        for st in steps:
            for k in ("ts", "end_ts"):
                if k in st:
                    st[k] += self.T0
        run = generic.normalize({"id": "t", "agent": "bot", "steps": steps})
        self.steps = run["steps"]
        return analysis.run_tasks(run)[0]

    def tools(self):
        return [s for s in self.steps if s["kind"] == "tool"]

    def llm(self, ts, inp, out=100, **kw):
        return dict({"kind": "llm", "ts": ts, "end_ts": ts + 10, "model": "claude-sonnet-5", "input_tokens": inp,
                     "output_tokens": out}, **kw)

    def read(self, ts, path="a.py", chars=40_000, **kw):
        return dict({"kind": "tool", "name": "Read", "ts": ts, "end_ts": ts + 1, "input": {"file_path": path},
                     "output_chars": chars}, **kw)

    def test_agent_time_is_what_steps_covered(self):
        t = self.task([{"kind": "prompt", "ts": 0, "text": "build it"}, self.llm(0, 100),
                       {"kind": "tool", "name": "Bash", "ts": 10, "end_ts": 1210, "input": {"command": "make"}},
                       self.llm(1210, 100), self.llm(4820, 100)])
        self.assertEqual(t["duration_s"], 10 + 1200 + 10 + 10, "the 20-minute build counts; the hour away doesn't")
        self.assertEqual(t["wall_s"], 4830)

    def test_parallel_calls_count_once_and_a_persons_time_not_at_all(self):
        t = self.task([{"kind": "prompt", "ts": 0, "text": "go"}, self.llm(0, 100),
                       {"kind": "tool", "name": "Bash", "ts": 10, "end_ts": 70, "input": {"command": "a"}},
                       {"kind": "tool", "name": "Bash", "ts": 20, "end_ts": 80, "input": {"command": "b"}},
                       {"kind": "span", "name": "approve refund", "ts": 80, "end_ts": 320, "hitl": True},
                       {"kind": "tool", "name": "Bash", "ts": 320, "end_ts": 380, "input": {"command": "rm"}, "rejected": True},
                       self.llm(380, 100)])
        self.assertEqual(t["duration_s"], 10 + 70 + 10, "two overlapping calls are 70 s, the approval and the rejection none")

    def test_a_result_costs_what_carrying_it_cost(self):
        steps = [{"kind": "prompt", "ts": 0, "text": "fix a.py"},
                 self.llm(0, 1_000), self.read(10),                 # 40 kB = 10k tokens, carried by every later turn
                 self.llm(20, 11_000), self.read(30),               # the same file again: a redundant read
                 self.llm(40, 21_000), self.llm(60, 21_000, out=50)]
        t = self.task(steps)
        reads = self.tools()
        rate_in, rate_out = 2 / 1e6, 10 / 1e6                      # claude-sonnet-5, no cache: 2e-6 per context token
        self.assertAlmostEqual(reads[0]["attributed_cost"], 100 * rate_out + 3 * 10_000 * rate_in)
        self.assertAlmostEqual(reads[1]["attributed_cost"], 100 * rate_out + 2 * 10_000 * rate_in)
        self.assertEqual(t["redundant_reads"], 1)
        self.assertAlmostEqual(t["waste_cost"], 100 * rate_out + 2 * 10_000 * rate_in, places=5)
        self.assertAlmostEqual(sum(t["phase_cost"].values()), t["cost"], places=5, msg="every dollar goes somewhere")
        self.assertAlmostEqual(t["phase_cost"]["respond"], 150 * rate_out, places=5)
        self.assertAlmostEqual(t["phase_cost"]["context"], (1_000 + 1_000 + 1_000 + 1_000) * rate_in, places=5)

    def test_compaction_ends_the_carry_and_each_agent_carries_its_own(self):
        steps = [{"kind": "prompt", "ts": 0, "text": "go"}, self.llm(0, 1_000), self.read(10),
                 {"kind": "notice", "name": "compaction", "ts": 15},
                 self.llm(20, 11_000), self.llm(40, 11_000)]
        self.task(steps)
        self.assertAlmostEqual(self.tools()[0]["attributed_cost"], 100 * 10 / 1e6, msg="dropped from context: nothing after")
        steps = [{"kind": "prompt", "ts": 0, "text": "go"}, self.llm(0, 1_000, agent="a"), self.read(10, agent="a"),
                 self.llm(20, 11_000, agent="b"), self.llm(40, 11_000, agent="a")]
        self.task(steps)
        self.assertAlmostEqual(self.tools()[0]["attributed_cost"], 100 * 10 / 1e6 + 10_000 * 2 / 1e6,
                               msg="carried by a's next turn, not by b's")
        steps = [{"kind": "prompt", "ts": 0, "text": "go"}, self.llm(0, 1_000, agent="a"), self.read(10),
                 self.llm(20, 11_000, agent="a")]
        self.task(steps)
        self.assertAlmostEqual(self.tools()[0]["attributed_cost"], 100 * 10 / 1e6 + 10_000 * 2 / 1e6,
                               msg="a tool step naming no agent is carried by the one agent taking turns")
        steps = [{"kind": "prompt", "ts": 0, "text": "go"}, self.llm(0, 1_000),
                 self.read(10, chars=4_000, denied=True, rule="capability.not_granted"), self.llm(20, 11_000)]
        t = self.task(steps)
        self.assertAlmostEqual(t["blocked_cost"], 100 * 10 / 1e6, msg="a refused call cost writing it, and nothing after")


def reset(sock):
    """Close with an RST, the way a browser drops a connection it has given up on."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


class ClientDisconnectTest(unittest.TestCase):
    """A client hanging up mid-request (a reload, a closed tab) is routine: nothing is printed and the
    server goes on serving. The same exception types raised by our own code are still real errors."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.eng = Engine(os.path.join(self.tmp, "data"), None)
        self.addCleanup(self.eng.con.close)
        self.api = api = Api(self.eng)
        api.overview = lambda q: {"blob": "x" * (16 << 20)}     # far more than the socket buffers hold
        self.reading = reading = threading.Event()

        class H(Handler):
            timeout = 10                                         # a stuck handler can't hang server_close()

            def _body(self):
                reading.set()
                return super()._body()

        H.api = api
        # everything the server prints (tracebacks, socketserver's "Exception occurred") lands here
        self.err = io.StringIO()
        redirect = contextlib.redirect_stderr(self.err)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.stop)
        self.addr = self.srv.server_address
        self.url = f"http://127.0.0.1:{self.addr[1]}"

    def stop(self):
        """Stop the server and join its handler threads, so whatever they print has been printed."""
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()                              # joins them: daemon_threads is off
            self.srv = None

    def assert_still_serving_quietly(self):
        with urllib.request.urlopen(self.url + "/healthz", timeout=30) as r:
            self.assertEqual(r.status, 200)
        self.stop()
        self.assertEqual(self.err.getvalue(), "")

    def test_a_client_hanging_up_mid_response(self):
        s = socket.create_connection(self.addr)
        s.sendall(b"GET /api/overview HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        s.close()                                                # before reading a byte of the answer
        self.assert_still_serving_quietly()

    def test_a_client_resetting_an_idle_keep_alive_connection(self):
        s = socket.create_connection(self.addr)
        s.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        r = http.client.HTTPResponse(s)
        r.begin()
        r.read()
        self.assertEqual(r.status, 200)
        reset(s)                                                 # the server is waiting for the next request
        self.assert_still_serving_quietly()

    def test_a_client_hanging_up_mid_upload(self):
        s = socket.create_connection(self.addr)
        s.sendall(b"POST /api/ingest HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 100000\r\n\r\n[{\"id\": ")
        self.assertTrue(self.reading.wait(10))
        reset(s)
        self.assert_still_serving_quietly()

    def test_our_own_errors_are_still_logged_and_answered(self):
        def fail(*a, **kw):
            raise ConnectionResetError("upstream went away")    # a disconnect type, but not from the client
        self.api.overview = fail
        self.eng.ingest_runs = fail
        for req, code in ((self.url + "/api/overview", 500),
                          (urllib.request.Request(self.url + "/api/ingest", data=b"{}", method="POST"), 400)):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=30)
            self.assertEqual(cm.exception.code, code)
            self.assertIn("upstream went away", json.loads(cm.exception.read())["error"])
            cm.exception.close()
        self.stop()
        self.assertEqual(self.err.getvalue().count("ConnectionResetError: upstream went away"), 2)


if __name__ == "__main__":
    unittest.main()
