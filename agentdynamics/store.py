"""SQLite storage (WAL mode).

Two kinds of tables:
  * durable  - spans_raw (pushed/pulled telemetry), source_state, alerts_sent, grades, alert_outbox,
               alert_state, rollup_daily, revocations. These are a system of record. grades holds outcomes stated after
               the fact, so it must survive a schema change; the alert tables hold what was promised to a
               pager; rollup_daily holds the only copy of days that retention has purged.
  * derived  - runs, steps, tasks, events, baselines, meta. Rebuildable from sources; dropped on schema change.

Where it lives: a SQLite file in the data directory (the default, and the zero-dependency path), or a
Postgres schema when [store] url is set (pg.py adapts the same queries). `target()` says which.
"""
import json
import os
import sqlite3
import time

from .privacy import TASK_TEXT_FIELDS

SCHEMA_VERSION = 10   # 6: outcome_source / outcome_reason (graded outcomes)
                     # 7: tokens_unverified (cache accounting that rests on a guess)
                     # 8: steps.governed (did this call go through an Aegis kernel)
                     # 9: task_type_source / task_type_match (how a task type was decided)
                     # 10: tasks.tripwires / tripwire_what, steps.tripwire (decoys touched)

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
             # how the task type was decided: workflow, prompt kind, follow-up, keywords, unmatched
             "task_type_source", "task_type_match",
             # agent-flow metrics
             "steps_total", "llm_errors", "truncations", "refusals", "rate_limited", "ttft_ms", "out_tps", "retrievals",
             "empty_retrievals", "nodes", "max_node_visits", "loop_node", "handoffs", "pingpong", "hitl", "feedback_score",
             "unpriced", "tokens_unverified", "critical_node", "critical_share", "root_error",
             # governance (Aegis)
             "governed", "policy_version", "policy_denials", "spend_denials", "budget_denials", "repeated_denials",
             "revocations", "blocked_cost", "tripwires", "tripwire_what"]
TASK_JSON = ["phase_calls", "phase_cost", "scores", "path", "denied_rules"]
STEP_COLS = ["run_id", "seq", "task_id", "kind", "name", "model", "phase", "target", "ts", "start_ts", "end_ts",
             "duration_ms", "cost", "attributed_cost", "input_tokens", "output_tokens", "cache_read", "cache_write",
             "context_tokens", "thinking_tokens", "is_error", "output_chars", "text", "input_preview", "error",
             "subagent_id", "stop_reason", "tool_calls", "effort",
             "span_id", "parent_span_id", "depth", "node", "agent", "span_kind", "ttft_ms", "docs", "hitl", "rate_limited",
             "denied", "rule", "guard", "grant_depth", "args_json", "governed", "tripwire"]
EVENT_COLS = ["id", "ts", "rule_id", "rule", "severity", "task_id", "run_id", "project", "task_type", "message", "value"]

DERIVED = ["runs", "tasks", "steps", "events", "baselines", "meta"]

# Daily totals of tasks, kept after retention purges the tasks themselves. One row per local day and
# combination of these dimensions; every measure is a sum, so rows add up across any grouping.
ROLLUP_DIMS = ["day", "project", "environment", "framework", "source", "workflow", "task_type", "outcome", "is_subagent"]
ROLLUP_SUMS = ["tasks", "cost", "subagent_cost", "waste_cost", "total_tokens", "input_tokens", "output_tokens",
               "cache_read", "cache_write", "llm_calls", "tool_calls", "tool_errors", "duration_s", "wall_s",
               "score_sum", "score_n", "apdex_satisfied", "apdex_tolerating", "apdex_frustrated", "code_changed",
               "verified", "max_context"]
_ROLLUP_SELECT = ("COUNT(*), SUM(cost), SUM(subagent_cost), SUM(waste_cost), SUM(total_tokens), SUM(input_tokens), "
                  "SUM(output_tokens), SUM(cache_read), SUM(cache_write), SUM(llm_calls), SUM(tool_calls), "
                  "SUM(tool_errors), SUM(duration_s), SUM(wall_s), SUM(score), COUNT(score), "
                  "SUM(CASE WHEN apdex = 'satisfied' THEN 1 ELSE 0 END), "
                  "SUM(CASE WHEN apdex = 'tolerating' THEN 1 ELSE 0 END), "
                  "SUM(CASE WHEN apdex = 'frustrated' THEN 1 ELSE 0 END), SUM(CASE WHEN code_changed = 1 THEN 1 ELSE 0 END), "
                  "SUM(CASE WHEN code_changed = 1 THEN verified ELSE 0 END), SUM(max_context)")

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
CREATE INDEX IF NOT EXISTS events_task ON events(task_id);

