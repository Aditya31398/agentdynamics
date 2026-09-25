"""Policy coverage (#3): how much of the traffic that actually ran would a candidate policy refuse?

`aegis ratify` and `aegis drift` compare declarations; neither sees a single call. A tightening can be
narrower than its base, constitutional and drift-free, and still refuse production. Coverage replays
the recorded calls through Aegis's own CapabilityGuard, so it has to agree with enforcement exactly --
the central test here checks that against a real kernel.
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
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.govern import coverage  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

try:
    import yaml  # noqa: F401  (PyYAML ships with aegis-kernel)
    from aegis import Grant, PolicyViolation, ToolRegistry, build_kernel, parse_policy
    HAS_AEGIS = True
except ImportError:
    HAS_AEGIS = False

T = time.time() - 900

# What actually ran, all of it allowed at the time by a broad policy.
CALLS = (
    [("orders.lookup", {"order_id": f"ORD-{1000 + i}"}) for i in range(10)]
    + [("payments.refund", {"order_id": "ORD-1001", "amount": a, "reason": "damaged"})
       for a in (18.0, 42.5, 76.4, 129.99, 180.0)]
    + [("kb.search", {"query": "late delivery policy"}) for _ in range(4)]
    + [("email.send", {"to": "customer@shop.example", "body": "hello"})]
)

# The candidate someone wants to deploy. Every change is a *tightening*, so ratify and drift pass:
#   payments.refund  cap lowered 200 -> 100          refuses the 129.99 and 180.0 refunds
#   kb.search        now requires a `locale` argument  refuses every recorded search
#   email.send       dropped: "we never email"         refuses the one email
CANDIDATE = {
    "name": "support", "version": 2,
    "tools": {"allow": [
        {"name": "orders.lookup", "require_args": ["order_id"], "args": {"order_id": {"matches": "^ORD-[0-9]+$"}}},
        {"name": "payments.refund", "require_args": ["order_id", "amount", "reason"],
         "args": {"order_id": {"matches": "^ORD-[0-9]+$"}, "amount": {"max_value": 100},
                  "reason": {"one_of": ["damaged", "late"]}}},
        {"name": "kb.search", "require_args": ["query", "locale"],
         "args": {"query": {"max_len": 256}, "locale": {"one_of": ["en", "de"]}}},
    ]},
}


def governed_steps(calls=CALLS, task="t#0"):
    return [{"kind": "tool", "name": n, "denied": 0, "governed": 1, "task_id": task,
             "args_json": json.dumps(a)} for n, a in calls]


@unittest.skipUnless(HAS_AEGIS, "aegis-kernel not installed")
class CoverageTest(unittest.TestCase):
    def test_counts_per_tool_and_per_rule(self):
        cov = coverage(CANDIDATE, governed_steps())
        self.assertEqual((cov["calls"], cov["denied"]), (20, 7))
        self.assertAlmostEqual(cov["denied_fraction"], 0.35)
        rows = {r["tool"]: r for r in cov["by_tool"]}
        self.assertEqual(rows["kb.search"]["rules"], {"capability.missing_arg": 4})
        self.assertEqual(rows["payments.refund"]["rules"], {"capability.arg_max_value": 2})
        self.assertEqual(rows["email.send"]["rules"], {"capability.not_granted": 1})
        self.assertEqual(rows["orders.lookup"]["denied"], 0)
        self.assertEqual(cov["by_tool"][0]["tool"], "kb.search", "worst first")
        ex = {(e["tool"], e["rule"]): e for e in cov["examples"]}
        self.assertEqual(ex[("payments.refund", "capability.arg_max_value")]["value"], 129.99)

    def test_agrees_with_a_real_kernel_call_for_call(self):
        """The whole claim: on which tools and arguments are allowed, coverage says what enforcement
        would do. Replay every call through a kernel built from the candidate and require the same
        verdict for each. The budget is generous and each call gets a fresh grant, so the only
        verdicts that can differ are capability ones -- budgets are cumulative per run and are
        reported as headroom instead."""
        pol = parse_policy(dict(CANDIDATE, budget={"usd": 100.0, "tokens": 10**9, "wall_clock_s": 3600,
                                                   "tool_calls": 10**6}), source="candidate")
        registry = ToolRegistry()
        for name in {n for n, _ in CALLS}:
            registry.register(name, lambda **kw: "ok", effects={"read"})
        kernel, _ = build_kernel(pol, registry, constitution=_permissive())
        enforced = []
        for name, args in CALLS:
            try:
                kernel.invoke(Grant.root(pol), name, **args)
                enforced.append(None)
            except PolicyViolation as ex:
                enforced.append(ex.verdict.rule)
        judged = []
        for name, args in CALLS:
            cov = coverage(CANDIDATE, governed_steps([(name, args)]))
            judged.append(next(iter(cov["by_tool"][0]["rules"]), None) if cov["denied"] else None)
        self.assertEqual(judged, enforced, "coverage and the kernel disagree on some call")
        self.assertEqual(sum(1 for r in enforced if r), 7)

    def test_plain_tools_the_policy_never_covers_are_not_counted(self):
        """draft_reply ran as an @tool function no kernel mediates. Leaving it out of a policy
        refuses nothing, so it must not show up as denied."""
        steps = governed_steps() + [{"kind": "tool", "name": "draft_reply", "denied": 0,
                                     "args_json": json.dumps({"text": "hi"})}]
        cov = coverage(CANDIDATE, steps)
        self.assertEqual(cov["calls"], 20)
        self.assertNotIn("draft_reply", {r["tool"] for r in cov["by_tool"]})

    def test_calls_that_were_refused_at_the_time_are_not_counted(self):
        steps = governed_steps() + [{"kind": "tool", "name": "payments.refund", "denied": 1, "governed": 1,
                                     "args_json": json.dumps({"amount": 950})}]
        self.assertEqual(coverage(CANDIDATE, steps)["calls"], 20)

    def test_unrecorded_arguments_are_not_failed_as_missing(self):
        """With content capture off there are no arguments to check. Judging them would report every
        call as missing its required arguments -- 100% refused, all of it false."""
        steps = [{"kind": "tool", "name": n, "denied": 0, "governed": 1} for n, _ in CALLS]
        cov = coverage(CANDIDATE, steps)
        self.assertEqual(cov["args_unrecorded"], 20)
        # only the tool grant can be judged: email.send was dropped, everything else is granted
        self.assertEqual(cov["denied"], 1)
        self.assertEqual(cov["by_tool"][0]["rules"], {"capability.not_granted": 1})

    def test_the_base_policy_denies_nothing_it_allowed(self):
        broad = {"name": "support", "version": 1, "tools": {"allow": [
            {"name": n} for n in sorted({n for n, _ in CALLS})]}}
        self.assertEqual(coverage(broad, governed_steps())["denied"], 0)


def _permissive():
    """A constitution that ratifies anything: this test is about capability verdicts, and the
    candidate is deliberately minimal (no data or budget sections) to keep it readable."""
    class Anything:
        def ratify(self, policy, registry):
            return None
    return Anything()


@unittest.skipUnless(HAS_AEGIS, "aegis-kernel not installed")
class CheckCommandTest(unittest.TestCase):
    """`agentdynamics policy check` and POST /api/policy/check: gate a policy change in CI."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.data = os.path.join(cls.tmp, "data")
        eng = Engine(cls.data, None)
        steps = [{"kind": "prompt", "ts": T, "text": "refund please"}]
        for i, (name, args) in enumerate(CALLS):
            steps.append({"kind": "tool", "ts": T + i, "end_ts": T + i + 0.5, "name": name,
                          "input": args, "governed": True, "grant_depth": 0})
        eng.ingest({"id": "cov-run", "project": "cov", "workflow": "support", "steps": steps,
                    "policy_version": "support@v1#x", "policy": {"name": "support", "doc": {"name": "support"}}})
        eng.refresh(force=True)
        eng.con.close()
        import yaml
        cls.candidate = os.path.join(cls.tmp, "candidate.yaml")
        cls.base = os.path.join(cls.tmp, "base.yaml")
        with open(cls.candidate, "w", encoding="utf-8") as f:
            yaml.safe_dump(dict(CANDIDATE, budget={"usd": 1.0, "tokens": 10000, "wall_clock_s": 60, "tool_calls": 50}), f)
        with open(cls.base, "w", encoding="utf-8") as f:
            yaml.safe_dump({"name": "support", "version": 1,
                            "tools": {"allow": [{"name": n} for n in sorted({n for n, _ in CALLS})]},
                            "budget": {"usd": 1.0, "tokens": 10000, "wall_clock_s": 60, "tool_calls": 50}}, f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def check(self, policy_file, *extra):
        return subprocess.run([sys.executable, "-m", "agentdynamics", "--data", self.data, "--claude-root", "",
                               "policy", "check", "--candidate", policy_file, *extra],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_a_policy_that_refuses_observed_traffic_fails_the_build(self):
        p = self.check(self.candidate)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("would deny 7 of 20", p.stdout)
        self.assertIn("kb.search", p.stdout)
        self.assertIn("capability.missing_arg", p.stdout)

    def test_the_threshold_lets_a_deliberate_tightening_through(self):
        self.assertEqual(self.check(self.candidate, "--max-denied-fraction", "0.4").returncode, 0)
        self.assertEqual(self.check(self.candidate, "--max-denied-fraction", "0.3").returncode, 1)

    def test_a_policy_that_refuses_nothing_passes(self):
        p = self.check(self.base)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("would deny 0 of 20", p.stdout)

    def test_the_http_endpoint_needs_only_a_read_key(self):
        eng = Engine(self.data, None)
        eng.refresh(force=True)
        eng.cfg["auth"] = {"enabled": True, "keys": [{"name": "ci", "role": "read", "key": "k-read"}]}
        Handler.api = Api(eng)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/policy/check",
                                         data=json.dumps({"candidate": CANDIDATE, "workflow": "support"}).encode(),
                                         method="POST", headers={"Authorization": "Bearer k-read",
                                                                 "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                cov = json.load(r)["coverage"]
            self.assertEqual((cov["calls"], cov["denied"]), (20, 7))
        finally:
            srv.shutdown()
            srv.server_close()
            eng.con.close()


if __name__ == "__main__":
    unittest.main()
