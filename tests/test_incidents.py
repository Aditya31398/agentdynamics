"""Incidents: security signals about one agent, grouped, judged by a person, alerted once.

What must hold: signals about one agent in one project make one incident, and each joins once however often
the analysis is rebuilt; a quiet gap or a verdict ends an incident; a destination that takes incidents hears
when one opens, escalates and is resolved, and not about history; a verdict takes an admin key, and a scoped
one only for its projects' incidents."""
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

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

from agentdynamics import alerts as alertmod, config, store  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402
from test_revocations import probing_run  # noqa: E402
from test_tripwires import WIRES, run  # noqa: E402

HOUR = 3600


def ungoverned(rid, t0, workflow="otel-agent", project="shop"):
    """A run from an agent without an Aegis name (OTLP, LangSmith): its tripwire touch has no agent."""
    p = run(rid, t0, "read", project=project)
    for s in p["steps"]:
        s.pop("agent", None)
        s.pop("governed", None)
    p["workflow"] = workflow
    return p


def warn_only(rid, t0, agent="support-bot", project="shop"):
    """Three refused calls, not in a row: `policy_denials` (a warning), not probing."""
    p = probing_run(rid, t0, agent=agent, denials=0, project=project)
    for i in range(3):
        p["steps"] += [{"kind": "tool", "name": "fs.read", "ts": t0 + 3 + 2 * i, "end_ts": t0 + 3.1 + 2 * i,
                        "agent": agent, "governed": True, "denied": True, "rule": "arg.forbid_matches"},
                       {"kind": "tool", "name": "kb.search", "ts": t0 + 4 + 2 * i, "end_ts": t0 + 4.1 + 2 * i,
                        "agent": agent, "governed": True, "rule": "kernel.admitted"}]
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.now = time.time()

    def engine(self, incidents=None, webhooks=None, wires=WIRES):
        cfg = config.load(self.tmp)
        cfg["enforcement"] = {"tripwires": dict(wires, revoke_minutes=0)} if wires else {}
        if incidents is not None:
            cfg["incidents"] = incidents
        if webhooks:
            cfg["alerts"] = {"webhooks": webhooks}
        e = Engine(self.tmp, None, cfg=cfg)
        e._clock = lambda: self.now
        self.addCleanup(e.con.close)
        return e

    def incidents(self, e, **q):
        return Api(e).incidents(q)["incidents"]


