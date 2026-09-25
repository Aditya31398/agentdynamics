// Configure: get started, health rules, integrations, settings.
// Uses the helpers app.js exports on window.AD; see app.js.
(() => {
  const { $, $$, esc, usd, tok, dur, ms, pct, num, dt, ago, dayLabel, pill, scoreColor, statusOfRate, store, toast, F, qs, authHeaders, api, post, showLogin, NAV, ALIAS, renderNav, initFilters, showRefreshed, persist, route, head, kpi, card, typedBy, coverageBlock, spendCaveats, sourceMark, evidence, healthOfApdex, taskTable, bindRows, PHASE_ORDER, phaseParts, SCORE_HELP, PAGES } = window.AD;

  // ------------------------------------------------------------------ rules
  PAGES.rules = async (host, _a, _p, alive) => {
    const d = await api("rules");
    if (!alive()) return;
    const rules = d.rules;
    host.innerHTML = head("Health Rules", "Thresholds evaluated against every task. Violations become <a href='#/events'>events</a>. Rules are saved to <code>rules.json</code> in the data directory.", `<button class="primary" id="r-save">Save & re-evaluate</button>`) +
      card("Rules", `<div class="table-wrap"><table><thead><tr><th>On</th><th>Rule</th><th>Metric</th><th>Condition</th><th>Severity</th><th>Message</th></tr></thead><tbody>
      ${rules.map((r, i) => `<tr class="rule-row"><td><input type="checkbox" data-i="${i}" data-k="enabled" ${r.enabled !== false ? "checked" : ""}></td><td><b>${esc(r.name)}</b></td><td class="mono">${esc(r.metric)}</td>
        <td class="row" style="flex-wrap:nowrap"><span class="mono">${esc(r.op)}</span><input type="number" step="any" data-i="${i}" data-k="value" value="${r.value}"></td>
        <td><select data-i="${i}" data-k="severity">${["critical", "warning", "info"].map((s) => `<option ${s === r.severity ? "selected" : ""}>${s}</option>`).join("")}</select></td><td class="small muted">${esc(r.message)}</td></tr>`).join("")}</tbody></table></div>`) +
      `<div class="card" style="margin-top:14px"><h2>Metrics available to rules</h2><p class="small muted">cost_vs_baseline, duration_vs_baseline, tool_error_rate, max_error_streak, duplicate_calls, redundant_reads, unverified_edits, max_context, compactions, interrupts, rework, cache_hit, api_errors, cost, duration_s, tool_calls, score, waste_cost, max_edits_one_file. Edit <code>rules.json</code> to add rules with a <code>guard</code> (minimum values required before the rule applies).</p></div>`;
    $("#r-save").onclick = async () => {
      $$("[data-k]", host).forEach((inp) => { const r = rules[+inp.dataset.i]; const k = inp.dataset.k; r[k] = k === "enabled" ? inp.checked : k === "value" ? +inp.value : inp.value; });
      try { await post("rules", { rules }); toast("Rules saved, events re-evaluated"); } catch (e) { toast("Save failed: " + e.message); }
    };
  };

  // ------------------------------------------------------------------ get started
  const STACKS = [
    ["python", "Python agent", "Anthropic / OpenAI SDK, custom loops"],
    ["langgraph", "LangGraph / LangChain", "Python or JS, env vars only"],
    ["otel", "OpenTelemetry", "OpenAI Agents SDK, CrewAI, LlamaIndex, any language"],
    ["langsmith", "Already on LangSmith", "pull runs, change nothing"],
    ["langfuse", "Already on Langfuse", "pull traces, change nothing"],
    ["logs", "Log pipeline", "Fluent Bit, Vector, files"],
    ["http", "Any language", "one HTTP call"],
    ["aegis", "Aegis-governed agent", "enforce + observe"],
    ["claude-code", "Claude Code", "automatic"],
  ];
  let startPoll = null;

  PAGES.start = async (host, _a, p, alive) => {
    const d = await api(`connect?url=${encodeURIComponent(location.origin)}`);
    if (!alive()) return;
    const pick = p.stack || store.get("stack", "python");
    const snip = d.snippets.find((s) => s.id === pick) || d.snippets[0];
    host.innerHTML = head("Get started", "Connect an agent in under two minutes. Pick how your agent is built, paste the snippet, run it, and this page lights up when the first trace arrives.") +
      `<div class="card" style="margin-bottom:14px"><h2>1 · How is your agent built?</h2><div class="stack-grid">${STACKS.map(([id, label, sub]) =>
        `<button class="stack ${id === pick ? "on" : ""}" data-id="${id}"><b>${label}</b><span>${sub}</span></button>`).join("")}</div></div>
      <div class="card" style="margin-bottom:14px"><div class="between"><h2 style="margin:0">2 · Add this</h2><button id="copy">Copy</button></div>
        <p class="small muted" style="margin:6px 0 10px">${esc(snip.title)}${d.auth ? " · Auth is on: create an ingest key on the server with <code>agentdynamics keys create --role ingest</code>." : ""}</p>
        <div class="code-block" id="snip">${esc(snip.body)}</div></div>
      <div class="card"><h2>3 · Run your agent</h2><div id="live" class="live"><span class="spinner"></span> Waiting for the first trace…</div>
        <p class="small muted" style="margin:10px 0 0">Nothing showing up? Run <code>agentdynamics doctor</code> from the machine running your agent. It checks the URL and key, then sends a test trace.</p></div>`;
    $$(".stack", host).forEach((b) => b.onclick = () => { store.set("stack", b.dataset.id); location.hash = `#/start?stack=${b.dataset.id}`; });
    $("#copy").onclick = async () => { try { await navigator.clipboard.writeText(snip.body); toast("Copied"); } catch { toast("Select the text and copy it"); } };
    const baseline = await api("sources").then((s) => s.sources.reduce((n, x) => n + (x.items || 0) + (x.traces || 0), 0)).catch(() => 0);
    clearInterval(startPoll);
    startPoll = setInterval(async () => {
      if (!alive() || !$("#live")) { clearInterval(startPoll); return; }
      const s = await api("sources").catch(() => null);
      if (!s) return;
      const total = s.sources.reduce((n, x) => n + (x.items || 0) + (x.traces || 0), 0);
      const src = s.sources.filter((x) => x.last_data && Date.now() / 1000 - x.last_data < 600 || x.last_ok && Date.now() / 1000 - x.last_ok < 600 && x.items);
      if (total > baseline || src.length) {
        $("#live").classList.add("ok");
        $("#live").innerHTML = `✅ Receiving data from <b>${src.map((x) => esc(x.name)).join(", ") || "your agent"}</b>. <a href="#/overview">Open the overview →</a> or <a href="#/workflows">see your workflows →</a>`;
        clearInterval(startPoll);
      }
    }, 3000);
  };

  // ------------------------------------------------------------------ integrations
  PAGES.integrations = async (host, _a, _p, alive) => {
    const d = await api("sources");
    if (!alive()) return;
    const o = location.origin;
    const snip = (title, body, note = "") => `<details class="card" style="margin-bottom:10px"><summary><b>${title}</b> ${note ? `<span class="muted small">— ${note}</span>` : ""}</summary><div class="code-block" style="margin-top:10px">${esc(body)}</div></details>`;
    host.innerHTML = head("Integrations", "Where telemetry comes from and how to connect more. Every path lands in the same durable span store, so a LangGraph graph traced through LangSmith, an OpenAI Agents SDK app traced with OpenTelemetry, and Claude Code sessions are all analyzed the same way.") +
      card("Source status", `<div class="table-wrap"><table><thead><tr><th>Source</th><th>Type</th><th>Status</th><th class="num">Items</th><th class="num">Traces</th><th>Last data</th><th>Detail / last error</th></tr></thead><tbody>
        ${d.sources.map((s) => `<tr><td><b>${esc(s.name)}</b></td><td><span class="tag">${esc(s.type)}</span></td><td>${pill(s.status === "ok" ? "ok" : s.status === "error" ? "critical" : "unknown", s.status)}</td>
          <td class="num">${num(s.items)}</td><td class="num">${s.traces == null ? "–" : num(s.traces)}</td><td class="small muted">${s.last_data ? ago(s.last_data) : s.last_ok ? ago(s.last_ok) : "never"}</td>
          <td class="small ${s.last_error ? "" : "muted"}" style="${s.last_error ? "color:var(--critical-text)" : ""}">${esc(s.last_error || s.detail)}</td></tr>`).join("")}</tbody></table></div>
        <p class="small muted" style="margin:8px 0 0">Auth ${d.auth ? "<b>on</b>: push clients need an <i>ingest</i> key" : "<b>off</b> (local mode): set keys in agentdynamics.toml before exposing this server"} · ${num(d.stats.spans_ingested)} spans ingested since start · ${d.stats.refreshes} analysis cycles.</p>`) +
      `<h2 style="margin:20px 0 10px">Connect a source</h2>
      <div class="grid g2"><div>
      ${snip("LangChain / LangGraph (zero code)", `# Existing LangSmith tracing is redirected to AgentDynamics. No code changes.
export LANGSMITH_TRACING=true
export LANGSMITH_ENDPOINT=${o}/langsmith
export LANGSMITH_API_KEY=<AgentDynamics ingest key>
export LANGSMITH_PROJECT=my-agent-prod

# To keep sending to LangSmith as well, leave LangSmith as the endpoint and add
# a "langsmith_api" pull source below instead.`, "LangSmith-compatible receiver")}
      ${snip("OpenTelemetry: any framework via OpenInference", `pip install openinference-instrumentation-langchain opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
# also: -crewai, -llama-index, -openai-agents, -anthropic, -bedrock, -dspy, -autogen ...

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from openinference.instrumentation.langchain import LangChainInstrumentor

provider = TracerProvider(resource=Resource.create({
    "service.name": "support-agent", "deployment.environment": "production"}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
    endpoint="${o}/v1/traces", headers={"Authorization": "Bearer <ingest key>"})))
trace.set_tracer_provider(provider)
LangChainInstrumentor().instrument()`, "OTLP/HTTP protobuf or JSON at /v1/traces")}
      ${snip("OpenTelemetry Collector (fan-in from many services)", `receivers:
  otlp:
    protocols: { grpc: {}, http: {} }
processors:
  batch: {}
  filter/genai:          # forward only agent spans
    traces:
      span:
        - 'attributes["gen_ai.operation.name"] == nil and attributes["openinference.span.kind"] == nil and attributes["traceloop.span.kind"] == nil'
exporters:
  otlphttp/agentdynamics:
    endpoint: ${o}
    headers: { Authorization: "Bearer \${env:AGENTDYNAMICS_INGEST_KEY}" }
service:
  pipelines:
    traces/agents:
      receivers: [otlp]
      processors: [filter/genai, batch]
      exporters: [otlphttp/agentdynamics]   # add your existing APM exporter alongside`, "recommended for production")}
      ${snip("OpenAI Agents SDK / Strands / Semantic Kernel (GenAI semconv)", `# Frameworks that emit OpenTelemetry GenAI semantic conventions (gen_ai.*) work as-is:
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=${o}/v1/traces
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Bearer <ingest key>"
export OTEL_SERVICE_NAME=helpdesk-agents
export OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=production`)}
      </div><div>
      ${snip("Pull from LangSmith or Langfuse (keep your current tracer)", `# <data>/agentdynamics.toml
[[sources]]
type = "langsmith_api"
project = "my-agent-prod"
api_key_env = "LANGSMITH_API_KEY"   # read from the environment, never stored
interval = 60

[[sources]]
type = "langfuse_api"
host = "https://cloud.langfuse.com"
public_key_env = "LANGFUSE_PUBLIC_KEY"
secret_key_env = "LANGFUSE_SECRET_KEY"`, "incremental, idempotent")}
      ${snip("Log pipelines: Fluent Bit / Vector / Logstash", `# Agents that log traces as JSON lines can ship them over HTTP. Accepted records:
# OTLP JSON, LangSmith runs, Langfuse traces, canonical spans, AgentDynamics runs.
# Shipper envelopes like {"log": "..."} or {"message": {...}} are unwrapped.

# Fluent Bit
[OUTPUT]
    Name        http
    Match       agent.traces
    Host        agentdynamics.internal
    Port        8787
    URI         /api/ingest/records
    Format      json_lines
    Header      Authorization Bearer <ingest key>

# Vector
[sinks.agentdynamics]
type = "http"
inputs = ["agent_traces"]
uri = "${o}/api/ingest/records"
encoding.codec = "json"
request.headers.Authorization = "Bearer <ingest key>"`, "HTTP NDJSON at /api/ingest/records")}
      ${snip("Files, S3/GCS exports, air-gapped hosts", `# Tail a directory of *.jsonl / *.json (growing files are followed by byte offset,
# rotation is detected). Point an S3 sync job or the OTel Collector 'file' exporter at it.
[[sources]]
type = "inbox"
path = "/var/log/agent-traces"
interval = 5

# One-off import:
python -m agentdynamics push exported_runs.json`)}
      ${snip("Custom agents: Python SDK or raw HTTP", `from agentdynamics.sdk import Tracer
tracer = Tracer(agent="support-bot", endpoint="${o}")
with tracer.task("Refund order #42") as task:
    task.record_anthropic(client.messages.create(...))
    with task.tool("lookup_order", {"id": 42}) as call:
        call.output(lookup_order(42))`)}
      ${snip("Export: Prometheus, Grafana and alert webhooks", `# prometheus.yml
scrape_configs:
  - job_name: agentdynamics
    metrics_path: /metrics
    authorization: { credentials: <read key> }
    static_configs: [{ targets: ["agentdynamics.internal:8787"] }]

# agentdynamics.toml: Slack or any JSON webhook on new health events
[[alerts.webhooks]]
url = "https://hooks.slack.com/services/..."
min_severity = "warning"
format = "slack"`)}
      ${snip("Claude Code", `Read automatically from ~/.claude/projects (sessions, subagents, workflows).
Change with --claude-root, or disable with --claude-root "".`)}
      </div></div>`;
  };

  // ------------------------------------------------------------------ settings
  PAGES.settings = async (host, _a, _p, alive) => {
    const d = await api("config");
    if (!alive()) return;
    const c = d.config;
    const row = (k, v) => `<tr><td class="muted" style="width:220px">${k}</td><td>${v}</td></tr>`;
    host.innerHTML = head("Settings", `Read-only view of the running configuration (${esc(c._path || "defaults: no agentdynamics.toml found")}). Edit the TOML file and restart to change it.`) +
      `<div class="grid g2">
      ${card("Security", `<table>${row("Authentication", c.auth.enabled ? pill("ok", "API keys required") : pill("warning", "off: local mode"))}
        ${row("Keys", c.auth.keys.length ? c.auth.keys.map((k) => `<span class="tag">${esc(k.name)} · ${esc(k.role)} · ${esc(k.key)}</span>`).join(" ") : "<span class='muted'>none</span>")}
        ${row("Roles", "<span class='small'><b>ingest</b> writes telemetry only · <b>read</b> views console, API and /metrics · <b>admin</b> edits rules and SLOs</span>")}</table>`)}
      ${card("Privacy", `<table>${row("Store prompt/tool content", c.privacy.store_content ? "yes" : pill("ok", "no: sizes and metadata only"))}
        ${row("Redaction", c.privacy.redact.map((r) => `<span class="tag">${esc(r)}</span>`).join(" ") + (c.privacy.extra_patterns.length ? ` + ${c.privacy.extra_patterns.length} custom` : ""))}</table>`)}
      ${card("Data", `<table>${row("Retention", c.retention.days ? `${c.retention.days} days` : "unlimited")}${row("Data directory", `<code>${esc(d.data_dir)}</code>`)}${row("Database", `<code>${esc(d.db)}</code>`)}
        ${row("Analysis interval", `${c.analysis.interval}s (push ingestion triggers a debounced run)`)}</table>`)}
      ${card("Alerting", c.alerts.webhooks.length ? `<table>${c.alerts.webhooks.map((w) => row(esc(w.format || "json"), `${esc(w.url)} · ≥ ${esc(w.min_severity || "warning")}`)).join("")}</table>` : `<div class="empty">No webhooks configured. See Integrations.</div>`)}
      </div>`;
  };

})();
