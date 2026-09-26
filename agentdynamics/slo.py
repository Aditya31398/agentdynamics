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


def scoped(tasks, slo):
    """The tasks an SLO covers: those matching every non-empty field of its scope."""
    sc = {k: v for k, v in (slo.get("scope") or {}).items() if v}
    return [t for t in tasks if all((t.get(k) or "") == v for k, v in sc.items())]


def evaluate(all_tasks, slo, now=None):
    now = now or time.time()
    ts = scoped(all_tasks, slo)
    win = [t for t in ts if (t["started"] or 0) >= now - slo["window_days"] * 86400]
    val = metric(win, slo["metric"])
    # No tasks in the window means no value, and so no verdict. The old one-liner guarded only its
    # second branch, so every ">=" objective raised on an empty window instead of reading "unknown".
    if val is None:
        good = None
    else:
        good = val >= slo["target"] if slo["op"] == ">=" else val <= slo["target"]
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


# ---------------------------------------------------------------- burn-rate alerts
# Multi-window, multi-burn-rate alerts (Google SRE Workbook, "Alerting on SLOs"). A policy holds when the
# error budget is being spent fast enough to use `budget` of it within `long_h` hours, and a short window
# (1/12 of the long one) shows it is still happening, so the alert clears soon after the burn stops. As in
# the workbook, the fast and slow policies are one alert (a page) and the third is another (a ticket): a
# hard failure satisfies all three, and should page once. Thresholds scale with the SLO's window; for a
# 30-day SLO they are the workbook's 14.4, 6 and 1. None is below 1: a burn under 1 leaves budget at the
# end of the window, which is the objective being met.
BURN_POLICIES = [
    {"id": "fast", "alert": "page", "budget": 0.02, "long_h": 1, "short_h": 5 / 60, "severity": "critical"},
    {"id": "slow", "alert": "page", "budget": 0.05, "long_h": 6, "short_h": 0.5, "severity": "critical"},
    {"id": "ticket", "alert": "ticket", "budget": 0.10, "long_h": 72, "short_h": 6, "severity": "warning"},
]


def burn_threshold(slo, policy):
    return max(1.0, policy["budget"] * slo["window_days"] * 24 / policy["long_h"])


def _finished(t):
    # an outcome is known when the task ends, so that is when it counts against the budget
    return t.get("ended") or t.get("started") or 0


def burn_rate(tasks, slo, since, until):
    """(burn, n) over tasks that finished in [since, until): 1.0 spends the error budget exactly over the
    SLO's window. burn is None with no tasks, or for an objective with no budget (a target of 1)."""
    win = [t for t in tasks if since <= _finished(t) < until]
    v = metric(win, slo["metric"])
    allowed = 1 - slo["target"]
    if v is None or allowed <= 0:
        return None, len(win)
    return (1 - v) / allowed, len(win)


def alert_conditions(tasks, slos, now, min_tasks=10, firing=()):
    """The SLO alerts that should be firing now, {key: details}.

    Keys are "slo/<id>/<alert>". Ratio objectives (success rate, Apdex) raise a "page" and a "ticket" alert
    on burn rate, per BURN_POLICIES; the others raise "breach" when the objective is missed over its window. `min_tasks` is the fewest tasks a long window needs before its
    burn counts: at low volume one failure is a large fraction. `firing` holds the keys firing now: an
    agent can go quiet for longer than a short window, and an empty short window is no evidence the burn
    stopped, so it keeps an alert firing but never starts one.
    """
    out = {}
    horizon = now - max(p["long_h"] for p in BURN_POLICIES) * 3600
    for s in slos:
        ts = scoped(tasks, s)
        base = {"slo": s["id"], "name": s.get("name") or s["id"], "metric": s["metric"], "op": s["op"],
                "target": s["target"], "window_days": s["window_days"], "scope": s.get("scope") or {}}
        if s["metric"] not in RATIO:
            r = evaluate(ts, s, now)
            if r["status"] == "breached" and r["n"] >= min_tasks:
                out[f"slo/{s['id']}/breach"] = dict(base, alert="breach", policy="breach", severity="warning",
                                                   value=r["value"], tasks=r["n"])
            continue
        recent = [t for t in ts if _finished(t) >= horizon]
        for p in BURN_POLICIES:
            if p["long_h"] > s["window_days"] * 24:
                continue
            key, thr = f"slo/{s['id']}/{p['alert']}", burn_threshold(s, p)
            if key in out:                    # an earlier, faster policy already raised this alert
                continue
            long_b, n = burn_rate(recent, s, now - p["long_h"] * 3600, now + 1)
            if long_b is None or n < min_tasks or long_b < thr:
                continue
            short_b, _ = burn_rate(recent, s, now - p["short_h"] * 3600, now + 1)
            if short_b is None and key not in firing:
                continue
            if short_b is not None and short_b < thr:
                continue
            out[key] = dict(base, alert=p["alert"], policy=p["id"], severity=p["severity"], burn_rate=round(long_b, 2),
                            short_burn_rate=None if short_b is None else round(short_b, 2),
                            threshold=round(thr, 2), tasks=n, budget=p["budget"], long_h=p["long_h"],
                            short_h=p["short_h"])
    return out


def burn_rates(tasks, slo, now):
    """{window label: burn} over each policy's long window, for /metrics. Ratio objectives only."""
    if slo["metric"] not in RATIO:
        return {}
    ts = scoped(tasks, slo)
    out = {}
    for p in BURN_POLICIES:
        b, _ = burn_rate(ts, slo, now - p["long_h"] * 3600, now + 1)
        if b is not None:
            out[f"{p['long_h']}h"] = round(b, 4)
    return out
