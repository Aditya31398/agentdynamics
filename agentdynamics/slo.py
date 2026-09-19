"""Service level objectives with error budgets and burn rates."""
import json
import os
import time
from collections import defaultdict

from .analysis import apdex_score, pct

DEFAULT_SLOS = [
    {"id": "success", "name": "Task success rate", "metric": "success_rate", "op": ">=", "target": 0.95, "window_days": 7, "scope": {}},
    {"id": "apdex", "name": "Agent Apdex", "metric": "apdex", "op": ">=", "target": 0.85, "window_days": 7, "scope": {}},
    {"id": "latency", "name": "p95 task latency (s)", "metric": "p95_seconds", "op": "<=", "target": 900, "window_days": 7, "scope": {}},
    {"id": "cost", "name": "Median cost per task ($)", "metric": "median_cost", "op": "<=", "target": 1.0, "window_days": 7, "scope": {}},
    {"id": "tool_errors", "name": "Tool error rate", "metric": "tool_error_rate", "op": "<=", "target": 0.05, "window_days": 7, "scope": {}},
]
RATIO = {"success_rate", "apdex"}  # "good event" ratios get an error budget


def path(data_dir):
    return os.path.join(data_dir, "slos.json")


def load(data_dir):
    p = path(data_dir)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_SLOS


def save(data_dir, slos):
    with open(path(data_dir), "w", encoding="utf-8") as f:
        json.dump(slos, f, indent=2)


def metric(tasks, m):
    if not tasks:
        return None
    if m == "success_rate":
        return sum(1 for t in tasks if t["outcome"] == "completed") / len(tasks)
    if m == "apdex":
        return apdex_score([t for t in tasks if t["apdex"]])
    if m == "p95_seconds":
        return pct([t["wall_s"] or 0 for t in tasks], 0.95)
    if m == "median_cost":
        return pct([t["cost"] + (t["subagent_cost"] or 0) for t in tasks], 0.5)
    if m == "tool_error_rate":
        calls = sum(t["tool_calls"] for t in tasks)
        return sum(t["tool_errors"] for t in tasks) / calls if calls else 0
    return None


def evaluate(all_tasks, slo, now=None):
    now = now or time.time()
    sc = slo.get("scope") or {}
    ts = [t for t in all_tasks if all((t.get(k) or "") == v for k, v in sc.items() if v)]
    win = [t for t in ts if (t["started"] or 0) >= now - slo["window_days"] * 86400]
    val = metric(win, slo["metric"])
    good = (val >= slo["target"]) if slo["op"] == ">=" else (val <= slo["target"]) if val is not None else None
    res = {**slo, "value": val, "n": len(win), "met": good if val is not None else None}
    if slo["metric"] in RATIO and val is not None:
        allowed = 1 - slo["target"]
        bad = 1 - val
        res["budget_remaining"] = round(1 - bad / allowed, 3) if allowed > 0 else None
        last = [t for t in win if (t["started"] or 0) >= now - 86400]
        v24 = metric(last, slo["metric"])
        res["burn_rate_24h"] = round((1 - v24) / allowed, 2) if (v24 is not None and allowed > 0) else None
    daily = defaultdict(list)
    for t in win:
        daily[time.strftime("%Y-%m-%d", time.localtime(t["started"]))].append(t)
    res["daily"] = [{"day": d, "value": metric(g, slo["metric"]), "n": len(g)} for d, g in sorted(daily.items())]
    if res["daily"]:
        ok_days = [d for d in res["daily"] if d["value"] is not None and
                   ((d["value"] >= slo["target"]) if slo["op"] == ">=" else (d["value"] <= slo["target"]))]
        res["days_met"] = f"{len(ok_days)}/{len(res['daily'])}"
    status = "unknown" if val is None else "ok"
    if val is not None and not good:
        status = "breached"
    elif res.get("budget_remaining") is not None and res["budget_remaining"] < 0.25:
        status = "at risk"
    elif res.get("burn_rate_24h") is not None and res["burn_rate_24h"] > 2:
        status = "at risk"
    res["status"] = status
    return res
