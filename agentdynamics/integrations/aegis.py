"""AgentDynamics x Aegis: observe what agents do, and govern what they may do.

    import agentdynamics
    from agentdynamics.integrations import aegis as governance

    agentdynamics.init(project="support")
    kernel, root = build_kernel(load_policy("policy.yaml"), registry)       # Aegis
    governance.instrument(kernel, root, watchdog=governance.Watchdog(max_repeated_denials=3))

One call wires four flows:

1. Govern -> observe. Every Aegis decision (allowed tool call, denial with its rule id, spawn,
   revocation, budget reservation) becomes a step in the AgentDynamics task it happened in, and every
   Aegis audit record carries the task's run id, workflow and node (`details.ctx`) so the two logs join.
2. Budgets cover model spend. Anthropic / OpenAI / `agentdynamics.llm_call` requests reserve their
   estimated cost against the Aegis ledger *before* they are sent and settle the actual cost after.
   An exhausted budget or a revoked grant stops the request; it is never made.
3. Detect -> enforce. A watchdog evaluates each run as it happens (repeated denials, loops, runaway
   cost, too many model calls) and revokes the grant through Aegis, which disables the whole agent tree.
   Tripwires (decoy tools, planted canary values) revoke the run's grant tree before the call that touches
   one is made, so the kernel refuses it.
4. Observe -> govern. Runs carry the policy name, version and digest, so the console can compare
   policy versions, find unused grants, and generate a tightened policy (`agentdynamics policy export`).

Nothing here can loosen Aegis: it only adds correlation data, reserves budget, and revokes.
"""
from __future__ import annotations

import contextvars
import json
import math
import threading
import time
import urllib.parse
import urllib.request
import weakref
from collections import Counter

from .. import autotrace as at
from .. import pricing
from .. import tripwires as tripmod

_grant = contextvars.ContextVar("agentdynamics_aegis_grant", default=None)
_state = {"kernel": None, "root": None, "installed": False}


def _require_aegis():
    try:
        import aegis  # noqa: F401
        return aegis
    except ImportError as ex:  # pragma: no cover
        raise ImportError("pip install aegis-kernel to use the Aegis integration") from ex


# ---------------------------------------------------------------- policy identity

def policy_info(policy):
    """Name, version, digest and full content of an Aegis policy (for the run payload)."""
    aegis = _require_aegis()
    doc = aegis.dump_policy(policy) if hasattr(aegis, "dump_policy") else {"name": policy.name, "version": policy.version,
                                                                           "tools": {"allow": [{"name": n} for n in sorted(policy.tools)]}}
    digest = aegis.policy_digest(policy) if hasattr(aegis, "policy_digest") else "unknown"
    return {"name": policy.name, "version": policy.version, "digest": digest,
            "label": f"{policy.name}@v{policy.version}#{digest[:8]}", "doc": doc}


# ---------------------------------------------------------------- grant binding

class bind:
    """Make `grant` the one model calls and the watchdog act on (e.g. inside a spawned sub-agent).

        child = kernel.spawn(root, SpawnRequest("researcher", ...))
        with governance.bind(child):
            ...
    """

    def __init__(self, grant):
        self.grant = grant

    def __enter__(self):
        self._tok = _grant.set(self.grant)
        return self.grant

    def __exit__(self, *a):
        _grant.reset(self._tok)
        return False


def current_grant():
    return _grant.get() or _state["root"]


# ---------------------------------------------------------------- 1. decisions -> steps

def _step_from_call(grant, tool, args, t0, result=None, exc=None):
    step = {"kind": "tool", "name": tool, "ts": t0, "end_ts": time.time(), "agent": grant.agent_name,
            "grant_depth": grant.depth, "governed": True,
            "input": args if at._cfg["content"] else {}}
    v = getattr(exc, "verdict", None)
    if v is not None and not v.allowed:
        step.update(denied=True, rule=v.rule, guard=v.guard, error=f"[{v.rule}] {v.reason}"[:300])
    elif exc is not None:
        step.update(is_error=True, error=f"{type(exc).__name__}: {exc}"[:300], rule="kernel.admitted")
    else:
        step.update(rule="kernel.admitted", output_chars=len(at._clip(result, 10 ** 7)), text=at._clip(result, 300))
    return step


