"""Turn normalized runs into tasks, baselines, scores, waste findings and events.

AppDynamics -> AgentDynamics mapping implemented here:
  Business Transaction  -> Task (one user request, end to end)
  BT type / entry point -> Task type (bugfix, feature, question ...)
  Dynamic baselines     -> per-task-type median/p90 of cost, duration, tokens
  Apdex                 -> Agent Apdex from outcome + cost vs baseline
  Health rules / events -> configurable rules evaluated per task
  Code-level hotspots   -> waste findings (redundant reads, loops, error streaks)
"""
import math
import operator
import re
import statistics
import time
from collections import Counter, defaultdict


# ---------------------------------------------------------------- task typing

TYPE_RULES = [
    ("review/audit", r"\b(security|audit|review|vulnerab|performance analysis|pen ?test|fuzz)"),
    ("bugfix", r"\b(fix|bug|error|broken|not working|doesn'?t work|isn'?t|aren'?t|crash|fail|messed up|wrong|issue|stuck|missing|forbidden|negative value|no data|still (getting|not|seeing)|4\d\d\b|5\d\d\b)"),
    ("git/deploy", r"\b(github|commit|push|pull request|\bpr\b|deploy|release|repo\b|merge|publish)"),
    ("testing", r"\b(tests?|unit test|coverage|e2e)\b"),
    ("refactor", r"\b(refactor|clean ?up|simplif|restructure|rename|reorganiz)"),
    ("question/research", r"(\?\s*$|^\s*(what|why|how|is|are|can|does|do|should|would|which|explain|any other|give me (more )?(ideas|options|the values))\b)"),
    ("feature/build", r"\b(create|build|add|implement|make|integrate|generate|develop|design|write|extend|enhance|improve)"),
    ("change request", r"^\s*(change|switch|use|update|replace|remove|move|star|convert|set|modify|have)\b"),
    ("question/research", r"\b(explain|research|compare|ideas?|options|think of|thoughts|opinion|check if|go through)\b"),
    ("setup/run", r"\b(install|set ?up|configure|run|start|launch|open)\b"),
]
FOLLOWUP = re.compile(
    r"^\s*(continue|go ahead|yes|yep|ok(ay)?|proceed|sure|do it|try( it)? (again|now)|next|both|skip)\b|"
    r"\b(continue|proceed|go ahead)\b|\b(done|is up|is uo|installed|completed)\s*[.!]?\s*$", re.I)
CORRECTION = re.compile(
    r"^\s*(no\b|nope|wrong|that'?s not|still\b|doesn'?t|didn'?t|not working|it'?s broken|revert|undo|why did you|"
    r"you (forgot|missed|broke|didn'?t)|i don'?t like|dont like)|still (not|doesn'?t|broken|failing|getting)|(completely )?messed up|not what I (asked|wanted)",
    re.I,
)


def classify_task(prompt_kind, text):
    return classify_task_detail(prompt_kind, text)[0]


def classify_task_detail(prompt_kind, text):
    """(task_type, how it was decided, what matched).

    how: "prompt kind" when the prompt says what it is (a slash command, a scheduled job, a subagent's
    brief); "follow-up" for a short "continue"/"yes" that carries on the previous task; "keywords" when an
    intent rule matched, with the matched text; "unmatched" when nothing did. Keyword typing is a guess
    about what someone meant, so it says so -- the same way outcome_source separates graded from inferred.
    """
    if prompt_kind == "scheduled":
        return "scheduled job", "prompt kind", None
    if prompt_kind == "delegated":
        return "subagent", "prompt kind", None
    t = (text or "").strip()
    low = t.lower()
    if prompt_kind == "command":
        return "slash command", "prompt kind", None
    if len(t) < 60 and FOLLOWUP.search(t):
        return "follow-up", "follow-up", None
    # The earliest intent keyword wins: "Create X, then test it" is a build, not a testing task.
    best = None
    for rank, (name, pat) in enumerate(TYPE_RULES):
        m = re.search(pat, low[:600])
        if m and (best is None or (m.start(), rank) < best[0]):
            best = ((m.start(), rank), name, m.group(0).strip())
    return (best[1], "keywords", best[2][:40]) if best else ("other", "unmatched", None)


# ---------------------------------------------------------------- helpers

def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    return v[f] if f == c else v[f] + (v[c] - v[f]) * (k - f)


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


IDLE_CAP = 300  # gaps longer than this (user away, waiting on approval) don't count as agent time


def _active_seconds(tss):
    v = sorted(x for x in tss if x)
    return round(sum(min(b - a, IDLE_CAP) for a, b in zip(v, v[1:])), 1)


# ---------------------------------------------------------------- segmentation

def segment(run):
    """Split a run's steps into tasks at each prompt step."""
    tasks, cur = [], None
    for s in run["steps"]:
        if s["kind"] == "prompt":
            cur = {"prompt_step": s, "steps": [s]}
            tasks.append(cur)
        else:
            if cur is None:
                cur = {"prompt_step": None, "steps": []}
                tasks.append(cur)
            cur["steps"].append(s)
    return tasks


# ---------------------------------------------------------------- per-task metrics

