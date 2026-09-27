"""Retention with daily rollups: purging tasks must not change any total the console or /metrics reports.

The property: take a store holding 60 days, let retention purge everything past 30, and every additive total
-- per day, per project, per outcome, the Prometheus counters -- is what it was before the purge. Then the
same after a restart (which re-reads old spans that retention hasn't removed yet) and after days go by.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agentdynamics import config, store  # noqa: E402
from agentdynamics.engine import Engine  # noqa: E402
from agentdynamics.server import Api  # noqa: E402

DAY = 86400
ADDITIVE = "tasks,cost,tokens,output_tokens,tool_calls,tool_errors,waste"
RATIOS = "avg_cost,avg_duration,avg_score,error_rate,rework_rate,cache_hit"


def run(rid, t0, project, failed=False, calls=1):
    steps = [{"kind": "prompt", "ts": t0, "text": f"request {rid}"},
             {"kind": "llm", "ts": t0, "end_ts": t0 + 20, "model": "claude-sonnet-5", "input_tokens": 400 + calls * 50,
              "output_tokens": 60, "cache_read_tokens": 100, "stop_reason": "end_turn"}]
    steps += [{"kind": "tool", "ts": t0 + 20 + i, "end_ts": t0 + 21 + i, "name": "lookup", "is_error": failed and i == 0}
              for i in range(calls)]
    return {"id": rid, "project": project, "workflow": f"{project}_flow", "status": "error" if failed else "ok",
            "error": "boom" if failed else None, "steps": steps}


def otlp_trace(tid, t0):
    attrs = [{"key": k, "value": v} for k, v in (
        ("gen_ai.operation.name", {"stringValue": "chat"}), ("gen_ai.request.model", {"stringValue": "claude-sonnet-5"}),
        ("gen_ai.usage.input_tokens", {"intValue": "300"}), ("gen_ai.usage.output_tokens", {"intValue": "30"}))]
    return json.dumps({"resourceSpans": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "otel-agent"}}]}, "scopeSpans": [{"spans": [{
            "traceId": tid, "spanId": "ab" * 8, "name": "chat", "status": {}, "attributes": attrs,
            "startTimeUnixNano": str(int(t0 * 1e9)), "endTimeUnixNano": str(int((t0 + 5) * 1e9))}]}]}]}).encode()


class RollupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.now = time.time()
        self.offset = 0.0

    def engine(self, days=30):
        cfg = config.load(self.tmp)
        cfg["retention"]["days"] = days
        e = Engine(self.tmp, None, cfg=cfg)
        e._clock = lambda: self.now + self.offset
        self.addCleanup(e.con.close)
        return e

    def seed(self, e):
        for d in range(60):
            for i, project in enumerate(("shop", "shop", "billing")):
                t0 = self.now - d * DAY - 3600 * (i + 1)
                e.ingest(run(f"d{d}-{i}", t0, project, failed=(d + i) % 4 == 0, calls=1 + (d + i) % 3))
        e.ingest_otlp(otlp_trace("0e" * 16, self.now - 45 * DAY), "application/json")   # old, but stored today

    def totals(self, e, projects=None):
        """Everything that must survive a purge unchanged."""
        api = Api(e, projects=projects) if projects is not None else Api(e)
        try:
            out = {}
            for group in ("day", "week", "project", "outcome", "task_type", "source"):
                for metrics in (ADDITIVE, RATIOS):
                    r = api.analytics({"group": group, "metrics": metrics, "days": "", "sub": "1"})
                    out[(group, metrics)] = [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in row.items()}
                                             for row in r["rows"]]
            out["daily"] = [(d["day"], d["tasks"], round(d["cost"], 6), d["tokens"], d["errors"])
                            for d in api.overview({"days": ""})["daily"]]
            if projects is None:
                out["metrics"] = sorted(ln for ln in api.prometheus().splitlines()
                                        if re.match(r"agentdynamics_(tasks|cost_usd|tokens|tool_calls|tool_errors)_total\{", ln))
            return out
        finally:
            if projects is not None:
                api.close()

    def assertSameTotals(self, before, after):
        for k in before:
            self.assertEqual(before[k], after[k], f"{k} changed")

    def test_a_purge_changes_no_total(self):
        e = self.engine()
        self.seed(e)
        e.refresh(force=True)                        # first refresh: never purges
        self.assertEqual(e.con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 181)
        before = self.totals(e)
        e.refresh()                                  # retention: roll up, then purge
        held = e.con.execute("SELECT COUNT(*), MIN(ended) FROM runs").fetchone()
        self.assertLess(held[0], 100, "retention should have purged the older half")
        self.assertGreaterEqual(held[1], self.now - 30 * DAY)
        self.assertGreater(e.con.execute("SELECT COUNT(DISTINCT day) FROM rollup_daily").fetchone()[0], 28)
        self.assertSameTotals(before, self.totals(e))
        # a grouping rollups can't express (per-task detail) covers exactly the tasks still held
        held = e.con.execute("SELECT COUNT(*) FROM tasks WHERE llm_calls > 0 OR tool_calls > 0").fetchone()[0]
        by_hour = Api(e).analytics({"group": "hour", "metrics": "tasks", "days": "", "sub": "1"})
        self.assertFalse(by_hour["history"]["included"])
        self.assertEqual(sum(r["tasks"] for r in by_hour["rows"]), held)
        overview = Api(e).overview({"days": ""})
        self.assertGreater(overview["history"]["tasks"], 80)
        self.assertEqual(overview["history"]["through"], store.rollup_boundary(e.con)[0])

    @unittest.skipIf(os.environ.get("AGENTDYNAMICS_DB_URL"), "SDK runs are files only in a SQLite store")
    def test_expired_sdk_run_files_are_deleted(self):
        e = self.engine()
        self.seed(e)
        e.refresh(force=True)
        e.refresh()
        files = os.listdir(e.runs_dir)
        self.assertIn("d0-0.json", files)
        self.assertNotIn("d59-0.json", files)
        self.assertLess(len(files), 100)

    def test_a_restart_and_the_days_after_it_change_no_total(self):
        e = self.engine()
        self.seed(e)
        e.refresh(force=True)
        before = self.totals(e)
        e.refresh()
        e.con.close()
        # The old OTLP trace's spans were stored today, so they outlive its purge from the analysis, and a
        # restart's first refresh analyses it again, into a day that is already rolled up.
        e = self.engine()
        e.refresh(force=True)
        self.assertEqual(e.con.execute("SELECT COUNT(*) FROM tasks WHERE source = 'otlp'").fetchone()[0], 1)
        self.assertSameTotals(before, self.totals(e))
        e.refresh()
        self.assertSameTotals(before, self.totals(e))
        frozen = e.con.execute("SELECT COUNT(DISTINCT day) FROM rollup_daily").fetchone()[0]
        self.offset = 2 * DAY + 60                   # two more days reach the cutoff
        e.refresh()
        self.assertEqual(e.con.execute("SELECT COUNT(DISTINCT day) FROM rollup_daily").fetchone()[0], frozen + 2)
        self.assertSameTotals(before, self.totals(e))

    def test_a_backfill_older_than_retention_is_dropped(self):
        e = self.engine()
        self.seed(e)
        e.refresh(force=True)
        e.refresh()
        before = self.totals(e)
        e.ingest(run("late", self.now - 50 * DAY, "shop"))
        e.refresh()
        self.assertEqual(e.con.execute("SELECT COUNT(*) FROM runs WHERE id = 'late'").fetchone()[0], 0)
        self.assertSameTotals(before, self.totals(e))

    def test_a_scoped_key_sees_only_its_projects_history(self):
        e = self.engine()
        self.seed(e)
        e.refresh(force=True)
        before = self.totals(e, projects=["billing"])
        e.refresh()
        after = self.totals(e, projects=["billing"])
        self.assertSameTotals(before, after)
        self.assertEqual([r["grp"] for r in after[("project", ADDITIVE)]], ["billing"])

    def test_without_retention_nothing_is_rolled_up_or_purged(self):
        e = self.engine(days=0)
        self.seed(e)
        e.refresh(force=True)
        e.refresh()
        self.assertEqual(e.con.execute("SELECT COUNT(*) FROM rollup_daily").fetchone()[0], 0)
        self.assertEqual(e.con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 181)
        self.assertIsNone(Api(e).overview({"days": ""})["history"])


if __name__ == "__main__":
    unittest.main()
