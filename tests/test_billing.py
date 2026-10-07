"""What the provider billed beside what was estimated (collectors/billing.py, /api/billing).

A local stand-in for Anthropic's Usage & Cost Admin API serves the cost report in its documented shape (amounts as
decimal strings in cents, daily buckets, group_by description, pagination by next_page); never the real one. What
must hold: the request is the documented one, with the key from the environment; cents become dollars; pagination
is followed; a re-pull replaces its days rather than adding to them; the comparison sets the bill beside the
estimate per model and day, over the days both cover; a scoped key can't read it; no key is a source error, not
an exception."""
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentdynamics import config  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

DAY = 86400
KEY = "test-admin-key-not-real"


def day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class FakeCostApi(BaseHTTPRequestHandler):
    """Two pages: today and yesterday, Claude Sonnet 5 tokens plus a web search line."""
    requests = []
    amounts = {"input": "120.5", "output": "80"}            # cents

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        FakeCostApi.requests.append({"path": u.path, "q": q, "key": self.headers.get("x-api-key"),
                                     "version": self.headers.get("anthropic-version")})
        if self.headers.get("x-api-key") != KEY:
            return self._send(401, {"type": "error", "error": {"type": "authentication_error"}})
        now = time.time()
        today, yesterday = day(now), day(now - DAY)
        line = lambda tt, amt: {"amount": amt, "currency": "USD", "cost_type": "tokens", "model": "claude-sonnet-5",  # noqa: E731
                                "description": f"Claude Sonnet 5 Usage - {tt}", "token_type": tt, "service_tier": "standard",
                                "context_window": "0-200k", "inference_geo": "global", "workspace_id": None}
        if "page" not in q:
            body = {"data": [{"starting_at": f"{yesterday}T00:00:00Z", "ending_at": f"{today}T00:00:00Z", "results": [
                line("uncached_input_tokens", self.amounts["input"]), line("output_tokens", self.amounts["output"])]}],
                "has_more": True, "next_page": "page_2"}
        else:
            body = {"data": [{"starting_at": f"{today}T00:00:00Z", "ending_at": f"{today}T23:59:59Z", "results": [
                line("uncached_input_tokens", "50"),
                {"amount": "1000", "currency": "USD", "cost_type": "web_search", "model": None,
                 "description": "Web Search Usage", "token_type": None, "service_tier": None, "context_window": None,
                 "inference_geo": None, "workspace_id": None}]}], "has_more": False, "next_page": None}
        self._send(200, body)

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class BillingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        FakeCostApi.requests = []
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), FakeCostApi)
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)
        old = os.environ.get("TEST_ADMIN_KEY")
        os.environ["TEST_ADMIN_KEY"] = KEY
        self.addCleanup(lambda: os.environ.pop("TEST_ADMIN_KEY", None) if old is None
                        else os.environ.__setitem__("TEST_ADMIN_KEY", old))
        cfg = config.load(self.tmp)
        cfg["sources"] = [{"type": "anthropic_costs", "name": "bill", "admin_key_env": "TEST_ADMIN_KEY", "days": 3,
                           "base_url": f"http://127.0.0.1:{self.api.server_address[1]}"}]
        self.e = Engine(os.path.join(self.tmp, "data"), None, cfg=cfg)
        self.addCleanup(self.e.con.close)
        now = time.time()
        for i, (ts, tokens) in enumerate(((now - DAY + 60, 400_000), (now - 120, 100_000))):
            self.e.ingest({"id": f"r{i}", "project": "p", "workflow": "w", "steps": [
                {"kind": "prompt", "ts": ts, "text": "x"},
                {"kind": "llm", "ts": ts, "end_ts": ts + 1, "model": "claude-sonnet-5", "input_tokens": tokens,
                 "output_tokens": 50_000}]})
        self.e.refresh(force=True)

    def pull(self):
        name, t, sc, st = self.e.pullers[0]
        self.e._run_puller(name, t, sc, st)
        return st

    def test_the_cost_report_is_pulled_as_documented(self):
        st = self.pull()
        self.assertIsNone(st.d["last_error"])
        first = FakeCostApi.requests[0]
        self.assertEqual((first["path"], first["key"], first["version"]),
                         ("/v1/organizations/cost_report", KEY, "2023-06-01"))
        self.assertEqual((first["q"]["group_by[]"], first["q"]["bucket_width"]), (["description"], ["1d"]))
        self.assertEqual(FakeCostApi.requests[1]["q"]["page"], ["page_2"], "the next page is followed")
        usd = dict(self.e.con.execute("SELECT token_type, SUM(usd) FROM billing_daily GROUP BY token_type").fetchall())
        self.assertAlmostEqual(usd["uncached_input_tokens"], 1.705, msg="cents, as decimal strings, to dollars")
        self.assertAlmostEqual(usd["output_tokens"], 0.80)

    def test_a_repull_replaces_its_days(self):
        self.pull()
        FakeCostApi.amounts = {"input": "200", "output": "80"}
        self.addCleanup(setattr, FakeCostApi, "amounts", {"input": "120.5", "output": "80"})
        self.pull()
        total = self.e.con.execute("SELECT SUM(usd) FROM billing_daily").fetchone()[0]
        self.assertAlmostEqual(total, 2.00 + 0.80 + 0.50 + 10.0, msg="the second pull's figures, once")

    def test_the_bill_beside_the_estimate(self):
        self.pull()
        b = Api(self.e).billing({"days": "7"})
        est = (400_000 * 2 + 50_000 * 10 + 100_000 * 2 + 50_000 * 10) / 1e6         # claude-sonnet-5 list price
        self.assertAlmostEqual(b["totals"]["estimated"], est, places=4)
        self.assertAlmostEqual(b["totals"]["billed"], 1.205 + 0.80 + 0.50, places=4)
        self.assertEqual([m["model"] for m in b["models"]], ["claude-sonnet-5"])
        self.assertAlmostEqual(b["totals"]["gap"], b["totals"]["billed"] - est, places=4)
        self.assertEqual(b["other"], {"web_search": 10.0}, "not token costs: shown, not compared")
        self.assertEqual(len(b["days"]), 2)

    def test_a_scoped_key_gets_nothing_of_the_organizations_bill(self):
        self.pull()
        self.e.cfg["auth"] = {"enabled": True, "keys": [{"name": "a", "role": "read", "key": "k-scoped", "projects": ["p"]},
                                                        {"name": "b", "role": "read", "key": "k-all"}]}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"api": Api(self.e)}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def get(key):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/billing",
                                         headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.load(r)
        st, body = get("k-scoped")
        self.assertEqual((st, body["totals"], body["models"]), (200, None, []), "an empty answer: the page loads clean")
        self.assertIsNotNone(get("k-all")[1]["totals"])

    def test_no_key_is_a_source_error_not_an_exception(self):
        os.environ.pop("TEST_ADMIN_KEY")
        st = self.pull()
        self.assertIn("Admin API key", st.d["last_error"])
        self.assertEqual(FakeCostApi.requests, [], "nothing is sent without a key")
        self.assertEqual(self.e.con.execute("SELECT COUNT(*) FROM billing_daily").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
