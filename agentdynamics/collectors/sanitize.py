"""Every canonical run, whatever its source, with the types the analysis expects.

Telemetry is input from outside. What a receiver accepts is stored (spans_raw, runs/*.json) and read again on every
rebuild, so one value of the wrong type -- a stop reason that is a number, a token count that is an object, a
timestamp of infinity -- would fail the analysis for everyone, on every refresh, until someone removed it by hand.
`run` is applied where each collector hands over its run (generic.normalize, spans.build_run,
claude_code.parse_file). A known field that holds the wrong type, or a number that isn't finite or within reason,
is dropped, as if it had never been sent -- a count or a cost becomes 0 instead, since every sum reads it; a valid
value, or an explicit None, is left exactly as it is. A step that names no kind is dropped. Unknown fields pass through. tests/test_fuzz.py feeds the receivers garbage.
"""
import math

MAX_TS = 1e11                 # the year 5138: a timestamp beyond is a unit mistake or an attack, not a time
MAX_COUNT = 1e13              # tokens, characters, calls
MAX_COST = 1e9
MAX_TEXT = 200_000
DROP = object()

RUN_STR = ("id", "source", "file", "project", "cwd", "title", "agent_name", "parent_id", "workflow", "version",
           "git_branch", "entrypoint", "environment", "framework", "thread_id", "user_id", "root_status", "root_error",
           "policy_version")
RUN_TS = ("started", "ended")
STEP_STR = ("kind", "name", "model", "rule", "guard", "agent", "node", "stop_reason", "tripwire", "text", "error",
            "span_kind", "phase", "target", "service_tier", "speed", "inference_geo", "input_preview", "args_json",
            "llm_msg", "_id", "input_hash", "output_preview", "status", "span_id", "parent_span_id")
STEP_TS = ("ts", "end_ts", "start_ts")
STEP_COUNT = ("input_tokens", "output_tokens", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h",
              "thinking_tokens", "context_tokens", "output_chars", "tool_calls", "grant_depth", "seq")
STEP_NUM = ("duration_ms", "ttft_ms")
STEP_COST = ("cost", "attributed_cost")
STEP_BOOL = ("is_error", "denied", "governed", "priced")


def text(v, limit=MAX_TEXT):
    if v is None:
        return None
    if isinstance(v, str):
        v = v if len(v) <= limit else v[:limit]
        try:
            v.encode("utf-8")
        except UnicodeEncodeError:     # a lone surrogate ("\ud800" is valid JSON): no store can hold it
            v = v.encode("utf-8", "replace").decode("utf-8")
        return v
    if isinstance(v, bool) or (isinstance(v, (int, float)) and math.isfinite(v)):
        return str(v)
    return DROP                        # a list or an object where a string belongs


def number(v, lo, hi, whole=False):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v) if whole else DROP
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return DROP
    if not isinstance(v, (int, float)):
        return DROP
    try:
        f = float(v)
    except OverflowError:              # an int too big to be a float is too big to be a count
        return DROP
    if not (math.isfinite(f) and lo <= f <= hi):
        return DROP
    return int(f) if whole and not isinstance(v, int) else v


def zero(v):
    return 0 if v is DROP else v


def flag(v):
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    if isinstance(v, (int, float)) and math.isfinite(v):
        return bool(v)
    return DROP


def _apply(d, keys, fn):
    for k in keys:
        if k in d:
            v = fn(d[k])
            if v is DROP:
                del d[k]
            else:
                d[k] = v


def run(r):
    """`r`, cleaned in place, and returned."""
    _apply(r, RUN_STR, lambda v: text(v, 1000))
    if not r.get("id"):
        r["id"] = "unnamed"
    if not r.get("project"):
        r["project"] = "default"
    _apply(r, RUN_TS, lambda v: number(v, 0, MAX_TS))
    _apply(r, ("is_subagent", "complete"), flag)
    for k in ("feedback", "tags"):
        if k in r and not isinstance(r[k], list):
            r[k] = []
    if isinstance(r.get("feedback"), list):
        fb = []
        for f in r["feedback"]:
            if isinstance(f, dict):
                f = dict(f)
                _apply(f, ("key", "comment"), lambda v: text(v, 1000))
                _apply(f, ("score",), lambda v: number(v, -1e6, 1e6))
                fb.append(f)
        r["feedback"] = fb
    if isinstance(r.get("tags"), list):
        r["tags"] = [t for t in r["tags"] if isinstance(t, str)]
    for k in ("policy", "metadata"):
        if k in r and r[k] is not None and not isinstance(r[k], dict):
            r[k] = None
    steps = r.get("steps")
    r["steps"] = [step(s) for s in steps if isinstance(s, dict) and isinstance(s.get("kind"), str)] \
        if isinstance(steps, list) else []
    return r


def step(s):
    """A step, cleaned in place (see the module docstring)."""
    _apply(s, STEP_STR, text)
    _apply(s, STEP_TS, lambda v: number(v, 0, MAX_TS))
    # a count or a cost that can't be read counted nothing: 0, which every sum can take
    _apply(s, STEP_COUNT, lambda v: zero(number(v, 0, MAX_COUNT, whole=True)))
    _apply(s, STEP_NUM, lambda v: number(v, -MAX_COUNT, MAX_COUNT))
    _apply(s, STEP_COST, lambda v: zero(number(v, 0, MAX_COST)))
    _apply(s, STEP_BOOL, flag)
    if s["kind"] in ("tool", "llm", "span") and not isinstance(s.get("name"), str):
        # the analysis names every call it counts; a call that arrived without a name still happened
        s["name"] = s.get("model") if s["kind"] == "llm" and isinstance(s.get("model"), str) else "unknown"
    return s
