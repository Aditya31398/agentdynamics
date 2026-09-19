"""Collector for Claude Code session transcripts (~/.claude/projects/**/*.jsonl).

Normalizes each transcript file into a *run*: session metadata plus an ordered
list of steps (prompt / llm / tool / notice). Analysis never looks at the raw
transcript format, so other agent frameworks only need their own collector.
"""
import hashlib
import json
import os
from datetime import datetime

from .. import pricing
from ..phases import classify

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _content_len(c):
    if c is None:
        return 0
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        n = 0
        for b in c:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    n += len(b.get("text", ""))
                elif b.get("type") == "image":
                    n += 6000  # ~1.5k tokens per image, rough
                else:
                    n += len(json.dumps(b))
        return n
    return len(json.dumps(c))


def _text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _classify_user_text(text, entry):
    """Return (kind, subkind) for a user text message."""
    t = text.lstrip()
    origin = (entry.get("origin") or {}).get("kind")
    if entry.get("isCompactSummary") or t.startswith("This session is being continued from a previous conversation"):
        return "notice", "compaction"
    if t.startswith("[Request interrupted by user"):
        return "notice", "interrupt"
    if origin == "task-notification" or t.startswith("<task-notification"):
        return "notice", "task_notification"
    if t.startswith("<local-command-stdout") or t.startswith("<local-command-caveat") or t.startswith("<local-command-stderr"):
        return "skip", None
    if entry.get("isMeta"):
        return "skip", None
    if t.startswith("Base directory for this skill") or t.startswith("<system-reminder>"):
        return "skip", None
    if t.startswith("<scheduled-task"):
        return "prompt", "scheduled"
    if t.startswith("<command-name>") or t.startswith("<command-message>"):
        return "prompt", "command"
    return "prompt", "human"


def _hash_input(name, inp):
    return hashlib.sha1((name + json.dumps(inp, sort_keys=True, default=str)).encode()).hexdigest()[:16]


