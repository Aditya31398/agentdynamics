"""HTTP API + static web console (stdlib only)."""
import gzip
import hmac
import json
import mimetypes
import os
import statistics
import threading
import time
import traceback
import zlib
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .analysis import REFUSAL as REFUSE, TRUNCATION as TRUNC, apdex_score, pct
from .store import connect_reader, rows

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
DAY = 86400


def mcp_group(name):
    if name and name.startswith("mcp__"):
        parts = name.split("__")
        return f"MCP · {parts[1]}" if len(parts) > 2 else name
    return name


class Api:
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

    # ------------------------------------------------------------ filtering
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

    # ------------------------------------------------------------ helpers
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

    # ------------------------------------------------------------ endpoints
    def filters(self, q):
        live = "WHERE llm_calls>0 OR tool_calls>0"
        projects = rows(self.con, f"SELECT project, COUNT(*) n, SUM(cost) cost FROM tasks {live} GROUP BY project ORDER BY cost DESC")
        types = rows(self.con, "SELECT DISTINCT task_type FROM tasks ORDER BY task_type")
        sources = rows(self.con, "SELECT DISTINCT source FROM tasks")
        envs = rows(self.con, f"SELECT environment, COUNT(*) n FROM tasks {live} GROUP BY environment ORDER BY n DESC")
        fws = rows(self.con, f"SELECT DISTINCT framework FROM tasks {live}")
        meta = {r["k"]: json.loads(r["v"]) for r in self.con.execute("SELECT k, v FROM meta")}
        return {"projects": projects, "types": [r["task_type"] for r in types], "sources": [r["source"] for r in sources],
                "environments": [r["environment"] for r in envs], "frameworks": [r["framework"] for r in fws],
                "auth": self.e.cfg["auth"]["enabled"],
                "refreshed": meta.get("refreshed"), "refresh_seconds": self.e.last_duration,
                "runs": meta.get("runs"), "task_count": meta.get("tasks")}

    def overview(self, q):
        ts = self.tasks(q)
        subq = dict(q, sub="1")
        all_ts = self.tasks(subq)
        k = self.kpis(ts)
        k["cost_incl_subagents"] = round(sum(t["cost"] for t in all_ts), 4)
        events = self.events(q)["events"]
        sev = Counter(e["severity"] for e in events)
        by_type = defaultdict(list)
        for t in ts:
            by_type[t["task_type"]].append(t)
        types = []
        for ty, g in by_type.items():
            ap = apdex_score([t for t in g if t["apdex"]])
            types.append({"type": ty, "tasks": len(g), "cost": round(sum(t["cost"] + (t["subagent_cost"] or 0) for t in g), 3),
                          "apdex": ap, "health": self.health(ap)})
        types.sort(key=lambda x: -x["cost"])
        models = rows(self.con, f"""SELECT s.model, COUNT(*) calls, SUM(s.cost) cost FROM steps s JOIN tasks t ON t.id = s.task_id
            {self.where(subq)[0]} AND s.kind='llm' AND s.model != '<synthetic>' GROUP BY s.model ORDER BY cost DESC""", self.where(subq)[1])
        phase = Counter()
        for t in ts:
            phase.update(t["phase_cost"] or {})
        top = sorted(ts, key=lambda t: -(t["cost"] + (t["subagent_cost"] or 0)))[:8]
        return {"kpis": k, "daily": self.daily(ts), "events": events[:12], "severity": dict(sev), "types": types,
                "models": models, "phase_cost": {p: round(v, 4) for p, v in phase.items()},
                "outcomes": dict(Counter(t["outcome"] for t in ts)),
                "apdex_mix": dict(Counter(t["apdex"] for t in ts if t["apdex"])),
                "top_tasks": [self._task_brief(t) for t in top]}

    @staticmethod
    def _task_brief(t):
        keys = ["id", "run_id", "project", "task_type", "prompt", "started", "duration_s", "cost", "subagent_cost", "outcome",
                "apdex", "score", "tool_calls", "tool_errors", "llm_calls", "total_tokens", "cost_vs_baseline", "is_subagent",
                "waste_cost", "verified", "models", "max_context", "workflow", "environment", "framework", "wall_s",
                "steps_total", "max_node_visits", "feedback_score"]
        d = {k: t.get(k) for k in keys}
        d["prompt"] = (d["prompt"] or "")[:160]
        return d

    def types(self, q):
        ts = self.tasks(q)
        base = {r["task_type"]: r["data"] for r in rows(self.con, "SELECT * FROM baselines")}
        by = defaultdict(list)
        for t in ts:
            by[t["task_type"]].append(t)
        out = []
        for ty, g in by.items():
            k = self.kpis(g)
            costs = [t["cost"] + (t["subagent_cost"] or 0) for t in g]
            k.update({"type": ty, "health": self.health(k["apdex"]), "baseline": base.get(ty) or base.get("__all__"),
                      "p90_cost": round(pct(costs, 0.9), 4), "p90_duration": round(pct([t["duration_s"] for t in g], 0.9), 1),
                      "daily": [{"day": d["day"], "tasks": d["tasks"], "cost": d["cost"]} for d in self.daily(g)]})
            out.append(k)
        ev = Counter(r["task_type"] for r in rows(self.con, "SELECT task_type FROM events"))
        for k in out:
            k["events"] = ev.get(k["type"], 0)
        out.sort(key=lambda x: -x["cost"])
        return {"types": out}

    def task_list(self, q):
        extra, args = [], []
        for f in ("outcome", "apdex"):
            if q.get(f):
                extra.append(f"t.{f} = ?")
                args.append(q[f])
        if q.get("run"):
            extra.append("t.run_id = ?")
            args.append(q["run"])
        if q.get("q"):
            extra.append("t.prompt LIKE ?")
            args.append(f"%{q['q']}%")
        if q.get("flag") == "unverified":
            extra.append("t.unverified_edits = 1")
        if q.get("flag") == "waste":
            extra.append("t.waste_cost > 0")
        sort = {"cost": "(t.cost + t.subagent_cost) DESC", "recent": "t.started DESC", "score": "t.score ASC",
                "duration": "t.duration_s DESC", "baseline": "t.cost_vs_baseline DESC", "waste": "t.waste_cost DESC"}.get(q.get("sort"), "t.started DESC")
        ex = (" AND " + " AND ".join(extra)) if extra else ""
        ts = self.tasks(q, f"{ex} ORDER BY {sort} LIMIT {int(q.get('limit', 300))}", args)
        return {"tasks": [self._task_brief(t) for t in ts]}

    def task(self, tid):
        t = rows(self.con, "SELECT * FROM tasks WHERE id = ?", (tid,))
        if not t:
            return None
        t = t[0]
        steps = rows(self.con, "SELECT * FROM steps WHERE task_id = ? ORDER BY seq", (tid,))
        events = rows(self.con, "SELECT * FROM events WHERE task_id = ?", (tid,))
        run = rows(self.con, "SELECT * FROM runs WHERE id = ?", (t["run_id"],))
        base = rows(self.con, "SELECT data FROM baselines WHERE task_type IN (?, '__all__') ORDER BY task_type = '__all__'", (t["task_type"],))
        children = rows(self.con, "SELECT id, run_id, prompt, cost, duration_s, tool_calls, tool_errors, outcome, score FROM tasks WHERE parent_task_id = ?", (tid,))
        for c in children:
            c["prompt"] = (c["prompt"] or "")[:200]
            c["title"] = (rows(self.con, "SELECT agent_name, title FROM runs WHERE id=?", (c["run_id"],)) or [{}])[0]
        sibs = rows(self.con, "SELECT id, idx, prompt, outcome FROM tasks WHERE run_id = ? ORDER BY idx", (t["run_id"],))
        pos = next((i for i, s in enumerate(sibs) if s["id"] == tid), 0)
        nav = {"prev": sibs[pos - 1]["id"] if pos > 0 else None, "next": sibs[pos + 1]["id"] if pos + 1 < len(sibs) else None,
               "count": len(sibs), "pos": pos + 1}
        return {"task": t, "steps": steps, "events": events, "run": run[0] if run else None,
                "baseline": base[0]["data"] if base else None, "children": children, "nav": nav}

    def sessions(self, q):
        w, a = self.where(q)
        ts = rows(self.con, f"SELECT * FROM tasks t{w}", a)
        by = defaultdict(list)
        for t in ts:
            by[t["run_id"]].append(t)
        runs = {r["id"]: r for r in rows(self.con, "SELECT * FROM runs")}
        ev = Counter(r["run_id"] for r in rows(self.con, "SELECT run_id FROM events"))
        out = []
        for rid, g in by.items():
            r = runs.get(rid, {})
            k = self.kpis(g)
            out.append({"id": rid, "title": r.get("title") or r.get("agent_name") or (g[0]["prompt"] or "")[:80],
                        "project": r.get("project"), "cwd": r.get("cwd"), "started": r.get("started"), "ended": r.get("ended"),
                        "source": r.get("source"), "models": ",".join(sorted({m for t in g for m in (t["models"] or "").split(",") if m})),
                        "events": ev.get(rid, 0), **k})
        out.sort(key=lambda x: -(x["started"] or 0))
        return {"sessions": out}

    def flowmap(self, q):
        cond, args = self.where(dict(q, sub="1"))
        extra = ""
        if q.get("task"):
            ids = [q["task"]] + [r["id"] for r in rows(self.con, "SELECT id FROM tasks WHERE parent_task_id = ?", (q["task"],))]
            extra = f" AND t.id IN ({','.join('?' * len(ids))})"
            args = args + ids
        if q.get("run"):
            extra += " AND (t.run_id = ? OR t.run_id LIKE ?)"
            args = args + [q["run"], q["run"] + ":%"]
        steps = rows(self.con, f"""SELECT s.kind, s.name, s.model, s.duration_ms, s.cost, s.attributed_cost, s.is_error, s.phase,
            s.output_chars, s.context_tokens, s.input_tokens, s.output_tokens, s.cache_read, s.cache_write, t.is_subagent, t.id tid,
            s.agent, t.workflow, t.source
            FROM steps s JOIN tasks t ON t.id = s.task_id{cond}{extra}""", args)
        nodes, edges = {}, {}

        def node(nid, kind, label):
            if nid not in nodes:
                nodes[nid] = {"id": nid, "kind": kind, "label": label, "calls": 0, "errors": 0, "ms": [], "cost": 0.0,
                              "tokens": 0, "phases": Counter(), "members": Counter()}
            return nodes[nid]

        def edge(a, b):
            k = (a, b)
            if k not in edges:
                edges[k] = {"from": a, "to": b, "calls": 0, "errors": 0, "ms": []}
            return edges[k]

        user = node("user", "user", "User")
        main = node("agent:main", "agent", "Main agent")
        subs = None
        for s in steps:
            agent = main
            if s["is_subagent"]:
                subs = subs or node("agent:sub", "agent", "Subagents")
                agent = subs
            elif s["source"] != "claude-code" and s["kind"] != "prompt":
                # traced apps: one node per agent (multi-agent) or per workflow entry point
                label = s["agent"] or s["workflow"] or "agent"
                agent = node(f"agent:{label}", "agent", label)
            if s["kind"] == "prompt" and s["source"] != "claude-code" and not s["is_subagent"]:
                label = s["workflow"] or "agent"
                e = edge("user", f"agent:{label}")
                node(f"agent:{label}", "agent", label)
                e["calls"] += 1
                user["calls"] += 1
                continue
            if s["kind"] == "prompt":
                if s["is_subagent"]:
                    e = edge("agent:main", "agent:sub")
                else:
                    e = edge("user", "agent:main")
                    user["calls"] += 1
                e["calls"] += 1
            elif s["kind"] == "llm":
                if s["model"] == "<synthetic>":
                    continue
                m = node(f"model:{s['model']}", "model", s["model"] or "?")
                m["calls"] += 1
                m["cost"] += s["cost"] or 0
                m["tokens"] += (s["input_tokens"] or 0) + (s["output_tokens"] or 0) + (s["cache_read"] or 0) + (s["cache_write"] or 0)
                if s["duration_ms"] is not None:
                    m["ms"].append(s["duration_ms"])
                agent["calls"] += 1
                agent["cost"] += s["cost"] or 0
                e = edge(agent["id"], m["id"])
                e["calls"] += 1
                if s["duration_ms"] is not None:
                    e["ms"].append(s["duration_ms"])
            elif s["kind"] == "tool":
                g = mcp_group(s["name"])
                tn = node(f"tool:{g}", "tool", g)
                tn["calls"] += 1
                tn["errors"] += s["is_error"] or 0
                tn["cost"] += s["attributed_cost"] or 0
                tn["phases"][s["phase"]] += 1
                tn["members"][s["name"]] += 1
                if s["duration_ms"] is not None:
                    tn["ms"].append(s["duration_ms"])
                e = edge(agent["id"], tn["id"])
                e["calls"] += 1
                e["errors"] += s["is_error"] or 0
                if s["duration_ms"] is not None:
                    e["ms"].append(s["duration_ms"])
            elif s["kind"] == "notice" and s["name"] == "api_error":
                agent["errors"] += 1

        def fin(d):
            ms = d.pop("ms")
            d["avg_ms"] = round(statistics.mean(ms)) if ms else None
            d["p95_ms"] = round(pct(ms, 0.95)) if ms else None
            d["error_rate"] = round(d["errors"] / d["calls"], 3) if d["calls"] else 0
            for k in ("phases", "members"):
                if k in d:
                    d[k] = dict(d[k].most_common(8))
            return d

        # agent-to-agent handoffs for multi-agent traces
        prev = {}
        for s in steps:
            if s["kind"] in ("llm", "tool") and s["agent"] and s["source"] != "claude-code":
                p = prev.get(s["tid"])
                if p and p != s["agent"]:
                    edge(f"agent:{p}", f"agent:{s['agent']}")["calls"] += 1
                prev[s["tid"]] = s["agent"]
        has_main = main["calls"] > 0 or any(k[0] == "user" and k[1] == "agent:main" for k in edges)
        if not has_main:
            nodes.pop("agent:main", None)
        return {"nodes": [fin(n) for n in nodes.values() if n["calls"] or n["id"] == "user" or (n["kind"] == "agent" and n["id"] in {e[1] for e in edges})],
                "edges": [fin(e) for e in edges.values()]}

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

    def process(self, q):
        ts = self.tasks(q)
        meta = {r["k"]: json.loads(r["v"]) for r in self.con.execute("SELECT k, v FROM meta")}
        from .analysis import process_insights
        insights = process_insights(ts) if (q.get("project") or q.get("days") or q.get("type")) else meta.get("insights", [])
        dims = ["efficiency", "focus", "reliability", "verification", "context", "autonomy", "overall"]
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
        col = {"models": "models", "task_type": "task_type", "project": "project", "source": "source"}.get(dim)
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

    # ------------------------------------------------------------ enterprise endpoints
    def workflows(self, q):
        from .flows import workflows
        return workflows(self, q)

    def workflow(self, q):
        from .flows import workflow_detail
        return workflow_detail(self, q.get("name", ""), q)

    def slos(self, q):
        from . import slo
        ts = self.tasks(dict(q, days=""))
        return {"slos": [slo.evaluate(ts, s) for s in slo.load(self.e.data_dir)]}

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
        from .__main__ import SNIPPETS
        url = (q.get("url") or "http://127.0.0.1:8787").rstrip("/")
        return {"snippets": [{"id": k, "title": t, "body": b.format(url=url)} for k, (t, b) in SNIPPETS.items()],
                "auth": self.e.cfg["auth"]["enabled"]}

    def config(self, q):
        from .config import public_view
        return {"config": public_view(self.e.cfg), "data_dir": self.e.data_dir, "db": self.e.db_path}

    def healthz(self):
        ok = self.e.last_refresh is not None
        return {"status": "ok" if ok else "starting", "last_refresh": self.e.last_refresh, "refresh_seconds": self.e.last_duration,
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
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- HTTP layer

ROLE_FOR = {"ingest": 1, "read": 2, "admin": 3}
# ingest keys only write telemetry, read keys only read, admin can do everything
CAN = {1: {"ingest"}, 2: {"read"}, 3: {"ingest", "read", "admin"}}
MAX_BODY = 64 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    api = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", extra_headers=None):
        if isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    # --- auth: API keys with roles ingest < read < admin
    def _role(self):
        auth = self.api.e.cfg["auth"]
        if not auth.get("enabled"):
            return 3
        key = self.headers.get("x-api-key") or ""
        h = self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer "):
            key = h[7:].strip()
        for k in auth.get("keys") or []:
            if key and hmac.compare_digest(key, str(k.get("key", ""))):
                return ROLE_FOR.get(k.get("role"), 0)
        return 0

    def _require(self, need):
        r = self._role()
        if need in CAN.get(r, set()):
            return True
        self._send(401 if r == 0 else 403, {"error": "unauthorized" if r == 0 else f"requires '{need}' role"},
                   extra_headers={"WWW-Authenticate": "Bearer"} if r == 0 else None)
        return False

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("payload too large")
        body = self.rfile.read(n) if n else b""
        enc = (self.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            body = gzip.decompress(body)
        elif enc == "deflate":
            body = zlib.decompress(body)
        elif enc and enc != "identity":
            raise ValueError(f"unsupported Content-Encoding {enc}")
        return body

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items() if v and v[0] != ""}
        p = u.path
        api = self.api
        try:
            if p == "/healthz":
                return self._send(200, api.healthz())
            if p in ("/langsmith/info", "/langsmith/api/v1/info"):
                from .collectors.langsmith import INFO
                return self._send(200, INFO)
            if p.startswith("/api/") or p == "/metrics":
                if not self._require("read"):
                    return
            if p == "/metrics":
                return self._send(200, api.prometheus(), "text/plain; version=0.0.4")
            routes = {"/api/filters": api.filters, "/api/overview": api.overview, "/api/types": api.types,
                      "/api/tasks": api.task_list, "/api/sessions": api.sessions, "/api/flowmap": api.flowmap,
                      "/api/tools": api.tools, "/api/models": api.models, "/api/events": api.events,
                      "/api/process": api.process, "/api/analytics": api.analytics, "/api/compare": api.compare,
                      "/api/workflows": api.workflows, "/api/workflow": api.workflow, "/api/slos": api.slos,
                      "/api/sources": api.sources, "/api/config": api.config, "/api/connect": api.connect}
            if p in routes:
                r = routes[p](q)
                return self._send(200 if r is not None else 404, r if r is not None else {"error": "not found"})
            if p.startswith("/api/task/"):
                r = api.task(unquote(p[len("/api/task/"):]))
                return self._send(200 if r else 404, r or {"error": "not found"})
            if p == "/api/rules":
                return self._send(200, {"rules": api.e.rules()})
            if p == "/api/whoami":
                return self._send(200, {"role": {3: "admin", 2: "read", 1: "ingest"}.get(self._role())})
            if p.startswith("/api/") or p.startswith("/langsmith/"):
                return self._send(404 if p.startswith("/api/") else 200, {"error": "not found"} if p.startswith("/api/") else {})
        except Exception as ex:  # surface errors to the console instead of a dropped connection
            traceback.print_exc()
            return self._send(500, {"error": str(ex)})
        # static console
        rel = "index.html" if p in ("/", "") else p.lstrip("/")
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            path = os.path.join(WEB_DIR, "index.html")
        with open(path, "rb") as f:
            self._send(200, f.read(), mimetypes.guess_type(path)[0] or "application/octet-stream")

    def do_PATCH(self):
        u = urlparse(self.path)
        try:
            if u.path.startswith("/langsmith/") and "/runs/" in u.path:
                if not self._require("ingest"):
                    return
                rid = u.path.rstrip("/").rsplit("/", 1)[-1]
                patch = json.loads(self._body() or b"{}")
                patch["id"] = rid
                self.api.e.ingest_langsmith([], [patch])
                return self._send(200, {})
        except Exception as ex:
            return self._send(400, {"error": str(ex)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        e = self.api.e
        try:
            # ---- telemetry ingestion (role: ingest)
            if p in ("/v1/traces", "/otlp/v1/traces"):
                if not self._require("ingest"):
                    return
                ctype = self.headers.get("Content-Type") or ""
                n = e.ingest_otlp(self._body(), ctype)
                if "protobuf" in ctype:
                    return self._send(200, b"", "application/x-protobuf")  # empty ExportTraceServiceResponse
                return self._send(200, {"partialSuccess": {}, "accepted": n})
            if p.startswith("/langsmith/"):
                if not self._require("ingest"):
                    return
                from .collectors import langsmith as ls
                sub = p[len("/langsmith"):].replace("/api/v1", "")
                body = self._body()
                if sub == "/runs/batch":
                    posts, patches = ls.parse_batch(body)
                    e.ingest_langsmith(posts, patches)
                elif sub == "/runs/multipart":
                    posts, patches, fb = ls.parse_multipart(body, self.headers.get("Content-Type") or "")
                    e.ingest_langsmith(posts, patches, fb)
                elif sub == "/runs":
                    e.ingest_langsmith([json.loads(body or b"{}")], [])
                elif sub == "/feedback":
                    e.ingest_langsmith([], [], [json.loads(body or b"{}")])
                else:
                    return self._send(200, {})  # accept and ignore other LangSmith calls (datasets, sessions...)
                return self._send(202, {})
            if p == "/api/ingest":
                if not self._require("ingest"):
                    return
                payload = json.loads(self._body() or b"{}")
                runs = payload if isinstance(payload, list) else [payload]
                return self._send(200, {"ok": True, "ids": [e.ingest(r) for r in runs]})
            if p == "/api/ingest/records":
                # log pipelines (Fluent Bit / Vector / Logstash HTTP outputs): JSON array or NDJSON of any supported format
                if not self._require("ingest"):
                    return
                from .collectors.inbox import detect, unwrap
                body = self._body().strip()
                recs = json.loads(body) if body.startswith(b"[") else [json.loads(x) for x in body.splitlines() if x.strip()]
                recs = [unwrap(r) for r in recs]
                n = e.ingest_records([(detect(r), r) for r in recs if detect(r)])
                return self._send(200, {"ok": True, "accepted": n, "received": len(recs)})
            # ---- operations
            if p == "/api/refresh":
                if not self._require("read"):
                    return
                changed = e.refresh(force=True)
                return self._send(200, {"ok": True, "changed": changed, "seconds": e.last_duration})
            if p == "/api/rules":
                if not self._require("admin"):
                    return
                e.save_rules(json.loads(self._body())["rules"])
                return self._send(200, {"ok": True})
            if p == "/api/slos":
                if not self._require("admin"):
                    return
                from . import slo
                slo.save(e.data_dir, json.loads(self._body())["slos"])
                return self._send(200, {"ok": True})
        except Exception as ex:
            traceback.print_exc()
            return self._send(400, {"error": str(ex)})
        self._send(404, {"error": "not found"})


def serve(engine, host="127.0.0.1", port=8787):
    Handler.api = Api(engine)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"AgentDynamics console: http://{host}:{port}")
    print(f"  OTLP/HTTP traces : http://{host}:{port}/v1/traces")
    print(f"  LangSmith API    : http://{host}:{port}/langsmith   (set LANGSMITH_ENDPOINT to this)")
    print(f"  auth             : {'on' if engine.cfg['auth']['enabled'] else 'off (local mode)'}")
    httpd.serve_forever()