def task_metrics(run, idx, seg):
    steps = seg["steps"]
    p = seg["prompt_step"]
    llm = [s for s in steps if s["kind"] == "llm" and not s.get("denied")]  # a denied model call was never made
    tools = [s for s in steps if s["kind"] == "tool"]
    notices = [s for s in steps if s["kind"] == "notice"]
    tss = [s["ts"] for s in steps if s.get("ts")] + [s["end_ts"] for s in steps if s.get("end_ts")]
    started = min(tss) if tss else run.get("started")
    ended = max(tss) if tss else started

    t = {
        "id": f"{run['id']}#{idx}",
        "run_id": run["id"],
        "idx": idx,
        "project": run["project"],
        "source": run["source"],
        "is_subagent": 1 if run.get("is_subagent") else 0,
        "prompt_kind": p["name"] if p else "none",
        "prompt": (p or {}).get("text", "")[:2000],
        "started": started,
        "ended": ended,
        "wall_s": round((ended - started), 1) if started and ended else 0,
        "duration_s": _active_seconds(tss),
        "llm_calls": len(llm),
        "tool_calls": len(tools),
        "tool_errors": sum(1 for s in tools if s.get("is_error") and not s.get("denied")),
        "input_tokens": sum(s.get("input_tokens", 0) for s in llm),
        "output_tokens": sum(s.get("output_tokens", 0) for s in llm),
        "cache_read": sum(s.get("cache_read", 0) for s in llm),
        "cache_write": sum(s.get("cache_write", 0) for s in llm),
        "thinking_tokens": sum(s.get("thinking_tokens", 0) for s in llm),
        "cost": sum(s.get("cost", 0) for s in llm),
        "max_context": max([s.get("context_tokens", 0) for s in llm] or [0]),
        "models": ",".join(sorted({s["model"] for s in llm if s.get("model") and s["model"] != "<synthetic>"})),
        "interrupts": sum(1 for s in notices if s["name"] == "interrupt")
        + sum(1 for s in tools if s.get("interrupted") or s.get("rejected")),
        "compactions": sum(1 for s in notices if s["name"] == "compaction"),
        "api_errors": sum(1 for s in notices if s["name"] == "api_error"),
        "subagent_cost": 0.0,
        "subagents": 0,
    }
    t["total_tokens"] = t["input_tokens"] + t["output_tokens"] + t["cache_read"] + t["cache_write"]
    in_side = t["input_tokens"] + t["cache_read"] + t["cache_write"]
    t["cache_hit"] = round(t["cache_read"] / in_side, 3) if in_side else None
    t["tool_error_rate"] = round(t["tool_errors"] / len(tools), 3) if tools else 0
    t["task_type"], t["task_type_source"], t["task_type_match"] = classify_task_detail(t["prompt_kind"], t["prompt"])
    if run.get("workflow") and run.get("source") != "claude-code":
        # traced apps: the entry point names the business transaction, which beats any guess
        t["task_type"], t["task_type_source"], t["task_type_match"] = run["workflow"], "workflow", None

    # --- phase attribution: split each LLM call's cost across the tool calls it issued
    phase_calls = Counter(s["phase"] for s in tools)
    phase_cost = Counter()
    # map llm step -> tool steps that immediately follow it (same llm_msg)
    for i, s in enumerate(steps):
        if s["kind"] != "llm":
            continue
        issued = []
        for s2 in steps[i + 1:]:
            if s2["kind"] == "tool":
                issued.append(s2)
            elif s2["kind"] == "llm":
                break
            else:
                continue
        # only count tool steps sharing the same message id
        if issued:
            mid = issued[0].get("llm_msg")
            issued = [x for x in issued if x.get("llm_msg") == mid]
        c = s.get("cost", 0)
        if issued:
            share = c / len(issued)
            for x in issued:
                x["attributed_cost"] = share
                phase_cost[x["phase"]] += share
        else:
            phase_cost["respond"] += c
    t["phase_calls"] = dict(phase_calls)
    t["phase_cost"] = {k: round(v, 5) for k, v in phase_cost.items()}
    with_tools = [s for s in llm if s.get("tool_calls")]
    t["parallelism"] = round(sum(s["tool_calls"] for s in with_tools) / len(with_tools), 2) if with_tools else 0

    # --- process analysis & waste detection
    read_seen = {}  # input_hash -> seq
    call_seen = {}
    edited_since = set()
    edits_per_file = Counter()
    files_read = set()
    redundant = duplicates = 0
    streak = max_streak = 0
    large = 0
    first_edit_idx = None
    last_edit_i = None
    last_verify_i = None
    waste_cost = 0.0
    for i, s in enumerate(tools):
        s.setdefault("flags", [])
        ph = s["phase"]
        tgt = s.get("target", "")
        if s["name"] in ("Read", "NotebookRead"):
            files_read.add(tgt)
            h = s["input_hash"]
            if h in read_seen and tgt not in edited_since:
                s["flags"].append("redundant_read")
                redundant += 1
            read_seen[h] = i
            edited_since.discard(tgt)
        elif ph == "edit":
            edits_per_file[tgt] += 1
            edited_since.add(tgt)
            # an edit invalidates earlier identical calls
            call_seen.clear()
            if first_edit_idx is None:
                first_edit_idx = i
            last_edit_i = i
        if ph == "verify":
            last_verify_i = i
        if ph not in ("plan", "communicate", "edit") and s["name"] not in ("Read", "NotebookRead"):
            h = s["input_hash"]
            if h in call_seen and not s.get("is_error"):
                s["flags"].append("duplicate_call")
                duplicates += 1
            call_seen[h] = i
        if s.get("is_error"):
            streak += 1
            max_streak = max(max_streak, streak)
            if streak >= 3:
                s["flags"].append("error_streak")
        else:
            streak = 0
        if s.get("output_chars", 0) > 40000:
            s["flags"].append("large_output")
            large += 1
        if s.get("denied"):
            s["flags"].append("denied")  # tokens spent generating a call the policy refused
        if any(f in s["flags"] for f in ("redundant_read", "duplicate_call", "error_streak", "denied")):
            waste_cost += s.get("attributed_cost", 0)
    t["files_read"] = len(files_read)
    t["files_edited"] = len(edits_per_file)
    t["edits"] = sum(edits_per_file.values())
    t["max_edits_one_file"] = max(edits_per_file.values() or [0])
    t["churn_file"] = edits_per_file.most_common(1)[0][0] if edits_per_file else None
    t["redundant_reads"] = redundant
    t["duplicate_calls"] = duplicates
    t["max_error_streak"] = max_streak
    t["large_outputs"] = large
    t["explore_ratio"] = round(phase_calls.get("explore", 0) / len(tools), 3) if tools else 0
    t["steps_to_first_edit"] = first_edit_idx
    code_task = t["edits"] > 0 and any(
        re.search(r"\.(py|js|ts|tsx|jsx|go|rs|java|cs|rb|php|c|cpp|h|kt|swift|vue|svelte|sql)$", f or "", re.I)
        for f in edits_per_file
    )
    t["code_changed"] = 1 if code_task else 0
    t["verified"] = None
    if code_task:
        t["verified"] = 1 if (last_verify_i is not None and last_verify_i > last_edit_i) else 0
    t["unverified_edits"] = 1 if t["verified"] == 0 else 0
    t["waste_cost"] = round(waste_cost, 5)

    # --- final response & outcome signals (outcome finalized later with next prompt)
    last_llm = llm[-1] if llm else None
    t["final_stop"] = last_llm.get("stop_reason") if last_llm else None
    t["final_text"] = (last_llm or {}).get("text", "")[:600]
    last_tool = tools[-1] if tools else None
    t["ended_on_error"] = 1 if (last_tool and last_tool.get("is_error") and (not last_llm or last_llm["seq"] < last_tool["seq"])) else 0
    for s in steps:
        s["task_id"] = t["id"]
    flow_metrics(t, run, steps, llm, tools)
    return t


