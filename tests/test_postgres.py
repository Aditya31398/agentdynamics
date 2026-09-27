"""The Postgres backend answers exactly as SQLite does.

The same traffic goes into a SQLite store and a Postgres one, and every GET route in server.py -- read out
of the source, so a route added later is covered -- is called on both with several query variants. The
JSON must match, numbers to 1e-9. Differences in dialect (NULL ordering, integer division, GROUP BY rules,
row order where the SQL left it unspecified) show up here as a diff on a route, not as a bug report.

Runs when AGENTDYNAMICS_TEST_PG_URL points at a Postgres the test may create schemas in, e.g.

    docker run -d -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=... postgres:16-alpine
    AGENTDYNAMICS_TEST_PG_URL=postgresql://postgres:...@127.0.0.1:55432/postgres python -m unittest tests.test_postgres

and a driver (psycopg 3 or psycopg2) is installed. The whole suite can also run against Postgres:
AGENTDYNAMICS_DB_URL=<url> AGENTDYNAMICS_DB_SCHEMA="t_{data_dir}" python -m unittest discover tests
"""
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import quote

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "bench"))
sys.path.insert(0, os.path.dirname(__file__))

from agentdynamics import config, slo  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api, Handler  # noqa: E402

PG_URL = os.environ.get("AGENTDYNAMICS_TEST_PG_URL")


def has_driver():
    try:
        from agentdynamics.pg import _driver
        _driver()
        return True
    except RuntimeError:
        return False


# values that describe the store or the moment, not the data
VOLATILE = {"refreshed", "refresh_seconds", "last_refresh", "data_dir", "db", "db_schema", "seconds", "last_ok",
            "last_data", "last", "stats", "last_duration", "file"}


