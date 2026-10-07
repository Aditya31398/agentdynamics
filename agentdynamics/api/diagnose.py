"""Diagnose: tools, models and health events."""
import calendar
import statistics
import time
from collections import Counter, defaultdict

from ..analysis import REFUSAL as REFUSE, TRUNCATION as TRUNC, error_cause, pct
from ..store import rows



from .base import DAY, mcp_group


class DiagnoseMixin:
    def tools(self, q):
        cond, args = self.where(dict(q, sub=q.get("sub", "1")))
        st = rows(self.con, f"""SELECT s.name, s.phase, s.duration_ms, s.is_error, s.output_chars, s.attributed_cost, s.flags, s.error, s.task_id
            FROM steps s JOIN tasks t ON t.id = s.task_id{cond} AND s.kind = 'tool' ORDER BY t.started, t.id, s.seq""", args)
        by = defaultdict(list)
        for s in st:
            by[s["name"]].append(s)
        out = []
        for name, g in by.items():
            ms = [s["duration_ms"] for s in g if s["duration_ms"] is not None]
            errs = [s for s in g if s["is_error"]]
            flags = Counter(f for s in g for f in (s["flags"] or []))
            out.append({"name": name, "group": mcp_group(name), "phase": Counter(s["phase"] for s in g).most_common(1)[0][0],
                        "calls": len(g), "errors": len(errs), "error_rate": round(len(errs) / len(g), 3),
                        "avg_ms": round(statistics.mean(ms)) if ms else None, "p95_ms": round(pct(ms, 0.95)) if ms else None,
                        "avg_output_chars": round(statistics.mean([s["output_chars"] or 0 for s in g])),
                        "output_tokens_est": round(sum(s["output_chars"] or 0 for s in g) / 4),
                        "cost": round(sum(s["attributed_cost"] or 0 for s in g), 4), "flags": dict(flags),
                        "causes": dict(Counter(error_cause(s["error"]) for s in errs).most_common()),
                        "sample_errors": [{"error": (s["error"] or "")[:200], "task_id": s["task_id"]} for s in errs[-4:]]})
        out.sort(key=lambda x: -x["calls"])
        return {"tools": out}

    def billing(self, q):
        """Estimated against billed, per day and model (UTC days, the provider's). Only the days the bill and the held
        steps both cover. The bill is the organization's: a key scoped to projects gets nothing from it (an empty
        answer, not a refusal, so the Models page loads clean for it)."""
        from .. import pricing
        if self.projects is not None:
            return {"configured": False, "scoped": True, "days": [], "models": [], "other": {}, "totals": None,
                    "fetched": None}
        days = int(q.get("days") or 30)
        since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
        billed = rows(self.con, "SELECT day, model, cost_type, token_type, SUM(usd) usd, MAX(fetched) fetched "
                                "FROM billing_daily WHERE provider = 'anthropic' AND day >= ? "
                                "GROUP BY day, model, cost_type, token_type ORDER BY day, model, cost_type, token_type", (since,))
        if not billed:
            return {"configured": any(sc.get("type") == "anthropic_costs" for sc in self.e.cfg.get("sources") or []),
                    "days": [], "models": [], "other": {}, "totals": None, "fetched": None}
        first = min(r["day"] for r in billed)
        start = calendar.timegm(time.strptime(first, "%Y-%m-%d"))
        est = defaultdict(float)
        for s in rows(self.con, "SELECT s.model, s.ts, s.cost FROM steps s WHERE s.kind = 'llm' AND s.ts >= ? "
                                "AND s.cost > 0 ORDER BY s.ts", (start,)):
            key = pricing.entry(s["model"])
            if key:                                   # an Anthropic model the price table knows
                est[(time.strftime("%Y-%m-%d", time.gmtime(s["ts"])), key)] += s["cost"]
        bill, other = defaultdict(float), defaultdict(float)
        for r in billed:
            if r["cost_type"] == "tokens":
                bill[(r["day"], pricing.entry(r["model"]) or r["model"] or "other")] += r["usd"]
            else:
                other[r["cost_type"] or "other"] += r["usd"]
        # only the days both cover: from the first the bill and the held steps both reach, to the bill's last
        lo = max(first, min((d for d, _ in est), default=first))
        hi = max(r["day"] for r in billed)
        keys = {k for k in set(bill) | set(est) if lo <= k[0] <= hi}

        def line(b, e):
            return {"billed": round(b, 4), "estimated": round(e, 4), "gap": round(b - e, 4),
                    "gap_share": round((b - e) / b, 4) if b else None}
        by_day, by_model = defaultdict(lambda: [0.0, 0.0]), defaultdict(lambda: [0.0, 0.0])
        for k in keys:
            for agg, kk in ((by_day, k[0]), (by_model, k[1])):
                agg[kk][0] += bill.get(k, 0.0)
                agg[kk][1] += est.get(k, 0.0)
        tb, te = sum(v[0] for v in by_day.values()), sum(v[1] for v in by_day.values())
        return {"configured": True, "fetched": max(r["fetched"] or 0 for r in billed),
                "days": [dict(day=d, **line(*by_day[d])) for d in sorted(by_day)],
                "models": sorted((dict(model=m, **line(*by_model[m])) for m in by_model), key=lambda x: (-x["billed"], x["model"])),
                "other": {k: round(v, 4) for k, v in sorted(other.items())}, "totals": line(tb, te)}

    def models(self, q):
        cond, args = self.where(dict(q, sub=q.get("sub", "1")))
        st = rows(self.con, f"""SELECT s.model, s.ts, s.duration_ms, s.cost, s.input_tokens, s.output_tokens, s.cache_read, s.cache_write,
            s.context_tokens, s.thinking_tokens, s.effort, s.stop_reason, s.is_error, s.rate_limited, s.ttft_ms FROM steps s JOIN tasks t ON t.id = s.task_id{cond} AND s.kind = 'llm'
            AND s.model != '<synthetic>' ORDER BY t.started, t.id, s.seq""", args)
        by = defaultdict(list)
        for s in st:
            by[s["model"]].append(s)
        out = []
        daily = defaultdict(lambda: defaultdict(float))
        for m, g in by.items():
            ms = [s["duration_ms"] for s in g if s["duration_ms"] is not None]
            inp = sum(s["input_tokens"] + s["cache_read"] + s["cache_write"] for s in g)
            for s in g:
                if s["ts"]:
                    daily[time.strftime("%Y-%m-%d", time.localtime(s["ts"]))][m] += s["cost"] or 0
            out.append({"model": m, "calls": len(g), "cost": round(sum(s["cost"] for s in g), 4),
                        "input_tokens": sum(s["input_tokens"] for s in g), "output_tokens": sum(s["output_tokens"] for s in g),
                        "cache_read": sum(s["cache_read"] for s in g), "cache_write": sum(s["cache_write"] for s in g),
                        "thinking_tokens": sum(s["thinking_tokens"] or 0 for s in g),
                        "cache_hit": round(sum(s["cache_read"] for s in g) / inp, 3) if inp else None,
                        "avg_ms": round(statistics.mean(ms)) if ms else None, "p95_ms": round(pct(ms, 0.95)) if ms else None,
                        "avg_context": round(statistics.mean([s["context_tokens"] for s in g])),
                        "max_context": max(s["context_tokens"] for s in g),
                        "avg_output": round(statistics.mean([s["output_tokens"] for s in g])),
                        "effort": dict(Counter(s["effort"] for s in g if s["effort"])),
                        "errors": sum(1 for s in g if s["is_error"]),
                        "rate_limited": sum(1 for s in g if s["rate_limited"]),
                        "truncation_rate": round(sum(1 for s in g if s["stop_reason"] in TRUNC) / len(g), 4),
                        "refusals": sum(1 for s in g if s["stop_reason"] in REFUSE),
                        "ttft_p50": pct([s["ttft_ms"] for s in g if s["ttft_ms"]], 0.5),
                        "ttft_p95": pct([s["ttft_ms"] for s in g if s["ttft_ms"]], 0.95),
                        "tps_p50": pct([s["output_tokens"] / (s["duration_ms"] / 1000) for s in g
                                        if s["duration_ms"] and s["output_tokens"] > 20], 0.5)})
        out.sort(key=lambda x: -x["cost"])
        return {"models": out, "daily": [{"day": d, **{k: round(v, 4) for k, v in daily[d].items()}} for d in sorted(daily)]}

    def events(self, q):
        clauses, args = ["1=1"], []
        for f in ("severity", "rule_id", "project", "task_type"):
            if q.get(f):
                clauses.append(f"{f} = ?")
                args.append(q[f])
        if q.get("days"):
            clauses.append("ts >= ?")
            args.append(time.time() - float(q["days"]) * DAY)
        ev = rows(self.con, f"SELECT e.*, t.prompt FROM events e LEFT JOIN tasks t ON t.id = e.task_id WHERE {' AND '.join(clauses).replace('project', 'e.project').replace('task_type', 'e.task_type')} ORDER BY e.ts DESC, e.id LIMIT 500", args)
        for e in ev:
            e["prompt"] = (e["prompt"] or "")[:140]
        rule_counts = rows(self.con, "SELECT rule_id, MAX(rule) rule, MAX(severity) severity, COUNT(*) n FROM events GROUP BY rule_id ORDER BY n DESC, rule_id")
        return {"events": ev, "rules": rule_counts}