def governance_metrics(t, run, steps, tools):
    """Policy enforcement seen from the task's side (Aegis decisions recorded as steps)."""
    denied = [s for s in steps if s.get("denied")]
    tool_denied = [s for s in tools if s.get("denied")]
    t["governed"] = 1 if (run.get("policy_version") or any(s.get("governed") or s.get("rule") for s in steps)) else 0
    t["policy_version"] = run.get("policy_version")
    t["policy_denials"] = len(tool_denied)
    t["spend_denials"] = sum(1 for s in denied if s["kind"] == "llm")
    t["budget_denials"] = sum(1 for s in denied if str(s.get("rule") or "").startswith("budget."))
    t["revocations"] = sum(1 for s in steps if s["kind"] == "notice" and s.get("name") == "revoked")
    t["blocked_cost"] = round(sum(s.get("attributed_cost") or 0 for s in tool_denied), 6)
    t["denied_rules"] = dict(Counter(s.get("rule") or "unknown" for s in denied))
    # Probing is "refused again and again without getting anywhere". Counting only repeats of the
    # *same* tool misses the agent that alternates between two forbidden ones -- which is what a
    # prompt-injected agent told to "refund, then confirm by email" does without trying to evade
    # anything. Track both, and report the longer: consecutive denials end at the first success.
    streak = run_ = best = 0
    last = None
    for s in tools:
        if s.get("denied"):
            name = s.get("name")
            streak = streak + 1 if name == last else 1
            run_ += 1
            last = name
        else:
            streak, run_, last = 0, 0, None
        best = max(best, streak, run_)
    t["repeated_denials"] = best


OUTCOMES = ("completed", "failed", "rework", "interrupted")   # what a grade may state (store.OUTCOMES)
FEEDBACK_PASS = 0.5                                             # a feedback score at or above this is a pass

TRUNCATION = {"max_tokens", "length", "max_output_tokens", "MAX_TOKENS"}
REFUSAL = {"refusal", "content_filter", "SAFETY", "safety", "blocked"}
RATE_HINTS = ("429", "rate limit", "rate_limit", "overloaded", "529", "too many requests", "quota")


def flow_metrics(t, run, steps, llm, tools):
    """Agent-flow metrics that apply to any framework (graph nodes, handoffs, LLM health, retrieval)."""
    spans = [s for s in steps if s["kind"] == "span"]
    notices = [s for s in steps if s["kind"] == "notice"]
    t["environment"] = run.get("environment") or "default"
    t["framework"] = run.get("framework") or run.get("source")
    t["workflow"] = run.get("workflow") or (t["task_type"] if run.get("source") == "claude-code" else None)
    t["steps_total"] = len(llm) + len(tools)
    # model calls whose cache accounting rests on a guess (collectors/spans.uncached_input); None is
    # a source that is exact by construction, like the SDK, so only an explicit False counts
    t["tokens_unverified"] = sum(1 for s in llm if s.get("tokens_verified") is False)
    # agentdynamics.outcome() records a notice; the last one in the task wins. Kept on a transient
    # key -- finalize decides precedence, and write_analysis persists only TASK_COLS.
    graded = [s for s in notices if s.get("name") == "outcome" and s.get("outcome") in OUTCOMES]
    t["_sdk_grade"] = {"outcome": graded[-1]["outcome"], "reason": graded[-1].get("text")} if graded else None
    t["llm_errors"] = sum(1 for s in llm if s.get("is_error"))
    t["truncations"] = sum(1 for s in llm if s.get("stop_reason") in TRUNCATION)
    t["refusals"] = sum(1 for s in llm if s.get("stop_reason") in REFUSAL)
    t["rate_limited"] = sum(1 for s in llm if s.get("rate_limited")) + sum(
        1 for s in notices if s["name"] == "api_error" and any(h in (s.get("text") or "").lower() for h in RATE_HINTS))
    tt = [s["ttft_ms"] for s in llm if s.get("ttft_ms")]
    t["ttft_ms"] = round(statistics.median(tt)) if tt else None
    tps = [s["output_tokens"] / (s["duration_ms"] / 1000) for s in llm if s.get("duration_ms") and s.get("output_tokens", 0) > 20]
    t["out_tps"] = round(statistics.median(tps), 1) if tps else None
    rets = [s for s in tools if s.get("phase") == "retrieve"]
    t["retrievals"] = len(rets)
    t["empty_retrievals"] = sum(1 for s in rets if s.get("docs") == 0)
    t["unpriced"] = sum(1 for s in llm if s.get("priced") is False and (s.get("input_tokens") or s.get("output_tokens")))
    t["hitl"] = sum(1 for s in spans if s.get("hitl"))

    # node executions: LangGraph nodes (a span named after its node), explicit node spans, or agent spans
    node_execs = [s for s in spans if s.get("node") and (s.get("name") == s.get("node") or s.get("span_kind") == "node")]
    if not node_execs:
        node_execs = [s for s in spans if s.get("span_kind") == "agent"]
        for s in node_execs:
            if not s.get("node"):
                s["node"] = s.get("agent") or s.get("name")
    if node_execs:
        seq = [s.get("node") or s.get("name") for s in node_execs]
    else:
        # no graph: use the process phases of tool calls (explore -> edit -> verify ...)
        seq = [s["phase"] for s in tools]
    collapsed = [x for i, x in enumerate(seq) if i == 0 or x != seq[i - 1]]
    t["path"] = collapsed[:60]
    visits = Counter(s.get("node") or s.get("name") for s in node_execs)
    t["nodes"] = len(visits)
    t["max_node_visits"] = max(visits.values()) if visits else 0
    t["loop_node"] = visits.most_common(1)[0][0] if visits and t["max_node_visits"] > 1 else None
    # critical node: the node whose executions account for most of the task's elapsed time
    wall = max(t["wall_s"], 0.001)
    dur_by_node = Counter()
    for s in node_execs:
        dur_by_node[s.get("node") or s.get("name")] += (s.get("duration_ms") or 0) / 1000
    if dur_by_node:
        n, d = dur_by_node.most_common(1)[0]
        t["critical_node"], t["critical_share"] = n, round(min(1.0, d / wall), 3)
    else:
        t["critical_node"], t["critical_share"] = None, None
    # multi-agent handoffs
    agents = [s.get("agent") for s in steps if s["kind"] in ("llm", "tool") and s.get("agent")]
    aseq = [a for i, a in enumerate(agents) if i == 0 or a != agents[i - 1]]
    t["handoffs"] = max(0, len(aseq) - 1)
    t["pingpong"] = sum(1 for i in range(2, len(aseq)) if aseq[i] == aseq[i - 2])
    governance_metrics(t, run, steps, tools)
    fb = [f["score"] for f in run.get("feedback") or [] if isinstance(f.get("score"), (int, float))]
    t["feedback_score"] = round(statistics.mean(fb), 3) if fb and t["idx"] <= 1 else None


