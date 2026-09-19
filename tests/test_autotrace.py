"""The one-line integration: agentdynamics.init() against the real Anthropic and OpenAI SDKs (pointed at mock APIs),
`agentdynamics run` (zero-code), `agentdynamics keys` and `agentdynamics doctor`."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agentdynamics import config as cfgmod  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

try:
    import anthropic
    import openai
except ImportError:  # pragma: no cover
    anthropic = openai = None

MSG = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
       "content": [{"type": "text", "text": "Refund issued."}], "stop_reason": "end_turn", "stop_sequence": None,
       "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 0}}
SSE = [
    ("message_start", {"type": "message_start", "message": {**MSG, "content": [], "stop_reason": None,
                                                            "usage": {"input_tokens": 900, "output_tokens": 1, "cache_read_input_tokens": 0}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "max_tokens", "stop_sequence": None}, "usage": {"output_tokens": 256}}),
    ("message_stop", {"type": "message_stop"}),
]
CHAT = {"id": "c1", "object": "chat.completion", "created": 0, "model": "gpt-test", "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 500, "completion_tokens": 40, "total_tokens": 540, "prompt_tokens_details": {"cached_tokens": 100}}}


class MockLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.endswith("/messages") and body.get("stream"):
            data = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in SSE).encode()
            ctype = "text/event-stream"
        elif self.path.endswith("/messages"):
            if body.get("model") == "overloaded":
                data = json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}).encode()
                self.send_response(529)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            data, ctype = json.dumps(MSG).encode(), "application/json"
        else:
            data, ctype = json.dumps(CHAT).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@unittest.skipIf(anthropic is None, "anthropic/openai SDKs not installed")
class AutotraceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.data = os.path.join(cls.tmp, "data")
        os.makedirs(cls.data)
        # keys created the same way a user would
        out = subprocess.run([sys.executable, "-m", "agentdynamics", "--data", cls.data, "keys", "create", "--role", "admin", "--name", "ops"],
                             cwd=ROOT, capture_output=True, text=True)
        cls.admin = next(line.strip() for line in out.stdout.splitlines() if line.strip().startswith("ad_a_"))
        out = subprocess.run([sys.executable, "-m", "agentdynamics", "--data", cls.data, "keys", "create", "--role", "ingest"],
                             cwd=ROOT, capture_output=True, text=True)
        cls.ingest = next(line.strip() for line in out.stdout.splitlines() if line.strip().startswith("ad_i_"))
        cls.eng = Engine(cls.data, None, cfgmod.load(cls.data))
        cls.eng.refresh(force=True)
        Handler.api = Api(cls.eng)
        cls.srv, cls.url = serve(Handler)
        cls.llm, cls.llm_url = serve(MockLLM)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.llm.shutdown()
        cls.eng.con.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tasks(self, project):
        return [t for t in Api(self.eng).tasks({"project": project, "sub": "1"})]

    def test_1_keys_enable_auth(self):
        self.assertTrue(self.eng.cfg["auth"]["enabled"])
        self.assertEqual({k["role"] for k in self.eng.cfg["auth"]["keys"]}, {"admin", "ingest"})

    def test_2_init_trace_tool_span(self):
        import agentdynamics as ad
        done = ad.init(url=self.url, api_key=self.ingest, project="unit-app", environment="test", otel=False, quiet=True)
        self.assertTrue(done["anthropic"] and done["openai"])
        client = anthropic.Anthropic(api_key="x", base_url=self.llm_url)
        oai = openai.OpenAI(api_key="x", base_url=self.llm_url + "/v1")

        @ad.tool
        def lookup_order(order_id):
            return {"id": order_id, "status": "shipped"}

        @ad.tool(name="refund")
        def refund(order_id):
            raise TimeoutError("payments API timed out")

        @ad.trace
        def support(question):
            with ad.span("plan"):
                client.messages.create(model="claude-sonnet-5", max_tokens=100, messages=[{"role": "user", "content": question}])
            with ad.span("act"):
                lookup_order("42")
                try:
                    refund("42")
                except TimeoutError:
                    pass
            with ad.span("respond"):
                with client.messages.create(model="claude-sonnet-5", max_tokens=256, stream=True,
                                            messages=[{"role": "user", "content": question}]) as stream:
                    for _ in stream:
                        pass
            return "done"

        self.assertEqual(support("Where is my refund for order 42?"), "done")
        oai.chat.completions.create(model="gpt-test", messages=[{"role": "user", "content": "hi"}])  # outside any trace
        try:
            client.messages.create(model="overloaded", max_tokens=10, messages=[{"role": "user", "content": "x"}])
        except anthropic.APIStatusError:
            pass
        ad.flush()
        self.eng.refresh()
        ts = {t["workflow"]: t for t in self.tasks("unit-app")}
        t = ts["support"]
        self.assertEqual(t["environment"], "test")
        self.assertEqual(t["prompt"], "Where is my refund for order 42?")
        self.assertEqual(t["path"], ["plan", "act", "respond"])
        self.assertEqual((t["llm_calls"], t["tool_calls"], t["tool_errors"]), (2, 2, 1))
        self.assertEqual(t["truncations"], 1)                      # streamed response hit max_tokens
        self.assertEqual(t["cache_read"], 3000)
        self.assertEqual(t["output_tokens"], 80 + 256)
        self.assertIn("openai.call", ts)                            # un-traced call still recorded
        self.assertEqual(ts["openai.call"]["input_tokens"], 400)    # cached part split out
        self.assertEqual(ts["anthropic.call"]["outcome"], "failed")
        self.assertEqual(ts["anthropic.call"]["rate_limited"], 1)

    def test_3_zero_code_run(self):
        app = os.path.join(self.tmp, "app.py")
        with open(app, "w") as f:
            f.write("import anthropic\n"
                    f"c = anthropic.Anthropic(api_key='x', base_url='{self.llm_url}')\n"
                    "c.messages.create(model='claude-sonnet-5', max_tokens=50, messages=[{'role':'user','content':'hello'}])\n"
                    "print('app finished')\n")
        env = dict(os.environ, AGENTDYNAMICS_URL=self.url, AGENTDYNAMICS_API_KEY=self.ingest, AGENTDYNAMICS_QUIET="1", PYTHONPATH=ROOT)
        p = subprocess.run([sys.executable, "-m", "agentdynamics", "run", "--project", "zero-code", sys.executable, app],
                           cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("app finished", p.stdout)
        self.eng.refresh()
        ts = self.tasks("zero-code")
        self.assertEqual(len(ts), 1)
        self.assertEqual(ts[0]["models"], "claude-sonnet-5")

    def test_4_doctor(self):
        env = dict(os.environ, AGENTDYNAMICS_API_KEY=self.admin)
        p = subprocess.run([sys.executable, "-m", "agentdynamics", "doctor", "--url", self.url], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("visible in the console", p.stdout)
        bad = subprocess.run([sys.executable, "-m", "agentdynamics", "doctor", "--url", self.url, "--key", "wrong"], cwd=ROOT,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("keys create", bad.stdout)


if __name__ == "__main__":
    unittest.main()
