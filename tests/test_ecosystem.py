"""Compatibility with the *real* ecosystem, at whatever versions are installed.

CI runs this file twice: once with the pinned versions from `[test]`, and once (the `ecosystem` job, also
nightly) with the latest releases, so upstream changes show up as a failing check rather than a user bug.
Every test skips cleanly when its library isn't installed.

  * LangGraph StateGraph traced by the LangSmith SDK -> /langsmith (batch, or zstd multipart when available)
  * OpenTelemetry SDK + OTLP/HTTP protobuf exporter with GenAI semantic-convention spans -> /v1/traces
  * OpenInference LangChain instrumentor on a LangGraph graph -> OTLP -> graph nodes
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402


def has(*mods):
    for m in mods:
        try:
            if importlib.util.find_spec(m) is None:
                return False
        except ModuleNotFoundError:  # a dotted name whose parent package is missing
            return False
    return True


HAS_LANGGRAPH = has("langgraph", "langchain_core", "langsmith")
HAS_OTEL = has("opentelemetry.sdk", "opentelemetry.exporter.otlp.proto.http")
HAS_OPENINFERENCE = HAS_OTEL and HAS_LANGGRAPH and has("openinference.instrumentation.langchain")


def build_graph(run_name="support_graph"):
    """A real LangGraph graph: agent <-> tools loop, LLM with usage metadata."""
    from typing import TypedDict

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langgraph.graph import END, StateGraph

    class State(TypedDict):
        question: str
        turns: int

    def agent(s):
        # LangChain's contract: input_tokens is the "Sum of all input token types", so the 700 read
        # from cache and the 300 written to it are inside the 1200; only 200 were uncached (#2).
        llm = GenericFakeChatModel(messages=iter([AIMessage(content="calling tool", usage_metadata={
            "input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280,
            "input_token_details": {"cache_read": 700, "cache_creation": 300}})]))
        llm.with_config(metadata={"ls_model_name": "claude-sonnet-5"}).invoke(s["question"])
        return {"turns": s["turns"] + 1}

    def tools(s):
        return {}

    g = StateGraph(State)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.set_entry_point("agent")
    g.add_conditional_edges("agent", lambda s: "tools" if s["turns"] < 2 else END)
    g.add_edge("tools", "agent")
    return g.compile().with_config(run_name=run_name)


class EcosystemBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.eng = Engine(os.path.join(cls.tmp, "data"), None)
        cls.eng.refresh(force=True)
        Handler.api = cls.api = Api(cls.eng)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.eng.con.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def wait_for(self, pred, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            self.eng.refresh()
            got = pred()
            if got:
                return got
            time.sleep(0.3)
        return pred()


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph / langsmith not installed")
class LangGraphViaLangSmithTest(EcosystemBase):
    def test_real_langgraph_graph(self):
        import langsmith
        from langchain_core.tracers import LangChainTracer
        from langchain_core.tracers.langchain import wait_for_all_tracers
        from langsmith import Client
        project = f"eco-{uuid.uuid4().hex[:6]}"
        client = Client(api_url=self.url + "/langsmith", api_key="test")
        tracer = LangChainTracer(project_name=project, client=client)
        graph = build_graph()
        for i in range(3):
            graph.invoke({"question": f"Where is order {i}?", "turns": 0}, config={"callbacks": [tracer]})
        wait_for_all_tracers()
        if hasattr(client, "flush"):
            client.flush()
        ts = self.wait_for(lambda: [t for t in self.api.tasks({"project": project, "sub": "1"})] if
                           len(self.api.tasks({"project": project, "sub": "1"})) >= 3 else None)
        self.assertEqual(len(ts), 3, f"langsmith {langsmith.__version__}")
        t = ts[0]
        self.assertEqual((t["workflow"], t["framework"]), ("support_graph", "langgraph"))
        self.assertEqual(t["path"], ["agent", "tools", "agent"])
        self.assertEqual(t["llm_calls"], 2)
        # two calls: each token counted exactly once, and nothing estimated
        self.assertEqual((t["input_tokens"], t["cache_read"], t["cache_write"]), (400, 1400, 600),
                         f"langchain-core usage_metadata via langsmith {langsmith.__version__}")
        self.assertEqual(t["tokens_unverified"], 0)
        self.assertEqual(sorted(x["prompt"] for x in ts), ["Where is order 0?", "Where is order 1?", "Where is order 2?"])


@unittest.skipUnless(HAS_OTEL, "opentelemetry-sdk / otlp http exporter not installed")
class OpenTelemetrySdkTest(EcosystemBase):
    def test_otlp_protobuf_exporter_genai_spans(self):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.trace import Status, StatusCode
        provider = TracerProvider(resource=Resource.create({"service.name": "otel-eco", "deployment.environment.name": "staging"}))
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=self.url + "/v1/traces")))
        tracer = provider.get_tracer("eco")
        with tracer.start_as_current_span("invoke_agent triage", attributes={
                "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "triage", "gen_ai.prompt": "Refund my order"}):
            with tracer.start_as_current_span("chat", attributes={
                    "gen_ai.operation.name": "chat", "gen_ai.request.model": "claude-sonnet-5", "gen_ai.agent.name": "triage",
                    "gen_ai.usage.input_tokens": 1500, "gen_ai.usage.output_tokens": 200,
                    "gen_ai.response.finish_reasons": ["tool_use"]}):
                pass
            with tracer.start_as_current_span("execute_tool refund", attributes={
                    "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "refund"}) as sp:
                sp.set_status(Status(StatusCode.ERROR, "payments API timed out"))
        provider.shutdown()
        ts = self.wait_for(lambda: self.api.tasks({"project": "otel-eco", "sub": "1"}))
        self.assertEqual(len(ts), 1)
        t = ts[0]
        self.assertEqual((t["workflow"], t["environment"], t["framework"]), ("triage", "staging", "genai-semconv"))
        self.assertEqual((t["llm_calls"], t["tool_calls"], t["tool_errors"]), (1, 1, 1))
        self.assertEqual(t["prompt"], "Refund my order")


@unittest.skipUnless(HAS_OPENINFERENCE, "openinference-instrumentation-langchain not installed")
class OpenInferenceLangGraphTest(EcosystemBase):
    def test_openinference_instrumented_langgraph(self):
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        saved = {k: os.environ.get(k) for k in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")}
        os.environ["LANGSMITH_TRACING"] = "false"   # only OpenInference should see this graph
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        provider = TracerProvider(resource=Resource.create({"service.name": "oi-eco"}))
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=self.url + "/v1/traces")))
        inst = LangChainInstrumentor()
        inst.instrument(tracer_provider=provider)
        try:
            build_graph("research_graph").invoke({"question": "Summarize churn drivers", "turns": 0})
        finally:
            inst.uninstrument()
            provider.shutdown()
        ts = self.wait_for(lambda: self.api.tasks({"project": "oi-eco", "sub": "1"}))
        self.assertEqual(len(ts), 1)
        t = ts[0]
        self.assertEqual(t["framework"], "langgraph")
        self.assertEqual(t["path"], ["agent", "tools", "agent"])
        self.assertEqual(t["llm_calls"], 2)
        # the same cache breakdown, through OpenInference's own attribute mapping (#2)
        self.assertEqual((t["input_tokens"], t["cache_read"], t["cache_write"]), (400, 1400, 600),
                         "openinference llm.token_count.prompt_details.*")
        self.assertEqual(t["tokens_unverified"], 0)


if __name__ == "__main__":
    unittest.main()
