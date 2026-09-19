"""Collector for runs pushed by any agent framework (SDK / HTTP ingest).

Accepted format (JSON), one run per file in <data_dir>/runs/:
{
  "id": "run-123", "agent": "support-bot", "project": "helpdesk",
  "steps": [
    {"kind": "prompt", "ts": 1718000000.0, "text": "Refund order 42"},
    {"kind": "llm", "ts": ..., "end_ts": ..., "model": "claude-opus-5",
     "input_tokens": 1200, "output_tokens": 300, "cache_read": 0, "cache_write": 0, "stop_reason": "tool_use"},
    {"kind": "tool", "ts": ..., "end_ts": ..., "name": "lookup_order", "input": {...},
     "is_error": false, "output_chars": 512, "phase": "explore"}
  ]
}
Missing costs are computed from pricing; missing phases are inferred.
"""
import hashlib
import json
import os
import uuid

from .. import pricing
from ..phases import classify


def normalize(payload):
    rid = str(payload.get("id") or uuid.uuid4())
    run = {
        "id": rid, "source": payload.get("source") or "sdk", "file": None,
        "project": payload.get("project") or payload.get("agent") or "default",
        "cwd": payload.get("cwd"), "title": payload.get("title"), "agent_name": payload.get("agent"),
        "parent_id": payload.get("parent_id"), "is_subagent": bool(payload.get("parent_id")),
        "workflow": payload.get("workflow"), "version": payload.get("version"), "git_branch": None,
        "entrypoint": payload.get("entrypoint"), "environment": payload.get("environment") or "default",
        "framework": payload.get("framework") or "sdk", "thread_id": payload.get("thread_id"), "user_id": payload.get("user_id"),
        "feedback": payload.get("feedback") or [], "tags": payload.get("tags") or [],
        "root_status": payload.get("status"), "root_error": payload.get("error"), "complete": payload.get("complete", True),
        "policy_version": payload.get("policy_version"), "policy": payload.get("policy"),
        "steps": [],
    }
    last_llm = None
    for i, s in enumerate(payload.get("steps") or []):
        k = s.get("kind")
        st = dict(s)
        st["seq"] = i
        st.setdefault("ts", st.get("start_ts"))
        st.setdefault("start_ts", st.get("ts"))
        if st.get("ts") and st.get("end_ts") and "duration_ms" not in st:
            st["duration_ms"] = int((st["end_ts"] - st["ts"]) * 1000)
        if k == "llm":
            st.setdefault("name", st.get("model"))
            for f in ("input_tokens", "output_tokens", "cache_read", "cache_write", "thinking_tokens"):
                st[f] = int(st.get(f) or 0)
            st.setdefault("cache_write_5m", st["cache_write"])
            st.setdefault("cache_write_1h", 0)
            if st.get("cost") is None:
                st["cost"] = pricing.cost(st.get("model"), st["input_tokens"], st["output_tokens"], st["cache_read"],
                                          st["cache_write_5m"], st["cache_write_1h"])
            st["context_tokens"] = st["input_tokens"] + st["cache_read"] + st["cache_write"]
            st.setdefault("text", "")
            st["_id"] = st.get("id") or f"{rid}:{i}"
            st["tool_calls"] = 0
            last_llm = st
        elif k == "tool":
            inp = st.pop("input", None) or {}
            phase, target = classify(st.get("name"), inp)
            st.setdefault("phase", phase)
            st.setdefault("target", target)
            st["input_hash"] = hashlib.sha1((str(st.get("name")) + json.dumps(inp, sort_keys=True, default=str)).encode()).hexdigest()[:16]
            st["input_preview"] = json.dumps(inp, default=str)[:400]
            if inp:
                st["args_json"] = json.dumps(inp, default=str)[:4000]
            st["is_error"] = bool(st.get("is_error"))
            st["output_chars"] = int(st.get("output_chars") or 0)
            if last_llm is not None:
                st["llm_msg"] = last_llm["_id"]
                last_llm["tool_calls"] += 1
        elif k == "prompt":
            st.setdefault("name", "human")
        elif k == "span":
            st.setdefault("span_kind", "node")
            st.setdefault("node", st.get("name"))
        run["steps"].append(st)
    tss = [s["ts"] for s in run["steps"] if s.get("ts")]
    run["started"] = min(tss) if tss else None
    run["ended"] = max(tss) if tss else None
    return run


def load_dir(runs_dir):
    out = []
    if not os.path.isdir(runs_dir):
        return out
    for fn in sorted(os.listdir(runs_dir)):
        if fn.endswith(".json"):
            p = os.path.join(runs_dir, fn)
            try:
                with open(p, encoding="utf-8") as f:
                    run = normalize(json.load(f))
                run["file"] = p
                out.append(run)
            except (ValueError, OSError):
                continue
    return out
