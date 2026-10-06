"""Server-side revocation (#8), the server's half: directives, who may issue and read them, the probing
detector that issues them, and the alerts that say so. The in-process half -- applying a directive through a
real Aegis kernel -- is in test_aegis_integration.py."""
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

from agentdynamics import config, store  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Handler  # noqa: E402


def probing_run(rid, t0, agent="probe", denials=4, project="shop"):
    steps = [{"kind": "prompt", "ts": t0, "text": "read the secrets"},
             {"kind": "llm", "ts": t0, "end_ts": t0 + 1, "model": "claude-sonnet-5", "input_tokens": 100,
              "output_tokens": 10}]
    steps += [{"kind": "tool", "name": "fs.read", "ts": t0 + 2 + i, "end_ts": t0 + 2.1 + i, "agent": agent,
               "governed": True, "denied": True, "rule": "arg.forbid_matches", "guard": "args"} for i in range(denials)]
    return {"id": rid, "project": project, "workflow": "support", "steps": steps, "status": "ok"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.now = time.time()

    def engine(self, probing=None, webhooks=None):
        cfg = config.load(self.tmp)
        if probing:
            cfg["enforcement"] = {"probing": probing}
        if webhooks:
            cfg["alerts"] = {"webhooks": webhooks}
        e = Engine(self.tmp, None, cfg=cfg)
        e._clock = lambda: self.now
        self.addCleanup(e.con.close)
        return e


class DetectorTest(Base):
    PROBING = {"denials": 10, "runs": 3, "window_minutes": 30, "revoke_minutes": 60}

    def test_an_agent_probing_across_runs_is_revoked_once(self):
        e = self.engine(self.PROBING)
        for i in range(3):
            e.ingest(probing_run(f"r{i}", self.now - 900 + i * 60))
        e.refresh(force=True)
        ds = store.revocations(e.con, self.now, active=True)
        self.assertEqual([(d["project"], d["agent"], d["source"]) for d in ds], [("shop", "probe", "probing")])
        self.assertIn("12 denied calls across 3 runs", ds[0]["reason"])
        self.assertIn("arg.forbid_matches", ds[0]["reason"])
        self.assertAlmostEqual(ds[0]["expires"] - ds[0]["created"], 3600)
        # probing again past the threshold, all of it after the directive: still one while it is active
        self.now += 60
        for i in range(3):
            e.ingest(probing_run(f"again{i}", self.now - 30 + i))
        e.refresh()
        self.assertEqual(len(store.revocations(e.con, self.now)), 1, "an active directive is not issued twice")

    def test_it_is_off_unless_configured(self):
        e = self.engine()
        for i in range(5):
            e.ingest(probing_run(f"r{i}", self.now - 900 + i * 60))
        e.refresh(force=True)
        self.assertEqual(store.revocations(e.con, self.now), [])

    def test_one_run_or_old_denials_are_not_probing_across_runs(self):
        e = self.engine(self.PROBING)
        e.ingest(probing_run("one", self.now - 300, denials=20))           # the in-process Watchdog's case
        for i in range(3):
            e.ingest(probing_run(f"old{i}", self.now - 3 * 3600 + i * 60))  # outside the window
        e.refresh(force=True)
        self.assertEqual(store.revocations(e.con, self.now), [])

    def test_the_count_restarts_after_a_directive(self):
        e = self.engine(dict(self.PROBING, revoke_minutes=10))  # shorter than the 30-minute window
        for i in range(3):
            e.ingest(probing_run(f"r{i}", self.now - 900 + i * 60))
        e.refresh(force=True)
        self.now += 11 * 60              # the directive has expired, and its denials are still in the window...
        e.ingest(probing_run("quiet", self.now - 5, denials=0))
        e.refresh()
        self.assertEqual(len(store.revocations(e.con, self.now)), 1, "...but they were acted on: no second directive")


class ApiTest(Base):
    def setUp(self):
        super().setUp()
        self.eng = self.engine()
        self.eng.cfg["auth"] = {"enabled": True, "keys": [
            {"name": "ops", "role": "admin", "key": "k-admin"}, {"name": "app", "role": "ingest", "key": "k-ingest"},
            {"name": "sre", "role": "read", "key": "k-read"},
            {"name": "shop-app", "role": "ingest", "key": "k-shop", "projects": ["shop"]}]}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"api": __import__(
            "agentdynamics.server", fromlist=["Api"]).Api(self.eng)}))
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

    def test_only_an_admin_issues_or_clears_and_a_reason_is_required(self):
        body = {"agent": "probe", "project": "shop", "reason": "probing the payments API", "minutes": 30}
        for key in ("k-ingest", "k-read", "k-shop"):
            self.assertEqual(self.call("/api/revocations", key, body)[0], 403, key)
        self.assertEqual(self.call("/api/revocations", "k-admin", dict(body, reason=""))[0], 400)
        st, r = self.call("/api/revocations", "k-admin", body)
        self.assertEqual(st, 200)
        d = store.revocations(self.eng.con, time.time())[0]
        self.assertEqual((d["id"], d["agent"], d["project"], d["source"]), (r["id"], "probe", "shop", "operator"))
        self.assertIn("(ops)", d["reason"], "who issued it goes into the reason, and so into the audit log")
        self.assertEqual(self.call(f"/api/revocations/{r['id']}/clear", "k-read", {})[0], 403)
        self.assertEqual(self.call(f"/api/revocations/{r['id']}/clear", "k-admin", {})[0], 200)
        self.assertEqual(self.call(f"/api/revocations/{r['id']}/clear", "k-admin", {})[0], 404, "already cleared")

    def test_an_app_key_polls_what_applies_to_it(self):
        self.eng.revoke(agent="probe", project="shop", reason="shop agent", minutes=30)
        self.eng.revoke(agent="x", project="billing", reason="billing agent", minutes=30)
        self.eng.revoke(agent="y", project=None, reason="everywhere", minutes=30)
        self.eng.revoke(agent="z", project="shop", reason="over", minutes=-1)            # already expired
        cleared = self.eng.revoke(agent="w", project="shop", reason="cleared", minutes=30)
        self.eng.clear_revocation(cleared)
        st, r = self.call("/api/revocations?active=1&project=shop", "k-ingest")
        self.assertEqual(st, 200)
        self.assertEqual(sorted(d["agent"] for d in r["revocations"]), ["probe", "y"])
        # a key scoped to shop sees shop's and the install-wide one, never billing's, whatever it asks for
        st, r = self.call("/api/revocations?project=billing", "k-shop")
        self.assertEqual(sorted(d["agent"] for d in r["revocations"]), ["y"])
        st, r = self.call("/api/revocations", "k-shop")
        self.assertNotIn("x", [d["agent"] for d in r["revocations"]])
        statuses = {d["agent"]: d["status"] for d in self.call("/api/revocations", "k-read")[1]["revocations"]}
        self.assertEqual(statuses, {"probe": "active", "x": "active", "y": "active", "z": "expired", "w": "cleared"})


