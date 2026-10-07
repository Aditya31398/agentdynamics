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

**Restrict instead of revoke.** A directive can take some of an agent's authority away and leave it the rest:

```bash
agentdynamics revoke --agent support-bot --project helpdesk --tools fs.read,email.send --reason "probing fs.read"
agentdynamics revoke --agent researcher --budget 0.25 --reason "running up opus spend"   # keep a quarter of what's left
```

(or `{"tools": [...], "budget": ...}` in `POST /api/revocations`, the "tools to take away" field on the card, or
**Take ... away** on an incident, which offers the tools the agent misused there). With aegis-kernel 0.6 or later it
is applied through `Kernel.restrict`: the grant and everything under it lose those tools in place, Aegis refuses
a call to one with `capability.not_granted`, and the agent keeps working with the rest. With an older Aegis,
which can't narrow a grant, the integration revokes a grant -- or anything under it -- the moment it calls a tool
that was taken; the budget part can't be applied there, and the process warns once. Like a revocation, a
restriction is permanent for the grants it reached and only stops applying to new ones when cleared.

- **How it arrives.** With `revocations=True`, `instrument()` fetches the directives once before it returns (waiting
  at most 3 seconds), so a process restarted while its agent is revoked or restricted is held from its first call;
  then a background thread polls `GET /api/revocations` every 10 seconds, with the key given to
  `agentdynamics.init()` (the ingest role is enough). It revokes or restricts the matching grants through
  `kernel.revoke` / `kernel.restrict`, in every grant tree it has seen (an app often makes a root per conversation).
  Before every governed call and on every spawn, it checks the grant against the directives too, so a grant it
  hasn't seen yet is dealt with before it acts. The kernel enforces, and records each one in its audit log; the
  task shows it with the directive's id and reason.
- **It can only take away.** A directive revokes or restricts and nothing else, and both are permanent in Aegis:
  clearing a directive stops it applying to new grants and restores none. This keeps the promise that nothing here
  can loosen Aegis.
- **Failing.** If the server can't be reached, nothing is revoked and the agent carries on, as it would without
  this; the process warns once, and startup waits no more than those 3 seconds. A directive issued while a process
  runs takes effect within one poll interval. Clocks matter: a directive's expiry is compared with the process's
  clock.
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

### 3d. Incidents: one thing to judge per agent

A tripwire touched in one task, probing in the next, the directive that stopped the agent: three health-rule
events and a directive, one story. The **Incidents** page tells it once. An incident gathers the security
signals about one agent in one project -- or, for agents without an Aegis name (OTLP, LangSmith), one
workflow -- from its first signal until it has been quiet for `gap_hours`, and stays open until someone
resolves it as **real** or a **false alarm**, with a note.

```toml
[incidents]
rules = ["tripwire", "repeated_denials", "revoked", "policy_denials"]   # the health rules that are signals
gap_hours = 24
```

Every directive is a signal too, of the agent it names. The incident page shows the signals task by task,
the steps that are the evidence (the touching, refused and revoked calls), and what to do: revoke the agent
(recommended when the evidence is of intent -- a tripwire, probing -- and not for a few refused calls),
tighten the policy it ran under, or give the verdict.

- **New evidence after a verdict is news.** A signal about an agent whose incident was resolved opens a new
  incident, even one from before the verdict that arrived late: nobody has judged it. A directive issued
  before the verdict is the exception -- it is what was done about the incident (revoke, then resolve), and
  joins it.
- **Alerts.** A destination with `kinds = ["incidents"]` is told when an incident opens, when it escalates
  (warning to critical), and when it is resolved -- once each, however many signals it gathers, so it can
  take incidents in place of `events`. PagerDuty gets one alert per incident, resolved with the verdict.
  History is never announced: nothing on a process's first refresh, nothing more than an hour old.
- **Durable.** Incidents and their signals are kept in the store like grades: a verdict is something a
  person said. Each signal joins one incident once, and an incident's id is a hash of the agent and its
  first signal, so a rebuild -- or a second store fed the same traffic -- derives the same incidents.
  `agentdynamics store copy` copies them.
