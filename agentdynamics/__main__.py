"""CLI.

  agentdynamics serve                 start console + receivers (default command)
  agentdynamics run <cmd ...>         run a Python program with zero-code instrumentation
  agentdynamics connect <framework>   print the exact setup for a framework
  agentdynamics doctor                check connectivity/auth and send a test trace
  agentdynamics keys create|list|revoke
  agentdynamics report | ingest | push <file>
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

from .collectors.claude_code import DEFAULT_ROOT

DEFAULT_DATA = os.path.join(os.path.expanduser("~"), ".agentdynamics")
DEFAULT_URL = os.environ.get("AGENTDYNAMICS_URL", "http://127.0.0.1:8787")

SNIPPETS = {
    "python": ("Any Python agent (Anthropic / OpenAI SDKs, LangChain, LangGraph, OpenTelemetry auto-detected)", """\
pip install agentdynamics

# in your entry point, before creating clients:
import agentdynamics
agentdynamics.init(url="{url}", project="my-agent")   # api_key=... if auth is on

# optional: group each request into one task and name its stages
@agentdynamics.trace
def handle(question):
    with agentdynamics.span("plan"):
        ...

# or, with no code changes at all:
agentdynamics run python app.py"""),
    "langgraph": ("LangGraph / LangChain (Python or JS): environment variables only", """\
export LANGSMITH_TRACING=true
export LANGSMITH_ENDPOINT={url}/langsmith
export LANGSMITH_API_KEY=<ingest key, or anything when auth is off>
export LANGSMITH_PROJECT=my-agent
# works for langchain / langgraph (Python) and langchainjs / @langchain/langgraph (JS)"""),
    "otel": ("Anything that speaks OpenTelemetry (OpenAI Agents SDK, Strands, Semantic Kernel, Vercel AI SDK, CrewAI/LlamaIndex via OpenInference, Java/Go/.NET/JS)", """\
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT={url}/v1/traces
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Bearer <ingest key>"
export OTEL_SERVICE_NAME=my-agent
export OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=production"""),
    "langsmith": ("Keep LangSmith, copy runs into AgentDynamics (pull)", """\
# <data>/agentdynamics.toml
[[sources]]
type = "langsmith_api"
project = "my-agent-prod"
api_key_env = "LANGSMITH_API_KEY\""""),
    "langfuse": ("Keep Langfuse, copy traces into AgentDynamics (pull)", """\
# <data>/agentdynamics.toml
[[sources]]
type = "langfuse_api"
host = "https://cloud.langfuse.com"
public_key_env = "LANGFUSE_PUBLIC_KEY"
secret_key_env = "LANGFUSE_SECRET_KEY\""""),
    "logs": ("Log pipelines: Fluent Bit / Vector / Logstash HTTP output, or files", """\
POST {url}/api/ingest/records   (NDJSON or JSON array; OTLP JSON, LangSmith runs, Langfuse traces, spans)
Authorization: Bearer <ingest key>

# or tail a directory:  [[sources]] type = "inbox"  path = "/var/log/agent-traces\""""),
    "http": ("Any language, plain HTTP", """\
curl -X POST {url}/api/ingest -H 'Content-Type: application/json' -d '{{
  "workflow": "support_bot", "project": "helpdesk",
  "steps": [
    {{"kind": "prompt", "ts": 1726700000, "text": "Refund order 42"}},
    {{"kind": "llm", "ts": 1726700000, "end_ts": 1726700002, "model": "claude-sonnet-5",
     "input_tokens": 1200, "output_tokens": 300, "stop_reason": "end_turn"}}]}}'"""),
    "aegis": ("Agents governed by Aegis: every decision recorded, model spend gated, watchdog kill switch", """\
pip install agentdynamics aegis-kernel

import agentdynamics
from agentdynamics.integrations import aegis as governance
from aegis import build_kernel, load_policy

agentdynamics.init(url="{url}", project="my-agent")
kernel, root = build_kernel(load_policy("policy.yaml"), registry)
governance.instrument(kernel, root, watchdog=governance.Watchdog(max_repeated_denials=3))

# later: least-privilege policy from what the agent actually did
agentdynamics policy export --workflow my_workflow --base policy.yaml --out tightened.yaml"""),
    "claude-code": ("Claude Code", "Nothing to do: sessions in ~/.claude/projects are read automatically by `agentdynamics serve`."),
}


