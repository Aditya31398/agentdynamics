# Architecture & integration design

```
 PUSH                                        PULL                           FILES
 LangChain/LangGraph --LangSmith API--┐      LangSmith API (runs/query) ─┐  Claude Code transcripts ─┐
 OTel SDKs / Collector --OTLP/HTTP----┤      Langfuse API (traces)   ───┤  inbox/*.jsonl (tailing) ─┤
 Fluent Bit / Vector --NDJSON---------┤                                  │  SDK run files ───────────┤
 Python SDK / webhooks --JSON---------┘                                  │                           │
                    │                                                    │                           │
                    ▼                                                    ▼                           ▼
        adapters: semantic-convention mapping → canonical spans (trace_id, span_id, parent, kind, model, tokens, node, agent, …)
                    ▼
        spans_raw  (durable, idempotent upsert by span id; late or partial spans are simply merged)
                    ▼
        trace assembly → runs → per-run analysis (cached, only dirty runs recomputed)
                    ▼
        cross-run analysis: baselines · scores · Apdex · health rules · SLOs
                    ▼
        SQLite (WAL) → REST API · web console · /metrics (Prometheus) · alert webhooks
```

Design choices that make this production-safe:

- **Speak the protocols teams already emit.** Nobody wants to re-instrument. The LangSmith-compatible endpoint makes any LangChain/LangGraph app work by changing `LANGSMITH_ENDPOINT`. The OTLP receiver accepts both protobuf and JSON with no dependencies, so standard OTel exporters and the Collector work unchanged.
- **Map semantic conventions, not vendors.** GenAI semconv (`gen_ai.*`), OpenInference (`openinference.span.kind`, `llm.*`), OpenLLMetry (`traceloop.*`), LangSmith (`run_type`, `langgraph_node`) and Langfuse observation types all map to one canonical span. New frameworks usually need only a few attribute aliases.
- **Idempotent and order-independent ingestion.** Spans are upserted by id. LangSmith PATCHes, late child spans and feedback that arrives before its run all merge correctly. Re-pulling the same window never double-counts.
- **Pull where push isn't possible.** Incremental cursors are persisted per source, with a re-read window for runs that were still open.
- **Log pipelines as a first-class path.** `/api/ingest/records` accepts NDJSON or JSON arrays of any supported format and unwraps shipper envelopes (`{"log": "..."}`, `{"message": {...}}`). The `inbox` source tails files by byte offset and handles rotation, which suits air-gapped hosts and S3/GCS sync jobs.
- **Filter at the edge.** The Collector config in `deploy/` forwards only agent spans and can strip prompt content before it leaves your network.

Setup snippets for each path are on the **Integrations** page in the console.

## Alerting

Configured as `[[alerts.webhooks]]` (the full reference is the docstring of `agentdynamics/alerts.py`):

```toml
[alerts]
console_url = "https://agentdynamics.internal"   # alerts link to the task or SLO

[[alerts.webhooks]]            # a team channel: its project's events and SLO alerts
format = "slack"
url = "https://hooks.slack.com/services/..."
kinds = ["events", "slos"]
projects = ["checkout"]

[[alerts.webhooks]]            # the pager: only fast SLO burns
name = "pager"
format = "pagerduty"
routing_key_env = "PD_ROUTING_KEY"
kinds = ["slos"]
min_severity = "critical"
```

- **Health-rule events** are points in time. Each new one is sent once. Nothing is sent for history on a
  process's first start, or for tasks that ended more than an hour ago (a backfill does not page).
  PagerDuty and Slack group a burst by rule, project and task type: fifty runaway-cost tasks are one
  PagerDuty alert and one Slack line. JSON webhooks receive every event, in the original
  `{"source": "agentdynamics", "events": [...]}` shape.
- **SLO alerts** have a start and an end. Ratio objectives (success rate, Apdex) use multi-window,
  multi-burn-rate alerting from the Google SRE Workbook. A *page* (critical) fires when 2% of the error
  budget would go in an hour or 5% in six hours. A *ticket* (warning) fires at 10% in three days. Each is
  confirmed by a short window (1/12 of the long one), so it clears soon after the burn stops. Thresholds
  scale with the SLO's window; for 30 days they are the workbook's 14.4, 6 and 1. Other objectives raise a
  *breach* alert while missed. A long window needs `slo_min_tasks` tasks (default 10) before it can alert.
  An agent can go quiet for longer than a short window, so an empty short window keeps an alert firing
  but never starts one. What is firing is kept in the `alert_state` table, so a restart neither repeats a
  page nor forgets to resolve it. The alerts are evaluated every 30 seconds, not only when traffic
  arrives, so a burn resolves as time passes.
- **Delivery** goes through the durable `alert_outbox` table: at least once, and in order per destination
  (a resolve never overtakes its trigger). A 429, 5xx or network error is retried with backoff for a day.
  Any other 4xx is a configuration error: dropped, logged and shown on the Integrations page. No secret is
  stored: PagerDuty routing keys are added at send time, and destinations are identified by name, not URL.
- **Privacy.** A rule message can quote user text (the `rework` rule quotes the correcting message), so
  messages get the same redaction as stored text, and none of it with `store_content = false`.