class GroupingTest(Base):
    def test_signals_about_one_agent_make_one_incident(self):
        e = self.engine()
        e.ingest(run("a", self.now - 900, "tool"))
        e.ingest(probing_run("p", self.now - 800, agent="support-bot"))
        e.ingest(run("b", self.now - 700, "read", agent="other-bot", project="billing"))
        e.ingest(ungoverned("o", self.now - 600))
        e.ingest(run("clean", self.now - 500))
        e.refresh(force=True)
        e.revoke(agent="support-bot", project="shop", reason="stop it", minutes=30)
        e.update_incidents()
        got = {(i["project"], i["subject"]): i for i in self.incidents(e)}
        self.assertEqual(sorted(got), [("billing", "other-bot"), ("shop", "support-bot"), ("shop", "workflow otel-agent")])
        mine = got[("shop", "support-bot")]
        self.assertEqual(mine["counts"], {"directive (operator)": 1, "probing": 1, "refused calls": 1, "tripwire": 1})
        self.assertEqual((mine["signals"], mine["tasks"], mine["severity"], mine["status"]), (4, 2, "critical", "open"))
        self.assertEqual(mine["what"], ["decoy tool secrets.dump"])
        self.assertEqual(mine["title"], "support-bot in shop: directive (operator), probing, refused calls, tripwire")
        self.assertAlmostEqual(mine["opened"], self.now - 900 + 5.1, delta=1)

    def test_each_signal_joins_once_however_often_it_is_rebuilt(self):
        e = self.engine()
        e.ingest(run("a", self.now - 900, "tool"))
        e.ingest(probing_run("p", self.now - 800, agent="support-bot"))
        e.refresh(force=True)
        before = self.incidents(e)
        e.refresh(force=True)
        e.update_incidents()
        e.con.close()
        again = self.engine()                   # a restart: a new process rebuilds the analysis
        again.refresh(force=True)
        after = self.incidents(again)
        self.assertEqual([(i["id"], i["signals"]) for i in after], [(i["id"], i["signals"]) for i in before])
        self.assertEqual(again.con.execute("SELECT COUNT(*) FROM incident_signals").fetchone()[0], 3)
        self.tmp = tempfile.mkdtemp()           # another store fed the same traffic derives the same incidents
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        other = self.engine()
        other.ingest(run("a", self.now - 900, "tool"))
        other.ingest(probing_run("p", self.now - 800, agent="support-bot"))
        other.refresh(force=True)
        self.assertEqual([i["id"] for i in self.incidents(other)], [i["id"] for i in before])

    def test_a_quiet_gap_or_a_verdict_ends_an_incident(self):
        e = self.engine({"gap_hours": 1})
        e.ingest(run("a", self.now - 5 * HOUR, "tool"))
        e.ingest(run("b", self.now - 2 * HOUR, "read"))       # three hours later: a new incident
        e.refresh(force=True)
        first = self.incidents(e)
        self.assertEqual([i["signals"] for i in first], [1, 1])
        latest = first[0]["id"]
        e.incident_verdict(first[1]["id"], "real", None, "ops")
        e.incident_verdict(latest, "false_alarm", "the decoy was hit by a test script", "ops")
        # within the gap of the latest, and from before its verdict -- but evidence nobody has judged
        e.ingest(run("c", self.now - 1.5 * HOUR, "read"))
        e.refresh()
        now = self.incidents(e)
        self.assertEqual(len(now), 3, "a signal after a verdict is news: a new incident")
        resolved = [i for i in now if i["id"] == latest][0]
        self.assertEqual((resolved["status"], resolved["verdict"], resolved["signals"], resolved["note"]),
                         ("resolved", "false_alarm", 1, "the decoy was hit by a test script"))
        self.assertEqual([i["status"] for i in now], ["open", "resolved", "resolved"], "open ones first")

    def test_a_directive_issued_before_the_verdict_joins_the_incident_it_answered(self):
        """Revoke from the incident page, then resolve it -- before the directive was picked up."""
        e = self.engine()
        e.ingest(run("a", self.now - 900, "tool"))
        e.refresh(force=True)
        iid = self.incidents(e)[0]["id"]
        store.add_revocation(e.con, "shop", "support-bot", f"incident {iid}", "operator", self.now - 10, self.now + 600)
        e.incident_verdict(iid, "real", None, "ops")
        e.update_incidents()
        got = self.incidents(e)
        self.assertEqual([(i["id"], i["status"], i["signals"]) for i in got], [(iid, "resolved", 2)])
        self.assertIn("directive (operator)", got[0]["counts"])
        self.now += 60                   # issued after the verdict: a new incident
        store.add_revocation(e.con, "shop", "support-bot", "again", "operator", self.now, self.now + 600)
        e.update_incidents()
        self.assertEqual(len(self.incidents(e)), 2)

    def test_a_directive_joins_its_incident_as_it_is_issued(self):
        e = self.engine()
        e.ingest(run("a", self.now - 900, "tool"))
        e.refresh(force=True)
        e.revoke(agent="support-bot", project="shop", reason="from the incident page", minutes=30)
        self.assertEqual(self.incidents(e)[0]["signals"], 2, "no wait for the alert tick")

    def test_the_agent_is_the_one_behind_the_evidence(self):
        """A run where the root agent does the work and a sub-agent touches the tripwire."""
        p = run("a", self.now - 900, agent="root")
        p["steps"] += [{"kind": "tool", "ts": self.now - 890 + i, "end_ts": self.now - 889 + i, "name": "kb.search",
                        "agent": "root", "governed": True, "rule": "kernel.admitted", "input": {"q": i}} for i in range(4)]
        p["steps"].append({"kind": "tool", "ts": self.now - 880, "end_ts": self.now - 879, "name": "secrets.dump",
                           "agent": "researcher", "governed": True, "denied": True, "rule": "capability.not_granted"})
        e = self.engine()
        e.ingest(p)
        e.refresh(force=True)
        self.assertEqual([i["subject"] for i in self.incidents(e)], ["researcher"])

    def test_resolved_ones_follow_the_window_open_ones_dont(self):
        e = self.engine()
        e.ingest(run("old-open", self.now - 3 * 86400, "tool", agent="a-bot"))
        e.ingest(run("old-done", self.now - 3 * 86400, "tool", agent="b-bot"))
        e.ingest(run("new-done", self.now - 600, "tool", agent="c-bot"))
        e.refresh(force=True)
        for i in self.incidents(e):
            if i["agent"] != "a-bot":
                e.incident_verdict(i["id"], "real")
        self.assertEqual(sorted(i["agent"] for i in self.incidents(e, days="1")), ["a-bot", "c-bot"])
        self.assertEqual(sorted(i["agent"] for i in self.incidents(e, days="1", status="resolved")), ["c-bot"])
        self.assertEqual(len(self.incidents(e)), 3)

    def test_which_rules_are_signals_is_configurable(self):
        e = self.engine({"rules": ["tripwire"]})
        e.ingest(run("a", self.now - 900, "tool"))
        e.ingest(probing_run("p", self.now - 800, agent="support-bot"))
        e.refresh(force=True)
        self.assertEqual([i["counts"] for i in self.incidents(e)], [{"tripwire": 1}])
        self.tmp = tempfile.mkdtemp()           # a store of its own
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        e2 = self.engine({"rules": []})
        e2.ingest(run("a", self.now - 900, "tool"))
        e2.refresh(force=True)
        self.assertEqual(self.incidents(e2), [], "rules = [] and no directives: no incidents")

    def test_evidence_and_actions(self):
        e = self.engine()
        e.ingest(dict(run("a", self.now - 900, "tool"), policy_version="support@v2#abc"))
        e.ingest(dict(warn_only("w", self.now - 800, agent="helper-bot"), policy_version="support@v2#abc"))
        e.refresh(force=True)
        by = {i["subject"]: i for i in self.incidents(e)}
        d = Api(e).incident(by["support-bot"]["id"])
        self.assertEqual([(s["name"], s["tripwire"]) for s in d["evidence"]], [("secrets.dump", "decoy tool secrets.dump")])
        self.assertEqual(d["actions"], [{"kind": "revoke", "agent": "support-bot", "project": "shop", "minutes": 60,
                                         "recommended": True}, {"kind": "tighten", "policy": "support@v2#abc"}])
        self.assertFalse(Api(e).incident(by["helper-bot"]["id"])["actions"][0]["recommended"],
                         "three refused calls are worth a look, not a revocation")
        e.revoke(agent="support-bot", project="shop", reason="tripwire", minutes=30)
        self.assertEqual(Api(e).incident(by["support-bot"]["id"])["actions"][0]["kind"], "revoked")
        self.assertIsNone(Api(e).incident("nope"))


