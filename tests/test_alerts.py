"""Alert routing: what reaches Slack, PagerDuty and JSON webhooks, and that it gets there.

Every destination here is a local HTTP receiver, so the tests see the exact bytes a real endpoint would.
PagerDuty bodies are checked against the Events API v2 schema PagerDuty publishes
(https://raw.githubusercontent.com/PagerDuty/api-schema/main/reference/events-v2/openapiv3.json).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agentdynamics import alerts as alertmod, config, slo, store  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api  # noqa: E402


class Receiver:
    """A webhook endpoint that records what it is sent. `fail` lists status codes for the next requests."""

    def __init__(self):
        self.got, self.fail = [], []
        rec = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                code = rec.fail.pop(0) if rec.fail else 202
                if code < 300:
                    rec.got.append((self.path, json.loads(body)))
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b'{"status": "invalid event", "errors": ["routing_key is invalid"]}'
                                 if code == 400 else b"{}")

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def check_pagerduty(testcase, body):
    """The Events API v2 request schema."""
    testcase.assertIn(body["event_action"], ("trigger", "acknowledge", "resolve"))
    testcase.assertIsInstance(body["routing_key"], str)
    testcase.assertLessEqual(len(body["dedup_key"]), 255)
    if body["event_action"] != "trigger":
        return
    p = body["payload"]
    for f in ("summary", "source"):
        testcase.assertIsInstance(p[f], str)
        testcase.assertTrue(p[f])
    testcase.assertLessEqual(len(p["summary"]), 1024)
    testcase.assertIn(p["severity"], ("critical", "error", "warning", "info"))
    time.strptime(p["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
    testcase.assertIsInstance(p.get("custom_details", {}), dict)
    for link in body.get("links", []):
        testcase.assertTrue(link["href"].startswith("http"))


def run(rid, t0, failed=False, project="shop", workflow="checkout", prompt="place the order", thread=None):
    return {"id": rid, "project": project, "workflow": workflow, "thread_id": thread or rid,
            "status": "error" if failed else "ok", "error": "card declined" if failed else None,
            "steps": [{"kind": "prompt", "ts": t0, "text": prompt},
                      {"kind": "llm", "ts": t0, "end_ts": t0 + 20, "model": "claude-sonnet-5",
                       "input_tokens": 500, "output_tokens": 80, "stop_reason": "end_turn"},
                      {"kind": "tool", "ts": t0 + 20, "end_ts": t0 + 30, "name": "charge", "is_error": failed}]}


class AlertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rx = Receiver()
        self.addCleanup(self.rx.close)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.now = time.time()
        self.offset = 0.0

    def engine(self, webhooks, privacy=None, **alerts_cfg):
        cfg = config.load(self.tmp)
        cfg["alerts"] = {"webhooks": webhooks, "console_url": "http://console.test", "slo_min_tasks": 10, **alerts_cfg}
        if privacy:
            cfg["privacy"].update(privacy)
        e = Engine(self.tmp, None, cfg=cfg)
        e._clock = lambda: self.now + self.offset
        self.addCleanup(e.con.close)
        return e

    def bodies(self, path=None):
        return [b for p, b in self.rx.got if path is None or p == path]


class EventAlertTest(AlertTest):
    def test_a_new_event_is_delivered_once_and_history_never(self):
        e = self.engine([{"url": self.rx.url + "/slack", "format": "slack"}])
        e.ingest(run("old", self.now - 600, failed=True))
        e.refresh(force=True)                      # first refresh of a process: that's history
        self.assertEqual(e.deliver_alerts(), 0)
        e.ingest(run("new", self.now - 300, failed=True))
        e.refresh()
        self.assertEqual(e.deliver_alerts(), 1)
        e.refresh(force=True)
        self.assertEqual(e.deliver_alerts(), 0, "an event is alerted once")
        text = self.bodies("/slack")[0]["text"]
        self.assertIn("*CRITICAL* · Run failed · shop / checkout", text)
        self.assertIn("<http://console.test/#/task/new%230|open>", text)
        self.assertNotIn("old", text)

    def test_a_burst_is_one_slack_line(self):
        e = self.engine([{"url": self.rx.url + "/slack", "format": "slack", "rules": ["run_failed"]}])
        e.refresh(force=True)
        for i in range(12):
            e.ingest(run(f"r{i:02}", self.now - 300 + i, failed=True))
        e.refresh()
        e.deliver_alerts()
        text = self.bodies("/slack")[0]["text"]
        self.assertEqual(text.count("Run failed"), 1)
        self.assertIn("Run failed ×12 · shop / checkout", text)
        self.assertNotIn("more", text)

    def test_the_json_body_keeps_its_contract(self):
        e = self.engine([{"url": self.rx.url + "/json"}])
        e.refresh(force=True)
        e.ingest(run("r1", self.now - 300, failed=True))
        e.refresh()
        e.deliver_alerts()
        body = self.bodies("/json")[0]
        self.assertEqual(set(body), {"source", "events"})
        self.assertEqual(body["source"], "agentdynamics")
        ev = [x for x in body["events"] if x["rule_id"] == "run_failed"][0]
        for f in ("id", "ts", "rule_id", "rule", "severity", "task_id", "run_id", "project", "task_type",
                  "message", "value"):
            self.assertIn(f, ev)

    def test_pagerduty_gets_one_alert_per_rule_project_and_task_type(self):
        e = self.engine([{"format": "pagerduty", "url": self.rx.url + "/pd", "routing_key": "R0UT1NGKEY",
                          "min_severity": "critical"}])
        e.refresh(force=True)
        for i in range(5):
            e.ingest(run(f"r{i}", self.now - 300 + i, failed=True))
        e.refresh()
        queued = [r["body"] for r in store.outbox_pending(e.con)]
        self.assertTrue(queued)
        self.assertFalse(any("R0UT1NGKEY" in b for b in queued), "the routing key is never stored")
        e.deliver_alerts()
        bodies = self.bodies("/pd")
        for b in bodies:
            check_pagerduty(self, b)
        failed = [b for b in bodies if b["payload"]["class"] == "run_failed"]
        self.assertEqual(len(failed), 1, "five failures of one kind are one PagerDuty alert")
        self.assertEqual(failed[0]["payload"]["custom_details"]["occurrences"], 5)
        self.assertEqual(failed[0]["routing_key"], "R0UT1NGKEY")
        self.assertEqual(failed[0]["dedup_key"], "agentdynamics/event/run_failed/shop/checkout")
        self.assertTrue(all(b["payload"]["severity"] == "critical" for b in bodies), "min_severity is applied")

    def test_a_backfill_does_not_page(self):
        e = self.engine([{"url": self.rx.url + "/h"}])
        e.refresh(force=True)
        e.ingest(run("imported", self.now - 3 * 3600, failed=True))   # happened hours ago, arrived now
        e.refresh()
        self.assertEqual(e.deliver_alerts(), 0)

    def test_slack_markup_in_a_message_is_escaped(self):
        d = alertmod.destinations({"webhooks": [{"url": "http://x", "format": "slack"}]})[0][0]
        ev = {"id": "t#0:r", "ts": 0, "rule_id": "r", "rule": "Rule <b>", "severity": "warning", "task_id": "t#0",
              "run_id": "t", "project": "a&b", "task_type": "x", "message": "<!channel> ran `rm` > /dev/null",
              "value": 1}
        text = alertmod.render(d, [alertmod.from_event(ev)])[0]["text"]
        self.assertNotIn("<!channel>", text)
        self.assertIn("&lt;!channel&gt; ran `rm` &gt; /dev/null", text)
        self.assertIn("a&amp;b", text)

    def test_routing_by_project_rule_and_kind(self):
        e = self.engine([{"name": "shop", "url": self.rx.url + "/shop", "projects": ["shop"]},
                         {"name": "failures", "url": self.rx.url + "/failures", "rules": ["run_failed"]},
                         {"name": "slos-only", "url": self.rx.url + "/slos", "kinds": ["slos"]}])
        e.refresh(force=True)
        e.ingest(run("a", self.now - 300, failed=True, project="shop"))
        e.ingest(run("b", self.now - 300, failed=True, project="billing"))
        streak = run("c", self.now - 300)          # three failing calls in a row: another rule
        streak["steps"] += [{"kind": "tool", "ts": self.now - 250 + i, "end_ts": self.now - 249 + i, "name": "charge",
                             "is_error": True} for i in range(3)]
        e.ingest(streak)
        e.refresh()
        e.deliver_alerts()
        projects = {x["project"] for b in self.bodies("/shop") for x in b["events"]}
        self.assertEqual(projects, {"shop"})
        self.assertIn("error_streak", {x["rule_id"] for b in self.bodies("/shop") for x in b["events"]})
        rules = {x["rule_id"] for b in self.bodies("/failures") for x in b["events"]}
        self.assertEqual(rules, {"run_failed"})
        self.assertEqual(self.bodies("/slos"), [], "a destination for SLO alerts gets no health-rule events")


class DeliveryTest(AlertTest):
    def queue_one(self, e):
        e.refresh(force=True)
        e.ingest(run(f"r{len(self.rx.got)}-{self.offset}", self.now + self.offset - 60, failed=True))
        e.refresh()

    def test_a_failed_send_is_retried_in_order_and_survives_a_restart(self):
        hooks = [{"name": "hook", "url": self.rx.url + "/h", "rules": ["run_failed"]}]
        e = self.engine(hooks)
        self.rx.fail = [503]
        self.queue_one(e)
        self.assertEqual(e.deliver_alerts(), 0)
        self.assertEqual(e.stats["alerts_retried"], 1)
        e.ingest(run("second", self.now - 30, failed=True))
        e.refresh()
        self.assertEqual(e.deliver_alerts(), 0, "the retry holds back what was queued after it")
        e.con.close()
        e = self.engine(hooks)                     # a restart: the queue is durable
        self.offset = alertmod.backoff(0) + 1
        self.assertEqual(e.deliver_alerts(), 2)
        order = [x["run_id"] for _, b in self.rx.got for x in b["events"]]
        self.assertEqual(order[0], [r for r in order if r != "second"][0])
        self.assertEqual(order[-1], "second", "delivered in the order queued")

    def test_it_gives_up_after_a_day(self):
        e = self.engine([{"name": "hook", "url": self.rx.url + "/h"}])
        self.queue_one(e)
        self.rx.fail = [503] * 3
        self.offset = alertmod.GIVE_UP_S + 1
        e.deliver_alerts()
        self.assertEqual(e.stats["alerts_dropped"], 1)
        self.assertEqual(store.outbox_pending(e.con), [])

    def test_a_config_error_is_dropped_and_reported(self):
        e = self.engine([{"name": "pd", "format": "pagerduty", "url": self.rx.url + "/pd", "routing_key": "bad"}])
        self.queue_one(e)
        self.rx.fail = [400]
        e.deliver_alerts()
        self.assertEqual((e.stats["alerts_dropped"], store.outbox_pending(e.con)), (1, []))
        status = Api(e).alerts({})
        d = status["destinations"][0]
        self.assertEqual((d["id"], d["dropped"]), ("pd", 1))
        self.assertIn("routing_key is invalid", d["last_error"])

    def test_a_bad_destination_is_reported_and_the_others_still_work(self):
        e = self.engine([{"name": "typo", "url": self.rx.url, "format": "pagerdutty"},
                         {"name": "nokey", "format": "pagerduty", "routing_key_env": "AD_TEST_UNSET_KEY"},
                         {"name": "good", "url": self.rx.url + "/good"}])
        problems = Api(e).alerts({})["problems"]
        self.assertEqual(len(problems), 2)
        self.assertIn("AD_TEST_UNSET_KEY is not set", " ".join(problems))
        self.queue_one(e)
        self.assertGreaterEqual(e.deliver_alerts(), 1)

    def test_no_secret_reaches_the_config_page(self):
        e = self.engine([{"format": "pagerduty", "routing_key": "R0UT1NGKEY"},
                         {"url": "https://hooks.slack.com/services/T000/B000/SECRETPART", "format": "slack"}])
        shown = json.dumps(Api(e).config({})) + json.dumps(Api(e).alerts({}))
        self.assertNotIn("R0UT1NGKEY", shown)
        self.assertNotIn("SECRETPART", shown)


class PrivacyTest(AlertTest):
    """A rule message can quote what the user wrote (the `rework` rule quotes the correcting message), so it
    gets the same treatment as the task's own text: redaction, and nothing at all with store_content off."""

    def correction(self, privacy):
        e = self.engine([{"url": self.rx.url + "/h", "min_severity": "info"}], privacy=privacy)
        e.refresh(force=True)
        e.ingest(run("r1", self.now - 600, prompt="please fix the invoice export", thread="th"))
        e.ingest(run("r2", self.now - 300, prompt="no that's wrong, mail bob.jones@example.com the SECRET-PLAN",
                     thread="th"))
        e.refresh()
        e.deliver_alerts()
        stored = [r[0] for r in e.con.execute("SELECT message FROM events WHERE rule_id = 'rework'")]
        sent = [x["message"] for b in self.bodies() for x in b.get("events", []) if x["rule_id"] == "rework"]
        self.assertTrue(stored and sent, "the fixture should produce a rework event")
        return stored + sent

    def test_redaction_applies_to_alerts(self):
        for m in self.correction({"store_content": True}):
            self.assertNotIn("bob.jones@example.com", m)
            self.assertIn("[REDACTED]", m)

    def test_no_content_leaves_with_store_content_off(self):
        for m in self.correction({"store_content": False}):
            self.assertNotIn("SECRET-PLAN", m)
            self.assertNotIn("wrong", m)


