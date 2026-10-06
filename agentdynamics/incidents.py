"""Incidents: the security signals about one agent, grouped, so a person gets one thing to judge.

A tripwire touched in one task, probing in the next, the directive that revoked the agent: three events and a
directive, one story. An incident is that story for one agent in one project -- or, for signals from agents
without an Aegis name (OTLP, LangSmith), one workflow. It opens with its first signal, takes each later one
until it goes quiet for `gap_hours`, and stays open until a person resolves it as real or a false alarm. A
signal arriving after that opens a new incident: "it happened again" is news.

    [incidents]
    rules = ["tripwire", "repeated_denials", "revoked", "policy_denials"]   # health rules that are signals
    gap_hours = 24

Every directive (probing, tripwire, operator) is a signal too, of the agent it names.

Incidents are durable (store.py): a verdict is something a person said, and must survive a rebuild. They are
written only by the engine that writes the analysis, and only ever gain signals, each once (`incident_signals`
is keyed by the signal). An incident's id is a hash of its key and first signal, so a rebuild, or a second
store fed the same traffic, derives the same incidents. Verdicts are recorded now; the trust score will read
them, which is why giving one needs an admin key.
"""
import hashlib
import json
from collections import Counter

RULES = ("tripwire", "repeated_denials", "revoked", "policy_denials")
SEV = {"info": 1, "warning": 2, "critical": 3}
VERDICTS = ("real", "false_alarm")
LABELS = {"tripwire": "tripwire", "repeated_denials": "probing", "revoked": "revoked mid-run",
          "policy_denials": "refused calls", "directive.probing": "directive (probing)",
          "directive.tripwire": "directive (tripwire)", "directive.operator": "directive (operator)",
          "restrict.operator": "restricted (operator)"}


def _is_evidence(s):
    return s.get("tripwire") or s.get("denied") or (s.get("kind") == "notice" and s.get("name") == "revoked")


def from_events(events, runs, tasks, rules=RULES):
    """Signals from health-rule events. The agent is the one behind most of the task's evidence steps (the
    touching, refused or revoked ones); without one, the workflow stands in."""
    out = []
    for e in events:
        if e["rule_id"] not in rules:
            continue
        run = runs.get(e["run_id"]) or {}
        agents = Counter(s["agent"] for s in run.get("steps") or ()
                         if s.get("task_id") == e["task_id"] and s.get("agent") and _is_evidence(s))
        agent = min(agents, key=lambda a: (-agents[a], a)) if agents else None
        t = tasks.get(e["task_id"]) or {}
        workflow = t.get("workflow") or e.get("task_type")
        out.append({"ref": f"event:{e['id']}", "kind": "event", "ts": e.get("ts") or t.get("ended") or 0,
                    "rule": e["rule_id"], "severity": e["severity"], "task_id": e["task_id"], "run_id": e["run_id"],
                    "project": e["project"], "agent": agent, "workflow": None if agent else workflow,
                    "detail": {"rule": e["rule"], "message": e["message"], "workflow": workflow,
                               "what": t.get("tripwire_what")}})
    return out


def from_directives(directives):
    """A revocation is a critical signal, a restriction a warning. One the server renews on its own while an
    agent's trust stays low (source "trust") is not: it is a consequence of evidence the incident already
    holds, and renewed every hour it would open an incident each time."""
    return [{"ref": f"directive:{d['id']}", "kind": "directive", "ts": d["created"],
             "rule": f"{'restrict' if d.get('kind') == 'restrict' else 'directive'}.{d['source']}",
             "severity": "warning" if d.get("kind") == "restrict" else "critical", "task_id": None, "run_id": None,
             "project": d["project"], "agent": d["agent"], "workflow": None,
             "detail": {"directive": d["id"], "reason": d["reason"], "source": d["source"], "expires": d["expires"],
                        "kind": d.get("kind") or "revoke", "spec": d.get("spec")}}
            for d in directives if d["source"] != "trust"]


def key(x):
    return (x["project"], x["agent"], x["workflow"])


def group(new, open_, gap_s, resolved=None):
    """Attach each new signal (oldest first) to its key's open incident, or open one. Returns the incidents
    touched, by id -- one key can open several in a batch, split by the gap; each signal gets its
    `incident_id`. `open_` and `resolved` are store.open_incidents() for each status.

    A directive issued before its agent's latest incident was resolved joins that incident, resolved as it
    is: it is what was done about it (an operator revokes, then gives the verdict, before the directive is
    picked up). An event is evidence, and evidence arriving after a verdict opens a new incident."""
    touched, current = {}, dict(open_)
    for s in sorted(new, key=lambda s: (s["ts"] or 0, s["ref"])):
        k = key(s)
        inc = current.get(k)
        done = (resolved or {}).get(k)
        if inc is None and s["kind"] == "directive" and done and (s["ts"] or 0) <= (done["resolved_at"] or 0):
            inc = touched.get(done["id"]) or done
        if inc is None or (inc["status"] == "open" and (s["ts"] or 0) - (inc["updated"] or 0) > gap_s):
            iid = hashlib.sha1(json.dumps([*k, s["ref"]]).encode()).hexdigest()[:12]
            inc = {"id": iid, "project": s["project"], "agent": s["agent"], "workflow": s["workflow"],
                   "opened": s["ts"], "updated": s["ts"], "status": "open", "severity": s["severity"], "signals": 0,
                   "verdict": None, "note": None, "resolved_by": None, "resolved_at": None, "alerted": None}
        else:
            inc = dict(inc)
        inc["signals"] = int(inc["signals"] or 0) + 1
        inc["opened"] = min(inc["opened"], s["ts"])
        inc["updated"] = max(inc["updated"], s["ts"])
        if SEV.get(s["severity"], 1) > SEV.get(inc["severity"], 1):
            inc["severity"] = s["severity"]
        s["incident_id"] = inc["id"]
        touched[inc["id"]] = inc
        if inc["status"] == "open":
            current[k] = inc
    return touched


def subject(inc):
    return inc["agent"] or (f"workflow {inc['workflow']}" if inc["workflow"] else "every agent")


def counts(signals):
    """{label: n}, most first: what an incident is made of."""
    c = Counter(LABELS.get(s["rule"], s["rule"]) for s in signals)
    return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def title(inc, signals):
    what = ", ".join(f"{k} ×{n}" if n > 1 else k for k, n in counts(signals).items())
    where = f" in {inc['project']}" if inc["project"] else ""
    return f"{subject(inc)}{where}: {what}"