class RestrictApiTest(ApiTest):
    def test_an_admin_restricts_and_a_restriction_must_take_something(self):
        body = {"agent": "probe", "project": "shop", "reason": "keeps probing fs.read", "tools": ["fs.read", " "]}
        for key in ("k-ingest", "k-read", "k-shop"):
            self.assertEqual(self.call("/api/revocations", key, body)[0], 403, key)
        st, r = self.call("/api/revocations", "k-admin", body)
        self.assertEqual((st, r["kind"]), (200, "restrict"))
        st, r = self.call("/api/revocations?active=1&project=shop", "k-ingest")
        self.assertEqual([(d["kind"], d["spec"]) for d in r["revocations"]], [("restrict", {"tools": ["fs.read"]})])
        for bad in ({"tools": []}, {"kind": "restrict"}, {"budget": 1.5}, {"budget": -0.1}, {"kind": "expel"}):
            st, r = self.call("/api/revocations", "k-admin", {**body, "tools": None, **bad})
            self.assertEqual(st, 400, bad)
        st, r = self.call("/api/revocations", "k-admin", dict(body, tools=None, budget=0.25))
        self.assertEqual(st, 200)
        specs = [d["spec"] for d in self.call("/api/revocations?active=1", "k-read")[1]["revocations"]]
        self.assertCountEqual(specs, [{"tools": ["fs.read"]}, {"budget": 0.25}])      # same clock: any order

    def test_the_command(self):
        from agentdynamics.__main__ import main
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--data", self.tmp, "--claude-root", "", "revoke", "--agent", "probe",
                                   "--project", "shop", "--reason", "probing", "--tools", "fs.read,db.query"]), 0)
            self.assertEqual(main(["--data", self.tmp, "--claude-root", "", "revoke", "--agent", "probe",
                                   "--reason", "nothing", "--budget", "1"]), 2)
            self.assertEqual(main(["--data", self.tmp, "--claude-root", "", "revoke", "--list"]), 0)
        text = out.getvalue()
        self.assertIn("loses db.query, fs.read", text)
        self.assertIn("restrict: db.query, fs.read", text)
        self.assertIn("share of what remains", text)


