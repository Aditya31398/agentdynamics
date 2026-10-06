"""Trust: what each agent's own behaviour says about it.

What must hold: evidence is attributed to the agent that produced it, not to others in the same task; old
evidence counts less; mistakes (errors, failed tasks) don't lower trust; refusals count as a rate, so volume
alone never does; a person's verdict wins -- a false alarm removes its evidence, a confirmation weighs it more."""
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from agentdynamics import config, trust  # noqa: E402
from agentdynamics.analysis import agent_evidence  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api  # noqa: E402
from test_tripwires import WIRES, run  # noqa: E402

DAY = 86400
NOW = 1_800_000_000.0
CONF = trust.settings({})


def task(tid, age_days=0, outcome="completed", project="shop", **agents):
    ev = {a: dict({"calls": 4, "denied": 0, "touches": 0, "streak": 0}, **v) for a, v in agents.items()}
    return {"id": tid, "project": project, "started": NOW - age_days * DAY - 60, "ended": NOW - age_days * DAY,
            "outcome": outcome, "agents": ev}


def by_agent(tasks, judged=None, conf=CONF):
    return {a["agent"]: a for a in trust.score(tasks, judged or {}, NOW, conf)}


class EvidenceTest(unittest.TestCase):
    def test_each_agent_answers_for_its_own_steps(self):
        steps = [{"kind": "tool", "name": "kb.search", "agent": "root"} for _ in range(4)]
        steps += [{"kind": "tool", "name": "fs.read", "agent": "researcher", "denied": True},
                  {"kind": "tool", "name": "kb.search", "agent": "root"},       # another agent's success
                  {"kind": "tool", "name": "db.query", "agent": "researcher", "denied": True},   # a different tool
                  {"kind": "tool", "name": "fs.read", "agent": "researcher", "denied": True},
                  {"kind": "llm", "agent": "researcher", "tripwire": "canary k"},
                  {"kind": "tool", "name": "untraced"}]
        # its own success ends a run of refusals: two, a success, two more is never three in a row
        steps += [{"kind": "tool", "name": n, "agent": "careful", "denied": d}
                  for n, d in (("fs.read", True), ("fs.read", True), ("kb.search", False), ("fs.read", True), ("db.query", True))]
        self.assertEqual(agent_evidence(steps), {
            "careful": {"calls": 5, "denied": 4, "touches": 0, "streak": 2, "misused": ["db.query", "fs.read"]},
            "researcher": {"calls": 3, "denied": 3, "touches": 1, "streak": 3, "misused": ["db.query", "fs.read"]},
            "root": {"calls": 5, "denied": 0, "touches": 0, "streak": 0, "misused": []}})


class ScoreTest(unittest.TestCase):
    def test_a_clean_agent_is_fully_trusted(self):
        a = by_agent([task("t1", bot={})])["bot"]
        self.assertEqual((a["trust"], a["band"], a["evidence"]), (100.0, "trusted", []))

    def test_tripwires_and_probing_cost_points_and_bands_follow(self):
        got = by_agent([task("t1", tw={"touches": 1}), task("t2", pr={"streak": 3, "denied": 3}),
                        task("t3", both={"touches": 1}), task("t4", both={"streak": 4, "denied": 4, "calls": 4})])
        self.assertEqual((got["tw"]["trust"], got["tw"]["band"]), (60.0, "watch"))
        self.assertEqual(got["pr"]["penalty"]["probing"], 15.0)
        two = by_agent([task("t5", two={"streak": 2, "denied": 2})])["two"]
        self.assertEqual((two["probing_tasks"], two["penalty"]["probing"]), (0, 0.0), "two refusals in a row isn't probing")
        self.assertEqual(got["both"]["band"], "low")
        self.assertEqual([r["agent"] for r in trust.score([task("a", x={"touches": 1}), task("b", y={})], {}, NOW, CONF)],
                         ["x", "y"], "lowest first")

    def test_old_evidence_counts_less(self):
        got = by_agent([task("t1", 7, week={"touches": 1}), task("t2", 14, fortnight={"touches": 1})])
        self.assertAlmostEqual(got["week"]["trust"], 80.0, places=1)
        self.assertAlmostEqual(got["fortnight"]["trust"], 90.0, places=1)
        self.assertEqual(got["week"]["evidence"][0]["points"], 40.0, "the evidence keeps its full weight on record")

    def test_mistakes_are_not_misbehaviour(self):
        a = by_agent([task("t1", outcome="failed", bot={}), task("t2", outcome="failed", bot={}), task("t3", bot={})])["bot"]
        self.assertEqual((a["trust"], a["success_rate"]), (100.0, 0.3333))

    def test_refusals_are_a_rate_so_volume_alone_costs_nothing(self):
        one = by_agent([task("t", busy={"calls": 10, "denied": 1})])["busy"]
        many = by_agent([task(f"t{i}", busy={"calls": 10, "denied": 1}) for i in range(100)])["busy"]
        self.assertEqual(one["trust"], many["trust"])
        self.assertEqual(one["trust"], 98.0)

    def test_a_verdict_wins(self):
        tasks = [task("t1", bot={"touches": 1, "calls": 2, "denied": 2, "streak": 2})]
        dismissed = by_agent(tasks, {("shop", "bot", "t1"): "false_alarm"})["bot"]
        self.assertEqual((dismissed["trust"], dismissed["denied"]), (100.0, 0), "none of a false alarm counts")
        confirmed = by_agent(tasks, {("shop", "bot", "t1"): "real"})["bot"]
        self.assertEqual(confirmed["penalty"]["tripwire"], 60.0)
        self.assertEqual(confirmed["evidence"][0]["verdict"], "real")
        other = by_agent(tasks, {("billing", "bot", "t1"): "false_alarm"})["bot"]
        self.assertLess(other["trust"], 100, "another project's verdict is about another agent")

    def test_settings(self):
        conf = trust.settings({"trust": {"tripwire": 10, "half_life_days": 1, "unknown": 5}})
        self.assertEqual((conf["tripwire"], conf["half_life_days"]), (10.0, 1.0))
        self.assertNotIn("unknown", conf)
        self.assertEqual(by_agent([task("t", bot={"touches": 1})], conf=conf)["bot"]["trust"], 90.0)
        self.assertEqual(trust.verdicts([{"project": "p", "agent": "a", "task_id": "t", "verdict": "real"},
                                         {"project": "p", "agent": "a", "task_id": "t", "verdict": "false_alarm"}]),
                         {("p", "a", "t"): "real"}, "confirmed wins over dismissed")


