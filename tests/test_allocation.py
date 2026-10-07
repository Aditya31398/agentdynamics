"""Who the spend was for, why tools fail, and how sure an objective's value is.

  * cost per user and per tenant (chargeback): Analytics groups by the run's user and by a tenant named in the
    trace's metadata ([analysis] tenant_key; "tenant" or "tenant_id" by default);
  * tool errors by cause, from their messages: rate limit, timeout, permission, not found, bad input, upstream;
  * a success-rate objective's value with its 95% interval: 9 of 10 and 900 of 1,000 are both 90%.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentdynamics import config, slo  # noqa: E402
from agentdynamics.analysis import error_cause  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api  # noqa: E402

NOW = time.time() - 3600


def run(rid, user, tenant_md, tokens=1000, error=None):
    p = {"id": rid, "project": "p", "workflow": "w", "user_id": user, "metadata": tenant_md, "steps": [
        {"kind": "prompt", "ts": NOW, "text": "x"},
        {"kind": "llm", "ts": NOW, "end_ts": NOW + 1, "model": "claude-sonnet-5", "input_tokens": tokens, "output_tokens": 0},
        {"kind": "tool", "ts": NOW + 1, "end_ts": NOW + 2, "name": "payments", "input": {"a": 1},
         "is_error": bool(error), "error": error}]}
    return p


class AllocationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def engine(self, tenant_key=None):
        cfg = config.load(self.tmp)
        if tenant_key:
            cfg["analysis"]["tenant_key"] = tenant_key
        e = Engine(os.path.join(self.tmp, "data"), None, cfg=cfg)
        self.addCleanup(e.con.close)
        e.ingest(run("a1", "alice", {"tenant_id": "acme", "org": "o1"}, 1000, "TimeoutError: payments timed out"))
        e.ingest(run("a2", "alice", {"tenant_id": "acme", "org": "o1"}, 3000, "HTTP 429 Too Many Requests"))
        e.ingest(run("b1", "bob", {"tenant_id": "globex", "org": "o2"}, 5000, "ValueError: invalid amount"))
        e.refresh(force=True)
        return e

    def by(self, e, group):
        rows = Api(e).analytics({"group": group, "metrics": "tasks,cost", "days": ""})["rows"]
        return {r["grp"]: (r["tasks"], round(r["cost"], 6)) for r in rows}

    def test_cost_per_user_and_per_tenant(self):
        e = self.engine()
        self.assertEqual(self.by(e, "user"), {"alice": (2, 0.008), "bob": (1, 0.01)})
        self.assertEqual(self.by(e, "tenant"), {"acme": (2, 0.008), "globex": (1, 0.01)})
        self.assertIn("tenant", Api(e).analytics({"group": "tenant", "days": ""})["available"]["groups"])

    def test_the_tenant_key_is_yours_to_name(self):
        e = self.engine(tenant_key="org")
        self.assertEqual(self.by(e, "tenant"), {"o1": (2, 0.008), "o2": (1, 0.01)})

    def test_tool_errors_by_cause(self):
        e = self.engine()
        tool = Api(e).tools({"days": ""})["tools"][0]
        self.assertEqual(tool["causes"], {"timeout": 1, "rate limit": 1, "bad input": 1})
        for text, cause in (("403 Forbidden", "permission"), ("ENOENT: no such file", "not found"),
                            ("502 Bad Gateway", "upstream"), ("boom", "other"), (None, "other")):
            self.assertEqual(error_cause(text), cause, text)

    def test_a_success_objective_says_how_sure_it_is(self):
        tasks = [{"started": time.time() - 60, "outcome": "completed" if i < 9 else "failed"} for i in range(10)]
        r = slo.evaluate(tasks, {"id": "s", "metric": "success_rate", "op": ">=", "target": 0.8, "window_days": 7})
        self.assertEqual(r["value"], 0.9)
        self.assertLess(r["value_ci"][0], 0.6, "nine of ten is weak evidence of 90%")
        many = [dict(t, outcome="completed" if i % 10 else "failed") for i, t in enumerate(tasks * 100)]
        r = slo.evaluate(many, {"id": "s", "metric": "success_rate", "op": ">=", "target": 0.8, "window_days": 7})
        self.assertGreater(r["value_ci"][0], 0.87)


if __name__ == "__main__":
    unittest.main()
