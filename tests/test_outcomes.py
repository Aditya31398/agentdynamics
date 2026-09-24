"""Graded outcomes (#1): a stated outcome beats a guessed one, and the console can tell them apart.

Precedence, strongest first:
    grade set after the fact (API)  >  grade stated in the run (agentdynamics.outcome)
    >  recorded feedback  >  inference from errors, interrupts and corrections
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
import warnings
from http.server import ThreadingHTTPServer
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

NOW = time.time() - 600


def run(rid, *, error=False, feedback=None, grade=None, extra_steps=()):
    """A traced run whose inferred outcome is `failed` if error else `completed`."""
    steps = [
        {"kind": "prompt", "ts": NOW, "text": f"request {rid}"},
        {"kind": "llm", "ts": NOW, "end_ts": NOW + 1, "model": "claude-sonnet-5",
         "input_tokens": 100, "output_tokens": 20, "stop_reason": "end_turn"},
        *extra_steps,
    ]
    if grade:
        steps.append({"kind": "notice", "name": "outcome", "outcome": grade[0], "text": grade[1], "ts": NOW + 2})
    r = {"id": rid, "project": "grading", "workflow": "support", "steps": steps,
         "status": "error" if error else "ok", "error": "boom" if error else None, "complete": True}
    if feedback is not None:
        r["feedback"] = [{"key": "user", "score": feedback}]
    return r


class OutcomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.eng = Engine(self.data, None)

    def tearDown(self):
        self.eng.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def task(self, rid):
        self.eng.refresh()
        return dict(self.eng.con.execute("SELECT * FROM tasks WHERE run_id=?", (rid,)).fetchone())

    # ------------------------------------------------------------------ precedence
    def test_inference_is_labelled_as_inference(self):
        self.eng.ingest(run("ok"))
        self.eng.ingest(run("bad", error=True))
        self.eng.refresh(force=True)
        self.assertEqual((self.task("ok")["outcome"], self.task("ok")["outcome_source"]), ("completed", "inferred"))
        self.assertEqual((self.task("bad")["outcome"], self.task("bad")["outcome_source"]), ("failed", "inferred"))

    def test_feedback_overrides_inference_both_ways(self):
        # a run that errored but the user was happy with, and a clean run the user rejected
        self.eng.ingest(run("errored-but-liked", error=True, feedback=0.9))
        self.eng.ingest(run("clean-but-rejected", feedback=0.1))
        self.eng.refresh(force=True)
        a, b = self.task("errored-but-liked"), self.task("clean-but-rejected")
        self.assertEqual((a["outcome"], a["outcome_source"]), ("completed", "feedback"))
        self.assertEqual((b["outcome"], b["outcome_source"]), ("rework", "feedback"))
        self.assertIn("0.9", a["outcome_reason"])

    def test_a_grade_in_the_run_overrides_feedback(self):
        self.eng.ingest(run("r", feedback=0.95, grade=("failed", "refund issued to wrong account")))
        self.eng.refresh(force=True)
        t = self.task("r")
        self.assertEqual((t["outcome"], t["outcome_source"]), ("failed", "graded"))
        self.assertEqual(t["outcome_reason"], "refund issued to wrong account")

    def test_the_last_grade_in_a_run_wins(self):
        self.eng.ingest(run("r", extra_steps=[
            {"kind": "notice", "name": "outcome", "outcome": "failed", "text": "first", "ts": NOW + 1.5}],
            grade=("completed", "recovered")))
        self.eng.refresh(force=True)
        self.assertEqual(self.task("r")["outcome"], "completed")

    def test_an_api_grade_overrides_everything(self):
        self.eng.ingest(run("r", feedback=0.95, grade=("completed", "agent says fine")))
        self.eng.refresh(force=True)
        tid = self.task("r")["id"]
        self.eng.grade(tid, "failed", "QA: wrong refund amount", graded_by="qa-team")
        t = self.task("r")
        self.assertEqual((t["outcome"], t["outcome_source"], t["outcome_reason"]),
                         ("failed", "graded", "QA: wrong refund amount"))

    def test_clearing_a_grade_falls_back_to_feedback_then_inference(self):
        self.eng.ingest(run("fb", error=True, feedback=0.9))
        self.eng.ingest(run("plain", error=True))
        self.eng.refresh(force=True)
        for rid, fallback in (("fb", ("completed", "feedback")), ("plain", ("failed", "inferred"))):
            tid = self.task(rid)["id"]
            self.eng.grade(tid, "rework")
            self.assertEqual(self.task(rid)["outcome"], "rework")
            self.assertEqual(self.eng.ungrade(tid), 1)
            t = self.task(rid)
            self.assertEqual((t["outcome"], t["outcome_source"]), fallback, rid)

    # ------------------------------------------------------------------ it actually moves the numbers
    def test_a_grade_moves_success_rate_and_the_failed_flag(self):
        for i in range(4):
            self.eng.ingest(run(f"r{i}"))
        self.eng.refresh(force=True)
        api = Api(self.eng)
        self.assertEqual(api.overview({})["kpis"]["success_rate"], 1.0)
        self.eng.grade(self.task("r0")["id"], "failed", "wrong answer")
        self.eng.refresh()
        k = api.overview({})["kpis"]
        self.assertEqual(k["success_rate"], 0.75)
        self.assertEqual(k["outcomes_by_source"], {"graded": 1, "inferred": 3})

    # ------------------------------------------------------------------ the traps
    def test_a_new_grade_applies_without_any_other_traffic(self):
        """A grade dirties no run. refresh() returned early when nothing was dirty, so a grade sat
        unapplied until unrelated traffic arrived. Verified to fail without Engine._regrade."""
        self.eng.ingest(run("r"))
        self.eng.refresh(force=True)
        tid = self.task("r")["id"]
        self.eng.grade(tid, "failed")
        self.assertTrue(self.eng.refresh(), "refresh must re-finalize after a grade")
        self.assertEqual(self.task("r")["outcome"], "failed")
        self.assertFalse(self.eng.refresh(), "and must not keep re-finalizing afterwards")

    def test_a_grade_that_arrives_before_its_task_applies_when_the_task_does(self):
        """Ingestion is order-independent (invariant 4); grades are too."""
        self.eng.grade("early#0", "failed", "graded from a ticket before the trace landed")
        self.eng.refresh(force=True)
        self.eng.ingest(run("early"))
        t = self.task("early")
        self.assertEqual((t["outcome"], t["outcome_source"]), ("failed", "graded"))

    def test_grades_survive_a_schema_rebuild(self):
        """Grades are stated after the fact and exist nowhere else: they must not live in a derived
        table (invariant 3). A SCHEMA_VERSION change drops derived tables; grades must survive."""
        self.eng.ingest(run("r"))
        self.eng.refresh(force=True)
        tid = self.task("r")["id"]
        self.eng.grade(tid, "failed", "kept")
        self.eng.con.execute("PRAGMA user_version=1")     # pretend the schema is stale
        self.eng.con.commit()
        self.eng.con.close()
        self.eng = Engine(self.data, None)                 # reconnect: derived tables drop
        self.eng.refresh(force=True)
        t = self.task("r")
        self.assertEqual((t["outcome"], t["outcome_reason"]), ("failed", "kept"))

    def test_an_invalid_outcome_is_refused(self):
        with self.assertRaises(ValueError):
            self.eng.grade("x#0", "succeeded")
        with self.assertRaises(ValueError):
            self.eng.grade("x#0", "complete")          # the easy typo


class SdkOutcomeTest(unittest.TestCase):
    """agentdynamics.outcome must never raise into the agent (invariant 2)."""

    def setUp(self):
        import agentdynamics.autotrace as at
        self.at = at
        self.sent = []
        self._emit = at._emit
        at._emit = self.sent.append
        at._warned.clear()

    def tearDown(self):
        self.at._emit = self._emit

    def test_outcome_is_recorded_on_the_run(self):
        import agentdynamics as ad

        @ad.trace("handle")
        def handle():
            ad.outcome("failed", reason="escalated")

        handle()
        notes = [s for s in self.sent[-1]["steps"] if s.get("name") == "outcome"]
        self.assertEqual([(n["outcome"], n["text"]) for n in notes], [("failed", "escalated")])

    def test_the_trace_handle_has_outcome_like_it_has_feedback(self):
        import agentdynamics as ad
        with ad.trace("h") as t:
            t.outcome("rework", "customer asked again")
        self.assertTrue(any(s.get("outcome") == "rework" for s in self.sent[-1]["steps"]))

    def test_a_typo_warns_and_is_dropped_not_raised(self):
        import agentdynamics as ad

        @ad.trace("h")
        def handle():
            ad.outcome("complete")                      # not a valid outcome
            return "the agent kept going"

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            self.assertEqual(handle(), "the agent kept going")
        self.assertFalse(any(s.get("name") == "outcome" for s in self.sent[-1]["steps"]))

    def test_outside_a_trace_it_is_a_no_op(self):
        import agentdynamics as ad
        ad.outcome("failed")                             # must not raise
        self.assertEqual(self.sent, [])


class OutcomeHttpTest(unittest.TestCase):
    """The endpoints: single and bulk, clear with null, and read-only keys cannot grade."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.eng = Engine(os.path.join(cls.tmp, "data"), None)
        for i in range(3):
            cls.eng.ingest(run(f"h{i}"))
        cls.eng.refresh(force=True)
        cls.eng.cfg["auth"] = {"enabled": True, "keys": [
            {"name": "qa-team", "role": "ingest", "key": "k-ingest"},
            {"name": "dash", "role": "read", "key": "k-read"},
            {"name": "ops", "role": "admin", "key": "k-admin"}]}
        Handler.api = Api(cls.eng)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.eng.con.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def post(self, path, body, key):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def outcome(self, rid):
        r = self.eng.con.execute("SELECT outcome, outcome_source, outcome_reason FROM tasks WHERE run_id=?",
                                 (rid,)).fetchone()
        return tuple(r)

    def test_single_grade_records_who_graded(self):
        st, body = self.post(f"/api/tasks/{quote('h0#0', safe='')}/outcome",
                             {"outcome": "failed"}, "k-ingest")
        self.assertEqual((st, body["graded"]), (200, 1))
        self.assertEqual(self.outcome("h0"), ("failed", "graded", "graded by qa-team"))

    def test_bulk_grading_for_eval_pipelines(self):
        st, body = self.post("/api/outcomes", [
            {"task_id": "h1#0", "outcome": "rework", "reason": "eval: hallucinated order id"},
            {"task_id": "h2#0", "outcome": "completed", "reason": "eval: pass"}], "k-admin")
        self.assertEqual((st, body["graded"]), (200, 2))
        self.assertEqual(self.outcome("h1")[:2], ("rework", "graded"))
        self.assertEqual(self.outcome("h2")[2], "eval: pass")

    def test_null_clears_a_grade(self):
        self.post("/api/outcomes", [{"task_id": "h2#0", "outcome": "failed"}], "k-ingest")
        st, body = self.post("/api/outcomes", [{"task_id": "h2#0", "outcome": None}], "k-ingest")
        self.assertEqual((st, body["cleared"]), (200, 1))
        self.assertEqual(self.outcome("h2")[:2], ("completed", "inferred"))

    def test_a_read_only_key_cannot_grade(self):
        st, _ = self.post("/api/outcomes", [{"task_id": "h0#0", "outcome": "completed"}], "k-read")
        self.assertEqual(st, 403)

    def test_an_invalid_outcome_is_a_400_not_a_500(self):
        st, body = self.post("/api/outcomes", [{"task_id": "h0#0", "outcome": "succeeded"}], "k-ingest")
        self.assertEqual(st, 400)
        self.assertIn("outcome must be one of", body["error"])


if __name__ == "__main__":
    unittest.main()
