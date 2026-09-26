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

    # Every metric is built from sums, so it can be computed over tasks and rollup_daily alike. FACTS gives
    # each sum's per-task expression and its column in rollup_daily.
    FACTS = {"n": ("1", "tasks"), "cost": ("t.cost + t.subagent_cost", "cost + subagent_cost"),
             "total_tokens": ("t.total_tokens", "total_tokens"), "output_tokens": ("t.output_tokens", "output_tokens"),
             "input_tokens": ("t.input_tokens", "input_tokens"), "cache_read": ("t.cache_read", "cache_read"),
             "cache_write": ("t.cache_write", "cache_write"), "duration_s": ("t.duration_s", "duration_s"),
             "duration_n": ("t.duration_s IS NOT NULL", "tasks"),
             "score": ("t.score", "score_sum"), "score_n": ("t.score IS NOT NULL", "score_n"),
             "tool_calls": ("t.tool_calls", "tool_calls"), "tool_errors": ("t.tool_errors", "tool_errors"),
             "waste": ("t.waste_cost", "waste_cost"),
             "reworked": ("t.outcome IN ('rework','interrupted')",
                          "CASE WHEN outcome IN ('rework','interrupted') THEN tasks ELSE 0 END"),
             "code_changed": ("t.code_changed = 1", "code_changed"),
             "verified": ("CASE WHEN t.code_changed = 1 THEN t.verified END", "verified"),
             "context": ("t.max_context", "max_context"), "context_n": ("t.max_context IS NOT NULL", "tasks")}

    METRICS = {"tasks": "SUM(n)", "cost": "SUM(cost)", "avg_cost": "1.0 * SUM(cost) / SUM(n)",
               "tokens": "SUM(total_tokens)", "output_tokens": "SUM(output_tokens)",
               "avg_duration": "1.0 * SUM(duration_s) / NULLIF(SUM(duration_n), 0)",
               "avg_score": "1.0 * SUM(score) / NULLIF(SUM(score_n), 0)", "tool_calls": "SUM(tool_calls)",
               "tool_errors": "SUM(tool_errors)", "error_rate": "1.0 * SUM(tool_errors) / MAX(1, SUM(tool_calls))",
               "waste": "SUM(waste)", "rework_rate": "1.0 * SUM(reworked) / SUM(n)",
               "verification_rate": "1.0 * SUM(verified) / NULLIF(SUM(code_changed), 0)",
               "avg_context": "1.0 * SUM(context) / NULLIF(SUM(context_n), 0)",
               "cache_hit": "1.0 * SUM(cache_read) / MAX(1, SUM(cache_read + cache_write + input_tokens))"}
    # the same groups over rollup_daily; a group it can't express (per-task detail) is live tasks only
    ROLLUP_GROUPS = {"task_type": "r.task_type", "project": "r.project", "outcome": "r.outcome", "source": "r.source",
                     "day": "r.day", "week": "strftime('%Y-W%W', r.day)"}

    def analytics(self, q):
        group = q.get("group", "task_type")
        g = self.GROUPS.get(group, "t.task_type")
        metrics = [m for m in (q.get("metrics") or "tasks,cost,avg_cost,avg_score").split(",") if m in self.METRICS]
        through, through_end = self.boundary()
        w, a = self.where(q)
        live = ", ".join(f"{expr} AS {k}" for k, (expr, _) in self.FACTS.items())
        facts, args = f"SELECT {g} AS grp, {live} FROM tasks t{w}", list(a)
        history = bool(through) and group in self.ROLLUP_GROUPS
        if history:
            # rolled-up days are counted from their totals, so not again from tasks still held for them
            rw, ra = self.rollup_where(q)
            old = ", ".join(f"{col} AS {k}" for k, (_, col) in self.FACTS.items())
            facts += f" AND t.started >= ? UNION ALL SELECT {self.ROLLUP_GROUPS[group]} AS grp, {old} FROM rollup_daily r{rw}"
            args += [through_end] + ra
        sel = ", ".join(f"{self.METRICS[m]} AS {m}" for m in metrics)
        order = "grp" if group in ("day", "week", "hour") else f"{metrics[0]} DESC"
        data = rows(self.con, f"SELECT grp, {sel} FROM ({facts}) GROUP BY grp ORDER BY {order} LIMIT 200", args)
        return {"rows": data, "metrics": metrics, "group": group,
                # a grouping by per-task detail (model, hour, ...) can only cover the tasks still held
                "history": {"through": through, "included": history} if through else None,
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
        from ..store import alert_state
        firing = alert_state(self.con)
        now, out = time.time(), []
        for s in slos:
            r = slo.evaluate(ts, s)
            r["burn"] = slo.burn_rates(ts, s, now)
            r["burn_thresholds"] = {f"{p['long_h']}h": round(slo.burn_threshold(s, p), 2) for p in slo.BURN_POLICIES
                                    if s["metric"] in slo.RATIO and p["long_h"] <= s["window_days"] * 24}
            # an alert is judged on every project's tasks; a scoped key sees one only on its own project's SLO
            mine = self.projects is None or (s.get("scope") or {}).get("project") in self.projects
            r["alerts"] = [{"alert": v.get("alert"), "severity": v.get("severity"), "since": v["since"],
                            "burn_rate": v.get("burn_rate"), "policy": v.get("policy")}
                           for k, v in firing.items() if mine and v.get("slo") == s["id"]]
            out.append(r)
        return {"slos": out}
