"""SQLite storage (WAL mode).

Two kinds of tables:
  * durable  - spans_raw (pushed/pulled telemetry), source_state, alerts_sent, grades. These are a system of
               record. grades holds outcomes stated after the fact, so it must survive a schema change.
  * derived  - runs, steps, tasks, events, baselines, meta. Rebuildable from sources; dropped on schema change.

The storage layer is intentionally thin so it can be swapped for Postgres/ClickHouse at larger scale.
"""
import json
import sqlite3
import time

SCHEMA_VERSION = 8   # 6: outcome_source / outcome_reason (graded outcomes)
                     # 7: tokens_unverified (cache accounting that rests on a guess)
                     # 8: steps.governed (did this call go through an Aegis kernel)

RUN_COLS = ["id", "source", "project", "environment", "framework", "workflow", "cwd", "title", "agent_name", "parent_id",
            "parent_task_id", "is_subagent", "thread_id", "user_id", "tags", "root_status", "complete", "version", "git_branch",
            "entrypoint", "started", "ended", "file", "policy_version", "policy"]
TASK_COLS = ["id", "run_id", "idx", "project", "environment", "source", "framework", "workflow", "is_subagent", "parent_task_id",
             "prompt_kind", "prompt", "task_type", "started", "ended", "wall_s", "duration_s", "llm_calls", "tool_calls", "tool_errors",
             "tool_error_rate", "input_tokens", "output_tokens", "cache_read", "cache_write", "thinking_tokens", "total_tokens", "cost",
             "subagent_cost", "subagents", "max_context", "cache_hit", "models", "interrupts", "compactions", "api_errors",
             "parallelism", "files_read", "files_edited", "edits", "max_edits_one_file", "churn_file", "redundant_reads",
             "duplicate_calls", "max_error_streak", "large_outputs", "explore_ratio", "steps_to_first_edit", "code_changed",
             "verified", "unverified_edits", "waste_cost", "final_stop", "final_text", "ended_on_error", "next_prompt", "rework",
             "outcome", "cost_vs_baseline", "duration_vs_baseline", "score", "apdex",
             # how the outcome was decided: graded (stated), feedback (a recorded score), or inferred
             "outcome_source", "outcome_reason",
             # agent-flow metrics
             "steps_total", "llm_errors", "truncations", "refusals", "rate_limited", "ttft_ms", "out_tps", "retrievals",
             "empty_retrievals", "nodes", "max_node_visits", "loop_node", "handoffs", "pingpong", "hitl", "feedback_score",
             "unpriced", "tokens_unverified", "critical_node", "critical_share", "root_error",
             # governance (Aegis)
             "governed", "policy_version", "policy_denials", "spend_denials", "budget_denials", "repeated_denials",
             "revocations", "blocked_cost"]
TASK_JSON = ["phase_calls", "phase_cost", "scores", "path", "denied_rules"]
STEP_COLS = ["run_id", "seq", "task_id", "kind", "name", "model", "phase", "target", "ts", "start_ts", "end_ts",
             "duration_ms", "cost", "attributed_cost", "input_tokens", "output_tokens", "cache_read", "cache_write",
             "context_tokens", "thinking_tokens", "is_error", "output_chars", "text", "input_preview", "error",
             "subagent_id", "stop_reason", "tool_calls", "effort",
             "span_id", "parent_span_id", "depth", "node", "agent", "span_kind", "ttft_ms", "docs", "hitl", "rate_limited",
             "denied", "rule", "guard", "grant_depth", "args_json", "governed"]
EVENT_COLS = ["id", "ts", "rule_id", "rule", "severity", "task_id", "run_id", "project", "task_type", "message", "value"]

