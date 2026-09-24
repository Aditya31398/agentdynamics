"""OpenTelemetry (OTLP/HTTP) trace receiver: JSON and protobuf encodings, zero dependencies.

Maps the three agent semantic conventions in use today onto canonical spans:
  * OpenTelemetry GenAI semconv (gen_ai.*)       - OpenAI Agents SDK, Strands, Semantic Kernel, PydanticAI, ...
  * OpenInference (openinference.span.kind, llm.*) - Arize Phoenix instrumentors: LangChain/LangGraph, LlamaIndex,
                                                    CrewAI, DSPy, AutoGen, OpenAI, Anthropic, Bedrock, ...
  * OpenLLMetry / Traceloop (traceloop.*)         - Traceloop SDK instrumentors
plus LangSmith's OTel attributes (langsmith.*) and LangGraph metadata (langgraph_node, langgraph_step).
"""
import json
import struct

# ---------------------------------------------------------------- protobuf wire decoding

def _varint(buf, i):
    shift = res = 0
    while True:
        b = buf[i]
        i += 1
        res |= (b & 0x7F) << shift
        if not b & 0x80:
            return res, i
        shift += 7


def _fields(buf):
    """Yield (field_number, wire_type, value) for a protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            v = buf[i:i + 8]
            i += 8
        elif wt == 2:
            ln, i = _varint(buf, i)
            v = buf[i:i + ln]
            i += ln
        elif wt == 5:
            v = buf[i:i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        yield fn, wt, v


def _any_value(buf):
    for fn, wt, v in _fields(buf):
        if fn == 1:
            return v.decode("utf-8", "replace")
        if fn == 2:
            return bool(v)
        if fn == 3:
            return v - (1 << 64) if v >= 1 << 63 else v
        if fn == 4:
            return struct.unpack("<d", v)[0]
        if fn == 5:
            return [_any_value(x) for f2, _, x in _fields(v) if f2 == 1]
        if fn == 6:
            return dict(_kv(x) for f2, _, x in _fields(v) if f2 == 1)
        if fn == 7:
            return v.hex()
    return None


def _kv(buf):
    k, val = "", None
    for fn, _, v in _fields(buf):
        if fn == 1:
            k = v.decode("utf-8", "replace")
        elif fn == 2:
            val = _any_value(v)
    return k, val


def _span_pb(buf):
    s = {"attributes": {}, "events": [], "status": {}}
    for fn, wt, v in _fields(buf):
        if fn == 1:
            s["traceId"] = v.hex()
        elif fn == 2:
            s["spanId"] = v.hex()
        elif fn == 4:
            s["parentSpanId"] = v.hex()
        elif fn == 5:
            s["name"] = v.decode("utf-8", "replace")
        elif fn == 6:
            s["kind"] = v
        elif fn == 7:
            s["startTimeUnixNano"] = struct.unpack("<Q", v)[0]
        elif fn == 8:
            s["endTimeUnixNano"] = struct.unpack("<Q", v)[0]
        elif fn == 9:
            k, val = _kv(v)
            s["attributes"][k] = val
        elif fn == 11:
            ev = {"attributes": {}}
            for f2, _, x in _fields(v):
                if f2 == 1:
                    ev["timeUnixNano"] = struct.unpack("<Q", x)[0]
                elif f2 == 2:
                    ev["name"] = x.decode("utf-8", "replace")
                elif f2 == 3:
                    k, val = _kv(x)
                    ev["attributes"][k] = val
            s["events"].append(ev)
        elif fn == 15:
            for f2, _, x in _fields(v):
                if f2 == 2:
                    s["status"]["message"] = x.decode("utf-8", "replace")
                elif f2 == 3:
                    s["status"]["code"] = x
    return s


def decode_protobuf(body):
    """ExportTraceServiceRequest bytes -> list of (resource_attrs, scope_name, span_dict)."""
    out = []
    for fn, _, rs in _fields(body):
        if fn != 1:
            continue
        res_attrs, scopes = {}, []
        for f2, _, v in _fields(rs):
            if f2 == 1:
                for f3, _, kv in _fields(v):
                    if f3 == 1:
                        k, val = _kv(kv)
                        res_attrs[k] = val
            elif f2 == 2:
                scopes.append(v)
        for ss in scopes:
            scope = ""
            for f3, _, v in _fields(ss):
                if f3 == 1:
                    for f4, _, x in _fields(v):
                        if f4 == 1:
                            scope = x.decode("utf-8", "replace")
                elif f3 == 2:
                    out.append((res_attrs, scope, _span_pb(v)))
    return out


# ---------------------------------------------------------------- JSON decoding

def _json_any(v):
    if not isinstance(v, dict):
        return v
    for k, val in v.items():
        if k == "stringValue":
            return val
        if k == "boolValue":
            return bool(val)
        if k == "intValue":
            return int(val)
        if k == "doubleValue":
            return float(val)
        if k == "arrayValue":
            return [_json_any(x) for x in (val or {}).get("values", [])]
        if k == "kvlistValue":
            return {x["key"]: _json_any(x.get("value")) for x in (val or {}).get("values", [])}
        if k == "bytesValue":
            return val
    return None


def _json_attrs(lst):
    return {a["key"]: _json_any(a.get("value")) for a in lst or []}


def _hexid(v):
    """OTLP/JSON ids are hex strings; some exporters send base64. Normalize to hex."""
    if not v:
        return None
    if all(c in "0123456789abcdefABCDEF" for c in v):
        return v.lower()
    import base64
    try:
        return base64.b64decode(v).hex()
    except ValueError:
        return v


def decode_json(doc):
    out = []
    for rs in doc.get("resourceSpans", []):
        res_attrs = _json_attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans", []) or rs.get("instrumentationLibrarySpans", []):
            scope = (ss.get("scope") or ss.get("instrumentationLibrary") or {}).get("name", "")
            for sp in ss.get("spans", []):
                out.append((res_attrs, scope, {
                    "traceId": _hexid(sp.get("traceId")), "spanId": _hexid(sp.get("spanId")),
                    "parentSpanId": _hexid(sp.get("parentSpanId")), "name": sp.get("name"), "kind": sp.get("kind"),
                    "startTimeUnixNano": int(sp.get("startTimeUnixNano") or 0), "endTimeUnixNano": int(sp.get("endTimeUnixNano") or 0),
                    "attributes": _json_attrs(sp.get("attributes")),
                    "events": [{"name": e.get("name"), "timeUnixNano": int(e.get("timeUnixNano") or 0), "attributes": _json_attrs(e.get("attributes"))}
                               for e in sp.get("events", [])],
                    "status": {"code": {"STATUS_CODE_ERROR": 2, "STATUS_CODE_OK": 1}.get(sp.get("status", {}).get("code"), sp.get("status", {}).get("code")),
                               "message": sp.get("status", {}).get("message")},
                }))
    return out


# ---------------------------------------------------------------- semantic-convention mapping

def _input_convention(a):
    """Whether this span's input_tokens already counts its cache reads and writes, per the documented
    contract of whichever attribute supplied it. Mirrors _g's order so it describes the same key.

    gen_ai.usage.input_tokens   GenAI semconv: "SHOULD include all types of input tokens, including
                                cached tokens."
    llm.token_count.prompt      OpenInference: cache reads and writes are "tokens in the prompt".
    anything else               (gen_ai.usage.prompt_tokens from older semconv / OpenLLMetry, llm.usage.*)
                                has no documented rule we can cite: None, and the call is reported as
                                unverified if it involves cache tokens.
    """
    for k, conv in (("gen_ai.usage.input_tokens", "inclusive"), ("gen_ai.usage.prompt_tokens", None),
                    ("llm.token_count.prompt", "inclusive"), ("llm.usage.prompt_tokens", None)):
        if a.get(k) not in (None, "", []):
            return conv
    return None


def _g(a, *keys):
    for k in keys:
        v = a.get(k)
        if v not in (None, "", []):
            return v
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _metadata(a):
    md = a.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except ValueError:
            md = {}
    out = dict(md) if isinstance(md, dict) else {}
    for k, v in a.items():
        if k.startswith("metadata.") or k.startswith("langsmith.metadata."):
            out[k.split("metadata.", 1)[1]] = v
    return out


OI_KIND = {"LLM": "llm", "TOOL": "tool", "CHAIN": "chain", "AGENT": "agent", "RETRIEVER": "retriever", "EMBEDDING": "embedding",
           "RERANKER": "retriever", "GUARDRAIL": "guardrail", "EVALUATOR": "evaluator"}
GENAI_OP = {"chat": "llm", "text_completion": "llm", "generate_content": "llm", "completion": "llm", "execute_tool": "tool",
            "invoke_agent": "agent", "create_agent": "agent", "embeddings": "embedding", "handoff": "handoff"}


def _doc_count(a, kind):
    """Retrieved documents: explicit count, a list, or OpenInference's flattened retrieval.documents.<i>.* keys."""
    if kind != "retriever":
        return None
    if _num(a.get("retrieval.documents.count")) is not None:
        return int(_num(a["retrieval.documents.count"]))
    if isinstance(a.get("retrieval.documents"), list):
        return len(a["retrieval.documents"])
    idx = {k.split(".")[2] for k in a if k.startswith("retrieval.documents.") and k.split(".")[2].isdigit()}
    return len(idx)