def same(a, b, path=""):
    """Where two JSON values differ, or None. Numbers compare to 1e-9; int 3 equals float 3.0."""
    if isinstance(a, bool) or isinstance(b, bool):
        return None if a == b else f"{path}: {a!r} != {b!r}"
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return None if math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9) else f"{path}: {a!r} != {b!r}"
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) - VOLATILE != set(b) - VOLATILE:
            return f"{path}: keys {sorted(set(a) ^ set(b))}"
        for k in a:
            if k not in VOLATILE:
                d = same(a[k], b[k], f"{path}.{k}")
                if d:
                    return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: {len(a)} items != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = same(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {a!r} != {b!r}"


def traffic(e, now):
    """A varied store: workflows, models, tools, errors, subagents, conversations, governance, OTLP, grades."""
    import bench
    from test_scoped_keys import run as gov_run
    rng = random.Random(7)
    t0 = now - 6 * 86400
    for i in range(120):
        p = bench.sdk_run(i, rng, t0 + i * 3000)
        p["project"] = ["shop", "billing", "support"][i % 3]
        if i % 7 == 0:
            p["thread_id"] = f"conv-{i // 21}"
        if i % 11 == 0 and i:
            p = dict(p, id=f"agent-sub{i}", parent_id=f"bench-{i - 1}", is_subagent=True)
        e.ingest(p)
    for i in range(6):
        e.ingest(gov_run(f"gov-{i}", "shop", "refund_flow", f"refund {i}", "issue_refund", error=i == 0))
    e.ingest_otlp(bench.otlp_batch(0, 30, random.Random(3), t0 + 50000), "application/json")
    e.refresh(force=True)
    e.grade("bench-3#0", "failed", "graded in the test")
    e.grade("bench-4#0", "completed")
    e.refresh()


@unittest.skipUnless(PG_URL and has_driver(), "set AGENTDYNAMICS_TEST_PG_URL and install psycopg to run")
class SameAnswersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        now = time.time()
        cls.engines, cls.urls, cls.servers = [], [], []
        for kind in ("sqlite", "postgres"):
            d = os.path.join(cls.tmp, kind)
            os.makedirs(d)
            slo.save(d, slo.DEFAULT_SLOS + [{"id": "shop", "name": "Shop success", "metric": "success_rate",
                                             "op": ">=", "target": 0.9, "window_days": 7, "scope": {"project": "shop"}}])
            cfg = config.load(d)
            cfg["store"] = {"url": PG_URL, "schema": "diff_{data_dir}"} if kind == "postgres" else {"url": ""}
            e = Engine(d, None, cfg=cfg)
            e._clock = lambda: now
            traffic(e, now)
            srv = ThreadingHTTPServer(("127.0.0.1", 0), type(f"H{kind}", (Handler,), {"api": Api(e)}))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            cls.engines.append(e)
            cls.servers.append(srv)
            cls.urls.append(f"http://127.0.0.1:{srv.server_address[1]}")
        with open(os.path.join(ROOT, "agentdynamics", "server.py"), encoding="utf-8") as f:
            src = f.read()
        cls.routes = sorted(set(re.findall(r'"(/api/[a-z/]+|/metrics)"', src)))

    @classmethod
    def tearDownClass(cls):
        for srv in cls.servers:
            srv.shutdown()
            srv.server_close()
        for e in cls.engines:
            e.con.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, base, path):
        try:
            with urllib.request.urlopen(base + path, timeout=60) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as ex:
            return ex.code, ex.read().decode()

    def paths(self):
        for r in self.routes:
            if r.endswith("/") or r in ("/api/config", "/api/sources", "/api/alerts"):
                continue                              # install facts: where the store is, process stats
            for qs in ("", "?days=", "?project=shop", "?days=2&sub=1", "?type=support", "?group=project&metrics="
                       "tasks,cost,avg_cost,avg_score,error_rate,rework_rate,verification_rate,avg_context,cache_hit",
                       "?group=day", "?group=week", "?group=hour", "?group=models", "?sort=score", "?sort=baseline",
                       "?dim=project&a=shop&b=billing", "?name=support"):
                yield r + qs
        for tid in ("bench-3#0", "bench-10#0", "otlp:" + f"{5:032x}" + "#0", "gov-0#0"):
            yield "/api/task/" + quote(tid, safe="")

    def test_every_route_answers_the_same(self):
        diffs, compared = [], 0
        for p in self.paths():
            (sa, a), (sb, b) = self.get(self.urls[0], p), self.get(self.urls[1], p)
            if sa != sb:
                diffs.append(f"{p}: status {sa} != {sb}: {b[:200]}")
                continue
            if p.startswith("/metrics"):
                a = {ln.rsplit(" ", 1)[0]: float(ln.rsplit(" ", 1)[1]) for ln in a.splitlines() if ln and ln[0] != "#"}
                b = {ln.rsplit(" ", 1)[0]: float(ln.rsplit(" ", 1)[1]) for ln in b.splitlines() if ln and ln[0] != "#"}
                d = same({k: v for k, v in a.items() if "refresh" not in k and "alerts_" not in k},
                         {k: v for k, v in b.items() if "refresh" not in k and "alerts_" not in k}, p)
            else:
                d = same(json.loads(a), json.loads(b), p)
            compared += 1
            if d:
                diffs.append(d[:300])
        self.assertGreater(compared, 200)
        self.assertEqual(diffs, [], f"{len(diffs)} of {compared} responses differ between SQLite and Postgres:\n"
                         + "\n".join(diffs))

    def test_the_password_never_shows(self):
        from urllib.parse import urlsplit
        password = urlsplit(PG_URL).password
        if not password:
            self.skipTest("the test database URL has no password")
        for path in ("/api/config", "/metrics", "/healthz", "/api/sources"):
            self.assertNotIn(password, self.get(self.urls[1], path)[1], path)

    def test_the_stores_really_differ(self):
        """So the comparison can't pass by both servers reading one store."""
        self.assertIsNone(self.engines[0].db_schema)
        self.assertTrue(self.engines[1].db_schema.startswith("diff_"))
        n = self.engines[1].con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.assertGreater(n, 100)


