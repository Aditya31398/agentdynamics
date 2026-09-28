"""Governance (Aegis): decisions, policies in use, policy export and coverage."""
import statistics
import time
from collections import Counter, defaultdict

from .. import incidents as incmod
from ..analysis import pct
from ..store import incident_signals, incidents as list_incidents, revocations as list_revocations, rows

DAY = 86400





class GovernanceMixin:
    def _governed(self, q):
        ts = [t for t in self.tasks(dict(q, sub="1")) if t["governed"]]
        return ts

    def _steps_for(self, ids, cols="*", extra=""):
        out = []
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            out += rows(self.con, f"SELECT {cols} FROM steps WHERE task_id IN ({','.join('?' * len(chunk))}){extra} "
                                  "ORDER BY task_id, seq", chunk)
        return out

    def revocations(self, q):
        """Revocation directives (#8), newest first, each with its status. `active=1` and `project=` are what
        the in-process poller asks for."""
        now = time.time()
        out = list_revocations(self.con, now, active=q.get("active") == "1", project=q.get("project"))
        for d in out:
            d["status"] = "cleared" if d["cleared"] else "expired" if d["expires"] <= now else "active"
        return {"revocations": out, "now": now, "probing": (self.e.cfg.get("enforcement") or {}).get("probing")}

    def _tripwires(self, q):
        """Steps that touched a tripwire, newest first, in every task of the window -- governed or not: a decoy
        can be touched by any agent. A touch is named by its label; a canary's value appears nowhere."""
        ts = self.tasks(dict(q, sub="1"), " AND t.tripwires > 0")        # where() always has a clause
        by_task = {t["id"]: t for t in ts}
        hits = self._steps_for(list(by_task), "task_id, seq, kind, name, ts, agent, tripwire", " AND tripwire IS NOT NULL")
        hits.sort(key=lambda s: (-(s["ts"] or 0), s["task_id"], s["seq"]))
        cfg = (self.e.cfg.get("enforcement") or {}).get("tripwires") or {}
        wires = self.e.tripwires
        on = bool(cfg) and float(cfg.get("revoke_minutes", 60)) > 0
        return {"tasks": len(ts), "touches": len(hits),
                # how many, not which: the list is install-wide config, and a scoped key reads this route
                "set": {"tools": len(wires.tools) if wires else 0, "canaries": len(wires.canaries) if wires else 0},
                "directives": {"runs": int(cfg.get("runs", 2)), "window_minutes": float(cfg.get("window_minutes", 60)),
                               "revoke_minutes": float(cfg.get("revoke_minutes", 60))} if on else None,
                "recent": [{"task_id": s["task_id"], "ts": s["ts"], "what": s["tripwire"], "agent": s["agent"],
                            "step": s["name"], "kind": s["kind"], "workflow": by_task[s["task_id"]]["workflow"],
                            "project": by_task[s["task_id"]]["project"],
                            "prompt": (by_task[s["task_id"]]["prompt"] or "")[:120]} for s in hits[:25]]}

    # ------------------------------------------------------------------ incidents (incidents.py)
    @staticmethod
    def _incident_row(r, signals):
        return dict(r, subject=incmod.subject(r), title=incmod.title(r, signals), counts=incmod.counts(signals),
                    tasks=len({s["task_id"] for s in signals if s["task_id"]}),
                    what=sorted({s["detail"].get("what") for s in signals if s["detail"].get("what")}),
                    workflows=sorted({s["detail"].get("workflow") for s in signals if s["detail"].get("workflow")}))

    def incidents(self, q):
        """Security incidents, most recently active first. Every open one, whatever `days` says -- an incident
        waiting for a verdict doesn't expire -- and resolved ones last active within `days`."""
        since = time.time() - float(q["days"]) * DAY if q.get("days") else None
        status = q.get("status")
        found = []
        if status in (None, "open"):
            found += list_incidents(self.con, status="open", project=q.get("project"))
        if status in (None, "resolved"):
            found += list_incidents(self.con, status="resolved", since=since, project=q.get("project"))
        found.sort(key=lambda r: (r["status"] != "open", -(r["updated"] or 0), r["id"]))
        sig = incident_signals(self.con, [r["id"] for r in found])
        out = [self._incident_row(r, sig[r["id"]]) for r in found]
        return {"incidents": out, "open": sum(1 for r in out if r["status"] == "open"),
                "critical_open": sum(1 for r in out if r["status"] == "open" and r["severity"] == "critical")}

    def incident(self, iid):
        """One incident: its signals, the steps that are its evidence, and what can be done about it."""
        found = rows(self.con, "SELECT * FROM incidents WHERE id = ?", (iid,))
        if not found:
            return None
        sig = incident_signals(self.con, [iid])[iid]
        inc = self._incident_row(found[0], sig)
        ids = sorted({s["task_id"] for s in sig if s["task_id"]})
        tasks = {}
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            tasks.update({t["id"]: t for t in rows(
                self.con, f"SELECT id, prompt, workflow, outcome, policy_version, started FROM tasks "
                          f"WHERE id IN ({','.join('?' * len(chunk))})", chunk)})
        for s in sig:
            t = tasks.get(s["task_id"]) or {}
            s["label"] = incmod.LABELS.get(s["rule"], s["rule"])
            s["prompt"] = (t.get("prompt") or "")[:160]
            s["held"] = bool(t) or not s["task_id"]       # a task past retention is gone; its signal stays
        evidence = self._steps_for(list(tasks), "task_id, seq, kind, name, ts, agent, rule, denied, tripwire, error, text",
                                   " AND (tripwire IS NOT NULL OR denied = 1 OR (kind = 'notice' AND name = 'revoked'))")
        evidence.sort(key=lambda s: (s["ts"] or 0, s["task_id"], s["seq"]))
        now = time.time()
        active = [d for d in list_revocations(self.con, now, active=True, project=inc["project"])
                  if d["agent"] in (inc["agent"], None)] if inc["agent"] else []
        actions = []
        if inc["agent"]:
            if active:
                actions.append({"kind": "revoked", "directive": active[0]["id"], "until": active[0]["expires"]})
            else:
                # recommended when the evidence is of intent -- a tripwire, probing -- not a few refused calls
                actions.append({"kind": "revoke", "agent": inc["agent"], "project": inc["project"], "minutes": 60,
                                "recommended": any(s["rule"] in ("tripwire", "repeated_denials") or s["kind"] == "directive"
                                                   for s in sig)})
        for pv in sorted({t["policy_version"] for t in tasks.values() if t.get("policy_version")}):
            actions.append({"kind": "tighten", "policy": pv})
        return {"incident": inc, "signals": sig, "evidence": [{k: s[k] for k in s} for s in evidence[:100]],
                "evidence_total": len(evidence), "actions": actions}

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
            "tripwires": self._tripwires(q),
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