class AlertTest(Base):
    def setUp(self):
        super().setUp()
        self.hook = {"name": "sec", "url": "http://127.0.0.1:9/", "kinds": ["incidents"]}

    def queued(self, e):
        out = []
        for r in store.outbox_pending(e.con):
            out += json.loads(r["body"]).get("incidents", [])
            store.outbox_done(e.con, r["id"])
        return [(i["action"], i["severity"], i["agent"]) for i in out]

    def test_opened_escalated_and_resolved_each_alert_once_and_history_never(self):
        e = self.engine(webhooks=[self.hook])
        e.ingest(run("old", self.now - 5 * HOUR, "tool", agent="old-bot"))
        e.refresh(force=True)
        self.assertEqual(self.queued(e), [], "history on the first refresh is not news")
        e.ingest(warn_only("w1", self.now - 300))
        e.refresh()
        self.assertEqual(self.queued(e), [("trigger", "warning", "support-bot")])
        e.ingest(warn_only("w2", self.now - 200))
        e.refresh()
        self.assertEqual(self.queued(e), [], "another signal of the same severity is not a new alert")
        e.ingest(run("t", self.now - 100, "tool"))
        e.refresh()
        self.assertEqual(self.queued(e), [("escalate", "critical", "support-bot")])
        iid = self.incidents(e)[0]["id"]
        e.incident_verdict(iid, "real", "prompt injection via ticket 4411", "ops")
        self.assertEqual(self.queued(e), [("resolve", "critical", "support-bot")])
        old = [i for i in self.incidents(e) if i["agent"] == "old-bot"][0]
        e.incident_verdict(old["id"], "false_alarm")
        self.assertEqual(self.queued(e), [("resolve", "critical", "old-bot")],
                         "history was never announced, but it is marked as such and resolves like the rest")

    def test_a_resolved_incident_a_directive_joins_is_not_paged_again(self):
        e = self.engine(webhooks=[self.hook])
        e.refresh(force=True)
        e.ingest(warn_only("w1", self.now - 300))
        e.refresh()
        self.assertEqual(self.queued(e), [("trigger", "warning", "support-bot")])
        iid = self.incidents(e)[0]["id"]
        store.add_revocation(e.con, "shop", "support-bot", "answering it", "operator", self.now - 5, self.now + 600)
        e.incident_verdict(iid, "real")
        self.assertEqual(self.queued(e), [("resolve", "warning", "support-bot")])
        e.update_incidents()             # the critical directive joins the resolved incident
        self.assertEqual(self.incidents(e)[0]["severity"], "critical")
        self.assertEqual(self.queued(e), [], "resolved is resolved: no escalation page")

    def test_a_directive_issued_elsewhere_opens_one_on_the_alert_tick(self):
        e = self.engine(webhooks=[self.hook])
        e.refresh(force=True)
        store.add_revocation(e.con, "shop", "rogue", "from the CLI", "operator", self.now - 5, self.now + 600)
        e.update_incidents()
        self.assertEqual(self.queued(e), [("trigger", "critical", "rogue")])

    def test_formats(self):
        inc = {"id": "abc123", "project": "shop", "agent": "support-bot", "workflow": None, "opened": self.now,
               "updated": self.now, "status": "resolved", "severity": "critical", "signals": 3, "verdict": "real",
               "note": None, "resolved_at": self.now}
        a = alertmod.from_incident(inc, "support-bot in shop: tripwire ×3", "resolve")
        pd = {"format": "pagerduty"}
        self.assertEqual(alertmod.render(pd, [a], "https://ad"),
                         [{"event_action": "resolve", "dedup_key": "agentdynamics/incident/abc123"}])
        t = alertmod.render(pd, [dict(a, action="trigger")], "https://ad")[0]
        self.assertEqual((t["payload"]["class"], t["payload"]["group"], t["links"][0]["href"]),
                         ("incident", "support-bot", "https://ad/#/incident/abc123"))
        slack = alertmod.render({"format": "slack"}, [a], "https://ad")[0]["text"]
        self.assertIn("*RESOLVED* (real) · support-bot in shop: tripwire ×3", slack)
        j = alertmod.render({"format": "json"}, [a], "https://ad")[0]
        self.assertEqual((j["incidents"][0]["action"], j["incidents"][0]["link"]), ("resolve", "https://ad/#/incident/abc123"))
        self.assertEqual(alertmod.destinations({"webhooks": [{"url": "http://x", "kinds": ["incidents"]}]})[1], [])


