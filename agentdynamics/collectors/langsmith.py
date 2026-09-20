"""LangSmith / LangChain / LangGraph integration.

Two ways in:
  1. Drop-in receiver: point any LangChain/LangGraph app at AgentDynamics with
         LANGSMITH_TRACING=true
         LANGSMITH_ENDPOINT=http://<host>:8787/langsmith
         LANGSMITH_API_KEY=<an AgentDynamics ingest key>
     The LangSmith SDK then sends runs here (/runs/batch, /runs, PATCH /runs/{id}, /runs/multipart, /feedback).
  2. Pull connector: read runs from an existing LangSmith project via its API (POST /runs/query).
"""
import json
import time
import urllib.request
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_policy


def parse_time(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e12 else float(v)
    s = str(v).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def merge(existing, update):
    """Apply a LangSmith PATCH onto a stored run."""
    out = dict(existing or {})
    for k, v in (update or {}).items():
        if v is None:
            continue
        if k == "_stub":
            continue
        if k == "extra" and isinstance(v, dict):
            ex = dict(out.get("extra") or {})
            for k2, v2 in v.items():
                if k2 == "metadata" and isinstance(v2, dict):
                    ex["metadata"] = {**(ex.get("metadata") or {}), **v2}
                else:
                    ex[k2] = v2
            out["extra"] = ex
        elif k == "feedback":
            out["feedback"] = (out.get("feedback") or []) + list(v)
        else:
            out[k] = v
    return out


def _usage(outputs, run):
    """Token usage from the many places LangChain puts it."""
    it = ot = cr = cw = 0
    stop = model = None
    if run.get("prompt_tokens") or run.get("completion_tokens"):
        it, ot = run.get("prompt_tokens") or 0, run.get("completion_tokens") or 0
    o = outputs or {}
    um = o.get("usage_metadata")
    gens = o.get("generations") or []
    flat = []
    for g in gens:
        flat.extend(g if isinstance(g, list) else [g])
    for g in flat:
        if not isinstance(g, dict):
            continue
        msg = g.get("message") or {}
        kw = msg.get("kwargs") if isinstance(msg, dict) and "kwargs" in msg else msg
        kw = kw or {}
        um = um or kw.get("usage_metadata")
        rm = kw.get("response_metadata") or {}
        gi = g.get("generation_info") or {}
        stop = stop or rm.get("stop_reason") or rm.get("finish_reason") or gi.get("finish_reason")
        model = model or rm.get("model") or rm.get("model_name")
    if um:
        it = um.get("input_tokens") or it
        ot = um.get("output_tokens") or ot
        det = um.get("input_token_details") or {}
        cr = det.get("cache_read") or 0
        cw = det.get("cache_creation") or 0
    tu = (o.get("llm_output") or {}).get("token_usage") or (o.get("llm_output") or {}).get("usage") or {}
    if tu and not (it or ot):
        it = tu.get("prompt_tokens") or tu.get("input_tokens") or 0
        ot = tu.get("completion_tokens") or tu.get("output_tokens") or 0
    return it, ot, cr, cw, stop, model


KIND = {"llm": "llm", "tool": "tool", "retriever": "retriever", "embedding": "embedding", "chain": "chain", "prompt": "chain", "parser": "chain"}


def to_span(r):
    """LangSmith run dict -> canonical span (None for feedback-only stubs whose run hasn't arrived yet)."""
    if r.get("_stub") and not r.get("run_type"):
        return None
    extra = r.get("extra") or {}
    md = {**(extra.get("metadata") or {}), **(r.get("metadata") or {})}
    inv = extra.get("invocation_params") or {}
    run_type = r.get("run_type") or "chain"
    kind = KIND.get(run_type, "chain")
    node = md.get("langgraph_node")
    if kind == "chain" and node and r.get("name") == node:
        kind = "node"
    if r.get("name") in ("__interrupt__",) or "GraphInterrupt" in str(r.get("error") or ""):
        kind = "human"
    it = ot = cr = cw = 0
    stop = model_out = None
    if kind in ("llm", "embedding"):
        it, ot, cr, cw, stop, model_out = _usage(r.get("outputs"), r)
    start = parse_time(r.get("start_time"))
    ttft = None
    for ev in r.get("events") or []:
        if ev.get("name") == "new_token" and start:
            t = parse_time(ev.get("time"))
            if t:
                ttft = max(0.0, (t - start) * 1000)
            break
    docs = None
    if kind == "retriever":
        d = (r.get("outputs") or {}).get("documents")
        docs = len(d) if isinstance(d, list) else None
    fb = []
    for k, v in (r.get("feedback_stats") or {}).items():
        if isinstance(v, dict) and v.get("avg") is not None:
            fb.append({"key": k, "score": v["avg"]})
    fb += [f for f in (r.get("feedback") or []) if f.get("score") is not None]
    err = r.get("error")
    return {
        "trace_id": str(r.get("trace_id") or r.get("id")), "span_id": str(r.get("id")),
        "parent_id": str(r["parent_run_id"]) if r.get("parent_run_id") else None,
        "name": r.get("name"), "kind": kind, "start": start, "end": parse_time(r.get("end_time")),
        "status": "error" if err else ("ok" if r.get("end_time") else "unset"), "error": err,
        "model": md.get("ls_model_name") or inv.get("model") or inv.get("model_name") or model_out,
        "provider": md.get("ls_provider"), "input_tokens": it, "output_tokens": ot, "cache_read": cr, "cache_write": cw,
        "cost": r.get("total_cost") if kind == "llm" and r.get("total_cost") is not None else None,
        "stop_reason": stop, "ttft_ms": ttft, "input": r.get("inputs"), "output": r.get("outputs"),
        "node": node, "agent": md.get("agent_name") or md.get("lc_agent_name"),
        "workflow": None,
        "project": r.get("session_name") or md.get("project"), "environment": md.get("environment") or md.get("env"),
        "session_id": md.get("thread_id") or md.get("session_id") or md.get("conversation_id"),
        "user_id": md.get("user_id"), "framework": "langgraph" if (node or md.get("langgraph_step") is not None) else "langchain",
        "docs": docs, "feedback": fb, "tags": r.get("tags") or [], "source": "langsmith",
    }


# ---------------------------------------------------------------- receiver payloads

def parse_batch(body):
    d = json.loads(body or b"{}")
    return d.get("post") or [], d.get("patch") or []


def parse_multipart(body, content_type):
    """/runs/multipart: parts named post.<id>, post.<id>.inputs, patch.<id>.outputs, feedback.<id> ..."""
    msg = BytesParser(policy=email_policy).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    posts, patches, feedback = {}, {}, []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition") or ""
        try:
            data = json.loads(part.get_content() if part.get_content_type().startswith("text") else part.get_payload(decode=True))
        except (ValueError, TypeError):
            continue
        bits = name.split(".")
        if len(bits) < 2:
            continue
        op, rid = bits[0], bits[1]
        target = posts if op == "post" else patches if op == "patch" else None
        if op == "feedback":
            feedback.append(data)
            continue
        if target is None:
            continue
        if len(bits) == 2:
            target.setdefault(rid, {}).update(data)
        else:
            target.setdefault(rid, {})[bits[2]] = data
    for rid, r in list(posts.items()) + list(patches.items()):
        r.setdefault("id", rid)
    return list(posts.values()), list(patches.values()), feedback


def zstd_available():
    try:
        import zstandard  # noqa: F401  (installed alongside recent langsmith SDKs)
        return True
    except ImportError:
        return False


def zstd_decompress(body):
    import io

    import zstandard
    # the SDK streams frames without a content size, so read through a stream reader
    return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body)).read()