# ---------------------------------------------------------------- scoring

def score_task(t, base):
    s = {}
    b = base.get(t["task_type"]) or base.get("__all__") or {}
    med_cost = b.get("cost_p50") or 0
    ratio = (t["cost"] + t["subagent_cost"]) / med_cost if med_cost else 1
    t["cost_vs_baseline"] = round(ratio, 2)
    med_dur = b.get("duration_p50") or 0
    t["duration_vs_baseline"] = round(t["duration_s"] / med_dur, 2) if med_dur else 1
    waste_share = t["waste_cost"] / t["cost"] if t["cost"] else 0
    s["efficiency"] = clamp(100 - 35 * math.log2(max(ratio, 1)) - 100 * waste_share)
    focus = 100 - 6 * t["redundant_reads"] - 10 * t["duplicate_calls"] - 4 * max(0, t["max_edits_one_file"] - 4)
    if t["edits"] and t["explore_ratio"] > 0.75:
        focus -= 15
    focus -= 8 * max(0, (t.get("max_node_visits") or 0) - 3) + 10 * (t.get("pingpong") or 0)
    s["focus"] = clamp(focus) if (t["tool_calls"] or t.get("nodes")) else None
    llm_pen = 8 * min(t.get("llm_errors") or 0, 5) + 6 * min(t.get("truncations") or 0, 5) + 10 * min(t.get("refusals") or 0, 3)
    llm_pen += 40 if t.get("outcome") == "failed" else 0
    if t["tool_calls"]:
        s["reliability"] = clamp(100 * (1 - t["tool_error_rate"]) - 12 * max(0, t["max_error_streak"] - 1) - 15 * t["api_errors"] - llm_pen)
    else:
        s["reliability"] = clamp(100 - 15 * t["api_errors"] - llm_pen)
    s["verification"] = None if t["verified"] is None else (100.0 if t["verified"] else 25.0)
    if t["cache_hit"] is not None and t["llm_calls"] >= 3:
        ctx = 100 * min(1, t["cache_hit"] / 0.9)
        if t["max_context"] > 200_000:
            ctx -= 20
        ctx -= 15 * t["compactions"]
        s["context"] = clamp(ctx)
    else:
        s["context"] = None
    s["autonomy"] = clamp(100 - 45 * min(t["interrupts"], 2) - (35 if t.get("outcome") == "rework" else 0))
    if t.get("governed"):
        s["compliance"] = clamp(100 - 12 * (t.get("policy_denials") or 0) - 20 * max(0, (t.get("repeated_denials") or 0) - 1)
                                - 40 * (t.get("revocations") or 0) - 15 * (t.get("budget_denials") or 0))
    else:
        s["compliance"] = None
    weights = {"efficiency": 0.25, "focus": 0.15, "reliability": 0.2, "verification": 0.15, "context": 0.1, "autonomy": 0.15,
               "compliance": 0.15}
    tot = sum(weights[k] for k, v in s.items() if v is not None)
    s["overall"] = round(sum(weights[k] * v for k, v in s.items() if v is not None) / tot, 1) if tot else None
    t["scores"] = {k: (round(v, 1) if v is not None else None) for k, v in s.items()}
    t["score"] = t["scores"]["overall"]

    # Agent Apdex: T = 1.5x median cost of this task type
    T = 1.5 * med_cost if med_cost else None
    total = t["cost"] + t["subagent_cost"]
    if t["outcome"] in ("interrupted", "rework", "failed") or t["max_error_streak"] >= 4:
        t["apdex"] = "frustrated"
    elif T is None or total <= T:
        t["apdex"] = "satisfied"
    elif total <= 4 * T:
        t["apdex"] = "tolerating"
    else:
        t["apdex"] = "frustrated"


def apdex_score(tasks):
    if not tasks:
        return None
    c = Counter(t["apdex"] for t in tasks)
    return round((c["satisfied"] + c["tolerating"] / 2) / len(tasks), 3)


# ---------------------------------------------------------------- health rules

