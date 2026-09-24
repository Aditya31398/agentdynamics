# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/). Public contracts: the Python API exported from
`agentdynamics/__init__.py`, the ingestion formats (`/api/ingest`, `/api/ingest/records`, `/v1/traces`,
`/langsmith/*`), health-rule ids, and the REST API field names used by the console. Before 1.0, a breaking change
bumps the minor version.

## [Unreleased]

### Added
- **Graded outcomes** (#1). An outcome can now be stated instead of inferred: `agentdynamics.outcome()`
  (and `trace(...).outcome()`) inside a run, `POST /api/tasks/{id}/outcome` after the fact, or
  `POST /api/outcomes` in bulk for eval pipelines. Every task records `outcome_source` (`graded`,
  `feedback` or `inferred`) and `outcome_reason`, the Overview says how much of the success rate is
  graded versus guessed, and `kpis.outcomes_by_source` carries the counts. Grades are durable: they
  survive schema upgrades, and a grade that arrives before its task applies when the task does.

### Changed
- **Recorded feedback now settles the outcome** rather than being one signal among several. A score
  of 0.5 or more makes a task `completed` even if the run ended in an error; below 0.5 makes it
  `rework`. Success rate, failed-run rate and Apdex can shift for existing data on upgrade: on the
  demo dataset, failed runs moved from 16.0% to 13.8% and Apdex from 0.69 to 0.72.
- `SCHEMA_VERSION` 7. Derived tables rebuild from sources on first start; nothing needs migrating.

### Fixed
- **Prompt-cache writes were charged twice** for traces from OpenTelemetry GenAI, OpenInference and
  LangSmith (#2). All three document their input count as including cache reads *and* cache writes;
  the collector subtracted only the reads, so every cached write was also billed as uncached input.
  Each collector now reads its format's documented rule. Formats with no rule we can cite (the older
  `gen_ai.usage.prompt_tokens` naming, Langfuse) keep the previous estimate but are counted as
  `tokens_unverified` and shown under Spend, the way unpriced models already are. Costs for cached
  traffic from the three documented formats go down on upgrade; that is the correction.

## [0.4.1] - 2026-09-24

Governance fixes found by running a governed agent against the published packages. Two of them
mean an enforcement control was not doing its job, so this is worth taking promptly.

### Changed
- `/healthz` returns `degraded` (not `ok`) while analysis refreshes are failing. Anything alerting
  on the literal string `ok` will now see the difference, which is the point.

### Added
- `policy export` replays the observed traffic against the policy it just generated and refuses
  to stay quiet about calls the candidate would now deny (`regressions` in the API, a warning and
  a non-zero exit in the CLI). A synthesized policy can be strictly narrower than its base,
  constitutional, free of drift, and still refuse everything; `aegis ratify` and `aegis drift`
  compare declarations, so only the recorded calls can catch that.

### Fixed
- The watchdog's `max_repeated_denials` counted refusals of the *same* tool in a row, so an agent
  alternating between two forbidden tools was never revoked however often it was refused -- the
  shape a prompt-injected agent produces on its own ("do X, then confirm by Y"). Consecutive
  refusals are now counted across tools as well, ending at the first call that succeeds. The same
  blind spot in the server-side `repeated_denials` metric (the Governance page's boundary-probing
  count) is fixed too, so that number can rise for existing data.
- Two `Watchdog`s on one run shared their per-run state, so the first to trip silently switched
  off every other one.
- `policy export` turned observed *numeric* arguments into a `one_of` list of those values,
  written as strings. Aegis compares the raw value, so the generated policy denied every call
  including the ones it was built from. Numbers now tighten `max_value` instead, which keeps the
  next legitimate amount working; `one_of` is still inferred for genuinely categorical arguments.
- The Governance page and `policy report` could show more grants used than granted ("6 of 5").
  `used` counted every traced tool in the task, including plain `@tool` functions no kernel
  mediates. It now counts only granted capabilities that were exercised, and tools called outside
  the policy are reported separately as **ungoverned** — a tool nothing mediates is its own finding.
- Removing `<data>/runs` while the server ran raised `FileNotFoundError` out of every subsequent
  refresh, so ingestion stopped for good while `/healthz` kept reporting `ok` from the last
  successful timestamp. The directory is recreated, and a directory that cannot be read is no
  longer treated as "every run in it was deleted" (deleting an individual run file still removes
  that run). `/healthz` reports `degraded` with `failed_refreshes` and `last_refresh_error` when
  refreshes are failing, and `agentdynamics_refresh_failures` is exported to Prometheus.

## [0.4.0] - 2026-09-24

First public release.

### Added
- **Governance with Aegis** (`pip install aegis-kernel`; `agentdynamics.integrations.aegis.instrument`). Aegis decisions become steps in
  their task. Model calls reserve and settle against the Aegis budget, so an exhausted budget blocks the call.
  A `Watchdog` revokes the grant when one tool is refused N times in a row, on loops, and on cost or call caps.
  `agentdynamics policy export|report` and the Governance page generate a tightened, least-privilege policy from
  observed behaviour. Aegis audit JSONL can also be ingested out of process. There are four governance health
  rules, a compliance score, and compare-by-policy-version.
- `agentdynamics.llm_call` / `record_llm` for model clients the SDK doesn't patch.
- LangSmith receiver: zstd-compressed multipart ingestion when `zstandard` is installed. `/info` advertises it
  only then; otherwise the SDK uses `/runs/batch`.
- Ecosystem test suite (`tests/test_ecosystem.py`) running a real LangGraph graph, the OpenTelemetry OTLP
  exporter and the OpenInference LangChain instrumentor, plus a nightly CI job on the latest releases and an
  optional live Anthropic smoke test.
- `agentdynamics --version`, CLAUDE.md, a release workflow with trusted publishing, provenance and a container
  image.

### Fixed
- Probing detection keyed on (tool, rule) missed agents that vary their payload. It is now keyed on the tool.

## [0.3.0] - 2026-09-19

### Added
- One-line integration: `agentdynamics.init()` auto-instruments the Anthropic and OpenAI SDKs (sync, async,
  streaming), LangChain/LangGraph (via the LangSmith endpoint) and OpenTelemetry. `@trace`, `span`, `@tool`.
- `agentdynamics run <cmd>` for zero-code instrumentation, plus `connect`, `doctor` and `keys` commands.
- A Get-started page in the console that detects the first trace live.

## [0.2.0] - 2026-09-19

### Added
- Receivers: OTLP/HTTP (JSON and protobuf, with no dependencies; GenAI semconv, OpenInference, OpenLLMetry), a
  LangSmith-compatible API, and log-pipeline records. Pull connectors for LangSmith and Langfuse. Inbox file
  tailing.
- Agent-flow metrics (paths, loops, critical node, handoffs, TTFT, truncations, rate limits, empty retrievals),
  the Workflows page (process mining), SLOs with error budgets, role-based API keys, redaction, retention,
  alert webhooks, Prometheus `/metrics`, and Docker plus OTel Collector deployment.

## [0.1.0] - 2026-09-19

### Added
- Claude Code transcript analysis: tasks, baselines, Apdex, health rules, flow map, process scorecard, and the
  web console.
