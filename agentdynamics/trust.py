"""Trust: how far an agent's own behaviour says it can be trusted with what it holds.

    [trust]
    half_life_days = 7      # evidence counts half as much a week on
    tripwire = 40           # points for each task in which the agent touched a tripwire
    probing = 15            # ... in which it had 3+ calls refused in a row
    denial_rate = 20        # points at a 100% refusal rate, pro rata below
    confirmed = 1.5         # evidence in an incident a person confirmed as real counts this much more
    watch = 80              # below this: watch
    low = 50                # below this: low

Trust starts at 100 and loses the points of each piece of evidence, weighted by its age. It is about
behaviour, not competence: a failed task or a tool error is a mistake, not an attempt to exceed what the
agent was given, so neither lowers trust -- success rate is shown beside it instead. Refused calls count as a
rate, so an agent is not marked down for being busy, and tripwires and probing count per task, since each is
one attempt to go somewhere the agent had no business going.

A person's verdict wins. Evidence in a task whose incident was resolved as a false alarm doesn't count, and
evidence in one confirmed as real counts `confirmed` times over. Verdicts are why giving one takes an admin
key (incidents.py).

Computed from the tasks still held (`tasks.agents`, analysis.agent_evidence) each time it is read, so it
follows retention, verdicts and the clock without being stored. An agent is the name its steps carry -- an
Aegis grant name, or the agent a source records -- in one project. Only a governed agent can have calls
refused or probe; any named agent can touch a tripwire.
"""
from collections import defaultdict

DEFAULTS = {"half_life_days": 7.0, "tripwire": 40.0, "probing": 15.0, "denial_rate": 20.0, "confirmed": 1.5,
            "watch": 80.0, "low": 50.0}
DAY = 86400


def settings(cfg):
    out = dict(DEFAULTS)
    out.update({k: float(v) for k, v in (cfg.get("trust") or {}).items() if k in DEFAULTS})
    return out


def band(trust, conf):
    return "low" if trust < conf["low"] else "watch" if trust < conf["watch"] else "trusted"


def verdicts(rows):
    """{(project, agent, task_id): "real" | "false_alarm"} from rows of (project, agent, task_id, verdict).
    A task in both a confirmed and a dismissed incident is taken as confirmed."""
    out = {}
    for r in rows:
        k = (r["project"], r["agent"], r["task_id"])
        if out.get(k) != "real":
            out[k] = r["verdict"]
    return out


def score(tasks, judged, now, conf):
    """Trust per (project, agent), lowest first. `tasks`: rows with id, project, started, ended, outcome, agents;
    `judged`: verdicts()."""
    half = max(conf["half_life_days"], 1e-6) * DAY
    acc = defaultdict(lambda: {"tasks": 0, "completed": 0, "calls": 0, "denied": 0, "w_calls": 0.0, "w_denied": 0.0,
                               "evidence": []})
    for t in tasks:
        ts = t.get("ended") or t.get("started") or now
        w = 0.5 ** (max(0.0, now - ts) / half)
        for agent, a in (t.get("agents") or {}).items():
            x = acc[(t["project"], agent)]
            x["tasks"] += 1
            x["completed"] += 1 if t.get("outcome") == "completed" else 0
            verdict = judged.get((t["project"], agent, t["id"]))
            x["calls"] += a["calls"]
            x["w_calls"] += w * a["calls"]
            if verdict == "false_alarm":
                continue                   # a person said this wasn't it: none of it counts
            x["denied"] += a["denied"]
            x["w_denied"] += w * a["denied"]
            f = conf["confirmed"] if verdict == "real" else 1.0
            for kind, hit in (("tripwire", a["touches"] > 0), ("probing", a["streak"] >= 3)):
                if hit:
                    x["evidence"].append({"kind": kind, "task_id": t["id"], "ts": ts, "verdict": verdict,
                                          "points": round(conf[kind] * f, 2), "now": round(conf[kind] * f * w, 2)})
    out = []
    for (project, agent), x in acc.items():
        rate = x["w_denied"] / x["w_calls"] if x["w_calls"] else 0.0
        penalty = {"tripwire": sum((e["now"] for e in x["evidence"] if e["kind"] == "tripwire"), 0.0),
                   "probing": sum((e["now"] for e in x["evidence"] if e["kind"] == "probing"), 0.0),
                   "denial_rate": conf["denial_rate"] * rate}
        trust = round(max(0.0, 100.0 - sum(penalty.values())), 1)
        ev = sorted(x["evidence"], key=lambda e: (-e["now"], -e["ts"], e["task_id"], e["kind"]))
        out.append({"project": project, "agent": agent, "trust": trust, "band": band(trust, conf),
                    "penalty": {k: round(v, 2) for k, v in penalty.items()},
                    "tasks": x["tasks"], "success_rate": round(x["completed"] / x["tasks"], 4) if x["tasks"] else None,
                    "calls": x["calls"], "denied": x["denied"], "denial_rate": round(rate, 4),
                    "tripwire_tasks": sum(1 for e in ev if e["kind"] == "tripwire"),
                    "probing_tasks": sum(1 for e in ev if e["kind"] == "probing"),
                    "last_evidence": max((e["ts"] for e in ev), default=None), "evidence": ev[:10]})
    out.sort(key=lambda r: (r["trust"], r["project"] or "", r["agent"]))
    return out
