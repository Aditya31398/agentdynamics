"""The Postgres store backend: store.py's SQLite model, held in a Postgres schema.

store.py and the read API are written against sqlite3's connection interface and SQLite's SQL. Rather than
fork every query, a Postgres connection is adapted to that interface (PgConnection) and each statement is
translated once (translate): placeholders, INSERT OR REPLACE/IGNORE, INDEXED BY, scalar MAX, and the date
functions. Anything SQLite accepts that no translation can express (GROUP BY on a bare column, a boolean
summed as an integer) is written portably at the source instead.

    [store]
    url = "postgresql://agentdynamics@db.internal/agentdynamics"   # or AGENTDYNAMICS_DB_URL
    schema = "agentdynamics"                                        # or AGENTDYNAMICS_DB_SCHEMA

The driver is an optional dependency, imported only when a postgres URL is configured: psycopg 3
(`pip install "agentdynamics[postgres]"`), else psycopg2. Schema semantics match SQLite's: derived tables
are dropped and rebuilt when store.SCHEMA_VERSION changes, durable tables never are.

Types. SQLite columns are untyped; here each is TEXT or DOUBLE PRECISION, never an integer type, so no value
is ever silently rounded on insert. Whole numbers read back as int, as SQLite returns them for the columns
that only ever hold integers.
"""
import decimal
import hashlib
import os
import re
import threading
import time
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

NUM = "DOUBLE PRECISION"


def _driver():
    try:
        import psycopg
        return psycopg
    except ImportError:
        pass
    try:
        import psycopg2
        return psycopg2
    except ImportError:
        raise RuntimeError("a postgres store needs a driver: pip install \"agentdynamics[postgres]\" "
                           "(psycopg 3), or psycopg2") from None


def redact_url(url):
    """The URL without its password, for logs and the config page."""
    try:
        p = urlsplit(url)
        if p.password:
            host = p.hostname + (f":{p.port}" if p.port else "")
            return urlunsplit((p.scheme, f"{p.username}:***@{host}", p.path, p.query, ""))
    except ValueError:
        return "postgresql://***"
    return url


def schema_name(template, data_dir):
    """The schema for a data directory. "{data_dir}" in the template becomes a short hash of the directory,
    so several instances (or a test suite's many temporary stores) can share one database."""
    name = template.replace("{data_dir}", hashlib.sha1(os.path.abspath(data_dir).encode()).hexdigest()[:12])
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"store schema {name!r}: use lower-case letters, digits and _")
    return name


# ---------------------------------------------------------------- rows and connections

class Row:
    """sqlite3.Row's interface: by index or column name, keys(), iteration over values, dict(row)."""
    __slots__ = ("_names", "_index", "_values")

    def __init__(self, names, index, values):
        self._names, self._index, self._values = names, index, values

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, (int, slice)) else self._values[self._index[key]]

    def keys(self):
        return list(self._names)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        return tuple(self) == tuple(other)

    def __repr__(self):
        return f"Row({dict(zip(self._names, self._values))})"


def _value(v):
    # SUM and AVG over DOUBLE PRECISION give float, over COUNT's bigint give Decimal. SQLite gives int for a
    # whole number of an integer column, and callers (and JSON) expect that
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, float) and v.is_integer() and abs(v) < 2 ** 53:
        return int(v)
    return v


def _param(v):
    return int(v) if isinstance(v, bool) else v      # SQLite stores True as 1; a DOUBLE column wants a number


# an INSERT of one row of placeholders, possibly with an ON CONFLICT clause: batched as a multi-row INSERT
_VALUES = re.compile(r"(\s*INSERT INTO \w+ \([^)]*\) VALUES ?)(\((?:%s, ?)*%s\))(\s*(?:ON CONFLICT\b.*)?)$", re.S)


class PgCursor:
    def __init__(self, cur):
        self._cur = cur
        self.rowcount = cur.rowcount
        desc = cur.description
        self._names = [d[0] for d in desc] if desc else []
        self._index = {n: i for i, n in enumerate(self._names)}

    def _row(self, r):
        return Row(self._names, self._index, tuple(_value(v) for v in r))

    def fetchone(self):
        r = self._cur.fetchone() if self._names else None
        return None if r is None else self._row(r)

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()] if self._names else []

    def __iter__(self):
        return iter(self.fetchall())