DEFAULT_RULES = [
    {"id": "cost_spike", "name": "Cost above baseline", "metric": "cost_vs_baseline", "op": ">", "value": 3, "severity": "warning",
     "message": "Cost {v}x the median for '{task_type}' tasks"},
    {"id": "cost_critical", "name": "Runaway cost", "metric": "cost_vs_baseline", "op": ">", "value": 8, "severity": "critical",
     "message": "Cost {v}x the median for '{task_type}' tasks"},
    {"id": "error_rate", "name": "High tool error rate", "metric": "tool_error_rate", "op": ">", "value": 0.25, "severity": "warning",
     "guard": {"tool_calls": 4}, "message": "{v:.0%} of tool calls failed"},
    {"id": "error_streak", "name": "Error retry loop", "metric": "max_error_streak", "op": ">=", "value": 3, "severity": "critical",
     "message": "{v} consecutive failing tool calls"},
    {"id": "loop", "name": "Repeated identical calls", "metric": "duplicate_calls", "op": ">=", "value": 3, "severity": "warning",
     "message": "{v} identical tool calls repeated without an intervening edit"},
    {"id": "redundant_reads", "name": "Redundant file reads", "metric": "redundant_reads", "op": ">=", "value": 3, "severity": "info",
     "message": "{v} files re-read with no change in between"},
    {"id": "unverified", "name": "Code changed but not verified", "metric": "unverified_edits", "op": "==", "value": 1, "severity": "warning",
     "message": "Edited code but never ran tests/build/app after the last edit"},
    {"id": "context", "name": "Context window pressure", "metric": "max_context", "op": ">", "value": 250000, "severity": "warning",
     "message": "Context reached {v:,} tokens"},
    {"id": "compaction", "name": "Context compacted", "metric": "compactions", "op": ">=", "value": 1, "severity": "info",
     "message": "Conversation was compacted {v} time(s); earlier detail was lost"},
    {"id": "interrupted", "name": "User interrupted agent", "metric": "interrupts", "op": ">=", "value": 1, "severity": "critical",
     "message": "User stopped or rejected the agent {v} time(s)"},
    {"id": "rework", "name": "User had to correct result", "metric": "rework", "op": "==", "value": 1, "severity": "warning",
     "message": "Next message was a correction: \"{next_prompt}\""},
    {"id": "low_cache", "name": "Poor prompt-cache reuse", "metric": "cache_hit", "op": "<", "value": 0.5, "severity": "info",
     "guard": {"llm_calls": 5}, "message": "Only {v:.0%} of input tokens came from cache"},
    {"id": "api_error", "name": "Model API errors", "metric": "api_errors", "op": ">=", "value": 1, "severity": "warning",
     "message": "{v} API error(s) (overload, rate limit, timeout)"},
    {"id": "slow", "name": "Slow task", "metric": "duration_vs_baseline", "op": ">", "value": 5, "severity": "info",
     "guard": {"duration_s": 120}, "message": "Took {v}x the usual time for '{task_type}' tasks"},
    # --- agent-flow rules (graphs, multi-agent, LLM health)
    {"id": "run_failed", "name": "Run failed", "metric": "failed", "op": "==", "value": 1, "severity": "critical",
     "message": "Run ended in error: {root_error}"},
    {"id": "graph_loop", "name": "Node loop", "metric": "max_node_visits", "op": ">=", "value": 5, "severity": "warning",
     "message": "Node '{loop_node}' ran {v} times in one run (possible loop / recursion)"},
    {"id": "pingpong", "name": "Agent handoff ping-pong", "metric": "pingpong", "op": ">=", "value": 2, "severity": "warning",
     "message": "Agents handed work back and forth {v} times"},
    {"id": "truncation", "name": "Output truncated", "metric": "truncations", "op": ">=", "value": 1, "severity": "warning",
     "message": "{v} model response(s) hit the max-token limit"},
    {"id": "refusal", "name": "Model refusal", "metric": "refusals", "op": ">=", "value": 1, "severity": "warning",
     "message": "{v} model response(s) refused or filtered"},
    {"id": "rate_limit", "name": "Rate limited / overloaded", "metric": "rate_limited", "op": ">=", "value": 1, "severity": "warning",
     "message": "{v} model call(s) rate-limited or overloaded"},
    {"id": "llm_errors", "name": "Model call errors", "metric": "llm_errors", "op": ">=", "value": 2, "severity": "warning",
     "message": "{v} model calls failed"},
    {"id": "empty_retrieval", "name": "Retrieval returned nothing", "metric": "empty_retrievals", "op": ">=", "value": 1,
     "severity": "info", "message": "{v} retrieval(s) returned no documents"},
    {"id": "negative_feedback", "name": "Negative user feedback", "metric": "feedback_score", "op": "<", "value": 0.5,
     "severity": "warning", "message": "Feedback score {v}"},
    # --- governance rules (Aegis)
    {"id": "policy_denials", "name": "Actions blocked by policy", "metric": "policy_denials", "op": ">=", "value": 3,
     "severity": "warning", "message": "{v} tool calls were refused by the policy"},
    {"id": "repeated_denials", "name": "Agent probing a boundary", "metric": "repeated_denials", "op": ">=", "value": 3,
     "severity": "critical", "message": "Same forbidden call attempted {v} times in a row (prompt injection or stuck agent)"},
    {"id": "revoked", "name": "Grant revoked", "metric": "revocations", "op": ">=", "value": 1, "severity": "critical",
     "message": "The agent's authority was revoked mid-run"},
    {"id": "budget_stop", "name": "Budget stop", "metric": "budget_denials", "op": ">=", "value": 1, "severity": "warning",
     "message": "Budget limit reached {v} time(s); work was stopped"},
    {"id": "slow_ttft", "name": "Slow first token", "metric": "ttft_ms", "op": ">", "value": 8000, "severity": "info",
     "message": "Median time to first token {v:,.0f} ms"},
]

OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
       "==": lambda a, b: a == b}


def evaluate_rules(t, rules):
    events = []
    for r in rules:
        if not r.get("enabled", True):
            continue
        v = t.get(r["metric"])
        if v is None:
            continue
        guard = r.get("guard") or {}
        if any((t.get(k) or 0) < gv for k, gv in guard.items()):
            continue
        if OPS[r["op"]](v, r["value"]):
            ctx = dict(t)
            ctx["v"] = v
            ctx["next_prompt"] = (t.get("next_prompt") or "")[:80]
            try:
                msg = r["message"].format(**ctx)
            except (KeyError, ValueError, IndexError):
                msg = f"{r['metric']}={v}"
            events.append({
                "id": f"{t['id']}:{r['id']}", "ts": t["ended"], "rule_id": r["id"], "rule": r["name"],
                "severity": r["severity"], "task_id": t["id"], "run_id": t["run_id"], "project": t["project"],
                "task_type": t["task_type"], "message": msg, "value": v,
            })
    return events