def _emit_step(step, fallback_name):
    run = at._current.get()
    if run is not None:
        run.add(step)
    else:  # a governed call outside any @trace still becomes a (tiny) task
        r = at._Run(fallback_name)
        r.add(step)
        at._emit(r.payload())


def _wrap_kernel(kernel):
    if getattr(kernel, "_agentdynamics_wrapped", False):
        return
    orig_invoke, orig_ainvoke, orig_spawn, orig_revoke = kernel.invoke, kernel.ainvoke, kernel.spawn, kernel.revoke

    def check(grant, tool=None, args=None):
        """Directives and tripwires, before a grant acts; returns the tripwire the call touches, if any."""
        rv = getattr(kernel, "_agentdynamics_revocations", None)    # this kernel's, not another's in-process
        if rv is not None:
            rv.check_use(grant)
        tw = getattr(kernel, "_agentdynamics_tripwires", None)
        return tw.before_call(grant, tool, args) if tw is not None and tool is not None else None

    def after(grant, result):
        tw = getattr(kernel, "_agentdynamics_tripwires", None)
        return tw.after_call(grant, result) if tw is not None else None

    def step(grant, tool, args, t0, hit, result=None, exc=None):
        s = _step_from_call(grant, tool, args, t0, result=result, exc=exc)
        if hit:
            s["tripwire"] = hit
        return s

    def invoke(grant, tool, /, **args):
        hit = check(grant, tool, args)
        t0 = time.time()
        with bind(grant):
            try:
                r = orig_invoke(grant, tool, **args)
            except Exception as ex:
                _emit_step(step(grant, tool, args, t0, hit, exc=ex), f"aegis.{tool}")
                raise
            hit = hit or after(grant, r)
            _emit_step(step(grant, tool, args, t0, hit, result=r), f"aegis.{tool}")
            return r

    async def ainvoke(grant, tool, /, **args):
        hit = check(grant, tool, args)
        t0 = time.time()
        tok = _grant.set(grant)
        try:
            r = await orig_ainvoke(grant, tool, **args)
        except Exception as ex:
            _emit_step(step(grant, tool, args, t0, hit, exc=ex), f"aegis.{tool}")
            raise
        finally:
            _grant.reset(tok)
        hit = hit or after(grant, r)
        _emit_step(step(grant, tool, args, t0, hit, result=r), f"aegis.{tool}")
        return r

    def spawn(parent, req):
        check(parent)
        t0 = time.time()
        try:
            child = orig_spawn(parent, req)
        except Exception as ex:
            _emit_step(_step_from_call(parent, "agent.spawn", {"name": req.name, "tools": sorted(req.tools)}, t0, exc=ex),
                       "aegis.spawn")
            raise
        _emit_step({"kind": "span", "span_kind": "agent", "name": child.agent_name, "node": None, "ts": t0,
                    "start_ts": t0, "end_ts": time.time(), "agent": parent.agent_name, "governed": True,
                    "rule": "spawn.granted", "text": f"spawned {child.agent_name} (depth {child.depth}) "
                                                     f"with {sorted(req.tools)}"}, "aegis.spawn")
        check(child)                     # a new grant for an agent a directive names is revoked before it acts
        return child

    def revoke(grant, reason="operator"):
        orig_revoke(grant, reason)
        _emit_step({"kind": "notice", "name": "revoked", "ts": time.time(), "agent": grant.agent_name,
                    "rule": "grant.revoked_subtree", "text": f"{grant.agent_name}: {reason}"}, "aegis.revoke")

    kernel.invoke, kernel.ainvoke, kernel.spawn, kernel.revoke = invoke, ainvoke, spawn, revoke
    kernel._agentdynamics_wrapped = True


def _context():
    """Correlation ids stamped into every Aegis audit record (details.ctx)."""
    run = at._current.get()
    if run is None:
        return None
    return {"run_id": run.id, "workflow": run.name, "node": at._node.get(), "project": at._cfg["project"],
            "source": "agentdynamics"}


# ---------------------------------------------------------------- 2. budgets cover model spend

