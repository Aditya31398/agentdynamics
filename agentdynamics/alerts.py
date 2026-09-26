"""Alert routing: health-rule events and SLO burn rates, to Slack, PagerDuty and JSON webhooks.

Destinations are `[[alerts.webhooks]]` in agentdynamics.toml:

    [alerts]
    console_url = "https://agentdynamics.internal"   # optional: alerts link to the task / SLO page

    [[alerts.webhooks]]
    name = "sre-pager"                   # optional; names it in `agentdynamics alerts status/test`
    format = "pagerduty"                 # json (default) | slack | pagerduty
    routing_key_env = "PD_ROUTING_KEY"   # PagerDuty Events API v2 integration key (or routing_key = "...")
    min_severity = "critical"            # info | warning (default) | critical
    kinds = ["events", "slos"]           # default ["events"]; "slos" adds SLO burn-rate alerts
    projects = ["checkout"]              # optional: only these projects
    rules = ["run_failed"]               # optional: only these health rules

What is sent:
- A health-rule event is a point in time. Slack and JSON get each one (JSON in the original
  `{"source": "agentdynamics", "events": [...]}` shape, a contract). PagerDuty gets one alert per rule,
  project and task type (the dedup key), so a burst of runaway-cost tasks is one incident, not fifty.
- An SLO alert has a start and an end: a trigger when a burn-rate policy starts firing and a resolve when
  it stops (slo.alert_conditions). What is firing is kept in the durable `alert_state` table, so a restart
  neither repeats a trigger nor forgets to resolve.

Delivery is at least once, in order per destination, through the durable `alert_outbox` table: a failed
send is retried with backoff (429, 5xx, network errors) for up to a day; any other 4xx is a configuration
error and is dropped and reported. No secret is stored: bodies are queued without the PagerDuty routing key,
and destinations are identified by name or a hash, never by URL.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from urllib.parse import quote

FORMATS = ("json", "slack", "pagerduty")
KINDS = ("events", "slos")
SEV = {"info": 1, "warning": 2, "critical": 3}
# https://raw.githubusercontent.com/PagerDuty/api-schema/main/reference/events-v2/openapiv3.json
PAGERDUTY_URL = "https://events.pagerduty.com/v2/enqueue"
PAGERDUTY_SEVERITY = {"critical": "critical", "warning": "warning", "info": "info"}
SLACK_LINES = 10          # a Slack message lists this many alerts, then "and N more"
GIVE_UP_S = 86400         # a message still failing after a day is dropped
MAX_BACKOFF_S = 3600


def backoff(attempts):
    return min(MAX_BACKOFF_S, 15 * 2 ** attempts)


# ---------------------------------------------------------------- destinations

def destinations(alerts_cfg, env=None):
    """([destination], [problem]) from the [alerts] config. A misconfigured destination is reported, not
    raised: one bad entry must not stop the others, or the server."""
    env = os.environ if env is None else env
    out, problems, seen = [], [], set()
    for i, h in enumerate((alerts_cfg or {}).get("webhooks") or []):
        h = dict(h)
        label = h.get("name") or f"webhook #{i + 1}"
        fmt = h.get("format") or "json"
        if fmt not in FORMATS:
            problems.append(f"{label}: unknown format {fmt!r}; use one of {', '.join(FORMATS)}")
            continue
        kinds = h.get("kinds") or ["events"]
        bad = [k for k in kinds if k not in KINDS]
        if bad:
            problems.append(f"{label}: unknown kinds {bad}; use {list(KINDS)}")
            continue
        if h.get("min_severity", "warning") not in SEV:
            problems.append(f"{label}: min_severity must be one of {', '.join(SEV)}")
            continue
        secret = ""
        if fmt == "pagerduty":
            h.setdefault("url", PAGERDUTY_URL)
            secret = h.get("routing_key") or env.get(h.get("routing_key_env") or "", "")
            if not secret:
                problems.append(f"{label}: a pagerduty destination needs routing_key or routing_key_env"
                                + (f" ({h['routing_key_env']} is not set)" if h.get("routing_key_env") else ""))
                continue
        if not h.get("url"):
            problems.append(f"{label}: no url")
            continue
        did = h.get("name") or f"{fmt}-{hashlib.sha256((h['url'] + secret).encode()).hexdigest()[:10]}"
        if did in seen:
            problems.append(f"{label}: duplicate destination name {did!r}")
            continue
        seen.add(did)
        out.append({"id": did, "format": fmt, "url": h["url"], "secret": secret, "kinds": list(kinds),
                    "min_severity": h.get("min_severity", "warning"),
                    "projects": h.get("projects"), "rules": h.get("rules")})
    return out, problems


def routes(dest, alert):
    """Whether this destination takes this alert."""
    if alert["kind"] + "s" not in dest["kinds"]:
        return False
    if SEV.get(alert["severity"], 1) < SEV[dest["min_severity"]]:
        return False
    if dest["projects"] is not None and alert.get("project") not in dest["projects"]:
        return False            # an install-wide SLO has no project, so it goes only to unfiltered destinations
    if dest["rules"] is not None and alert["kind"] == "event" and alert.get("rule_id") not in dest["rules"]:
        return False
    return True


# ---------------------------------------------------------------- alerts

def from_event(e):
    return {"kind": "event", "action": "trigger", "key": f"event/{e['rule_id']}/{e['project']}/{e['task_type']}",
            "severity": e["severity"], "summary": f"{e['rule']}: {e['message']}", "project": e["project"],
            "rule_id": e["rule_id"], "task_type": e["task_type"], "ts": e.get("ts"), "event": e}


def slo_summary(c, action="trigger"):
    scope = " ".join(f"{k}={v}" for k, v in (c.get("scope") or {}).items() if v)
    where = f" [{scope}]" if scope else ""
    if action == "resolve":
        what = "is back within its objective" if c["alert"] == "breach" else \
            f"is no longer burning its error budget fast enough to {c['alert']}"
        return f"Resolved: SLO '{c['name']}'{where} {what}"
    if c["policy"] == "breach":
        return f"SLO '{c['name']}'{where} breached: {c['value']:.4g} against a target of {c['op']} {c['target']}"
    return (f"SLO '{c['name']}'{where} burning its error budget at {c['burn_rate']}x "
            f"(threshold {c['threshold']}x: {c['budget']:.0%} of the budget in {c['long_h']}h)")


def from_slo(key, c, action, now):
    return {"kind": "slo", "action": action, "key": key, "severity": c["severity"],
            "summary": slo_summary(c, action),
            "project": (c.get("scope") or {}).get("project") or None, "slo": c["slo"], "ts": now, "condition": c}


# ---------------------------------------------------------------- formats

def _dedup_key(key):
    key = "agentdynamics/" + key
    return key if len(key) <= 255 else key[:200] + "#" + hashlib.sha256(key.encode()).hexdigest()[:32]


def link(console_url, alert):
    if not console_url:
        return None
    base = console_url.rstrip("/")
    if alert["kind"] == "event":
        return f"{base}/#/task/{quote(alert['event']['task_id'], safe='')}"
    return f"{base}/#/slos"


def _slack_escape(s):
    # Slack mrkdwn: these three are control characters in message text
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def render(dest, alerts, console_url=""):
    """The request bodies that deliver `alerts` to `dest`, in order. Secrets are added at send time."""
    fmt = dest["format"]
    if fmt == "json":
        bodies = []
        events = [a["event"] for a in alerts if a["kind"] == "event"]
        if events:
            bodies.append({"source": "agentdynamics", "events": events})
        slos = [{"action": a["action"], "key": a["key"], "severity": a["severity"], "summary": a["summary"],
                 "link": link(console_url, a), **a["condition"]} for a in alerts if a["kind"] == "slo"]
        if slos:
            bodies.append({"source": "agentdynamics", "slo_alerts": slos})
        return bodies
    # one line (Slack) or one alert (PagerDuty) per dedup key, so a burst of the same violation reads as one
    groups = OrderedDict()
    for a in alerts:
        groups.setdefault(a["key"], []).append(a)
    if fmt == "slack":
        lines = []
        for g in list(groups.values())[:SLACK_LINES]:
            a = g[-1]
            head = "RESOLVED" if a["action"] == "resolve" else a["severity"].upper()
            where = " / ".join(_slack_escape(x) for x in (a.get("project"), a.get("task_type")) if x)
            url = link(console_url, a)
            ref = f" · <{url}|{'latest' if len(g) > 1 else 'open'}>" if url else ""
            if a["kind"] == "event":
                e = a["event"]
                times = f" ×{len(g)}" if len(g) > 1 else ""
                lines.append(f"*{head}* · {_slack_escape(e['rule'])}{times} · {where}{ref}\n{_slack_escape(e['message'])}")
            else:
                lines.append(f"*{head}* · {_slack_escape(a['summary'])}{ref}")
        if len(groups) > SLACK_LINES:
            lines.append(f"…and {len(groups) - SLACK_LINES} more")
        return [{"text": "AgentDynamics alerts\n" + "\n\n".join(lines)}] if lines else []
    bodies = []
    for key, g in groups.items():
        a = g[-1]
        if a["action"] == "resolve":
            bodies.append({"event_action": "resolve", "dedup_key": _dedup_key(key)})
            continue
        if a["kind"] == "event":
            e = a["event"]
            details = {"rule": e["rule"], "rule_id": e["rule_id"], "message": e["message"], "value": e.get("value"),
                       "task_id": e["task_id"], "run_id": e.get("run_id"), "project": e["project"],
                       "task_type": e["task_type"], "occurrences": len(g)}
            group, klass = e["task_type"], e["rule_id"]
        else:
            details, group, klass = dict(a["condition"]), a["slo"], "slo_" + a["condition"]["alert"]
        body = {"event_action": "trigger", "dedup_key": _dedup_key(key),
                "payload": {"summary": a["summary"][:1024], "source": "agentdynamics",
                            "severity": PAGERDUTY_SEVERITY.get(a["severity"], "warning"),
                            "timestamp": _iso(a.get("ts")), "component": a.get("project") or "all projects",
                            "group": group, "class": klass, "custom_details": details},
                "client": "AgentDynamics"}
        url = link(console_url, a)
        if url:
            body["client_url"] = console_url
            body["links"] = [{"href": url, "text": "Open in AgentDynamics"}]
        bodies.append(body)
    return bodies


# ---------------------------------------------------------------- sending

def send(dest, body, timeout=10):
    """POST one body. Returns (ok, retryable, detail)."""
    if dest["format"] == "pagerduty":
        body = dict(body, routing_key=dest["secret"])
    req = urllib.request.Request(dest["url"], data=json.dumps(body, default=str).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "agentdynamics"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, False, f"HTTP {r.status}"
    except urllib.error.HTTPError as ex:
        text = ex.read(300).decode("utf-8", "replace").strip()
        return False, ex.code == 429 or ex.code >= 500, f"HTTP {ex.code}: {text}"[:300]
    except (urllib.error.URLError, OSError) as ex:
        return False, True, f"{type(ex).__name__}: {getattr(ex, 'reason', ex)}"[:300]


def test_alert(now=None):
    """A harmless alert for `agentdynamics alerts test`: a trigger, and for PagerDuty the resolve after it."""
    now = now or time.time()
    c = {"slo": "agentdynamics-test", "name": "AgentDynamics test alert", "alert": "breach", "policy": "breach",
         "severity": "info",
         "metric": "success_rate", "op": ">=", "target": 1, "value": 0, "window_days": 1, "scope": {}, "tasks": 0}
    a = from_slo("test/agentdynamics", c, "trigger", now)
    a["summary"] = "AgentDynamics test alert: this destination is configured correctly"
    return a
