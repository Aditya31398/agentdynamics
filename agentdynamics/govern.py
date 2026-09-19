"""Observe -> govern: turn what agents actually did into a least-privilege Aegis policy.

Given the policy runs were governed by (the *base*) and the runs themselves, `synthesize` produces a
policy that can only be tighter than the base:

  tools    only tools that were actually called (and allowed); unused grants are dropped
  args     observed path/URL prefixes narrow a base prefix; small categorical sets become `one_of`;
           `max_len` shrinks to 2x the longest observed value; base regexes are kept as they are
  budget   p99 of real per-task usage x headroom, never above the base
  spawn    depth / fan-out actually used, never above the base; no spawning if none was observed
  data     unchanged (egress sinks are kept even for dropped tools; removing one reads as widening)

Without a base it drafts a standalone policy from observation alone (review it before use).
Every change is reported so the diff can be reviewed like any other PR. Verify the result with
`aegis ratify` and `aegis drift --baseline <base> --candidate <generated>`.
"""
from __future__ import annotations

import copy
import json
import math
import os
from collections import defaultdict

from .analysis import pct

INTERNAL_TOOLS = {"model.spend", "agent.spawn", "agent.revoke"}
OUTWARD_HINTS = ("http", "post", "send", "email", "mail", "slack", "webhook", "upload", "write", "publish", "notify")


def _round_up(x, step):
    return math.ceil(x / step) * step if x > 0 else step


def _common_prefix(values):
    if not values:
        return ""
    p = os.path.commonprefix(values)
    cut = p.rfind("/")
    return p[:cut + 1] if cut >= 0 else ""


def _looks_pathlike(values):
    return all(isinstance(v, str) and ("/" in v) for v in values)


def _arg_constraints(name, values, base_c, n_calls, changes, tool):
    """Tighten one argument's constraint from observed values."""
    c = dict(base_c or {})
    strs = [v if isinstance(v, str) else json.dumps(v) for v in values]
    if not strs:
        return c
    if _looks_pathlike(strs):
        prefix = _common_prefix(strs)
        base_prefix = c.get("prefix")
        if prefix and len(prefix) > 1 and (base_prefix is None or (prefix.startswith(base_prefix) and prefix != base_prefix)):
            if base_prefix is None and "matches" in c:
                pass  # a base regex already shapes this argument; don't guess how a prefix composes with it
            else:
                c["prefix"] = prefix
                changes.append(f"{tool}.{name}: prefix {base_prefix!r} -> {prefix!r} (observed in {len(strs)} calls)")
    distinct = sorted(set(strs))
    if (len(distinct) <= 5 and n_calls >= 5 and all(len(v) <= 64 for v in distinct) and not _looks_pathlike(strs)
            and "one_of" not in c and "matches" not in c and "prefix" not in c):
        c["one_of"] = distinct
        changes.append(f"{tool}.{name}: one_of {distinct}")
    longest = max(len(v) for v in strs)
    new_len = max(64, _round_up(longest * 2, 64))
    if c.get("max_len") is None or new_len < c["max_len"]:
        changes.append(f"{tool}.{name}: max_len {c.get('max_len')} -> {new_len} (longest observed {longest})")
        c["max_len"] = new_len
    return c


