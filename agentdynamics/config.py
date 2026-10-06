"""Configuration: <data_dir>/agentdynamics.toml (optional) + environment overrides.

Example agentdynamics.toml:

    [server]
    host = "0.0.0.0"
    port = 8787

    [auth]
    enabled = true
    # role: ingest (write telemetry), read (view console/API), admin (rules, SLOs, config)
    keys = [
      { name = "otel-collector", key = "ad_ingest_xxx", role = "ingest" },
      { name = "sre-team",       key = "ad_read_xxx",   role = "read" },
      { name = "platform-admin", key = "ad_admin_xxx",  role = "admin" },
    ]

    [privacy]
    store_content = true            # false keeps only sizes/metadata, never prompt or tool text
    redact = ["email", "api_key", "credit_card", "bearer", "aws_key"]
    extra_patterns = []             # additional regexes to mask

    [store]                         # optional: Postgres instead of the SQLite file in the data directory
    url = "postgresql://agentdynamics@db.internal/agentdynamics"   # needs pip install "agentdynamics[postgres]"
    schema = "agentdynamics"

    [enforcement]                   # server-side revocation (off unless set): an agent the policy keeps
    probing = { denials = 10, runs = 3, window_minutes = 30, revoke_minutes = 60 }   # refusing across runs

    [incidents]                     # security signals about one agent, grouped into one thing to judge
    rules = ["tripwire", "repeated_denials", "revoked", "policy_denials"]
    gap_hours = 24                  # quiet this long, and the agent's next signal opens a new incident

    [enforcement.trust]             # restrict a low-trust agent: it loses the tools it misused (off unless set)
    restrict_below = 50
    minutes = 60

    [trust]                         # each agent's trust score (trust.py): points for evidence, halved weekly
    half_life_days = 7
    tripwire = 40                   # a task in which the agent touched a tripwire
    probing = 15                    # a task in which it had 3+ calls refused in a row
    denial_rate = 20                # at a 100% refusal rate, pro rata below
    confirmed = 1.5                 # evidence in an incident confirmed as real counts this much more

    [enforcement.tripwires]         # decoys no legitimate agent touches: each touch raises a critical event
    tools = ["secrets.vault_export"]
    canaries = { planted_aws_key = "AKIA-CANARY-7F3E9Q" }  # name = planted value (8+ characters)
    runs = 2                        # touched in this many runs within window_minutes: revoke the agent
    window_minutes = 60
    revoke_minutes = 60             # 0: events and alerts only

    [retention]
    days = 90                       # spans/runs older than this are purged; daily totals are kept

    [alerts]
    console_url = "https://agentdynamics.internal"   # alerts link back to the task or SLO
    slo_min_tasks = 10              # fewest tasks in a window before an SLO burn rate can alert

    [[alerts.webhooks]]
    url = "https://hooks.slack.com/services/..."
    min_severity = "warning"
    format = "slack"                # slack | pagerduty | json (see agentdynamics/alerts.py)
    kinds = ["events", "slos"]      # health-rule events (default), SLO burn-rate alerts, and "incidents":
                                    # security signals about one agent, alerted once (incidents.py)
    projects = ["checkout"]         # optional routing: only these projects / health rules
    [[alerts.webhooks]]
    name = "pager"
    format = "pagerduty"
    routing_key_env = "PD_ROUTING_KEY"
    min_severity = "critical"
    kinds = ["slos"]

    [[sources]]
    type = "claude_code"            # built-in, on by default
    [[sources]]
    type = "langsmith_api"          # pull runs from LangSmith
    project = "my-agent-prod"
    api_key_env = "LANGSMITH_API_KEY"
    interval = 60
    [[sources]]
    type = "langfuse_api"
    host = "https://cloud.langfuse.com"
    public_key_env = "LANGFUSE_PUBLIC_KEY"
    secret_key_env = "LANGFUSE_SECRET_KEY"
    [[sources]]
    type = "inbox"                  # tail *.jsonl / *.json dropped by Fluent Bit, Vector, S3 sync...
    path = "/var/log/agent-traces"
"""
import copy
import os

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.10
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

