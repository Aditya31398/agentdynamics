"""Outcomes from the systems that know them, by webhook: nothing to write in the agent's app.

A success rate is only as good as the outcomes behind it, and the systems that know whether the work held up are
usually somewhere else: the pull request was merged or abandoned, the ticket was reopened. Each
`[[outcomes.webhooks]]` entry is a URL such a system can call, `POST /hooks/<name>`, signed with a shared secret
instead of an API key (webhook senders sign; few can send a bearer token):

    [[outcomes.webhooks]]
    name = "github"                 # https://agentdynamics.internal/hooks/github
    provider = "github"             # a pull request's fate grades the work on its branch
    secret_env = "GITHUB_WEBHOOK_SECRET"
    key = "branch"                  # the trace metadata key that holds the branch (Claude Code sessions carry it)
    projects = ["checkout"]         # optional: grade only tasks in these projects

    [[outcomes.webhooks]]
    name = "support"
    provider = "generic"            # /api/outcomes' own body, signed: X-AgentDynamics-Signature: sha256=<hex>
    secret_env = "SUPPORT_HOOK_SECRET"

GitHub (`pull_request` events, X-Hub-Signature-256): a pull request merged grades every task on its branch
"completed"; one closed without merging, "failed"; merging GitHub's revert of a pull request (its branch is
`revert-<n>-<branch>`), "rework" for the original branch. Work on the default branch is never graded this way.
The grades are outcomes by key (store.set_keyed_grade), so they apply to tasks that arrive later too.
"""
import hashlib
import hmac
import json
import os
import re

PROVIDERS = ("github", "generic")
REVERT = re.compile(r"^revert-\d+-(.+)$")


class HookError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def find(cfg, name):
    for h in ((cfg.get("outcomes") or {}).get("webhooks") or []):
        if isinstance(h, dict) and h.get("name") == name:
            return h
    return None


def secret_of(conf):
    env = conf.get("secret_env")
    s = os.environ.get(env) if env else conf.get("secret")
    return s.encode() if s else None


def verify(conf, headers, body):
    """Raises HookError unless `body` was signed with the hook's secret."""
    if conf.get("provider") not in PROVIDERS:
        raise HookError(500, f"outcome webhook {conf.get('name')!r}: provider is one of {', '.join(PROVIDERS)}")
    secret = secret_of(conf)
    if not secret:
        raise HookError(401, f"outcome webhook {conf.get('name')!r} has no secret configured: it accepts nothing")
    header = "X-Hub-Signature-256" if conf["provider"] == "github" else "X-AgentDynamics-Signature"
    sent = (headers.get(header) or "").strip()
    want = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sent.encode(), want.encode()):
        raise HookError(401, f"bad or missing {header}")


def github(conf, event, payload):
    """Outcome items (as /api/outcomes takes them) for one GitHub delivery; [] for anything not about an outcome."""
    if event != "pull_request" or payload.get("action") != "closed":
        return []
    pr = payload.get("pull_request") or {}
    branch = (pr.get("head") or {}).get("ref")
    default = (payload.get("repository") or {}).get("default_branch")
    if not branch or branch == default:
        return []
    key, url = conf.get("key") or "branch", pr.get("html_url") or f"#{pr.get('number')}"
    reverted = REVERT.match(branch)
    if pr.get("merged") and reverted:
        if reverted.group(1) == default:
            return []
        return [{"key": {key: reverted.group(1)}, "outcome": "rework", "match": "all", "reason": f"reverted by {url}"}]
    if pr.get("merged"):
        return [{"key": {key: branch}, "outcome": "completed", "match": "all", "reason": f"merged: {url}"}]
    return [{"key": {key: branch}, "outcome": "failed", "match": "all", "reason": f"closed without merging: {url}"}]


def generic(body):
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise HookError(400, "the body is not JSON") from None
    items = data if isinstance(data, list) else (data.get("grades") if isinstance(data, dict) and "grades" in data else [data])
    for it in items:                     # the outcomes themselves are checked where /api/outcomes checks them
        if not isinstance(it, dict) or not ("key" in it or "task_id" in it):
            raise HookError(400, "each outcome names a task_id or a key")
    return items


def items_for(conf, headers, body):
    """Verify a delivery and turn it into outcome items. Raises HookError."""
    verify(conf, headers, body)
    if conf["provider"] == "generic":
        return generic(body)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HookError(400, "the body is not JSON") from None
    return github(conf, headers.get("X-GitHub-Event"), payload if isinstance(payload, dict) else {})
