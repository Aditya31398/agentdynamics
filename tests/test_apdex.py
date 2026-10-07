"""Agent Apdex against targets people set, per task type.

Apdex used to be cost alone, against 1.5x the type's own median: what users feel (time) wasn't in it, and a type
that was uniformly slow or dear scored well. Now the outcome comes first, then the worse of cost and agent time
against T -- the target set for the type, or 1.5x its baseline median when there is none -- and every task says
which it was judged against (`apdex_basis`)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agentdynamics import analysis, slo  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

NOW = 1_900_000_000.0


def payload(rid, wf, seconds, tokens=1000, status="ok", ts=None):
    ts = ts or NOW - 3600
    return {"id": rid, "project": "p", "workflow": wf, "status": status, "complete": True, "steps": [
        {"kind": "prompt", "ts": ts, "text": "help"},
        {"kind": "llm", "ts": ts, "end_ts": ts + seconds, "model": "claude-sonnet-5", "input_tokens": tokens,
         "output_tokens": 100, "stop_reason": "end_turn"}]}


class ScoringTest(unittest.TestCase):
    def task(self, cost, secs, outcome="completed"):
        return {"cost": cost, "subagent_cost": 0.0, "duration_s": secs, "outcome": outcome, "max_error_streak": 0,
                "waste_cost": 0, "redundant_reads": 0, "duplicate_calls": 0, "max_edits_one_file": 0, "edits": 0,
                "explore_ratio": 0, "tool_calls": 0, "tool_error_rate": 0, "api_errors": 0, "verified": None,
                "cache_hit": None, "llm_calls": 1, "interrupts": 0, "max_context": 0, "compactions": 0}

    def apdex(self, t, b=None, target=None):
        analysis.score_task(t, {"cost_p50": 0.01, "duration_p50": 10} if b is None else b, target)
        return t["apdex"], t["apdex_basis"]

    def test_time_counts_now_not_only_cost(self):
        self.assertEqual(self.apdex(self.task(0.01, 10)), ("satisfied", "baseline"))
        self.assertEqual(self.apdex(self.task(0.01, 40)), ("tolerating", "baseline"), "cheap but 4x as slow as usual")
        self.assertEqual(self.apdex(self.task(0.01, 61)), ("frustrated", "baseline"), "beyond 4 x 1.5 x the median")

    def test_a_target_wins_over_the_baseline_one_dimension_at_a_time(self):
        target = {"latency_s": 5}
        self.assertEqual(self.apdex(self.task(0.01, 4), target=target), ("satisfied", "targets"))
        self.assertEqual(self.apdex(self.task(0.01, 10), target=target), ("tolerating", "targets"),
                         "usual for the type, but slower than people want")
        self.assertEqual(self.apdex(self.task(0.10, 4), target=target), ("frustrated", "targets"),
                         "no cost target: cost is still judged against the baseline")
        self.assertEqual(self.apdex(self.task(0.10, 4), target={"latency_s": 5, "cost": 0.2}), ("satisfied", "targets"))

    def test_the_outcome_comes_first(self):
        self.assertEqual(self.apdex(self.task(0.001, 1, "failed"), target={"latency_s": 5}), ("frustrated", "targets"))

    def test_with_nothing_to_judge_by_it_is_satisfied(self):
        self.assertEqual(self.apdex(self.task(5.0, 500), b={}), ("satisfied", "baseline"))

    def test_good_task_rate_is_completed_and_satisfied(self):
        ts = [{"outcome": "completed", "apdex": "satisfied"}, {"outcome": "completed", "apdex": "tolerating"},
              {"outcome": "failed", "apdex": "frustrated"}, {"outcome": "completed", "apdex": "satisfied"}]
        self.assertEqual(slo.metric(ts, "good_task_rate"), 0.5)
        self.assertIn("good_task_rate", slo.RATIO, "it gets an error budget and burn-rate alerts")


class TargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.eng = Engine(os.path.join(self.tmp, "data"), None)
        self.eng._clock = lambda: NOW
        self.addCleanup(self.eng.con.close)
        for i in range(6):
            self.eng.ingest(payload(f"s{i}", "search", 20 + i))
            self.eng.ingest(payload(f"r{i}", "refund", 2 + i))
        self.eng.refresh(force=True)

    def basis(self, wf):
        return {r[0] for r in self.eng.con.execute("SELECT apdex_basis FROM tasks WHERE workflow = ?", (wf,))}

    def apdex(self, wf):
        return sorted(r[0] for r in self.eng.con.execute("SELECT apdex FROM tasks WHERE workflow = ?", (wf,)))

    def test_saving_a_target_rescores_that_type(self):
        self.assertEqual(self.basis("search"), {"baseline"})
        self.eng.save_apdex_targets({"search": {"latency_s": 5}})
        self.assertEqual(self.basis("search"), {"targets"})
        self.assertEqual(self.apdex("search"), ["frustrated"] * 5 + ["tolerating"], "20 s is 4x 5 s; 21-25 s beyond")
        self.assertEqual(self.basis("refund"), {"baseline"}, "another type keeps its own")

    def test_targets_merge_and_clear_per_type(self):
        self.eng.save_apdex_targets({"search": {"latency_s": 5}})
        self.eng.save_apdex_targets({"refund": {"cost": 0.5, "latency_s": ""}})
        self.assertEqual(self.eng.apdex_targets(), {"search": {"latency_s": 5.0}, "refund": {"cost": 0.5}})
        self.eng.save_apdex_targets({"search": None, "refund": {"latency_s": None, "cost": None}})
        self.assertEqual(self.eng.apdex_targets(), {})
        self.assertEqual(self.basis("search") | self.basis("refund"), {"baseline"})

    def test_a_bad_target_saves_nothing(self):
        self.eng.save_apdex_targets({"search": {"latency_s": 5}})
        for bad in ({"refund": {"latency_s": -1}}, {"refund": {"latency_s": "fast"}}, {"refund": {"speed": 3}},
                    {"refund": {"cost": True}}, {"refund": 7}, ["refund"], None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.eng.save_apdex_targets(bad)
        self.assertEqual(self.eng.apdex_targets(), {"search": {"latency_s": 5.0}})

    def test_incremental_refresh_equals_a_rebuild_after_a_target_changes(self):
        self.eng.save_apdex_targets({"search": {"latency_s": 22}})
        self.eng.ingest(payload("late", "search", 30, ts=NOW - 60))
        self.eng.refresh()
        q = "SELECT id, apdex, apdex_basis, score FROM tasks ORDER BY id"
        inc = [tuple(r) for r in self.eng.con.execute(q)]
        self.eng.refresh(force=True)
        self.assertEqual(inc, [tuple(r) for r in self.eng.con.execute(q)])


class RouteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        with open(os.path.join(data, "keys.json"), "w", encoding="utf-8") as f:
            json.dump([{"name": "ops", "key": "ad_admin_test", "role": "admin"},
                       {"name": "viewer", "key": "ad_read_test", "role": "read"}], f)
        self.eng = Engine(data, None)
        self.addCleanup(self.eng.con.close)
        self.eng.ingest(payload("s0", "search", 20, ts=__import__("time").time() - 3600))
        self.eng.refresh(force=True)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"api": Api(self.eng)}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.url = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(self, body, key):
        req = urllib.request.Request(self.url + "/api/apdex", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_admins_set_targets_and_nobody_else(self):
        self.assertEqual(self.post({"targets": {"search": {"latency_s": 5}}}, "ad_read_test")[0], 403)
        st, body = self.post({"targets": {"search": {"latency_s": 5}}}, "ad_admin_test")
        self.assertEqual((st, body["targets"]), (200, {"search": {"latency_s": 5.0}}))
        st, body = self.post({"targets": {"search": {"latency_s": 0}}}, "ad_admin_test")
        self.assertEqual(st, 400)
        self.assertIn("positive", body["error"])
        self.assertEqual(self.post({}, "ad_admin_test")[0], 400, "no targets at all is an error, not a wipe")
        req = urllib.request.Request(self.url + "/api/types?days=", headers={"Authorization": "Bearer ad_read_test"})
        with urllib.request.urlopen(req, timeout=30) as r:
            types = json.load(r)["types"]
        self.assertEqual(types[0]["apdex_target"], {"latency_s": 5.0}, "and the Task Types page shows it")


if __name__ == "__main__":
    unittest.main()
