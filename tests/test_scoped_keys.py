"""Project-scoped API keys: a key scoped to one team's projects must never see another team's data.

The failure mode is quiet: one endpoint that forgets to filter. So scoping isn't left to endpoints: a
scoped request reads through a connection whose `tasks`, `runs`, `steps` and `events` are views limited to
its projects (store.connect_reader). This test holds it to that. It plants a marker in another team's
data, reads the route list out of server.py itself (so a route added later is covered without anyone
remembering to add it here), calls every one with a scoped key and several query variants, and requires
the marker never appears. It also requires the unscoped key *does* see the marker, or the test would pass
for the wrong reason.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import quote

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agentdynamics import slo  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

T = time.time() - 1800
MARK = "zzbravo9f2"                    # the other team's project; appears in every bravo row
SECRET = "BRAVO-SECRET-7f3a"           # and in its prompts, tool names and errors


def run(rid, project, workflow, prompt, tool, error=False):
    steps = [{"kind": "prompt", "ts": T, "text": prompt},
             {"kind": "span", "ts": T, "end_ts": T + 4, "span_kind": "node", "name": f"{workflow}_node",
              "node": f"{workflow}_node"},
             {"kind": "llm", "ts": T, "end_ts": T + 1, "model": "claude-sonnet-5", "input_tokens": 900,
              "output_tokens": 120, "stop_reason": "end_turn"},
             {"kind": "tool", "ts": T + 1, "end_ts": T + 2, "name": tool, "is_error": error, "governed": True,
              "grant_depth": 0, "input": {"q": prompt}, "error": f"{prompt} failed" if error else None}]
    return {"id": rid, "project": project, "workflow": workflow, "thread_id": f"{project}-thread",
            "steps": steps, "status": "error" if error else "ok", "error": f"{prompt} crashed" if error else None,
            "policy_version": f"{project}-policy@v1#abc", "policy": {"name": f"{project}-policy", "doc": {
                "name": f"{project}-policy", "tools": {"allow": [{"name": tool}]}}},
            "feedback": [{"key": "user", "score": 0.2}]}


class ScopedKeysTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.eng = Engine(os.path.join(cls.tmp, "data"), None)
        for i in range(4):
            cls.eng.ingest(run(f"alpha-{i}", "alpha", "alpha_flow", f"alpha request {i}", "alpha_tool", error=i == 0))
            cls.eng.ingest(run(f"{MARK}-{i}", MARK, f"{MARK}_flow", f"{SECRET} request {i}", f"{MARK}_tool", error=i == 0))
        cls.eng.refresh(force=True)
        # install-wide config can name the other project too: an SLO on it, and one on its workflow
        slo.save(cls.eng.data_dir, slo.DEFAULT_SLOS + [
            {"id": "b1", "name": f"{MARK} latency", "metric": "p95_seconds", "op": "<=", "target": 60,
             "window_days": 7, "scope": {"project": MARK}},
            {"id": "b2", "name": "flow success", "metric": "success_rate", "op": ">=", "target": 0.9,
             "window_days": 7, "scope": {"workflow": f"{MARK}_flow"}},
            {"id": "a1", "name": "alpha success", "metric": "success_rate", "op": ">=", "target": 0.9,
             "window_days": 7, "scope": {"project": "alpha"}}])
        cls.eng.cfg["auth"] = {"enabled": True, "keys": [
            {"name": "ops", "role": "admin", "key": "k-admin"},
            {"name": "team-alpha", "role": "read", "key": "k-alpha", "projects": ["alpha"]},
            {"name": "alpha-ingest", "role": "ingest", "key": "k-alpha-ingest", "projects": ["alpha"]},
            {"name": "empty", "role": "read", "key": "k-none", "projects": []},
            {"name": "bad-admin", "role": "admin", "key": "k-bad", "projects": ["alpha"]}]}
        Handler.api = Api(cls.eng)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        src = open(os.path.join(ROOT, "agentdynamics", "server.py"), encoding="utf-8").read()
        cls.routes = sorted(set(re.findall(r'"(/api/[a-z/]+|/metrics)"', src)))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.eng.con.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, path, key, body=None):
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def variants(self):
        bravo_task = quote(f"{MARK}-1#0", safe="")
        for r in self.routes:
            if r.endswith("/"):
                continue
            for qs in ("", "?days=", f"?project={MARK}", "?sub=1&days=", f"?name={MARK}_flow",
                       f"?type={MARK}_flow", "?group=project&metrics=tasks,cost",
                       f"?dim=project&a=alpha&b={MARK}", f"?policy={MARK}-policy@v1%23abc"):
                yield r + qs
        yield f"/api/task/{bravo_task}"
        yield f"/api/workflow?name={MARK}_flow"

    def test_a_scoped_key_never_sees_another_projects_data(self):
        leaks, admin_saw = [], 0
        for path in self.variants():
            st, body = self.call(path, "k-alpha")
            if MARK in body or SECRET in body:
                leaks.append(f"{path} -> {st}")
            st_a, body_a = self.call(path, "k-admin")
            admin_saw += (MARK in body_a or SECRET in body_a)
        self.assertEqual(leaks, [], "a key scoped to 'alpha' saw the other project's data")
        # the check has to be able to fail: the unscoped key sees the other project on many routes
        self.assertGreater(admin_saw, 10, "the fixture's other project should be visible to an unscoped key")
        self.assertGreater(len(self.routes), 20, "route discovery from server.py found too few routes")

    def test_it_still_sees_its_own_data(self):
        st, body = self.call("/api/overview?days=", "k-alpha")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["kpis"]["tasks"], 4)
        st, body = self.call("/api/filters", "k-alpha")
        f = json.loads(body)
        self.assertEqual([p["project"] for p in f["projects"]], ["alpha"])
        self.assertEqual((f["runs"], f["task_count"]), (4, 4), "counts are the scoped ones, not the install's")
        self.assertEqual(f["scope"], ["alpha"])          # the console labels its project filter with it
        self.assertIsNone(json.loads(self.call("/api/filters", "k-admin")[1])["scope"])
        st, body = self.call(f"/api/task/{quote('alpha-1#0', safe='')}", "k-alpha")
        self.assertEqual(st, 200)
        slos = {s["id"] for s in json.loads(self.call("/api/slos", "k-alpha")[1])["slos"]}
        self.assertEqual(slos - {s["id"] for s in slo.DEFAULT_SLOS}, {"a1"}, "its own project's SLO, not bravo's")

    def test_another_projects_task_is_not_found_rather_than_forbidden(self):
        st, _ = self.call(f"/api/task/{quote(MARK + '-1#0', safe='')}", "k-alpha")
        self.assertEqual(st, 404)        # the same answer as for an id that doesn't exist

    def test_install_wide_endpoints_need_an_unscoped_key(self):
        for p in ("/metrics", "/api/sources", "/api/config", "/api/alerts"):
            with self.subTest(p):
                self.assertEqual(self.call(p, "k-alpha")[0], 403)
                self.assertEqual(self.call(p, "k-admin")[0], 200)

    def test_a_refresh_reports_no_install_wide_count_to_a_scoped_key(self):
        self.assertNotIn("changed", json.loads(self.call("/api/refresh", "k-alpha", {})[1]))
        self.assertIn("changed", json.loads(self.call("/api/refresh", "k-admin", {})[1]))

    def test_an_empty_project_list_sees_nothing(self):
        st, body = self.call("/api/overview?days=", "k-none")
        self.assertEqual((st, json.loads(body)["kpis"]["tasks"]), (200, 0))

    def test_an_admin_key_cannot_be_scoped(self):
        st, body = self.call("/api/overview", "k-bad")
        self.assertEqual(st, 403)
        self.assertIn("cannot be scoped", body)

    def test_whoami_reports_the_scope(self):
        self.assertEqual(json.loads(self.call("/api/whoami", "k-alpha")[1]),
                         {"role": "read", "projects": ["alpha"]})
        self.assertIsNone(json.loads(self.call("/api/whoami", "k-admin")[1])["projects"])

    def test_a_scoped_key_grades_only_its_own_tasks(self):
        st, body = self.call("/api/outcomes", "k-alpha-ingest", [
            {"task_id": "alpha-2#0", "outcome": "failed"}, {"task_id": f"{MARK}-2#0", "outcome": "failed"}])
        self.assertEqual(st, 403)
        self.assertEqual(json.loads(body)["task_ids"], [f"{MARK}-2#0"])
        # all or nothing: the alpha grade in the refused request was not applied either
        self.assertEqual(self.eng.con.execute("SELECT COUNT(*) FROM grades").fetchone()[0], 0)
        st, _ = self.call("/api/outcomes", "k-alpha-ingest", [{"task_id": "alpha-2#0", "outcome": "failed"}])
        self.assertEqual(st, 200)
        self.eng.ungrade("alpha-2#0")

    def test_a_policy_check_sees_only_its_projects_calls(self):
        cand = {"name": "c", "version": 1, "tools": {"allow": [{"name": "alpha_tool"}]}}
        st, body = self.call("/api/policy/check", "k-alpha", {"candidate": cand})
        if st == 400 and "needs Aegis" in body:
            self.skipTest("aegis-kernel not installed")
        cov = json.loads(body)["coverage"]
        self.assertNotIn(MARK, body)
        self.assertEqual({r["tool"] for r in cov["by_tool"]}, {"alpha_tool"})


class IngestScopeTest(unittest.TestCase):
    """A scoped ingest key writes only into its own projects, and never over another project's data."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.eng = Engine(os.path.join(self.tmp, "data"), None)
        e = self.eng
        # another team's data, written with an unscoped key
        e.ingest(run("bravo-run-1", "bravo", "bravo_flow", "bravo asks", "bravo_tool"))
        e.ingest_otlp(otlp_trace("b" * 32, "bravo-svc", "bravo"), "application/json")
        e.ingest_langsmith([{"id": LS_RUN, "trace_id": LS_RUN, "name": "Chain", "run_type": "chain",
                             "session_name": "bravo", "start_time": iso(T), "end_time": iso(T + 1)}], [])
        e.refresh(force=True)
        e.cfg["auth"] = {"enabled": True, "keys": [
            {"name": "all", "role": "ingest", "key": "k-all"},
            {"name": "alpha", "role": "ingest", "key": "k-alpha", "projects": ["alpha"]},
            {"name": "multi", "role": "ingest", "key": "k-multi", "projects": ["alpha", "gamma"]},
            {"name": "bravo", "role": "ingest", "key": "k-bravo", "projects": ["bravo"]}]}
        Handler.api = Api(e)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.eng.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def send(self, path, key, body, method="POST"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def project_of_run(self, rid):
        self.eng.refresh()
        r = self.eng.con.execute("SELECT project FROM runs WHERE id = ?", (rid,)).fetchone()
        return r[0] if r else None

    def run_file(self, rid):
        return os.path.join(self.eng.runs_dir, f"{rid}.json")

    def test_a_single_project_key_lands_everything_in_its_project(self):
        self.assertEqual(self.send("/api/ingest", "k-alpha", run("a-1", "whatever", "alpha_flow", "hi", "t"))[0], 200)
        self.assertEqual(self.project_of_run("a-1"), "alpha")
        self.assertEqual(self.send("/api/ingest", "k-alpha", run("a-2", "bravo", "alpha_flow", "hi", "t"))[0], 200)
        self.assertEqual(self.project_of_run("a-2"), "alpha", "naming bravo still lands in alpha")
        # its own span id: bravo's trace already uses 111..., and spans are stored by span id, so reusing
        # it would overwrite bravo's span -- which is refused (test_it_cannot_add_spans_...)
        self.assertEqual(self.send("/v1/traces", "k-alpha", otlp_trace("a" * 32, "checkout-svc", None, span="2" * 16))[0], 200)
        self.assertEqual(self.project_of_run("otlp:" + "a" * 32), "alpha")

    def test_it_cannot_overwrite_another_projects_run(self):
        def read():
            with open(self.run_file("bravo-run-1"), encoding="utf-8") as f:
                return f.read()
        before = read()
        st, body = self.send("/api/ingest", "k-alpha", run("bravo-run-1", "alpha", "x", "hijack", "t"))
        self.assertEqual(st, 403)
        self.assertIn("bravo-run-1", json.loads(body)["ids"])
        self.assertEqual(read(), before)

    def test_a_batch_is_all_or_nothing(self):
        st, _ = self.send("/api/ingest", "k-alpha", [run("a-ok", "alpha", "x", "fine", "t"),
                                                      run("bravo-run-1", "alpha", "x", "hijack", "t")])
        self.assertEqual(st, 403)
        self.assertFalse(os.path.exists(self.run_file("a-ok")), "the valid half of a refused batch was written")
        st, _ = self.send("/api/ingest/records", "k-alpha",
                          [run("a-rec", "alpha", "x", "fine", "t"), otlp_doc("b" * 32, "evil", None, span="7" * 16)])
        self.assertEqual(st, 403)
        self.assertFalse(os.path.exists(self.run_file("a-rec")))

    def test_it_cannot_add_spans_to_another_projects_trace(self):
        count = "SELECT COUNT(*) FROM spans_raw WHERE trace_id = ?"
        n = self.eng.con.execute(count, ("b" * 32,)).fetchone()[0]
        st, _ = self.send("/v1/traces", "k-alpha", otlp_trace("b" * 32, "evil", None, span="9" * 16))
        self.assertEqual(st, 403)
        self.assertEqual(self.eng.con.execute(count, ("b" * 32,)).fetchone()[0], n)

    def test_it_cannot_reuse_another_projects_span_id(self):
        """Spans are stored by span id, so a new trace reusing one would move bravo's span into it."""
        st, _ = self.send("/v1/traces", "k-alpha", otlp_trace("c" * 32, "evil", None, span="1" * 16))
        self.assertEqual(st, 403)
        row = self.eng.con.execute("SELECT trace_id FROM spans_raw WHERE source = 'otlp' AND span_id = ?",
                                   ("1" * 16,)).fetchone()
        self.assertEqual(row[0], "b" * 32)

    def test_feedback_ahead_of_its_run_is_placed_only_by_a_single_project_key(self):
        """The LangSmith SDK posts feedback directly but batches runs, so feedback can arrive first. A
        single-project key's stub is its project's (so its own run can land there and bravo's can't); a
        multi-project key can't say which project the stub is in, so it's refused."""
        rid = "22222222-2222-2222-2222-222222222222"
        self.assertEqual(self.send("/langsmith/feedback", "k-multi", {"run_id": rid, "key": "user", "score": 0})[0], 403)
        self.assertEqual(self.send("/langsmith/feedback", "k-alpha", {"run_id": rid, "key": "user", "score": 1})[0], 202)
        post = {"id": rid, "trace_id": rid, "name": "Chain", "run_type": "chain", "start_time": iso(T),
                "end_time": iso(T + 1)}
        self.assertEqual(self.send("/langsmith/runs", "k-bravo", dict(post, session_name="bravo"))[0], 403)
        self.assertEqual(self.send("/langsmith/runs", "k-alpha", post)[0], 202)
        doc = json.loads(self.eng.con.execute("SELECT doc FROM spans_raw WHERE span_id = ?", (rid,)).fetchone()[0])
        self.assertEqual((doc.get("session_name"), len(doc.get("feedback") or [])), ("alpha", 1))

    def test_it_cannot_patch_or_rate_another_projects_langsmith_run(self):
        st, _ = self.send(f"/langsmith/runs/{LS_RUN}", "k-alpha", {"outputs": {"hijacked": True}}, method="PATCH")
        self.assertEqual(st, 403)
        st, _ = self.send("/langsmith/feedback", "k-alpha", {"run_id": LS_RUN, "key": "user", "score": 0})
        self.assertEqual(st, 403)
        doc = json.loads(self.eng.con.execute("SELECT doc FROM spans_raw WHERE span_id = ?", (LS_RUN,)).fetchone()[0])
        self.assertNotIn("hijacked", json.dumps(doc))
        self.assertFalse(doc.get("feedback"))

    def test_a_multi_project_key_must_name_one_of_its_projects(self):
        self.assertEqual(self.send("/api/ingest", "k-multi", run("m-1", "gamma", "x", "hi", "t"))[0], 200)
        self.assertEqual(self.project_of_run("m-1"), "gamma")
        self.assertEqual(self.send("/api/ingest", "k-multi", run("m-2", "bravo", "x", "hi", "t"))[0], 403)
        p = run("m-3", "alpha", "x", "hi", "t")
        p.pop("project")
        self.assertEqual(self.send("/api/ingest", "k-multi", p)[0], 403, "no project named: which one is it?")

    def test_an_unscoped_key_is_unchanged(self):
        self.assertEqual(self.send("/api/ingest", "k-all", run("bravo-run-1", "bravo", "bravo_flow", "update", "t"))[0], 200)

    def test_client_chosen_ids_cannot_link_runs_across_projects(self):
        """Thread ids and subagent parent ids are chosen by clients. Reusing another project's thread id
        must not make this run "the next message" in that conversation (which can turn its task into
        rework), and naming its run as a parent must not add this run's cost to that task."""
        bravo = run("b-thread-1", "bravo", "bravo_flow", "please help", "bravo_tool")
        bravo["thread_id"] = "shared-thread"
        self.eng.ingest(bravo)
        self.eng.refresh()
        q = "SELECT outcome, subagent_cost, next_prompt FROM tasks WHERE id = 'b-thread-1#0'"
        before = tuple(self.eng.con.execute(q).fetchone())
        follow = run("a-thread-2", "alpha", "alpha_flow", "still not working", "t")
        follow["thread_id"] = "shared-thread"
        for st in follow["steps"]:                 # after bravo's run, so it would be its next message
            st["ts"] += 60
            if st.get("end_ts"):
                st["end_ts"] += 60
        child = run("a-child", "alpha", "alpha_flow", "sub", "t")
        child["parent_id"] = "b-thread-1"
        for r in (follow, child):
            self.assertEqual(self.send("/api/ingest", "k-alpha", r)[0], 200)
        self.eng.refresh()
        self.assertEqual(tuple(self.eng.con.execute(q).fetchone()), before)


LS_RUN = "11111111-1111-1111-1111-111111111111"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000000"


def otlp_doc(tid, service, project=None, span="1" * 16):
    attrs = [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
             {"key": "gen_ai.request.model", "value": {"stringValue": "claude-sonnet-5"}},
             {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "100"}},
             {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "10"}}]
    if project:
        attrs.append({"key": "metadata", "value": {"stringValue": json.dumps({"project": project})}})
    return {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
                               "scopeSpans": [{"spans": [{
                                   "traceId": tid, "spanId": span, "name": "chat", "status": {}, "attributes": attrs,
                                   "startTimeUnixNano": str(int(T * 1e9)),
                                   "endTimeUnixNano": str(int((T + 1) * 1e9))}]}]}]}


def otlp_trace(tid, service, project, span="1" * 16):
    return json.dumps(otlp_doc(tid, service, project, span)).encode()


if __name__ == "__main__":
    unittest.main()