class ModelSpendGate:
    """Reserve a model call's estimated cost in Aegis before it is sent; settle the real cost after."""

    def __init__(self, kernel, default_output_tokens=1024):
        self.kernel = kernel
        self.default_output_tokens = default_output_tokens

    def estimate(self, model, kwargs):
        body = kwargs.get("messages") if kwargs.get("messages") is not None else kwargs.get("input")
        try:
            chars = len(json.dumps(body, default=str)) + len(json.dumps(kwargs.get("system") or "", default=str))
        except (TypeError, ValueError):
            chars = 4000
        tin = math.ceil(chars / 4)
        tout = kwargs.get("max_tokens") or kwargs.get("max_output_tokens") or kwargs.get("max_completion_tokens") \
            or self.default_output_tokens
        return pricing.cost(model or "", tin, tout), tin + tout

    def before(self, provider, model, kwargs):
        grant = current_grant()
        if grant is None:
            return None
        usd, tokens = self.estimate(model, kwargs)
        return self.kernel.reserve_spend(grant, usd=usd, tokens=tokens, label=str(model))

    def after(self, reservation, usd, tokens, err):
        if reservation is not None:
            self.kernel.settle_spend(reservation, usd=usd if err is None else 0.0, tokens=tokens if err is None else 0)


# ---------------------------------------------------------------- 3. detect -> enforce

class Watchdog:
    """Live limits evaluated on every step of a run. When one trips, the grant is revoked through Aegis
    (its whole sub-tree stops) and the reason is recorded in both tools.

    max_repeated_denials  the same tool refused N times in a row (whatever the rule): the agent is probing
                          a boundary (a classic prompt-injection symptom) or stuck
    max_denials           total denials in one run
    max_node_visits       one node/stage executed N times: a loop
    max_run_cost_usd      model spend of the run (what AgentDynamics prices, not just the Aegis estimate)
    max_llm_calls         model calls in one run
    """

    def __init__(self, max_repeated_denials=3, max_denials=None, max_node_visits=None, max_run_cost_usd=None,
                 max_llm_calls=None, action="revoke"):
        self.limits = {"max_repeated_denials": max_repeated_denials, "max_denials": max_denials,
                       "max_node_visits": max_node_visits, "max_run_cost_usd": max_run_cost_usd,
                       "max_llm_calls": max_llm_calls}
        self.action = action
        self.kernel = None
        self.trips = []
        self._lock = threading.Lock()

    def _stats(self, run):
        # Keyed per watchdog: two of them on one run used to share this dict, so the first to
        # trip set "tripped" and silently switched off every other one.
        all_ = getattr(run, "_ad_watch", None)
        if all_ is None:
            all_ = run._ad_watch = {}
        st = all_.get(id(self))
        if st is None:
            st = all_[id(self)] = {"streak": 0, "run": 0, "last": None, "denials": 0, "nodes": Counter(),
                                   "cost": 0.0, "llm": 0, "tripped": False}
        return st

    def __call__(self, run, step):
        if step.get("kind") == "notice":
            return
        with self._lock:
            st = self._stats(run)
            if st["tripped"]:
                return
            if step.get("denied"):
                # Two counters, because an agent evades each one differently. `streak` is the same
                # tool refused in a row whatever the rule, so varying the payload (/etc/passwd, then
                # /workspace/../etc/passwd) still trips it. `run` is denials in a row whatever the
                # tool, so alternating between two forbidden tools trips it too -- which a prompt
                # injected agent does naturally, without trying to evade anything. Either ends at
                # the first call that actually succeeds.
                key = step.get("name")
                st["streak"] = st["streak"] + 1 if key == st["last"] else 1
                st["run"] += 1
                st["last"] = key
                st["denials"] += 1
            elif step.get("kind") == "tool":
                st["streak"], st["run"], st["last"] = 0, 0, None
            if step.get("kind") == "span" and step.get("node"):
                st["nodes"][step["node"]] += 1
            if step.get("kind") == "llm" and not step.get("denied"):
                st["llm"] += 1
                st["cost"] += step.get("cost") or 0.0
            lim = self.limits
            reason = None
            if lim["max_repeated_denials"] and max(st["streak"], st["run"]) >= lim["max_repeated_denials"]:
                reason = (f"repeated_denials: {st['last']} refused {st['streak']}x in a row "
                          f"(last rule {step.get('rule')})" if st["streak"] >= lim["max_repeated_denials"]
                          else f"repeated_denials: {st['run']} refusals in a row across tools "
                               f"(last {st['last']}, rule {step.get('rule')})")
            elif lim["max_denials"] and st["denials"] >= lim["max_denials"]:
                reason = f"denials: {st['denials']} denials in one run"
            elif lim["max_node_visits"] and st["nodes"] and max(st["nodes"].values()) >= lim["max_node_visits"]:
                node, n = st["nodes"].most_common(1)[0]
                reason = f"loop: node '{node}' ran {n}x"
            elif lim["max_run_cost_usd"] and st["cost"] >= lim["max_run_cost_usd"]:
                reason = f"cost: run spent ${st['cost']:.4f} (limit ${lim['max_run_cost_usd']})"
            elif lim["max_llm_calls"] and st["llm"] >= lim["max_llm_calls"]:
                reason = f"llm_calls: {st['llm']} model calls"
            if not reason:
                return
            st["tripped"] = True
        grant = current_grant()
        self.trips.append({"run_id": run.id, "reason": reason, "agent": getattr(grant, "agent_name", None), "ts": time.time()})
        if self.action == "revoke" and grant is not None and self.kernel is not None:
            self.kernel.revoke(grant, reason=f"agentdynamics.watchdog: {reason}")


