"""Token pricing (USD per 1M tokens). Anthropic first-party list prices.

Cache writes are billed at 1.25x input (5-minute TTL) or 2x input (1-hour TTL);
cache reads at 0.1x input. Override any entry via `pricing.json` in the data dir.
"""
import json
import os

# model-id prefix -> (input, output)
BASE = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-mythos-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
}
# Explicit cache-read overrides where the rate is not 0.1x input.
CACHE_READ_OVERRIDE = {"claude-fable-5-1": 0.25}

_overrides = {}


def load_overrides(data_dir):
    path = os.path.join(data_dir, "pricing.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            _overrides.update(json.load(f))


def rates(model):
    """Return dict of per-1M-token rates for a model id (longest prefix match)."""
    model = (model or "").lower()
    if model in _overrides:
        return _overrides[model]
    best = None
    for prefix in BASE:
        if model.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    if best is None:
        return None
    inp, out = BASE[best]
    return {
        "input": inp,
        "output": out,
        "cache_write_5m": inp * 1.25,
        "cache_write_1h": inp * 2.0,
        "cache_read": CACHE_READ_OVERRIDE.get(best, inp * 0.1),
    }


def cost(model, input_tokens=0, output_tokens=0, cache_read=0, cache_write_5m=0, cache_write_1h=0):
    r = rates(model)
    if not r:
        return 0.0
    return (
        input_tokens * r["input"]
        + output_tokens * r["output"]
        + cache_read * r["cache_read"]
        + cache_write_5m * r["cache_write_5m"]
        + cache_write_1h * r["cache_write_1h"]
    ) / 1_000_000