CREATE TABLE IF NOT EXISTS spans_raw (source, trace_id, span_id, fmt, doc, updated REAL, PRIMARY KEY(source, span_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS spans_trace ON spans_raw(source, trace_id);
CREATE INDEX IF NOT EXISTS spans_updated ON spans_raw(updated);
CREATE TABLE IF NOT EXISTS source_state (name PRIMARY KEY, data);
CREATE TABLE IF NOT EXISTS alerts_sent (event_id PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS grades (task_id PRIMARY KEY, outcome, reason, graded_by, ts REAL);
CREATE TABLE IF NOT EXISTS alert_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, dest, body, created REAL,
                                         attempts INTEGER DEFAULT 0, next_try REAL, last_error);
CREATE TABLE IF NOT EXISTS alert_state (key PRIMARY KEY, since REAL, data);
CREATE TABLE IF NOT EXISTS revocations (id PRIMARY KEY, project, agent, reason, source, created REAL, expires REAL,
                                        cleared REAL);
CREATE TABLE IF NOT EXISTS rollup_daily ({", ".join(ROLLUP_DIMS + ROLLUP_SUMS)}, PRIMARY KEY({", ".join(ROLLUP_DIMS)})) WITHOUT ROWID;
"""


def target(cfg, data_dir):
    """(where the store is, its Postgres schema or None): the [store] url if set, else SQLite in data_dir."""
    st = cfg.get("store") or {}
    if st.get("url"):
        from .pg import schema_name
        return st["url"], schema_name(st.get("schema") or "agentdynamics", data_dir)
    return os.path.join(data_dir, "agentdynamics.db"), None


def is_postgres(where):
    return isinstance(where, str) and where.split(":", 1)[0] in ("postgres", "postgresql")


def connect(path, schema=None):
    if is_postgres(path):
        from . import pg
        return pg.connect_store(path, schema, SCHEMA_VERSION, DERIVED)
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


def claim_writer(con, schema):
    """Whether this connection's engine may write the analysis. Always, for SQLite (one process per data
    directory). On a shared Postgres schema, the one instance holding a session advisory lock: it is released
    when that instance's connection closes, and the next claim takes it."""
    if schema is None:
        return True
    return bool(con.execute("SELECT pg_try_advisory_lock(hashtext(?))", (f"agentdynamics-writer:{schema}",)).fetchone()[0])


def mark_schema_stale(con):
    """Make the next connect treat the derived tables as another schema version's: dropped and rebuilt."""
    if isinstance(con, sqlite3.Connection):
        con.execute("PRAGMA user_version=0")
        con.commit()
    else:
        with con:
            con.execute("UPDATE schema_version SET version = -1")


# Tables a project-scoped reader sees through a filtering view. Anything a read endpoint can return about
# a task, run, step or event comes from one of these, so scoping them scopes every endpoint -- including
# ones written later -- instead of trusting each to remember a WHERE clause.
SCOPED_VIEWS = {
    "tasks": "SELECT * FROM main.tasks WHERE project IN ({p})",
    "runs": "SELECT * FROM main.runs WHERE project IN ({p})",
    "events": "SELECT * FROM main.events WHERE project IN ({p})",
    "steps": "SELECT * FROM main.steps WHERE run_id IN (SELECT id FROM main.runs WHERE project IN ({p}))",
    "rollup_daily": "SELECT * FROM main.rollup_daily WHERE project IN ({p})",
    # an install-wide directive (no project) applies to every project, so every scoped key sees it
    "revocations": "SELECT * FROM main.revocations WHERE project IN ({p}) OR project IS NULL",
}


def connect_reader(path, projects=None, schema=None):
    """A read-only connection. With `projects`, the connection sees only those projects' data: SQLite
    resolves an unqualified table name in the temp schema first, so a temp view named `tasks` stands in
    for the real table in every query on this connection. The views are created before query_only is
    switched on, after which the connection can write nothing at all. Postgres resolves unqualified names
    in the temp schema first too; there an unscoped reader comes from a pool (pg.reader)."""
    if is_postgres(path):
        from . import pg
        if projects is None:
            return pg.reader(path, schema)
        con = pg.connect(path, schema)
        lits = ",".join("'" + str(x).replace("'", "''") + "'" for x in projects) or "NULL"
        for name, q in SCOPED_VIEWS.items():
            con.execute(f"CREATE TEMP VIEW {name} AS " + q.format(p=lits))
        con.execute("SET default_transaction_read_only = on")
        return con
    con = sqlite3.connect(path, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    if projects is not None:
        lits = ",".join("'" + str(x).replace("'", "''") + "'" for x in projects) or "NULL"
        for name, q in SCOPED_VIEWS.items():
            con.execute(f"CREATE TEMP VIEW {name} AS " + q.format(p=lits))
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


def _task_row(t, red):
    row = [t.get(c) for c in TASK_COLS] + [json.dumps(t.get(c) if t.get(c) is not None else ([] if c == "path" else {}))
                                           for c in TASK_JSON]
    if red:
        for f in TASK_TEXT_FIELDS:
            i = TASK_COLS.index(f)
            row[i] = red.text(row[i])
    return row


def _event_row(e, red):
    row = [e.get(c) for c in EVENT_COLS]
    v = row[EVENT_COLS.index("value")]
    if not isinstance(v, (int, float)):          # a custom rule on a text field: the message says it
        row[EVENT_COLS.index("value")] = None
    if red and red.rx is not None:  # messages can quote user text (e.g. the correcting follow-up)
        i = EVENT_COLS.index("message")
        row[i] = red.rx.sub("[REDACTED]", row[i] or "")
    return row


def _write_small(con, baselines, meta):
    for t in ("baselines", "meta"):
        con.execute(f"DELETE FROM {t}")
    _ins(con, "baselines", ["task_type", "data"], [[k, json.dumps(v)] for k, v in baselines.items()])
    _ins(con, "meta", ["k", "v"], [[k, json.dumps(v)] for k, v in meta.items()])


def write_analysis(con, tasks, baselines, events, meta, red=None):
    """Replace every derived analysis row: after a full rebuild."""
    with con:
        for t in ("tasks", "events"):
            con.execute(f"DELETE FROM {t}")
        _ins(con, "tasks", TASK_COLS + TASK_JSON, [_task_row(t, red) for t in tasks])
        _ins(con, "events", EVENT_COLS, [_event_row(e, red) for e in events])
        _write_small(con, baselines, meta)


def write_analysis_delta(con, changed, removed_ids, events, baselines, meta, red=None):
    """Write only what an incremental refresh changed: the rows of `changed` tasks and their events,
    the removal of `removed_ids`, and the small baselines/meta tables. `events` must be exactly the
    events of the changed tasks. Rewriting every row cost 2.1 s of a 5.6 s refresh at 20k tasks."""
    ids = [t["id"] for t in changed] + list(removed_ids)
    with con:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            con.execute(f"DELETE FROM tasks WHERE id IN ({marks})", chunk)
            con.execute(f"DELETE FROM events INDEXED BY events_task WHERE task_id IN ({marks})", chunk)
        _ins(con, "tasks", TASK_COLS + TASK_JSON, [_task_row(t, red) for t in changed])
        _ins(con, "events", EVENT_COLS, [_event_row(e, red) for e in events])
        _write_small(con, baselines, meta)


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


# spans_raw is WITHOUT ROWID with PRIMARY KEY (source, span_id). With no ANALYZE statistics -- which
# is every fresh install -- SQLite's planner answers "source=? AND trace_id=?" by walking the primary
# key on source alone, visiting every span of that source for each trace: a full refresh was O(n^2),
# ~6 s at 1k traces and minutes at 10k. And "updated > ?" scanned the whole table on every incremental
# refresh. Naming the index makes the plan independent of statistics; both indexes are created on every
# connect, so INDEXED BY cannot fail. bench/bench.py measures the difference.
def trace_spans(con, source, trace_id):
    return [(r["fmt"], json.loads(r["doc"])) for r in con.execute(
        "SELECT fmt, doc FROM spans_raw INDEXED BY spans_trace WHERE source=? AND trace_id=?", (source, trace_id))]


def spans_for_traces(con, pairs):
    """{(source, trace_id): [(fmt, doc)]} for many traces: a query per 500 of them, not one each -- which on
    Postgres is a round trip each, and made a full rebuild 6x slower than SQLite's. Spans come in span id
    order within a trace, as SQLite's index returns them anyway."""
    by_source, out = {}, {}
    for source, tid in pairs:
        by_source.setdefault(source, []).append(tid)
    for source, tids in by_source.items():
        for i in range(0, len(tids), 500):
            chunk = tids[i:i + 500]
            for r in con.execute(f"SELECT trace_id, fmt, doc FROM spans_raw INDEXED BY spans_trace WHERE source=? "
                                 f"AND trace_id IN ({','.join('?' * len(chunk))}) ORDER BY trace_id, span_id",
                                 [source] + chunk):
                out.setdefault((source, r["trace_id"]), []).append((r["fmt"], json.loads(r["doc"])))
    return out


def traces_updated_since(con, since):
    return [(r["source"], r["trace_id"]) for r in con.execute(
        "SELECT DISTINCT source, trace_id FROM spans_raw INDEXED BY spans_updated WHERE updated > ?", (since,))]


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


# ---------------------------------------------------------------- alert delivery (durable)

def outbox_add(con, items, now):
    """Queue (dest_id, body) pairs for delivery, in order."""
    with con:
        con.executemany("INSERT INTO alert_outbox (dest, body, created, next_try) VALUES (?, ?, ?, ?)",
                        [(d, json.dumps(b, default=str), now, now) for d, b in items])


def outbox_pending(con, limit=500):
    return [dict(r) for r in con.execute("SELECT * FROM alert_outbox ORDER BY id LIMIT ?", (limit,))]


def outbox_done(con, row_id):
    with con:
        con.execute("DELETE FROM alert_outbox WHERE id = ?", (row_id,))


def outbox_retry(con, row_id, attempts, next_try, error):
    with con:
        con.execute("UPDATE alert_outbox SET attempts = ?, next_try = ?, last_error = ? WHERE id = ?",
                    (attempts, next_try, error, row_id))


def outbox_depth(con):
    return {r[0]: r[1] for r in con.execute("SELECT dest, COUNT(*) FROM alert_outbox GROUP BY dest")}


def alert_state(con):
    return {r["key"]: dict(json.loads(r["data"]), since=r["since"]) for r in con.execute("SELECT * FROM alert_state")}


def set_alert_state(con, firing, resolved, now):
    """Record SLO alerts that started (`firing`: {key: details}) and stopped (`resolved`: keys)."""
    with con:
        con.executemany("INSERT OR IGNORE INTO alert_state (key, since, data) VALUES (?, ?, ?)",
                        [(k, now, json.dumps(v, default=str)) for k, v in firing.items()])
        con.executemany("DELETE FROM alert_state WHERE key = ?", [(k,) for k in resolved])


# ---------------------------------------------------------------- daily rollups (durable)

def freeze_days(con, days, through, through_end):
    """Write the daily totals of `days` [(label, start, end)] from the tasks table, and record that every
    day up to `through` (ending at `through_end`) is frozen. One transaction: a day is rolled up exactly once."""
    dims = ", ".join(ROLLUP_DIMS[1:])
    with con:
        for label, start, end in days:
            con.execute(f"INSERT OR REPLACE INTO rollup_daily ({', '.join(ROLLUP_DIMS + ROLLUP_SUMS)}) "
                        f"SELECT ?, {dims}, {_ROLLUP_SELECT} FROM tasks INDEXED BY tasks_started "
                        f"WHERE started >= ? AND started < ? AND (llm_calls > 0 OR tool_calls > 0) GROUP BY {dims}",
                        (label, start, end))
        con.execute("INSERT OR REPLACE INTO source_state (name, data) VALUES ('rollups', ?)",
                    (json.dumps({"through": through, "through_end": through_end}),))


def rollup_boundary(con):
    """(last frozen day, the time it ends). Tasks from before that time are counted in rollup_daily, not tasks."""
    st = get_state(con, "rollups")
    return st.get("through"), st.get("through_end") or 0


# ---------------------------------------------------------------- revocation directives (durable)

def add_revocation(con, project, agent, reason, source, created, expires):
    """A directive to revoke the grants of `agent` (all of a process's grants when None) in `project` (every
    project when None) until `expires`. Returns its id. Applied by the in-process Aegis integration."""
    import uuid
    rid = uuid.uuid4().hex[:12]
    with con:
        con.execute("INSERT INTO revocations (id, project, agent, reason, source, created, expires, cleared) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)", (rid, project, agent, str(reason)[:500], source, created, expires))
    return rid


def clear_revocation(con, rid, now):
    """Stop a directive applying to new grants. Grants it already revoked stay revoked: Aegis revocation is
    permanent, and nothing here can loosen Aegis."""
    with con:
        return con.execute("UPDATE revocations SET cleared = ? WHERE id = ? AND cleared IS NULL", (now, rid)).rowcount


def revocations(con, now, active=False, project=None, limit=200):
    """Directives, newest first. `active`: not cleared and not expired. `project`: those for it or for all."""
    clauses, args = [], []
    if active:
        clauses.append("cleared IS NULL AND expires > ?")
        args.append(now)
    if project is not None:
        clauses.append("(project = ? OR project IS NULL)")
        args.append(project)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return [dict(r) for r in con.execute(f"SELECT * FROM revocations{where} ORDER BY created DESC, id LIMIT ?",
                                         args + [limit])]
