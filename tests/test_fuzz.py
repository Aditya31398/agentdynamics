"""Telemetry is input from outside: an ingest key, an exporter, a log pipeline. Whatever is accepted lands in
spans_raw and runs/*.json and is re-read on every rebuild, so a payload that breaks the analysis breaks it for
everyone, every refresh, until someone deletes it by hand. This feeds the receivers malformed values -- wrong
types, NaN and infinity, huge numbers, truncated and bit-flipped protobuf -- and requires that each is either
refused with a 400 or accepted, and that the refresh and the console's endpoints still work after it.

Deterministic (a fixed seed); a failure names the payload that caused it.
"""
import json
import os
import random
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

from test_integrations import pb_request, pb_span  # noqa: E402

from agentdynamics import store  # noqa: E402
from agentdynamics.collectors import generic, otlp  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

NOW = time.time() - 600
SURROGATE = "\u0000\ud800"            # JSON can carry it; UTF-8 (protobuf) can't
GARBAGE = [None, "", "abc", "12", -1, 0, 1e308, -1e308, float("inf"), float("nan"), 2 ** 70, -(2 ** 70), True, [],
           {}, [1, "x"], {"a": {"b": None}}, "x" * 5000, "2026-13-45T99:99:99Z", SURROGATE]
RUN_FIELDS = ["id", "project", "workflow", "environment", "thread_id", "user_id", "status", "error", "metadata",
              "feedback", "agent", "parent_id", "started", "version", "policy_version", "policy", "steps", "tags"]
STEP_FIELDS = ["kind", "ts", "end_ts", "model", "input_tokens", "output_tokens", "cache_read", "cache_write", "cost",
               "name", "input", "output", "is_error", "error", "denied", "rule", "agent", "grant_depth", "node",
               "stop_reason", "tripwire", "text", "span_kind", "governed", "thinking_tokens", "service_tier",
               "output_chars", "phase", "duration_ms"]
ROUTES = ["/api/overview?days=", "/api/tasks?days=", "/api/types?days=", "/api/tools?days=", "/api/models?days=",
          "/api/events?days=", "/api/governance?days=", "/api/analytics?days=&group=project", "/api/flowmap?days=",
          "/api/sessions?days=", "/api/process?days=", "/api/incidents", "/api/trust", "/metrics"]


def valid_run(rid):
    return {"id": rid, "project": "fuzz", "workflow": "w", "thread_id": "t", "user_id": "u", "metadata": {"k": "v"},
            "feedback": [{"key": "user", "score": 0.5}], "steps": [
                {"kind": "prompt", "ts": NOW, "text": "hello"},
                {"kind": "llm", "ts": NOW, "end_ts": NOW + 1, "model": "claude-sonnet-5", "input_tokens": 100,
                 "output_tokens": 10, "cache_read": 5, "stop_reason": "end_turn"},
                {"kind": "tool", "ts": NOW + 1, "end_ts": NOW + 2, "name": "search", "input": {"q": "x"},
                 "governed": True, "agent": "a", "rule": "kernel.admitted"},
                {"kind": "tool", "ts": NOW + 2, "end_ts": NOW + 2.1, "name": "pay", "denied": True,
                 "rule": "capability.not_granted", "agent": "a", "error": "refused"}]}


def mutate(rng, run):
    for _ in range(rng.randint(1, 3)):
        if rng.random() < 0.35:
            run[rng.choice(RUN_FIELDS)] = rng.choice(GARBAGE)
        elif isinstance(run.get("steps"), list) and run["steps"]:
            step = rng.choice(run["steps"])
            if isinstance(step, dict):
                step[rng.choice(STEP_FIELDS)] = rng.choice(GARBAGE)
    return run


class FuzzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.eng = Engine(os.path.join(cls.tmp, "data"), None)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"api": Api(cls.eng)}))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.eng.con.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def post(self, path, body, ctype="application/json"):
        req = urllib.request.Request(self.url + path, data=body, method="POST", headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status
        except urllib.error.HTTPError as ex:
            return ex.code

    def assert_still_works(self, what):
        try:
            self.eng.refresh(force=True)
        except Exception as ex:                       # noqa: BLE001 -- the point of the test
            self.fail(f"refresh broke after {what}: {ex!r}")
        for r in ROUTES:
            try:
                with urllib.request.urlopen(self.url + r, timeout=60) as resp:
                    self.assertEqual(resp.status, 200)
            except urllib.error.HTTPError as ex:
                self.fail(f"{r} answered {ex.code} after {what}: {ex.read()[:300]!r}")

    def test_malformed_runs_are_refused_or_survived(self):
        rng = random.Random(20261009)
        for i in range(150):
            run = mutate(rng, valid_run(f"r{i}"))
            body = json.dumps(run, default=str).encode()
            st = self.post("/api/ingest", body)
            self.assertIn(st, (200, 400), f"payload {i} answered {st}: {body[:400]!r}")
            if st == 200 and i % 5 == 0:
                self.assert_still_works(f"payload {i}: {body[:400]!r}")
        self.assert_still_works("all of them")

    def test_malformed_otlp_is_refused_or_survived(self):
        rng = random.Random(7)
        tid = "c" * 32
        attrs = {"gen_ai.operation.name": "chat", "gen_ai.request.model": "claude-sonnet-5",
                 "gen_ai.usage.input_tokens": 1500, "gen_ai.usage.output_tokens": 200, "gen_ai.agent.name": "x"}
        values = [v for v in GARBAGE if v is not None and not isinstance(v, dict) and v != SURROGATE]
        for i in range(120):
            a = dict(attrs)
            for _ in range(rng.randint(1, 3)):
                a[rng.choice(list(attrs) + ["gen_ai.prompt", "gen_ai.tool.name", "session.id", "user.id"])] = \
                    rng.choice(values)
            # protobuf ints are 64-bit, negatives as two's complement (the test-side encoder takes them unsigned)
            a = {k: (v & (2 ** 64 - 1) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in a.items()
                 if not (isinstance(v, int) and not isinstance(v, bool) and abs(v) >= 2 ** 63)}
            body = pb_request({"service.name": "fuzz-otlp"}, [pb_span(tid, f"{i:016x}", None, "chat", NOW, NOW + 1, a)])
            data = bytearray(body)
            if i % 3 == 1:                                  # flip some bits
                for _ in range(rng.randint(1, 4)):
                    j = rng.randrange(len(data))
                    data[j] ^= 1 << rng.randrange(8)
            elif i % 3 == 2:                                # cut it short
                data = data[:rng.randrange(1, len(data))]
            st = self.post("/v1/traces", bytes(data), "application/x-protobuf")
            self.assertIn(st, (200, 400), f"protobuf payload {i} answered {st}")
            js = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "fuzz"}}]},
                                     "scopeSpans": [{"spans": [{
                                         "traceId": "d" * 32, "spanId": f"{i:016x}", "name": "chat",
                                         "startTimeUnixNano": rng.choice([str(int(NOW * 1e9)), "x", -5, 2 ** 70, None]),
                                         "endTimeUnixNano": str(int((NOW + 1) * 1e9)),
                                         "attributes": [{"key": k, "value": rng.choice(
                                             [{"intValue": v}, {"stringValue": v}, {"doubleValue": v}, v])}
                                             for k, v in a.items()]}]}]}]}
            st = self.post("/v1/traces", json.dumps(js, default=str).encode())
            self.assertIn(st, (200, 400), f"OTLP JSON payload {i} answered {st}")
        self.assert_still_works("the OTLP payloads")

    def test_a_stored_run_that_cannot_be_read_is_skipped_not_fatal(self):
        # what an older version accepted, or a hand-edited store, can't stop the refresh for everyone
        bad = {"id": "stored-bad", "project": "fuzz", "steps": [{"kind": "llm", "ts": NOW, "input_tokens": {"x": 1}}]}
        good = dict(valid_run("stored-good"))
        real = generic.normalize
        try:
            generic.normalize = lambda d: (_ for _ in ()).throw(ValueError("unreadable")) if d.get("id") == "stored-bad" else real(d)
            if self.eng._runs_in_store:                      # Postgres: runs live in spans_raw
                store.upsert_spans(self.eng.con, "sdk", [("stored-bad", "stored-bad", "run", bad),
                                                         ("stored-good", "stored-good", "run", good)])
            else:                                            # SQLite: one file per run
                for d in (bad, good):
                    with open(self.eng._run_path(d["id"]), "w", encoding="utf-8") as f:
                        json.dump(d, f)
            self.eng.refresh(force=True)
        finally:
            generic.normalize = real
        self.assertTrue(self.eng.con.execute("SELECT 1 FROM runs WHERE id = 'stored-good'").fetchone())
        self.assertIn("stored-bad", self.eng.sources["sdk"].d.get("last_error") or "")

    def test_one_bad_value_does_not_lose_the_run(self):
        # telemetry is taken leniently: the value that can't be read is dropped, the run and the rest are kept
        run = valid_run("lenient")
        run["steps"][1].update(input_tokens="lots", output_tokens={"n": 3}, cost=float("inf"), stop_reason=7)
        run["steps"][2]["name"] = ["search"]
        self.assertEqual(self.post("/api/ingest", json.dumps(run).encode()), 200)
        self.eng.refresh(force=True)
        llm = dict(self.eng.con.execute("SELECT input_tokens, output_tokens, stop_reason FROM steps "
                                        "WHERE run_id = 'lenient' AND kind = 'llm'").fetchone())
        self.assertEqual(llm, {"input_tokens": 0, "output_tokens": 0, "stop_reason": "7"})
        tool = self.eng.con.execute("SELECT name FROM steps WHERE run_id = 'lenient' AND kind = 'tool' "
                                    "ORDER BY seq").fetchone()[0]
        self.assertEqual(tool, "unknown")

    def test_varints_are_at_most_ten_bytes(self):
        # protobuf varints encode 64 bits; a longer one is malformed, not a 1000-digit token count
        with self.assertRaises(ValueError):
            otlp._varint(b"\xff" * 64 + b"\x01", 0)
        self.assertEqual(otlp._varint(b"\xff" * 9 + b"\x01", 0)[0], 2 ** 64 - 1)


if __name__ == "__main__":
    unittest.main()