class TripwireGuard:
    """Decoy tools and canary values (agentdynamics.tripwires), checked in process on every governed call.

    Before a call: a decoy tool, or a canary in the arguments, revokes the run's whole grant tree through
    Aegis first -- the kernel then refuses the call, so a canary is never sent anywhere. After a call: a canary
    in the result (the agent read a planted file) revokes the tree, and every later call and model request is
    refused. A model response quoting a canary does the same. The whole tree, not only the touching grant: a
    sub-agent that found a canary got there with what its parent gave it.

    Only this run is stopped. The server revokes the agent everywhere only when it touches tripwires in
    several runs (tripwires.py says why).
    """

    def __init__(self, tools=(), canaries=None):
        self.wires = tools if isinstance(tools, tripmod.Tripwires) else tripmod.Tripwires(tools, canaries)
        self.kernel = None
        self.trips = []                  # {"label", "agent", "grant_id", "ts"}

    def _trip(self, grant, label):
        root = grant
        while root.parent is not None:
            root = root.parent
        self.trips.append({"label": label, "agent": grant.agent_name, "grant_id": grant.grant_id, "ts": time.time()})
        if root.is_active() and self.kernel is not None:
            self.kernel.revoke(root, reason=f"agentdynamics tripwire: {label}")

    def before_call(self, grant, tool, args):
        try:
            label = self.wires.match_call(tool, args)
            if label:
                self._trip(grant, label)
            return label
        except Exception as ex:          # a fault here must not break the agent
            at._warn("tripwire-check", f"could not check a call against tripwires: {ex}")
            return None

    def after_call(self, grant, result):
        try:
            label = self.wires.match_text(result) if self.wires.canaries else None     # whatever content is sent
            if label:
                self._trip(grant, label)
            return label
        except Exception as ex:
            at._warn("tripwire-check", f"could not check a result against tripwires: {ex}")
            return None

    def __call__(self, run, step):
        """Step hook: a model response quoting a canary."""
        if step.get("kind") != "llm" or step.get("tripwire") or not self.wires.canaries:
            return
        label = self.wires.match_text(step.get("text"))
        grant = current_grant()
        if label and grant is not None:
            step["tripwire"] = label
            self._trip(grant, label)


# ---------------------------------------------------------------- entry point

# ---------------------------------------------------------------- 5. server-side revocation (#8)

