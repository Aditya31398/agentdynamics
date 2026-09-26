"""Monitor: the overview, flow map, workflows, task types, tasks, one task, sessions."""
import json
import statistics
from collections import Counter, defaultdict

from ..analysis import apdex_score, pct
from ..store import rows



from .base import mcp_group


class MonitorMixin:
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
                "auth": self.e.cfg["auth"]["enabled"], "scope": self.projects,   # None: every project
                "refreshed": meta.get("refreshed"), "refresh_seconds": self.e.last_duration,
                # meta holds install-wide counts; a scoped reader gets its own
                "runs": meta.get("runs") if self.projects is None else
                self.con.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                "task_count": meta.get("tasks") if self.projects is None else
                self.con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]}

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
            # how these tasks got this label: a workflow name is a fact, a keyword match is a guess
            srcs = Counter(t.get("task_type_source") or "unmatched" for t in g)
            k.update({"typed_by": dict(srcs),
                      "top_matches": [m for m, _ in Counter(t["task_type_match"] for t in g if t.get("task_type_match")).most_common(3)]})
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

    def workflows(self, q):
        from ..flows import workflows
        return workflows(self, q)

    def workflow(self, q):
        from ..flows import workflow_detail
        return workflow_detail(self, q.get("name", ""), q)
