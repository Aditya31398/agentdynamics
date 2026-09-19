"""Aegis audit logs -> AgentDynamics runs (out-of-process path).

For agents that run Aegis without the in-process integration (a different language runtime, CI
conformance runs, historical logs), point the inbox at the JSONL audit file (`AuditLog(path=...)`)
or POST records to /api/ingest/records. Records are grouped into runs by the correlation id Aegis
stamps into `details.ctx` (run_id / trace_id) when a context provider is registered, otherwise by
grant id.

Aegis records hash the arguments rather than storing them, so these runs show *what was decided*
(tool, verdict, rule, agent, depth) but not argument values. Policy export needs argument values and
therefore uses the in-process integration.
"""
from __future__ import annotations

REQUIRED = {"seq", "grant_id", "prev_hash", "rule", "allowed", "tool"}


def is_record(rec):
    return isinstance(rec, dict) and REQUIRED <= set(rec)


def group_key(rec):
    ctx = (rec.get("details") or {}).get("ctx") or {}
    return ctx.get("run_id") or ctx.get("trace_id") or f"grant:{rec.get('grant_id')}"


def span_id(rec):
    return rec.get("hash") or f"{rec.get('grant_id')}:{rec.get('seq')}:{rec.get('ts')}"


def build_payload(group, records):
    """Generic-run payload for one group of audit records."""
    records = sorted(records, key=lambda r: (r.get("ts") or 0, r.get("seq") or 0))
    ctx = next(((r.get("details") or {}).get("ctx") for r in records if (r.get("details") or {}).get("ctx")), {}) or {}
    t0 = records[0].get("ts")
    steps = [{"kind": "prompt", "ts": t0, "text": ctx.get("workflow") or f"Aegis-governed run ({records[0].get('agent')})"}]
    pending = {}  # (grant, tool, args_digest) -> index of an admitted step a later record may deny
    for r in records:
        tool, rule, ts = r.get("tool"), r.get("rule"), r.get("ts")
        det = r.get("details") or {}
        base = {"ts": ts, "end_ts": ts, "agent": r.get("agent"), "grant_depth": r.get("depth"), "governed": True,
                "rule": rule, "guard": r.get("guard"), "node": (det.get("ctx") or {}).get("node")}
        if tool == "model.spend":
            if rule == "budget.settled":
                steps.append({**base, "kind": "llm", "model": "model", "cost": float(det.get("usd") or 0),
                              "input_tokens": int(det.get("tokens") or 0), "output_tokens": 0})
            elif not r.get("allowed"):
                steps.append({**base, "kind": "llm", "model": "model", "denied": True, "error": r.get("reason")})
            continue
        if tool == "agent.revoke":
            steps.append({"kind": "notice", "name": "revoked", "ts": ts, "agent": r.get("agent"), "rule": rule,
                          "text": f"{r.get('agent')}: revoked"})
            continue
        key = (r.get("grant_id"), tool, r.get("args_digest"))
        if not r.get("allowed") and key in pending:
            # a budget charge or post-guard refused a call that had been admitted: one call, denied
            i = pending.pop(key)
            steps[i].update(denied=True, rule=rule, guard=r.get("guard"), error=f"[{rule}] {r.get('reason') or ''}")
            continue
        if tool == "agent.spawn" and r.get("allowed"):
            steps.append({**base, "kind": "span", "span_kind": "agent", "name": "sub-agent", "rule": "spawn.granted",
                          "start_ts": ts})
            continue
        step = {**base, "kind": "tool", "name": tool}
        if r.get("allowed"):
            pending[key] = len(steps)
        else:
            step.update(denied=True, error=f"[{rule}] {r.get('reason') or ''}")
        steps.append(step)
    return {"id": f"aegis-{group}", "source": "aegis", "workflow": ctx.get("workflow") or "aegis-governed",
            "project": ctx.get("project") or "aegis", "agent": records[0].get("agent"), "framework": "aegis",
            "status": "ok", "complete": True, "steps": steps}
