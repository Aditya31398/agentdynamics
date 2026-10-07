"""What the provider billed, to set beside what AgentDynamics estimated (`[[sources]] type = "anthropic_costs"`).

Every cost here is estimated from list prices and the token counts a trace carries (pricing.py). Discounts, a
price the table doesn't know yet, traffic nobody traced, a cache write priced at the wrong TTL -- none of it shows
in an estimate. The bill shows all of it. This pulls Anthropic's Usage & Cost Admin API cost report, daily and by
description (model, token type, service tier), into `billing_daily`; `/api/billing` and the Models page set it
beside the estimate, per day and model.

    [[sources]]
    type = "anthropic_costs"
    admin_key_env = "ANTHROPIC_ADMIN_KEY"   # an Admin API key (sk-ant-admin...); a regular API key is refused
    days = 31                               # re-read this far back each pull: costs settle for a while
    interval = 3600

The key is read from the environment and never stored. Amounts come as decimal strings in cents. Costs are
per organization: the comparison covers what was traced, so a bill above the estimate can also mean traffic
nobody traced. Priority Tier costs aren't in the cost report (Anthropic's docs say so).
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

DAY = 86400
API_VERSION = "2023-06-01"


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AnthropicCostPuller:
    def __init__(self, cfg, state, clock=time.time):
        self.base = (cfg.get("base_url") or "https://api.anthropic.com").rstrip("/")
        self.key = os.environ.get(cfg.get("admin_key_env") or "ANTHROPIC_ADMIN_KEY", "")
        self.days = int(cfg.get("days", 31))
        self.state, self.clock = state, clock

    def _get(self, params):
        from .. import __version__
        req = urllib.request.Request(f"{self.base}/v1/organizations/cost_report?{urlencode(params)}", headers={
            "x-api-key": self.key, "anthropic-version": API_VERSION, "Accept": "application/json",
            "User-Agent": f"AgentDynamics/{__version__} (https://github.com/Aditya31398/agentdynamics)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def pull(self, max_pages=50):
        """(first day, last day, rows) for the days pulled: every day in the window, so a day whose costs moved or
        went is replaced whole. Rows: {day, description, model, cost_type, token_type, service_tier, usd}."""
        if not self.key:
            raise RuntimeError("no Admin API key: set the variable admin_key_env names (ANTHROPIC_ADMIN_KEY by "
                               "default) to an Admin API key")
        now = self.clock()
        start = (int(now // DAY) - self.days + 1) * DAY
        params = [("starting_at", _iso(start)), ("ending_at", _iso(now)), ("bucket_width", "1d"),
                  ("group_by[]", "description"), ("limit", "31")]
        agg, page = {}, None
        for _ in range(max_pages):
            res = self._get(params + ([("page", page)] if page else []))
            for bucket in res.get("data") or []:
                day = (bucket.get("starting_at") or "")[:10]
                for r in bucket.get("results") or []:
                    if (r.get("currency") or "USD") != "USD":
                        continue
                    k = (day, r.get("description") or "", r.get("model") or "", r.get("cost_type") or "",
                         r.get("token_type") or "", r.get("service_tier") or "")
                    agg[k] = agg.get(k, 0.0) + float(r.get("amount") or 0) / 100     # cents, as a decimal string
            page = res.get("next_page")
            if not res.get("has_more") or not page:
                break
        self.state["pulled"] = now
        rows = [{"day": k[0], "description": k[1], "model": k[2], "cost_type": k[3], "token_type": k[4],
                 "service_tier": k[5], "usd": v} for k, v in sorted(agg.items())]
        return _iso(start)[:10], _iso(now)[:10], rows
