"""Assess: process review, analytics, compare and SLOs."""
import json
import statistics
import time
from collections import Counter, defaultdict

from ..store import rows



from .base import DAY


class AssessMixin:
    def process(self, q):
        ts = self.tasks(q)
        meta = {r["k"]: json.loads(r["v"]) for r in self.con.execute("SELECT k, v FROM meta")}
        from ..analysis import process_insights
        # the stored insights are install-wide: recompute for any filter, and always for a scoped reader
        insights = process_insights(ts) if (q.get("project") or q.get("days") or q.get("type") or self.projects is not None) else meta.get("insights", [])
        dims = ["efficiency", "focus", "reliability", "verification", "context", "autonomy", "compliance", "overall"]
        avg = {}
        for d in dims:
            v = [t["scores"].get(d) for t in ts if t["scores"] and t["scores"].get(d) is not None]
            avg[d] = round(statistics.mean(v), 1) if v else None
        by = defaultdict(list)
        for t in ts:
            by[t["task_type"]].append(t)
        per_type = []
        for ty, g in by.items():
            row = {"type": ty, "n": len(g)}
            for d in dims:
                v = [t["scores"].get(d) for t in g if t["scores"] and t["scores"].get(d) is not None]
                row[d] = round(statistics.mean(v), 1) if v else None
            ph = Counter()
            for t in g:
                ph.update(t["phase_cost"] or {})
            tot = sum(ph.values()) or 1
            row["phase_mix"] = {k: round(v / tot, 3) for k, v in ph.items()}
            per_type.append(row)
        per_type.sort(key=lambda r: -r["n"])
        worst = sorted([t for t in ts if t["score"] is not None], key=lambda t: t["score"])[:10]
        return {"insights": insights, "avg": avg, "per_type": per_type, "daily": self.daily(ts),
                "worst": [self._task_brief(t) | {"scores": t["scores"]} for t in worst]}

    GROUPS = {"task_type": "t.task_type", "project": "t.project", "outcome": "t.outcome", "apdex": "t.apdex",
              "day": "date(t.started, 'unixepoch', 'localtime')", "week": "strftime('%Y-W%W', t.started, 'unixepoch', 'localtime')",
              "models": "t.models", "source": "t.source", "prompt_kind": "t.prompt_kind", "hour": "strftime('%H', t.started, 'unixepoch', 'localtime')"}

    METRICS = {"tasks": "COUNT(*)", "cost": "SUM(t.cost + t.subagent_cost)", "avg_cost": "AVG(t.cost + t.subagent_cost)",
               "tokens": "SUM(t.total_tokens)", "output_tokens": "SUM(t.output_tokens)", "avg_duration": "AVG(t.duration_s)",
               "avg_score": "AVG(t.score)", "tool_calls": "SUM(t.tool_calls)", "tool_errors": "SUM(t.tool_errors)",
               "error_rate": "1.0 * SUM(t.tool_errors) / MAX(1, SUM(t.tool_calls))", "waste": "SUM(t.waste_cost)",
               "rework_rate": "AVG(CASE WHEN t.outcome IN ('rework','interrupted') THEN 1.0 ELSE 0 END)",
               "verification_rate": "AVG(CASE WHEN t.code_changed = 1 THEN t.verified END)", "avg_context": "AVG(t.max_context)",
               "cache_hit": "1.0 * SUM(t.cache_read) / MAX(1, SUM(t.cache_read + t.cache_write + t.input_tokens))"}

    def analytics(self, q):
        g = self.GROUPS.get(q.get("group", "task_type"), "t.task_type")
        metrics = [m for m in (q.get("metrics") or "tasks,cost,avg_cost,avg_score").split(",") if m in self.METRICS]
        w, a = self.where(q)
        sel = ", ".join(f"{self.METRICS[m]} AS {m}" for m in metrics)
        order = "grp" if q.get("group") in ("day", "week", "hour") else f"{metrics[0]} DESC"
        data = rows(self.con, f"SELECT {g} AS grp, {sel} FROM tasks t{w} GROUP BY grp ORDER BY {order} LIMIT 200", a)
        return {"rows": data, "metrics": metrics, "group": q.get("group", "task_type"),
                "available": {"groups": list(self.GROUPS), "metrics": list(self.METRICS)}}

    def compare(self, q):
        dim = q.get("dim", "models")
        col = {"models": "models", "task_type": "task_type", "project": "project", "source": "source",
               "policy": "policy_version", "workflow": "workflow"}.get(dim)
        ts = self.tasks(q)

        def pick(val):
            if dim == "period":
                lo, hi = val.split("..")
                lo_t = time.mktime(time.strptime(lo, "%Y-%m-%d"))
                hi_t = time.mktime(time.strptime(hi, "%Y-%m-%d")) + DAY
                return [t for t in ts if t["started"] and lo_t <= t["started"] < hi_t]
            return [t for t in ts if (t[col] or "") == val]

        res = {}
        for side in ("a", "b"):
            if q.get(side):
                g = pick(q[side])
                k = self.kpis(g)
                ph = Counter()
                for t in g:
                    ph.update(t["phase_cost"] or {})
                tot = sum(ph.values()) or 1
                k["phase_mix"] = {p: round(v / tot, 3) for p, v in ph.items()}
                sc = defaultdict(list)
                for t in g:
                    for d, v in (t["scores"] or {}).items():
                        if v is not None:
                            sc[d].append(v)
                k["scores"] = {d: round(statistics.mean(v), 1) for d, v in sc.items()}
                res[side] = k
        options = sorted({t[col] for t in ts if t.get(col)}) if col else []
        return {"dim": dim, "options": options, **res}

    def slos(self, q):
        from .. import slo
        ts = self.tasks(dict(q, days=""))
        slos = slo.load(self.e.data_dir)
        if self.projects is not None:
            # SLOs are install-wide config, and one's name and scope describe what it covers: another
            # team's project or workflow. A scoped key sees those covering its projects or its own tasks.
            def covers(s):
                sc = {k: v for k, v in (s.get("scope") or {}).items() if v}
                if "project" in sc:
                    return sc["project"] in self.projects
                return not sc or any(all((t.get(k) or "") == v for k, v in sc.items()) for t in ts)
            slos = [s for s in slos if covers(s)]
        return {"slos": [slo.evaluate(ts, s) for s in slos]}
