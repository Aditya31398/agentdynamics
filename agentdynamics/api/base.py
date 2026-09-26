"""What every endpoint shares: the read connection, filtering, and the KPI and daily roll-ups."""
import statistics
import threading
import time
from collections import Counter, defaultdict

from ..analysis import apdex_score, pct
from ..store import connect_reader, rollup_boundary, rows

DAY = 86400



def mcp_group(name):
    if name and name.startswith("mcp__"):
        parts = name.split("__")
        return f"MCP · {parts[1]}" if len(parts) > 2 else name
    return name



class ApiBase:
    def __init__(self, engine, projects=None):
        self.e = engine
        self._local = threading.local()
        # None: the whole install. A list: a project-scoped key's view, enforced by the connection itself
        # (store.connect_reader), so no endpoint has to remember to filter.
        self.projects = sorted(set(projects)) if projects is not None else None

    @property
    def con(self):
        """One read-only connection per server thread (WAL lets readers run while the engine writes)."""
        c = getattr(self._local, "con", None)
        if c is None:
            c = self._local.con = connect_reader(self.e.db_path, self.projects)
        return c

    def close(self):
        c = getattr(self._local, "con", None)
        if c is not None:
            c.close()
            self._local.con = None

    def where(self, q, alias="t", subagents_default="0"):
        clauses, args = [], []
        if q.get("project"):
            clauses.append(f"{alias}.project = ?")
            args.append(q["project"])
        if q.get("days"):
            clauses.append(f"{alias}.started >= ?")
            args.append(time.time() - float(q["days"]) * DAY)
        if q.get("source"):
            clauses.append(f"{alias}.source = ?")
            args.append(q["source"])
        if q.get("sub", subagents_default) == "0":
            clauses.append(f"{alias}.is_subagent = 0")
        if q.get("type"):
            clauses.append(f"{alias}.task_type = ?")
            args.append(q["type"])
        for f in ("environment", "workflow", "framework"):
            if q.get(f):
                clauses.append(f"{alias}.{f} = ?")
                args.append(q[f])
        clauses.append(f"({alias}.llm_calls > 0 OR {alias}.tool_calls > 0)")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def tasks(self, q, extra="", extra_args=(), cols="t.*"):
        w, a = self.where(q)
        return rows(self.con, f"SELECT {cols} FROM tasks t{w}{extra}", a + list(extra_args))

    @staticmethod
    def kpis(ts):
        n = len(ts)
        cost = sum(t["cost"] + (t["subagent_cost"] or 0) for t in ts)
        calls = sum(t["tool_calls"] for t in ts)
        code = [t for t in ts if t["code_changed"]]
        scores = [t["score"] for t in ts if t["score"] is not None]
        ok = [t for t in ts if t["outcome"] == "completed"]
        return {
            "tasks": n,
            "sessions": len({t["run_id"] for t in ts}),
            "cost": round(cost, 4),
            "tokens": sum(t["total_tokens"] for t in ts),
            "output_tokens": sum(t["output_tokens"] for t in ts),
            "avg_cost": round(cost / n, 4) if n else 0,
            "median_cost": round(pct([t["cost"] + (t["subagent_cost"] or 0) for t in ts], 0.5) or 0, 4),
            "avg_duration": round(statistics.mean([t["duration_s"] for t in ts]), 1) if n else 0,
            "apdex": apdex_score([t for t in ts if t["apdex"]]),
            "success_rate": round(len(ok) / n, 3) if n else None,
            "tool_calls": calls,
            "tool_error_rate": round(sum(t["tool_errors"] for t in ts) / calls, 4) if calls else 0,
            "waste_cost": round(sum(t["waste_cost"] or 0 for t in ts), 4),
            "verification_rate": round(sum(1 for t in code if t["verified"]) / len(code), 3) if code else None,
            "avg_score": round(statistics.mean(scores), 1) if scores else None,
            "cache_hit": round(sum(t["cache_read"] for t in ts) / max(1, sum(t["cache_read"] + t["cache_write"] + t["input_tokens"] for t in ts)), 3),
            "rework_rate": round(sum(1 for t in ts if t["outcome"] in ("rework", "interrupted")) / n, 3) if n else None,
            # agent-flow KPIs
            "failed_rate": round(sum(1 for t in ts if t["outcome"] == "failed") / n, 3) if n else None,
            "avg_steps": round(statistics.mean([t["steps_total"] or 0 for t in ts]), 1) if n else 0,
            "loop_rate": round(sum(1 for t in ts if (t["max_node_visits"] or 0) >= 5) / n, 3) if n else 0,
            "truncations": sum(t["truncations"] or 0 for t in ts),
            "refusals": sum(t["refusals"] or 0 for t in ts),
            "rate_limited": sum(t["rate_limited"] or 0 for t in ts),
            "llm_errors": sum(t["llm_errors"] or 0 for t in ts),
            "handoffs": sum(t["handoffs"] or 0 for t in ts),
            "ttft_p50": pct([t["ttft_ms"] for t in ts if t["ttft_ms"]], 0.5),
            "p95_wall": round(pct([t["wall_s"] or 0 for t in ts], 0.95) or 0, 1),
            "feedback_avg": round(statistics.mean(fb), 3) if (fb := [t["feedback_score"] for t in ts if t["feedback_score"] is not None]) else None,
            "unpriced": sum(t["unpriced"] or 0 for t in ts),
            "tokens_unverified": sum(t.get("tokens_unverified") or 0 for t in ts),
            # how much of success_rate / apdex is stated rather than guessed
            "outcomes_by_source": dict(Counter(t.get("outcome_source") or "inferred" for t in ts)),
        }

    @staticmethod
    def health(apdex):
        if apdex is None:
            return "unknown"
        return "normal" if apdex >= 0.85 else "warning" if apdex >= 0.7 else "critical"

    # ---------------------------------------------------------------- history past retention
    # Retention purges tasks but keeps their daily totals (store.rollup_daily). Every day up to the boundary
    # is counted from those totals and never from tasks, which may still hold part of the last rolled-up day.

    def boundary(self):
        """(last rolled-up day or None, the time it ends)."""
        return rollup_boundary(self.con)

    def rollup_where(self, q, alias="r"):
        """where() for rollup_daily: the same filters, applied to whole days."""
        clauses, args = [], []
        for key, col in (("project", "project"), ("source", "source"), ("type", "task_type"),
                         ("environment", "environment"), ("workflow", "workflow"), ("framework", "framework")):
            if q.get(key):
                clauses.append(f"{alias}.{col} = ?")
                args.append(q[key])
        if q.get("days"):
            clauses.append(f"{alias}.day >= ?")
            args.append(time.strftime("%Y-%m-%d", time.localtime(time.time() - float(q["days"]) * DAY)))
        if q.get("sub", "0") == "0":
            clauses.append(f"{alias}.is_subagent = 0")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def history_daily(self, q):
        """Daily totals from rollup_daily, shaped like daily() and marked rolled_up."""
        if not self.boundary()[0]:
            return []
        w, a = self.rollup_where(q)
        out = []
        for r in rows(self.con, "SELECT day, SUM(tasks) tasks, SUM(cost + subagent_cost) cost, SUM(total_tokens) tokens, "
                                "SUM(apdex_satisfied) sat, SUM(apdex_tolerating) tol, SUM(apdex_frustrated) fr, "
                                f"SUM(tool_errors) errors, SUM(score_sum) ss, SUM(score_n) sn FROM rollup_daily r{w} "
                                "GROUP BY day ORDER BY day", a):
            rated = (r["sat"] or 0) + (r["tol"] or 0) + (r["fr"] or 0)
            out.append({"day": r["day"], "tasks": r["tasks"], "cost": round(r["cost"] or 0, 4), "tokens": r["tokens"] or 0,
                        "apdex": round((r["sat"] + r["tol"] / 2) / rated, 3) if rated else None,
                        "errors": r["errors"] or 0, "score": round(r["ss"] / r["sn"], 1) if r["sn"] else 0,
                        "rolled_up": True})
        return out

    def daily(self, ts, q=None):
        """Per-day totals of `ts`. With `q`, days that retention rolled up come from rollup_daily instead."""
        hist, since = [], 0
        if q is not None:
            hist, since = self.history_daily(q), self.boundary()[1]
        by = defaultdict(list)
        for t in ts:
            if t["started"] and t["started"] >= since:
                by[time.strftime("%Y-%m-%d", time.localtime(t["started"]))].append(t)
        out = hist
        for d in sorted(by):
            g = by[d]
            out.append({"day": d, "tasks": len(g), "cost": round(sum(t["cost"] + (t["subagent_cost"] or 0) for t in g), 4),
                        "tokens": sum(t["total_tokens"] for t in g), "apdex": apdex_score([t for t in g if t["apdex"]]),
                        "errors": sum(t["tool_errors"] for t in g),
                        "score": round(statistics.mean([t["score"] for t in g if t["score"] is not None] or [0]), 1)})
        return out

    @staticmethod
    def _task_brief(t):
        keys = ["id", "run_id", "project", "task_type", "prompt", "started", "duration_s", "cost", "subagent_cost", "outcome",
                "apdex", "score", "tool_calls", "tool_errors", "llm_calls", "total_tokens", "cost_vs_baseline", "is_subagent",
                "waste_cost", "verified", "models", "max_context", "workflow", "environment", "framework", "wall_s",
                "steps_total", "max_node_visits", "feedback_score"]
        d = {k: t.get(k) for k in keys}
        d["prompt"] = (d["prompt"] or "")[:160]
        return d