@unittest.skipUnless(PG_URL and has_driver(), "set AGENTDYNAMICS_TEST_PG_URL and install psycopg to run")
class DialectTest(unittest.TestCase):
    """Each SQLite construct pg.translate rewrites, run on both stores against data chosen to show a
    difference: NULLs in a sort, weeks at a year boundary and on Sundays, % beside a ? placeholder,
    an upsert repeated. The API comparison above only exercises what its fixture happens to contain."""

    @classmethod
    def setUpClass(cls):
        from agentdynamics import pg, store
        cls.tmp = tempfile.mkdtemp()
        cls.lite = store.connect(os.path.join(cls.tmp, "probe.db"))
        schema = pg.schema_name("probe_{data_dir}", cls.tmp)
        cls.pg = store.connect(PG_URL, schema)
        cls.lite.execute("CREATE TABLE probe (id, n, s, ts)")
        cls.pg.execute("DROP TABLE IF EXISTS probe")
        cls.pg.execute("CREATE TABLE probe (id TEXT, n DOUBLE PRECISION, s TEXT, ts DOUBLE PRECISION)")
        local = [(2025, 12, 28, 23), (2025, 12, 29, 0), (2026, 1, 1, 12), (2026, 1, 4, 23), (2026, 1, 5, 0),
                 (2026, 3, 1, 6), (2026, 9, 27, 18), (2026, 12, 31, 23)]
        rows_ = [(f"p{i}", n, s, time.mktime((y, mo, d, h, 30, 0, 0, 0, -1)))
                 for i, ((y, mo, d, h), n, s) in enumerate(zip(local, [3, None, 1, 7, None, 2, 5, 4],
                                                                ["b", None, "a", "50% off", "c", None, "a?", "d"]))]
        for con in (cls.lite, cls.pg):
            with con:
                con.executemany("INSERT INTO probe (id, n, s, ts) VALUES (?, ?, ?, ?)", rows_)

    @classmethod
    def tearDownClass(cls):
        cls.lite.close()
        cls.pg.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def both(self, sql, args=()):
        a = [tuple(r) for r in self.lite.execute(sql, args).fetchall()]
        b = [tuple(r) for r in self.pg.execute(sql, args).fetchall()]
        self.assertIsNone(same([list(x) for x in a], [list(x) for x in b], sql), f"{sql}\n sqlite {a}\n pg     {b}")
        return a

    def test_nulls_sort_as_sqlite_sorts_them(self):
        self.assertEqual(self.both("SELECT n FROM probe ORDER BY n, id")[0], (None,))
        self.assertEqual(self.both("SELECT n FROM probe ORDER BY n DESC, id")[-1], (None,))
        self.both("SELECT s FROM probe ORDER BY s DESC, id LIMIT ?", (5,))
        self.both("SELECT id FROM probe ORDER BY (SELECT MAX(n) FROM probe), s, id")

    def test_local_dates_weeks_and_hours(self):
        self.both("SELECT id, date(ts, 'unixepoch', 'localtime') FROM probe ORDER BY id")
        weeks = self.both("SELECT id, strftime('%Y-W%W', ts, 'unixepoch', 'localtime') FROM probe ORDER BY id")
        self.assertIn(("p1", "2025-W52"), weeks)           # a Monday
        self.assertIn(("p2", "2026-W00"), weeks)           # before the year's first Monday
        self.both("SELECT id, strftime('%H', ts, 'unixepoch', 'localtime') FROM probe ORDER BY id")
        self.both("SELECT id, strftime('%Y-W%W', d) FROM (SELECT id, date(ts, 'unixepoch', 'localtime') d FROM probe) "
                  "x ORDER BY id")

    def test_scalar_max_placeholders_and_sums(self):
        self.both("SELECT id, MAX(1, n) FROM probe WHERE n IS NOT NULL ORDER BY id")
        self.both("SELECT id FROM probe WHERE s LIKE '%off%' AND n > ? ORDER BY id", (1,))
        self.both("SELECT id FROM probe WHERE s = 'a?' OR id = ? ORDER BY id", ("p0",))
        self.both("SELECT id FROM probe WHERE length(id) % 2 = 0 AND n > ? ORDER BY id", (0,))    # % as modulo
        total = self.both("SELECT SUM(n), COUNT(*), 1.0 * SUM(n) / MAX(1, COUNT(*)) FROM probe")[0]
        self.assertIsInstance(self.pg.execute("SELECT COUNT(*) FROM probe").fetchone()[0], int)
        self.assertEqual(total[0], 22)

    def test_upserts(self):
        for con in (self.lite, self.pg):
            with con:
                con.execute("INSERT OR REPLACE INTO grades (task_id, outcome, reason, graded_by, ts) VALUES (?, ?, ?, ?, ?)",
                            ("t1", "failed", "first", "a", 1.0))
                con.execute("INSERT OR REPLACE INTO grades (task_id, outcome, reason, graded_by, ts) VALUES (?, ?, ?, ?, ?)",
                            ("t1", "completed", "second", "b", 2.0))
                con.execute("INSERT OR IGNORE INTO alerts_sent (event_id, ts) VALUES (?, ?)", ("e1", 1.0))
                con.execute("INSERT OR IGNORE INTO alerts_sent (event_id, ts) VALUES (?, ?)", ("e1", 2.0))
        self.assertEqual(self.both("SELECT task_id, outcome, reason FROM grades"), [("t1", "completed", "second")])
        self.assertEqual(self.both("SELECT event_id, ts FROM alerts_sent"), [("e1", 1)])
        # one batch upserting a span twice (a client re-sending it within one request): the last one wins.
        # Postgres writes a batch as one multi-row INSERT, which may not touch a row twice
        from agentdynamics import store
        for con in (self.lite, self.pg):
            store.upsert_spans(con, "otlp", [("tr", "s1", "canonical", {"v": 1}), ("tr", "s2", "canonical", {"v": 2}),
                                             ("tr", "s1", "canonical", {"v": 3})])
        self.assertEqual(self.both("SELECT span_id, doc FROM spans_raw WHERE trace_id = ? ORDER BY span_id", ("tr",)),
                         [("s1", '{"v": 3}'), ("s2", '{"v": 2}')])


def store_state(e):
    from agentdynamics import store
    return store.alert_state(e.con)