# ---------------------------------------------------------------- orchestration

def run_tasks(run):
    """Per-run analysis (cacheable): segment into tasks and compute task metrics."""
    return [task_metrics(run, i, seg) for i, seg in enumerate(segment(run))]


def _outcomes_claude(ts, now):
    for i, t in enumerate(ts):
        # "continue" / "yes" carries on the previous request, so it belongs to that task type
        if t["task_type"] == "follow-up" and i > 0 and ts[i - 1]["task_type"] not in ("follow-up",):
            t["task_type"] = ts[i - 1]["task_type"]
            t["task_type_source"], t["task_type_match"] = "follow-up", None   # inherited, not re-guessed
            if t["source"] == "claude-code":
                t["workflow"] = t["task_type"]
        nxt = next((x for x in ts[i + 1:] if x["prompt_kind"] in ("human", "command")), None)
        t["next_prompt"] = nxt["prompt"][:300] if nxt else None
        t["rework"] = 1 if nxt and CORRECTION.search(nxt["prompt"][:200]) else 0
        if t["interrupts"]:
            t["outcome"] = "interrupted"
        elif t["rework"]:
            t["outcome"] = "rework"
        elif t["ended_on_error"] or (t["api_errors"] and t["final_stop"] != "end_turn"):
            t["outcome"] = "failed"
        elif nxt is None and t["source"] == "claude-code" and t["ended"] and now - t["ended"] < 600:
            t["outcome"] = "in progress"
        elif t["final_stop"] in ("end_turn", "stop_sequence") or t["llm_calls"]:
            t["outcome"] = "completed"
        else:
            t["outcome"] = "unknown"


def _outcome_trace(t, run, now):
    t.setdefault("next_prompt", None)
    t.setdefault("rework", 0)
    if run.get("root_status") == "error" or t["ended_on_error"]:
        t["outcome"] = "failed"
    elif t["hitl"] and not run.get("complete"):
        t["outcome"] = "interrupted"
    elif t["rework"] or (t["feedback_score"] is not None and t["feedback_score"] < 0.5):
        t["outcome"] = "rework"
    elif run.get("complete") is False:
        t["outcome"] = "in progress" if t["ended"] and now - t["ended"] < 600 else "unknown"
    else:
        t["outcome"] = "completed"
    t["root_error"] = run.get("root_error")


def _apply_grades(tasks, grades):
    """Settle each outcome by the strongest evidence available, and record which one it was.

    stated after the fact (API)  >  stated in the run (agentdynamics.outcome)  >  recorded feedback
    >  inference from signals.

    Inference is a guess built from errors, interrupts and corrections. Apdex, success rate and the
    process score all inherit it, so anything that states the outcome outright must win, and the
    console has to be able to say how much of a success rate is guessed. Idempotent: the cached
    per-run tasks are re-finalized on every refresh, so nothing here may consume its input.
    """
    for t in tasks:
        api, sdk = grades.get(t["id"]), t.get("_sdk_grade")
        if api:
            t["outcome"], t["outcome_source"] = api["outcome"], "graded"
            t["outcome_reason"] = api.get("reason") or f"graded by {api.get('graded_by') or 'api'}"
        elif sdk:
            t["outcome"], t["outcome_source"] = sdk["outcome"], "graded"
            t["outcome_reason"] = sdk.get("reason") or "graded in the run"
        elif t.get("feedback_score") is not None:
            ok = t["feedback_score"] >= FEEDBACK_PASS
            t["outcome"] = "completed" if ok else "rework"
            t["outcome_source"], t["outcome_reason"] = "feedback", f"feedback score {t['feedback_score']:g}"
        else:
            t["outcome_source"], t["outcome_reason"] = "inferred", None


# Everything finalize writes onto a task *before* scoring, for tasks whose run did not change. If one of
# these moves, the task's row must be rewritten and re-scored; if none did and its baseline didn't either,
# the previous score and events still hold. A field added to the settle phase below must be added here,
# or incremental refreshes will serve it stale -- test_incremental.py compares every column against a
# full rebuild to catch exactly that.
SETTLED_FIELDS = ("outcome", "outcome_source", "outcome_reason", "next_prompt", "rework", "root_error",
                  "task_type", "task_type_source", "task_type_match", "workflow",
                  "subagent_cost", "subagents", "parent_task_id")


_settled = operator.itemgetter(*SETTLED_FIELDS)     # built in C: this runs for every task on every refresh


def _settled_values(t):
    try:
        return _settled(t)
    except KeyError:          # a field some sources never set (Claude Code tasks carry no root_error)
        return tuple(t.get(f) for f in SETTLED_FIELDS)


class ScoreCache:
    """What finalize needs to skip unchanged tasks on the next refresh.

    sig[task_id]     the settled fields plus the two baseline numbers scoring reads
    events[task_id]  the health events that signature produced
    changed          task ids scored this time (their rows must be written)
    """

    def __init__(self):
        self.sig, self.events, self.changed = {}, {}, set()


BASELINE_EXACT_UP_TO = 20      # below this many tasks, a type's baseline uses all of them
BASELINE_GROWTH = 1.05          # above it, only when the count has grown 5% since the last step


def baseline_sample_size(n):
    """How many of a type's n tasks its baseline is computed from.

    Every task is scored against its type's baseline, so a baseline that moved with each arrival would
    make every refresh re-score the whole type -- no cheaper than rebuilding. Instead the baseline is
    computed from the type's *earliest* M tasks (by start, then id), and M only advances in 5% steps: a
    type re-scores once per 5% of growth, about 20 re-scores per new task however large the store, and
    never oscillates. It depends on the data alone, so a full rebuild computes the same baseline. It
    leaves out at most the newest 5% of a type's history, which is noise for a "what is normal" figure.
    """
    if n <= BASELINE_EXACT_UP_TO:
        return n
    m = BASELINE_EXACT_UP_TO
    while True:
        nxt = max(m + 1, int(m * BASELINE_GROWTH))
        if nxt > n:
            return m
        m = nxt


