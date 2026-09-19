"""Integration tests: every ingestion path through a live in-process server.

  * LangSmith SDK (real `langsmith` + `langchain_core` packages, LangGraph-style metadata) -> /langsmith
  * OTLP/HTTP JSON (OpenInference / LangGraph) with gzip           -> /v1/traces
  * OTLP/HTTP protobuf (OTel GenAI semconv, multi-agent handoffs)   -> /v1/traces
  * Log pipeline records (Fluent Bit envelopes, NDJSON)            -> /api/ingest/records
  * Inbox directory tailing                                        -> config source "inbox"
  * Pull connectors against mock LangSmith and Langfuse APIs
  * Auth roles, redaction, Prometheus, healthz
"""
import gzip
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from agentdynamics import config as cfgmod  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

try:
    import langchain_core  # noqa: F401
    import langsmith  # noqa: F401
    HAS_LANGCHAIN = True
except ImportError:
    HAS_LANGCHAIN = False

KEYS = {"ingest": "k_ingest_123", "read": "k_read_456", "admin": "k_admin_789"}
T = time.time() - 3600
KEYMAP = {"input_value": "input.value", "llm_model_name": "llm.model_name", "llm_token_count_prompt": "llm.token_count.prompt",
          "llm_token_count_completion": "llm.token_count.completion", "llm_finish_reason": "llm.finish_reason", "tool_name": "tool.name"}


# ---------------------------------------------------------------- tiny protobuf encoder (test-side)
def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _key(num, wt):
    return _varint(num << 3 | wt)


def _ld(num, b):
    return _key(num, 2) + _varint(len(b)) + b


def _str(num, s):
    return _ld(num, s.encode())


def _u64(num, v):
    return _key(num, 1) + struct.pack("<Q", int(v))


def _vi(num, v):
    return _key(num, 0) + _varint(v)


def _any(v):
    if isinstance(v, bool):
        return _vi(2, int(v))
    if isinstance(v, int):
        return _vi(3, v)
    if isinstance(v, float):
        return _key(4, 1) + struct.pack("<d", v)
    if isinstance(v, list):
        return _ld(5, b"".join(_ld(1, _any(x)) for x in v))
    return _str(1, str(v))


def _kv(k, v):
    return _str(1, k) + _ld(2, _any(v))


def pb_span(trace, span, parent, name, start, end, attrs, error=None):
    b = _ld(1, bytes.fromhex(trace)) + _ld(2, bytes.fromhex(span))
    if parent:
        b += _ld(4, bytes.fromhex(parent))
    b += _str(5, name) + _vi(6, 1) + _u64(7, start * 1e9) + _u64(8, end * 1e9)
    for k, v in attrs.items():
        b += _ld(9, _kv(k, v))
    if error:
        b += _ld(15, _str(2, error) + _vi(3, 2))
    return b


def pb_request(resource, spans):
    res = b"".join(_ld(1, _kv(k, v)) for k, v in resource.items())
    scope = _ld(1, _str(1, "opentelemetry.instrumentation.openai_agents")) + b"".join(_ld(2, s) for s in spans)
    return _ld(1, _ld(1, res) + _ld(2, scope))