def info():
    """What we tell the LangSmith SDK (GET /info). With zstandard available we accept compressed multipart
    (about 10x smaller uploads); otherwise the SDK falls back to plain JSON /runs/batch."""
    z = zstd_available()
    return {
        "version": "agentdynamics-0.4",
        "batch_ingest_config": {"scale_up_qsize_trigger": 1000, "scale_up_nthreads_limit": 16, "scale_down_nempty_trigger": 4,
                                "size_limit": 100, "size_limit_bytes": 20_971_520, "use_multipart_endpoint": z},
        "instance_flags": {"zstd_compression_enabled": z},
    }


INFO = info()


# ---------------------------------------------------------------- pull connector

class LangSmithPuller:
    """Incrementally pull runs from a LangSmith project."""

    def __init__(self, cfg, state):
        import os
        self.api = (cfg.get("api_url") or os.environ.get("LANGSMITH_ENDPOINT") or "https://api.smith.langchain.com").rstrip("/")
        self.key = os.environ.get(cfg.get("api_key_env", "LANGSMITH_API_KEY"), "")
        self.project = cfg["project"]
        self.state = state  # dict persisted by the engine
        self.lookback = float(cfg.get("lookback_hours", 24)) * 3600

    def _req(self, method, path, body=None, params=""):
        req = urllib.request.Request(f"{self.api}{path}{params}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"x-api-key": self.key, "Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")

    def pull(self, max_runs=5000):
        from urllib.parse import quote
        if not self.state.get("session_id"):
            ses = self._req("GET", "/sessions", params=f"?name={quote(self.project)}&limit=1")
            if not ses:
                raise RuntimeError(f"LangSmith project '{self.project}' not found")
            self.state["session_id"] = ses[0]["id"]
        since = self.state.get("since") or (time.time() - self.lookback)
        body = {"session": [self.state["session_id"]], "start_time": datetime.fromtimestamp(since, timezone.utc).isoformat(), "limit": 100}
        runs, newest = [], since
        while len(runs) < max_runs:
            res = self._req("POST", "/runs/query", body) or {}
            batch = res.get("runs") or []
            runs.extend(batch)
            for r in batch:
                newest = max(newest, parse_time(r.get("start_time")) or 0)
            nxt = (res.get("cursors") or {}).get("next")
            if not batch or not nxt:
                break
            body["cursor"] = nxt
        # re-read a small window next time so runs that were still open get their end state
        self.state["since"] = max(since, newest - 600)
        return runs