- **Who.** Anyone who can read a project sees its incidents. A verdict needs an admin key: the trust score
  will read verdicts, and marking real incidents false alarms would weaken it.

### 3e. Trust: what an agent's own behaviour says about it

Each agent has a trust score from 0 to 100, on the Incidents page (lowest first), on each incident, and in
Prometheus as `agentdynamics_agent_trust{project, agent}`. It is a reputation: every task the agent works in is
either a clean task or bad evidence, weighing as many bad tasks as its severity says --

| Evidence | Counts as |
|---|---|
| a task in which the agent touched a tripwire | 10 bad tasks |
| a task in which it had 3 or more calls refused in a row (probing) | 3 bad tasks |
| a task in which the policy refused its calls | up to 1 bad task, by the share refused |

-- on top of a start of 19 clean tasks and 1 bad one. Trust is the cautious (10th percentile) estimate of the
share of clean tasks that evidence implies, times 100. So the same evidence weighs more on an agent with little
history: one tripwire puts a new agent on watch, and barely moves one with thousands of clean tasks behind it.
A new agent starts near 89 and earns its way up with clean work. Evidence counts half as much a week later
(`half_life_days`). Below 80 it is on **watch**; below 50, **low**. Click an agent for the counts and the tasks
behind them. Tripwires act on their own whatever an agent's trust: they stop the run, and revoke an agent that
touches them repeatedly (3c).

- **Behaviour, not competence.** A failed task or a tool error is a mistake, not an attempt to go further than
  allowed, so neither lowers trust. The agent's success rate is shown beside it instead.
- **A share, not a count.** Refused calls count as a share of the task's calls: a busy agent is not marked down
  for being busy, only for being refused more often.
- **The policy's friction is not the agent's fault.** Refusals under a rule that at least half of a project's
  agents (and at least 3) run into say the rule is too tight, not that each agent is misbehaving: they are shown
  as policy friction and don't count against anyone (`friction_share`, `friction_agents`).
- **Each agent answers for itself.** When a root agent and the sub-agents it spawned act in one task, each is
  scored on its own steps.
- **A person's verdict wins.** A task whose incident was resolved as a false alarm counts as clean; evidence in
  one confirmed as real counts 1.5 times (`confirmed`).
- **Computed when read.** From the tasks still held, the verdicts, and the clock -- nothing stored, so it
  follows retention and a changed verdict at once.

```toml
[trust]
half_life_days = 7
tripwire = 10          # bad tasks a tripwire task counts as
probing = 3
denial_rate = 1        # a task with every call refused
confirmed = 1.5
prior_clean = 19       # where every agent starts
prior_bad = 1
friction_share = 0.5
friction_agents = 3
watch = 80
low = 50
```

Trust is a number to watch and to alert on (`agentdynamics_agent_trust < 50` in Alertmanager), and the server can
act on it:

```toml
[enforcement.trust]
restrict_below = 50     # an agent below this loses the tools it misused, wherever it runs ...
minutes = 60            # ... for this long, renewed while it stays low
```

A low-trust agent is restricted, not revoked: it loses the tools it was refused or touched a tripwire with (or, if
nothing names a tool, half of what remains of its budget) and keeps doing everything else. The restriction is
renewed while trust stays low and lapses once it recovers -- including when a person resolves its incident as a
false alarm. Renewals aren't incident signals: they follow from evidence the incident already holds. Off unless
configured, since it acts on running agents.

### 3f. Untrusted input (aegis-kernel 0.6+)

Aegis can mark a tool `untrusted` -- it returns content from outside the trust boundary, like a web page, an inbound
email or an upload -- and a policy can name what an agent may no longer do once it has read such content:

```python
registry.register("web.fetch", fetch, effects={"network", "read"}, untrusted=True)
```
```yaml
integrity:
  untrusted_blocks: [egress]     # nothing leaves after untrusted input
```

The kernel refuses those calls with `integrity.untrusted_input`, which the Governance page, incidents and trust
count like any other refusal. The governed demo's phishing scenario shows it: the agent fetches the page a ticket
links to, then does what the page says, and the call is refused though the policy allows it otherwise.