def finalize(runs, tasks_by_run, rules=None, now=None, grades=None, cache=None, dirty=()):
    """Cross-run analysis: outcomes, subagent roll-up, baselines, scores, events.

    With a `cache` (a ScoreCache kept between calls), tasks from runs not in `dirty` whose settled
    fields and baseline are unchanged keep their previous score and events instead of being re-scored;
    `cache.changed` then lists the task ids that were. The result is the same as without a cache.
    """
    rules = rules or DEFAULT_RULES
    now = now or time.time()
    all_tasks = [t for r in runs for t in tasks_by_run[r["id"]]]
    for t in all_tasks:
        t["subagent_cost"], t["subagents"], t["parent_task_id"] = 0.0, 0, None

    threads = defaultdict(list)
    for run in runs:
        ts = tasks_by_run[run["id"]]
        if run["source"] == "claude-code" or not run.get("workflow"):
            _outcomes_claude(ts, now)
        else:
            # Derived afresh every pass, never carried over. The engine keeps an unchanged run's task
            # objects between refreshes, and linking below only *sets* these on tasks that have a
            # successor: a task whose follow-up was deleted, edited or moved to another thread kept the
            # old values -- and so its "rework" outcome -- until a full rebuild. test_incremental found it.
            for t in ts:
                t["next_prompt"], t["rework"] = None, 0
            if run.get("thread_id"):
                # per project: thread ids are chosen by clients, and a run from another project that happens
                # to (or means to) reuse one must not become "the next message" in this conversation
                threads[(run["source"], run.get("project"), run["thread_id"])].extend(ts)
    # traced conversations: the next trace in the same thread plays the role of "your next message"
    for group in threads.values():
        # ties on start time break by id, not by whatever order the runs happen to be held in -- which
        # differs between a long-running engine and a fresh rebuild
        group.sort(key=lambda x: (x["started"] or 0, x["id"]))
        for a, b in zip(group, group[1:]):
            a["next_prompt"] = b["prompt"][:300]
            a["rework"] = 1 if CORRECTION.search(b["prompt"][:200]) else 0
    for run in runs:
        if not (run["source"] == "claude-code" or not run.get("workflow")):
            for t in tasks_by_run[run["id"]]:
                _outcome_trace(t, run, now)
    # before the roll-up, so baselines, scores and health events all see the settled outcome
    _apply_grades(all_tasks, grades or {})

    # subagent roll-up: link child runs to the parent task that spawned them
    task_by_id = {t["id"]: t for t in all_tasks}
    parent_tasks = defaultdict(list)
    for t in all_tasks:
        if not t["is_subagent"]:
            parent_tasks[t["run_id"]].append(t)
    spawn_map = {}
    for run in runs:
        if run.get("is_subagent"):
            continue
        for s in run["steps"]:
            if s["kind"] == "tool" and s.get("subagent_id"):
                spawn_map[s["subagent_id"]] = s.get("task_id")
    for run in runs:
        if not run.get("is_subagent"):
            continue
        agent_id = run["id"].split(":")[-1].replace("agent-", "")
        parent_task_id = spawn_map.get(agent_id)
        if not parent_task_id:
            cands = [t for t in parent_tasks.get(run["parent_id"], []) if t["started"] and run["started"] and t["started"] <= run["started"]]
            parent_task_id = cands[-1]["id"] if cands else None
        # parent_id is chosen by the client: a subagent rolls up only into a parent in its own project,
        # or another project's task would carry this one's cost
        pt = task_by_id.get(parent_task_id)
        if pt is not None and pt.get("project") != run.get("project"):
            parent_task_id = None
        run["parent_task_id"] = parent_task_id
        for t in tasks_by_run[run["id"]]:
            t["parent_task_id"] = parent_task_id
            pt = task_by_id.get(parent_task_id)
            if pt:
                pt["subagent_cost"] += t["cost"]
                pt["subagents"] += 1

    # baselines per task type (top-level tasks only)
    main = [t for t in all_tasks if not t["is_subagent"] and t["llm_calls"] > 0]
    groups = defaultdict(list)
    for t in main:
        groups[t["task_type"]].append(t)
    groups["__all__"] = main
    sub = [t for t in all_tasks if t["is_subagent"] and t["llm_calls"] > 0]
    if sub:
        groups["subagent"] = sub
    baselines = {}
    for k, g in groups.items():
        if len(g) < 3 and k != "__all__":
            continue
        n = len(g)
        g = sorted(g, key=lambda t: (t["started"] or 0, t["id"]))[:baseline_sample_size(n)]
        costs = [t["cost"] + t["subagent_cost"] for t in g]
        durs = [t["duration_s"] for t in g]
        baselines[k] = {
            "n": n, "sample": len(g),
            "cost_p50": pct(costs, 0.5), "cost_p90": pct(costs, 0.9),
            "duration_p50": pct(durs, 0.5), "duration_p90": pct(durs, 0.9),
            "tokens_p50": pct([t["total_tokens"] for t in g], 0.5),
            "tool_calls_p50": pct([t["tool_calls"] for t in g], 0.5),
            "steps_p50": pct([t["steps_total"] for t in g], 0.5),
        }

    events = []
    dirty = set(dirty)
    if cache is not None:
        cache.changed = set()
        live = {t["id"] for t in all_tasks}
        for tid in [k for k in cache.sig if k not in live]:     # tasks that no longer exist
            cache.sig.pop(tid, None)
            cache.events.pop(tid, None)
    for t in all_tasks:
        if cache is not None:
            b = baselines.get(t["task_type"]) or baselines.get("__all__") or {}
            sig = (_settled_values(t), b.get("cost_p50"), b.get("duration_p50"))
            if t["run_id"] not in dirty and cache.sig.get(t["id"]) == sig:
                events.extend(cache.events[t["id"]])            # nothing it depends on moved
                continue
        t["failed"] = 1 if t.get("outcome") == "failed" else 0
        if t["llm_calls"] == 0 and t["tool_calls"] == 0:
            t.update({"scores": {}, "score": None, "apdex": None, "cost_vs_baseline": None, "duration_vs_baseline": None})
            ev = []
        else:
            score_task(t, baselines)
            ev = evaluate_rules(t, rules)
        events.extend(ev)
        if cache is not None:
            cache.sig[t["id"]], cache.events[t["id"]] = sig, ev
            cache.changed.add(t["id"])
    return all_tasks, baselines, events


