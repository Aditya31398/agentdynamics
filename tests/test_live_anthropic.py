"""Live smoke test against the real Anthropic API (costs a fraction of a cent).

Runs only when both ANTHROPIC_API_KEY and AGENTDYNAMICS_LIVE=1 are set, e.g. in the nightly CI job with the
key stored as a repository secret. Catches SDK/API drift that mock servers cannot: response shapes, usage
fields, streaming events.
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

LIVE = bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("AGENTDYNAMICS_LIVE") == "1"
MODEL = os.environ.get("AGENTDYNAMICS_LIVE_MODEL", "claude-opus-5")


@unittest.skipUnless(LIVE, "set ANTHROPIC_API_KEY and AGENTDYNAMICS_LIVE=1 to run")
class LiveAnthropicTest(unittest.TestCase):
    def test_real_messages_create_and_stream(self):
        import anthropic

        import agentdynamics as ad
        tmp = tempfile.mkdtemp()
        eng = Engine(os.path.join(tmp, "data"), None)
        eng.refresh(force=True)
        Handler.api = api = Api(eng)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            ad.init(url=f"http://127.0.0.1:{srv.server_address[1]}", project="live", otel=False, langchain=False, quiet=True)
            client = anthropic.Anthropic()

            @ad.trace("live_smoke")
            def run():
                with ad.span("plain"):
                    client.messages.create(model=MODEL, max_tokens=16, messages=[{"role": "user", "content": "Say OK."}])
                with ad.span("stream"):
                    with client.messages.create(model=MODEL, max_tokens=16, stream=True,
                                                messages=[{"role": "user", "content": "Say OK."}]) as stream:
                        for _ in stream:
                            pass
            run()
            ad.flush()
            eng.refresh()
            t = api.tasks({"project": "live", "sub": "1"})[0]
            self.assertEqual(t["llm_calls"], 2)
            self.assertGreater(t["input_tokens"], 0)
            self.assertGreater(t["output_tokens"], 0)
            self.assertGreater(t["cost"], 0)               # model is in the pricing table
            self.assertEqual(t["path"], ["plain", "stream"])
        finally:
            srv.shutdown()
            eng.con.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