def _http(method, url, key=None, body=None, timeout=10):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def cmd_doctor(a):
    url, key = a.url.rstrip("/"), a.key or os.environ.get("AGENTDYNAMICS_API_KEY")
    ok = True

    def line(good, msg, hint=""):
        nonlocal ok
        ok &= good
        print(f"  [{'ok' if good else '!!'}] {msg}" + (f"\n        -> {hint}" if hint and not good else ""))

    print(f"AgentDynamics doctor - {url}")
    try:
        st, h = _http("GET", url + "/healthz")
    except OSError as ex:
        line(False, f"server reachable ({ex})", "start it with: agentdynamics serve   (or set AGENTDYNAMICS_URL)")
        return 1
    line(st == 200, f"server reachable (status {h and h.get('status')})")
    st, who = _http("GET", url + "/api/whoami", key)
    role = (who or {}).get("role") if st == 200 else ("ingest" if st == 403 else None)
    line(st in (200, 403), f"credentials accepted (role: {role or 'none'})",
         "set AGENTDYNAMICS_API_KEY, or create a key on the server: agentdynamics keys create --role ingest")
    run_id = f"doctor-{int(time.time())}"
    st, _ = _http("POST", url + "/api/ingest", key, {"id": run_id, "workflow": "doctor_check", "project": "agentdynamics-doctor", "steps": [
        {"kind": "prompt", "ts": time.time() - 2, "text": "connectivity check"},
        {"kind": "llm", "ts": time.time() - 2, "end_ts": time.time(), "model": "claude-haiku-4-5", "input_tokens": 10, "output_tokens": 5,
         "stop_reason": "end_turn"}]})
    line(st == 200, "test trace accepted", "the key needs the 'ingest' (or 'admin') role")
    if st == 200 and role in ("read", "admin"):
        _http("POST", url + "/api/refresh", key, {})
        st, d = _http("GET", url + "/api/tasks?project=agentdynamics-doctor&days=&limit=5", key)
        seen = st == 200 and any(t["run_id"] == run_id for t in (d or {}).get("tasks", []))
        line(seen, "test trace analyzed and visible in the console")
    if ok:
        print(f"\nAll good. Open {url} and look for project 'agentdynamics-doctor'.")
    else:
        print("\nFix the items above and run again.")
    return 0 if ok else 1


def cmd_keys(a, data):
    from .config import keys_path, load_keys, save_keys
    keys = load_keys(data)
    if a.action == "create":
        k = {"name": a.name or f"{a.role}-{len(keys) + 1}", "role": a.role, "key": f"ad_{a.role[0]}_{secrets.token_urlsafe(24)}",
             "created": int(time.time())}
        keys.append(k)
        save_keys(data, keys)
        print(f"Created {k['role']} key '{k['name']}':\n\n  {k['key']}\n\nAuth is now ON for the server using {keys_path(data)} (restart it).")
        if a.role == "ingest":
            print(f"Use it in your app:  export AGENTDYNAMICS_API_KEY={k['key']}")
    elif a.action == "list":
        if not keys:
            print("No keys: auth is off (local mode).")
        for k in keys:
            print(f"  {k['name']:<20} {k['role']:<7} {k['key'][:8]}...  created {time.strftime('%Y-%m-%d', time.localtime(k.get('created', 0)))}")
    elif a.action == "revoke":
        left = [k for k in keys if k["name"] != a.name and not k["key"].startswith(a.name or "\0")]
        save_keys(data, left)
        print(f"Revoked {len(keys) - len(left)} key(s).")


