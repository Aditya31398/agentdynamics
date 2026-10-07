"""Ground truth for outcomes: what a business system says happened, and a checker that has to earn its say.

Most outcomes are inferred: "completed" means "didn't visibly fail", so an inferred success rate is an upper
bound. Three ways past that, tested here:
  * outcomes stated by your own key -- a business system knows its ticket and order ids, not task ids. A trace
    carries them as metadata; `POST /api/outcomes` with {"key": {"ticket_id": "T-1"}, "outcome": "rework"}
    settles the latest task with it (or all of them), now or when it arrives, within the stating key's projects;
  * the checker's grades, applied to inferred outcomes only when switched on and only once they agree with
    people's grades (Cohen's kappa over enough pairs) -- graded blind, from evidence that holds no outcome;
  * the success rate with a 95% interval, so 4 of 5 doesn't read like 800 of 1000.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from agentdynamics import analysis, config  # noqa: E402
from agentdynamics.api.base import wilson  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

NOW = time.time() - 3600


def run(rid, project="shop", error=False, ts=None, **metadata):
    ts = ts or NOW
    p = {"id": rid, "project": project, "workflow": "support", "status": "error" if error else "ok",
         "error": "boom" if error else None, "complete": True, "steps": [
             {"kind": "prompt", "ts": ts, "text": f"request {rid}"},
             {"kind": "llm", "ts": ts, "end_ts": ts + 1, "model": "claude-sonnet-5", "input_tokens": 100,
              "output_tokens": 20, "stop_reason": "end_turn"}]}
    if metadata:
        p["metadata"] = metadata
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def engine(self, checker_conf=None):
        cfg = config.load(self.tmp)
        if checker_conf is not None:
            cfg["checker"] = checker_conf
        e = Engine(os.path.join(self.tmp, "data"), None, cfg=cfg)
        self.addCleanup(e.con.close)
        return e

    @staticmethod
    def outcome(e, rid):
        e.refresh()
        return tuple(e.con.execute("SELECT outcome, outcome_source, outcome_reason FROM tasks WHERE run_id = ?",
                                   (rid,)).fetchone())


class ByKeyTest(Base):
    def test_a_reopened_ticket_settles_its_last_task(self):
        e = self.engine()
        e.ingest(run("first", ticket_id="T-1", ts=NOW - 600))
        e.ingest(run("second", ticket_id="T-1"))
        e.ingest(run("other", ticket_id="T-2"))
        e.refresh(force=True)
        e.grade_by_key("ticket_id", "T-1", "rework", "ticket reopened", "helpdesk")
        self.assertEqual(self.outcome(e, "second"), ("rework", "graded", "ticket_id=T-1: ticket reopened"))
        self.assertEqual(self.outcome(e, "first")[:2], ("completed", "inferred"), "only the last attempt, by default")
        self.assertEqual(self.outcome(e, "other")[:2], ("completed", "inferred"))
        e.grade_by_key("ticket_id", "T-1", "failed", "refund reversed", "billing", match="all")
        self.assertEqual(self.outcome(e, "first")[:2], ("failed", "graded"), "all: every task with the key")

    def test_it_waits_for_its_task_and_a_task_id_grade_still_wins(self):
        e = self.engine()
        e.grade_by_key("order_id", "42", "failed", "chargeback")
        e.ingest(run("late", order_id=42))                     # a number in the trace, a string in the request
        e.refresh(force=True)
        self.assertEqual(self.outcome(e, "late")[:2], ("failed", "graded"))
        e.grade("late#0", "completed", "manual review")
        self.assertEqual(self.outcome(e, "late"), ("completed", "graded", "manual review"))

    def test_clearing_by_key_falls_back(self):
        e = self.engine()
        e.ingest(run("r", ticket_id="T-9", error=True))
        e.grade_by_key("ticket_id", "T-9", "completed")
        self.assertEqual(self.outcome(e, "r")[:2], ("completed", "graded"))
        self.assertEqual(e.ungrade_by_key("ticket_id", "T-9"), 1)
        self.assertEqual(self.outcome(e, "r")[:2], ("failed", "inferred"))

    def test_a_scoped_statement_stops_at_its_projects_and_cant_overwrite_anothers(self):
        e = self.engine()
        e.ingest(run("mine", project="shop", ticket_id="T-5"))
        e.ingest(run("theirs", project="bank", ticket_id="T-5"))
        e.refresh(force=True)
        e.grade_by_key("ticket_id", "T-5", "failed", "from shop", projects=["shop"])
        e.grade_by_key("ticket_id", "T-5", "rework", "from bank", projects=["bank"])
        self.assertEqual(self.outcome(e, "mine")[:2], ("failed", "graded"))
        self.assertEqual(self.outcome(e, "theirs")[:2], ("rework", "graded"))

    def test_metadata_is_kept_from_every_source_and_masked(self):
        e = self.engine()
        e.ingest(run("sdk", ticket_id="T-7", customer="jane.doe@example.com", nested={"x": 1}))
        e.refresh(force=True)
        md = json.loads(e.con.execute("SELECT metadata FROM runs WHERE id = 'sdk'").fetchone()[0])
        self.assertEqual(md, {"ticket_id": "T-7", "customer": "[REDACTED]"}, "scalars only, secrets masked")

    def test_the_sdk_sends_its_trace_metadata(self):
        from agentdynamics import autotrace as at
        r = at._Run("support", prompt="hi", metadata={"ticket_id": "T-3", "version": "2.1", "obj": object()})
        p = r.payload()
        self.assertEqual(p["metadata"], {"ticket_id": "T-3", "version": "2.1"})
        self.assertEqual(p["version"], "2.1", "a version in metadata is the release recent baselines segment by")


class ByKeyHttpTest(Base):
    def setUp(self):
        super().setUp()
        self.e = self.engine()
        self.e.ingest(run("a1", project="alpha", ticket_id="T-1"))
        self.e.ingest(run("b1", project="bravo", ticket_id="T-1"))
        self.e.refresh(force=True)
        self.e.cfg["auth"] = {"enabled": True, "keys": [
            {"name": "helpdesk", "role": "ingest", "key": "k-in"},
            {"name": "alpha-only", "role": "ingest", "key": "k-alpha", "projects": ["alpha"]}]}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"api": Api(self.e)}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.url = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(self, body, key):
        req = urllib.request.Request(self.url + "/api/outcomes", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as ex:
            return ex.code, json.load(ex)

    def test_by_key_over_http_and_a_scoped_key_reaches_only_its_own(self):
        st, body = self.post([{"key": {"ticket_id": "T-1"}, "outcome": "rework", "reason": "reopened"}], "k-alpha")
        self.assertEqual((st, body["graded"]), (200, 1))
        self.assertEqual(self.outcome(self.e, "a1")[:2], ("rework", "graded"))
        self.assertEqual(self.outcome(self.e, "b1")[:2], ("completed", "inferred"), "bravo's task is out of its reach")
        st, _ = self.post([{"key": {"ticket_id": "T-1"}, "outcome": "failed"}], "k-in")
        self.assertEqual(self.outcome(self.e, "b1")[:2], ("failed", "graded"))
        self.assertEqual(self.post([{"key": {"ticket_id": "T-1"}, "outcome": None}], "k-alpha")[1]["cleared"], 1)

    def test_a_malformed_statement_is_a_400(self):
        for bad in ({"key": "T-1", "outcome": "failed"}, {"key": {"a": 1, "b": 2}, "outcome": "failed"},
                    {"key": {"a": {"x": 1}}, "outcome": "failed"}, {"key": {"a": "1"}, "outcome": "won"},
                    {"key": {"a": "1"}, "outcome": "failed", "match": "first"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.post([bad], "k-in")[0], 400)


def grade_answer(outcome):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(
        {"outcome": outcome, "confidence": "high", "reason": f"looks {outcome}"}))],
        stop_reason="end_turn", stop_details=None, model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=500, output_tokens=40))


class Grader:
    """client.beta.messages.create for grading: says what `verdict(evidence)` says, recording what it saw."""

    def __init__(self, verdict):
        self.verdict, self.seen = verdict, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        ev = json.loads(kw["messages"][0]["content"].split("<evidence>\n", 1)[1].rsplit("\n</evidence>", 1)[0])
        self.seen.append(ev)
        return grade_answer(self.verdict(ev))


class CheckerPromotionTest(Base):
    CONF = {"grade_outcomes": True, "apply_grades": True, "min_pairs": 6, "min_kappa": 0.6, "max_per_hour": 1000}

    def setup_history(self, e, n_people=8, n_inferred=5):
        for i in range(n_people):                     # half of them went wrong, and people said so
            e.ingest(run(f"p{i}", error=False, ticket=f"P{i}"))
        for i in range(n_inferred):
            e.ingest(run(f"q{i}", error=False))
        e.refresh(force=True)
        for i in range(n_people):
            e.grade(f"p{i}#0", "failed" if i % 2 else "completed", "people said")
        e.refresh()                                    # the checker reads the settled outcomes

    def test_the_grader_never_sees_an_outcome(self):
        e = self.engine(self.CONF)
        self.setup_history(e)
        g = Grader(lambda ev: "completed")
        e.run_checker(cl=g)
        self.assertTrue(g.seen)
        for ev in g.seen:
            self.assertFalse({"inferred_outcome", "outcome", "outcome_source", "outcome_reason"} & set(ev))
            self.assertNotIn("people said", json.dumps(ev))

    def test_people_graded_tasks_are_graded_first(self):
        e = self.engine(dict(self.CONF, max_per_hour=4))
        self.setup_history(e)
        e.run_checker(cl=Grader(lambda ev: "completed"))
        graded = sorted(r[0] for r in e.con.execute("SELECT task_id FROM model_grades"))
        self.assertEqual(graded, ["p0#0", "p1#0", "q0#0", "q1#0"],
                         "below min_pairs, half the budget goes to the calibration sample")

    def test_grades_that_agree_with_people_are_applied_to_inferred_outcomes(self):
        e = self.engine(self.CONF)
        self.setup_history(e)
        # a good grader: it can tell the failed ones (odd numbers) from the rest, and calls the inferred ones failed
        e.run_checker(cl=Grader(lambda ev: "failed" if ev["request"].startswith("request q") or
                                int(ev["request"][-1]) % 2 else "completed"))
        e.refresh()
        self.assertEqual(self.outcome(e, "q0")[:2], ("failed", "model"))
        self.assertIn("claude-opus-5-5", self.outcome(e, "q0")[2])
        self.assertEqual(self.outcome(e, "p1")[:2], ("failed", "graded"), "people's grades are never replaced")
        kpis = Api(e).overview({"days": ""})["kpis"]
        self.assertEqual(kpis["outcomes_by_source"].get("model"), 5)
        rec = Api(e).checker({})["outcomes"]["calibration"]
        self.assertEqual((rec["pairs"], rec["kappa"], rec["applied"]), (8, 1.0, True))

    def test_a_grader_that_disagrees_is_left_in_shadow(self):
        e = self.engine(self.CONF)
        self.setup_history(e)
        e.run_checker(cl=Grader(lambda ev: "completed"))     # says everything went fine: kappa 0
        e.refresh()
        self.assertEqual(self.outcome(e, "q0")[:2], ("completed", "inferred"))
        rec = Api(e).checker({})["outcomes"]["calibration"]
        self.assertEqual((rec["pairs"], rec["applied"]), (8, False))

    def test_switched_off_it_stays_in_shadow_however_good(self):
        e = self.engine(dict(self.CONF, apply_grades=False))
        self.setup_history(e)
        e.run_checker(cl=Grader(lambda ev: "failed" if ev["request"].startswith("request q") or
                                int(ev["request"][-1]) % 2 else "completed"))
        e.refresh()
        self.assertEqual(self.outcome(e, "q0")[:2], ("completed", "inferred"))

    def test_kappa(self):
        self.assertEqual(analysis.kappa([("a", "a"), ("b", "b")]), 1.0)
        self.assertEqual(analysis.kappa([("a", "a"), ("a", "b"), ("b", "a"), ("b", "b")]), 0.0)
        self.assertIsNone(analysis.kappa([("a", "a")] * 5), "all one label: agreement is chance, it can't say")
        self.assertIsNone(analysis.kappa([]))


class IntervalTest(unittest.TestCase):
    def test_wilson(self):
        self.assertEqual(wilson(800, 1000), [0.774, 0.824])
        lo, hi = wilson(4, 5)
        self.assertLess(lo, 0.4, "4 of 5 is weak evidence of an 80% rate")
        self.assertIsNone(wilson(0, 0))
        self.assertEqual(wilson(0, 10)[0], 0.0)


if __name__ == "__main__":
    unittest.main()