class Revocations:
    """Directives from the AgentDynamics server to revoke an agent's grants, applied through Kernel.revoke.

    The server issues one when an operator asks (`agentdynamics revoke`, POST /api/revocations) or when it sees
    an agent probing its policy across runs ([enforcement] probing), which no single process can see. This polls
    for the directives that apply to this process's project and revokes the grants they name: at once in every
    grant tree it has seen (apps often make a root per conversation, not only the one given to instrument()),
    and at the point of use -- before any governed call, and on spawn -- for a grant not seen yet. The kernel
    enforces and audits each revocation.
    A directive can only take privileges away (invariant 6), and clearing it restores nothing: Aegis revocation
    is permanent. If the server can't be reached nothing is revoked and the agent carries on, as without this.

    It uses the URL, key and project given to agentdynamics.init(). The key needs the ingest role.
    """

    def __init__(self, interval=10.0):
        self.interval = interval
        self.active = {}                 # directive id -> directive, as of the last successful poll
        self.applied = []                # {"directive", "agent", "grant_id", "ts"} for each grant revoked here
        self._done = set()               # (directive id, grant id)
        self._roots = weakref.WeakValueDictionary()   # grant id -> root, for every tree seen (kept only while alive)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.kernel = self.root = None

    def start(self, kernel, root):
        self.kernel, self.root = kernel, root
        if self.interval:
            threading.Thread(target=self._loop, daemon=True, name="agentdynamics-revocations").start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while True:
            try:
                self.poll_once()
            except Exception as ex:      # never into the agent
                at._warn("revocations-err", f"revocation poll failed: {ex}")
            if self._stop.wait(self.interval):
                return

    def poll_once(self):
        """Fetch the active directives and apply them; returns the number of grants revoked."""
        url = at._cfg.get("url")
        if not url:
            at._warn("revocations-nourl", "revocations=True needs agentdynamics.init(url=...); nothing is polled")
            return 0
        q = urllib.parse.urlencode({"active": "1", "project": at._cfg.get("project") or "default"})
        headers = {"Authorization": f"Bearer {at._cfg['key']}"} if at._cfg.get("key") else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{url}/api/revocations?{q}", headers=headers),
                                        timeout=5) as r:
                data = json.loads(r.read())
        except Exception as ex:
            at._warn("revocations-down", f"could not fetch revocation directives from {url} ({ex}); "
                                         "nothing is revoked until it answers")
            return 0
        with self._lock:
            self.active = {d["id"]: d for d in data.get("revocations", [])}
        return self.apply()

    def _live(self):
        now = time.time()
        with self._lock:
            return [d for d in self.active.values() if (d.get("expires") or 0) > now]

    def apply(self):
        """Revoke every live grant an active directive names; returns how many."""
        if self.kernel is None or self.root is None:
            return 0
        return sum(self._revoke(d, g) for d in self._live() for g in self._targets(d))

    def note(self, grant):
        root = grant                     # up the parent chain (Grant.root is a constructor, not this)
        while root.parent is not None:
            root = root.parent
        self._roots[root.grant_id] = root

    def _all_roots(self):
        return [self.root] + [r for r in list(self._roots.values()) if r is not self.root]

    def _targets(self, d):
        if not d.get("agent"):
            return self._all_roots()     # every agent: every tree
        out, stack = [], self._all_roots()
        while stack:
            g = stack.pop()
            if g.agent_name == d["agent"]:
                out.append(g)            # its subtree goes with it
            else:
                stack.extend(g.children)
        return out

    def _revoke(self, d, grant):
        key = (d["id"], grant.grant_id)
        with self._lock:
            if key in self._done or not grant.is_active():
                return 0
            self._done.add(key)
        self.kernel.revoke(grant, reason=f"agentdynamics directive {d['id']}: {d.get('reason')}")
        self.applied.append({"directive": d["id"], "agent": grant.agent_name, "grant_id": grant.grant_id,
                             "ts": time.time()})
        return 1

    def check_use(self, grant):
        """Before a grant acts: if an active directive names its agent or an ancestor's (or every agent),
        revoke first, so the call is refused. Free while no directive is active."""
        try:
            self.note(grant)             # so a later directive finds this tree even while it sits idle
            if not self.active:
                return
            chain, g = [], grant
            while g is not None:
                chain.append(g)
                g = g.parent
            for d in self._live():
                hit = chain[-1] if not d.get("agent") else next((g for g in chain if g.agent_name == d["agent"]), None)
                if hit is not None:
                    self._revoke(d, hit)
                    return
        except Exception as ex:          # the check itself must not break the agent
            at._warn("revocations-check", f"could not check a grant against revocation directives: {ex}")


class Governance:
    def __init__(self, kernel, root, gate, watchdog, unregister, revocations=None, tripwires=None):
        self.kernel, self.root, self.gate, self.watchdog, self._unregister = kernel, root, gate, watchdog, unregister
        self.revocations = revocations
        self.tripwires = tripwires

    def uninstall(self):
        self._unregister()