- **Operating it.** `agentdynamics alerts status` lists destinations, queue and firing alerts;
  `agentdynamics alerts test` sends a test alert to each (and resolves it on PagerDuty). `/api/alerts` and
  the Integrations page show the same. `/metrics` exposes `agentdynamics_slo_burn_rate` and
  `agentdynamics_slo_burn_threshold` per window, so teams already paging through Alertmanager can alert
  on the same numbers.

## Project-scoped keys

A key created with `--project` (repeatable) is confined to those projects.

- **Reads.** Scoping is not left to each endpoint. A scoped request reads through its own connection on which
  `tasks`, `runs`, `steps` and `events` are temporary views limited to the key's projects
  (`store.connect_reader`), and the connection is read-only. Every endpoint, including ones added later, sees
  only those rows. Install-wide endpoints (`/metrics`, `/api/sources`, `/api/config`) refuse a scoped key.
  SLOs are shown only when they cover the key's projects or its own tasks. `tests/test_scoped_keys.py` reads
  the route list out of `server.py` and calls every route with a scoped key, requiring that another project's
  data never appears.
- **Writes.** Ingestion is keyed by ids the client chooses, so without checks a scoped key could add spans to
  another project's trace, overwrite its runs or PATCH and rate them. A single-project key has its project
  stamped onto everything it sends; a multi-project key must name one of its projects. No scoped key may
  touch a trace, span id, run or LangSmith run that exists in another project. The whole request is checked,
  under the engine lock, before any of it is written, and a refusal is a 403 listing the ids.
- **Analysis.** Conversation threads and subagent parents are linked only within a project, so a client-chosen
  thread or parent id can't reach into another project's tasks.
- **Limits.** Baselines are computed per task type across the install, so a scoped key's `cost_vs_baseline`
  reflects every project's tasks of that type. A scoped key can grade only tasks that already exist in its
  projects. Aegis audit records are hash-chained, so they are checked, never stamped: each must already name
  a project in scope. Feedback that arrives ahead of its run is accepted from a single-project key (the stub
  is stamped with its project) and refused from a multi-project key, which can't say where it belongs. Admin
  keys cover the whole install and can't be scoped.

## Enterprise features

| Area | What's there |
|---|---|
| Security | API keys with roles: `ingest` writes telemetry only, `read` views console, API and metrics, `admin` edits rules and SLOs. `read` and `ingest` keys can be scoped to projects (`keys create --project`); see below. Constant-time key comparison. The LangSmith `x-api-key` header is honored. |
| Privacy | Regex redaction of emails, API keys, bearer tokens, AWS keys and card numbers (plus custom patterns), applied before storage. `store_content = false` keeps only sizes and metadata. |
| Retention | Per-deployment retention window; old spans and runs are purged automatically. |
| Multi-environment | `deployment.environment` / metadata `environment` becomes a first-class filter, alongside project and framework. |
| Operations | `/healthz`, Prometheus `/metrics`, a per-source status page with last error, gzip/deflate request bodies, a 64 MB body limit, and a non-root Docker image with a healthcheck. |
| Alerting | Health-rule events and SLO burn-rate alerts go to Slack, PagerDuty (Events API v2) or JSON webhooks, routed per destination by project, rule, severity and kind. See [Alerting](#alerting). |
| Scale path | The storage layer is thin (`store.py`). SQLite in WAL mode handles single-node workloads. Swap in Postgres or ClickHouse for high volume; analysis already works incrementally per run. |

## Configuration

`<data>/agentdynamics.toml` (see `deploy/agentdynamics.toml` and `agentdynamics/config.py`). Environment overrides for containers:

- `AGENTDYNAMICS_API_KEYS="name:key:role,..."`
- `AGENTDYNAMICS_RETENTION_DAYS`
- `AGENTDYNAMICS_STORE_CONTENT=0`
- `AGENTDYNAMICS_CONFIG`
- `AGENTDYNAMICS_DATA`

Other files in the data directory: `rules.json` (health rules), `slos.json` (objectives) and `pricing.json` (per-model rate overrides; models without a known price are flagged as *unpriced*, never guessed).

## Examples

- `examples/langgraph_style_app.py` is a LangGraph-style support agent traced with the real LangSmith SDK. It exercises routing, retrieval, agent ⇄ tools loops, failures, multi-turn corrections and feedback.
- `examples/otel_multiagent.py` is a multi-agent helpdesk (triage → billing / tech_support / privacy) sent as OTLP GenAI semconv, with handoffs, ping-pong, rate limits and truncations.
- `examples/demo_agent.py` is a custom agent instrumented with the Python SDK.

## Known limitations

- Task intent for coding agents comes from keyword rules. Traced apps use their entry-point name instead.
- Outcomes are inferred from signals (errors, interrupts, corrections, feedback). There is no ground-truth grading yet (see roadmap).
- Costs use list prices. Subscription plans bill differently, so treat the figures as a relative measure of effort.
- LangSmith multipart ingestion is supported, but zstd-compressed multipart is not, so `/info` tells the SDK to use `/runs/batch`.