class EngineTest(unittest.TestCase):
    """End to end: a tripwire touched, an incident opened, the verdict given -- and the agent's trust moves."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.now = time.time() - 14 * DAY          # the engine's clock, a fortnight behind the wall's
        cfg = config.load(self.tmp)
        cfg["enforcement"] = {"tripwires": dict(WIRES, revoke_minutes=0)}
        self.e = Engine(self.tmp, None, cfg=cfg)
        self.e._clock = lambda: self.now
        self.addCleanup(self.e.con.close)
        self.e.ingest(run("a", self.now - 900, "tool"))
        self.e.ingest(run("b", self.now - 800, agent="clean-bot", project="billing"))
        self.e.refresh(force=True)

    def trust(self, **q):
        return {a["agent"]: a for a in Api(self.e).trust(q)["agents"]}

    def test_a_verdict_moves_trust(self):
        before = self.trust()
        # 100 - 40 (a tripwire 15 minutes ago, by the engine's clock) - 20 x 1/2 of its calls refused
        self.assertAlmostEqual(before["support-bot"]["trust"], 50.0, delta=0.1)
        self.assertEqual((before["support-bot"]["band"], before["clean-bot"]["trust"]), ("watch", 100.0))
        iid = Api(self.e).incidents({})["incidents"][0]["id"]
        self.assertEqual(Api(self.e).incident(iid)["trust"]["agent"], "support-bot")
        self.e.incident_verdict(iid, "false_alarm", "a test script", "ops")
        self.assertEqual(self.trust()["support-bot"]["trust"], 100.0)
        self.e.incident_verdict(iid, "real", None, "ops")
        self.assertLess(self.trust()["support-bot"]["trust"], before["support-bot"]["trust"])
        self.assertEqual(list(self.trust(project="billing")), ["clean-bot"])

    def test_a_low_trust_agent_loses_the_tools_it_misused(self):
        from agentdynamics import store
        from test_incidents import warn_only
        self.e.ingest(run("a2", self.now - 700, "tool"))        # a second touch: support-bot is well below 50
        self.e.ingest(warn_only("w", self.now - 600, agent="helper-bot"))   # refused calls only: trust 90
        self.e.refresh()
        self.assertLess(self.trust()["support-bot"]["trust"], 40)
        self.assertEqual(store.revocations(self.e.con, self.now), [], "off unless configured")
        self.e.cfg["enforcement"]["trust"] = {"restrict_below": 60, "minutes": 60}
        self.e.ingest(run("c", self.now - 500))           # new traffic, so the refresh runs the detector
        self.e.refresh()
        ds = store.revocations(self.e.con, self.now, active=True)
        self.assertEqual([(d["agent"], d["kind"], d["source"], d["spec"]) for d in ds],
                         [("support-bot", "restrict", "trust", {"tools": ["secrets.dump"]})],
                         "helper-bot, at 90, is above the threshold")
        self.assertRegex(ds[0]["reason"], r"^trust \d+(\.\d)? < 60: takes away secrets\.dump$")
        self.e.ingest(run("d", self.now - 400))
        self.e.refresh()
        self.assertEqual(len(store.revocations(self.e.con, self.now)), 1, "not issued again while it holds")
        incident = [i for i in Api(self.e).incidents({})["incidents"] if i["agent"] == "support-bot"][0]
        sources = [s["detail"].get("source") for s in Api(self.e).incident(incident["id"])["signals"]]
        self.assertNotIn("trust", sources, "an automatic renewal isn't an incident signal")
        # a person says it was a false alarm: once the restriction runs out, it isn't renewed
        self.e.incident_verdict(incident["id"], "false_alarm", None, "ops")
        self.now += 2 * 3600
        self.e.ingest(run("e", self.now - 60))
        self.e.refresh()
        self.assertEqual(store.revocations(self.e.con, self.now, active=True), [])

    def test_prometheus(self):
        lines = [ln for ln in Api(self.e).prometheus().splitlines() if ln.startswith("agentdynamics_agent_trust{")]
        self.assertEqual(sorted(lines), ['agentdynamics_agent_trust{project="billing",agent="clean-bot"} 100.0',
                                         f'agentdynamics_agent_trust{{project="shop",agent="support-bot"}} '
                                         f'{self.trust()["support-bot"]["trust"]}'])


if __name__ == "__main__":
    unittest.main()
