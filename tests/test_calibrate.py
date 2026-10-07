"""Do the process scores predict success? (calibrate.py)

The scores and the overall's weights were chosen by hand. Calibration fits them to the outcomes somebody stated:
each score's AUC alone, a logistic regression out of sample against the default weights, and fitted weights that
the overall score uses when switched on and better. What must hold: only stated outcomes count; a score is judged
without the terms that restate the outcome (or the test is circular); a score that predicts nothing gets no
weight; a score too rare to judge keeps its default; the fitted weights are adopted only when they predict better
out of sample, and then the overall uses them, the same on a full rebuild as incrementally."""
import os
import random
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentdynamics import calibrate, config  # noqa: E402
from agentdynamics.analysis import SCORE_WEIGHTS  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api  # noqa: E402


def row(i, outcome, source="graded", errors=0.0, streak=0, ratio=1.0, reads=0, **kw):
    """A scored task as the store holds it."""
    t = {"id": f"t{i:04}", "started": i, "outcome": outcome, "outcome_source": source, "score": 70.0, "cost": 0.01,
         "subagent_cost": 0.0, "waste_cost": 0.0, "redundant_reads": reads, "duplicate_calls": 0, "max_edits_one_file": 0,
         "edits": 0, "explore_ratio": 0.0, "max_node_visits": 0, "pingpong": 0, "tool_calls": 5, "nodes": 0,
         "llm_errors": 0, "truncations": 0, "refusals": 0, "tool_error_rate": errors, "max_error_streak": streak,
         "api_errors": 0, "verified": None, "cache_hit": None, "llm_calls": 1, "max_context": 0, "compactions": 0,
         "interrupts": 0, "governed": 0, "cost_vs_baseline": ratio}
    t.update(kw)
    return t


