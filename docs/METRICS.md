# Agent-flow metrics

These metrics were chosen because each one maps to a decision someone can act on. Everything marked ✅ is computed today for every source that carries the data.

**Outcome & quality: did the agent do the job?**
- ✅ Task success, failure and rework rates. Each outcome records **how it was decided** (`outcome_source`), strongest evidence first:
  1. **graded** after the fact: `POST /api/tasks/{id}/outcome`, or `POST /api/outcomes` with a list for eval pipelines (needs the `ingest` role; `"outcome": null` clears);
  2. **graded** in the run: `agentdynamics.outcome("failed", reason=...)` inside a traced task (the last call wins);
  3. **feedback**: a recorded score of 0.5 or more is `completed`, below is `rework`;
  4. **inferred** from signals, when nothing states it: an error ends a run as `failed`, an interrupt as `interrupted`, a correcting follow-up in the same thread ("still not working") as `rework`.

  The Overview shows how much of the success rate is stated and how much is guessed, and a task shows who graded it and why. Grades live in a durable table: they survive schema upgrades, and a grade may arrive before its task and applies when the task does.
- ✅ First-time-right rate, Agent Apdex and user feedback score (from LangSmith/Langfuse feedback and scores).
- ✅ Verification rate for coding agents: after the last code edit, did the agent run tests, a build or the app?

**Efficiency: did it do the job economically?**
- ✅ Cost, tokens and steps per task, compared with the baseline for that task type (the multiplier of normal cost).
- ✅ Avoidable spend: duplicate tool calls, redundant reads and retry streaks, priced at the model turn that issued them.
- ✅ Cost by phase and by node, showing where the money goes (explore / edit / verify, or per graph node).
- ✅ Prompt-cache hit rate, context growth per turn, peak context and compactions.
- ✅ Parallelism: tool calls per model turn.

**Flow structure: how did it move through the graph?**
- ✅ Execution path per run, and path variants with success rate and cost for each variant.
- ✅ Directly-follows graph (node → node transitions) with failure overlay.
- ✅ Loop depth: max executions of a single node per run. Loop rate: runs with a node executed ≥5 times.
- ✅ Critical node: the node that accounts for most of a run's elapsed time, and its share.
- ✅ Handoffs between agents, plus ping-pong detection (A → B → A).
- ✅ Human-in-the-loop interrupts (LangGraph `interrupt`, human spans).

**Reliability: where does it break?**
- ✅ Tool error rate, error streaks (the agent retrying the same failure), and failed runs with root error.
- ✅ Model-call errors, rate limits / overloads (429/529), `max_tokens` truncations and refusals / content filters.
- ✅ Empty retrievals (retriever returned zero documents).

**Latency: is it fast enough?**
- ✅ End-to-end p50/p95, active agent time (idle gaps capped), and node p50/p95.
- ✅ Time to first token and output tokens/second per model.

**Process quality: is it a good worker?**
- ✅ Six scores per task (efficiency, focus, reliability, verification, context, autonomy) with coaching findings.

**Roadmap (not implemented):** LLM-as-judge grading of outcomes, tool-selection accuracy against labeled datasets, plan adherence (plan steps compared with executed steps), goal drift, semantic loop detection (similar but not identical calls), per-user and per-tenant cost allocation, anomaly detection on time series.


## AppDynamics → AgentDynamics mapping

| AppDynamics | AgentDynamics | Agent question it answers |
|---|---|---|
| Application Flow Map | **Flow Map** | Who calls what: user → agents (and agent-to-agent handoffs) → models and tools, with volume, latency and errors |
| Business Transactions | **Task Types / Workflows** | One entry point = one transaction type (a LangGraph graph, an agent, or a coding-task intent) |
| Transaction Snapshots | **Task snapshot** | Span tree waterfall, execution path, context growth, cost against the baseline, findings, final answer and the user's reaction |
| *(no equivalent)* | **Workflow process mining** | The directly-follows graph of real node transitions, path variants with success rates, loops, critical node |
| Dynamic baselines | **Baselines** | Median and p90 cost, latency and steps per task type, learned from history |
| Apdex | **Agent Apdex** | Outcome combined with cost against the baseline: satisfied / tolerating / frustrated |
| Health rules → Events → Alerts | **Health Rules → Events → Webhooks** | 28 built-in rules (including Aegis governance), editable thresholds, Slack/JSON webhooks |
| Backends / DB calls | **Tools** | Error rate, p95 latency and context bloat per tool / MCP server / retriever |
| Infrastructure | **Models** | Spend, TTFT, tokens/s, truncations, rate limits, cache hit and context size per model |
| Business iQ | **Analytics** | Any metric by any dimension, with CSV export |
| Compare Releases | **Compare** | Model A vs B, prompt v1 vs v2 (by period), project vs project |
| SLAs | **SLOs** | Objectives with error budgets and 24h burn rates |
| *(no equivalent)* | **Process Review** | Scores the *agent's process* and suggests fixes |

