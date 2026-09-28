"""Copy a SQLite store into Postgres, so an existing install can move without starting empty.

Only what can't be rebuilt is copied (invariant 3): the first refresh on Postgres rebuilds runs, tasks, steps,
events and baselines from these sources, as any process's first refresh does.

  spans_raw       every span and LangSmith/Langfuse/OTLP/Aegis record
  runs/*.json     SDK runs -- files with SQLite, documents in spans_raw (source "sdk") on Postgres
  grades          outcomes stated after the fact
  rollup_daily    the only copy of days retention has purged
  revocations     server-side revocation directives
  incidents, incident_signals   security incidents, and the verdicts people gave them
  alerts_sent, alert_state, alert_outbox   what was alerted, what is firing, what is waiting to be sent
  source_state    pull cursors (LangSmith, Langfuse) and other saved state
  rules.json, slos.json in the data directory -> the store, where every instance reads them

Not copied: API keys (keys.json) and pricing.json are configuration -- give every instance the same files or
environment. Claude Code transcripts stay where they are; point `--claude-root` at them as before.

It reads one consistent snapshot of the SQLite file and can be re-run: to pick up what arrived during a first
copy, stop the SQLite instance and copy again before switching over. Until a Postgres instance has refreshed
on the schema, a copy makes it hold exactly what the source holds -- whatever the source has since deleted
(spans retention purged, a grade taken back, an alert delivered) goes too, or it would be sent again. After
that, or into a schema holding another store, it refuses unless told to merge (`force`): upserts only, and
the other store's queued alerts queued behind what is already there.
"""
import json
import os
import sqlite3
import time

from . import store

DURABLE = ["spans_raw", "source_state", "alerts_sent", "grades", "alert_outbox", "alert_state", "rollup_daily",
           "revocations", "incidents", "incident_signals"]
MARKER = "copied_from"


class CopyError(Exception):
    pass


def copy(data_dir, url, schema, force=False, log=print, batch=1000):
    """Copy the SQLite store in `data_dir` into Postgres `url`/`schema`. Returns {what: count}."""
    src_path = os.path.abspath(os.path.join(data_dir, "agentdynamics.db"))
    if not os.path.exists(src_path):
        raise CopyError(f"no SQLite store at {src_path}")
    dst = store.connect(url, schema)            # creates the schema and its tables if needed
    try:
        mark = store.get_state(dst, MARKER)
        held = dst.execute("SELECT COUNT(*) FROM spans_raw").fetchone()[0]
        used = dst.execute("SELECT COUNT(*) FROM meta").fetchone()[0]      # written by an instance's refresh
        why = (f"a Postgres instance has already run on schema {schema}, and copying again would undo what it has "
               f"done since" if used else
               f"schema {schema} already holds {held} spans from {mark.get('sqlite') or 'another store'}"
               if held and mark.get("sqlite") != src_path else None)
        if why and not force:
            raise CopyError(why + "; copy into an empty schema, or pass --force to merge this store into it")
        merging = bool(why)
        # one read-only snapshot across every table, even if a server is still writing the file
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, isolation_level=None)
        src.row_factory = sqlite3.Row
        counts = {}
        try:
            src.execute("BEGIN")
            for table in DURABLE:
                cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
                if not cols:
                    continue                    # a store from before this table existed
                verb = "INSERT OR REPLACE"
                if merging and table == "alert_outbox":
                    cols.remove("id")           # another store's ids: queue behind what is already here
                    verb = "INSERT"
                n, cur = 0, src.execute(f"SELECT {', '.join(cols)} FROM {table}")
                sql = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
                with dst:                       # a table at a time, never seen half-replaced
                    if not merging:
                        dst.execute(f"DELETE FROM {table}" + (" WHERE source != 'sdk'" if table == "spans_raw" else ""))
                    while True:
                        rows = cur.fetchmany(batch)
                        if not rows:
                            break
                        dst.executemany(sql, [tuple(r) for r in rows])
                        n += len(rows)
                counts[table] = n
                log(f"  {table:<14} {n:>10}")
            src.execute("COMMIT")
        finally:
            src.close()
        # new rows get ids after the copied ones, not colliding with them
        dst.execute("SELECT setval(pg_get_serial_sequence('alert_outbox', 'id'), "
                    "GREATEST((SELECT COALESCE(MAX(id), 0) FROM alert_outbox), 1))")
        counts["sdk runs"] = _copy_run_files(os.path.join(data_dir, "runs"), dst, not merging, log)
        for name in ("rules", "slos"):
            path = os.path.join(data_dir, f"{name}.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    store.set_state(dst, f"setting:{name}", {"value": json.load(f)})
                counts[f"{name}.json"] = 1
                log(f"  {name + '.json':<14} {'saved':>10}")
        if not merging:
            store.set_state(dst, MARKER, {"sqlite": src_path, "at": time.time(), "counts": counts})
        return counts
    finally:
        dst.close()


def _copy_run_files(runs_dir, dst, replace, log):
    """SDK run files -> sdk documents in spans_raw, stamped with the file's time as if ingested then, so
    Postgres retention purges them when it would have purged the file."""
    from .collectors import generic
    n, skipped, items = 0, 0, []
    sql = ("INSERT OR REPLACE INTO spans_raw (source, trace_id, span_id, fmt, doc, updated) "
           "VALUES ('sdk', ?, ?, 'run', ?, ?)")
    names = sorted(os.listdir(runs_dir)) if os.path.isdir(runs_dir) else []
    with dst:
        if replace:
            dst.execute("DELETE FROM spans_raw WHERE source = 'sdk'")
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(runs_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    payload = json.load(f)
                rid = generic.normalize(payload)["id"]
                mtime = os.path.getmtime(path)
            except (OSError, ValueError, KeyError, TypeError):
                skipped += 1                # unreadable here, and unreadable to the engine too
                continue
            items.append((rid, rid, json.dumps(payload, default=str), mtime))
            if len(items) >= 500:
                dst.executemany(sql, items)
                n, items = n + len(items), []
        if items:
            dst.executemany(sql, items)
            n += len(items)
    log(f"  {'sdk runs':<14} {n:>10}" + (f"   ({skipped} unreadable files skipped)" if skipped else ""))
    return n


def verify(data_dir, url, schema):
    """{table: (in SQLite, in Postgres)} for every copied table, and SDK run files against sdk documents."""
    src = sqlite3.connect(f"file:{os.path.join(data_dir, 'agentdynamics.db')}?mode=ro", uri=True)
    dst = store.connect(url, schema)
    try:
        out = {}
        for table in DURABLE:
            if not [r for r in src.execute(f"PRAGMA table_info({table})")]:
                continue
            a = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            b = dst.execute(f"SELECT COUNT(*) FROM {table}" + {
                "spans_raw": " WHERE source != 'sdk'",                  # SDK runs: counted against the files
                "source_state": f" WHERE name NOT LIKE 'setting:%' AND name != '{MARKER}'"}.get(table, "")
            ).fetchone()[0]
            out[table] = (a, b)
        runs = os.path.join(data_dir, "runs")
        files = len([f for f in os.listdir(runs) if f.endswith(".json")]) if os.path.isdir(runs) else 0
        out["sdk runs"] = (files, dst.execute("SELECT COUNT(*) FROM spans_raw WHERE source = 'sdk'").fetchone()[0])
        for name in ("rules", "slos"):
            if os.path.exists(os.path.join(data_dir, f"{name}.json")):
                out[f"{name}.json"] = (1, 1 if store.get_state(dst, f"setting:{name}") else 0)
        return out
    finally:
        src.close()
        dst.close()