class PgConnection:
    """What store.py uses of a sqlite3 connection: execute, executemany, `with con:` as a transaction, close.

    Autocommit, with `with con:` opening a transaction (nested ones join it) -- as sqlite3 behaves for the
    writes here, and without leaving a reader's snapshot open between requests."""

    def __init__(self, raw, schema, pool=None):
        self._raw, self.schema, self._pool = raw, schema, pool
        self._depth = 0
        self._lock = threading.RLock()

    def execute(self, sql, args=()):
        q = translate(sql, bool(args), self.schema)
        with self._lock:
            cur = self._raw.cursor()
            if args:
                cur.execute(q, [_param(a) for a in args])
            else:
                cur.execute(q)
            return PgCursor(cur)

    def executemany(self, sql, seq):
        # True -> 1 inline: this runs once per value of every row written (1.9M calls in a 5k-task rebuild)
        seq = [[int(a) if a.__class__ is bool else a for a in row] for row in seq]
        if not seq:
            return None
        q = translate(sql, True, self.schema)
        with self._lock:
            cur = self._raw.cursor()
            m = _VALUES.match(q)
            if m:
                # one INSERT of many rows, not many INSERTs: a full rebuild wrote 5k tasks' rows in 8 s as
                # batches of single-row statements, the server parsing each one
                target = re.match(r"\s*ON CONFLICT\s*\(([^)]*)\)\s*DO UPDATE", m.group(3))
                if target:
                    # one statement may not update a row twice, as successive upserts may: the last one wins
                    cols = [c.strip() for c in re.match(r"\s*INSERT INTO \w+ \(([^)]*)\)", q).group(1).split(",")]
                    at = [cols.index(k.strip()) for k in target.group(1).split(",")]
                    last = {}
                    for row in seq:
                        last[tuple(row[i] for i in at)] = row
                    seq = list(last.values())
                width = m.group(2).count("%s")
                per = max(1, min(500, 30000 // width))
                for i in range(0, len(seq), per):
                    chunk = seq[i:i + per]
                    cur.execute(m.group(1) + ", ".join([m.group(2)] * len(chunk)) + m.group(3),
                                [a for row in chunk for a in row])
            elif hasattr(cur, "copy"):                    # psycopg 3 pipelines executemany itself
                cur.executemany(q, seq)
            else:
                from psycopg2.extras import execute_batch
                execute_batch(cur, q, seq, page_size=500)
            return PgCursor(cur)

    def executescript(self, script):
        for stmt in (s.strip() for s in script.split(";")):
            if stmt:
                self.execute(stmt)

    def __enter__(self):
        self._lock.acquire()
        if self._depth == 0:
            self._raw.cursor().execute("BEGIN")
        self._depth += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._depth -= 1
            if self._depth == 0:
                self._raw.cursor().execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self._lock.release()
        return False

    def close(self):
        if self._pool is not None:
            self._pool.put(self)
        else:
            self._raw.close()

    def __del__(self):
        # one nobody closed (an Api object dropped without close()): end its server session now, rather than
        # when the driver's own finalizer gets to it (psycopg 3 warns; either way the session lingers)
        try:
            if not self._raw.closed:
                self._raw.close()
        except Exception:
            pass

    def set_trace_callback(self, fn):
        raise NotImplementedError("statement tracing is SQLite-only")


def _open(url, schema, create=False):
    drv = _driver()
    if drv.__name__ == "psycopg":
        # client-side binding, as psycopg2 does: a parameter in a select list (INSERT ... SELECT ?, ...) has no
        # type for the server to infer, and server-side binding refuses it
        raw = drv.connect(url, autocommit=True, cursor_factory=drv.ClientCursor)
    else:
        raw = drv.connect(url)
        raw.autocommit = True
    cur = raw.cursor()
    if create:                       # the engine's connection only: a read-only role may not create schemas
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    cur.execute(f"SET search_path TO {schema}")
    cur.execute(f"SET TIME ZONE {local_zone()}")
    return raw


def connect(url, schema, create=False):
    return PgConnection(_open(url, schema, create), schema)


class ReaderPool:
    """Unscoped read connections, reused across requests: the server handles each request on a new thread,
    and a connection per thread would open (and leak until collected) one server connection per request."""

    def __init__(self, url, schema, size=8):
        self.url, self.schema, self.size = url, schema, size
        self._idle = []
        self._mu = threading.Lock()

    def get(self):
        with self._mu:
            con = self._idle.pop() if self._idle else None
        if con is None:
            raw = _open(self.url, self.schema)
            raw.cursor().execute("SET default_transaction_read_only = on")
            con = PgConnection(raw, self.schema, pool=self)
        return con

    def put(self, con):
        with self._mu:
            if len(self._idle) < self.size:
                self._idle.append(con)
                return
        con._raw.close()


_pools = {}
_pools_mu = threading.Lock()


def reader(url, schema):
    with _pools_mu:
        p = _pools.get((url, schema))
        if p is None:
            p = _pools[(url, schema)] = ReaderPool(url, schema)
    return p.get()


def local_zone():
    """The session time zone that makes Postgres's local dates agree with Python's (and SQLite's
    'localtime'): TZ if set, the system zone's name where the OS says it, else today's UTC offset (which is
    wrong for dates across a daylight-saving change; set TZ to avoid that)."""
    tz = os.environ.get("TZ")
    if tz and re.fullmatch(r"[A-Za-z0-9_+\-/]+", tz):
        return f"'{tz}'"
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return "'" + link.split("zoneinfo/", 1)[1] + "'"
    except OSError:
        pass
    off = -(time.altzone if time.localtime().tm_isdst > 0 else time.timezone)
    sign = "+" if off >= 0 else "-"
    return f"INTERVAL '{sign}{abs(off) // 3600:02d}:{abs(off) % 3600 // 60:02d}' HOUR TO MINUTE"


# ---------------------------------------------------------------- schema

def ddl(schema_version, derived):
    """The Postgres schema, from the same column lists store.py builds SQLite's from."""
    from . import store as s
    text = {"runs": {"id", "source", "project", "environment", "framework", "workflow", "cwd", "title", "agent_name",
                     "parent_id", "parent_task_id", "thread_id", "user_id", "tags", "root_status", "version",
                     "git_branch", "entrypoint", "file", "policy_version", "policy"},
            "tasks": {"id", "run_id", "project", "environment", "source", "framework", "workflow", "parent_task_id",
                      "prompt_kind", "prompt", "task_type", "models", "churn_file", "final_stop", "final_text",
                      "next_prompt", "outcome", "apdex", "outcome_source", "outcome_reason", "task_type_source",
                      "task_type_match", "loop_node", "critical_node", "root_error", "policy_version", "tripwire_what",
                      *s.TASK_JSON},
            "steps": {"run_id", "task_id", "kind", "name", "model", "phase", "target", "text", "input_preview", "error",
                      "subagent_id", "stop_reason", "effort", "span_id", "parent_span_id", "node", "agent", "span_kind",
                      "rule", "guard", "args_json", "flags", "tripwire"},
            "events": {"id", "rule_id", "rule", "severity", "task_id", "run_id", "project", "task_type", "message"}}

    def cols(table, names):
        return ", ".join(f"{c} {'TEXT' if c in text[table] else NUM}" for c in names)
    dims = ", ".join(f"{c} {NUM if c == 'is_subagent' else 'TEXT'}" for c in s.ROLLUP_DIMS)
    return [
        f"CREATE TABLE IF NOT EXISTS runs ({cols('runs', s.RUN_COLS)}, PRIMARY KEY (id))",
        f"CREATE TABLE IF NOT EXISTS tasks ({cols('tasks', s.TASK_COLS + s.TASK_JSON)}, PRIMARY KEY (id))",
        f"CREATE TABLE IF NOT EXISTS steps ({cols('steps', s.STEP_COLS + ['flags'])})",
        f"CREATE TABLE IF NOT EXISTS events ({cols('events', s.EVENT_COLS)})",
        "CREATE TABLE IF NOT EXISTS baselines (task_type TEXT PRIMARY KEY, data TEXT)",
        "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)",
        "CREATE INDEX IF NOT EXISTS steps_task ON steps (task_id)",
        "CREATE INDEX IF NOT EXISTS steps_run ON steps (run_id)",
        "CREATE INDEX IF NOT EXISTS steps_node ON steps (node)",
        "CREATE INDEX IF NOT EXISTS tasks_run ON tasks (run_id)",
        "CREATE INDEX IF NOT EXISTS tasks_started ON tasks (started)",
        "CREATE INDEX IF NOT EXISTS events_task ON events (task_id)",
        # durable
        "CREATE TABLE IF NOT EXISTS spans_raw (source TEXT, trace_id TEXT, span_id TEXT, fmt TEXT, doc TEXT, "
        f"updated {NUM}, PRIMARY KEY (source, span_id))",
        "CREATE INDEX IF NOT EXISTS spans_trace ON spans_raw (source, trace_id)",
        "CREATE INDEX IF NOT EXISTS spans_updated ON spans_raw (updated)",
        "CREATE TABLE IF NOT EXISTS source_state (name TEXT PRIMARY KEY, data TEXT)",
        f"CREATE TABLE IF NOT EXISTS alerts_sent (event_id TEXT PRIMARY KEY, ts {NUM})",
        f"CREATE TABLE IF NOT EXISTS grades (task_id TEXT PRIMARY KEY, outcome TEXT, reason TEXT, graded_by TEXT, ts {NUM})",
        f"CREATE TABLE IF NOT EXISTS alert_outbox (id BIGSERIAL PRIMARY KEY, dest TEXT, body TEXT, created {NUM}, "
        f"attempts {NUM} DEFAULT 0, next_try {NUM}, last_error TEXT)",
        f"CREATE TABLE IF NOT EXISTS alert_state (key TEXT PRIMARY KEY, since {NUM}, data TEXT)",
        "CREATE TABLE IF NOT EXISTS revocations (id TEXT PRIMARY KEY, project TEXT, agent TEXT, reason TEXT, "
        f"source TEXT, created {NUM}, expires {NUM}, cleared {NUM})",
        f"CREATE TABLE IF NOT EXISTS rollup_daily ({dims}, {', '.join(f'{c} {NUM}' for c in s.ROLLUP_SUMS)}, "
        f"PRIMARY KEY ({', '.join(s.ROLLUP_DIMS)}))",
    ]


def connect_store(url, schema, schema_version, derived):
    """The engine's read-write connection: creates the schema, and drops derived tables on a version change."""
    con = connect(url, schema, create=True)
    with con:
        # one engine migrating at a time: a second waits here rather than racing the DROP/CREATE
        con.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"agentdynamics:{schema}",))
        con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
        r = con.execute("SELECT version FROM schema_version").fetchone()
        if r is None or r[0] != schema_version:
            for t in derived:
                con.execute(f"DROP TABLE IF EXISTS {t}")
            con.execute("DELETE FROM schema_version")
            con.execute("INSERT INTO schema_version (version) VALUES (?)", (schema_version,))
        for stmt in ddl(schema_version, derived):
            con.execute(stmt)
    return con


