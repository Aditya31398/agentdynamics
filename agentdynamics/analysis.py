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

from . import pricing
from .privacy import TASK_TEXT_FIELDS


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


def wilson(k, n, z=1.96):
    """The 95% Wilson score interval for k successes in n, [low, high]; None for n = 0."""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(max(0.0, mid - half), 3), round(min(1.0, mid + half), 3)]


ERROR_CAUSES = (            # first match wins: a tool error's text, sorted into what usually fixes it
    ("rate limit", re.compile(r"\b429\b|rate.?limit|too many requests|overloaded|\b529\b", re.I)),
    ("timeout", re.compile(r"time[ds]?\s*out|deadline exceeded|TimeoutError", re.I)),
    ("permission", re.compile(r"\b40[13]\b|permission|forbidden|unauthori[sz]ed|access denied|EACCES", re.I)),
    ("not found", re.compile(r"\b404\b|not found|no such file|ENOENT|does not exist", re.I)),
    ("bad input", re.compile(r"\b4(00|22)\b|invalid|validation|bad request|malformed|missing required|TypeError|"
                             r"ValueError|KeyError|schema", re.I)),
    ("upstream", re.compile(r"\b5\d\d\b|connection|unavailable|ECONN|reset by peer|bad gateway|internal server", re.I)),
)


def error_cause(text):
    """What kind of failure a tool error is, from its text: rate limit, timeout, permission, not found, bad input,
    upstream, or other. A heuristic on the message, not the source's own classification -- most don't give one."""
    for name, rx in ERROR_CAUSES:
        if text and rx.search(text):
            return name
    return "other"


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


IDLE_CAP = 300  # a gap between events longer than this, covered by no step, is someone away: not agent time


def _union(spans):
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1][1] = b
        else:
            out.append([a, b])
    return out


def _active_seconds(steps):
    """Agent time: when a step was running, or the agent was between events, less the time a person had it.

    Running: each step's own [start, end] -- a 20-minute build counts 20 minutes. Between events: a gap of up to
    IDLE_CAP between consecutive timestamps (a model call logged only when it ended, say); a longer gap that no
    step covers is idle. A person's time: human-in-the-loop spans and tool calls a person rejected. A wait for
    an approval inside a tool call that was then allowed can't be told apart from the tool running unless the
    source records it.
    """
    pts = sorted({x for s in steps for x in (s.get("ts"), s.get("end_ts")) if x is not None})
    busy = [(a, b) for a, b in zip(pts, pts[1:]) if b - a <= IDLE_CAP]
    human = []
    for s in steps:
        a = s.get("start_ts") if s.get("start_ts") is not None else s.get("ts")
        b = s.get("end_ts")
        if a is None or b is None or b <= a:
            continue
        if s.get("hitl") or s.get("rejected"):
            human.append((a, b))
        elif s["kind"] in ("llm", "tool", "span"):
            busy.append((a, b))
    busy, human = _union(busy), _union(human)
    total = sum(b - a for a, b in busy)
    i = 0
    for a, b in busy:                                  # both sorted: one sweep subtracts the overlap
        while i < len(human) and human[i][1] <= a:
            i += 1
        j = i
        while j < len(human) and human[j][0] < b:
            total -= min(b, human[j][1]) - max(a, human[j][0])
            j += 1
    return round(max(total, 0.0), 1)


CHARS_PER_TOKEN = 4     # a tool result's size in context, estimated from its characters


def _split_turn(s):
    """(input-side cost, output cost) of one model call: its cost, split by its list-price shares."""
    c = s.get("cost") or 0.0
    r = pricing.rates(s.get("model")) if c else None
    if not r:
        return c, 0.0
    w1h = s.get("cache_write_1h") or 0
    inp = (s.get("input_tokens", 0) * r["input"] + s.get("cache_read", 0) * r["cache_read"]
           + (s.get("cache_write", 0) - w1h) * r["cache_write_5m"] + w1h * r["cache_write_1h"])
    out = s.get("output_tokens", 0) * r["output"]
    return (c * inp / (inp + out), c * out / (inp + out)) if inp + out else (c, 0.0)


