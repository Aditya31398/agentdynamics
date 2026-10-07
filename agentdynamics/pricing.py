"""Token pricing (USD per 1M tokens). Anthropic first-party list prices
(https://platform.claude.com/docs/en/about-claude/pricing, read 2026-10-07).

Cache writes are billed at 1.25x input (5-minute TTL) or 2x input (1-hour TTL); cache reads at 0.1x input,
except where CACHE_READ_OVERRIDE says otherwise. Modifiers stack on all of them: the Batch API halves the
price, fast mode doubles it (Claude Opus 5.5, 5 and 4.8), and US-only inference (`inference_geo: "us"`)
adds 10%. Override any entry via `pricing.json` in the data dir.

An entry prices a model id only when it names that model: the id itself, or the id followed by a date or a
provider version tag (`claude-opus-4-1-20250805`, `us.anthropic.claude-sonnet-4-5-20250929-v1:0`,
`claude-opus-4-5@20251101`). It never prices a later version by prefix: `claude-opus-5` doesn't price
`claude-opus-5-5`, which was billed at Claude Opus 5's rate until 0.9.0. A model with no entry is unpriced.
"""
import json
import os
import re

# model id -> (input, output)
BASE = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-mythos-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4-0": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-0": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
}
# cache reads where the rate is not 0.1x input
CACHE_READ_OVERRIDE = {"claude-fable-5-1": 0.25, "claude-mythos-5-1": 0.25, "claude-opus-5-5": 0.20}
# what may follow an entry's id and still be the same model: a snapshot date, then provider tags
_SUFFIX = re.compile(r"(?:[-@]\d{8})?(?:-v\d+(?::\d+)?|-latest)?(?:\[[^\]]*\])?$")
_DOT_VERSION = re.compile(r"(?<=\d)\.(?=\d)")
MODIFIERS = {"batch": 0.5, "fast": 2.0, "us": 1.1}

_overrides = {}


def load_overrides(data_dir):
    path = os.path.join(data_dir, "pricing.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            _overrides.update(json.load(f))


def normalize(model):
    """The first-party id inside a provider's: `us.anthropic.claude-...-v1:0`, `anthropic/claude-opus-4.5`."""
    m = (model or "").strip().lower()
    i = m.find("claude-")
    if i > 0:
        m = m[i:]
    return _DOT_VERSION.sub("-", m)


def entry(model):
    """The BASE key that prices this model id, or None."""
    m = normalize(model)
    if m in BASE:
        return m
    for key in sorted(BASE, key=len, reverse=True):
        if m.startswith(key) and _SUFFIX.fullmatch(m[len(key):]):
            return key
    return None


def rates(model):
    """Per-1M-token rates for a model id, or None when it has no price (see the module docstring)."""
    m = (model or "").lower()
    if m in _overrides:
        return _overrides[m]
    best = entry(m)
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


def cost(model, input_tokens=0, output_tokens=0, cache_read=0, cache_write_5m=0, cache_write_1h=0,
         service_tier=None, speed=None, inference_geo=None):
    r = rates(model)
    if not r:
        return 0.0
    usd = (
        input_tokens * r["input"]
        + output_tokens * r["output"]
        + cache_read * r["cache_read"]
        + cache_write_5m * r["cache_write_5m"]
        + cache_write_1h * r["cache_write_1h"]
    ) / 1_000_000
    for mod in (service_tier, speed, inference_geo):
        usd *= MODIFIERS.get(mod, 1.0)
    return usd
