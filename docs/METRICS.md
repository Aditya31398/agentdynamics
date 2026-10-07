# Agent-flow metrics

These metrics were chosen because each one maps to a decision someone can act on. Everything marked ✅ is computed today for every source that carries the data.

**Outcome & quality: did the agent do the job?**
- ✅ Task success, failure and rework rates. Each outcome records **how it was decided** (`outcome_source`), strongest evidence first:
  1. **graded** after the fact: `POST /api/tasks/{id}/outcome`, or `POST /api/outcomes` with a list for eval pipelines (needs the `ingest` role; `"outcome": null` clears);
  2. **graded by your own key**: a business system knows its ticket and order ids, not task ids. Put them in the trace's metadata (`agentdynamics.trace("support", ticket_id="T-123")`, or LangSmith / Langfuse / OpenTelemetry metadata) and post `{"key": {"ticket_id": "T-123"}, "outcome": "rework", "reason": "ticket reopened"}` to `/api/outcomes` when the ticket is reopened, the refund reversed or the PR reverted. It settles the latest task carrying that key (`"match": "all"` for every one), now or when it arrives; a scoped key's statement reaches only its own projects;
  3. **graded** in the run: `agentdynamics.outcome("failed", reason=...)` inside a traced task (the last call wins);
  4. **feedback**: a recorded score of 0.5 or more is `completed`, below is `rework`;
  5. **model**: the checker's grade (`[checker] grade_outcomes`), applied only with `apply_grades = true` and only while it agrees with people's grades: it grades a sample of the tasks people graded, blind (its evidence never holds an outcome), and its grades are applied once Cohen's κ against them reaches `min_kappa` (0.6) over `min_pairs` (30). The Incidents page shows its record;
  6. **inferred** from signals, when nothing states it: an error ends a run as `failed`, an interrupt as `interrupted`, a correcting follow-up in the same thread ("still not working") as `rework`. Otherwise `completed`: "didn't visibly fail", so an inferred success rate is an upper bound.

  The Overview shows how much of the success rate is stated, graded by the checker or guessed, the rate's 95% interval (Wilson), and the rate over the stated outcomes alone. A task shows who graded it and why. Grades live in durable tables: they survive schema upgrades, and a grade may arrive before its task and applies when the task does.
- ✅ First-time-right rate, Agent Apdex and user feedback score (from LangSmith/Langfuse feedback and scores). Apdex judges the outcome first (failed, interrupted, rework or a 4+ error streak is frustrated), then the worse of cost and agent time against T: satisfied up to T, tolerating up to 4T, frustrated beyond. T is the target set for the task type on the Task Types page (`POST /api/apdex`), per dimension; with none, 1.5x the task's baseline median. Each task says which (`apdex_basis`). The `good_task_rate` SLO metric is the share completed *and* satisfied.
- ✅ Verification rate for coding agents: after the last code edit, did the agent run tests, a build or the app?

- ✅ Task types say how they were decided: a traced app's workflow name, the prompt's kind (slash command, scheduled job, subagent), inherited by a follow-up, or guessed from intent keywords — in which case the matched words are shown.

**Efficiency: did it do the job economically?**
- ✅ Cost, tokens and steps per task, compared with a baseline: the multiplier of its median, and where the task falls in it ("dearer than 66% of them"). Each task is compared with the tasks like it in the 14 days before its own day -- the same type, model and release (an app's version, or its Aegis policy version), when there are at least 10; else the same type and model, else the same type -- and with the type's whole history only when nothing recent is enough. The task says which (`baseline.basis`) and how many it was compared with. So "normal" follows a model switch or a deploy instead of staying where the oldest history put it.
- ✅ Avoidable spend: duplicate tool calls, redundant reads, retry streaks and refused calls, each priced at what it really cost: the output that wrote the call, plus its result's share of every later turn's input while it stayed in context (until a compaction). A result's size in tokens is estimated from its characters (÷ 4).
- ✅ Cost by phase and by node, showing where the money goes (explore / edit / verify, or per graph node), attributed the same way; the input no tool result accounts for (instructions, the prompt, the model's own messages) is "re-sent context". The parts add up to the task's cost.
- ✅ Prompt-cache hit rate, context growth per turn, peak context and compactions.
- ✅ Cost counts each input token once. Uncached input, cache reads and cache writes are priced separately; formats that document their input count as inclusive (OpenTelemetry GenAI, OpenInference, LangChain) have both kinds of cache token taken out of it. Where a format documents no rule, the split is estimated and those calls are shown under Spend as having estimated cache accounting.
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
- ✅ End-to-end p50/p95 and node p50/p95. Agent time is what steps covered: each step's own [start, end] (a 20-minute build counts 20 minutes) and gaps of up to 5 minutes between events, less human-in-the-loop spans and rejected tool calls. An approval wait inside a tool call that was then allowed is counted, unless the source records it.
- ✅ Time to first token and output tokens/second per model.

**Process quality: is it a good worker?**
- ✅ Seven scores per task (efficiency, focus, reliability, verification, context, autonomy, compliance) with coaching findings, and an overall weighted mean of those that apply.
- ✅ Whether they predict success: the Process Review page fits them to the outcomes somebody stated (graded, or a feedback score; never the inferred ones), each computed without the terms that restate the outcome. It shows each score's AUC alone, the overall's AUC with the default weights, and a logistic regression's AUC out of sample (5-fold). With `[scores] weights = "fitted"` the overall uses the fitted weights once they predict stated outcomes better than the defaults by 0.02 out of sample (refit daily): a score gets weight only if it also predicts on its own (AUC 0.55), and a score too rare to judge keeps its default.

**Roadmap (not implemented):** LLM-as-judge grading of outcomes, tool-selection accuracy against labeled datasets, plan adherence (plan steps compared with executed steps), goal drift, semantic loop detection (similar but not identical calls), per-user and per-tenant cost allocation, anomaly detection on time series.


## AppDynamics → AgentDynamics mapping

| AppDynamics | AgentDynamics | Agent question it answers |
|---|---|---|
| Application Flow Map | **Flow Map** | Who calls what: user → agents (and agent-to-agent handoffs) → models and tools, with volume, latency and errors |
| Business Transactions | **Task Types / Workflows** | One entry point = one transaction type (a LangGraph graph, an agent, or a coding-task intent) |
| Transaction Snapshots | **Task snapshot** | Span tree waterfall, execution path, context growth, cost against the baseline, findings, final answer and the user's reaction |
| *(no equivalent)* | **Workflow process mining** | The directly-follows graph of real node transitions, path variants with success rates, loops, critical node |
| Dynamic baselines | **Baselines** | Median and p90 cost, latency and steps per task type, learned from history |
| Apdex | **Agent Apdex** | Outcome first, then cost and agent time against targets you set per type (or the baseline): satisfied / tolerating / frustrated |
| Health rules → Events → Alerts | **Health Rules → Events → Webhooks** | 28 built-in rules (including Aegis governance), editable thresholds, Slack/PagerDuty/JSON destinations, SLO burn-rate alerts |
| Backends / DB calls | **Tools** | Error rate, p95 latency and context bloat per tool / MCP server / retriever |
| Infrastructure | **Models** | Spend, TTFT, tokens/s, truncations, rate limits, cache hit and context size per model |
| Business iQ | **Analytics** | Any metric by any dimension, with CSV export |
| Compare Releases | **Compare** | Model A vs B, prompt v1 vs v2 (by period), project vs project |
| SLAs | **SLOs** | Objectives with error budgets and 24h burn rates |
| *(no equivalent)* | **Process Review** | Scores the *agent's process* and suggests fixes |