def cmd_policy(a, eng):
    from .server import Api
    api = Api(eng)
    q = {k: v for k, v in (("workflow", a.workflow), ("project", a.project), ("environment", a.environment),
                           ("days", a.days), ("policy", a.policy), ("headroom", a.headroom)) if v}
    if a.base:
        try:
            from aegis import dump_policy, load_policy
        except ImportError:
            print("--base needs aegis-kernel: pip install aegis-kernel", file=sys.stderr)
            return 2
        q["base_doc"] = dump_policy(load_policy(a.base))
    if a.action == "report":
        g = api.governance(q)
        k = g["kpis"]
        print(f"Governed tasks {k['governed_tasks']} - decisions {k['decisions']} - denials {k['denials']} "
              f"({k['denial_rate']:.1%} of tool calls) - budget stops {k['budget_stops']} - revocations {k['revocations']}")
        for p in g["policies"]:
            print()
            print(f"{p['policy']}: {p['tasks']} tasks, success {p['success_rate']:.0%}, {p['denials']} denials")
            print(f"  used {len(p['used'])} of {len(p['granted'])} granted tools; unused: {', '.join(p['unused']) or 'none'}")
            if p.get("ungoverned"):
                print(f"  called but not in the policy: {', '.join(p['ungoverned'])}")
            print("  budget headroom (limit / p95 used): " + ", ".join(f"{k2} {v}x" for k2, v in p["headroom"].items() if v))
        for r in g["by_rule"][:8]:
            print(f"  {r['n']:>5}  {r['rule']}")
        return 0
    res = api.export_policy(q)
    if res.get("error"):
        print(res["error"], file=sys.stderr)
        return 1
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(res["yaml"])
        print(f"wrote {a.out}: {len(res['changes'])} change(s) vs {res['base'] or 'no base'}", file=sys.stderr)
        for c in res["changes"]:
            print(f"  - {c}", file=sys.stderr)
    else:
        print(res["yaml"])
    return 0


def cmd_run(a):
    if not a.command:
        print("usage: agentdynamics run python app.py [args...]")
        return 2
    boot = os.path.join(os.path.dirname(__file__), "bootstrap")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (boot, root, env.get("PYTHONPATH")) if p)
    if a.project:
        env["AGENTDYNAMICS_PROJECT"] = a.project
    cmd = a.command[1:] if a.command[0] == "--" else a.command
    return subprocess.call(cmd, env=env)


