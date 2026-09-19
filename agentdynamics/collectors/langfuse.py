"""Langfuse pull connector (public API) -> canonical spans.

Traces: GET /api/public/traces?fromTimestamp=..&page=..&limit=..
Detail: GET /api/public/traces/{id}  (includes observations: SPAN | GENERATION | EVENT | AGENT | TOOL | RETRIEVER ...)
Auth: HTTP Basic public_key:secret_key
"""
import base64
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

from .langsmith import parse_time

OBS_KIND = {"GENERATION": "llm", "SPAN": "chain", "EVENT": "span", "AGENT": "agent", "TOOL": "tool", "CHAIN": "chain",
            "RETRIEVER": "retriever", "EMBEDDING": "embedding", "GUARDRAIL": "guardrail", "EVALUATOR": "evaluator"}


def trace_to_spans(tr):
    """Langfuse trace (with observations) -> canonical spans. The trace itself becomes the root span."""
    tid = tr["id"]
    md = tr.get("metadata") or {}
    root = {
        "trace_id": tid, "span_id": f"trace-{tid}", "parent_id": None, "name": tr.get("name") or "trace", "kind": "chain",
        "start": parse_time(tr.get("timestamp")), "end": None, "status": "ok", "error": None,
        "input": tr.get("input"), "output": tr.get("output"), "project": md.get("project") or tr.get("release"),
        "environment": tr.get("environment") or md.get("environment"), "session_id": tr.get("sessionId"), "user_id": tr.get("userId"),
        "tags": tr.get("tags") or [], "framework": "langfuse", "source": "langfuse",
        "feedback": [{"key": s.get("name"), "score": s.get("value")} for s in tr.get("scores") or [] if isinstance(s.get("value"), (int, float))],
    }
    spans, ends = [root], []
    for o in tr.get("observations") or []:
        u = o.get("usageDetails") or o.get("usage") or {}
        it = u.get("input") or u.get("promptTokens") or u.get("input_tokens") or 0
        ot = u.get("output") or u.get("completionTokens") or u.get("output_tokens") or 0
        cr = u.get("cache_read_input_tokens") or u.get("input_cached_tokens") or u.get("cache_read") or 0
        cw = u.get("cache_creation_input_tokens") or 0
        err = o.get("statusMessage") if o.get("level") == "ERROR" else None
        end = parse_time(o.get("endTime"))
        ends.append(end)
        omd = o.get("metadata") or {}
        out = o.get("output")
        docs = None
        if o.get("type") == "RETRIEVER" or "retriev" in (o.get("name") or "").lower():
            d = out.get("documents") if isinstance(out, dict) else out
            docs = len(d) if isinstance(d, list) else None
        spans.append({
            "trace_id": tid, "span_id": o["id"], "parent_id": o.get("parentObservationId") or root["span_id"],
            "name": o.get("name") or o.get("type"), "kind": OBS_KIND.get(o.get("type"), "span"),
            "start": parse_time(o.get("startTime")), "end": end, "status": "error" if err else "ok", "error": err,
            "model": o.get("model"), "input_tokens": it, "output_tokens": ot, "cache_read": cr, "cache_write": cw,
            "cost": o.get("calculatedTotalCost") if o.get("calculatedTotalCost") is not None else (o.get("costDetails") or {}).get("total"),
            "ttft_ms": (parse_time(o.get("completionStartTime")) - parse_time(o.get("startTime"))) * 1000
            if o.get("completionStartTime") and o.get("startTime") else None,
            "stop_reason": omd.get("finish_reason"), "input": o.get("input"), "output": out,
            "node": omd.get("langgraph_node"), "agent": omd.get("agent_name"), "framework": "langfuse", "source": "langfuse", "docs": docs,
        })
    valid = [e for e in ends if e]
    root["end"] = max(valid) if valid else root["start"]
    if any(s.get("status") == "error" for s in spans[1:]) and not tr.get("output"):
        root["status"] = "error"
        root["error"] = next(s["error"] for s in spans[1:] if s.get("status") == "error")
    return spans


class LangfusePuller:
    def __init__(self, cfg, state):
        self.host = (cfg.get("host") or "https://cloud.langfuse.com").rstrip("/")
        pk = os.environ.get(cfg.get("public_key_env", "LANGFUSE_PUBLIC_KEY"), "")
        sk = os.environ.get(cfg.get("secret_key_env", "LANGFUSE_SECRET_KEY"), "")
        self.auth = "Basic " + base64.b64encode(f"{pk}:{sk}".encode()).decode()
        self.state = state
        self.lookback = float(cfg.get("lookback_hours", 24)) * 3600

    def _get(self, path, params=None):
        url = f"{self.host}{path}" + (("?" + urlencode(params)) if params else "")
        req = urllib.request.Request(url, headers={"Authorization": self.auth, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def pull(self, max_traces=2000):
        since = self.state.get("since") or (time.time() - self.lookback)
        iso = datetime.fromtimestamp(since, timezone.utc).isoformat().replace("+00:00", "Z")
        traces, page, newest = [], 1, since
        while len(traces) < max_traces:
            res = self._get("/api/public/traces", {"fromTimestamp": iso, "page": page, "limit": 50})
            data = res.get("data") or []
            for t in data:
                traces.append(self._get(f"/api/public/traces/{t['id']}"))
                newest = max(newest, parse_time(t.get("timestamp")) or 0)
            meta = res.get("meta") or {}
            if not data or page >= (meta.get("totalPages") or 1):
                break
            page += 1
        self.state["since"] = max(since, newest - 600)
        return traces
