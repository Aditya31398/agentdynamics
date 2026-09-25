"""Operations: sources, connection snippets, config, health and Prometheus metrics."""
from collections import Counter, defaultdict

from ..store import rows





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
        return "\n".join(lines) + "\n"