DERIVED = ["runs", "tasks", "steps", "events", "baselines", "meta"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs ({", ".join(RUN_COLS)}, PRIMARY KEY(id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tasks ({", ".join(TASK_COLS + TASK_JSON)}, PRIMARY KEY(id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS steps ({", ".join(STEP_COLS)}, flags);
CREATE TABLE IF NOT EXISTS events ({", ".join(EVENT_COLS)});
CREATE TABLE IF NOT EXISTS baselines (task_type PRIMARY KEY, data);
CREATE TABLE IF NOT EXISTS meta (k PRIMARY KEY, v);
CREATE INDEX IF NOT EXISTS steps_task ON steps(task_id);
CREATE INDEX IF NOT EXISTS steps_run ON steps(run_id);
CREATE INDEX IF NOT EXISTS steps_node ON steps(node);
CREATE INDEX IF NOT EXISTS tasks_run ON tasks(run_id);
CREATE INDEX IF NOT EXISTS tasks_started ON tasks(started);

CREATE TABLE IF NOT EXISTS spans_raw (source, trace_id, span_id, fmt, doc, updated REAL, PRIMARY KEY(source, span_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS spans_trace ON spans_raw(source, trace_id);
CREATE INDEX IF NOT EXISTS spans_updated ON spans_raw(updated);
CREATE TABLE IF NOT EXISTS source_state (name PRIMARY KEY, data);
CREATE TABLE IF NOT EXISTS alerts_sent (event_id PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS grades (task_id PRIMARY KEY, outcome, reason, graded_by, ts REAL);
"""


def connect(path):
    con = sqlite3.connect(path, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    if ver != SCHEMA_VERSION:
        for t in DERIVED:
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    con.executescript(SCHEMA)
    return con


def connect_reader(path):
    con = sqlite3.connect(path, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _ins(con, table, cols, rows_, verb="INSERT"):
    q = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    con.executemany(q, rows_)


def _redact_step(s, red):
    if red is None:
        return s
    s = dict(s)
    for f in ("text", "input_preview", "error"):
        if s.get(f):
            s[f] = red.text(s[f])
    if s.get("target") and red.rx is not None:
        s["target"] = red.rx.sub("[REDACTED]", s["target"]) if red.store_content else red.text(s["target"])
    return s


def write_runs(con, runs, removed_ids=(), red=None):
    """Replace runs + steps for the given runs (incremental)."""
    ids = [r["id"] for r in runs] + list(removed_ids)
    with con:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            con.execute(f"DELETE FROM steps WHERE run_id IN ({ph})", chunk)
            con.execute(f"DELETE FROM runs WHERE id IN ({ph})", chunk)
        rrows = []
        for r in runs:
            row = [r.get(c) for c in RUN_COLS]
            row[RUN_COLS.index("tags")] = json.dumps(r.get("tags") or [])
            row[RUN_COLS.index("policy")] = json.dumps(r["policy"]) if r.get("policy") else None
            row[RUN_COLS.index("title")] = red.text(r.get("title")) if red else r.get("title")
            rrows.append(row)
        _ins(con, "runs", RUN_COLS, rrows)
        srows = []
        for r in runs:
            for s in r["steps"]:
                s2 = _redact_step(s, red)
                row = [r["id"] if c == "run_id" else s2.get(c) for c in STEP_COLS]
                for b in ("is_error", "hitl", "rate_limited", "denied"):
                    row[STEP_COLS.index(b)] = 1 if s2.get(b) else 0
                srows.append(row + [json.dumps(s2.get("flags") or [])])
        _ins(con, "steps", STEP_COLS + ["flags"], srows)


def update_step_analysis(con, runs):
    """Persist analysis-time step fields (task id, flags, attributed cost) for the given runs."""
    rows_ = [(s.get("task_id"), json.dumps(s.get("flags") or []), s.get("attributed_cost"), r["id"], s["seq"])
             for r in runs for s in r["steps"]]
    with con:
        con.executemany("UPDATE steps SET task_id=?, flags=?, attributed_cost=? WHERE run_id=? AND seq=?", rows_)


def write_analysis(con, tasks, baselines, events, meta, red=None):
    with con:
        for t in ("tasks", "events", "baselines", "meta"):
            con.execute(f"DELETE FROM {t}")
        trows = []
        for t in tasks:
            row = [t.get(c) for c in TASK_COLS] + [json.dumps(t.get(c) if t.get(c) is not None else ([] if c == "path" else {}))
                                                   for c in TASK_JSON]
            if red:
                for f in ("prompt", "final_text", "next_prompt", "root_error"):
                    i = TASK_COLS.index(f)
                    row[i] = red.text(row[i])
            trows.append(row)
        _ins(con, "tasks", TASK_COLS + TASK_JSON, trows)
        erows = []
        for e in events:
            row = [e.get(c) for c in EVENT_COLS]
            if red and red.rx is not None:  # messages can quote user text (e.g. the correcting follow-up)
                i = EVENT_COLS.index("message")
                row[i] = red.rx.sub("[REDACTED]", row[i] or "")
            erows.append(row)
        _ins(con, "events", EVENT_COLS, erows)
        _ins(con, "baselines", ["task_type", "data"], [[k, json.dumps(v)] for k, v in baselines.items()])
        _ins(con, "meta", ["k", "v"], [[k, json.dumps(v)] for k, v in meta.items()])


# ---------------------------------------------------------------- durable span store

def upsert_spans(con, source, items):
    """items: list of (trace_id, span_id, fmt, doc_dict). Returns set of touched trace ids."""
    now = time.time()
    with con:
        con.executemany("INSERT INTO spans_raw (source, trace_id, span_id, fmt, doc, updated) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(source, span_id) DO UPDATE SET trace_id=excluded.trace_id, fmt=excluded.fmt, "
                        "doc=excluded.doc, updated=excluded.updated",
                        [(source, t, s, f, json.dumps(d, default=str), now) for t, s, f, d in items])
    return {t for t, _, _, _ in items}


def get_span_docs(con, source, span_ids):
    out = {}
    for i in range(0, len(span_ids), 500):
        chunk = span_ids[i:i + 500]
        for r in con.execute(f"SELECT span_id, doc FROM spans_raw WHERE source=? AND span_id IN ({','.join('?' * len(chunk))})",
                             [source] + chunk):
            out[r["span_id"]] = json.loads(r["doc"])
    return out


def trace_spans(con, source, trace_id):
    return [(r["fmt"], json.loads(r["doc"])) for r in con.execute(
        "SELECT fmt, doc FROM spans_raw WHERE source=? AND trace_id=?", (source, trace_id))]


def traces_updated_since(con, since):
    return [(r["source"], r["trace_id"]) for r in con.execute(
        "SELECT DISTINCT source, trace_id FROM spans_raw WHERE updated > ?", (since,))]


def all_traces(con):
    return [(r["source"], r["trace_id"]) for r in con.execute("SELECT DISTINCT source, trace_id FROM spans_raw")]


def purge_spans_before(con, cutoff):
    with con:
        return con.execute("DELETE FROM spans_raw WHERE updated < ?", (cutoff,)).rowcount


def get_state(con, name):
    r = con.execute("SELECT data FROM source_state WHERE name=?", (name,)).fetchone()
    return json.loads(r["data"]) if r else {}


def set_state(con, name, data):
    with con:
        con.execute("INSERT OR REPLACE INTO source_state (name, data) VALUES (?, ?)", (name, json.dumps(data)))


# ---------------------------------------------------------------- durable outcome grades

OUTCOMES = ("completed", "failed", "rework", "interrupted")


def set_grade(con, task_id, outcome, reason=None, graded_by=None):
    """State a task's outcome. Keyed by task id, so it may arrive before the task is ingested and
    applies when it does (ingestion is order-independent). Re-grading replaces."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")
    with con:
        con.execute("INSERT OR REPLACE INTO grades (task_id, outcome, reason, graded_by, ts) VALUES (?, ?, ?, ?, ?)",
                    (task_id, outcome, (reason or None) and str(reason)[:500], graded_by, time.time()))


def delete_grade(con, task_id):
    with con:
        return con.execute("DELETE FROM grades WHERE task_id=?", (task_id,)).rowcount


def get_grades(con):
    return {r["task_id"]: dict(r) for r in con.execute("SELECT * FROM grades").fetchall()}


def rows(con, q, args=()):
    out = []
    for r in con.execute(q, args).fetchall():
        d = dict(r)
        for k in TASK_JSON + ["flags", "data", "tags", "policy"]:
            if k in d and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        out.append(d)
    return out