class DurableColumnsTest(Base):
    @unittest.skipIf(os.environ.get("AGENTDYNAMICS_DB_URL"), "an old SQLite file; test_postgres covers the Postgres store")
    def test_a_store_from_before_restrict_gains_the_columns_and_keeps_its_directives(self):
        import sqlite3
        path = os.path.join(self.tmp, "agentdynamics.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE revocations (id PRIMARY KEY, project, agent, reason, source, created REAL, "
                    "expires REAL, cleared REAL)")
        con.execute("INSERT INTO revocations VALUES ('old1', 'shop', 'bot', 'from 0.8', 'operator', ?, ?, NULL)",
                    (self.now - 60, self.now + 600))
        con.commit()
        con.close()
        e = self.engine()
        self.assertEqual([(d["id"], d["kind"], d["spec"]) for d in store.revocations(e.con, self.now)],
                         [("old1", "revoke", None)])
        e.restrict(agent="bot", project="shop", reason="r", minutes=5, tools=["x"])
        self.assertEqual([d["kind"] for d in store.revocations(e.con, self.now)], ["restrict", "revoke"])


class KeepAliveTest(ApiTest):
    def test_a_body_a_route_doesnt_read_doesnt_become_the_next_request(self):
        """POST /api/revocations/<id>/clear and /api/refresh take no body, and the console sends "{}". Left in
        the socket, it prefixed the next request on the connection: "{}GET /api/..." -> 501."""
        import http.client
        rid = self.eng.revoke(agent="probe", project="shop", reason="x", minutes=30)
        c = http.client.HTTPConnection("127.0.0.1", int(self.url.rsplit(":", 1)[1]), timeout=30)
        self.addCleanup(c.close)
        auth = {"Authorization": "Bearer k-admin", "Content-Type": "application/json"}
        for path in (f"/api/revocations/{rid}/clear", "/api/refresh"):
            c.request("POST", path, body="{}", headers=auth)
            self.assertEqual(c.getresponse().read() and 200, 200)
            c.request("GET", "/api/revocations", headers=auth)
            r = c.getresponse()
            self.assertEqual(r.status, 200, f"after POST {path}: {r.read()[:120]}")
            r.read()


class AlertTest(Base):
    def test_a_new_directive_is_announced_once(self):
        from test_alerts import Receiver, check_pagerduty
        rx = Receiver()
        self.addCleanup(rx.close)
        e = self.engine(webhooks=[{"name": "j", "url": rx.url + "/json", "kinds": ["revocations"]},
                                  {"name": "s", "url": rx.url + "/slack", "format": "slack", "kinds": ["revocations"]},
                                  {"name": "p", "url": rx.url + "/pd", "format": "pagerduty", "routing_key": "RK",
                                   "kinds": ["revocations"], "min_severity": "critical"},
                                  {"name": "events-only", "url": rx.url + "/events"}])
        e.revoke(agent="old", project="shop", reason="before this process", minutes=30)
        self.assertEqual(e._alert_new_revocations(), 0, "a process's first look is history")
        self.now += 1
        rid = e.revoke(agent="probe", project="shop", reason="probing: 12 denied calls", minutes=60)
        self.assertEqual(e._alert_new_revocations(), 3)
        self.assertEqual(e._alert_new_revocations(), 0, "announced once")
        e.deliver_alerts()
        got = {p: b for p, b in rx.got}
        self.assertEqual([d["id"] for d in got["/json"]["revocations"]], [rid])
        self.assertIn("*REVOKED* · Revoked agent probe in project shop", got["/slack"]["text"])
        check_pagerduty(self, got["/pd"])
        self.assertEqual(got["/pd"]["dedup_key"], f"agentdynamics/revocation/{rid}")
        self.assertNotIn("/events", got, "a destination only for health events doesn't get it")


if __name__ == "__main__":
    unittest.main()