@unittest.skipUnless(PG_URL and has_driver(), "set AGENTDYNAMICS_TEST_PG_URL and install psycopg to run")
class FleetTest(unittest.TestCase):
    """Two instances on one Postgres schema: one writes the analysis, the other serves it and takes ingest,
    grades and settings -- all of which must reach the writer -- and takes over when the writer goes away."""

    def setUp(self):
        from test_alerts import run as alert_run
        self.run_ = alert_run
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        schema = "fleet_" + os.urandom(4).hex()
        self.now = time.time()
        self.engines = []
        for name in ("a", "b"):
            d = os.path.join(self.tmp, name)
            os.makedirs(d)
            cfg = config.load(d)
            cfg["store"] = {"url": PG_URL, "schema": schema}
            e = Engine(d, None, cfg=cfg)
            e._clock = lambda: self.now
            self.engines.append(e)
        self.addCleanup(lambda: [e.con.close() for e in self.engines if not e.con._raw.closed])

    def tasks(self, e, where="1=1"):
        return e.con.execute(f"SELECT COUNT(*) FROM tasks WHERE {where}").fetchone()[0]

    def test_one_writer_and_everything_reaches_it(self):
        w, r = self.engines
        self.assertEqual((w.writer, r.writer), (True, False))
        # ingest on the reader: SDK runs go in the shared store, not the reader's disk
        for i in range(4):
            r.ingest(self.run_(f"r{i}", self.now - 600 + i, failed=i == 0))
        self.assertEqual(os.listdir(r.runs_dir), [])
        self.assertFalse(r.refresh(), "a reader writes no analysis")
        self.assertTrue(w.refresh())
        self.assertEqual(self.tasks(w), 4)
        self.assertEqual(Api(r).overview({"days": ""})["kpis"]["tasks"], 4, "the reader serves the writer's analysis")
        self.assertEqual(Api(r).healthz()["role"], "reader")
        self.assertEqual(Api(r).healthz()["status"], "ok")
        # a grade given on the reader is applied by the writer
        r.grade("r1#0", "failed", "from the reader")
        self.assertTrue(w.refresh())
        self.assertEqual(w.con.execute("SELECT outcome, outcome_reason FROM tasks WHERE id = 'r1#0'").fetchone(),
                         ("failed", "from the reader"))
        # a rule switched off on the reader: the writer re-scores every task, not just new ones
        self.assertGreater(self.tasks(w, "id IN (SELECT task_id FROM events WHERE rule_id = 'run_failed')"), 0)
        r.save_rules([dict(x, enabled=False) if x["id"] == "run_failed" else x for x in r.rules()])
        self.assertTrue(w.refresh())
        self.assertEqual(w.con.execute("SELECT COUNT(*) FROM events WHERE rule_id = 'run_failed'").fetchone()[0], 0)
        # an SLO saved on the reader is the writer's too
        r.save_slos([{"id": "x", "name": "X", "metric": "success_rate", "op": ">=", "target": 0.5, "window_days": 7,
                      "scope": {}}])
        self.assertEqual([s["id"] for s in w.slos()], ["x"])

    def test_only_the_writer_alerts(self):
        """The alert state and outbox are shared. A reader holds no tasks, so to it no SLO is burning: if it
        ran the SLO check it would resolve every page the writer has open, and if it delivered, page twice."""
        from test_alerts import Receiver
        rx = Receiver()
        self.addCleanup(rx.close)
        slos = [{"id": "s", "name": "S", "metric": "success_rate", "op": ">=", "target": 0.9, "window_days": 7,
                 "scope": {}}]
        for e in self.engines:
            e.cfg["alerts"] = {"webhooks": [{"name": "h", "url": rx.url, "kinds": ["slos"]}], "slo_min_tasks": 10}
        w, r = self.engines
        w.save_slos(slos)
        for i in range(20):
            w.ingest(self.run_(f"b{i}", self.now - 200 + i, failed=i % 2 == 0))
        w.refresh()
        self.assertTrue(w.check_slos(), "the writer's burst should page")
        firing = sorted(store_state(w))
        self.assertEqual(r.check_slos(), [])
        self.assertEqual(sorted(store_state(w)), firing, "the reader resolved the writer's alerts")
        self.assertEqual(r.deliver_alerts(), 0)
        self.assertGreater(w.deliver_alerts(), 0)

    def test_the_reader_takes_over(self):
        w, r = self.engines
        r.ingest(self.run_("r0", self.now - 600))
        w.refresh()
        w.con.close()                 # the writer goes away: its session, and its lock, end
        r.ingest(self.run_("r1", self.now - 500))
        self.assertTrue(r.refresh())
        self.assertTrue(r.writer)
        self.assertEqual(self.tasks(r), 2)


if __name__ == "__main__":
    unittest.main()
