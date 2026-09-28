"""Tripwires: decoys no legitimate agent ever touches, so touching one is certain evidence.

    [enforcement.tripwires]
    tools = ["secrets.dump", "admin.export_all"]            # decoy tools: registered, never needed
    canaries = { fake_aws_key = "AKIA-CANARY-7F3E9Q", decoy_path = "/srv/canary/" }   # planted values
    runs = 2                  # an agent that touched them in this many runs ...
    window_minutes = 60       # ... within this long is revoked wherever it runs
    revoke_minutes = 60       # 0: mark, raise the `tripwire` event and alert, but issue no directive

A decoy tool is one an agent has no reason to call. A canary is a value planted where an agent has no reason
to look -- a fake credential in a config file, a path in a document -- so it turning up in what an agent
read, sent or wrote means it went there. Unlike the other detectors there is no threshold and no baseline:
one touch is enough, and there are no false positives to tune away.

Three places use them, from narrow to wide:
  * the Aegis integration checks every governed call *before* it runs (`instrument(..., tripwires=...)`), and
    revokes that run's whole grant tree first, so the kernel refuses the call and the canary never leaves;
  * the server marks each step that touches one (`mark`); the `tripwire` health rule raises a critical event
    for every task that did, and alerts;
  * the server revokes the agent everywhere in its project (`Engine._detect_tripwires`) only when it touched
    them in `runs` separate runs. One touch can come from one planted document: stopping the agent for
    everyone on that would let whoever planted it switch the agent off. Touches across runs mean the cause
    persists -- a poisoned document every conversation retrieves, a compromised model -- and then it should be.

Events, alerts, directives and the console name a tripwire by its label ("decoy tool secrets.dump", "canary
fake_aws_key"), never by a canary's value, which would tell whoever reads them what to avoid.

A canary shorter than 8 characters is ignored: it would match ordinary text. The server matches canaries in
what a source sends, before redaction, so `store_content = false` doesn't blind it; a source that sends no
content leaves it only decoy tools to see.
"""
import json

MIN_CANARY = 8
# what a step says it read, sent or produced; a user's own prompt is not the agent going somewhere
_TEXT = ("text", "input_preview", "args_json", "target", "error")
_SKIP_KINDS = ("prompt", "notice")


class Tripwires:
    def __init__(self, tools=(), canaries=None):
        self.tools = {str(t) for t in tools or ()}
        items = list(canaries.items() if isinstance(canaries, dict) else
                     ((f"#{i + 1}", v) for i, v in enumerate(canaries or ())))
        ok = [(str(name), v) for name, v in items if isinstance(v, str) and len(v) >= MIN_CANARY]
        self.canaries = ok
        self.ignored = [str(name) for name, _ in items if str(name) not in {n for n, _ in ok}]

    def __bool__(self):
        return bool(self.tools or self.canaries)

    def match_tool(self, name):
        return f"decoy tool {name}" if name in self.tools else None

    def match_text(self, *texts):
        """The label of the first canary found in any of `texts`, or None."""
        for t in texts:
            if not t:
                continue
            if not isinstance(t, str):
                try:
                    t = json.dumps(t, default=str)
                except (TypeError, ValueError):
                    t = str(t)
            for name, value in self.canaries:
                if value in t:
                    return f"canary {name}"
        return None

    def match_call(self, tool, args=None):
        """For a call about to be made: a decoy tool, or a canary in its arguments."""
        return self.match_tool(tool) or (self.match_text(args) if self.canaries and args else None)

    def match_step(self, s):
        if s.get("kind") in _SKIP_KINDS:
            return None
        hit = self.match_tool(s.get("name")) if s.get("kind") == "tool" else None
        return hit or (self.match_text(*(s.get(f) for f in _TEXT)) if self.canaries else None)

    def mark(self, run):
        """Label every step of `run` that touches a tripwire (`step["tripwire"]`); returns how many. A label
        the step already carries -- set in-process by the Aegis integration -- is kept."""
        n = 0
        for s in run.get("steps") or ():
            label = s.get("tripwire") or self.match_step(s)
            if label:
                s["tripwire"] = label
                n += 1
        return n


def from_config(cfg):
    """The server's tripwires from `[enforcement.tripwires]`, or None when none are set."""
    t = (cfg.get("enforcement") or {}).get("tripwires") or {}
    tw = Tripwires(t.get("tools"), t.get("canaries"))
    return tw if tw else None
