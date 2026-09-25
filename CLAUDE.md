# CLAUDE.md

Context for Claude Code working in this repo. Read this before changing anything under `agentdynamics/`.

## What this is

APM for AI agents. Telemetry from many sources is normalized into one run/step model, analyzed into tasks
(business transactions), baselines, scores and health events, and served by a zero-dependency web console.

- `agentdynamics/collectors/`: sources, each normalizing into runs. Claude Code transcripts, OTLP
  (JSON + protobuf), LangSmith (receiver + pull), Langfuse (pull), inbox/log files, the generic SDK format, and
  Aegis audit logs.
- `agentdynamics/analysis.py`: tasks, metrics, flow metrics, governance metrics, scores, baselines, health rules.
- `agentdynamics/engine.py`: ingestion pipeline and incremental refresh.
- `agentdynamics/server.py`: HTTP API, receivers and auth. `agentdynamics/web/`: the console (vanilla JS).
- `agentdynamics/autotrace.py`: the in-process SDK (`init`, `trace`, `span`, `tool`, `llm_call`).
- `agentdynamics/integrations/aegis.py` + `govern.py`: the Aegis bridge (decisions, spend gating, watchdog,
  policy export).

## Commands

```bash
pip install -e ".[test,toml]"
pip install "aegis-kernel>=0.4.0"           # for the governance tests
python -m unittest discover tests -v        # 4 skip without the optional ecosystem/live libs; UI tests need Chrome/Edge
ruff check agentdynamics tests examples --select F,E9

# latest-ecosystem run (what the `ecosystem` CI job does):
pip install -U langgraph langchain-core langsmith opentelemetry-sdk opentelemetry-exporter-otlp-proto-http \
  openinference-instrumentation-langchain anthropic openai

# see it the way a user does: a demo instance with synthetic traffic
python -m agentdynamics --data .demo-data --claude-root "" serve --port 8790 &
python examples/langgraph_style_app.py 60               # LangGraph via the LangSmith SDK
python examples/otel_multiagent.py 60 http://127.0.0.1:8790
AGENTDYNAMICS_URL=http://127.0.0.1:8790 python examples/governed_agent.py 45   # Aegis
```

## Invariants: do not break these

1. **Zero runtime dependencies.** `agentdynamics/` imports only the standard library. Optional integrations
   (zstandard, aegis, anthropic, openai, langchain, opentelemetry) are imported lazily and degrade with a
   warning. `dependencies = []` in pyproject is a feature.
2. **Telemetry never breaks the agent.** SDK code runs hooks and sends from a background thread with a bounded
   queue. Hooks are wrapped, failures warn once and are dropped. The only exception is by design: an Aegis gate
   may raise `PolicyViolation` to block a model call. That is enforcement, not telemetry.
3. **Sources are the system of record; the DB is derived.** `runs/steps/tasks/events/baselines/meta` can be
   rebuilt from `spans_raw`, transcript files and `runs/*.json`. Changing a derived schema means bumping
   `store.SCHEMA_VERSION`, which drops and rebuilds derived tables. Never put data only in a derived table.
4. **Ingestion is idempotent and order-independent.** Upserts are keyed by span/run id. LangSmith PATCHes, late
   child spans and feedback before its run must all merge. Re-pulling a window must not double count.
5. **One canonical model.** Collectors map to canonical spans (`collectors/spans.py`) or the generic run
   format (`collectors/generic.py`). Analysis never sees vendor formats.
6. **Nothing here can loosen Aegis.** The integration only adds correlation data, reserves budget and revokes.
   `govern.synthesize` only ever tightens a base policy, and tests verify it with `aegis ratify` and
   `aegis drift` (no widening).
7. **Health-rule ids and API field names are a contract** for saved `rules.json`, alert consumers and
   Prometheus labels. Add new ones; don't rename.
8. **The console works offline.** No CDN assets, all charts are inline SVG. Keep it that way for air-gapped
   installs.

## Working agreements