class SloAlertTest(AlertTest):
    SLOS = [{"id": "success", "name": "Checkout success", "metric": "success_rate", "op": ">=", "target": 0.9,
             "window_days": 7, "scope": {"project": "shop"}}]

    def setUp(self):
        super().setUp()
        slo.save(self.tmp, self.SLOS)
        self.hooks = [{"name": "pager", "format": "pagerduty", "url": self.rx.url + "/pd", "routing_key": "RK",
                       "kinds": ["slos"], "min_severity": "critical"},
                      {"name": "chat", "url": self.rx.url + "/chat", "format": "slack", "kinds": ["slos"]}]

    def burst(self, e, failed=10, ok=10):
        """Tasks that all finished in the last few minutes: half failed, a 5x burn of a 10% budget."""
        for i in range(failed + ok):
            e.ingest(run(f"b{i}", self.now - 240 + i, failed=i < failed))
        e.refresh(force=True)

    def keys(self, e):
        return sorted(store.alert_state(e.con))

    def test_a_burn_pages_once_then_resolves(self):
        e = self.engine(self.hooks)
        self.burst(e)
        e.check_slos()
        self.assertEqual(self.keys(e), ["slo/success/page", "slo/success/ticket"])
        e.deliver_alerts()
        pd = self.bodies("/pd")
        self.assertEqual([b["event_action"] for b in pd], ["trigger"], "one page, not one per policy")
        check_pagerduty(self, pd[0])
        self.assertEqual(pd[0]["dedup_key"], "agentdynamics/slo/success/page")
        self.assertEqual(pd[0]["payload"]["custom_details"]["policy"], "fast")
        self.assertEqual(pd[0]["payload"]["component"], "shop")
        chat = self.bodies("/chat")[0]["text"]
        self.assertIn("burning its error budget at 5.0x", chat)
        self.assertIn("*WARNING*", chat, "the ticket goes to the chat destination")

        self.offset = 600                           # quiet for 10 minutes: no evidence the burn stopped
        e.check_slos()
        self.assertIn("slo/success/page", self.keys(e))
        self.offset = 7 * 3600                      # out of the 1h and 6h windows
        e.check_slos()
        self.assertEqual(self.keys(e), ["slo/success/ticket"])
        e.deliver_alerts()
        pd = self.bodies("/pd")
        self.assertEqual([b["event_action"] for b in pd], ["trigger", "resolve"])
        self.assertEqual(pd[1]["dedup_key"], pd[0]["dedup_key"])
        check_pagerduty(self, pd[1])
        self.offset = 73 * 3600
        e.check_slos()
        self.assertEqual(self.keys(e), [])

    def test_a_restart_neither_repeats_nor_forgets(self):
        e = self.engine(self.hooks)
        self.burst(e)
        e.check_slos()
        e.deliver_alerts()
        e.con.close()
        e = self.engine(self.hooks)
        e.refresh(force=True)
        self.assertEqual(e.check_slos(), [], "a restart does not page again")
        self.offset = 7 * 3600
        e.check_slos()
        e.deliver_alerts()
        self.assertEqual([b["event_action"] for b in self.bodies("/pd")], ["trigger", "resolve"])

    def test_a_small_sample_does_not_page(self):
        e = self.engine(self.hooks)
        self.burst(e, failed=3, ok=2)               # 60% failing, but only five tasks
        self.assertEqual(e.check_slos(), [])

    def test_a_healthy_service_does_not_page(self):
        e = self.engine(self.hooks)
        self.burst(e, failed=1, ok=29)              # 3.3% failing against a 10% budget: burn 0.33
        self.assertEqual(e.check_slos(), [])

    def test_the_burn_rate_is_on_metrics(self):
        e = self.engine(self.hooks)
        self.burst(e)
        e.check_slos()
        text = Api(e).prometheus()
        self.assertIn('agentdynamics_slo_burn_rate{slo="success",window="1h"} 5.0', text)
        self.assertIn('agentdynamics_slo_burn_threshold{slo="success",window="1h"} 3.36', text)
        self.assertIn('agentdynamics_slo_alert_firing{slo="success",alert="page"} 1', text)


