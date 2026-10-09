"""Outcomes from the systems that know them, by signed webhook (outcome_hooks.py).

A pull request merged, closed or reverted grades the work on its branch, Claude Code sessions included (they carry
their branch); a generic hook takes /api/outcomes' own body, signed. No API key: the signature is the credential,
so a delivery that isn't signed with the hook's secret grades nothing.
"""
import hashlib
import hmac
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

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_core import Builder  # noqa: E402

from agentdynamics import config  # noqa: E402
from agentdynamics.collectors import claude_code  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler, warnings_for  # noqa: E402

NOW = time.time() - 3600
GH, SUP = "gh-secret-0123456789", "support-secret-0123456789"


def run(rid, project, metadata):
    return {"id": rid, "project": project, "workflow": "w", "metadata": metadata, "steps": [
        {"kind": "prompt", "ts": NOW, "text": f"work for {rid}"},
        {"kind": "llm", "ts": NOW, "end_ts": NOW + 1, "model": "claude-sonnet-5", "input_tokens": 100, "output_tokens": 10}]}


def pr(action="closed", merged=True, branch="feature-x", default="main", number=7):
    return {"action": action, "number": number,
            "pull_request": {"number": number, "merged": merged, "head": {"ref": branch},
                             "html_url": f"https://github.com/o/r/pull/{number}"},
            "repository": {"full_name": "o/r", "default_branch": default}}


class OutcomeHooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for k, v in (("AD_TEST_GH", GH), ("AD_TEST_SUP", SUP)):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)
        # a Claude Code session that started on main and ended on its feature branch
        root = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(root, "E--repo"))
        b = Builder("cc-1")
        b.user("Add a login page", gitBranch="main")
        b.tool("t1", "Edit", {"file_path": "login.py", "old_string": "a", "new_string": "b"})
        b.user("now write the tests", gitBranch="feature-login")
        b.assistant([{"type": "text", "text": "Done."}], stop="end_turn")
        b.write(os.path.join(root, "E--repo", "cc-1.jsonl"))
        cfg = config.load(self.tmp)
        cfg["auth"] = {"enabled": True, "keys": [{"name": "app", "role": "ingest", "key": "k-ingest-0123456789abcdef"}]}
        cfg["outcomes"] = {"webhooks": [
            {"name": "github", "provider": "github", "secret_env": "AD_TEST_GH", "key": "branch"},
            {"name": "support", "provider": "generic", "secret_env": "AD_TEST_SUP", "projects": ["support"]},
            {"name": "unset", "provider": "generic", "secret_env": "AD_TEST_NOT_SET"}]}
        self.e = Engine(os.path.join(self.tmp, "data"), root, cfg=cfg)
        self.addCleanup(self.e.con.close)
        for rid, project, md in (("g1", "checkout", {"branch": "feature-x"}), ("g2", "checkout", {"branch": "feature-x"}),
                                 ("g3", "checkout", {"branch": "feature-y"}), ("g4", "checkout", {"branch": "main"}),
                                 ("s1", "support", {"ticket_id": "T-1"}), ("s2", "billing", {"ticket_id": "T-1"})):
            self.e.ingest(run(rid, project, md))
        self.e.refresh(force=True)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"api": Api(self.e)}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.url = f"http://127.0.0.1:{srv.server_address[1]}"

    def deliver(self, name, payload, secret=GH, event="pull_request", header=None, sig=None):
        body = json.dumps(payload).encode()
        header = header or ("X-Hub-Signature-256" if name == "github" else "X-AgentDynamics-Signature")
        headers = {"Content-Type": "application/json", "X-GitHub-Event": event}
        if secret is not None:
            headers[header] = sig or "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(f"{self.url}/hooks/{name}", data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read() or b"{}")

    def outcomes(self, run_id):
        return [tuple(r) for r in self.e.con.execute(
            "SELECT outcome, outcome_source, outcome_reason FROM tasks WHERE run_id = ? ORDER BY idx", (run_id,))]

    def test_a_merged_pull_request_completes_the_work_on_its_branch(self):
        st, body = self.deliver("github", pr(merged=True))
        self.assertEqual((st, body["graded"]), (200, 1))
        for rid in ("g1", "g2"):
            self.assertEqual(self.outcomes(rid), [("completed", "graded", "branch=feature-x: merged: https://github.com/o/r/pull/7")])
        self.assertEqual(self.outcomes("g3")[0][1], "inferred", "another branch is untouched")
        graded_by = self.e.con.execute("SELECT graded_by FROM outcome_keys").fetchone()[0]
        self.assertEqual(graded_by, "webhook:github")

    def test_closed_without_merging_fails_and_a_revert_is_rework(self):
        self.deliver("github", pr(merged=False, branch="feature-y"))
        self.assertEqual(self.outcomes("g3")[0][:2], ("failed", "graded"))
        self.deliver("github", pr(merged=True))
        self.deliver("github", pr(merged=True, branch="revert-7-feature-x", number=8))
        self.assertEqual(self.outcomes("g1")[0], ("rework", "graded", "branch=feature-x: reverted by https://github.com/o/r/pull/8"))

    def test_only_closing_events_about_feature_branches_count(self):
        for payload, event in ((pr(action="opened"), "pull_request"), (pr(branch="main"), "pull_request"),
                               (pr(branch="revert-3-main", merged=True), "pull_request"), ({"zen": "hi"}, "ping"),
                               (pr(), "push")):
            st, body = self.deliver("github", payload, event=event)
            self.assertEqual((st, body.get("graded")), (202, 0), (payload.get("action"), event))
        self.assertEqual(self.outcomes("g4")[0][1], "inferred", "work on the default branch is never graded so")

    def test_an_unsigned_or_missigned_delivery_grades_nothing(self):
        for secret, sig in ((None, None), ("wrong-secret", None), (GH, "sha256=" + "0" * 64), (GH, "sha1=abc")):
            st, body = self.deliver("github", pr(), secret=secret, sig=sig)
            self.assertEqual(st, 401, (secret, sig))
        st, _ = self.deliver("unset", {"key": {"ticket_id": "T-1"}, "outcome": "failed"}, secret="")
        self.assertEqual(st, 401, "a hook whose secret isn't set accepts nothing, even unsigned")
        self.assertEqual(self.e.con.execute("SELECT COUNT(*) FROM outcome_keys").fetchone()[0], 0)
        self.assertEqual(self.deliver("nope", pr())[0], 404)
        self.assertIn("'unset' has no secret", " ".join(warnings_for(self.e.cfg, "127.0.0.1", False)))

    def test_a_claude_code_session_is_graded_by_the_branch_it_ended_on(self):
        rid = "cc-1"
        self.assertEqual(claude_code.parse_file(os.path.join(self.e.claude_root, "E--repo", "cc-1.jsonl"),
                                                self.e.claude_root)["metadata"], {"branch": "feature-login"})
        self.deliver("github", pr(branch="feature-login"))
        self.assertTrue(self.outcomes(rid))
        self.assertTrue(all(o[:2] == ("completed", "graded") for o in self.outcomes(rid)), self.outcomes(rid))

    def test_a_generic_hook_states_outcomes_within_its_projects(self):
        st, body = self.deliver("support", {"key": {"ticket_id": "T-1"}, "outcome": "failed", "reason": "reopened"},
                                secret=SUP)
        self.assertEqual((st, body["graded"]), (200, 1))
        self.assertEqual(self.outcomes("s1")[0], ("failed", "graded", "ticket_id=T-1: reopened"))
        self.assertEqual(self.outcomes("s2")[0][1], "inferred", "the same ticket id in another project is not its own")
        other = self.e.con.execute("SELECT id FROM tasks WHERE run_id = 'g1'").fetchone()[0]
        st, body = self.deliver("support", [{"task_id": other, "outcome": "failed"}], secret=SUP)
        self.assertEqual(st, 403)
        st, _ = self.deliver("support", {"key": {"ticket_id": "T-1"}, "outcome": "great"}, secret=SUP)
        self.assertEqual(st, 400)
        st, _ = self.deliver("support", {"key": {"ticket_id": "T-1"}, "outcome": "failed"}, secret=GH)
        self.assertEqual(st, 401, "the other hook's secret is no good here")

    def test_api_outcomes_still_work_with_a_key(self):
        req = urllib.request.Request(f"{self.url}/api/outcomes", method="POST",
                                     data=json.dumps([{"key": {"branch": "feature-y"}, "outcome": "completed",
                                                       "match": "all"}]).encode(),
                                     headers={"Authorization": "Bearer k-ingest-0123456789abcdef"})
        with urllib.request.urlopen(req, timeout=30) as r:
            self.assertEqual(json.loads(r.read())["graded"], 1)
        self.assertEqual(self.outcomes("g3")[0][:2], ("completed", "graded"))


if __name__ == "__main__":
    unittest.main()
