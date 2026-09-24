"""End-to-end tests on a synthetic Claude Code transcript + SDK run."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentdynamics import analysis, pricing  # noqa: E402
from agentdynamics.collectors import claude_code, generic  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api  # noqa: E402

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
        finally:
            eng.con.close()

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


if __name__ == "__main__":
    unittest.main()
