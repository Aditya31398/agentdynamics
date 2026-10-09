"""The API's field names are a contract (CLAUDE.md, invariant 7): dashboards, alert consumers, scripts and
Prometheus queries read them. This holds every GET route to the fields it answered with when the contract was last
taken (tests/api_contract.json), `/healthz` (probes read it), and the health-rule ids and Prometheus metric
names besides. A new field is
fine. A field that disappears or is renamed fails here, with the route and the field.

Changing the contract on purpose -- deprecating and then removing a field in a later minor release, as
docs/STABILITY.md describes -- is one command, and its diff is the review:

    python tests/test_api_contract.py --update

Data-shaped keys (dates, model names, rule ids: anything but lowercase words) are not fields and are left out.
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

from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

CONTRACT = os.path.join(os.path.dirname(__file__), "api_contract.json")
FIELD = re.compile(r"^[a-z][a-z0-9_]*$")
T = time.time() - 1800


def run(rid, project, error=False):
    steps = [{"kind": "prompt", "ts": T, "text": f"request {rid}"},
             {"kind": "span", "ts": T, "end_ts": T + 4, "span_kind": "node", "name": "plan", "node": "plan"},
             {"kind": "llm", "ts": T, "end_ts": T + 1, "model": "claude-sonnet-5", "input_tokens": 900,
              "output_tokens": 120, "cache_read": 300, "stop_reason": "end_turn"},
             {"kind": "tool", "ts": T + 1, "end_ts": T + 2, "name": "search", "is_error": error, "governed": True,
              "grant_depth": 0, "input": {"q": rid}, "error": "timeout" if error else None, "agent": "helper",
              "rule": "kernel.admitted"},
             {"kind": "tool", "ts": T + 3, "end_ts": T + 3.1, "name": "pay", "governed": True, "agent": "helper",
              "denied": True, "rule": "capability.not_granted"}]
    return {"id": rid, "project": project, "workflow": "flow", "thread_id": f"{project}-thread", "user_id": "u1",
            "metadata": {"tenant_id": "acme"}, "steps": steps, "status": "error" if error else "ok",
            "error": "crashed" if error else None, "policy_version": "pol@v1#abc",
            "policy": {"name": "pol", "doc": {"name": "pol", "tools": {"allow": [{"name": "search"}]}}},
            "feedback": [{"key": "user", "score": 0.8}]}


def fields(obj, prefix=""):
    """Every field path in a JSON value: `a.b`, `rows[].cost`. Keys that look like data are skipped."""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str) or not FIELD.match(k):
                continue
            p = f"{prefix}.{k}" if prefix else k
            out.add(p)
            out |= fields(v, p)
    elif isinstance(obj, list):
        for v in obj[:50]:
            out |= fields(v, prefix + "[]")
    return out


def snapshot():
    tmp = tempfile.mkdtemp()
    try:
        eng = Engine(os.path.join(tmp, "data"), None)
        for i in range(6):
            eng.ingest(run(f"a-{i}", "alpha", error=i == 0))
        eng.refresh(force=True)
        eng.revoke(agent="helper", project="alpha", reason="probing", minutes=60)
        eng.refresh(force=True)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"api": Api(eng)}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            with open(os.path.join(ROOT, "agentdynamics", "server.py"), encoding="utf-8") as f:
                routes = sorted(set(re.findall(r'"(/api/[a-z/]+)"', f.read())) - {"/api/whoami"}) + ["/healthz"]
            task = json.load(urllib.request.urlopen(url + "/api/tasks?days=", timeout=60))["tasks"][0]["id"]
            incident = eng.con.execute("SELECT id FROM incidents").fetchone()[0]
            out = {}
            for r in routes + [f"/api/task/{quote(task, safe='')}", f"/api/incident/{quote(incident, safe='')}"]:
                if r.endswith("/"):
                    continue
                q = "?days=&name=flow&type=flow&group=project&metrics=tasks,cost&dim=project&a=alpha&b=alpha"
                try:
                    with urllib.request.urlopen(url + r + q, timeout=60) as resp:
                        body = json.load(resp)
                except urllib.error.HTTPError:
                    continue                  # a POST-only route, or one this fixture can't answer
                key = re.sub(r"/api/(task|incident)/.*", r"/api/\1/{id}", r)
                out[key] = sorted(fields(body))
            with urllib.request.urlopen(url + "/metrics", timeout=60) as resp:
                text = resp.read().decode()
            out["/metrics"] = sorted({m.group(1) for m in re.finditer(r"^# TYPE (\S+)", text, re.M)})
            out["rules"] = sorted(r["id"] for r in eng.rules())
            return out
        finally:
            srv.shutdown()
            srv.server_close()
            eng.con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class ApiContractTest(unittest.TestCase):
    def test_no_field_disappears(self):
        with open(CONTRACT, encoding="utf-8") as f:
            contract = json.load(f)
        now = snapshot()
        missing = []
        for route, names in contract.items():
            have = set(now.get(route, ()))
            missing += [f"{route}: {n}" for n in names if n not in have]
        self.assertEqual(missing, [], "API fields, health-rule ids or metric names went missing. They are a contract "
                                      "(CLAUDE.md, invariant 7): deprecate first (docs/STABILITY.md), then update "
                                      "the contract on purpose: python tests/test_api_contract.py --update")
        self.assertGreater(len(contract), 25, "the contract covers the API")


if __name__ == "__main__":
    if "--update" in sys.argv:
        with open(CONTRACT, "w", encoding="utf-8") as f:
            json.dump(snapshot(), f, indent=1, sort_keys=True)
            f.write("\n")
        print(f"wrote {CONTRACT}")
    else:
        unittest.main()
