"""Trust: how far an agent's own behaviour says it can be trusted with what it holds.

    [trust]
    half_life_days = 7      # evidence counts half as much a week on
    tripwire = 10           # a task in which the agent touched a tripwire counts as this many bad tasks
    probing = 3             # ... one in which it had 3+ calls refused in a row
    denial_rate = 1         # ... one in which all its calls were refused (pro rata below)
    confirmed = 1.5         # evidence in an incident a person confirmed as real counts this much more
    prior_clean = 19        # where every agent starts: as if it had 19 clean tasks and 1 bad one (95%)
    prior_bad = 1
    friction_share = 0.5    # refusals under a rule this share of a project's agents hit (and at least
    friction_agents = 3     # this many) are the policy's friction, not the agent's misbehaviour
    watch = 80              # below this: watch
    low = 50                # below this: low

A reputation, counted like evidence for a coin's bias: each task an agent works in is a clean task, or bad
evidence weighing as many bad tasks as its severity says, both counting half as much a week on. Trust is the
lower bound (10th percentile) of what that evidence and the prior say the share of clean tasks is, times 100.
So it is volume-aware -- one tripwire after 10 tasks says far more than one after 10,000 -- and uncertainty
shows: a new agent starts near 89, and earns its way up with clean work. Tripwires stop a run and revoke an
agent on their own (tripwires.py, [enforcement.tripwires]), whatever its trust.

It is about behaviour, not competence: a failed task or a tool error is a mistake, not an attempt to exceed
what the agent was given, so neither counts -- success rate is shown beside it instead. Refused calls count as
a share of the task's calls, so being busy costs nothing; and refusals under a rule most of a project's agents
hit are the policy's friction, reported as such and not held against any one agent.

A person's verdict wins. A task whose incident was resolved as a false alarm counts as clean, and evidence in
one confirmed as real counts `confirmed` times over. Verdicts are why giving one takes an admin key
(incidents.py).

Computed from the tasks still held (`tasks.agents`, analysis.agent_evidence) each time it is read, so it
follows retention, verdicts and the clock without being stored. An agent is the name its steps carry -- an
Aegis grant name, or the agent a source records -- in one project. Only a governed agent can have calls
refused or probe; any named agent can touch a tripwire.
"""
import math
from collections import defaultdict

DEFAULTS = {"half_life_days": 7.0, "tripwire": 10.0, "probing": 3.0, "denial_rate": 1.0, "confirmed": 1.5,
            "prior_clean": 19.0, "prior_bad": 1.0, "friction_share": 0.5, "friction_agents": 3.0,
            "watch": 80.0, "low": 50.0}
DAY = 86400
Z10 = 1.2816            # the 10th percentile of a normal: trust is a cautious estimate, not the average


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


def lower_bound(clean, bad):
    """The 10th percentile of a Beta(clean, bad) share of clean tasks (normal approximation), in 0..1."""
    n = clean + bad
    mean = clean / n
    sd = math.sqrt(clean * bad / (n * n * (n + 1)))
    return max(0.0, min(1.0, mean - Z10 * sd))


def friction(tasks, conf):
    """{project: rules} whose refusals are the policy's friction: hit by at least friction_share of the project's
    governed agents, and by at least friction_agents of them."""
    agents, hit = defaultdict(set), defaultdict(lambda: defaultdict(set))
    for t in tasks:
        for agent, a in (t.get("agents") or {}).items():
            if a.get("calls"):
                agents[t["project"]].add(agent)
            for rule in (a.get("rules") or {}):
                hit[t["project"]][rule].add(agent)
    need = {p: max(conf["friction_agents"], conf["friction_share"] * len(a)) for p, a in agents.items()}
    return {p: {r for r, who in rules.items() if len(who) >= need.get(p, float("inf"))} for p, rules in hit.items()}


def score(tasks, judged, now, conf):
    """Trust per (project, agent), lowest first. `tasks`: rows with id, project, started, ended, outcome, agents;
    `judged`: verdicts()."""
    half = max(conf["half_life_days"], 1e-6) * DAY
    fr = friction(tasks, conf)
    acc = defaultdict(lambda: {"tasks": 0, "completed": 0, "calls": 0, "denied": 0, "friction": 0, "w_calls": 0.0,
                               "w_denied": 0.0, "clean": 0.0, "bad": defaultdict(float), "evidence": [], "misused": set()})
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
                x["clean"] += w                # a person said this wasn't it: a clean task
                continue
            rules = a.get("rules")
            policy = sum(n for r, n in (rules or {}).items() if r in fr.get(t["project"], ()))
            own = a["denied"] - policy         # refusals that are the agent's, not the policy's
            x["denied"] += own
            x["friction"] += policy
            x["w_denied"] += w * own
            x["misused"].update(a.get("misused") or ())
            f = conf["confirmed"] if verdict == "real" else 1.0
            bad = 0.0
            for kind, weight in (("tripwire", conf["tripwire"] if a["touches"] > 0 else 0.0),
                                 ("probing", conf["probing"] if a["streak"] >= 3 else 0.0),
                                 ("denial_rate", conf["denial_rate"] * own / a["calls"] if a["calls"] else 0.0)):
                if weight:
                    bad += weight * f
                    x["bad"][kind] += w * weight * f
                    if kind != "denial_rate":
                        x["evidence"].append({"kind": kind, "task_id": t["id"], "ts": ts, "verdict": verdict,
                                              "points": round(weight * f, 2), "now": round(weight * f * w, 2)})
            x["clean"] += w * max(0.0, 1.0 - bad)
    out = []
    for (project, agent), x in acc.items():
        rate = x["w_denied"] / x["w_calls"] if x["w_calls"] else 0.0
        clean, bad = conf["prior_clean"] + x["clean"], conf["prior_bad"] + sum(x["bad"].values())
        trust = round(100.0 * lower_bound(clean, bad), 1)
        ev = sorted(x["evidence"], key=lambda e: (-e["now"], -e["ts"], e["task_id"], e["kind"]))
        out.append({"project": project, "agent": agent, "trust": trust, "band": band(trust, conf),
                    # bad evidence by kind, in bad-task equivalents after decay; `clean`, the clean tasks after decay
                    "penalty": {k: round(x["bad"].get(k, 0.0), 2) for k in ("tripwire", "probing", "denial_rate")},
                    "clean": round(x["clean"], 2), "prior": [conf["prior_clean"], conf["prior_bad"]],
                    "tasks": x["tasks"], "success_rate": round(x["completed"] / x["tasks"], 4) if x["tasks"] else None,
                    "calls": x["calls"], "denied": x["denied"], "denial_rate": round(rate, 4),
                    "friction_denied": x["friction"], "friction_rules": sorted(fr.get(project, ())),
                    "tripwire_tasks": sum(1 for e in ev if e["kind"] == "tripwire"),
                    "probing_tasks": sum(1 for e in ev if e["kind"] == "probing"),
                    "last_evidence": max((e["ts"] for e in ev), default=None), "evidence": ev[:10],
                    "misused": sorted(x["misused"])})
    out.sort(key=lambda r: (r["trust"], r["project"] or "", r["agent"]))
    return out