def synthesize(base_doc, tasks, steps, headroom=1.5, name=None):
    """Return (policy_doc, changes, stats). `steps` are tool/span steps of the given tasks."""
    changes = []
    base = copy.deepcopy(base_doc) if base_doc else None
    base_tools = {t["name"]: t for t in (base or {}).get("tools", {}).get("allow", [])}
    calls = defaultdict(list)
    agents_by_tool = defaultdict(set)
    spawn_depth = 0
    fanout = defaultdict(int)
    for s in steps:
        if s.get("kind") == "span" and s.get("rule") == "spawn.granted":
            fanout[(s.get("task_id"), s.get("agent"))] += 1
            continue
        if s.get("kind") != "tool" or s.get("denied") or s.get("name") in INTERNAL_TOOLS:
            continue
        if s.get("grant_depth"):
            spawn_depth = max(spawn_depth, int(s["grant_depth"]))
        try:
            args = json.loads(s["args_json"]) if s.get("args_json") else {}
        except ValueError:
            args = {}
        calls[s["name"]].append(args if isinstance(args, dict) else {})
        agents_by_tool[s["name"]].add((s.get("grant_depth") or 0) > 0)

    used = sorted(calls)
    if base_tools:
        unused = sorted(set(base_tools) - set(used) - {"agent.spawn"})
        for t in unused:
            changes.append(f"tools: removed unused grant '{t}'")
        outside = sorted(set(used) - set(base_tools))
        used = [t for t in used if t in base_tools]  # never grant something the base did not
        for t in outside:
            changes.append(f"tools: '{t}' was called but is not in the base policy; not granted")
    allow = []
    for tool in used:
        entry = copy.deepcopy(base_tools.get(tool, {"name": tool}))
        observed = calls[tool]
        arg_names = sorted({k for a in observed for k in a})
        args = dict(entry.get("args") or {})
        for a in arg_names:
            vals = [o[a] for o in observed if a in o and o[a] is not None]
            args[a] = _arg_constraints(a, vals, args.get(a), len(observed), changes, tool)
        if args:
            entry["args"] = args
        always = sorted(a for a in arg_names if all(a in o for o in observed))
        req = sorted(set(entry.get("require_args") or []) | set(always))
        if req:
            if req != sorted(entry.get("require_args") or []):
                changes.append(f"{tool}: require_args {sorted(entry.get('require_args') or [])} -> {req}")
            entry["require_args"] = req
        allow.append(entry)

    # budget from real per-task usage
    cost = [(t.get("cost") or 0) + (t.get("subagent_cost") or 0) for t in tasks]
    obs = {
        "usd": max(0.01, round((pct(cost, 0.99) or 0) * headroom, 4)),
        "tokens": int(_round_up((pct([t.get("total_tokens") or 0 for t in tasks], 0.99) or 0) * headroom, 1000)),
        "wall_clock_s": float(_round_up((pct([t.get("wall_s") or 0 for t in tasks], 0.99) or 0) * headroom, 30)),
        "tool_calls": int(max(5, math.ceil((pct([t.get("tool_calls") or 0 for t in tasks], 0.99) or 0) * headroom))),
    }
    bbase = (base or {}).get("budget") or {}
    budget = {}
    for k, v in obs.items():
        b = bbase.get(k)
        budget[k] = min(v, b) if b else v
        if b is not None and budget[k] < b:
            changes.append(f"budget.{k}: {b} -> {budget[k]} (p99 observed x {headroom})")

    # spawning
    sbase = (base or {}).get("spawn") or {}
    max_fan = max(fanout.values()) if fanout else 0
    if spawn_depth == 0 and not fanout:
        spawn = {"max_depth": 0, "max_fanout": 0, "max_descendants": 0,
                 "child_budget_fraction": sbase.get("child_budget_fraction", 0.5), "allow_tools": []}
        if sbase.get("max_depth"):
            changes.append(f"spawn: no sub-agents observed; max_depth {sbase.get('max_depth')} -> 0")
    else:
        spawn = {"max_depth": min(spawn_depth or 1, sbase.get("max_depth", spawn_depth or 1)),
                 "max_fanout": min(max(max_fan, 1), sbase.get("max_fanout", max(max_fan, 1))),
                 "max_descendants": min(max(sum(fanout.values()), 1), sbase.get("max_descendants", 10 ** 6)),
                 "child_budget_fraction": sbase.get("child_budget_fraction", 0.5)}
        child_tools = sorted(t for t, flags in agents_by_tool.items() if True in flags)
        base_at = sbase.get("allow_tools")
        spawn["allow_tools"] = sorted(set(child_tools) & set(base_at)) if base_at is not None else child_tools
        for k in ("max_depth", "max_fanout", "max_descendants"):
            if sbase.get(k) is not None and spawn[k] < sbase[k]:
                changes.append(f"spawn.{k}: {sbase[k]} -> {spawn[k]}")
    if spawn["max_depth"] > 0 or fanout:
        if "agent.spawn" in base_tools or not base_tools:
            allow.append({"name": "agent.spawn"})
    elif "agent.spawn" in base_tools:
        changes.append("tools: removed unused grant 'agent.spawn'")

    data = copy.deepcopy((base or {}).get("data")) if base else None
    granted = {e["name"] for e in allow}
    if data:
        pass  # keep egress sinks as they are: Aegis drift treats dropping a sink as widening, even for a removed tool
    else:
        data = {"max_classification": "internal",
                "egress": {"sinks": sorted(t for t in granted if any(h in t.lower() for h in OUTWARD_HINTS)),
                           "max_classification": "public", "block_pii": ["email", "credit_card", "api_key", "private_key"]}}
    doc = {"name": name or ((base or {}).get("name", "observed") + "-observed"),
           "version": int((base or {}).get("version", 0)) + 1}
    if base and base.get("effects"):
        doc["effects"] = base["effects"]
    doc.update({"tools": {"allow": allow}, "budget": budget, "data": data, "spawn": spawn})
    stats = {"tasks": len(tasks), "tool_calls": sum(len(v) for v in calls.values()), "tools_used": len(used),
             "tools_granted_base": len(base_tools) or None, "observed_budget": obs}
    return doc, changes, stats


# ---------------------------------------------------------------- minimal YAML emitter (no dependency)

def _scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v))  # JSON strings are valid YAML double-quoted scalars


def to_yaml(obj, indent=0):
    pad = "  " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(to_yaml(v, indent + 1))
            elif isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}: {'{}' if isinstance(v, dict) else '[]'}")
            else:
                lines.append(f"{pad}{k}: {_scalar(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and item:
                inner = to_yaml(item, indent + 1).split("\n")
                lines.append(f"{pad}- {inner[0].strip()}")
                lines.extend(inner[1:])
            elif isinstance(item, list):
                lines.append(f"{pad}- {json.dumps(item)}")
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    return "\n".join(lines)


def render(doc, changes, stats, source=""):
    head = ["# Generated by `agentdynamics policy export` from observed agent behaviour.",
            f"# {source}".rstrip(),
            f"# Based on {stats['tasks']} tasks and {stats['tool_calls']} allowed tool calls.",
            "# Verify before use:  aegis ratify --policy <this file>",
            "#                     aegis drift --baseline <base> --candidate <this file>",
            "#", "# Changes:"]
    head += [f"#   - {c}" for c in changes] or ["#   (none)"]
    return "\n".join(h for h in head if h != "#") + "\n\n" + to_yaml(doc) + "\n"