def attribute(steps):
    """Each model call's cost, charged to what caused it. Returns {phase: cost}, which adds up to the task's cost,
    and sets each tool step's `attributed_cost`.

    * Output: split evenly over the tool calls the turn issued (writing them), or "respond" if it issued none.
    * Input: every later turn re-sends what earlier tool results put in the context. A turn's input-side cost is
      shared by token count among the results it carried (estimated from their size) and the rest of the context
      -- instructions, the prompt and the model's own messages -- which goes to "context". A result is carried
      by later turns of the same agent until a compaction.

    So a redundant read costs what it really cost: writing the call, then its output riding along in every turn
    after it. Linear in the steps: the per-token rate of each turn is summed once (prefix sums per agent).
    """
    phase_cost = Counter()
    F = defaultdict(lambda: [0.0])        # agent -> prefix sums of each turn's input cost per carried token
    E = defaultdict(float)                # agent -> tokens of tool results it currently carries
    open_items = defaultdict(list)        # agent -> [(tool step, tokens, prefix index when it entered)]
    carried = {}                          # id(tool step) -> its carried cost
    turns = {s.get("agent") for s in steps if s["kind"] == "llm"}

    def carrier(agent):                   # a result's agent, or whoever takes the turns when its agent has none
        if agent in turns or not turns:
            return agent
        return None if None in turns else next(iter(turns)) if len(turns) == 1 else agent

    def close(agent):
        f = F[agent]
        for x, e, start in open_items.pop(agent, ()):
            carried[id(x)] = e * (f[-1] - f[start])
        E[agent] = 0.0

    for i, s in enumerate(steps):
        k = s["kind"]
        if k == "llm":
            inp, out = _split_turn(s)
            a = s.get("agent")
            ctx = (s.get("input_tokens") or 0) + (s.get("cache_read") or 0) + (s.get("cache_write") or 0)
            denom = max(float(ctx), E[a])
            per_token = inp / denom if denom else 0.0
            F[a].append(F[a][-1] + per_token)
            phase_cost["context"] += inp - per_token * E[a] if denom else inp
            issued = []
            for s2 in steps[i + 1:]:
                if s2["kind"] == "tool":
                    issued.append(s2)
                elif s2["kind"] == "llm":
                    break
            if issued:                         # only tool steps from the same message
                mid = issued[0].get("llm_msg")
                issued = [x for x in issued if x.get("llm_msg") == mid]
            if issued:
                for x in issued:
                    x["_issue_cost"] = out / len(issued)
            else:
                phase_cost["respond"] += out
        elif k == "tool":
            a = carrier(s.get("agent"))
            e = 0.0 if s.get("denied") else (s.get("output_chars") or 0) / CHARS_PER_TOKEN
            if e:
                open_items[a].append((s, e, len(F[a]) - 1))
                E[a] += e
        elif k == "notice" and s.get("name") == "compaction":
            for a in list(open_items):
                close(a)
    for a in list(open_items):
        close(a)
    for s in steps:
        if s["kind"] == "tool":
            s["attributed_cost"] = s.pop("_issue_cost", 0.0) + carried.get(id(s), 0.0)
            phase_cost[s["phase"]] += s["attributed_cost"]
    return phase_cost


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
        "duration_s": _active_seconds(steps),
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

    # --- cost attribution: each tool call's cost is writing it plus carrying its result in later turns
    phase_calls = Counter(s["phase"] for s in tools)
    phase_cost = attribute(steps)
    t["phase_calls"] = dict(phase_calls)
    t["phase_cost"] = {k: round(v, 5) for k, v in phase_cost.items() if v}
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
    hits = [s["tripwire"] for s in steps if s.get("tripwire")]
    t["tripwires"] = len(hits)
    t["tripwire_what"] = ", ".join(sorted(set(hits)))[:300] or None
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
    t["agents"] = agent_evidence(steps)