# ---------------------------------------------------------------- SQL translation

# the primary key of every table an INSERT OR REPLACE writes, for its ON CONFLICT clause
PRIMARY_KEYS = {"runs": ("id",), "tasks": ("id",), "baselines": ("task_type",), "meta": ("k",),
                "spans_raw": ("source", "span_id"), "source_state": ("name",), "alerts_sent": ("event_id",),
                "grades": ("task_id",), "alert_state": ("key",), "revocations": ("id",), "alert_outbox": ("id",)}

_WEEK = ("(to_char({d}, 'YYYY') || '-W' || "
         "lpad(floor((extract(doy from {d}) + 7 - extract(isodow from {d})) / 7)::int::text, 2, '0'))")


def _rollup_keys():
    from . import store
    return tuple(store.ROLLUP_DIMS)


def _placeholders(sql, has_args):
    """? -> %s, and a literal % doubled so the driver's formatting leaves it alone; string literals kept."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            j = sql.index("'", i + 1)
            while j + 1 < n and sql[j + 1] == "'":        # '' inside a literal
                j = sql.index("'", j + 2)
            lit = sql[i:j + 1]
            out.append(lit.replace("%", "%%") if has_args else lit)
            i = j + 1
            continue
        if c == "?":
            out.append("%s")
        elif c == "%" and has_args:
            out.append("%%")
        else:
            out.append(c)
        i += 1
    return "".join(out)


_CLAUSE_END = re.compile(r"\b(LIMIT|OFFSET|UNION)\b", re.I)


def _nulls_like_sqlite(sql):
    """SQLite sorts NULL as the smallest value, Postgres as the largest: "worst score first" would put
    unscored tasks last, "furthest over baseline first" would put tasks with no baseline first. Every ORDER
    BY term gets SQLite's placement spelled out."""
    out, pos = [], 0
    for m in re.finditer(r"\bORDER BY\b", sql, re.I):
        if m.start() < pos:
            continue
        i, depth, quote = m.end(), 0, False
        while i < len(sql):                         # the clause runs to LIMIT/OFFSET/UNION, ")" or the end
            c = sql[i]
            if c == "'":
                quote = not quote
            elif not quote:
                if c == "(":
                    depth += 1
                elif c == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and _CLAUSE_END.match(sql, i) and (i == 0 or not sql[i - 1].isalnum()):
                    break
            i += 1
        terms, depth, quote, start = [], 0, False, m.end()
        for j in range(m.end(), i):
            c = sql[j]
            if c == "'":
                quote = not quote
            elif not quote and c == "(":
                depth += 1
            elif not quote and c == ")":
                depth -= 1
            elif not quote and depth == 0 and c == ",":
                terms.append(sql[start:j])
                start = j + 1
        terms.append(sql[start:i])
        fixed = []
        for t in terms:
            body = t.rstrip()
            if re.search(r"\bNULLS\b", body, re.I):
                fixed.append(t)
            elif re.search(r"\bDESC$", body, re.I):
                fixed.append(body + " NULLS LAST" + t[len(body):])
            else:
                fixed.append(body + " NULLS FIRST" + t[len(body):])
        out.append(sql[pos:m.end()] + ",".join(fixed))
        pos = i
    out.append(sql[pos:])
    return "".join(out)