### 3g. The checker: a model's view of each incident, in shadow mode

The detectors above are rules: certain, cheap, and blind to meaning. The checker asks a model what an incident
looks like -- prompt injection, probing, an exfiltration attempt, a policy too narrow for the agent's real work,
an honest error -- and what it would do about it, and optionally grades tasks whose outcome was only inferred.

```toml
[checker]
model = "claude-opus-5-5"     # pip install anthropic; ANTHROPIC_API_KEY (or an `ant auth login` profile)
effort = "medium"
max_per_hour = 20             # model calls, reviews and grades together
grade_outcomes = false
apply_grades = false          # apply its outcome grades once they agree with people's (below)
min_kappa = 0.6
min_pairs = 30
```

- **Shadow mode is the only mode.** A review is shown on the incident and in the incident list, and nothing acts
  on it; a grade is stored beside the task's own outcome, never in its place. The Incidents page keeps its record:
  how often its call agreed with the verdict you gave, and its grades with yours. That record -- not the model's
  stated confidence -- is what would justify ever letting it act.
- **It sees the stored copy.** The evidence is built from what the console shows (redacted, or no content at all
  with `store_content = false`), never from the raw telemetry.
- **It reads attacker-written text safely.** A prompt, a document, a tool's output can carry instructions aimed at
  whoever reads them next. The evidence goes to the model as one fenced JSON document that its instructions call
  data, and the answer must fit a fixed schema; an answer outside it is discarded.
- **It holds no authority, and costs what it says.** One call per incident, again when the incident has twice the
  signals, within `max_per_hour`; the writer instance makes them in the background. A refusal or a bad answer is
  recorded; an overloaded API is retried five minutes later. Tokens spent are on the card. A safety classifier
  that declines (security text trips them more than most) falls back to Anthropic's recommended model.
- **Its outcome grades can earn a say.** With `grade_outcomes`, it also grades a sample of the tasks people graded
  -- blind: the evidence it sees never holds an outcome, inferred or stated -- and the Incidents page keeps its
  agreement with them as Cohen's κ (agreement beyond chance). With `apply_grades = true`, once κ reaches
  `min_kappa` over `min_pairs` tasks, its grades replace inferred outcomes (`outcome_source` "model"); a person's,
  a business system's or a feedback score still wins. If its agreement drops below the bar, they stop applying.
  Incident reviews stay shadow-only.

### 3h. A review before calls that can't be undone

A policy decides from a call's shape: this tool, this argument range. It can't tell a refund the customer asked
for from one an injected note added, when both fit. For the few tools whose effect can't be taken back, a
model can check each call against what the user actually asked, before Aegis admits it:

```python
governance.instrument(kernel, root, review={"tools": ["payments.refund", "email.send"]})   # pip install anthropic

@agentdynamics.trace("support", prompt=ticket)      # the request each call is judged against
def handle(ticket): ...
```

- **It can only refuse.** It is the last guard in the kernel's chain, so a call the policy refuses never reaches
  it (and costs nothing), and a call it allows has passed every other guard. A refusal is `review.blocked`, with
  the reviewer's reason, in the audit log and on the task like any denial.
- **It doesn't read what the agent read.** It sees the user's request and the proposed call, not the documents
  and tool results on the way, which is where an injected instruction comes from: a reviewer that read the
  injection could be talked into approving it. The evidence is still fenced off as data.
- **It fails closed.** No traced request to judge against (`review.no_request`), no SDK or key, an API error, an
  answer outside the schema (`review.unavailable`): the call is refused. List only the tools worth a model call
  and that latency; the call is synchronous, as Aegis guards are.
- **Its cost is the task's.** Each review is a sub-agent run of the conversation (workflow `aegis.review`): the
  task shows it under its sub-agents and in its sub-agent cost, not as one of the agent's own turns. It isn't
  reserved against the Aegis budget, and the watchdog's per-run limits don't count it.
- The arguments go to the model as they are -- judging a recipient takes seeing it. Model and effort are
  `review={"tools": [...], "model": "claude-opus-5-5", "effort": "low"}`.

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