def agent_evidence(steps):
    """What each agent (Aegis grant name) did in a task, for its trust score (trust.py): governed tool calls,
    how many were refused, the longest run of refusals (the same two counters as `repeated_denials`), and
    tripwires touched, and which tools it misused (refused, or touching a tripwire) -- what a restriction
    takes away. A task where several agents act -- a root and the sub-agents it spawned -- says who did what,
    so one agent's probing is never another's."""
    per = {}
    for s in steps:
        a = s.get("agent")
        if not a:
            continue
        x = per.get(a)
        if x is None:
            x = per[a] = {"calls": 0, "denied": 0, "touches": 0, "streak": 0, "rules": {}, "_same": 0, "_any": 0,
                          "_last": None, "_misused": set()}
        if s.get("tripwire"):
            x["touches"] += 1
        if s["kind"] != "tool":
            continue
        x["calls"] += 1
        if (s.get("denied") or s.get("tripwire")) and s.get("name"):
            x["_misused"].add(s["name"])
        if s.get("denied"):
            x["denied"] += 1
            r = s.get("rule") or "unknown"          # by rule: one most agents hit is the policy's friction (trust.py)
            x["rules"][r] = x["rules"].get(r, 0) + 1
            x["_same"] = x["_same"] + 1 if s.get("name") == x["_last"] else 1
            x["_any"] += 1
            x["_last"] = s.get("name")
        else:
            x["_same"], x["_any"], x["_last"] = 0, 0, None
        x["streak"] = max(x["streak"], x["_same"], x["_any"])
    return {a: dict({k: v for k, v in x.items() if not k.startswith("_")}, misused=sorted(x["_misused"]),
                    rules=dict(sorted(x["rules"].items())))
            for a, x in sorted(per.items())}


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
    # what a recent baseline is segmented by (transient: finalize reads them, write_analysis doesn't keep them)
    by_model = Counter()
    for s in llm:
        if s.get("model") and s["model"] != "<synthetic>":
            by_model[s["model"]] += (s.get("cost") or 0) + 1e-12      # most spend; with none priced, most calls
    t["_model"] = min(by_model, key=lambda m: (-by_model[m], m)) if by_model else None
    t["_release"] = run.get("version") or run.get("policy_version")
    t["user_id"] = run.get("user_id")
    md = run.get("metadata") or {}
    t["tenant"] = md.get(run["_tenant_key"]) if run.get("_tenant_key") else (md.get("tenant") or md.get("tenant_id"))
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

def cost_rank(q, cost):
    """Where `cost` falls in a sample with quantiles `q` (0..1, interpolated): 0.9 is dearer than 90% of it."""
    if not q:
        return None
    if cost <= q[0]:
        return 0.0
    if cost >= q[-1]:
        return 1.0
    step = 1 / (len(q) - 1)
    for i in range(1, len(q)):
        if cost <= q[i]:
            lo, hi = q[i - 1], q[i]
            return round(step * (i - 1 + ((cost - lo) / (hi - lo) if hi > lo else 1.0)), 3)
    return 1.0


APDEX_DEFAULT = 1.5      # with no target set, T is 1.5x the baseline's median (cost, and agent time)


def apdex_targets(target, b):
    """(T for cost, T for agent time, basis): a target people set for this type wins; each dimension with none
    is 1.5x the task's baseline median. None: that dimension isn't judged."""
    target = target or {}
    med_cost, med_dur = b.get("cost_p50") or 0, b.get("duration_p50") or 0
    t_cost = target.get("cost") or (APDEX_DEFAULT * med_cost if med_cost else None)
    t_lat = target.get("latency_s") or (APDEX_DEFAULT * med_dur if med_dur else None)
    return t_cost, t_lat, "targets" if (target.get("cost") or target.get("latency_s")) else "baseline"


SCORE_WEIGHTS = {"efficiency": 0.25, "focus": 0.15, "reliability": 0.2, "verification": 0.15, "context": 0.1,
                 "autonomy": 0.15, "compliance": 0.15}