@lru_cache(maxsize=4096)
def translate(sql, has_args, schema):
    s = re.sub(r"\s+INDEXED BY \w+", "", sql)
    s = re.sub(r"(?<![\w.])main\.", f"{schema}.", s)
    s = re.sub(r"\bMAX\(\s*1\s*,", "GREATEST(1,", s)
    s = re.sub(r"date\(([\w.]+), 'unixepoch', 'localtime'\)", r"to_char(to_timestamp(\1), 'YYYY-MM-DD')", s)
    s = re.sub(r"strftime\('%H', ([\w.]+), 'unixepoch', 'localtime'\)", r"to_char(to_timestamp(\1), 'HH24')", s)
    s = re.sub(r"strftime\('%Y-W%W', ([\w.]+), 'unixepoch', 'localtime'\)",
               lambda m: _WEEK.format(d=f"to_timestamp({m.group(1)})"), s)
    s = re.sub(r"strftime\('%Y-W%W', ([\w.]+)\)", lambda m: _WEEK.format(d=f"({m.group(1)})::date"), s)
    m = re.match(r"\s*INSERT OR IGNORE INTO ", s)
    if m:
        s = "INSERT INTO " + s[m.end():] + " ON CONFLICT DO NOTHING"
    m = re.match(r"\s*INSERT OR REPLACE INTO (\w+) \(([^)]*)\)", s)
    if m:
        table, cols = m.group(1), [c.strip() for c in m.group(2).split(",")]
        keys = _rollup_keys() if table == "rollup_daily" else PRIMARY_KEYS.get(table)
        if keys is None:
            raise ValueError(f"INSERT OR REPLACE into {table}: add its primary key to pg.PRIMARY_KEYS")
        rest = [c for c in cols if c not in keys]
        action = ("DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in rest)) if rest else "DO NOTHING"
        s = f"INSERT INTO {table} ({m.group(2)})" + s[m.end():] + f" ON CONFLICT ({', '.join(keys)}) {action}"
    return _placeholders(_nulls_like_sqlite(s), has_args)
