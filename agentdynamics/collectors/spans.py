"""Canonical span model + trace assembler.

Every trace-based integration (OTLP, LangSmith, Langfuse, inbox files) converts its
native records into *canonical spans* and hands them to `build_run`, which assembles
one run (= one task) per trace. Analysis only ever sees runs.

Canonical span (dict):
  trace_id, span_id, parent_id, name, kind, start, end (epoch seconds), status ("ok"|"error"|"unset"),
  error, model, provider, input_tokens, output_tokens, cache_read, cache_write, cost, stop_reason,
  ttft_ms, input, output, node, agent, workflow, project, environment, session_id, user_id,
  framework, docs (retriever result count), feedback ([{key, score}]), tags, attrs, source

kind: llm | tool | retriever | embedding | agent | chain | node | guardrail | evaluator | human | span
"""
import hashlib
import json

from .. import pricing
from ..phases import classify

STRUCTURAL = {"agent", "chain", "node", "guardrail", "evaluator", "human", "span", "handoff"}
RATE_LIMIT_HINTS = ("429", "rate limit", "rate_limit", "ratelimit", "overloaded", "529", "too many requests", "quota")


def preview(v, n=600):
    if v is None:
        return ""
    if isinstance(v, str):
        return v[:n]
    try:
        return json.dumps(v, default=str)[:n]
    except (TypeError, ValueError):
        return str(v)[:n]


def extract_prompt(v):
    """Best-effort human-readable request text from a root span input."""
    if v is None:
        return ""
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return extract_prompt(json.loads(s))
            except ValueError:
                return s
        return s
    if isinstance(v, dict):
        for k in ("input", "question", "query", "prompt", "task", "text", "content", "message", "user_input"):
            if k in v and v[k]:
                return extract_prompt(v[k])
        if "messages" in v and v["messages"]:
            return extract_prompt(v["messages"])
        # graph state such as {"q": "...", "steps": 0}: use the first substantial string field
        for val in v.values():
            if isinstance(val, str) and len(val.strip()) > 3:
                return val.strip()
        return preview(v, 2000)
    if isinstance(v, list):
        # chat messages: take the last human/user message
        for m in reversed(v):
            if isinstance(m, list):
                r = extract_prompt(m)
                if r:
                    return r
            if isinstance(m, dict):
                role = m.get("role") or m.get("type") or (m.get("kwargs") or {}).get("type") or (m.get("id") or [""])[-1]
                if str(role).lower() in ("user", "human", "humanmessage"):
                    c = m.get("content") if "content" in m else (m.get("kwargs") or {}).get("content")
                    return extract_prompt(c)
            if isinstance(m, str):
                return m
        return preview(v, 2000)
    return str(v)


def _hash(name, inp):
    return hashlib.sha1((str(name) + preview(inp, 4000)).encode()).hexdigest()[:16]