def components(t, ratio, outcome=True):
    """The seven process scores, 0-100 each, None where one doesn't apply. `ratio`: cost against the baseline.
    With outcome=False, without the terms that restate the outcome (failed: -40 on reliability, rework: -35 on
    autonomy): what calibrate.py predicts the outcome from, which it couldn't fairly do from the outcome itself."""
    s = {}
    waste_share = t["waste_cost"] / t["cost"] if t["cost"] else 0
    s["efficiency"] = clamp(100 - 35 * math.log2(max(ratio, 1)) - 100 * waste_share)
    focus = 100 - 6 * t["redundant_reads"] - 10 * t["duplicate_calls"] - 4 * max(0, t["max_edits_one_file"] - 4)
    if t["edits"] and t["explore_ratio"] > 0.75:
        focus -= 15
    focus -= 8 * max(0, (t.get("max_node_visits") or 0) - 3) + 10 * (t.get("pingpong") or 0)
    s["focus"] = clamp(focus) if (t["tool_calls"] or t.get("nodes")) else None
    llm_pen = 8 * min(t.get("llm_errors") or 0, 5) + 6 * min(t.get("truncations") or 0, 5) + 10 * min(t.get("refusals") or 0, 3)
    llm_pen += 40 if outcome and t.get("outcome") == "failed" else 0
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
    s["autonomy"] = clamp(100 - 45 * min(t["interrupts"], 2) - (35 if outcome and t.get("outcome") == "rework" else 0))
    if t.get("governed"):
        s["compliance"] = clamp(100 - 12 * (t.get("policy_denials") or 0) - 20 * max(0, (t.get("repeated_denials") or 0) - 1)
                                - 40 * (t.get("revocations") or 0) - 15 * (t.get("budget_denials") or 0))
    else:
        s["compliance"] = None
    return s


def overall(s, weights=None):
    """The weighted mean of the scores that apply (SCORE_WEIGHTS, or fitted ones -- calibrate.py)."""
    weights = weights or SCORE_WEIGHTS
    tot = sum(weights.get(k, 0) for k, v in s.items() if v is not None)
    return round(sum(weights.get(k, 0) * v for k, v in s.items() if v is not None) / tot, 1) if tot else None


def score_task(t, b, target=None, weights=None):
    """Scores, Apdex and the comparison with `b`, the baseline chosen for this task; `target`, the Apdex target
    set for its type ({"latency_s", "cost"}), if any; `weights`, fitted score weights in use, if any."""
    b = b or {}
    med_cost = b.get("cost_p50") or 0
    ratio = (t["cost"] + t["subagent_cost"]) / med_cost if med_cost else 1
    t["cost_vs_baseline"] = round(ratio, 2)
    med_dur = b.get("duration_p50") or 0
    t["duration_vs_baseline"] = round(t["duration_s"] / med_dur, 2) if med_dur else 1
    s = components(t, ratio)
    s["overall"] = overall(s, weights)
    t["scores"] = {k: (round(v, 1) if v is not None else None) for k, v in s.items()}
    t["score"] = t["scores"]["overall"]

    # Agent Apdex: the outcome first, then the worse of cost and agent time against T (satisfied up to T,
    # tolerating up to 4T). T is what people set for the type, else 1.5x the baseline's median.
    t_cost, t_lat, t["apdex_basis"] = apdex_targets(target, b)
    level = max((0 if T is None or v <= T else 1 if v <= 4 * T else 2)
                for v, T in ((t["cost"] + t["subagent_cost"], t_cost), (t["duration_s"], t_lat)))
    if t["outcome"] in ("interrupted", "rework", "failed") or t["max_error_streak"] >= 4:
        t["apdex"] = "frustrated"
    else:
        t["apdex"] = ("satisfied", "tolerating", "frustrated")[level]


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
    {"id": "tripwire", "name": "Tripwire touched", "metric": "tripwires", "op": ">=", "value": 1, "severity": "critical",
     "message": "Touched {tripwire_what}: a decoy no legitimate agent uses"},
    {"id": "budget_stop", "name": "Budget stop", "metric": "budget_denials", "op": ">=", "value": 1, "severity": "warning",
     "message": "Budget limit reached {v} time(s); work was stopped"},
    {"id": "slow_ttft", "name": "Slow first token", "metric": "ttft_ms", "op": ">", "value": 8000, "severity": "info",
     "message": "Median time to first token {v:,.0f} ms"},
]

OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
       "==": lambda a, b: a == b}


def evaluate_rules(t, rules, redact=None):
    """Health-rule events for one task. `redact` (privacy.Redactor.text) is applied to the task's text
    fields before a message can quote them: a message is stored and sent to alert destinations, and must
    not carry what redaction or `store_content = false` keeps out of the task row."""
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
            if redact is not None:
                for f in TASK_TEXT_FIELDS:
                    if isinstance(ctx.get(f), str):
                        ctx[f] = redact(ctx[f])
            ctx["next_prompt"] = (ctx.get("next_prompt") or "")[:80]
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