class ApiTest(Base):
    def setUp(self):
        super().setUp()
        self.eng = self.engine()
        self.eng.ingest(run("a", self.now - 900, "tool"))
        self.eng.ingest(run("b", self.now - 800, "tool", agent="billing-bot", project="billing"))
        self.eng.refresh(force=True)
        self.ids = {i["project"]: i["id"] for i in self.incidents(self.eng)}
        self.eng.cfg["auth"] = {"enabled": True, "keys": [
            {"name": "ops", "role": "admin", "key": "k-admin"}, {"name": "sre", "role": "read", "key": "k-read"},
            {"name": "app", "role": "ingest", "key": "k-ingest"},
            {"name": "shop-ops", "role": "admin", "key": "k-shop-admin", "projects": ["shop"]},
            {"name": "shop-read", "role": "read", "key": "k-shop", "projects": ["shop"]}]}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"api": Api(self.eng)}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.url = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(self, path, key, body=None):
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read() or b"{}")

    def test_a_verdict_takes_an_admin_key(self):
        shop = self.ids["shop"]
        for key in ("k-read", "k-ingest", "k-shop", "k-shop-admin"):    # an admin key can't be scoped at all
            self.assertEqual(self.call(f"/api/incidents/{shop}/verdict", key, {"verdict": "real"})[0], 403, key)
        self.assertEqual(self.call(f"/api/incidents/{shop}/verdict", "k-admin", {"verdict": "maybe"})[0], 400)
        st, r = self.call(f"/api/incidents/{shop}/verdict", "k-admin", {"verdict": "real", "note": "confirmed"})
        self.assertEqual((st, r["incident"]["status"], r["incident"]["resolved_by"]), (200, "resolved", "ops"))
        st, r = self.call(f"/api/incidents/{shop}/verdict", "k-admin", {"verdict": None})
        self.assertEqual((r["incident"]["status"], r["incident"]["verdict"]), ("open", None), "reopened")
        self.assertEqual(self.call("/api/incidents/nope/verdict", "k-admin", {"verdict": "real"})[0], 404)

    def test_a_scoped_key_reads_only_its_projects_incidents(self):
        st, r = self.call("/api/incidents", "k-shop")
        self.assertEqual([i["project"] for i in r["incidents"]], ["shop"])
        self.assertEqual(self.call(f"/api/incident/{self.ids['billing']}", "k-shop")[0], 404)
        st, r = self.call(f"/api/incident/{self.ids['shop']}", "k-shop")
        self.assertEqual((st, r["incident"]["subject"]), (200, "support-bot"))
        self.assertEqual(len(self.call("/api/incidents", "k-read")[1]["incidents"]), 2)


if __name__ == "__main__":
    unittest.main()
