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

## Enterprise features

| Area | What's there |
|---|---|
| Security | API keys with roles: `ingest` writes telemetry only, `read` views console, API and metrics, `admin` edits rules and SLOs. Constant-time key comparison. The LangSmith `x-api-key` header is honored. |
| Privacy | Regex redaction of emails, API keys, bearer tokens, AWS keys and card numbers (plus custom patterns), applied before storage. `store_content = false` keeps only sizes and metadata. |
| Retention | Per-deployment retention window; old spans and runs are purged automatically. |
| Multi-environment | `deployment.environment` / metadata `environment` becomes a first-class filter, alongside project and framework. |
| Operations | `/healthz`, Prometheus `/metrics`, a per-source status page with last error, gzip/deflate request bodies, a 64 MB body limit, and a non-root Docker image with a healthcheck. |
| Alerting | New health events go to Slack or generic JSON webhooks, with deduplication and no replay of history on first start. |
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