# A task that ended this recently, in a run not yet complete, is "in progress"; after that it settles.
IN_PROGRESS_S = 600


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
        elif nxt is None and t["source"] == "claude-code" and t["ended"] and now - t["ended"] < IN_PROGRESS_S:
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
        t["outcome"] = "in progress" if t["ended"] and now - t["ended"] < IN_PROGRESS_S else "unknown"
    else:
        t["outcome"] = "completed"
    t["root_error"] = run.get("root_error")


def keyed_matches(runs, tasks_by_run, keyed):
    """task id -> the outcome stated for it by key (store.set_keyed_grade): runs whose metadata holds key=value,
    their latest top-level task (or all of them), within the stating key's projects. The newest statement wins."""
    out = {}
    if not keyed:
        return out
    want = defaultdict(list)
    for g in keyed:
        want[(g["key"], str(g["value"]))].append(g)
    hits = defaultdict(list)
    for run in runs:
        if run.get("is_subagent"):
            continue
        for k, v in (run.get("metadata") or {}).items():
            for i, g in enumerate(want.get((k, str(v)), ())):
                for t in tasks_by_run.get(run["id"], ()):
                    if g.get("projects") is None or t["project"] in g["projects"]:
                        hits[(k, str(v), i)].append(t)
    for (k, v, i), ts in hits.items():
        g = want[(k, v)][i]
        chosen = ts if g.get("match") == "all" else [max(ts, key=lambda t: (t["started"] or 0, t["id"]))]
        for t in chosen:
            prev = out.get(t["id"])
            if prev is None or (g["ts"] or 0, g["key"], g["value"]) > (prev["ts"] or 0, prev["key"], prev["value"]):
                out[t["id"]] = g
    return out


def kappa(pairs):
    """Cohen's kappa of (a, b) labels: agreement beyond chance. None when it can't say (no pairs, or every label
    the same, where chance agreement is total)."""
    n = len(pairs)
    if not n:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] * cb[c] for c in ca) / (n * n)
    return None if pe >= 1 else round((po - pe) / (1 - pe), 3)


def grade_agreement(tasks, model_grades):
    """The checker's grades against people's, on tasks with both: what would justify applying its grades."""
    pairs = [(model_grades[t["id"]]["outcome"], t["outcome"]) for t in tasks
             if t.get("outcome_source") == "graded" and t["id"] in model_grades
             and model_grades[t["id"]].get("outcome") in OUTCOMES]
    return {"pairs": len(pairs), "agreed": sum(1 for a, b in pairs if a == b), "kappa": kappa(pairs)}


def promoted(agreement, promote):
    """Whether the checker's grades are applied: switched on, on enough pairs, and agreeing well enough."""
    return bool(promote) and agreement["pairs"] >= promote["min_pairs"] and (agreement["kappa"] or 0) >= promote["min_kappa"]


