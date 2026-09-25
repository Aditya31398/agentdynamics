"""Governance (Aegis): decisions, policies in use, policy export and coverage."""
import statistics
import time
from collections import Counter, defaultdict

from ..analysis import pct
from ..store import rows





class GovernanceMixin:
    def _governed(self, q):
        ts = [t for t in self.tasks(dict(q, sub="1")) if t["governed"]]
        return ts

    def _steps_for(self, ids, cols="*", extra=""):
        out = []
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            out += rows(self.con, f"SELECT {cols} FROM steps WHERE task_id IN ({','.join('?' * len(chunk))}){extra}", chunk)
        return out

    def governance(self, q):
        ts = self._governed(q)
        ids = [t["id"] for t in ts]
        by_task = {t["id"]: t for t in ts}
        steps = self._steps_for(ids, "task_id, seq, kind, name, ts, denied, rule, guard, agent, grant_depth, error, text, "
                                     "attributed_cost, cost")
        decisions = [s for s in steps if s["rule"] and s["kind"] in ("tool", "llm", "span")]
        denied = [s for s in steps if s["denied"]]
        tool_dec = [s for s in decisions if s["kind"] == "tool"]
        revokes = [s for s in steps if s["kind"] == "notice" and s["name"] == "revoked"]
        n = len(ts)
        k = {
            "governed_tasks": n, "decisions": len(decisions), "denials": len(denied),
            "denial_rate": round(sum(1 for s in tool_dec if s["denied"]) / len(tool_dec), 4) if tool_dec else 0,
            "budget_stops": sum(1 for s in denied if str(s["rule"]).startswith("budget.")),
            "spend_denials": sum(1 for s in denied if s["kind"] == "llm"),
            "revocations": len(revokes),
            "blocked_cost": round(sum(s["attributed_cost"] or 0 for s in denied), 4),
            "probing_tasks": sum(1 for t in ts if (t["repeated_denials"] or 0) >= 3),
            "compliance": round(statistics.mean([t["scores"].get("compliance") for t in ts if t["scores"] and t["scores"].get("compliance") is not None]), 1)
            if any(t["scores"] and t["scores"].get("compliance") is not None for t in ts) else None,
            "policies": len({t["policy_version"] for t in ts if t["policy_version"]}),
        }
        by_rule = Counter(s["rule"] for s in denied)
        by_tool = Counter(s["name"] for s in denied)
        by_agent = Counter(s["agent"] or "?" for s in denied)
        daily = defaultdict(Counter)
        for s in denied:
            if s["ts"]:
                daily[time.strftime("%Y-%m-%d", time.localtime(s["ts"]))][(s["rule"] or "").split(".")[0]] += 1
        recent = sorted(denied, key=lambda s: -(s["ts"] or 0))[:25]
        return {
            "kpis": k,
            "by_rule": [{"rule": r, "n": c} for r, c in by_rule.most_common(15)],
            "by_tool": [{"tool": r, "n": c} for r, c in by_tool.most_common(15)],
            "by_agent": [{"agent": r, "n": c} for r, c in by_agent.most_common(10)],
            "daily": [{"day": d, **c} for d, c in sorted(daily.items())],
            "guards": sorted({g for c in daily.values() for g in c}),
            "recent": [{"task_id": s["task_id"], "tool": s["name"], "rule": s["rule"], "agent": s["agent"], "ts": s["ts"],
                        "error": (s["error"] or "")[:200], "kind": s["kind"],
                        "prompt": (by_task.get(s["task_id"], {}).get("prompt") or "")[:120],
                        "workflow": by_task.get(s["task_id"], {}).get("workflow")} for s in recent],
            "revocations": [{"task_id": s["task_id"], "ts": s["ts"], "agent": s["agent"], "text": s["text"]}
                            for s in sorted(revokes, key=lambda s: -(s["ts"] or 0))[:15]],
            "policies": self._policy_rows(ts, steps),
        }

    def _policy_docs(self):
        out = {}
        for r in rows(self.con, "SELECT policy_version, policy, started FROM runs WHERE policy IS NOT NULL ORDER BY started"):
            if isinstance(r["policy"], dict):
                out[r["policy_version"]] = r["policy"]
        return out

    def _policy_rows(self, ts, steps):
        docs = self._policy_docs()
        by_pol = defaultdict(list)
        for t in ts:
            if t["policy_version"]:
                by_pol[t["policy_version"]].append(t)
        used_by_task = defaultdict(set)
        for s in steps:
            if s["kind"] == "tool" and not s["denied"] and s["name"] not in ("model.spend", "agent.spawn"):
                used_by_task[s["task_id"]].add(s["name"])
        out = []
        for label, g in sorted(by_pol.items(), key=lambda kv: -len(kv[1])):
            doc = (docs.get(label) or {}).get("doc") or {}
            granted = sorted(e["name"] for e in (doc.get("tools") or {}).get("allow", []) if e["name"] != "agent.spawn")
            called = set().union(*[used_by_task[t["id"]] for t in g]) if g else set()
            # `used` is "of the granted capabilities, which were exercised", so it only counts
            # tools the policy actually covers. An agent also runs plain @tool functions that no
            # kernel mediates; counting those made `used` exceed `granted` ("6 of 5"). They are
            # reported separately, because a tool nothing governs is its own kind of finding.
            used = sorted(called & set(granted))
            ungoverned = sorted(called - set(granted))
            budget = doc.get("budget") or {}
            p95 = {"usd": pct([t["cost"] + (t["subagent_cost"] or 0) for t in g], 0.95),
                   "tokens": pct([t["total_tokens"] for t in g], 0.95),
                   "wall_clock_s": pct([t["wall_s"] or 0 for t in g], 0.95),
                   "tool_calls": pct([t["tool_calls"] for t in g], 0.95)}
            headroom = {k: (round(budget[k] / p95[k], 1) if budget.get(k) and p95[k] else None) for k in p95}
            out.append({"policy": label, "name": (docs.get(label) or {}).get("name"), "tasks": len(g),
                        "success_rate": round(sum(1 for t in g if t["outcome"] == "completed") / len(g), 3),
                        "denials": sum(t["policy_denials"] + t["spend_denials"] for t in g),
                        "revocations": sum(t["revocations"] for t in g),
                        "granted": granted, "used": used, "unused": sorted(set(granted) - set(used)),
                        "ungoverned": ungoverned,
                        "budget": budget, "p95": p95, "headroom": headroom,
                        "workflows": sorted({t["workflow"] for t in g if t["workflow"]})})
        return out

    def export_policy(self, q):
        """Observe -> govern: a tightened policy for a workflow, derived from its governed runs."""
        from ..govern import coverage, render, synthesize
        ts = self._governed(q)
        if q.get("policy"):
            ts = [t for t in ts if t["policy_version"] == q["policy"]]
        if not ts:
            return {"error": "no governed runs match (need runs recorded with agentdynamics.integrations.aegis)"}
        label = q.get("policy") or Counter(t["policy_version"] for t in ts if t["policy_version"]).most_common(1)[0][0]
        base = (self._policy_docs().get(label) or {}).get("doc") if label else None
        if q.get("base_doc"):
            base = q["base_doc"]
        steps = self._steps_for([t["id"] for t in ts], self.GOV_STEP_COLS)
        doc, changes, stats = synthesize(base, [t for t in ts if not t["is_subagent"]], steps,
                                         headroom=float(q.get("headroom") or 1.5))
        scope = ", ".join(f"{k}={q[k]}" for k in ("project", "workflow", "environment", "days") if q.get(k))
        # A candidate that refuses the traffic it was built from ratifies and shows no drift, so
        # nothing downstream would catch it. Check here, where the call history lives.
        cov = coverage(doc, steps)
        regressions = cov["examples"] if cov else []
        note = f"Base: {label or 'none'}. Scope: {scope or 'all governed runs'}."
        if cov and cov["denied"]:
            note += (f" WARNING: this policy would deny {cov['denied']} of {cov['calls']} observed calls "
                     f"({cov['denied_fraction']:.1%}) that were allowed.")
        return {"yaml": render(doc, changes, stats, note),
                "policy": doc, "changes": changes, "stats": stats, "base": label,
                "regressions": regressions, "coverage": cov}

    GOV_STEP_COLS = "task_id, kind, name, denied, rule, agent, grant_depth, args_json, governed"

    def check_policy(self, q):
        """Judge any candidate policy -- hand-edited or generated -- against the recorded traffic in
        scope: how much of what actually ran would it refuse, per tool and per rule."""
        from ..govern import coverage
        doc = q.get("candidate_doc")
        if not isinstance(doc, dict):
            return {"error": "candidate_doc (a policy document) is required"}
        ts = self._governed(q)
        if q.get("policy"):
            ts = [t for t in ts if t["policy_version"] == q["policy"]]
        cov = coverage(doc, self._steps_for([t["id"] for t in ts], self.GOV_STEP_COLS))
        if cov is None:
            return {"error": "checking a policy needs Aegis: pip install aegis-kernel"}
        return {"coverage": cov, "tasks": len(ts)}
