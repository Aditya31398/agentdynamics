"""The console, used: click, filter, type, save -- in headless Chrome, against a server process.

test_console_ui.py renders each page once and reads the DOM. That can't catch a bug that needs a person
to do something first, and several shipped or nearly did: a request after the Refresh or Clear button
answered 501 (a body-less POST left "{}" on the connection), a grouping that dropped three of ninety tasks,
a card that didn't redraw. Each test here does what a person would and then requires that nothing went
wrong along the way -- no console error, no uncaught exception, no HTTP response of 400 or more -- as well
as checking what it came to do.

The fixture's tasks sit at known ages (2 hours, 3 days, 20 days, 60 days) in two projects, so every
filter has an exact expected count. Skips without Chrome or Edge, like test_console_ui.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
import cdp  # noqa: E402

ROOT = cdp.ROOT
sys.path.insert(0, ROOT)
from agentdynamics.engine import Engine  # noqa: E402

BROWSER = cdp.find_browser()
NOW = time.time()
HOUR, DAY = 3600, 86400
# project -> [(age in seconds, failed?)]: the counts each filter must show
AGES = {"shop": [(2 * HOUR, False), (2 * HOUR, True), (3 * HOUR, False), (3 * DAY, False), (3 * DAY, True),
                 (20 * DAY, False), (60 * DAY, False)],
        "billing": [(2 * HOUR, False), (4 * HOUR, True), (3 * DAY, False), (60 * DAY, False)]}
WINDOWS = {"1": 1 * DAY, "7": 7 * DAY, "30": 30 * DAY, "": None}


def expected(project=None, days=""):
    out = 0
    for p, rows in AGES.items():
        if project and p != project:
            continue
        out += sum(1 for age, _ in rows if WINDOWS[days] is None or age < WINDOWS[days])
    return out


def run(rid, project, age, failed):
    t0 = NOW - age
    wf = "refund_flow" if project == "billing" else "support_flow"
    steps = [{"kind": "prompt", "ts": t0, "text": f"{project} request {rid}"},
             {"kind": "llm", "ts": t0, "end_ts": t0 + 2, "model": "claude-sonnet-5", "input_tokens": 900,
              "output_tokens": 150, "stop_reason": "end_turn"},
             {"kind": "tool", "ts": t0 + 2, "end_ts": t0 + 3, "name": "orders.lookup", "governed": True,
              "rule": "kernel.admitted", "agent": "support", "is_error": failed}]
    if failed:   # a refused call too, so the Governance page has something to show
        steps.append({"kind": "tool", "ts": t0 + 3, "end_ts": t0 + 3.1, "name": "fs.read", "governed": True,
                      "denied": True, "rule": "arg.forbid_matches", "guard": "args", "agent": "support",
                      "error": "[arg.forbid_matches] path outside /workspace"})
    return {"id": rid, "project": project, "workflow": wf, "environment": "production", "agent": "support",
            "status": "error" if failed else "ok", "error": "card declined" if failed else None,
            "policy_version": "support@v1#abc123", "steps": steps}


@unittest.skipUnless(BROWSER, "no Chrome or Edge found (set AGENTDYNAMICS_BROWSER)")
class ConsoleInteractionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        data = os.path.join(cls.tmp, "data")
        eng = Engine(data, None)
        for project, rows in AGES.items():
            for i, (age, failed) in enumerate(rows):
                p = run(f"{project}-{i}", project, age, failed)
                if p["id"] == "shop-1":      # its refused call carried a canary, as the Aegis integration marks it
                    p["steps"][-1]["tripwire"] = "canary vault_token"
                eng.ingest(p)
        eng.refresh(force=True)
        # what the checker said about that incident (checker.py; no model here, just its stored review)
        from agentdynamics import store
        iid = eng.con.execute("SELECT id FROM incidents WHERE agent = 'support'").fetchone()[0]
        store.add_review(eng.con, iid, 1, NOW - 60, "claude-opus-5-5",
                         {"classification": "prompt_injection", "confidence": "high",
                          "summary": "A canary was read during a refund request.", "evidence": ["canary vault_token"],
                          "recommendation": "restrict"}, usage={"input_tokens": 1500, "output_tokens": 90})
        eng.con.close()
        cls.srv, cls.url = cdp.serve(data)
        cls.browser = cdp.Browser(BROWSER)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cdp.stop(cls.srv)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.page = self.browser.page()
        self.addCleanup(self.page.close)

    def open(self, route, days="", project=""):
        """Open a page with the filters a person would have set, from a clean slate."""
        self.page.goto(self.url + "/")
        self.page.eval(f"localStorage.clear(); localStorage.setItem('ad.filters', "
                       f"JSON.stringify({{project: {json.dumps(project)}, days: {json.dumps(days)}, sub: '0'}}))")
        self.page.goto(f"{self.url}/#/{route}")
        self.page.settle()

    def api(self, path):
        with urllib.request.urlopen(self.url + path, timeout=30) as r:
            return json.load(r)

    def kpi(self, label):
        return self.page.eval(f"""(() => {{
            const k = [...document.querySelectorAll('.kpi')].find(x => x.querySelector('.label').innerText.trim()
                                                                  .startsWith({json.dumps(label)}));
            return k ? k.querySelector('.value').innerText : null; }})()""")

    @staticmethod
    def row(text):
        """JS for the directives-table row whose text includes `text` (tests share one server's directives)."""
        return f"[...document.querySelectorAll('#gv-dir-list tbody tr')].find(r => r.innerText.includes({json.dumps(text)}))"

    def assertNothingWentWrong(self):
        self.assertEqual(self.page.problems(), [], "the console logged an error or got an HTTP error")

    # ------------------------------------------------------------------ navigation
    def test_every_link_in_the_sidebar_opens_its_page(self):
        self.open("overview")
        links = self.page.eval("[...document.querySelectorAll('#nav a')].map(a => a.getAttribute('href'))")
        self.assertGreater(len(links), 15)
        for href in links:
            with self.subTest(href=href):
                self.page.click(f"#nav a[href='{href}']")
                self.page.wait(f"location.hash === {json.dumps(href)}", what=f"the hash to be {href}")
                self.page.settle()
                text = self.page.text()
                self.assertNotIn("Failed to load", text)
                self.assertTrue(self.page.eval("!!document.querySelector('#page h1, #page h2')"), f"{href} drew nothing")
                self.assertTrue(self.page.eval(f"document.querySelector('#nav a[href=\"{href}\"]').classList.contains('active')"))
        self.assertNothingWentWrong()

    def test_a_task_row_opens_the_task_and_back_returns(self):
        self.open("tasks")
        first = self.page.eval("document.querySelector('#page tr.click').dataset.href")
        self.page.click("#page tr.click")
        self.page.wait(f"location.hash === {json.dumps(first)}")
        self.page.settle()
        tid = first.split("/", 2)[2]
        from urllib.parse import unquote
        prompt = self.api(f"/api/task/{tid}")["task"]["prompt"]
        self.assertIn(prompt, self.page.text())
        self.page.eval("history.back()")
        self.page.wait("location.hash.startsWith('#/tasks')")
        self.page.settle()
        self.assertIn("Tasks", self.page.text("#page h1"))
        self.assertEqual(unquote(tid).split("#")[1], "0")
        self.assertNothingWentWrong()

    # ------------------------------------------------------------------ filters
    def test_the_time_window_buttons_set_the_window(self):
        self.open("overview", days="")
        for days, label in (("1", "24h"), ("7", "7d"), ("30", "30d"), ("", "All")):
            with self.subTest(window=label):
                self.page.click(f"#f-days button[data-v='{days}']")
                self.page.wait(f"document.querySelector(\"#f-days button[data-v='{days}']\").classList.contains('on')")
                self.page.settle()
                self.page.wait(f"(() => {{ const k = [...document.querySelectorAll('.kpi')].find(x => x.innerText.startsWith('Tasks'));"
                               f" return k && k.querySelector('.value').innerText === '{expected(days=days)}'; }})()",
                               what=f"{expected(days=days)} tasks in the {label} window")
        self.assertNothingWentWrong()

    def test_the_project_filter_narrows_everything_and_survives_a_reload(self):
        self.open("overview", days="")
        self.page.select("#f-project", "billing")
        self.page.settle()
        self.page.wait(f"[...document.querySelectorAll('.kpi')].some(k => k.innerText.startsWith('Tasks') && "
                       f"k.querySelector('.value').innerText === '{expected('billing')}')", what="billing's task count")
        self.page.eval("location.reload()")
        self.page.wait("document.readyState === 'complete'")
        self.page.settle()
        self.assertEqual(self.page.eval("document.querySelector('#f-project').value"), "billing", "the filter was kept")
        self.assertEqual(self.kpi("Tasks"), str(expected("billing")))
        self.page.click("#nav a[href='#/tasks']")
        self.page.wait("location.hash === '#/tasks'")
        self.page.settle()
        self.assertIn(f"{expected('billing')} tasks", self.page.text())
        self.assertNothingWentWrong()

    def test_the_task_list_filters(self):
        self.open("tasks", days="")
        self.assertIn(f"{expected()} tasks", self.page.text())
        self.page.select("#t-outcome", "failed")
        self.page.wait("location.hash.includes('outcome=failed')")
        self.page.settle()
        failed = sum(1 for rows in AGES.values() for _, f in rows if f)
        self.assertIn(f"{failed} tasks", self.page.text())
        self.page.fill("#t-q", "billing request")
        self.page.eval("document.querySelector('#t-q').dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}))")
        self.page.wait("location.hash.includes('q=billing')")
        self.page.settle()
        self.assertIn("1 tasks", self.page.text())
        self.assertNothingWentWrong()

    def test_analytics_regroups_and_adds_a_metric(self):
        self.open("analytics", days="")
        self.page.select("#a-group", "project")
        self.page.wait("location.hash.includes('group=project')")
        self.page.settle()
        rows = self.page.eval("[...document.querySelectorAll('#page table tbody tr')].map(r => r.cells[0].innerText)")
        self.assertEqual(sorted(rows), ["billing", "shop"])
        self.page.eval("(() => { const c = document.querySelector(\".a-m[value='error_rate']\"); c.checked = true;"
                       " c.dispatchEvent(new Event('change')); })()")
        self.page.wait("location.hash.includes('error_rate')")
        self.page.settle()
        self.assertIn("error rate", self.page.text("#page thead").lower())   # CSS upper-cases headers
        self.assertNothingWentWrong()

    # ------------------------------------------------------------------ actions
    def test_refresh_then_keep_using_the_console(self):
        """The Refresh button posts "{}" to a route that reads no body. The request after it on the same
        connection came back 501 ('{}GET')."""
        self.open("overview")
        self.page.click("#btn-refresh")
        self.page.wait("document.querySelector('#toast').innerText.startsWith('Re-indexed')", what="the refresh toast")
        for route in ("tasks", "events", "overview"):
            self.page.click(f"#nav a[href='#/{route}']")
            self.page.wait(f"location.hash === '#/{route}'")
            self.page.settle()
        self.assertNothingWentWrong()

    def test_issue_and_clear_a_revocation_directive(self):
        self.open("governance", days="")
        self.page.wait("!!document.querySelector('#gv-r-go')", what="the directives card")
        self.page.fill("#gv-r-agent", "support")
        self.page.fill("#gv-r-project", "shop")
        self.page.fill("#gv-r-reason", "interaction test")
        self.page.click("#gv-r-go")
        self.page.wait("!!(" + self.row("interaction test") + " && " + self.row("interaction test") + ".querySelector('.gv-clear'))",
                       what="the active directive")
        self.page.eval(self.row("interaction test") + ".querySelector('.gv-clear').click()")
        self.page.wait("!!(" + self.row("interaction test") + " && " + self.row("interaction test") + ".innerText.includes('cleared'))",
                       what="the directive to show as cleared")
        self.assertEqual([d["status"] for d in self.api("/api/revocations")["revocations"]
                          if d["reason"].startswith("interaction test")], ["cleared"])
        self.assertNothingWentWrong()

    def test_issue_a_restriction_from_the_directives_card(self):
        self.open("governance", days="")
        self.page.wait("!!document.querySelector('#gv-r-go')", what="the directives card")
        self.page.fill("#gv-r-agent", "support")
        self.page.fill("#gv-r-project", "shop")
        self.page.fill("#gv-r-tools", "fs.read, db.query")
        self.assertEqual(self.page.text("#gv-r-go"), "Restrict", "the button says what it will do")
        self.page.fill("#gv-r-reason", "restriction test")
        self.page.click("#gv-r-go")
        self.page.wait("document.querySelector('#gv-dir-list') && document.querySelector('#gv-dir-list').innerText.includes('restriction test')",
                       what="the restriction listed")
        self.assertIn("takes db.query, fs.read", self.page.text("#gv-dir-list"))
        mine = [d for d in self.api("/api/revocations")["revocations"] if d["reason"].startswith("restriction test")]
        self.assertEqual([(d["kind"], d["spec"]) for d in mine], [("restrict", {"tools": ["db.query", "fs.read"]})])
        self.page.eval(self.row("restriction test") + ".querySelector('.gv-clear').click()")     # leave nothing in force
        self.page.wait(self.row("restriction test") + ".innerText.includes('cleared')", what="the restriction cleared")
        self.assertNothingWentWrong()

    def test_take_the_misused_tool_away_from_an_incident(self):
        self.open("incidents", days="")
        self.page.wait("!!document.querySelector('#inc-list tr.click')", what="the incident list")
        self.page.click("#inc-list tr.click")
        self.page.wait("!!document.querySelector('#inc-restrict')", what="the restrict action")
        self.assertIn("Take fs.read away from support", self.page.text("#inc-restrict"))
        self.page.click("#inc-restrict")
        self.page.wait("document.querySelector('#inc-actions') && document.querySelector('#inc-actions').innerText.includes('restricted')",
                       what="the restriction in force")
        self.assertIn("without fs.read", self.page.text("#inc-actions"))
        self.assertFalse(self.page.eval("!!document.querySelector('#inc-restrict')"), "not offered twice")
        self.assertNothingWentWrong()

    def test_a_tripwire_row_opens_the_task_that_touched_it(self):
        self.open("governance", days="")
        self.page.wait("!!document.querySelector('#gv-trips tr.click')", what="the tripwires card")
        self.assertIn("canary vault_token", self.page.text("#gv-trips"))
        self.page.click("#gv-trips tr.click")
        self.page.wait("location.hash === '#/task/shop-1%230'", what="the task that touched it")
        self.page.settle()
        self.page.wait("!!document.querySelector('#wf .tag.bad')", what="the touching step tagged in the timeline")
        self.assertIn("tripwire · canary vault_token", self.page.text("#wf"))
        self.assertNothingWentWrong()

    def test_an_agents_trust_opens_to_its_evidence(self):
        self.open("incidents", days="")
        self.page.wait("!!document.querySelector('#trust-list .trust-row')", what="the trust card")
        self.assertIn("support", self.page.text("#trust-list"))
        self.page.click("#trust-list .trust-row")
        self.page.wait("!!document.querySelector('#trust-list .trust-detail')", what="the agent's evidence")
        detail = self.page.text("#trust-list .trust-detail")
        self.assertIn("tripwire", detail)
        self.assertIn("= ", detail, "the arithmetic is shown")
        self.page.click("#trust-list .trust-detail tr.click")
        self.page.wait("location.hash === '#/task/shop-1%230'", what="the task the evidence came from")
        self.page.settle()
        self.page.eval("history.back()")
        self.page.wait("!!document.querySelector('#trust-list .trust-row')", what="the incidents page again")
        self.page.click("#trust-list .trust-row")                 # open, then close again
        self.page.wait("!!document.querySelector('.trust-detail')")
        self.page.click("#trust-list .trust-row")
        self.page.wait("!document.querySelector('.trust-detail')", what="the evidence to close")
        self.page.click("#inc-list tr.click")
        self.page.wait("!!document.querySelector('#inc-trust')", what="the agent's trust on its incident")
        self.assertIn("support's trust", self.page.text("#inc-trust"))
        self.assertNothingWentWrong()

    def test_the_checkers_view_is_shown_and_nothing_more(self):
        self.open("incidents", days="")
        self.page.wait("!!document.querySelector('#checker-card')", what="the checker's record")
        self.assertIn("reviewed 1", self.page.text("#checker-card"))
        self.assertIn("checker: prompt injection", self.page.text("#inc-list"))
        self.page.click("#inc-list tr.click")
        self.page.wait("!!document.querySelector('#inc-review')", what="the checker's view of the incident")
        view = self.page.text("#inc-review")
        self.assertIn("A canary was read during a refund request.", view)
        self.assertIn("would restrict", view)
        self.assertIn("Shadow mode", view)
        self.assertNothingWentWrong()

    def test_resolve_an_incident_then_reopen_it(self):
        self.open("incidents", days="")
        self.page.wait("!!document.querySelector('#inc-list tr.click')", what="the incident list")
        self.assertIn("tripwire", self.page.text("#inc-list"))
        self.page.click("#inc-list tr.click")
        self.page.wait("location.hash.startsWith('#/incident/')", what="the incident page")
        self.page.settle()
        self.page.wait("!!document.querySelector('[data-verdict=\"false_alarm\"]')", what="the verdict buttons")
        self.page.fill("#inc-note", "a test script hit the decoy")
        self.page.click('[data-verdict="false_alarm"]')
        self.page.wait("document.querySelector('#inc-summary') && document.querySelector('#inc-summary').innerText.includes('false alarm')",
                       what="the incident shown as a false alarm")
        self.assertIn("a test script hit the decoy", self.page.text("#inc-summary"))
        iid = self.page.eval("location.hash").split("/")[-1]
        self.assertEqual(self.api(f"/api/incident/{iid}")["incident"]["verdict"], "false_alarm")
        self.page.click('[data-verdict=""]')                       # reopen
        self.page.wait("!!document.querySelector('[data-verdict=\"real\"]')", what="the incident reopened")
        self.page.goto(f"{self.url}/#/incidents?status=resolved")
        self.page.settle()
        self.page.wait("!!document.querySelector('#inc-tabs button.on[data-status=\"resolved\"]')", what="the Resolved tab")
        self.assertIn("No resolved incidents", self.page.text())
        self.page.click("#inc-tabs button[data-status='open']")
        self.page.wait("!!document.querySelector('#inc-list tr.click')", what="the open incident again")
        self.assertNothingWentWrong()

    def test_switching_a_rule_off_takes_its_events_away(self):
        self.open("rules")
        before = self.api("/api/events?days=")["events"]
        self.assertTrue(any(e["rule_id"] == "run_failed" for e in before), "the fixture should raise run_failed")
        i = self.page.eval("(() => { const rs = [...document.querySelectorAll('.rule-row')];"
                           " return rs.findIndex(r => r.innerText.includes('Run failed')); })()")
        try:
            self.page.eval(f"document.querySelector(\"input[data-i='{i}'][data-k='enabled']\").checked = false")
            self.page.click("#r-save")
            self.page.wait("document.querySelector('#toast').innerText.startsWith('Rules saved')", what="the save toast")
            after = self.api("/api/events?days=")["events"]
            self.assertFalse(any(e["rule_id"] == "run_failed" for e in after), "the rule's events should be gone")
        finally:   # put it back for the other tests
            rules = self.api("/api/rules")["rules"]
            for r in rules:
                r["enabled"] = True
            req = urllib.request.Request(self.url + "/api/rules", data=json.dumps({"rules": rules}).encode(),
                                         method="POST", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=60).read()
        self.assertNothingWentWrong()

    def test_the_slo_editor_saves_an_objective(self):
        self.open("slos", days="")
        original = self.api("/api/slos")["slos"]
        try:
            self.page.click("#slo-edit")
            self.page.wait("!!document.querySelector('#slo-json')")
            slos = json.loads(self.page.eval("document.querySelector('#slo-json').value"))
            slos.append({"id": "billing-ok", "name": "Billing success", "metric": "success_rate", "op": ">=",
                         "target": 0.5, "window_days": 7, "scope": {"project": "billing"}})
            self.page.fill("#slo-json", json.dumps(slos))
            self.page.click("#slo-save")
            self.page.wait("document.querySelector('#page').innerText.includes('Billing success')",
                           what="the new objective's card")
        finally:
            keep = [{k: s[k] for k in ("id", "name", "metric", "op", "target", "window_days", "scope")} for s in original]
            req = urllib.request.Request(self.url + "/api/slos", data=json.dumps({"slos": keep}).encode(),
                                         method="POST", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=60).read()
        self.assertNothingWentWrong()

    def test_an_apdex_target_is_set_and_cleared_from_task_types(self):
        self.open("types", days="")
        i = self.page.eval("(() => { const rs = [...document.querySelectorAll('#page table')[1].querySelectorAll('tbody tr')];"
                           " return rs.findIndex(r => r.innerText.includes('refund_flow')); })()")
        self.assertGreaterEqual(i, 0, "the targets table lists the type")
        try:
            self.page.fill(f"#apdex-lat-{i}", "0.5")              # half a second: every refund_flow task is too slow
            self.page.click("#apdex-save")
            self.page.wait("document.querySelector('#toast').innerText.startsWith('Apdex targets saved')",
                           what="the save toast")
            self.page.wait("[...document.querySelectorAll('#page table')[0].querySelectorAll('tbody tr')]"
                           ".some(r => r.innerText.includes('refund_flow') && r.innerText.includes('≤'))",
                           what="the type's target in the table")
            tasks = [t for t in self.api("/api/tasks?days=&type=refund_flow")["tasks"]]
            self.assertTrue(tasks and all(t["apdex_basis"] == "targets" for t in tasks))
            self.assertTrue(all(t["apdex"] == "frustrated" for t in tasks), "3 s of agent time is over 4x 0.5 s")
            self.open("types", days="")
            self.page.fill(f"#apdex-lat-{i}", "")
            self.page.click("#apdex-save")
            self.page.wait("document.querySelector('#toast').innerText.startsWith('Apdex targets saved')",
                           what="the clearing toast")
            tasks = self.api("/api/tasks?days=&type=refund_flow")["tasks"]
            self.assertTrue(all(t["apdex_basis"] == "baseline" for t in tasks), "cleared: judged by the baseline again")
        finally:
            req = urllib.request.Request(self.url + "/api/apdex", data=json.dumps({"targets": {"refund_flow": None}}).encode(),
                                         method="POST", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=60).read()
        self.assertNothingWentWrong()


if __name__ == "__main__":
    unittest.main()