def _apply_grades(tasks, grades, keyed=None, model_grades=None, promote=None):
    """Settle each outcome by the strongest evidence available, and record which one it was.

    stated after the fact (API, by task id)  >  stated by your own key (a ticket reopened, a refund reversed)
    >  stated in the run (agentdynamics.outcome)  >  recorded feedback  >  the checker's grade, once it has
    earned it (`promote`: on, and agreeing with people's grades to `min_kappa` over `min_pairs`)  >  inference.

    Inference is a guess built from errors, interrupts and corrections. Apdex, success rate and the
    process score all inherit it, so anything that states the outcome outright must win, and the
    console has to be able to say how much of a success rate is guessed. Idempotent: the cached
    per-run tasks are re-finalized on every refresh, so nothing here may consume its input.
    """
    keyed = keyed or {}
    for t in tasks:
        api, sdk, by_key = grades.get(t["id"]), t.get("_sdk_grade"), keyed.get(t["id"])
        if api:
            t["outcome"], t["outcome_source"] = api["outcome"], "graded"
            t["outcome_reason"] = api.get("reason") or f"graded by {api.get('graded_by') or 'api'}"
        elif by_key:
            t["outcome"], t["outcome_source"] = by_key["outcome"], "graded"
            t["outcome_reason"] = (f"{by_key['key']}={by_key['value']}: " + (by_key.get("reason") or
                                   f"graded by {by_key.get('graded_by') or 'api'}"))[:500]
        elif sdk:
            t["outcome"], t["outcome_source"] = sdk["outcome"], "graded"
            t["outcome_reason"] = sdk.get("reason") or "graded in the run"
        elif t.get("feedback_score") is not None:
            ok = t["feedback_score"] >= FEEDBACK_PASS
            t["outcome"] = "completed" if ok else "rework"
            t["outcome_source"], t["outcome_reason"] = "feedback", f"feedback score {t['feedback_score']:g}"
        else:
            t["outcome_source"], t["outcome_reason"] = "inferred", None
    if model_grades and promoted(grade_agreement(tasks, model_grades), promote):
        for t in tasks:
            mg = model_grades.get(t["id"])
            if (mg and mg.get("outcome") in OUTCOMES and t["outcome_source"] == "inferred"
                    and t.get("outcome") != "in progress"):
                t["outcome"], t["outcome_source"] = mg["outcome"], "model"
                t["outcome_reason"] = f"graded by {mg.get('model') or 'the checker'}: {mg.get('reason') or ''}"[:500]


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

    sig[task_id]     the settled fields plus the baseline scoring reads
    events[task_id]  the health events that signature produced
    changed          task ids scored this time (their rows must be written)
    baselines[group] the last sample of a baseline group (size, ids, last key) and the figures from it
    subagent_cost    task id -> the subagent cost it had last time (non-zero only)
    spawns[run_id]   the (subagent id, task id) pairs a run's tool steps spawned
    windows[(k, d)]  a recent baseline: segment k's figures over the BASELINE_DAYS before day d (or None)
    slots[task_id]   what a task puts into windows (segments, day, the figures' inputs), to see it change
    """

    def __init__(self):
        self.sig, self.events, self.changed = {}, {}, set()
        self.baselines, self.subagent_cost, self.spawns = {}, {}, {}
        self.windows, self.slots = {}, {}


BASELINE_EXACT_UP_TO = 20      # below this many tasks, a type's baseline uses all of them
BASELINE_GROWTH = 1.05          # above it, only when the count has grown 5% since the last step
BASELINE_DAYS = 14              # a recent baseline: the tasks like this one in the 14 days before its day
BASELINE_MIN = 10               # fewest tasks a recent baseline needs; with fewer, a broader one is used
BASELINE_MAX = 500              # at most the latest 500 of them, so a busy segment costs no more
QUANTILES = [i / 20 for i in range(21)]
DAY = 86400


def _figures(g):
    costs = sorted(t["cost"] + t["subagent_cost"] for t in g)
    durs = [t["duration_s"] for t in g]
    return {
        "sample": len(g),
        "cost_p50": pct(costs, 0.5), "cost_p90": pct(costs, 0.9),
        "duration_p50": pct(durs, 0.5), "duration_p90": pct(durs, 0.9),
        "tokens_p50": pct([t["total_tokens"] for t in g], 0.5),
        "tool_calls_p50": pct([t["tool_calls"] for t in g], 0.5),
        "steps_p50": pct([t["steps_total"] for t in g], 0.5),
        "cost_q": [round(pct(costs, q), 7) for q in QUANTILES] if costs else [],
    }


def segments(t):
    """The recent baselines a task can be compared with, most specific first: the same type, model and release
    (an app's `version`, else its Aegis policy version), the same type and model, the same type."""
    if t["is_subagent"]:
        return (("subagent", t["task_type"]), ("subagent",))
    ty, m, r = t["task_type"], t.get("_model"), t.get("_release")
    keys = [(ty, m, r)] if r else []
    return tuple(keys + ([(ty, m)] if m else []) + [(ty,)])


def segment_label(k):
    if k[0] == "subagent":
        return f"sub-agent {k[1]}" if len(k) > 1 else "sub-agents"
    if k == ("__all__",):
        return "all tasks"
    return " · ".join(str(x) for x in k)


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


def finalize(runs, tasks_by_run, rules=None, now=None, grades=None, cache=None, dirty=(), redact=None, targets=None,
             keyed=None, model_grades=None, promote=None, weights=None):
    """Cross-run analysis: outcomes, subagent roll-up, baselines, scores, events. `targets`: Apdex targets per task
    type, {type: {"latency_s", "cost"}}.

    With a `cache` (a ScoreCache kept between calls), tasks from runs not in `dirty` whose settled
    fields and baseline are unchanged keep their previous score and events instead of being re-scored;
    `cache.changed` then lists the task ids that were. The result is the same as without a cache.
    """
    rules = rules or DEFAULT_RULES
    now = now or time.time()
    dirty = set(dirty)
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
    _apply_grades(all_tasks, grades or {}, keyed_matches(runs, tasks_by_run, keyed), model_grades, promote)

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
        # a run's spawns change only with the run, so they are read from its steps once, not every pass
        spawns = cache.spawns.get(run["id"]) if cache is not None and run["id"] not in dirty else None
        if spawns is None:
            spawns = [(s["subagent_id"], s.get("task_id")) for s in run["steps"]
                      if s["kind"] == "tool" and s.get("subagent_id")]
            if cache is not None:
                cache.spawns[run["id"]] = spawns
        spawn_map.update(spawns)
    if cache is not None and len(cache.spawns) > len(runs):
        held = {r["id"] for r in runs}
        cache.spawns = {k: v for k, v in cache.spawns.items() if k in held}
    child_costs = defaultdict(list)
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
                child_costs[pt["id"]].append(t["cost"])
                pt["subagents"] += 1
    # summed exactly (fsum), so the total doesn't depend on the order runs happen to be held in: an incremental
    # refresh and a full rebuild hold them in different orders, and a last-bit difference showed in a baseline
    for tid, costs in child_costs.items():
        task_by_id[tid]["subagent_cost"] = math.fsum(costs)

    # baselines per task type (top-level tasks only)
    main = [t for t in all_tasks if not t["is_subagent"] and t["llm_calls"] > 0]
    groups = defaultdict(list)
    for t in main:
        groups[t["task_type"]].append(t)
    groups["__all__"] = main
    sub = [t for t in all_tasks if t["is_subagent"] and t["llm_calls"] > 0]
    if sub:
        groups["subagent"] = sub
    # A baseline is re-computed only when its sample could have changed: its size stepped, a task in it
    # changed or went, or a changed task sorts into it. New traffic is almost always later than a type's
    # earliest tasks, so this usually skips the sort -- which was 40% of a refresh at 20k tasks.
    def order(t):
        return (t["started"] or 0, t["id"])
    live = {t["id"] for t in all_tasks}
    touched = earliest = None
    if cache is not None:
        subs = {t["id"]: t["subagent_cost"] for t in all_tasks if t["subagent_cost"]}
        touched = {t["id"] for rid in dirty for t in tasks_by_run.get(rid, ())}
        touched |= {tid for tid in cache.subagent_cost.keys() | subs.keys() if cache.subagent_cost.get(tid) != subs.get(tid)}
        touched |= {tid for tid in cache.sig if tid not in live}          # tasks that went
        cache.subagent_cost = subs
        earliest = {}                  # group -> the earliest touched task in it
        for tid in touched:
            t = task_by_id.get(tid)
            if t is None or t["llm_calls"] <= 0:
                continue
            for k in ((t["task_type"], "__all__") if not t["is_subagent"] else ("subagent",)):
                if k not in earliest or order(t) < earliest[k]:
                    earliest[k] = order(t)
    baselines = {}
    for k, g in groups.items():
        if len(g) < 3 and k != "__all__":
            continue
        n, m = len(g), baseline_sample_size(len(g))
        prev = cache.baselines.get(k) if cache is not None else None
        if (prev is not None and prev["m"] == m and not (prev["ids"] & touched)
                and not (k in earliest and earliest[k] < prev["last"])):
            baselines[k] = {"n": n, **prev["figures"]}
            continue
        g = sorted(g, key=order)[:m]
        figures = _figures(g)
        baselines[k] = {"n": n, **figures}
        if cache is not None:
            cache.baselines[k] = {"m": m, "ids": {t["id"] for t in g}, "last": order(g[-1]) if g else (0, ""),
                                  "figures": figures}
    if cache is not None:
        cache.baselines = {k: v for k, v in cache.baselines.items() if k in baselines}

    # recent baselines: per segment and day, the BASELINE_DAYS before it. A window depends only on the tasks in
    # it, so it is kept until one of them changes -- new traffic today moves no window anyone is scored against.
    buckets = defaultdict(lambda: defaultdict(list))        # segment -> day -> tasks
    slots = {}
    for t in main + sub:
        d = int((t["started"] or 0) // DAY)
        keys = segments(t) + ((("__all__",),) if not t["is_subagent"] else ())
        for k in keys:
            buckets[k][d].append(t)
        slots[t["id"]] = (keys, d, t["cost"] + t["subagent_cost"], t["duration_s"], t["total_tokens"],
                          t["tool_calls"], t["steps_total"], order(t))
    windows = cache.windows if cache is not None else {}
    if cache is not None:
        stale = set()
        for tid, slot in slots.items():
            old = cache.slots.get(tid)
            if old != slot:
                for s_ in (old, slot):
                    if s_ is not None:
                        stale.update((k, s_[1] + i) for k in s_[0] for i in range(1, BASELINE_DAYS + 1))
        for tid, old in cache.slots.items():
            if tid not in slots:
                stale.update((k, old[1] + i) for k in old[0] for i in range(1, BASELINE_DAYS + 1))
        for kd in stale:
            windows.pop(kd, None)
        cache.slots = slots

    def window(k, d):
        if (k, d) not in windows:
            g = [t for dd in range(d - BASELINE_DAYS, d) for t in buckets[k].get(dd, ())]
            if len(g) > BASELINE_MAX:
                g = sorted(g, key=order)[-BASELINE_MAX:]
            windows[(k, d)] = _figures(g) if len(g) >= BASELINE_MIN else None
        return windows[(k, d)]

    def choose(t):
        """The baseline a task is compared with, most specific first, with what it is."""
        d = int((t["started"] or 0) // DAY)
        for k in segments(t):
            f = window(k, d)
            if f:
                return f, f"{segment_label(k)}, last {BASELINE_DAYS} days"
        hist = "subagent" if t["is_subagent"] else t["task_type"]
        if baselines.get(hist):
            return baselines[hist], f"{segment_label(('subagent',)) if t['is_subagent'] else hist}, all history"
        f = window(("__all__",), d)
        if f:
            return f, f"all tasks, last {BASELINE_DAYS} days"
        return baselines.get("__all__"), "all tasks, all history"

    events = []
    if cache is not None:
        cache.changed = set()
        for tid in [k for k in cache.sig if k not in live]:     # tasks that no longer exist
            cache.sig.pop(tid, None)
            cache.events.pop(tid, None)
    targets = targets or {}
    for t in all_tasks:
        b, basis = choose(t)
        b = b or {}
        target = targets.get(t["task_type"])
        if cache is not None:
            sig = (_settled_values(t), tuple(sorted((target or {}).items())), basis, b.get("sample"), b.get("cost_p50"), b.get("cost_p90"), b.get("duration_p50"),
                   b.get("duration_p90"), tuple(b.get("cost_q") or ()))
            if t["run_id"] not in dirty and cache.sig.get(t["id"]) == sig:
                events.extend(cache.events[t["id"]])            # nothing it depends on moved
                continue
        t["failed"] = 1 if t.get("outcome") == "failed" else 0
        if t["llm_calls"] == 0 and t["tool_calls"] == 0:
            t.update({"scores": {}, "score": None, "apdex": None, "cost_vs_baseline": None, "duration_vs_baseline": None,
                      "baseline": None, "apdex_basis": None})
            ev = []
        else:
            score_task(t, b, target, weights)
            t["baseline"] = {"basis": basis, "n": b.get("sample"),
                             **{k: b.get(k) for k in ("cost_p50", "cost_p90", "duration_p50", "duration_p90")},
                             "cost_rank": cost_rank(b.get("cost_q"), t["cost"] + t["subagent_cost"])} if b else None
            ev = evaluate_rules(t, rules, redact)
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
