"""Operations: sources, connection snippets, config, alerts, health and Prometheus metrics."""
import time
from collections import Counter, defaultdict

from .. import alerts as alertmod, slo as slomod
from ..store import alert_state, outbox_depth, rows





class OpsMixin:
    def sources(self, q):
        src = [dict(s.d) for s in self.e.sources.values()]
        spans = rows(self.con, "SELECT source, COUNT(*) spans, COUNT(DISTINCT trace_id) traces, MAX(updated) last FROM spans_raw GROUP BY source")
        by = {r["source"]: r for r in spans}
        for s in src:
            if s["name"] in by:
                s.update({"spans": by[s["name"]]["spans"], "traces": by[s["name"]]["traces"], "last_data": by[s["name"]]["last"]})
                if s["status"] == "idle":
                    s["status"] = "ok"  # has stored data from before this process started
        runs = rows(self.con, "SELECT source, framework, COUNT(*) n, MAX(ended) last FROM runs GROUP BY source, framework")
        return {"sources": src, "runs_by_source": runs, "stats": self.e.stats, "auth": self.e.cfg["auth"]["enabled"]}

    def connect(self, q):
        from ..__main__ import SNIPPETS
        url = (q.get("url") or "http://127.0.0.1:8787").rstrip("/")
        return {"snippets": [{"id": k, "title": t, "body": b.format(url=url)} for k, (t, b) in SNIPPETS.items()],
                "auth": self.e.cfg["auth"]["enabled"]}

    def config(self, q):
        from ..config import public_view
        return {"config": public_view(self.e.cfg), "data_dir": self.e.data_dir, "db": self.e.db_path}

    def alerts(self, q):
        """Alert destinations with what is queued and recent delivery results, config problems, and the SLO
        alerts firing now."""
        dests, problems = alertmod.destinations(self.e.cfg["alerts"])
        depth = outbox_depth(self.con)
        out = []
        for d in dests:
            log = self.e.alert_log.get(d["id"], {})
            out.append({"id": d["id"], "format": d["format"], "kinds": d["kinds"], "min_severity": d["min_severity"],
                        "projects": d["projects"], "rules": d["rules"], "queued": depth.get(d["id"], 0),
                        "sent": log.get("sent", 0), "dropped": log.get("dropped", 0), "last_ok": log.get("last_ok"),
                        "last_error": log.get("last_error"), "last_error_at": log.get("last_error_at")})
        firing = sorted((dict(v, key=k) for k, v in alert_state(self.con).items()), key=lambda a: a["since"])
        return {"destinations": out, "problems": problems, "firing": firing,
                "console_url": self.e.cfg["alerts"].get("console_url") or "",
                "stats": {k: self.e.stats.get(k, 0) for k in ("alerts_sent", "alerts_retried", "alerts_dropped")}}

    def healthz(self):
        # "ok" has to mean ingestion is working now, not that it worked once. A refresh that
        # keeps throwing leaves last_refresh frozen, which used to read as healthy forever.
        if self.e.last_refresh is None:
            status = "starting"
        elif self.e.failed_refreshes:
            status = "degraded"
        else:
            status = "ok"
        return {"status": status, "last_refresh": self.e.last_refresh, "refresh_seconds": self.e.last_duration,
                "failed_refreshes": self.e.failed_refreshes, "last_refresh_error": self.e.last_refresh_error,
                "sources": {k: v.d["status"] for k, v in self.e.sources.items()}}

    def prometheus(self):
        lines = []

        def esc(v):
            return str(v).replace("\\", "").replace('"', "").replace("\n", " ")

        def m(name, help_, typ, samples):
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} {typ}")
            for labels, v in samples:
                lab = ",".join(f'{k}="{esc(val)}"' for k, val in labels.items())
                lines.append(f"{name}{{{lab}}} {v}")

        t = rows(self.con, "SELECT project, environment, task_type, outcome, COUNT(*) n, SUM(cost+subagent_cost) cost, SUM(tool_calls) calls, "
                           "SUM(tool_errors) errs, SUM(total_tokens) tok FROM tasks WHERE llm_calls>0 OR tool_calls>0 "
                           "GROUP BY project, environment, task_type, outcome")

        def lab(r):
            return {"project": r["project"], "environment": r["environment"], "task_type": r["task_type"], "outcome": r["outcome"]}

        m("agentdynamics_tasks_total", "Tasks observed", "counter", [(lab(r), r["n"]) for r in t])
        m("agentdynamics_cost_usd_total", "Model spend in USD (list price)", "counter", [(lab(r), round(r["cost"] or 0, 6)) for r in t])
        m("agentdynamics_tokens_total", "Tokens processed", "counter", [(lab(r), r["tok"] or 0) for r in t])
        m("agentdynamics_tool_calls_total", "Tool calls", "counter", [(lab(r), r["calls"] or 0) for r in t])
        m("agentdynamics_tool_errors_total", "Failed tool calls", "counter", [(lab(r), r["errs"] or 0) for r in t])
        ap = rows(self.con, "SELECT task_type, apdex, COUNT(*) n FROM tasks WHERE apdex IS NOT NULL GROUP BY task_type, apdex")
        by = defaultdict(Counter)
        for r in ap:
            by[r["task_type"]][r["apdex"]] += r["n"]
        m("agentdynamics_apdex", "Agent Apdex by task type", "gauge",
          [({"task_type": k}, round((c["satisfied"] + c["tolerating"] / 2) / max(1, sum(c.values())), 4)) for k, c in by.items()])
        ev = rows(self.con, "SELECT rule_id, severity, COUNT(*) n FROM events GROUP BY rule_id, severity")
        m("agentdynamics_health_events", "Open health-rule violations", "gauge", [({"rule": r["rule_id"], "severity": r["severity"]}, r["n"]) for r in ev])
        m("agentdynamics_spans_ingested_total", "Spans/runs accepted by push and pull ingestion", "counter", [({}, self.e.stats["spans_ingested"])])
        m("agentdynamics_last_refresh_timestamp_seconds", "Last successful analysis", "gauge", [({}, self.e.last_refresh or 0)])
        m("agentdynamics_refresh_duration_seconds", "Duration of last analysis", "gauge", [({}, self.e.last_duration or 0)])
        m("agentdynamics_refresh_failures", "Consecutive failed analysis passes (0 when healthy)", "gauge",
          [({}, self.e.failed_refreshes)])
        # SLO burn rates, so teams that page through Alertmanager can alert on the same numbers:
        # agentdynamics_slo_burn_rate > agentdynamics_slo_burn_threshold, per window
        now, slos = time.time(), slomod.load(self.e.data_dir)
        horizon = max(p["long_h"] for p in slomod.BURN_POLICIES) * 3600
        recent = rows(self.con, "SELECT * FROM tasks WHERE is_subagent = 0 AND (llm_calls > 0 OR tool_calls > 0) "
                                "AND COALESCE(ended, started) >= ?", (now - horizon,))
        burn, thr = [], []
        for s in slos:
            for w, b in slomod.burn_rates(recent, s, now).items():
                burn.append(({"slo": s["id"], "window": w}, b))
            if s["metric"] in slomod.RATIO:
                thr += [({"slo": s["id"], "window": f"{p['long_h']}h"}, round(slomod.burn_threshold(s, p), 4))
                        for p in slomod.BURN_POLICIES if p["long_h"] <= s["window_days"] * 24]
        m("agentdynamics_slo_burn_rate", "Error-budget burn rate over each window (1 = spent exactly over the SLO window)",
          "gauge", burn)
        m("agentdynamics_slo_burn_threshold", "Burn rate at which AgentDynamics alerts, per window", "gauge", thr)
        m("agentdynamics_slo_alert_firing", "SLO alerts firing now (1): page, ticket or breach", "gauge",
          [({"slo": v.get("slo"), "alert": v.get("alert")}, 1) for v in alert_state(self.con).values()])
        m("agentdynamics_alerts_sent_total", "Alert messages delivered since start", "counter", [({}, self.e.stats["alerts_sent"])])
        m("agentdynamics_alerts_dropped_total", "Alert messages given up on since start (see /api/alerts)", "counter",
          [({}, self.e.stats["alerts_dropped"])])
        m("agentdynamics_alert_queue", "Alert messages waiting to be delivered, by destination", "gauge",
          [({"destination": k}, v) for k, v in outbox_depth(self.con).items()])
        return "\n".join(lines) + "\n"