DEFAULTS = {
    "server": {"host": "127.0.0.1", "port": 8787},
    "auth": {"enabled": False, "keys": []},
    "privacy": {"store_content": True, "redact": ["email", "api_key", "credit_card", "bearer", "aws_key"], "extra_patterns": []},
    "retention": {"days": 0},
    "alerts": {"webhooks": [], "console_url": "", "slo_min_tasks": 10},
    "store": {"url": "", "schema": "agentdynamics"},
    "enforcement": {},
    "analysis": {"interval": 15, "idle_cap_seconds": 300},
    "sources": [],
}

ROLES = {"ingest": 1, "read": 2, "admin": 3}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def keys_path(data_dir):
    return os.path.join(data_dir, "keys.json")


def load_keys(data_dir):
    p = keys_path(data_dir)
    if os.path.exists(p):
        import json
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_keys(data_dir, keys):
    import json
    p = keys_path(data_dir)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def load(data_dir):
    cfg = copy.deepcopy(DEFAULTS)
    path = os.environ.get("AGENTDYNAMICS_CONFIG") or os.path.join(data_dir, "agentdynamics.toml")
    if os.path.exists(path):
        if tomllib is None:
            raise RuntimeError("reading agentdynamics.toml on Python 3.10 needs `pip install tomli`")
        with open(path, "rb") as f:
            cfg = _merge(cfg, tomllib.load(f))
        cfg["_path"] = path
    # Env overrides for container deployments: AGENTDYNAMICS_API_KEYS="name:key:role,name2:key2:role2"
    env_keys = os.environ.get("AGENTDYNAMICS_API_KEYS")
    if env_keys:
        cfg["auth"]["enabled"] = True
        for item in env_keys.split(","):
            parts = item.strip().split(":")
            if len(parts) == 3:
                cfg["auth"]["keys"].append({"name": parts[0], "key": parts[1], "role": parts[2]})
    file_keys = load_keys(data_dir)  # created with `agentdynamics keys create`
    if file_keys:
        cfg["auth"]["enabled"] = True
        cfg["auth"]["keys"] = list(cfg["auth"]["keys"]) + file_keys
    # the store: a Postgres URL (the SQLite file in the data directory otherwise), and its schema
    if os.environ.get("AGENTDYNAMICS_DB_URL"):
        cfg["store"]["url"] = os.environ["AGENTDYNAMICS_DB_URL"]
    if os.environ.get("AGENTDYNAMICS_DB_SCHEMA"):
        cfg["store"]["schema"] = os.environ["AGENTDYNAMICS_DB_SCHEMA"]
    if os.environ.get("AGENTDYNAMICS_STORE_CONTENT") in ("0", "false"):
        cfg["privacy"]["store_content"] = False
    if os.environ.get("AGENTDYNAMICS_RETENTION_DAYS"):
        cfg["retention"]["days"] = int(os.environ["AGENTDYNAMICS_RETENTION_DAYS"])
    return cfg


def public_view(cfg):
    """Config safe to show in the UI (no secrets)."""
    v = copy.deepcopy(cfg)
    v["auth"]["keys"] = [{"name": k.get("name"), "role": k.get("role"), "key": (k.get("key") or "")[:6] + "…"} for k in v["auth"]["keys"]]
    if v.get("store", {}).get("url"):
        from .pg import redact_url
        v["store"]["url"] = redact_url(v["store"]["url"])
    for w in v["alerts"]["webhooks"]:
        w["url"] = (w.get("url") or "")[:28] + "…"     # a Slack webhook URL is itself the secret
        if w.get("routing_key"):
            w["routing_key"] = "…"
    tw = (v.get("enforcement") or {}).get("tripwires") or {}
    if isinstance(tw.get("canaries"), dict):       # a canary's value tells whoever sees it what to avoid
        tw["canaries"] = {name: "…" for name in tw["canaries"]}
    elif tw.get("canaries"):
        tw["canaries"] = ["…" for _ in tw["canaries"]]
    return v