def build_run(trace_id, spans, source):
    """Assemble canonical spans of one trace into a normalized run."""
    if not spans:
        return None
    by_id = {s["span_id"]: s for s in spans}
    roots = [s for s in spans if not s.get("parent_id") or s["parent_id"] not in by_id]
    roots.sort(key=lambda s: s.get("start") or 0)
    root = roots[0]

    depth_cache = {}

    def depth(s):
        sid = s["span_id"]
        if sid in depth_cache:
            return depth_cache[sid]
        d, cur, seen = 0, s, set()
        while cur.get("parent_id") in by_id and cur["span_id"] not in seen:
            seen.add(cur["span_id"])
            cur = by_id[cur["parent_id"]]
            d += 1
        depth_cache[sid] = d
        return d

    def inherited(s, field):
        cur, seen = s, set()
        while cur is not None and cur["span_id"] not in seen:
            if cur.get(field):
                return cur[field]
            seen.add(cur["span_id"])
            cur = by_id.get(cur.get("parent_id"))
        return None

    first = lambda f: next((s.get(f) for s in [root] + spans if s.get(f)), None)  # noqa: E731
    # the entry point names the workflow; GenAI-semconv agent roots are named "invoke_agent <agent>", so prefer the agent name
    workflow = root.get("workflow") or (root.get("agent") if root.get("kind") == "agent" else None) or root.get("name") or "trace"
    run = {
        "id": f"{source}:{trace_id}",
        "source": source,
        "file": None,
        "project": first("project") or "default",
        "environment": first("environment") or "default",
        "framework": "langgraph" if any(s.get("framework") == "langgraph" for s in spans) else (first("framework") or source),
        "workflow": workflow,
        "cwd": None,
        "title": extract_prompt(root.get("input"))[:200] or workflow,
        "agent_name": first("agent"),
        "thread_id": first("session_id"),
        "user_id": first("user_id"),
        "parent_id": None,
        "is_subagent": False,
        "version": None,
        "git_branch": None,
        "entrypoint": None,
        "tags": sorted({t for s in spans for t in (s.get("tags") or [])})[:20],
        "feedback": [f for s in spans for f in (s.get("feedback") or [])],
        "steps": [],
    }
    steps = run["steps"]
    prompt_text = extract_prompt(root.get("input"))
    steps.append({"kind": "prompt", "name": "trace", "ts": root.get("start"), "text": prompt_text[:2000] or workflow})

    ordered = sorted(spans, key=lambda s: ((s.get("start") or 0), depth(s)))
    last_llm = None
    for s in ordered:
        kind = s.get("kind") or "span"
        err = s.get("error") or ""
        st = {
            "span_id": s["span_id"], "parent_span_id": s.get("parent_id"), "depth": depth(s),
            "name": s.get("name") or kind, "ts": s.get("start"), "start_ts": s.get("start"), "end_ts": s.get("end"),
            "is_error": s.get("status") == "error", "error": preview(err, 400) if err else None,
            "node": inherited(s, "node"), "agent": inherited(s, "agent"), "span_kind": kind,
        }
        if st["start_ts"] and st["end_ts"]:
            st["duration_ms"] = max(0, int((st["end_ts"] - st["start_ts"]) * 1000))
        if kind in ("llm", "embedding"):
            model = s.get("model") or s.get("name")
            it, ot = int(s.get("input_tokens") or 0), int(s.get("output_tokens") or 0)
            cr, cw = int(s.get("cache_read") or 0), int(s.get("cache_write") or 0)
            # OTel/LangSmith report input_tokens inclusive of cache reads for most providers; keep uncached part separate
            if cr and it >= cr:
                it -= cr
            cost = s.get("cost")
            if cost is None:
                cost = pricing.cost(model, it, ot, cr, cw, 0)
            st.update({
                "kind": "llm", "model": model, "name": model, "input_tokens": it, "output_tokens": ot,
                "cache_read": cr, "cache_write": cw, "cost": float(cost or 0), "priced": bool(cost) or pricing.rates(model) is not None,
                "context_tokens": it + cr + cw, "stop_reason": s.get("stop_reason"), "ttft_ms": s.get("ttft_ms"),
                "text": preview(s.get("output"), 600), "tool_calls": 0, "thinking_tokens": int(s.get("thinking_tokens") or 0),
                "rate_limited": bool(err) and any(h in err.lower() for h in RATE_LIMIT_HINTS),
            })
            last_llm = st
        elif kind in ("tool", "retriever"):
            inp = s.get("input")
            phase, target = classify(s.get("name"), inp if isinstance(inp, dict) else {})
            if kind == "retriever":
                phase = "retrieve"
            elif phase == "other":
                phase = "execute"
            st.update({
                "kind": "tool", "phase": phase, "target": target or preview(inp, 200),
                "input_hash": _hash(s.get("name"), inp), "input_preview": preview(inp, 400),
                "output_chars": len(preview(s.get("output"), 10**7)), "docs": s.get("docs"),
                "text": preview(s.get("output"), 300),
            })
            if last_llm is not None:
                st["llm_msg"] = last_llm["span_id"]
                last_llm["tool_calls"] += 1
        else:
            st.update({"kind": "span", "input_preview": preview(s.get("input"), 400), "text": preview(s.get("output"), 300)})
            if kind == "human" or "interrupt" in (s.get("name") or "").lower() or "GraphInterrupt" in err:
                st["hitl"] = True
        steps.append(st)
    for i, st in enumerate(steps):
        st["seq"] = i
    tss = [s["ts"] for s in steps if s.get("ts")] + [s["end_ts"] for s in steps if s.get("end_ts")]
    run["started"] = min(tss) if tss else None
    run["ended"] = max(tss) if tss else None
    run["root_status"] = root.get("status")
    run["root_error"] = preview(root.get("error"), 400) if root.get("error") else None
    run["complete"] = root.get("end") is not None
    return run
