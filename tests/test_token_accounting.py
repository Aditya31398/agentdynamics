"""Input-token accounting with prompt caching (#2).

Canonical steps keep uncached input, cache reads and cache writes disjoint, because each is priced
differently. Formats disagree on whether their input count already contains the cache tokens:

    GenAI semconv   gen_ai.usage.input_tokens "SHOULD include all types of input tokens, including
                    cached tokens"
    OpenInference   cache_read / cache_write are "tokens in the prompt"
    LangChain       UsageMetadata.input_tokens is the "Sum of all input token types"

All three are inclusive of reads *and* writes. The old heuristic subtracted reads only, so every
cached write was charged twice. Formats with no rule we can cite keep the old estimate and are
reported as unverified, the way an unknown model is reported as unpriced.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentdynamics import pricing  # noqa: E402
from agentdynamics.collectors.spans import uncached_input  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api  # noqa: E402

T = time.time() - 600
MODEL = "claude-sonnet-5"
# one call: 1000 input tokens reported, of which 600 read from cache and 200 written to it
REPORTED, READ, WRITE, OUT = 1000, 600, 200, 100
UNCACHED = REPORTED - READ - WRITE                    # 200


def expected_cost(uncached):
    return pricing.cost(MODEL, uncached, OUT, READ, WRITE, 0)


class RuleTest(unittest.TestCase):
    def test_the_rule(self):
        cases = [
            # (input, read, write, convention) -> (uncached, verified)
            ((1000, 600, 200, "inclusive"), (200, True)),    # documented inclusive: subtract both
            ((1000, 600, 200, "exclusive"), (1000, True)),   # documented exclusive: leave it
            ((1000, 600, 200, None), (400, False)),          # no rule: old estimate, flagged
            ((100, 600, 0, "inclusive"), (100, False)),      # claims inclusive, can't be: flagged
            ((100, 600, 0, None), (100, False)),             # impossible either way: flagged
            ((1000, 0, 0, None), (1000, True)),              # nothing cached: nothing to disagree on
        ]
        for args, want in cases:
            with self.subTest(args=args):
                self.assertEqual(uncached_input(*args), want)


def otlp_doc(spans):
    return json.dumps({"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "cache-app"}}]},
        "scopeSpans": [{"spans": spans}]}]}).encode()


def otlp_span(tid, attrs):
    a = [{"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": v}}
         for k, v in attrs.items()]
    return {"traceId": tid, "spanId": tid[:16], "name": "chat", "status": {}, "attributes": a,
            "startTimeUnixNano": str(int(T * 1e9)), "endTimeUnixNano": str(int((T + 1) * 1e9))}


class CollectorPathTest(unittest.TestCase):
    """The same call, reported in each format, must price the same."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.eng = Engine(os.path.join(cls.tmp, "data"), None)
        e = cls.eng
        e.ingest_otlp(otlp_doc([
            # OpenTelemetry GenAI semantic conventions
            otlp_span("1" * 32, {"gen_ai.operation.name": "chat", "gen_ai.request.model": MODEL,
                                 "gen_ai.usage.input_tokens": REPORTED, "gen_ai.usage.output_tokens": OUT,
                                 "gen_ai.usage.cache_read.input_tokens": READ,
                                 "gen_ai.usage.cache_creation.input_tokens": WRITE}),
            # OpenInference
            otlp_span("2" * 32, {"openinference.span.kind": "LLM", "llm.model_name": MODEL,
                                 "llm.token_count.prompt": REPORTED, "llm.token_count.completion": OUT,
                                 "llm.token_count.prompt_details.cache_read": READ,
                                 "llm.token_count.prompt_details.cache_write": WRITE}),
            # older semconv / OpenLLMetry naming: no documented rule for cache inclusion
            otlp_span("3" * 32, {"gen_ai.operation.name": "chat", "gen_ai.request.model": MODEL,
                                 "gen_ai.usage.prompt_tokens": REPORTED, "gen_ai.usage.completion_tokens": OUT,
                                 "gen_ai.usage.cache_read_input_tokens": READ,
                                 "gen_ai.usage.cache_creation_input_tokens": WRITE}),
        ]), "application/json")
        # LangSmith: LangChain's usage_metadata, as the SDK uploads it
        iso = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000000"  # noqa: E731
        run_id = "44444444-4444-4444-4444-444444444444"
        e.ingest_langsmith([{
            "id": run_id, "trace_id": run_id, "name": "ChatAnthropic", "run_type": "llm",
            "start_time": iso(T), "end_time": iso(T + 1), "extra": {"metadata": {"ls_model_name": MODEL}},
            "outputs": {"usage_metadata": {"input_tokens": REPORTED, "output_tokens": OUT,
                                           "total_tokens": REPORTED + OUT,
                                           "input_token_details": {"cache_read": READ, "cache_creation": WRITE}},
                        "llm_output": {"model_name": MODEL}},
        }], [])
        e.refresh(force=True)

    @classmethod
    def tearDownClass(cls):
        cls.eng.con.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def llm_step(self, run_prefix):
        r = self.eng.con.execute(
            "SELECT s.input_tokens, s.cache_read, s.cache_write, s.cost, t.tokens_unverified FROM steps s "
            "JOIN tasks t ON t.run_id = s.run_id WHERE s.kind='llm' AND s.run_id LIKE ?", (run_prefix + "%",)).fetchone()
        self.assertIsNotNone(r, f"no llm step for {run_prefix}")
        return dict(r)

    def assert_exact(self, run_prefix):
        s = self.llm_step(run_prefix)
        self.assertEqual((s["input_tokens"], s["cache_read"], s["cache_write"]), (UNCACHED, READ, WRITE))
        self.assertAlmostEqual(s["cost"], expected_cost(UNCACHED), places=9)
        self.assertEqual(s["tokens_unverified"], 0)

    def test_genai_semconv_counts_each_token_once(self):
        self.assert_exact("otlp:" + "1" * 32)

    def test_openinference_counts_each_token_once(self):
        self.assert_exact("otlp:" + "2" * 32)

    def test_langsmith_usage_metadata_counts_each_token_once(self):
        self.assert_exact("langsmith:44444444")

    def test_undocumented_format_is_estimated_and_reported(self):
        s = self.llm_step("otlp:" + "3" * 32)
        self.assertEqual(s["input_tokens"], REPORTED - READ)    # the old estimate, unchanged
        self.assertEqual(s["tokens_unverified"], 1)              # but no longer silent

    def test_the_overview_says_how_many_calls_are_estimated(self):
        self.assertEqual(Api(self.eng).overview({"days": ""})["kpis"]["tokens_unverified"], 1)


if __name__ == "__main__":
    unittest.main()