def parse_file(path, root=DEFAULT_ROOT):
    rel = os.path.relpath(path, root)
    parts = rel.replace("\\", "/").split("/")
    project_dir = parts[0]
    is_sub = "subagents" in parts
    stem = os.path.splitext(parts[-1])[0]
    parent_id = parts[1] if is_sub else None
    run = {
        "id": stem if not is_sub else f"{parent_id}:{stem}",
        "source": "claude-code",
        "file": path,
        "project": project_dir,
        "cwd": None,
        "title": None,
        "agent_name": None,
        "parent_id": parent_id,
        "is_subagent": is_sub,
        "workflow": parts[3] if is_sub and len(parts) > 4 and parts[2] == "workflows" else None,
        "version": None,
        "git_branch": None,
        "entrypoint": None,
        "steps": [],
    }
    steps = run["steps"]
    llm_by_msg = {}
    tool_by_id = {}
    last_ts = None

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = e.get("type")
            if et == "custom-title":
                run["title"] = e.get("customTitle")
                continue
            if et == "agent-name":
                run["agent_name"] = e.get("agentName")
                continue
            ts = parse_ts(e.get("timestamp"))
            if e.get("cwd") and not run["cwd"]:
                run["cwd"] = e["cwd"]
                run["version"] = e.get("version")
                run["git_branch"] = e.get("gitBranch")
                run["entrypoint"] = e.get("entrypoint")

            if et == "system":
                st = e.get("subtype")
                if st in ("api_error", "compact_boundary"):
                    steps.append({"kind": "notice", "name": st if st != "compact_boundary" else "compaction",
                                  "ts": ts, "text": str(e.get("content") or e.get("error") or "")[:300]})
                if ts:
                    last_ts = ts
                continue

            msg = e.get("message")
            if et == "assistant" and isinstance(msg, dict):
                mid = msg.get("id") or e.get("uuid")
                step = llm_by_msg.get(mid)
                usage = msg.get("usage") or {}
                if step is None:
                    step = {
                        "kind": "llm", "name": msg.get("model"), "model": msg.get("model"), "ts": ts,
                        "start_ts": last_ts if last_ts and ts and ts - last_ts < 3600 else ts,
                        "end_ts": ts, "text_chars": 0, "thinking_blocks": 0, "tool_calls": 0,
                        "sidechain": bool(e.get("isSidechain")), "text": "",
                    }
                    llm_by_msg[mid] = step
                    steps.append(step)
                step["end_ts"] = ts or step["end_ts"]
                step["stop_reason"] = msg.get("stop_reason") or step.get("stop_reason")
                cc = usage.get("cache_creation") or {}
                cw_total = usage.get("cache_creation_input_tokens", 0) or 0
                cw_1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
                cw_5m = cc.get("ephemeral_5m_input_tokens", cw_total - cw_1h) or 0
                step.update({
                    "input_tokens": usage.get("input_tokens", 0) or 0,
                    "output_tokens": usage.get("output_tokens", 0) or 0,
                    "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                    "cache_write": cw_total,
                    "cache_write_1h": cw_1h,
                    "cache_write_5m": cw_5m,
                    "thinking_tokens": (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0,
                    "effort": e.get("effort") or step.get("effort"),
                })
                for b in msg.get("content") or []:
                    bt = b.get("type")
                    if bt == "text":
                        step["text_chars"] += len(b.get("text", ""))
                        if len(step["text"]) < 600:
                            step["text"] += b.get("text", "")[:600]
                    elif bt == "thinking":
                        step["thinking_blocks"] += 1
                    elif bt == "tool_use":
                        step["tool_calls"] += 1
                        name = b.get("name")
                        inp = b.get("input") or {}
                        phase, target = classify(name, inp)
                        tstep = {
                            "kind": "tool", "name": name, "ts": ts, "start_ts": ts, "end_ts": None,
                            "phase": phase, "target": target, "input_hash": _hash_input(name, inp),
                            "input_chars": len(json.dumps(inp)), "llm_msg": mid, "is_error": False,
                            "output_chars": 0, "tool_use_id": b.get("id"),
                            "input_preview": json.dumps(inp)[:400],
                        }
                        tool_by_id[b.get("id")] = tstep
                        steps.append(tstep)
                if ts:
                    last_ts = ts
                continue

            if et == "user" and isinstance(msg, dict):
                content = msg.get("content")
                blocks = content if isinstance(content, list) else []
                tool_results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
                if tool_results:
                    for b in tool_results:
                        tstep = tool_by_id.get(b.get("tool_use_id"))
                        if not tstep:
                            continue
                        tstep["end_ts"] = ts
                        tstep["is_error"] = bool(b.get("is_error"))
                        tstep["output_chars"] = _content_len(b.get("content"))
                        if tstep["is_error"]:
                            tstep["error"] = _text_of(b.get("content"))[:300] or str(b.get("content"))[:300]
                        tur = e.get("toolUseResult")
                        if isinstance(tur, dict):
                            if tur.get("interrupted"):
                                tstep["interrupted"] = True
                            if tur.get("agentId"):
                                tstep["subagent_id"] = tur.get("agentId")
                                tstep["subagent_tokens"] = tur.get("totalTokens")
                        elif isinstance(tur, str) and "rejected" in tur.lower():
                            tstep["rejected"] = True
                    if ts:
                        last_ts = ts
                    continue
                text = _text_of(content)
                if not text.strip():
                    continue
                kind, sub = _classify_user_text(text, e)
                if kind == "skip":
                    continue
                steps.append({"kind": kind, "name": sub, "ts": ts, "text": text[:2000], "sidechain": bool(e.get("isSidechain"))})
                if ts:
                    last_ts = ts

    # finalize costs and durations
    for s in steps:
        if s["kind"] == "llm":
            s["cost"] = pricing.cost(s["model"], s.get("input_tokens", 0), s.get("output_tokens", 0),
                                     s.get("cache_read", 0), s.get("cache_write_5m", 0), s.get("cache_write_1h", 0))
            s["context_tokens"] = s.get("input_tokens", 0) + s.get("cache_read", 0) + s.get("cache_write", 0)
            if s.get("start_ts") and s.get("end_ts"):
                s["duration_ms"] = max(0, int((s["end_ts"] - s["start_ts"]) * 1000))
        elif s["kind"] == "tool":
            if s.get("end_ts") and s.get("start_ts"):
                s["duration_ms"] = max(0, int((s["end_ts"] - s["start_ts"]) * 1000))
    for i, s in enumerate(steps):
        s["seq"] = i
    tss = [s["ts"] for s in steps if s.get("ts")]
    run["started"] = min(tss) if tss else None
    run["ended"] = max(tss) if tss else None
    if is_sub and steps and steps[0]["kind"] == "prompt":
        steps[0]["name"] = "delegated"
    return run


def discover(root=DEFAULT_ROOT):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(".jsonl"):
                out.append(os.path.join(dirpath, fn))
    return out
