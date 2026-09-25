"""Diagnose: tools, models and health events."""
import statistics
import time
from collections import Counter, defaultdict

from ..analysis import REFUSAL as REFUSE, TRUNCATION as TRUNC, pct
from ..store import rows



from .base import DAY, mcp_group


class DiagnoseMixin:
    def tools(self, q):
        cond, args = self.where(dict(q, sub=q.get("sub", "1")))
        st = rows(self.con, f"""SELECT s.name, s.phase, s.duration_ms, s.is_error, s.output_chars, s.attributed_cost, s.flags, s.error, s.task_id
            FROM steps s JOIN tasks t ON t.id = s.task_id{cond} AND s.kind = 'tool'""", args)
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
                        "sample_errors": [{"error": (s["error"] or "")[:200], "task_id": s["task_id"]} for s in errs[-4:]]})
        out.sort(key=lambda x: -x["calls"])
        return {"tools": out}

    def models(self, q):
        cond, args = self.where(dict(q, sub=q.get("sub", "1")))
        st = rows(self.con, f"""SELECT s.model, s.ts, s.duration_ms, s.cost, s.input_tokens, s.output_tokens, s.cache_read, s.cache_write,
            s.context_tokens, s.thinking_tokens, s.effort, s.stop_reason, s.is_error, s.rate_limited, s.ttft_ms FROM steps s JOIN tasks t ON t.id = s.task_id{cond} AND s.kind = 'llm'
            AND s.model != '<synthetic>'""", args)
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
        ev = rows(self.con, f"SELECT e.*, t.prompt FROM events e LEFT JOIN tasks t ON t.id = e.task_id WHERE {' AND '.join(clauses).replace('project', 'e.project').replace('task_type', 'e.task_type')} ORDER BY e.ts DESC LIMIT 500", args)
        for e in ev:
            e["prompt"] = (e["prompt"] or "")[:140]
        rule_counts = rows(self.con, "SELECT rule_id, rule, severity, COUNT(*) n FROM events GROUP BY rule_id ORDER BY n DESC")
        return {"events": ev, "rules": rule_counts}
