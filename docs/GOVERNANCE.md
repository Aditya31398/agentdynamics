# Governance with Aegis

[Aegis](https://github.com/Aditya31398/aegis) enforces what an agent **may** do: every tool call passes a
declarative policy (capability allowlists, argument constraints, budgets, PII egress rules, bounded spawning)
before it executes. AgentDynamics shows what the agent **did**. Connected, they form a loop:

```
          ┌──────────────── 4. observe → govern: tightened policy (ratified, drift-checked) ───────────────┐
          ▼                                                                                               │
   Aegis policy ──► Aegis kernel ──► tool executes / is denied ──► 1. decision recorded in the task ──► AgentDynamics
          ▲              ▲                                                                                │
          │              └──── 2. model calls reserve & settle against the same budget ◄─────────────────┤
          └──────────────────── 3. watchdog revokes the grant when a run misbehaves ◄────────────────────┘
```

## Setup

```python
import agentdynamics
from agentdynamics.integrations import aegis as governance
from aegis import build_kernel, load_policy

agentdynamics.init(project="support")                         # Anthropic / OpenAI / LangChain auto-instrumented
kernel, root = build_kernel(load_policy("policy.yaml"), registry)
governance.instrument(kernel, root, watchdog=governance.Watchdog(max_repeated_denials=3))
```

Use a separate grant per conversation or agent, and bind it so model calls and the watchdog act on it:

```python
@agentdynamics.trace
def handle(ticket):
    grant = Grant.root(policy)                 # or kernel.spawn(parent, SpawnRequest(...)) for a sub-agent
    with governance.bind(grant):
        ...
```

`instrument()` requires Aegis with `aegis.observe` and `Kernel.reserve_spend` (the version in the Aegis repo's
`main`). With an older Aegis, decisions are still recorded, but not correlated or gated, and a warning says so.

## The four flows

### 1. Govern → observe: every decision lands in its task

- Each `kernel.invoke` becomes a tool step carrying `rule`, `guard`, `agent` and depth. A denied call is a
  `denied` step (not a tool error), flagged as waste because the model spent tokens generating it.
- `kernel.spawn` becomes an agent span. `kernel.revoke` becomes a `revoked` notice.
- Aegis stamps `details.ctx = {run_id, workflow, node, project}` into every audit record, so the
  hash-chained Aegis log and AgentDynamics join on run id.
- New task metrics: `policy_denials`, `spend_denials`, `budget_denials`, `repeated_denials` (the same tool
  refused N times in a row, whatever the rule), `revocations`, `blocked_cost`, `denied_rules`, `policy_version`,
  and a **compliance** process score.
- New health rules: *Actions blocked by policy*, *Agent probing a boundary*, *Grant revoked*, *Budget stop*.

### 2. Budgets cover model spend

Aegis budgets used to count only the registered cost of tools. Now every Anthropic / OpenAI call (and anything
wrapped in `agentdynamics.llm_call`) first reserves its estimated cost against the Aegis ledger. The estimate is
input size plus `max_tokens`, at list price. The actual cost is settled after the call. An exhausted budget, a
passed deadline or a revoked grant refuses the reservation, so **the request is never sent**. Spend is
hierarchical: a sub-agent's model calls debit every ancestor, so a swarm can't outspend its root.

### 3. Detect → enforce: the watchdog

`Watchdog(max_repeated_denials=3, max_denials=None, max_node_visits=None, max_run_cost_usd=None, max_llm_calls=None)`
is evaluated on every step as it happens. When a limit trips, it calls `kernel.revoke(grant)`. That disables the
grant's whole sub-tree immediately, and every later tool or model call is denied with `grant.revoked`. The
revocation is recorded in both the Aegis audit log and the task.

The default (same tool refused 3 times in a row) catches the classic prompt-injection pattern: a retrieved
document tells the agent to read secrets, and the agent keeps trying with different payloads.

### 3b. Enforce from the server: revocation directives

The watchdog sees one run. The server sees them all, so it can spot an agent probing its policy a little in each
of many runs, and it can act on an operator's decision. It does so with a **directive**: revoke agent *A* in
project *P* (or every agent, or every project) until a given time.

```python
governance.instrument(kernel, root, revocations=True)   # opt in: lets the server stop agents in this process
```

```bash
agentdynamics revoke --agent researcher --project helpdesk --reason "running up opus spend" --minutes 60
agentdynamics revoke --list
agentdynamics revoke --clear <id>
```

or the **Revocation directives** card on the Governance page (an admin key issues and clears), or
`POST /api/revocations`. To have the server issue them itself:

```toml
[enforcement]
probing = { denials = 10, runs = 3, window_minutes = 30, revoke_minutes = 60 }
```

That revokes an agent whose calls the policy refused 10 times across 3 or more runs within 30 minutes.

- **How it arrives.** With `revocations=True`, a background thread polls `GET /api/revocations` every 10 seconds,
  with the key given to `agentdynamics.init()` (the ingest role is enough). It revokes the matching grants through
  `kernel.revoke`, in every grant tree it has seen (an app often makes a root per conversation). Before every
  governed call and on every spawn, it checks the grant against the directives too, so a grant it hasn't seen
  yet is revoked before it acts. The kernel enforces, and records each revocation in its audit log; the task
  shows it with the directive's id and reason.
- **It can only take away.** A directive revokes and nothing else, and Aegis revocation is permanent: clearing a
  directive stops it applying to new grants and restores none. This keeps the promise that nothing here can
  loosen Aegis.
- **Failing.** If the server can't be reached, nothing is revoked and the agent carries on, as it would without
  this; the process warns once. A directive takes effect within one poll interval. Clocks matter: a directive's
  expiry is compared with the process's clock.
- **Trust.** The process acts on directives from the server it is configured to send telemetry to. Someone
  controlling that server could stop agents, but never grant them anything.

### 3c. Tripwires: decoys no legitimate agent touches

The watchdog and the probing detector judge behaviour against thresholds. A tripwire needs none: it is a decoy tool
no agent has reason to call, or a **canary** -- a value planted where no agent has reason to look, such as a fake
credential in a config file or a path in a document. An agent that touches one went somewhere it had no business
going, whatever the policy allowed, so one touch is certain evidence and there is nothing to tune.

```python
governance.instrument(kernel, root, tripwires={
    "tools": ["secrets.vault_export"],                       # registered, never needed
    "canaries": {"planted_aws_key": "AKIA-CANARY-7F3E9Q"},   # name -> planted value, 8+ characters
})
```

In process, every governed call is checked **before** it runs: a decoy tool, or a canary in its arguments, revokes
the run's whole grant tree through `kernel.revoke` first, so Aegis refuses the call and the canary never leaves.
A canary in a tool's result (the agent read the planted file) or in a model response revokes the tree at once;
every later call and model request is refused. The whole tree, because a sub-agent that found a canary got there
with what its parent gave it. Only that run stops.

On the server, the same tripwires mark every step that touches one, from any source -- OTLP, LangSmith and SDK runs
included -- and the `tripwire` health rule raises a critical event for each task that did:

```toml
[enforcement.tripwires]
tools = ["secrets.vault_export"]
canaries = { planted_aws_key = "AKIA-CANARY-7F3E9Q" }
runs = 2                  # an agent that touched them in 2 runs ...
window_minutes = 60       # ... within 60 minutes is revoked wherever it runs (a directive, 3b)
revoke_minutes = 60       # 0: mark, raise the event and alert, but issue no directive
```

Why not revoke the agent everywhere on the first touch? A directive stops that agent for every user, and one touch
can come from one planted document: revoking on it would let whoever planted the document switch the agent off.
Touches in several runs mean the cause persists -- a poisoned document every conversation retrieves, a compromised
model -- and then the agent should stop everywhere. Directives are off unless `[enforcement.tripwires]` is set;
touches marked in process count towards them once it is.

- **Names, not values.** Events, alerts, directives and the console name a canary (`canary planted_aws_key`),
  never its value, which would tell whoever reads them what to avoid. A canary shorter than 8 characters is
  ignored: it would match ordinary text.
- **Where to plant them.** Somewhere an attacker's instructions would send an agent and a legitimate task never
  does: a credentials file beside real configuration, a document the policy lets the agent read. The server
  matches canaries in what a source sends, before redaction, so `store_content = false` doesn't blind it; a
  source that sends no content (an SDK with content capture off) leaves it only decoy tools to see. The
  in-process check sees every argument and result either way.
- **What you see.** The Governance page lists every touch, newest first, and the task's timeline tags the step.

### 4. Observe → govern: least-privilege policy from real behaviour

```bash
agentdynamics policy report                                        # unused grants, budget headroom, denials by rule
agentdynamics policy export --workflow support_agent --out tightened.yaml
aegis ratify --policy tightened.yaml
aegis drift  --baseline policy.yaml --candidate tightened.yaml    # must report no widening
```

The console's **Governance** page has the same **Generate tightened policy** button. The export can only
tighten the base policy:

| | |
|---|---|
| tools | only tools actually called and allowed; unused grants are removed; never adds a tool the base lacks |
| arguments | observed path/URL prefixes narrow a base prefix; numbers get a `max_value` ceiling (largest observed × headroom); small categorical sets become `one_of`; `max_len` = 2× longest observed; base regexes are kept |
| budget | p99 of real per-task usage × headroom (default 1.5), capped at the base |
| spawn | depth / fan-out actually used; no spawning if none was observed |
| data | unchanged (removing an egress sink reads as widening to Aegis drift, even for a removed tool) |

Every change is listed at the top of the YAML for review. Runs record the policy name, version and digest,
so **Compare → policy** shows whether a tighter policy changed success rate, cost or rework.

### Checking a policy change against real traffic

`aegis ratify` and `aegis drift` compare declarations. Neither ever sees a call, so a policy can be strictly
tighter than its base, constitutional and free of drift, and still refuse a third of production. Only the
recorded traffic can tell you that, so AgentDynamics checks it:

```bash
# any candidate, hand-edited or generated: how much of what actually ran would it refuse?
agentdynamics policy check --candidate policy.yaml --workflow support_agent
#   would deny 7 of 20 calls that were allowed (35.0%)
#     kb.search            4 of 4     100.0%  capability.missing_arg x4
#     payments.refund      2 of 5      40.0%  capability.arg_max_value x2
#     email.send           1 of 1     100.0%  capability.not_granted x1
#   FAIL: 35.0% exceeds --max-denied-fraction 0.0%
```

Each recorded call is judged by Aegis's own `CapabilityGuard`, the code that enforces the policy, so the
report agrees with enforcement on which tools and arguments are allowed. Budgets are cumulative per run, not
per call, and appear as headroom in the policy table instead. Only calls that went through a kernel are
counted: a plain `@tool` function the policy never applies to is not refused by leaving it out. Where
arguments weren't recorded (content capture off), only the tool grant is checked, and the report says how
many calls that was.

The command exits non-zero when the refused share exceeds `--max-denied-fraction` (default `0`: any refusal
fails), so it can gate a policy change in CI. `policy export` runs the same check on what it generates, and
so does the **Generate tightened policy** panel. A CI job without the data directory can use the API with a
`read` key:

```bash
curl -X POST $AGENTDYNAMICS_URL/api/policy/check -H "Authorization: Bearer $READ_KEY"      -d '{"workflow": "support_agent", "candidate": <policy document as JSON>}'
```

## Without the in-process integration

Aegis audit logs can be ingested directly: an agent in another runtime, CI conformance runs, or historical
logs. Use `AuditLog(path="audit.jsonl")`, then POST the file to `/api/ingest/records` or point an `inbox`
source at its directory. Records are grouped into runs by `details.ctx.run_id`, falling back to the grant.
Aegis hashes arguments instead of storing them, so these runs show decisions but can't feed policy export.
Runs already recorded in-process are not double-counted.

## Try it

```bash
pip install aegis-kernel
agentdynamics serve &
python examples/governed_agent.py 45     # normal tickets + prompt injection + runaway research + SQL escalation
```
