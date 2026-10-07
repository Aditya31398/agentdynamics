"""Trust: what each agent's own behaviour says about it.

What must hold: evidence is attributed to the agent that produced it, not to others in the same task; old
evidence counts less; mistakes (errors, failed tasks) don't lower trust; refusals count as a rate, so volume
alone never does, and refusals the whole project hits are the policy's; little history is less certain than a
lot; a person's verdict wins -- a false alarm makes a task clean, a confirmation weighs it more."""
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
            "careful": {"calls": 5, "denied": 4, "touches": 0, "streak": 2, "misused": ["db.query", "fs.read"],
                        "rules": {"unknown": 4}},
            "researcher": {"calls": 3, "denied": 3, "touches": 1, "streak": 3, "misused": ["db.query", "fs.read"],
                           "rules": {"unknown": 3}},
            "root": {"calls": 5, "denied": 0, "touches": 0, "streak": 0, "misused": [], "rules": {}}})


class ScoreTest(unittest.TestCase):
    """Trust is a reputation: clean tasks and bad evidence (decayed, by severity) as counts on a prior of 19 clean
    and 1 bad, scored at the cautious end (10th percentile) of the share of clean tasks they imply."""

    def test_a_clean_agent_earns_trust_with_clean_work(self):
        new = by_agent([task("t1", bot={})])["bot"]
        self.assertEqual((new["band"], new["evidence"]), ("trusted", []))
        self.assertAlmostEqual(new["trust"], 89.4, places=1, msg="one clean task says little yet")
        seasoned = by_agent([task(f"t{i}", bot={}) for i in range(200)])["bot"]
        self.assertGreater(seasoned["trust"], 98, "two hundred say a lot")

    def test_tripwires_and_probing_weigh_by_severity_and_bands_follow(self):
        got = by_agent([task("t1", tw={"touches": 1}), task("t2", pr={"streak": 3, "denied": 3}),
                        task("t3", both={"touches": 1}), task("t4", both={"streak": 4, "denied": 4, "calls": 4})])
        self.assertEqual(got["tw"]["band"], "watch")
        self.assertEqual(got["tw"]["penalty"]["tripwire"], 10.0, "a tripwire task counts as ten bad ones")
        self.assertEqual(got["pr"]["penalty"]["probing"], 3.0)
        self.assertGreater(got["pr"]["trust"], got["tw"]["trust"], "probing is less certain evidence than a tripwire")
        two = by_agent([task("t5", two={"streak": 2, "denied": 2})])["two"]
        self.assertEqual((two["probing_tasks"], two["penalty"]["probing"]), (0, 0.0), "two refusals in a row isn't probing")
        self.assertEqual(got["both"]["band"], "low")
        self.assertEqual([r["agent"] for r in trust.score([task("a", x={"touches": 1}), task("b", y={})], {}, NOW, CONF)],
                         ["x", "y"], "lowest first")

    def test_the_same_evidence_says_more_about_an_agent_with_little_history(self):
        """One tripwire cost 40 points whether the agent had 3 tasks or 3,000. Volume is evidence too."""
        young = by_agent([task("tw", young={"touches": 1})] + [task(f"t{i}", young={}) for i in range(3)])["young"]
        old = by_agent([task("tw", old={"touches": 1})] + [task(f"t{i}", old={}) for i in range(3000)])["old"]
        self.assertEqual(young["band"], "watch")
        self.assertEqual(old["band"], "trusted")
        self.assertGreater(old["trust"], 99)

    def test_old_evidence_counts_less(self):
        got = by_agent([task("t0", 0, now={"touches": 1}), task("t1", 7, week={"touches": 1}),
                        task("t2", 14, fortnight={"touches": 1})])
        self.assertLess(got["now"]["trust"], got["week"]["trust"])
        self.assertLess(got["week"]["trust"], got["fortnight"]["trust"])
        self.assertEqual(got["week"]["penalty"]["tripwire"], 5.0, "half a week on")
        self.assertEqual(got["week"]["evidence"][0]["points"], 10.0, "the evidence keeps its full weight on record")

    def test_mistakes_are_not_misbehaviour(self):
        failed = by_agent([task("t1", outcome="failed", bot={}), task("t2", outcome="failed", bot={}), task("t3", bot={})])["bot"]
        fine = by_agent([task(f"t{i}", bot={}) for i in range(3)])["bot"]
        self.assertEqual((failed["trust"], failed["success_rate"]), (fine["trust"], 0.3333))

    def test_refusals_count_as_a_share_of_the_tasks_calls(self):
        few = by_agent([task("t", busy={"calls": 10, "denied": 1})])["busy"]
        many = by_agent([task("t", busy={"calls": 100, "denied": 10})])["busy"]
        self.assertEqual(few["trust"], many["trust"], "a busier task with the same share of refusals is no worse")
        self.assertEqual(few["penalty"]["denial_rate"], 0.1)
        steady = by_agent([task(f"t{i}", busy={"calls": 10, "denied": 1}) for i in range(500)])["busy"]
        self.assertGreater(steady["trust"], 85, "10% refused, steadily: trusted, a little below a clean agent")
        self.assertLess(steady["trust"], 91)

    def test_policy_friction_is_not_held_against_an_agent(self):
        """A rule every agent in the project runs into is the policy's problem, not the agents'."""
        tight = {"calls": 4, "denied": 2, "rules": {"capability.arg_max_len": 2}}
        tasks = [task(f"f{a}{i}", **{a: dict(tight)}) for a in ("a", "b", "c", "d") for i in range(3)]
        tasks += [task("own", a={"calls": 4, "denied": 2, "rules": {"capability.not_granted": 2}})]
        got = by_agent(tasks)
        self.assertEqual(got["b"]["penalty"]["denial_rate"], 0.0)
        self.assertEqual((got["b"]["friction_denied"], got["b"]["friction_rules"]), (6, ["capability.arg_max_len"]))
        self.assertEqual(got["a"]["penalty"]["denial_rate"], 0.5, "its own refusals still count")
        lone = by_agent([task("t", a=dict(tight)), task("u", b={})])
        self.assertEqual(lone["a"]["penalty"]["denial_rate"], 0.5, "one agent hitting a rule is not friction")

    def test_a_verdict_wins(self):
        tasks = [task("t1", bot={"touches": 1, "calls": 2, "denied": 2, "streak": 2})]
        clean = by_agent([task("t1", bot={})])["bot"]
        dismissed = by_agent(tasks, {("shop", "bot", "t1"): "false_alarm"})["bot"]
        self.assertEqual((dismissed["trust"], dismissed["denied"]), (clean["trust"], 0), "a false alarm is a clean task")
        confirmed = by_agent(tasks, {("shop", "bot", "t1"): "real"})["bot"]
        self.assertEqual(confirmed["penalty"]["tripwire"], 15.0)
        self.assertLess(confirmed["trust"], by_agent(tasks)["bot"]["trust"])
        self.assertEqual(confirmed["evidence"][0]["verdict"], "real")
        other = by_agent(tasks, {("billing", "bot", "t1"): "false_alarm"})["bot"]
        self.assertLess(other["trust"], clean["trust"], "another project's verdict is about another agent")

    def test_settings(self):
        conf = trust.settings({"trust": {"tripwire": 1, "half_life_days": 1, "unknown": 5}})
        self.assertEqual((conf["tripwire"], conf["half_life_days"]), (1.0, 1.0))
        self.assertNotIn("unknown", conf)
        self.assertGreater(by_agent([task("t", bot={"touches": 1})], conf=conf)["bot"]["trust"],
                           by_agent([task("t", bot={"touches": 1})])["bot"]["trust"])
        self.assertEqual(trust.verdicts([{"project": "p", "agent": "a", "task_id": "t", "verdict": "real"},
                                         {"project": "p", "agent": "a", "task_id": "t", "verdict": "false_alarm"}]),
                         {("p", "a", "t"): "real"}, "confirmed wins over dismissed")

    def test_lower_bound(self):
        self.assertAlmostEqual(trust.lower_bound(19, 1), 0.889, places=3)
        self.assertEqual(trust.lower_bound(1, 1000), 0.0)
        self.assertLess(trust.lower_bound(19, 1), trust.lower_bound(190, 10), "same share, more evidence: surer")


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
        # a tripwire 15 minutes ago (10 bad tasks) and half its calls refused, on a start of 19 clean and 1 bad
        self.assertAlmostEqual(before["support-bot"]["trust"], 51.2, delta=0.1)
        self.assertEqual((before["support-bot"]["band"], before["clean-bot"]["trust"]), ("watch", 89.4))
        iid = Api(self.e).incidents({})["incidents"][0]["id"]
        self.assertEqual(Api(self.e).incident(iid)["trust"]["agent"], "support-bot")
        self.e.incident_verdict(iid, "false_alarm", "a test script", "ops")
        self.assertEqual(self.trust()["support-bot"]["trust"], 89.4, "a false alarm: a clean task")
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
        self.assertEqual(sorted(lines), ['agentdynamics_agent_trust{project="billing",agent="clean-bot"} 89.4',
                                         f'agentdynamics_agent_trust{{project="shop",agent="support-bot"}} '
                                         f'{self.trust()["support-bot"]["trust"]}'])


if __name__ == "__main__":
    unittest.main()