def analyze(runs, rules=None, now=None):
    """One-shot analysis of a list of runs (used by tests and the CLI)."""
    return finalize(runs, {r["id"]: run_tasks(r) for r in runs}, rules, now)


# ---------------------------------------------------------------- process review (coaching)

def process_insights(tasks):
    """Aggregate findings meant to help a human assess how the agent works."""
    # One canonical order. The engine holds tasks in whatever order runs arrived, which differs between
    # a long-running process and a fresh rebuild; float sums round differently in a different order,
    # and example lists are cut to their first few. Sorting makes the result depend on the data alone.
    main = sorted((t for t in tasks if not t["is_subagent"] and t["llm_calls"] > 0),
                  key=lambda t: (t["started"] or 0, t["id"]))
    if not main:
        return []
    out = []
    code = [t for t in main if t["code_changed"]]
    if code:
        unv = [t for t in code if not t["verified"]]
        out.append({
            "id": "verification", "title": "Verification after code changes",
            "metric": f"{100 * (1 - len(unv) / len(code)):.0f}% verified",
            "detail": f"{len(unv)} of {len(code)} code-changing tasks ended without running tests, a build, or the app after the last edit.",
            "severity": "warning" if len(unv) / len(code) > 0.3 else "ok",
            "advice": "Add to CLAUDE.md: 'After editing code, always run the relevant tests or build before reporting done.'",
            "examples": [t["id"] for t in sorted(unv, key=lambda x: -x["cost"])[:5]],
        })
    total_cost = sum(t["cost"] for t in main) or 1
    waste = sum(t["waste_cost"] for t in main)
    out.append({
        "id": "waste", "title": "Avoidable work (waste)",
        "metric": f"${waste:.2f} ({100 * waste / total_cost:.1f}%)",
        "detail": f"Redundant reads: {sum(t['redundant_reads'] for t in main)}, duplicate calls: {sum(t['duplicate_calls'] for t in main)}, "
                  f"calls inside error streaks: {sum(max(0, t['max_error_streak'] - 2) for t in main)}.",
        "severity": "warning" if waste / total_cost > 0.05 else "ok",
        "advice": "Large re-reads usually mean context was compacted or the agent lost track; smaller, focused tasks help.",
        "examples": [t["id"] for t in sorted(main, key=lambda x: -x["waste_cost"])[:5] if t["waste_cost"] > 0],
    })
    errs = sum(t["tool_errors"] for t in main)
    calls = sum(t["tool_calls"] for t in main) or 1
    streaky = [t for t in main if t["max_error_streak"] >= 3]
    out.append({
        "id": "errors", "title": "Tool reliability",
        "metric": f"{100 * errs / calls:.1f}% tool calls failed",
        "detail": f"{errs} failed calls; {len(streaky)} tasks had 3+ consecutive failures (the agent kept retrying).",
        "severity": "warning" if errs / calls > 0.08 or streaky else "ok",
        "advice": "Look at the error streak examples: repeated shell-quoting or path errors can be fixed with a CLAUDE.md note about the environment (e.g. Windows paths, PowerShell vs Bash).",
        "examples": [t["id"] for t in sorted(streaky, key=lambda x: -x["max_error_streak"])[:5]],
    })
    ph = Counter()
    for t in main:
        ph.update(t["phase_cost"])
    tot = sum(ph.values()) or 1
    out.append({
        "id": "phases", "title": "Where the effort goes",
        "metric": ", ".join(f"{k} {100 * v / tot:.0f}%" for k, v in ph.most_common(4)),
        "detail": "Share of model spend attributed to each phase (explore, edit, verify, ...). 'respond' is spend on turns that only produced text.",
        "severity": "info",
        "advice": "High explore share on small tasks suggests missing project context; a CLAUDE.md with architecture notes reduces it.",
        "examples": [],
        "breakdown": {k: round(v, 4) for k, v in ph.items()},
    })
    rework = [t for t in main if t["outcome"] in ("rework", "interrupted")]
    out.append({
        "id": "rework", "title": "First-time-right rate",
        "metric": f"{100 * (1 - len(rework) / len(main)):.0f}%",
        "detail": f"{len(rework)} of {len(main)} tasks were interrupted or followed by a correction from you.",
        "severity": "warning" if len(rework) / len(main) > 0.15 else "ok",
        "advice": "Open these tasks and compare the prompt to the final answer: ambiguity in the request vs. agent error.",
        "examples": [t["id"] for t in rework[:8]],
    })
    ctx = [t for t in main if t["max_context"] > 250_000 or t["compactions"]]
    ch = [t["cache_hit"] for t in main if t["cache_hit"] is not None]
    out.append({
        "id": "context", "title": "Context & cache hygiene",
        "metric": f"median cache hit {100 * (statistics.median(ch) if ch else 0):.0f}%",
        "detail": f"{len(ctx)} tasks pushed context past 250k tokens or triggered compaction.",
        "severity": "warning" if ctx else "ok",
        "advice": "Very long sessions get expensive per turn. Start a fresh session for unrelated work.",
        "examples": [t["id"] for t in sorted(ctx, key=lambda x: -x["max_context"])[:5]],
    })
    par = [t["parallelism"] for t in main if t["parallelism"]]
    out.append({
        "id": "parallel", "title": "Parallel tool use",
        "metric": f"{statistics.mean(par) if par else 0:.2f} tools per model turn",
        "detail": "Each model turn re-sends the whole context. Batching independent tool calls into one turn cuts turns and cost.",
        "severity": "info",
        "advice": "Values near 1.0 mean strictly sequential work.",
        "examples": [],
    })
    churn = [t for t in main if t["max_edits_one_file"] >= 8]
    out.append({
        "id": "churn", "title": "Edit churn",
        "metric": f"{len(churn)} tasks with 8+ edits to one file",
        "detail": "Many small edits to the same file often mean trial-and-error instead of a plan.",
        "severity": "info" if not churn else "warning",
        "advice": "Asking for a plan first (plan mode) on bigger changes usually reduces churn.",
        "examples": [t["id"] for t in sorted(churn, key=lambda x: -x["max_edits_one_file"])[:5]],
    })
    return out