def history(n=400, seed=7, noise_cost=True):
    """Tool errors decide success; how dear a task was (efficiency) and its re-reads (focus) are noise."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        bad = rng.random() < 0.35
        failed = (bad and rng.random() < 0.9) or rng.random() < 0.05
        out.append(row(i, "failed" if failed else "completed", errors=0.6 if bad else 0.0, streak=3 if bad else 0,
                       ratio=2 ** rng.uniform(0, 3) if noise_cost else 1.0, reads=rng.randint(0, 4)))
    return out


class AucTest(unittest.TestCase):
    def test_auc(self):
        self.assertEqual(calibrate.auc([1, 2, 3, 4], [0, 0, 1, 1]), 1.0)
        self.assertEqual(calibrate.auc([4, 3, 2, 1], [0, 0, 1, 1]), 0.0)
        self.assertEqual(calibrate.auc([5, 5, 5, 5], [0, 1, 0, 1]), 0.5, "ties count half")
        self.assertIsNone(calibrate.auc([1, 2], [1, 1]))


class FitTest(unittest.TestCase):
    def test_it_finds_what_predicts_and_weighs_it(self):
        rep = calibrate.fit(history())
        by = {c["name"]: c for c in rep["components"]}
        self.assertGreater(by["reliability"]["auc"], 0.85)
        self.assertLess(abs(by["focus"]["auc"] - 0.5), 0.08, "re-reads are noise here")
        self.assertEqual(rep["weights"]["focus"] + rep["weights"]["efficiency"], 0.0, "noise gets no say")
        self.assertEqual(rep["weights"]["reliability"], 0.6, "all that the scores it judged share")
        self.assertGreater(rep["auc_fitted"], rep["auc_default"] + calibrate.ADOPT_MARGIN)
        self.assertTrue(rep["adopt"])

    def test_scores_are_judged_without_the_terms_that_restate_the_outcome(self):
        """Reliability takes 40 off a failed task. Judged with that term, it 'predicts' failure perfectly from
        nothing but the outcome itself."""
        rng = random.Random(3)
        ts = [row(i, rng.choice(["completed", "failed"])) for i in range(200)]
        by = {c["name"]: c for c in calibrate.fit(ts)["components"]}
        self.assertEqual(by["reliability"]["auc"], 0.5)
        self.assertEqual(by["autonomy"]["auc"], 0.5, "nor rework's -35 on autonomy")

    def test_only_stated_outcomes_count(self):
        ts = history(100) + [row(1000 + i, "completed", source="inferred") for i in range(300)]
        self.assertEqual(calibrate.fit(ts)["n"], 100)
        self.assertEqual(calibrate.fit([row(i, "completed", source="feedback") for i in range(5)])["n"], 5)

    def test_the_fitted_auc_is_out_of_sample(self):
        """On outcomes that nothing predicts, a model judged on the tasks it was fitted to flatters itself (in-sample
        AUC 0.56-0.65 on these). Out of sample it is what it is: no better than a coin."""
        for seed in (0, 2, 4):
            rng = random.Random(seed)
            ts = [row(i, rng.choice(["completed", "failed"]), errors=rng.choice([0, 0.2, 0.6]), streak=rng.randint(0, 3),
                      ratio=2 ** rng.uniform(0, 3), reads=rng.randint(0, 4)) for i in range(80)]
            self.assertLess(calibrate.fit(ts)["auc_fitted"], 0.55, f"seed {seed}")

    def test_too_little_to_fit_says_so(self):
        rep = calibrate.fit(history(30))
        self.assertEqual((rep["enough"], rep["weights"], rep["adopt"]), (False, None, False))
        self.assertIsNotNone(rep["auc_default"], "the default's AUC is still reported")

    def test_when_the_defaults_already_rank_as_well_they_stay(self):
        rep = calibrate.fit(history(noise_cost=False))       # only reliability varies: nothing to improve
        self.assertFalse(rep["adopt"])

    def test_a_score_too_rare_to_judge_keeps_its_default_weight(self):
        w = calibrate.fit(history())["weights"]
        for name in ("verification", "context", "compliance"):          # never present in this history
            self.assertEqual(w[name], SCORE_WEIGHTS[name])
        self.assertAlmostEqual(sum(w.values()), 1.0, places=3)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def engine(self, scores=None):
        cfg = config.load(self.tmp)
        if scores is not None:
            cfg["scores"] = scores
        e = Engine(os.path.join(self.tmp, "data"), None, cfg=cfg)
        self.addCleanup(e.con.close)
        rng = random.Random(11)
        now = time.time() - 86400
        for i in range(160):
            bad = rng.random() < 0.4
            steps = [{"kind": "prompt", "ts": now + i * 60, "text": "help"},
                     {"kind": "llm", "ts": now + i * 60, "end_ts": now + i * 60 + 2, "model": "claude-sonnet-5",
                      "input_tokens": int(1000 * 2 ** rng.uniform(0, 6)), "output_tokens": 100}]
            steps += [{"kind": "tool", "ts": now + i * 60 + 3 + k, "end_ts": now + i * 60 + 4 + k, "name": "db",
                       "input": {"q": k}, "is_error": bad and k < 3} for k in range(5)]
            e.ingest({"id": f"r{i:03}", "project": "p", "workflow": "w", "status": "ok", "complete": True, "steps": steps})
            e._bad = getattr(e, "_bad", {})
            e._bad[f"r{i:03}#0"] = bad
        e.refresh(force=True)
        for tid, bad in sorted(e._bad.items()):
            e.grade(tid, "failed" if bad and rng.random() < 0.9 else "completed")
        e.refresh()
        return e

    def scores(self, e):
        return {r[0]: r[1] for r in e.con.execute("SELECT id, score FROM tasks")}

    def test_fitted_weights_are_used_when_switched_on_and_better(self):
        e = self.engine({"weights": "fitted"})
        before = self.scores(e)
        rep = e.fit_scores()
        self.assertTrue(rep["adopt"], rep)
        e.refresh()                                       # the next refresh scores with them
        after = self.scores(e)
        self.assertNotEqual(before, after)
        cal = Api(e).calibration({"days": ""})
        self.assertEqual(cal["in_use"]["mode"], "fitted")
        w = cal["in_use"]["weights"]
        self.assertEqual(max(w, key=w.get), "reliability", "tool errors decide these outcomes")
        inc = after
        e.refresh(force=True)
        self.assertEqual(inc, self.scores(e), "a full rebuild scores with the same weights")

    def test_off_by_default(self):
        e = self.engine()
        e.fit_scores()
        self.assertIsNone(e.score_weights())
        self.assertEqual(Api(e).calibration({"days": ""})["in_use"]["mode"], "default")

    def test_a_scoped_key_sees_its_own_fit_not_the_installs(self):
        e = self.engine({"weights": "fitted"})
        e.fit_scores()
        scoped = Api(e, projects=["p"]).calibration({"days": ""})
        self.assertNotIn("fit_n", scoped["in_use"])
        self.assertEqual(scoped["n"], 160)


if __name__ == "__main__":
    unittest.main()
