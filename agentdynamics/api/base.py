"""What every endpoint shares: the read connection, filtering, and the KPI and daily roll-ups."""
import statistics
import threading
import time
from collections import Counter, defaultdict

from ..analysis import apdex_score, pct
from ..store import connect_reader, rows

DAY = 86400



def mcp_group(name):
    if name and name.startswith("mcp__"):
        parts = name.split("__")
        return f"MCP · {parts[1]}" if len(parts) > 2 else name
    return name



class ApiBase:
    def __init__(self, engine):
        self.e = engine
        self._local = threading.local()

    @property
    def con(self):
        """One read-only connection per server thread (WAL lets readers run while the engine writes)."""
        c = getattr(self._local, "con", None)
        if c is None:
            c = self._local.con = connect_reader(self.e.db_path)
        return c

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

    def daily(self, ts, days=None):
        by = defaultdict(list)
        for t in ts:
            if t["started"]:
                by[time.strftime("%Y-%m-%d", time.localtime(t["started"]))].append(t)
        out = []
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