def map_span(res, scope, sp):
    a = sp["attributes"]
    md = _metadata(a)
    oi = str(a.get("openinference.span.kind") or "").upper()
    tl = str(a.get("traceloop.span.kind") or "").lower()
    ls = str(a.get("langsmith.span.kind") or "").lower()
    op = str(a.get("gen_ai.operation.name") or "").lower()
    if oi in OI_KIND:
        kind = OI_KIND[oi]
    elif op in GENAI_OP:
        kind = GENAI_OP[op]
    elif ls:
        kind = {"llm": "llm", "tool": "tool", "retriever": "retriever", "embedding": "embedding", "chain": "chain", "prompt": "chain", "parser": "chain"}.get(ls, "chain")
    elif tl:
        kind = {"workflow": "chain", "task": "chain", "agent": "agent", "tool": "tool"}.get(tl, "chain")
    elif _g(a, "gen_ai.request.model", "llm.model_name", "gen_ai.response.model"):
        kind = "llm"
    elif a.get("gen_ai.tool.name") or a.get("tool.name"):
        kind = "tool"
    else:
        kind = "span"
    if md.get("langgraph_node") and kind == "chain" and sp.get("name") == md.get("langgraph_node"):
        kind = "node"

    status = sp.get("status") or {}
    err = None
    if status.get("code") == 2:
        err = status.get("message") or "error"
    for ev in sp.get("events", []):
        if ev.get("name") == "exception":
            ea = ev.get("attributes", {})
            err = f"{ea.get('exception.type', '')}: {ea.get('exception.message', '')}".strip(": ") or err
    finish = _g(a, "gen_ai.response.finish_reasons", "llm.finish_reason", "gen_ai.response.finish_reason")
    if isinstance(finish, list):
        finish = finish[0] if finish else None
    ttft = _num(_g(a, "gen_ai.response.time_to_first_token", "gen_ai.server.time_to_first_token", "llm.time_to_first_token"))
    if ttft is not None and ttft < 100:  # seconds -> ms
        ttft *= 1000
    start = sp["startTimeUnixNano"] / 1e9 if sp.get("startTimeUnixNano") else None
    end = sp["endTimeUnixNano"] / 1e9 if sp.get("endTimeUnixNano") else None
    inp = _g(a, "input.value", "gen_ai.prompt", "traceloop.entity.input", "gen_ai.input.messages", "langsmith.inputs", "gen_ai.prompt.0.content",
             "gen_ai.tool.call.arguments")
    out = _g(a, "output.value", "gen_ai.completion", "traceloop.entity.output", "gen_ai.output.messages", "langsmith.outputs",
             "gen_ai.completion.0.content", "gen_ai.tool.call.result")
    name = sp.get("name")
    if kind == "tool":
        name = _g(a, "gen_ai.tool.name", "tool.name") or name
    fw = "otel"
    if oi:
        fw = "openinference"
    elif tl:
        fw = "openllmetry"
    elif ls:
        fw = "langsmith-otel"
    elif op:
        fw = "genai-semconv"
    if md.get("langgraph_node") or "langgraph" in (scope or "").lower():
        fw = "langgraph"
    feedback = []
    for k, v in a.items():
        if k.startswith("feedback.") and _num(v) is not None:
            feedback.append({"key": k[9:], "score": _num(v)})
    return {
        "trace_id": sp.get("traceId"), "span_id": sp.get("spanId"), "parent_id": sp.get("parentSpanId") or None,
        "name": name, "kind": kind, "start": start, "end": end, "status": "error" if err else "ok", "error": err,
        "model": _g(a, "gen_ai.response.model", "gen_ai.request.model", "llm.model_name", "llm.request.model"),
        "provider": _g(a, "gen_ai.system", "gen_ai.provider.name", "llm.provider", "llm.system"),
        "input_tokens": _num(_g(a, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.token_count.prompt", "llm.usage.prompt_tokens")) or 0,
        "input_convention": _input_convention(a),
        "output_tokens": _num(_g(a, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "llm.token_count.completion", "llm.usage.completion_tokens")) or 0,
        "cache_read": _num(_g(a, "gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_read_input_tokens", "llm.token_count.prompt_details.cache_read",
                             "gen_ai.usage.input_tokens.cached")) or 0,
        "cache_write": _num(_g(a, "gen_ai.usage.cache_creation.input_tokens", "gen_ai.usage.cache_creation_input_tokens",
                              "llm.token_count.prompt_details.cache_write")) or 0,
        "thinking_tokens": _num(_g(a, "gen_ai.usage.reasoning_tokens", "llm.token_count.completion_details.reasoning")) or 0,
        "cost": _num(_g(a, "gen_ai.usage.cost", "llm.cost.total", "langsmith.total_cost")),
        "stop_reason": finish, "ttft_ms": ttft, "input": inp, "output": out,
        "node": md.get("langgraph_node") or a.get("langgraph.node") or a.get("graph.node.id"),
        "agent": _g(a, "gen_ai.agent.name", "agent.name", "graph.node.parent_id") or md.get("agent_name"),
        "workflow": _g(a, "traceloop.workflow.name", "gen_ai.workflow.name") or None,
        "project": _g(res, "service.name") if _g(res, "service.name") not in (None, "unknown_service") else md.get("project"),
        "environment": _g(res, "deployment.environment.name", "deployment.environment") or md.get("environment"),
        "session_id": _g(a, "session.id", "gen_ai.conversation.id", "langsmith.trace.session_id") or md.get("thread_id") or md.get("session_id"),
        "user_id": _g(a, "user.id", "enduser.id") or md.get("user_id"),
        "framework": fw, "docs": _doc_count(a, kind),
        "feedback": feedback, "tags": a.get("tag.tags") if isinstance(a.get("tag.tags"), list) else [],
        "source": "otlp",
    }


def parse_request(body, content_type="application/json"):
    """Return canonical spans from an OTLP/HTTP export request body."""
    if "protobuf" in (content_type or "") or (body[:1] not in (b"{", b"[") and body):
        records = decode_protobuf(body)
    else:
        records = decode_json(json.loads(body))
    return [map_span(r, sc, sp) for r, sc, sp in records if sp.get("traceId") and sp.get("spanId")]