def main(argv=None):
    from . import __version__
    ap = argparse.ArgumentParser(prog="agentdynamics", description="APM for AI agents")
    ap.add_argument("--version", action="version", version=f"agentdynamics {__version__}")
    ap.add_argument("--data", default=os.environ.get("AGENTDYNAMICS_DATA", DEFAULT_DATA), help="data directory")
    ap.add_argument("--claude-root", default=DEFAULT_ROOT, help="Claude Code projects dir ('' to disable)")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the console and receivers")
    s.add_argument("--host", default=None, help="default from config [server] or 127.0.0.1")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--interval", type=int, default=None, help="seconds between source re-scans")
    s.add_argument("--open", action="store_true", help="open the browser")
    r = sub.add_parser("run", help="run a Python program with zero-code instrumentation")
    r.add_argument("--project", default=None)
    r.add_argument("command", nargs=argparse.REMAINDER)
    c = sub.add_parser("connect", help="print setup instructions for a framework")
    c.add_argument("framework", nargs="?", choices=sorted(SNIPPETS), default=None)
    c.add_argument("--url", default=DEFAULT_URL)
    d = sub.add_parser("doctor", help="check connectivity and auth, send a test trace")
    d.add_argument("--url", default=DEFAULT_URL)
    d.add_argument("--key", default=None)
    k = sub.add_parser("keys", help="manage API keys")
    k.add_argument("action", choices=["create", "list", "revoke"])
    k.add_argument("--role", choices=["ingest", "read", "admin"], default="ingest")
    k.add_argument("--name", default=None)
    po = sub.add_parser("policy", help="Aegis policy from observed behaviour (observe -> govern)")
    po.add_argument("action", choices=["export", "report"])
    po.add_argument("--workflow")
    po.add_argument("--project")
    po.add_argument("--environment")
    po.add_argument("--days", type=float)
    po.add_argument("--policy", help="policy label as shown in the console (name@vN#digest)")
    po.add_argument("--base", help="Aegis policy file to tighten (needs aegis-kernel installed)")
    po.add_argument("--headroom", type=float, default=1.5)
    po.add_argument("--out", help="write the YAML here (default: stdout)")
    sub.add_parser("ingest", help="scan sources once and rebuild the database")
    rp = sub.add_parser("report", help="print a text summary")
    rp.add_argument("--project")
    rp.add_argument("--days", type=float)
    p = sub.add_parser("push", help="ingest a JSON file (OTLP export, LangSmith runs, Langfuse traces or generic run)")
    p.add_argument("file")
    a = ap.parse_args(argv)
    cmd = a.cmd or "serve"

    if cmd == "run":
        return cmd_run(a)
    if cmd == "doctor":
        return cmd_doctor(a)
    if cmd == "connect":
        items = [a.framework] if a.framework else list(SNIPPETS)
        for name in items:
            title, body = SNIPPETS[name]
            print(f"\n== {name}: {title}\n\n{body.format(url=a.url.rstrip('/'))}\n")
        return 0
    os.makedirs(a.data, exist_ok=True)
    if cmd == "keys":
        return cmd_keys(a, a.data)

    from .engine import Engine
    eng = Engine(a.data, a.claude_root or None)
    if cmd == "push":
        with open(a.file, encoding="utf-8") as f:
            doc = json.load(f)
        from .collectors.inbox import detect
        recs = doc if isinstance(doc, list) else [doc]
        print("accepted", eng.ingest_records([(detect(x), x) for x in recs if detect(x)]))
        eng.refresh()
        return 0
    eng.refresh(force=True)
    print(f"indexed in {eng.last_duration}s -> {os.path.join(a.data, 'agentdynamics.db')}", file=sys.stderr)
    if cmd == "ingest":
        return 0
    if cmd == "policy":
        return cmd_policy(a, eng)
    if cmd == "report":
        from .server import Api
        api = Api(eng)
        q = {k2: v for k2, v in (("project", a.project), ("days", a.days)) if v}
        o = api.overview(q)
        kp = o["kpis"]
        print(f"\nTasks {kp['tasks']}  sessions {kp['sessions']}  cost ${kp['cost']:.2f}  Apdex {kp['apdex']}  "
              f"success {kp['success_rate'] or 0:.0%}  tool errors {kp['tool_error_rate']:.1%}  waste ${kp['waste_cost']:.2f}")
        print("\nTask types:")
        for t in o["types"]:
            print(f"  {t['type']:<20} {t['tasks']:>4} tasks  ${t['cost']:>9.2f}  apdex {t['apdex']}  {t['health']}")
        print("\nProcess review:")
        for i in api.process(q)["insights"]:
            print(f"  [{i['severity']:>7}] {i['title']}: {i['metric']}\n            {i['detail']}")
        return 0
    host = a.host or os.environ.get("AGENTDYNAMICS_HOST") or eng.cfg["server"]["host"]
    port = a.port or int(os.environ.get("AGENTDYNAMICS_PORT") or eng.cfg["server"]["port"])
    if (a.interval or 1) > 0:
        eng.watch(a.interval)
    from .server import serve
    if a.open:
        webbrowser.open(f"http://{host}:{port}/#/start")
    serve(eng, host, port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
