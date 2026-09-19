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
| arguments | observed path/URL prefixes narrow a base prefix; small categorical sets become `one_of`; `max_len` = 2× longest observed; base regexes are kept |
| budget | p99 of real per-task usage × headroom (default 1.5), capped at the base |
| spawn | depth / fan-out actually used; no spawning if none was observed |
| data | unchanged (removing an egress sink reads as widening to Aegis drift, even for a removed tool) |

Every change is listed at the top of the YAML for review. Runs record the policy name, version and digest,
so **Compare → policy** shows whether a tighter policy changed success rate, cost or rework.

## Without the in-process integration

Aegis audit logs can be ingested directly: an agent in another runtime, CI conformance runs, or historical
logs. Use `AuditLog(path="audit.jsonl")`, then POST the file to `/api/ingest/records` or point an `inbox`
source at its directory. Records are grouped into runs by `details.ctx.run_id`, falling back to the grant.
Aegis hashes arguments instead of storing them, so these runs show decisions but can't feed policy export.
Runs already recorded in-process are not double-counted.

## Try it

```bash
pip install aegis-guard
agentdynamics serve &
python examples/governed_agent.py 45     # normal tickets + prompt injection + runaway research + SQL escalation
```
