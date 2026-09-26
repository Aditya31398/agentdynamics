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

    [retention]
    days = 90                       # spans/runs older than this are purged

    [alerts]
    console_url = "https://agentdynamics.internal"   # alerts link back to the task or SLO
    slo_min_tasks = 10              # fewest tasks in a window before an SLO burn rate can alert

    [[alerts.webhooks]]
    url = "https://hooks.slack.com/services/..."
    min_severity = "warning"
    format = "slack"                # slack | pagerduty | json (see agentdynamics/alerts.py)
    kinds = ["events", "slos"]      # health-rule events (default) and SLO burn-rate alerts
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
    if os.environ.get("AGENTDYNAMICS_STORE_CONTENT") in ("0", "false"):
        cfg["privacy"]["store_content"] = False
    if os.environ.get("AGENTDYNAMICS_RETENTION_DAYS"):
        cfg["retention"]["days"] = int(os.environ["AGENTDYNAMICS_RETENTION_DAYS"])
    return cfg


def public_view(cfg):
    """Config safe to show in the UI (no secrets)."""
    v = copy.deepcopy(cfg)
    v["auth"]["keys"] = [{"name": k.get("name"), "role": k.get("role"), "key": (k.get("key") or "")[:6] + "…"} for k in v["auth"]["keys"]]
    for w in v["alerts"]["webhooks"]:
        w["url"] = (w.get("url") or "")[:28] + "…"     # a Slack webhook URL is itself the secret
        if w.get("routing_key"):
            w["routing_key"] = "…"
    return v