def instrument(kernel, root=None, *, gate_models=True, watchdog=None, correlate=True, record_decisions=True,
               revocations=None, tripwires=None):
    """Connect an Aegis kernel to AgentDynamics. Call after `agentdynamics.init()` and `build_kernel()`.

    kernel      the Aegis Kernel
    root        the root Grant (model calls outside a bound grant are charged to it)
    gate_models reserve model spend against the Aegis budget before each call
    watchdog    a Watchdog, or None
    revocations True (or a Revocations) to apply the server's revocation directives: off by default, since it
                lets the server stop agents in this process. Needs `root`.
    tripwires   decoy tools and canary values -- {"tools": [...], "canaries": {name: value}}, an
                agentdynamics.tripwires.Tripwires, or a TripwireGuard. Touching one revokes the run's grant tree
                before the call is made.
    """
    _require_aegis()
    _state.update(kernel=kernel, root=root)
    undo = []
    if record_decisions or revocations or tripwires:
        _wrap_kernel(kernel)             # the spawn hook applies directives to new grants
    if correlate:
        try:
            from aegis.observe import register_context_provider
        except ImportError:
            at._warn("aegis-obs", "this aegis version has no aegis.observe; decisions are recorded but not correlated")
        else:
            undo.append(register_context_provider(_context))
    gate = None
    if gate_models:
        if hasattr(kernel, "reserve_spend"):
            gate = ModelSpendGate(kernel)
            at._hooks["llm_gates"].append(gate)
            undo.append(lambda: at._hooks["llm_gates"].remove(gate))
        else:
            at._warn("aegis-old", "this aegis version has no reserve_spend; model spend is observed but not gated")
    def tag_agent(run, step):  # model calls don't pass through the kernel; attribute them to the bound agent
        if step.get("kind") == "llm" and not step.get("agent"):
            g = current_grant()
            if g is not None:
                step["agent"] = g.agent_name
                step["governed"] = True
    at._hooks["step"].insert(0, tag_agent)
    undo.append(lambda: at._hooks["step"].remove(tag_agent))
    if watchdog is not None:
        watchdog.kernel = kernel
        at._hooks["step"].append(watchdog)
        undo.append(lambda: at._hooks["step"].remove(watchdog))

    info_cache = {}

    def meta(run):
        grant = current_grant()
        policy = getattr(grant, "policy", None)
        if policy is None:
            return {}
        key = id(policy)
        if key not in info_cache:
            info_cache[key] = policy_info(policy)
        info = info_cache[key]
        return {"policy_version": info["label"], "policy": info, "framework": "agentdynamics-sdk+aegis"}
    at._hooks["run_meta"].append(meta)
    undo.append(lambda: at._hooks["run_meta"].remove(meta))

    tw = None
    if tripwires:
        if isinstance(tripwires, TripwireGuard):
            tw = tripwires
        elif isinstance(tripwires, tripmod.Tripwires):
            tw = TripwireGuard(tripwires)
        else:
            tw = TripwireGuard(tripwires.get("tools"), tripwires.get("canaries"))
        for name in tw.wires.ignored:
            at._warn(f"tripwire-short-{name}", f"tripwire canary {name!r} ignored: shorter than "
                                               f"{tripmod.MIN_CANARY} characters would match ordinary text")
        tw.kernel = kernel
        kernel._agentdynamics_tripwires = tw
        at._hooks["step"].append(tw)
        undo += [lambda: at._hooks["step"].remove(tw), lambda: setattr(kernel, "_agentdynamics_tripwires", None)]

    rv = None
    if revocations:
        if root is None:
            at._warn("revocations-noroot", "revocations need the root grant: instrument(kernel, root, revocations=True)")
        else:
            rv = revocations if isinstance(revocations, Revocations) else Revocations()
            kernel._agentdynamics_revocations = rv
            rv.start(kernel, root)
            undo += [rv.stop, lambda: setattr(kernel, "_agentdynamics_revocations", None)]

    def unregister():
        for u in undo:
            try:
                u()
            except ValueError:
                pass
    return Governance(kernel, root, gate, watchdog, unregister, rv, tw)