class BurnMathTest(unittest.TestCase):
    def test_thresholds_are_the_workbooks_for_a_30_day_objective(self):
        s = {"window_days": 30}
        self.assertEqual([round(slo.burn_threshold(s, p), 2) for p in slo.BURN_POLICIES], [14.4, 6.0, 1.0])


class CliTest(AlertTest):
    def test_alerts_test_reaches_every_destination(self):
        with open(os.path.join(self.tmp, "agentdynamics.toml"), "w", encoding="utf-8") as f:
            f.write(f'[[alerts.webhooks]]\nname = "json"\nurl = "{self.rx.url}/json"\n'
                    f'[[alerts.webhooks]]\nname = "pd"\nformat = "pagerduty"\nurl = "{self.rx.url}/pd"\n'
                    f'routing_key_env = "AD_TEST_PD_KEY"\n')
        env = dict(os.environ, AD_TEST_PD_KEY="RKFROMENV", PYTHONPATH=ROOT)
        env.pop("AGENTDYNAMICS_CONFIG", None)
        out = subprocess.run([sys.executable, "-m", "agentdynamics", "--data", self.tmp, "--claude-root", "",
                              "alerts", "test"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("test alert", self.bodies("/json")[0]["slo_alerts"][0]["summary"])
        pd = self.bodies("/pd")
        self.assertEqual([b["event_action"] for b in pd], ["trigger", "resolve"], "a test leaves no open incident")
        for b in pd:
            check_pagerduty(self, b)
            self.assertEqual(b["routing_key"], "RKFROMENV")
        self.rx.fail = [400]
        out = subprocess.run([sys.executable, "-m", "agentdynamics", "--data", self.tmp, "--claude-root", "",
                              "alerts", "test", "--to", "json"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 1)
        self.assertIn("FAILED", out.stdout)


if __name__ == "__main__":
    unittest.main()
