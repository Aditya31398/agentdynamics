"""Tiny tracing SDK for custom agents. Stdlib only.

    tracer = Tracer(agent="support-bot", endpoint="http://127.0.0.1:8787")
    with tracer.task("Refund order #42") as task:
        task.record_anthropic(response)             # Anthropic SDK Message
        with task.tool("lookup_order", {"id": 42}) as call:
            call.output(result)
"""
import json
import time
import urllib.request
import uuid


class _ToolCall:
    def __init__(self, run, name, inp):
        self.step = {"kind": "tool", "name": name, "input": inp, "ts": time.time(), "is_error": False, "output_chars": 0}
        run._steps.append(self.step)

    def output(self, value):
        self.step["output_chars"] = len(value if isinstance(value, str) else json.dumps(value, default=str))

    def error(self, message):
        self.step["is_error"] = True
        self.step["error"] = str(message)[:300]

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        self.step["end_ts"] = time.time()
        if ev is not None:
            self.error(ev)
        return False


class Run:
    def __init__(self, tracer, prompt, run_id=None, parent_id=None):
        self.tracer = tracer
        self.id = run_id or f"{tracer.agent}-{uuid.uuid4().hex[:12]}"
        self.parent_id = parent_id
        self._steps = []
        self._last = time.time()
        if prompt is not None:
            self.prompt(prompt)

    def prompt(self, text):
        """Start a new task inside this run (a follow-up user request)."""
        self._steps.append({"kind": "prompt", "ts": time.time(), "text": text})
        self._last = time.time()

    def llm(self, model, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0, stop_reason=None,
            text="", start_ts=None, end_ts=None, cost=None):
        end_ts = end_ts or time.time()
        self._steps.append({"kind": "llm", "model": model, "ts": start_ts or self._last, "end_ts": end_ts,
                            "input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read": cache_read,
                            "cache_write": cache_write, "stop_reason": stop_reason, "text": (text or "")[:600], "cost": cost})
        self._last = end_ts

    def record_anthropic(self, message, start_ts=None):
        """Record an Anthropic SDK `Message` (or dict) using its usage block."""
        m = message if isinstance(message, dict) else message.model_dump()
        u = m.get("usage") or {}
        text = "".join(b.get("text", "") for b in m.get("content") or [] if b.get("type") == "text")
        self.llm(m.get("model"), u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                 u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0,
                 m.get("stop_reason"), text, start_ts=start_ts)

    def tool(self, name, inp=None):
        return _ToolCall(self, name, inp or {})

    def payload(self):
        return {"id": self.id, "agent": self.tracer.agent, "project": self.tracer.project, "parent_id": self.parent_id,
                "source": "sdk", "steps": self._steps}

    def flush(self):
        return self.tracer.send(self.payload())

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if ev is not None:
            self._steps.append({"kind": "notice", "name": "api_error", "ts": time.time(), "text": repr(ev)[:300]})
        try:
            self.flush()
        except OSError as ex:  # monitoring must never break the agent
            print(f"[agentdynamics] could not send run {self.id}: {ex}")
        return False


class Tracer:
    def __init__(self, agent, project=None, endpoint="http://127.0.0.1:8787"):
        self.agent = agent
        self.project = project or agent
        self.endpoint = endpoint.rstrip("/")

    def task(self, prompt, run_id=None, parent_id=None):
        return Run(self, prompt, run_id, parent_id)

    def send(self, payload):
        req = urllib.request.Request(f"{self.endpoint}/api/ingest", data=json.dumps(payload, default=str).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
