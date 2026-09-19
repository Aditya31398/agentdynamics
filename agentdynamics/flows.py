"""Workflow (agent-graph) analytics: process mining over node paths.

Works for LangGraph / multi-agent traces (nodes = graph nodes or agents) and for coding agents
without an explicit graph (nodes = process phases: explore -> edit -> verify ...).
"""
import statistics
from collections import Counter, defaultdict

from .analysis import apdex_score, pct
from .store import rows


def _stats(tasks):
    n = len(tasks)
    cost = [t["cost"] + (t["subagent_cost"] or 0) for t in tasks]
    wall = [t["wall_s"] or 0 for t in tasks]
    ok = sum(1 for t in tasks if t["outcome"] == "completed")
    return {
        "runs": n, "success_rate": round(ok / n, 3) if n else None,
        "failed": sum(1 for t in tasks if t["outcome"] == "failed"),
        "cost": round(sum(cost), 4), "avg_cost": round(sum(cost) / n, 5) if n else 0,
        "p50_s": round(pct(wall, 0.5) or 0, 2), "p95_s": round(pct(wall, 0.95) or 0, 2),
        "avg_steps": round(statistics.mean([t["steps_total"] or 0 for t in tasks]), 1) if n else 0,
        "loop_rate": round(sum(1 for t in tasks if (t["max_node_visits"] or 0) >= 5) / n, 3) if n else 0,
        "apdex": apdex_score([t for t in tasks if t["apdex"]]),
        "tokens": sum(t["total_tokens"] for t in tasks),
    }


def workflows(api, q):
    ts = api.tasks(q)
    by = defaultdict(list)
    for t in ts:
        by[t["workflow"] or t["task_type"]].append(t)
    out = []
    for wf, g in by.items():
        paths = Counter(tuple(t["path"] or []) for t in g)
        s = _stats(g)
        s.update({"workflow": wf, "framework": Counter(t["framework"] for t in g).most_common(1)[0][0],
                  "variants": len(paths), "top_path": list(paths.most_common(1)[0][0]) if paths else [],
                  "nodes": max((t["nodes"] or 0) for t in g), "graph": any((t["nodes"] or 0) > 0 for t in g),
                  "projects": sorted({t["project"] for t in g})[:5]})
        out.append(s)
    out.sort(key=lambda x: -x["runs"])
    return {"workflows": out}


def workflow_detail(api, name, q):
    ts = [t for t in api.tasks(q) if (t["workflow"] or t["task_type"]) == name]
    if not ts:
        return None
    graph = any((t["nodes"] or 0) > 0 for t in ts)
    # --- transitions (directly-follows graph) with failure overlay
    edges = Counter()
    edge_fail = Counter()
    node_runs = Counter()
    for t in ts:
        p = ["__start__"] + list(t["path"] or []) + ["__end__"]
        failed = t["outcome"] in ("failed", "interrupted", "rework")
        for a, b in zip(p, p[1:]):
            edges[(a, b)] += 1
            if failed:
                edge_fail[(a, b)] += 1
        for n in set(t["path"] or []):
            node_runs[n] += 1
    # --- node / phase statistics from steps
    ids = [t["id"] for t in ts]
    steps = []
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        steps += rows(api.con, f"""SELECT task_id, kind, name, node, phase, span_kind, duration_ms, cost, attributed_cost, is_error,
            input_tokens, output_tokens, cache_read, cache_write, agent FROM steps WHERE task_id IN ({','.join('?' * len(chunk))})""", chunk)
    nodes = defaultdict(lambda: {"executions": 0, "errors": 0, "ms": [], "llm_calls": 0, "tool_calls": 0, "cost": 0.0, "tokens": 0})
    for s in steps:
        if graph:
            key = s["node"] or (s["name"] if s["span_kind"] == "agent" else None)
            if not key:
                continue
            n = nodes[key]
            if s["kind"] == "span" and (s["name"] == s["node"] or s["span_kind"] in ("node", "agent")):
                n["executions"] += 1
                n["errors"] += s["is_error"] or 0
                if s["duration_ms"] is not None:
                    n["ms"].append(s["duration_ms"])
            elif s["kind"] == "llm":
                n["llm_calls"] += 1
                n["cost"] += s["cost"] or 0
                n["tokens"] += sum((s[k] or 0) for k in ("input_tokens", "output_tokens", "cache_read", "cache_write"))
                n["errors"] += s["is_error"] or 0
            elif s["kind"] == "tool":
                n["tool_calls"] += 1
                n["errors"] += s["is_error"] or 0
        else:
            if s["kind"] != "tool":
                continue
            n = nodes[s["phase"] or "other"]
            n["executions"] += 1
            n["tool_calls"] += 1
            n["errors"] += s["is_error"] or 0
            n["cost"] += s["attributed_cost"] or 0
            if s["duration_ms"] is not None:
                n["ms"].append(s["duration_ms"])
    total_cost = sum(n["cost"] for n in nodes.values()) or 1
    node_list = []
    for k, n in nodes.items():
        ms = n.pop("ms")
        node_list.append({"node": k, **n, "runs": node_runs.get(k, 0), "per_run": round(n["executions"] / len(ts), 2),
                          "p50_ms": round(pct(ms, 0.5)) if ms else None, "p95_ms": round(pct(ms, 0.95)) if ms else None,
                          "error_rate": round(n["errors"] / max(1, n["executions"] + n["llm_calls"] + (0 if graph else 0)), 3),
                          "cost_share": round(n["cost"] / total_cost, 3)})
    node_list.sort(key=lambda x: -x["executions"])
    # --- path variants
    by_path = defaultdict(list)
    for t in ts:
        by_path[tuple(t["path"] or [])].append(t)
    variants = []
    for p, g in sorted(by_path.items(), key=lambda kv: -len(kv[1]))[:15]:
        st = _stats(g)
        variants.append({"path": list(p), "runs": len(g), "share": round(len(g) / len(ts), 3), "success_rate": st["success_rate"],
                         "avg_cost": st["avg_cost"], "p50_s": st["p50_s"], "example": g[0]["id"]})
    # --- handoffs between agents
    handoffs = Counter()
    by_task = defaultdict(list)
    for s in steps:
        if s["kind"] in ("llm", "tool") and s["agent"]:
            by_task[s["task_id"]].append(s["agent"])
    for seq in by_task.values():
        c = [a for i, a in enumerate(seq) if i == 0 or a != seq[i - 1]]
        for a, b in zip(c, c[1:]):
            handoffs[(a, b)] += 1
    loops = Counter(min(t["max_node_visits"] or 0, 10) for t in ts)
    crit = Counter(t["critical_node"] for t in ts if t["critical_node"])
    return {
        "workflow": name, "graph": graph, "summary": _stats(ts), "nodes": node_list,
        "edges": [{"from": a, "to": b, "count": c, "fail": edge_fail.get((a, b), 0)} for (a, b), c in edges.items()],
        "variants": variants, "variant_count": len(by_path),
        "handoffs": [{"from": a, "to": b, "count": c} for (a, b), c in handoffs.most_common(20)],
        "loops": {str(k): v for k, v in sorted(loops.items())},
        "critical": [{"node": k, "runs": v} for k, v in crit.most_common(8)],
        "failures": [api._task_brief(t) | {"root_error": None} for t in sorted(ts, key=lambda x: -(x["started"] or 0))
                     if t["outcome"] in ("failed", "interrupted")][:10],
    }