- **Test against the real library, not a reconstruction.** New integrations get a test in
  `tests/test_ecosystem.py` that runs the actual SDK at whatever version is installed, and the nightly
  `ecosystem` job runs it on the latest releases. Hand-built fixtures prove parsing; only the real SDK proves
  compatibility.
- **Verify in the demo console.** Several real bugs (watchdog payload variation, the Aegis audit-file bug) only
  appeared when the demo traffic was viewed as a user would see it.
- Tests that set `os.environ` must restore it (`addCleanup`); later tests spawn subprocesses that inherit it.
- **Check that a new test can fail.** Reintroduce the bug and watch it go red before trusting it green.
  The first version of the grants-used UI test passed against the broken code.
- UI tests run `agentdynamics serve` as a subprocess. On a thread inside the test process the page
  raced Chrome's DOM dump and was captured mid-boot, intermittently. Fixture timestamps must be
  recent: the console defaults to the last 30 days, and an empty window looks like a failed load.
- Optional-dependency checks: `importlib.util.find_spec("a.b")` raises when `a` is missing. Use the `has()`
  helper in `test_ecosystem.py`.
- Prices come from `pricing.py` (list prices) plus `<data>/pricing.json`. Unknown models are reported as
  *unpriced*, never guessed.

## Layout

```
agentdynamics/
  autotrace.py        SDK: init/trace/span/tool/llm_call, Anthropic+OpenAI patches, hook points (_hooks)
  integrations/aegis.py  instrument(): decisions -> steps, ModelSpendGate, Watchdog, policy identity
  govern.py           observed behaviour -> tightened Aegis policy (+ minimal YAML emitter)
  collectors/         claude_code, spans (canonical assembler), otlp, langsmith, langfuse, inbox, generic, aegis_audit
  analysis.py         segment -> task_metrics -> flow_metrics / governance_metrics -> finalize (baselines, scores, events)
  flows.py, slo.py    workflow process mining, SLOs
  engine.py           sources, spans_raw assembly, incremental refresh, alerts, pullers
  store.py            SQLite (WAL); durable vs derived tables
  server.py           API + receivers (/v1/traces, /langsmith/*, /api/ingest*) + auth roles
  web/                index.html, app.js (all pages), charts.js, style.css
tests/                test_core, test_integrations, test_autotrace, test_aegis_integration, test_ecosystem, test_live_anthropic, test_console_ui
examples/             langgraph_style_app, otel_multiagent, governed_agent, demo_agent
deploy/               Dockerfile companion: compose, OTel Collector config, example TOML
```

## Known weak areas, ranked

1. **Scale.** Each refresh rewrites the whole `tasks` table and `finalize()` runs over every run in memory. Fine
   to about 100k tasks. The fix is incremental finalize plus a Postgres/ClickHouse backend behind `store.py`.
2. **Most outcomes are still inferred.** They *can* be graded now (`agentdynamics.outcome`, `/api/outcomes`)
   and every task says which (`outcome_source`), but nothing grades them automatically.
3. **Coding-task typing is still keyword rules.** Traced apps use the workflow name; every task records
   which (`task_type_source`) and the matched word. Follow-ups inherit the previous task's type, so a
   "continue" after a slash command is typed "slash command".
4. **`server.py` and `web/app.js` are large single files.** Split by feature before adding much more.
5. **UI tests are smoke-level.** `tests/test_console_ui.py` renders every page in headless Chrome and
   checks headings plus the governance table cells. It does not click, filter or navigate.
6. **Watchdog enforcement is in-process only.** Server-side events can alert but not revoke.
7. **Cache accounting for undocumented formats is estimated.** `collectors/spans.uncached_input` applies each
   format's documented rule (GenAI semconv, OpenInference, LangChain: inclusive of reads and writes). Formats
   with no citable rule (older `gen_ai.usage.prompt_tokens`, Langfuse) are estimated and counted as
   `tokens_unverified`. Pin a new format's rule only from its own docs, and cite it in `_input_convention`.