# ---------------------------------------------------------------- mock vendor APIs
class MockVendor(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/sessions"):
            assert self.headers.get("x-api-key") == "ls-secret"
            return self._json([{"id": "sess-1", "name": "prod"}])
        if self.path.startswith("/api/public/traces/"):
            assert self.headers.get("Authorization", "").startswith("Basic ")
            tid = self.path.rsplit("/", 1)[-1]
            return self._json({"id": tid, "name": "rag_pipeline", "timestamp": iso(T), "input": {"question": "What is our refund policy?"},
                               "sessionId": "s-9", "userId": "u-1", "scores": [{"name": "helpfulness", "value": 0.2}],
                               "observations": [
                                   {"id": f"{tid}-r", "type": "RETRIEVER", "name": "vector_search", "startTime": iso(T + 0.1), "endTime": iso(T + 0.4),
                                    "output": {"documents": []}},
                                   {"id": f"{tid}-g", "type": "GENERATION", "name": "answer", "model": "claude-sonnet-5",
                                    "startTime": iso(T + 0.5), "completionStartTime": iso(T + 0.9), "endTime": iso(T + 2.0),
                                    "usageDetails": {"input": 1200, "output": 300}, "metadata": {"finish_reason": "end_turn"}}]})
        if self.path.startswith("/api/public/traces"):
            return self._json({"data": [{"id": "lf-1", "timestamp": iso(T)}, {"id": "lf-2", "timestamp": iso(T + 5)}],
                               "meta": {"page": 1, "totalPages": 1}})
        self._json({})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path.startswith("/runs/query"):
            assert body["session"] == ["sess-1"]
            if body.get("cursor"):
                return self._json({"runs": [ls_run("p2", "tr-9", None, "chain", "billing_agent", T + 10, T + 12)], "cursors": {"next": None}})
            return self._json({"runs": [ls_run("p1", "tr-9", "p2", "llm", "ChatAnthropic", T + 10.5, T + 11.5, tokens=(900, 120))],
                               "cursors": {"next": "c2"}})
        self._json({})


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def ls_run(rid, trace, parent, run_type, name, start, end, tokens=None):
    r = {"id": rid, "trace_id": trace, "parent_run_id": parent, "run_type": run_type, "name": name,
         "start_time": iso(start), "end_time": iso(end), "inputs": {"input": "Why was I billed twice?"}, "outputs": {},
         "session_name": "prod", "extra": {"metadata": {"ls_model_name": "claude-opus-5"}}}
    if tokens:
        r["prompt_tokens"], r["completion_tokens"] = tokens
    return r


def start(handler_cls, **kw):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.vendor, cls.vendor_url = start(MockVendor)
        cls.inbox = os.path.join(cls.tmp, "inbox")
        os.makedirs(cls.inbox)
        os.environ["TEST_LS_KEY"] = "ls-secret"
        os.environ["TEST_LF_PK"], os.environ["TEST_LF_SK"] = "pk", "sk"
        cfg = cfgmod.load(cls.tmp)
        cfg["auth"] = {"enabled": True, "keys": [{"name": r, "key": k, "role": r} for r, k in KEYS.items()]}
        cfg["sources"] = [
            {"type": "langsmith_api", "name": "ls-pull", "api_url": cls.vendor_url, "project": "prod", "api_key_env": "TEST_LS_KEY", "lookback_hours": 48},
            {"type": "langfuse_api", "name": "lf-pull", "host": cls.vendor_url, "public_key_env": "TEST_LF_PK", "secret_key_env": "TEST_LF_SK", "lookback_hours": 48},
            {"type": "inbox", "name": "inbox", "path": cls.inbox},
        ]
        cls.eng = Engine(os.path.join(cls.tmp, "data"), None, cfg)
        cls.eng.refresh(force=True)
        Handler.api = Api(cls.eng)
        cls.srv, cls.url = start(Handler)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.vendor.shutdown()
        cls.eng.con.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- helpers
    def req(self, path, body=None, key="admin", method=None, headers=None):
        h = dict(headers or {})
        if key:
            h["Authorization"] = f"Bearer {KEYS[key]}"
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        if data is not None and "Content-Type" not in h:
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(self.url + path, data=data, headers=h, method=method or ("POST" if data is not None else "GET"))
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw and resp.headers.get("Content-Type", "").startswith("application/json") else raw)
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def task(self, tid):
        from urllib.parse import quote
        return self.req(f"/api/task/{quote(tid, safe='')}", key="read")[1]["task"]

    def tasks(self, **q):
        from urllib.parse import urlencode
        return self.req("/api/tasks?" + urlencode({"limit": 1000, **q}), key="read")[1]["tasks"]

    # --- tests
    def test_1_auth_roles(self):
        self.assertEqual(self.req("/api/overview", key=None)[0], 401)
        self.assertEqual(self.req("/api/overview", key="ingest")[0], 403)
        self.assertEqual(self.req("/api/overview", key="read")[0], 200)
        self.assertEqual(self.req("/api/ingest", {"steps": []}, key="read")[0], 403)
        self.assertEqual(self.req("/api/rules", {"rules": []}, key="read")[0], 403)
        self.assertEqual(self.req("/healthz", key=None)[0], 200)

    @unittest.skipUnless(HAS_LANGCHAIN, "langchain-core / langsmith not installed")
    def test_2_langsmith_sdk_langgraph(self):
        env = dict(os.environ, LANGSMITH_ENDPOINT=self.url + "/langsmith", LANGCHAIN_ENDPOINT=self.url + "/langsmith",
                   LANGSMITH_API_KEY=KEYS["ingest"], LANGCHAIN_API_KEY=KEYS["ingest"], LANGSMITH_PROJECT="support-bot-prod",
                   LANGCHAIN_PROJECT="support-bot-prod", PYTHONPATH=ROOT)
        code = "import sys; sys.path.insert(0, 'examples'); import langgraph_style_app as a; a.run_demo(14, seed=3)"
        p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.eng.refresh()
        ts = self.tasks(workflow="support_graph")
        self.assertGreaterEqual(len(ts), 14)
        self.assertTrue(all(t["framework"] == "langgraph" for t in ts))
        self.assertTrue(any(t["outcome"] == "rework" for t in ts), "thread follow-up correction should mark rework")
        self.assertTrue(any(t["feedback_score"] is not None for t in ts), "LangSmith feedback should attach to runs")
        self.assertFalse(any(t["prompt"].startswith("{") for t in ts))
        wf = self.req("/api/workflow?name=support_graph", key="read")[1]
        self.assertEqual({n["node"] for n in wf["nodes"]}, {"router", "retrieve", "agent", "tools", "respond"})
        self.assertIn(("agent", "tools"), {(e["from"], e["to"]) for e in wf["edges"]})

    def test_3_otlp_json_openinference_gzip(self):
        tid = "a" * 32
        md = lambda n: json.dumps({"langgraph_node": n, "thread_id": "th-1"})  # noqa: E731

        def span(sid, parent, name, kind, s, e, **attrs):
            a = [{"key": "openinference.span.kind", "value": {"stringValue": kind}}]
            a += [{"key": KEYMAP.get(k, k), "value": ({"intValue": str(v)} if isinstance(v, int) else {"stringValue": v})} for k, v in attrs.items()]
            return {"traceId": tid, "spanId": sid, "parentSpanId": parent, "name": name, "startTimeUnixNano": str(int(s * 1e9)),
                    "endTimeUnixNano": str(int(e * 1e9)), "attributes": a, "status": {}}
        spans = [span("0000000000000001", "", "research_graph", "CHAIN", T, T + 9, input_value='{"messages": [{"role": "user", "content": "Summarize Q3 churn drivers"}]}'),
                 span("0000000000000002", "0000000000000001", "planner", "CHAIN", T + 0.1, T + 2, metadata=md("planner")),
                 span("0000000000000003", "0000000000000002", "ChatAnthropic", "LLM", T + 0.2, T + 1.9, llm_model_name="claude-opus-5",
                      llm_token_count_prompt=5000, llm_token_count_completion=700, metadata=md("planner")),
                 span("0000000000000004", "0000000000000001", "search", "CHAIN", T + 2, T + 6, metadata=md("search")),
                 span("0000000000000005", "0000000000000004", "web_search", "TOOL", T + 2.1, T + 5.9, tool_name="web_search", metadata=md("search")),
                 span("0000000000000006", "0000000000000001", "writer", "CHAIN", T + 6, T + 9, metadata=md("writer")),
                 span("0000000000000007", "0000000000000006", "ChatAnthropic", "LLM", T + 6.1, T + 8.9, llm_model_name="claude-opus-5",
                      llm_token_count_prompt=9000, llm_token_count_completion=4096, llm_finish_reason="max_tokens", metadata=md("writer"))]
        doc = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "research-agent"}},
                                                              {"key": "deployment.environment", "value": {"stringValue": "staging"}}]},
                                  "scopeSpans": [{"scope": {"name": "openinference.instrumentation.langchain"}, "spans": spans}]}]}
        st, _ = self.req("/v1/traces", gzip.compress(json.dumps(doc).encode()), key="ingest",
                         headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})
        self.assertEqual(st, 200)
        self.eng.refresh()
        t = self.tasks(project="research-agent")[0]
        self.assertEqual((t["workflow"], t["environment"], t["framework"]), ("research_graph", "staging", "langgraph"))
        self.assertEqual(t["prompt"], "Summarize Q3 churn drivers")
        full = self.task(t["id"])
        self.assertEqual(full["path"], ["planner", "search", "writer"])
        self.assertEqual(full["truncations"], 1)
        self.assertEqual(full["critical_node"], "search")
        self.assertAlmostEqual(full["cost"], (14000 * 5 + 4796 * 25) / 1e6, places=6)

    def test_4_otlp_protobuf_genai_multiagent(self):
        tid = "b" * 32
        g = lambda sid: sid.rjust(16, "0")  # noqa: E731
        spans = [
            pb_span(tid, g("1"), None, "invoke_agent triage", T, T + 20, {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "triage",
                                                                          "gen_ai.prompt": "My invoice is wrong and I want a refund"}),
            pb_span(tid, g("2"), g("1"), "chat", T + 0.1, T + 2, {"gen_ai.operation.name": "chat", "gen_ai.request.model": "claude-sonnet-5",
                                                                   "gen_ai.usage.input_tokens": 1500, "gen_ai.usage.output_tokens": 200,
                                                                   "gen_ai.agent.name": "triage", "gen_ai.response.finish_reasons": ["tool_use"]}),
            pb_span(tid, g("3"), g("1"), "invoke_agent billing", T + 2, T + 10, {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "billing"}),
            pb_span(tid, g("4"), g("3"), "chat", T + 2.1, T + 4, {"gen_ai.operation.name": "chat", "gen_ai.request.model": "claude-sonnet-5",
                                                                   "gen_ai.usage.input_tokens": 1800, "gen_ai.usage.output_tokens": 150}),
            pb_span(tid, g("5"), g("3"), "execute_tool refund", T + 4, T + 5, {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "refund"},
                    error="429 Too Many Requests"),
            pb_span(tid, g("6"), g("1"), "invoke_agent triage", T + 10, T + 14, {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "triage"}),
            pb_span(tid, g("7"), g("6"), "chat", T + 10.1, T + 12, {"gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-9-unknown",
                                                                     "gen_ai.usage.input_tokens": 900, "gen_ai.usage.output_tokens": 90}),
            pb_span(tid, g("8"), g("1"), "invoke_agent billing", T + 14, T + 20, {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "billing"}),
            pb_span(tid, g("9"), g("8"), "chat", T + 14.1, T + 19, {"gen_ai.operation.name": "chat", "gen_ai.request.model": "claude-sonnet-5",
                                                                     "gen_ai.usage.input_tokens": 2000, "gen_ai.usage.output_tokens": 300}),
        ]
        body = pb_request({"service.name": "helpdesk-agents", "deployment.environment.name": "production"}, spans)
        st, _ = self.req("/v1/traces", body, key="ingest", headers={"Content-Type": "application/x-protobuf"})
        self.assertEqual(st, 200)
        self.eng.refresh()
        t = self.tasks(project="helpdesk-agents")[0]
        full = self.task(t["id"])
        self.assertEqual(full["framework"], "genai-semconv")
        self.assertEqual(full["path"], ["triage", "billing", "triage", "billing"])
        self.assertEqual(full["handoffs"], 3)
        self.assertEqual(full["pingpong"], 2)
        self.assertEqual(full["tool_errors"], 1)
        self.assertEqual(full["unpriced"], 1)
        rules = {e["rule_id"] for e in self.req("/api/events?project=helpdesk-agents", key="read")[1]["events"]}
        self.assertIn("pingpong", rules)

    def test_5_log_pipeline_records_and_redaction(self):
        run = {"id": "vector-1", "agent": "ops-bot", "project": "ops", "steps": [
            {"kind": "prompt", "ts": T, "text": "Rotate keys for alice@example.com"},
            {"kind": "llm", "ts": T, "end_ts": T + 1, "model": "claude-haiku-4-5", "input_tokens": 300, "output_tokens": 50, "stop_reason": "end_turn"}]}
        ndjson = "\n".join(json.dumps(x) for x in [{"log": json.dumps(run)}, {"message": {"not": "a trace"}}]).encode()
        st, res = self.req("/api/ingest/records", ndjson, key="ingest", headers={"Content-Type": "application/x-ndjson"})
        self.assertEqual((st, res["received"]), (200, 2))
        self.eng.refresh()
        t = self.tasks(project="ops")[0]
        self.assertIn("[REDACTED]", t["prompt"])
        self.assertNotIn("alice@example.com", t["prompt"])

    def test_6_inbox_and_pull_connectors(self):
        with open(os.path.join(self.inbox, "fluentbit.jsonl"), "w") as f:
            f.write(json.dumps({"date": 1, "log": json.dumps(ls_run("ib1", "tr-ib", None, "chain", "nightly_report", T, T + 3))}) + "\n")
            f.write(json.dumps({"date": 1, "log": json.dumps(ls_run("ib2", "tr-ib", "ib1", "llm", "ChatAnthropic", T + 1, T + 2, tokens=(500, 80)))}) + "\n")
        for name, t, sc, st in self.eng.pullers:
            self.eng._run_puller(name, t, sc, st)
            self.assertEqual(st.d["status"], "ok", f"{name}: {st.d['last_error']}")
        self.eng.refresh()
        wfs = {w["workflow"] for w in self.req("/api/workflows", key="read")[1]["workflows"]}
        self.assertTrue({"nightly_report", "billing_agent", "rag_pipeline"} <= wfs, wfs)
        lf = self.tasks(workflow="rag_pipeline")
        self.assertEqual(len(lf), 2)
        self.assertEqual(lf[0]["outcome"], "rework")  # helpfulness 0.2 -> negative feedback
        full = self.task(lf[0]["id"])
        self.assertEqual((full["empty_retrievals"], full["ttft_ms"]), (1, 400))
        # pulling again is idempotent (span ids are upsert keys)
        for name, t, sc, st in self.eng.pullers:
            self.eng._run_puller(name, t, sc, st)
        self.eng.refresh()
        self.assertEqual(len(self.tasks(workflow="rag_pipeline")), 2)

    def test_7_metrics_slos_sources(self):
        st, body = self.req("/metrics", key="read")
        self.assertEqual(st, 200)
        text = body.decode() if isinstance(body, bytes) else str(body)
        self.assertIn("agentdynamics_tasks_total{", text)
        slos = self.req("/api/slos", key="read")[1]["slos"]
        self.assertTrue(all("status" in s for s in slos))
        src = {s["name"]: s for s in self.req("/api/sources", key="read")[1]["sources"]}
        self.assertEqual(src["otlp"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
