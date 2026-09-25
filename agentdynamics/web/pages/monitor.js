// Monitor: overview, flow map, task types, tasks, one task, sessions, workflows.
// Uses the helpers app.js exports on window.AD; see app.js.
(() => {
  const { $, $$, esc, usd, tok, dur, ms, pct, num, dt, ago, dayLabel, pill, scoreColor, statusOfRate, store, toast, F, qs, authHeaders, api, post, showLogin, NAV, ALIAS, renderNav, initFilters, showRefreshed, persist, route, head, kpi, card, typedBy, coverageBlock, spendCaveats, sourceMark, evidence, healthOfApdex, taskTable, bindRows, PHASE_ORDER, phaseParts, SCORE_HELP, PAGES } = window.AD;

  PAGES.overview = async (host, _a, _p, alive) => {
    const d = await api("overview");
    if (!alive()) return;
    if (!d.kpis.tasks) {
      const all = await api("filters", { project: "", days: "", environment: "" }).catch(() => ({}));
      if (!all.task_count) { location.hash = "#/start"; return; }
    }
    if (!d.kpis.tasks) { host.innerHTML = `<div class="card empty">No tasks match the current filters. <a href="#/start">Connect an agent</a> or widen the time range.</div>`; return; }
    const k = d.kpis;
    host.innerHTML = head("Agent Overview", "Every user request is tracked as a <b>task</b>, the agent equivalent of a business transaction. Health is judged against baselines learned from your own history.",
      `<span class="small muted">${k.sessions} sessions</span>`) +
      `<div class="kpis">
        ${kpi("Tasks", num(k.tasks), `${k.sessions} sessions`)}
        ${kpi("Spend", usd(k.cost), `median ${usd(k.median_cost)} / task${spendCaveats(k)}`)}
        ${kpi("Agent Apdex", k.apdex == null ? "–" : k.apdex.toFixed(2), "satisfied + ½ tolerating", " " + pill(healthOfApdex(k.apdex)))}
        ${kpi("Completed cleanly", pct(k.success_rate), `${pct(k.rework_rate)} interrupted or corrected${evidence(k.outcomes_by_source, k.tasks)}`)}
        ${kpi("Tool error rate", pct(k.tool_error_rate, 1), `${num(k.tool_calls)} tool calls`)}
        ${kpi("Verified code changes", pct(k.verification_rate), "tests/build/run after last edit")}
        ${kpi("Avoidable spend", usd(k.waste_cost), "duplicate calls, redundant reads, error loops")}
        ${kpi("Prompt-cache hit", pct(k.cache_hit), `${tok(k.tokens)} tokens processed`)}
      </div>
      <h3 style="margin:4px 0 8px">Agent flow health</h3>
      <div class="kpis">
        ${kpi("Failed runs", pct(k.failed_rate, 1), "ended in error")}
        ${kpi("p95 task latency", dur(k.p95_wall), `avg ${k.avg_steps} steps / task`)}
        ${kpi("Loop rate", pct(k.loop_rate, 1), "a node ran ≥5× in one run")}
        ${kpi("Model errors", num(k.llm_errors), `${num(k.rate_limited)} rate-limited / overloaded`)}
        ${kpi("Truncated outputs", num(k.truncations), `${num(k.refusals)} refusals`)}
        ${kpi("Time to first token", k.ttft_p50 == null ? "–" : ms(k.ttft_p50), "median, where reported")}
        ${kpi("Agent handoffs", num(k.handoffs), "multi-agent transfers")}
        ${kpi("User feedback", k.feedback_avg == null ? "–" : k.feedback_avg.toFixed(2), "avg score where collected")}
      </div>
      <div class="grid g-3-2">
        ${card("Daily spend", `<div id="ch-daily"></div>`, "click a day to see its tasks")}
        ${card("Health by task type", `<div id="types-list"></div>`, `<a href="#/types">all types →</a>`)}
      </div>
      <div class="grid g3" style="margin-top:14px">
        ${card("User experience (Apdex)", `<div id="apdex-mix"></div><h3 style="margin-top:14px">Outcomes</h3><div id="outcome-mix"></div>`)}
        ${card("Where the spend goes", `<div id="phase-mix"></div><p class="small muted" style="margin:10px 0 0">Model cost attributed to the phase of the tool calls each turn issued.</p>`, `<a href="#/process">process review →</a>`)}
        ${card("Spend by model", `<div id="model-bars"></div>`, `<a href="#/models">models →</a>`)}
      </div>
      <div class="grid g2" style="margin-top:14px">
        ${card("Recent health events", d.events.length ? `<table><tbody>${d.events.slice(0, 10).map((e) => `<tr class="click" data-href="#/task/${encodeURIComponent(e.task_id)}"><td style="width:92px">${pill(e.severity)}</td><td><b>${esc(e.rule)}</b><div class="small muted">${esc(e.message)}</div></td><td class="small muted num">${ago(e.ts)}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">No violations 🎉</div>`, `<a href="#/events">all events →</a>`)}
        ${card("Most expensive tasks", taskTable(d.top_tasks.slice(0, 6), { compact: true }), `<a href="#/tasks?sort=cost">all →</a>`)}
      </div>`;
    C.columns($("#ch-daily"), d.daily, { x: "day", keys: [{ key: "cost", label: "Spend", color: "var(--s1)" }], fmt: usd, xfmt: dayLabel, height: 210,
      onClick: (r) => { location.hash = `#/tasks?day=${r.day}`; } });
    $("#types-list").innerHTML = `<table><thead><tr><th>Type</th><th class="num">Tasks</th><th class="num">Spend</th><th class="num">Apdex</th><th>Health</th></tr></thead><tbody>${d.types.map((t) =>
      `<tr class="click" data-href="#/tasks?type=${encodeURIComponent(t.type)}"><td>${esc(t.type)}</td><td class="num">${t.tasks}</td><td class="num">${usd(t.cost)}</td><td class="num">${t.apdex == null ? "–" : t.apdex.toFixed(2)}</td><td>${pill(t.health)}</td></tr>`).join("")}</tbody></table>`;
    C.stack100($("#apdex-mix"), [["satisfied", "var(--good)"], ["tolerating", "var(--warning)"], ["frustrated", "var(--critical)"]].map(([k, c]) => ({ key: k, label: k, value: d.apdex_mix[k] || 0, color: c })));
    const oc = { completed: "var(--good)", "in progress": "var(--s1)", rework: "var(--warning)", interrupted: "var(--critical)", failed: "var(--serious)", unknown: "var(--neutral)" };
    C.stack100($("#outcome-mix"), Object.keys(oc).map((k) => ({ key: k, label: k, value: d.outcomes[k] || 0, color: oc[k] })));
    C.stack100($("#phase-mix"), phaseParts(d.phase_cost), { fmt: usd });
    C.hbars($("#model-bars"), d.models.filter((m) => m.cost > 0).map((m, i) => ({ label: esc(m.model), value: m.cost, color: C.color(i) })), { fmt: usd });
    bindRows(host);
  };

  // ------------------------------------------------------------------ flow map
  PAGES.flow = async (host, _a, p, alive) => {
    const extra = {}; if (p.task) extra.task = p.task; if (p.run) extra.run = p.run;
    const d = await api("flowmap", extra);
    if (!alive()) return;
    host.innerHTML = head("Flow Map", "Live topology of how work flows: from you, through the agent and its subagents, to models and tools. Line thickness is call volume; red means failures." +
      (p.task ? ` <b>Scoped to one task.</b> <a href="#/flow">Show all</a>` : p.run ? ` <b>Scoped to one session.</b> <a href="#/flow">Show all</a>` : "")) +
      `<div class="flow-wrap"><div class="card flow" id="flow"></div><div class="card" id="flow-side"><h2>Details</h2><p class="muted small">Select a node to see its metrics.</p></div></div>`;
    drawFlow($("#flow"), d, $("#flow-side"));
  };

  function drawFlow(host, d, side) {
    const W = Math.max(640, host.clientWidth - 32);
    const nodes = d.nodes;
    const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
    const models = nodes.filter((n) => n.kind === "model").sort((a, b) => b.calls - a.calls);
    let tools = nodes.filter((n) => n.kind === "tool").sort((a, b) => b.calls - a.calls);
    const CAP = 16;
    let edges = d.edges.slice();
    if (tools.length > CAP) {
      const rest = tools.slice(CAP);
      const other = { id: "tool:__other", kind: "tool", label: `${rest.length} other tools`, calls: rest.reduce((s, n) => s + n.calls, 0),
        errors: rest.reduce((s, n) => s + n.errors, 0), cost: rest.reduce((s, n) => s + n.cost, 0), avg_ms: null, members: Object.fromEntries(rest.map((n) => [n.label, n.calls])) };
      other.error_rate = other.calls ? other.errors / other.calls : 0;
      const restIds = new Set(rest.map((n) => n.id));
      const merged = {};
      edges = edges.filter((e) => {
        if (!restIds.has(e.to)) return true;
        const k = e.from; merged[k] = merged[k] || { from: k, to: other.id, calls: 0, errors: 0, avg_ms: null };
        merged[k].calls += e.calls; merged[k].errors += e.errors; return false;
      }).concat(Object.values(merged));
      tools = tools.slice(0, CAP).concat([other]);
      byId[other.id] = other;
    }
    const agents = nodes.filter((n) => n.kind === "agent");
    const user = byId["user"];
    const NW = Math.min(210, W * 0.24), NH = 46, GAP = 12;
    const right = [...models, ...tools];
    const H = Math.max(360, 60 + right.length * (NH + GAP) + (models.length ? 24 : 0));
    const colX = [16, W * 0.3, W - NW - 16];
    const pos = {};
    const cy = Math.min(H / 2, 190);
    pos.user = { x: colX[0], y: cy - NH / 2 };
    agents.forEach((a, i) => { pos[a.id] = { x: colX[1], y: cy - ((agents.length - 1) * (NH + 70)) / 2 + i * (NH + 70) - NH / 2 }; });
    let y = 40;
    models.forEach((m) => { pos[m.id] = { x: colX[2], y }; y += NH + GAP; });
    if (models.length) y += 24;
    const toolTitleY = y - 10;
    tools.forEach((t) => { pos[t.id] = { x: colX[2], y }; y += NH + GAP; });

    const sw = (c) => 1 + Math.log10(c + 1) * 2.2;
    let svg = `<svg viewBox="0 0 ${W} ${H}" height="${H}">`;
    svg += `<text class="col-title" x="${colX[0]}" y="22">Entry</text><text class="col-title" x="${colX[1]}" y="22">Agents</text>`;
    if (models.length) svg += `<text class="col-title" x="${colX[2]}" y="28">Models</text>`;
    if (tools.length) svg += `<text class="col-title" x="${colX[2]}" y="${toolTitleY}">Tools & backends</text>`;
    for (const e of edges) {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) continue;
      const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2;
      const mx = (x1 + x2) / 2;
      const er = e.calls ? e.errors / e.calls : 0;
      const lbl = `${byId[e.from]?.label} → ${byId[e.to]?.label}: ${num(e.calls)} calls${e.avg_ms != null ? " · avg " + ms(e.avg_ms) : ""}${e.errors ? " · " + e.errors + " errors" : ""}`;
      svg += `<path class="edge ${er >= 0.1 ? "err" : ""}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" stroke-width="${sw(e.calls)}"><title>${esc(lbl)}</title></path>`;
      if (e.to.startsWith("agent:") || e.from === "user") svg += `<text class="elabel" x="${(x1 + x2) / 2}" y="${(y1 + y2) / 2 - 6}" text-anchor="middle">${num(e.calls)} ${e.from === "user" ? "requests" : "delegations"}</text>`;
    }
    const all = [user, ...agents, ...right].filter(Boolean);
    for (const n of all) {
      const p = pos[n.id]; if (!p) continue;
      const er = n.error_rate || 0;
      const stripe = n.kind === "user" ? "var(--accent)" : statusOfRate(er);
      let sub = "";
      if (n.kind === "user") sub = `${num(n.calls)} requests`;
      else if (n.kind === "agent") sub = `${num(n.calls)} model turns · ${usd(n.cost)}`;
      else if (n.kind === "model") sub = `${num(n.calls)} calls · ${usd(n.cost)} · ${ms(n.avg_ms)}`;
      else sub = `${num(n.calls)} calls · ${ms(n.avg_ms)}${er ? " · " + pct(er, 1) + " err" : ""}`;
      const label = n.label.length > 26 ? n.label.slice(0, 25) + "…" : n.label;
      svg += `<g class="node" data-id="${esc(n.id)}" style="cursor:pointer"><rect x="${p.x}" y="${p.y}" width="${NW}" height="${NH}" rx="8"/>
        <rect x="${p.x}" y="${p.y}" width="4" height="${NH}" rx="2" style="fill:${stripe};stroke:none"/>
        <text x="${p.x + 12}" y="${p.y + 19}">${esc(label)}</text><text class="sub" x="${p.x + 12}" y="${p.y + 35}">${esc(sub)}</text></g>`;
    }
    svg += `</svg>`;
    host.innerHTML = svg + `<div class="legend"><span><i style="background:var(--good)"></i>&lt;2% errors</span><span><i style="background:var(--warning)"></i>2–10%</span><span><i style="background:var(--critical)"></i>≥10%</span><span class="muted">Hover a line for calls, latency and errors</span></div>`;
    $$(".node", host).forEach((g) => g.addEventListener("click", () => {
      $$(".node", host).forEach((x) => x.classList.remove("sel")); g.classList.add("sel");
      const n = byId[g.dataset.id];
      const rows = [["Calls", num(n.calls)], ["Errors", `${num(n.errors)} (${pct(n.error_rate, 1)})`], ["Avg latency", ms(n.avg_ms)], ["p95 latency", ms(n.p95_ms)],
        [n.kind === "tool" ? "Attributed model cost" : "Cost", usd(n.cost)]];
      if (n.tokens) rows.push(["Tokens", tok(n.tokens)]);
      side.innerHTML = `<h2>${esc(n.label)}</h2><p class="small muted">${esc(n.kind)}</p><table>${rows.map(([a, b]) => `<tr><td class="muted">${a}</td><td class="num">${b}</td></tr>`).join("")}</table>` +
        (n.members && Object.keys(n.members).length > 1 ? `<h3 style="margin-top:14px">Includes</h3><div id="mem"></div>` : "") +
        (n.phases && Object.keys(n.phases).length ? `<h3 style="margin-top:14px">Phase</h3><div id="ph"></div>` : "") +
        (n.kind === "tool" ? `<p style="margin-top:14px"><a href="#/tools">Tool diagnostics →</a></p>` : n.kind === "model" ? `<p style="margin-top:14px"><a href="#/models">Model details →</a></p>` : "");
      if ($("#mem")) C.hbars($("#mem"), Object.entries(n.members).map(([k, v]) => ({ label: esc(k.replace(/^mcp__[^_]+__/, "")), value: v })), { fmt: num });
      if ($("#ph")) C.stack100($("#ph"), Object.entries(n.phases).map(([k, v]) => ({ key: k, label: k, value: v, color: C.phaseColor(k) })));
    }));
  }

  // ------------------------------------------------------------------ task types (business transactions)
  PAGES.types = async (host, _a, _p, alive) => {
    const d = await api("types");
    if (!alive()) return;
    host.innerHTML = head("Task Types", "The agent equivalent of business transactions. Requests are grouped by intent. Each group gets a learned baseline (median and p90 cost/time) that individual tasks are judged against.") +
      card("All task types", `<div class="table-wrap"><table><thead><tr><th>Health</th><th>Type</th><th class="num">Tasks</th><th class="num">Spend</th><th class="num">Baseline cost (p50 / p90)</th>
      <th class="num">Baseline time (p50)</th><th class="num">Apdex</th><th class="num">Clean completion</th><th class="num">Tool errors</th><th class="num">Verified</th><th class="num">Avg score</th><th class="num">Events</th><th>Trend</th></tr></thead><tbody>
      ${d.types.map((t) => `<tr class="click" data-href="#/tasks?type=${encodeURIComponent(t.type)}">
        <td>${pill(t.health)}</td><td><b>${esc(t.type)}</b><div class="small muted">${typedBy(t)}</div></td><td class="num">${t.tasks}</td><td class="num">${usd(t.cost)}</td>
        <td class="num">${t.baseline ? usd(t.baseline.cost_p50) + " / " + usd(t.baseline.cost_p90) : "–"}</td>
        <td class="num">${t.baseline ? dur(t.baseline.duration_p50) : "–"}</td>
        <td class="num">${t.apdex == null ? "–" : t.apdex.toFixed(2)}</td><td class="num">${pct(t.success_rate)}</td><td class="num">${pct(t.tool_error_rate, 1)}</td>
        <td class="num">${pct(t.verification_rate)}</td><td class="num" style="color:${scoreColor(t.avg_score)};font-weight:600">${t.avg_score == null ? "–" : Math.round(t.avg_score)}</td>
        <td class="num">${t.events}</td><td>${C.sparkline(t.daily.map((x) => x.cost))}</td></tr>`).join("")}</tbody></table></div>`) +
      `<p class="small muted">A traced app's type is its workflow name, which is a fact. Coding sessions have none, so their type is guessed from intent keywords in the request (fix/bug → bugfix, create/build → feature, and so on); each type says which it was. Baselines need at least 3 tasks; smaller groups fall back to the global baseline.</p>`;
    bindRows(host);
  };

  // ------------------------------------------------------------------ tasks
  PAGES.tasks = async (host, _a, p, alive) => {
    const [f, d] = await Promise.all([api("filters"), api("tasks", { ...p, day: undefined, sort: p.sort || "recent", limit: p.day ? 1000 : 400 })]);
    if (!alive()) return;
    let tasks = d.tasks;
    if (p.day) tasks = tasks.filter((t) => new Date(t.started * 1000).toLocaleDateString("sv") === p.day);
    const set = (k, v) => { const q = new URLSearchParams(p); if (v) q.set(k, v); else q.delete(k); location.hash = "#/tasks?" + q.toString(); };
    host.innerHTML = head("Tasks", "Each row is one request you made, followed end to end. Open a task for its full snapshot: timeline, token flow, scorecard and what went wrong." + (p.run ? ` <b>Filtered to one session.</b>` : "") + (p.day ? ` <b>Day: ${p.day}</b>` : "")) +
      `<div class="card"><div class="row" style="margin-bottom:10px">
        <select id="t-type"><option value="">All types</option>${f.types.map((t) => `<option ${p.type === t ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>
        <select id="t-outcome"><option value="">All outcomes</option>${["completed", "rework", "interrupted", "failed", "in progress", "unknown"].map((o) => `<option ${p.outcome === o ? "selected" : ""}>${o}</option>`).join("")}</select>
        <select id="t-apdex"><option value="">All Apdex</option>${["satisfied", "tolerating", "frustrated"].map((o) => `<option ${p.apdex === o ? "selected" : ""}>${o}</option>`).join("")}</select>
        <select id="t-flag"><option value="">No flag filter</option><option value="unverified" ${p.flag === "unverified" ? "selected" : ""}>Unverified code changes</option><option value="waste" ${p.flag === "waste" ? "selected" : ""}>Has avoidable work</option></select>
        <input type="text" id="t-q" placeholder="Search requests…" value="${esc(p.q || "")}">
        <label class="small muted">Sort <select id="t-sort">${[["recent", "Most recent"], ["cost", "Highest cost"], ["baseline", "Most over baseline"], ["score", "Lowest score"], ["duration", "Longest"], ["waste", "Most waste"]].map(([v, l]) => `<option value="${v}" ${(p.sort || "recent") === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <span class="spacer"></span><span class="small muted">${tasks.length} tasks · ${usd(tasks.reduce((s, t) => s + t.cost + (t.subagent_cost || 0), 0))}</span>
        ${p.run ? `<a class="btn" href="#/flow?run=${encodeURIComponent(p.run)}">Session flow map</a>` : ""}
      </div>${taskTable(tasks)}</div>`;
    $("#t-type").onchange = (e) => set("type", e.target.value);
    $("#t-outcome").onchange = (e) => set("outcome", e.target.value);
    $("#t-apdex").onchange = (e) => set("apdex", e.target.value);
    $("#t-flag").onchange = (e) => set("flag", e.target.value);
    $("#t-sort").onchange = (e) => set("sort", e.target.value);
    $("#t-q").onkeydown = (e) => { if (e.key === "Enter") set("q", e.target.value); };
    bindRows(host);
  };

  // ------------------------------------------------------------------ task snapshot

  PAGES.task = async (host, id, _p, alive) => {
    const d = await api(`task/${encodeURIComponent(id)}`);
    if (!alive()) return;
    const t = d.task, b = d.baseline || {};
    const steps = d.steps;
    const llm = steps.filter((s) => s.kind === "llm");
    const t0 = Math.min(...steps.map((s) => s.start_ts || s.ts).filter(Boolean));
    const t1 = Math.max(...steps.map((s) => s.end_ts || s.ts).filter(Boolean));
    const span = Math.max(1, t1 - t0);
    const flagsAll = steps.flatMap((s) => (s.flags || []).map((f) => ({ f, s })));
    const scores = t.scores || {};
    const nav = d.nav;
    host.innerHTML = `<div class="between" style="margin-bottom:12px"><div class="small muted"><a href="#/tasks">Tasks</a> / <a href="#/tasks?run=${encodeURIComponent(t.run_id)}">${esc(d.run?.title || t.run_id.slice(0, 12))}</a> / task ${nav.pos} of ${nav.count}</div>
      <div class="row">${nav.prev ? `<a class="btn" href="#/task/${encodeURIComponent(nav.prev)}">← Previous</a>` : ""}${nav.next ? `<a class="btn" href="#/task/${encodeURIComponent(nav.next)}">Next →</a>` : ""}
      <a class="btn" href="#/flow?task=${encodeURIComponent(t.id)}">Flow map</a></div></div>` +
      `<div class="card" style="margin-bottom:14px"><div class="row" style="margin-bottom:8px"><span class="tag">${esc(t.task_type)}</span>${t.framework && t.framework !== "claude-code" ? `<span class="tag">${esc(t.framework)}</span>` : ""}${t.environment && t.environment !== "default" ? `<span class="tag">env: ${esc(t.environment)}</span>` : ""}${t.policy_version ? `<span class="tag">🛡 ${esc(t.policy_version)}</span>` : ""}${t.policy_denials ? `<span class="tag bad">${t.policy_denials} denied</span>` : ""}${t.revocations ? `<span class="tag bad">revoked</span>` : ""}${pill(t.outcome)}${sourceMark(t, true)}${pill(t.apdex)}${t.is_subagent ? `<span class="tag">subagent</span>` : ""}
        <span class="small muted">${dt(t.started)} · ${esc(t.project)} · ${esc(t.models)}</span></div>
        <div class="prompt-box">${esc(t.prompt) || "<span class='muted'>(no prompt)</span>"}</div></div>` +
      `<div class="kpis">
        ${kpi("Cost", usd(t.cost + (t.subagent_cost || 0)), t.subagent_cost ? `${usd(t.subagent_cost)} in ${t.subagents} subagents` : `baseline ${usd(b.cost_p50)}`)}
        ${kpi("vs baseline", t.cost_vs_baseline == null ? "–" : t.cost_vs_baseline.toFixed(1) + "×", `p90 ${usd(b.cost_p90)} for ${esc(t.task_type)}`)}
        ${kpi("Agent time", dur(t.duration_s), `wall clock ${dur(t.wall_s)}`)}
        ${kpi("Model turns", num(t.llm_calls), `${t.parallelism || 0} tools per turn`)}
        ${kpi("Tool calls", num(t.tool_calls), t.tool_errors ? `<span style="color:var(--critical-text)">${t.tool_errors} failed</span>` : "no failures")}
        ${kpi("Tokens", tok(t.total_tokens), `${tok(t.output_tokens)} out · ${tok(t.thinking_tokens)} thinking`)}
        ${kpi("Peak context", tok(t.max_context), `cache hit ${pct(t.cache_hit)}`)}
        ${kpi("Avoidable spend", usd(t.waste_cost), `${t.duplicate_calls} dup calls · ${t.redundant_reads} re-reads`)}
      </div>
      ${(t.path || []).length ? `<div class="card" style="margin-bottom:14px"><div class="between"><h2 style="margin:0">Execution path</h2><span class="small muted">${t.nodes ? `${t.nodes} nodes · max ${t.max_node_visits}× one node` : "process phases"}${t.handoffs ? ` · ${t.handoffs} handoffs` : ""}${t.critical_node ? ` · critical: <b>${esc(t.critical_node)}</b> (${pct(t.critical_share)} of time)` : ""}</span></div>
        <div style="margin-top:8px;line-height:2">${pathChips(t.path, 40)}</div>
        ${t.root_error ? `<div class="wf-detail" style="color:var(--critical-text)">${esc(t.root_error)}</div>` : ""}
        <div class="small muted" style="margin-top:6px">${[t.truncations && `${t.truncations} truncated`, t.refusals && `${t.refusals} refused`, t.rate_limited && `${t.rate_limited} rate-limited`, t.llm_errors && `${t.llm_errors} model errors`, t.empty_retrievals && `${t.empty_retrievals}/${t.retrievals} empty retrievals`, t.ttft_ms && `TTFT ${ms(t.ttft_ms)}`, t.feedback_score != null && `feedback ${t.feedback_score}`, t.hitl && `${t.hitl} human-in-the-loop`].filter(Boolean).join(" · ")}</div></div>` : ""}
      <div class="grid g-2-1">
        ${card("Context growth & spend per turn", `<div id="ch-ctx"></div><div id="ch-cost" style="margin-top:8px"></div>`, "hover for details")}
        ${card("Process scorecard", `<div class="row" style="align-items:baseline;gap:8px"><div class="big-score" style="color:${scoreColor(t.score)}">${t.score == null ? "–" : Math.round(t.score)}</div><span class="muted">/ 100 overall</span></div>
          <div style="margin-top:10px">${Object.keys(SCORE_HELP).map((k) => `<div class="score-row" title="${esc(SCORE_HELP[k])}"><span>${k}</span><div class="track"><div class="fill" style="width:${scores[k] ?? 0}%;background:${scoreColor(scores[k])}"></div></div><span class="num">${scores[k] == null ? "n/a" : Math.round(scores[k])}</span></div>`).join("")}</div>
          <h3 style="margin-top:14px">Spend by phase</h3><div id="task-phase"></div>`)}
      </div>
      <div class="grid g2" style="margin-top:14px">
        ${card("Findings", findingsHtml(t, d.events, flagsAll))}
        ${card("How it ended", `<h3>Agent's final message</h3><div class="prompt-box">${esc(t.final_text) || "<span class='muted'>(no text)</span>"}</div>
          <h3 style="margin-top:12px">Your next message</h3><div class="prompt-box">${t.next_prompt ? esc(t.next_prompt) : "<span class='muted'>(none, end of session)</span>"}</div>
          ${t.rework ? `<p class="small" style="color:var(--warning-text)">Classified as a correction, so this task is marked as rework.</p>` : ""}`)}
      </div>
      ${d.children.length ? `<div style="margin-top:14px">${card(`Subagents (${d.children.length})`, `<table><thead><tr><th>Delegated task</th><th>Outcome</th><th class="num">Cost</th><th class="num">Time</th><th class="num">Tools / errors</th><th class="num">Score</th></tr></thead><tbody>${d.children.map((c) =>
        `<tr class="click" data-href="#/task/${encodeURIComponent(c.id)}"><td><div class="truncate">${esc(c.title?.agent_name || c.prompt)}</div></td><td>${pill(c.outcome)}</td><td class="num">${usd(c.cost)}</td><td class="num">${dur(c.duration_s)}</td><td class="num">${c.tool_calls} / ${c.tool_errors}</td><td class="num" style="color:${scoreColor(c.score)}">${c.score == null ? "–" : Math.round(c.score)}</td></tr>`).join("")}</tbody></table>`)}</div>` : ""}
      <div style="margin-top:14px">${card("Execution timeline", `<div class="legend" style="margin:0 0 8px">${PHASE_ORDER.filter((p) => p !== "respond").map((p) => `<span><i style="background:${C.phaseColor(p)}"></i>${p}</span>`).join("")}<span><i style="background:var(--text-3)"></i>model turn</span></div><div class="wf" id="wf"></div>`, `${steps.length} steps · click a row for details`)}</div>`;

    // context chart
    if (llm.length) {
      const pts = llm.map((s, i) => ({ x: i + 1, y: s.context_tokens || 0, s }));
      C.line($("#ch-ctx"), pts, { fmt: tok, xfmt: (v) => "turn " + v, height: 170, area: true, label: "context", color: "var(--s1)",
        tipExtra: (p) => `<div class="tr"><span>cache read</span><b>${tok(p.s.cache_read)}</b></div><div class="tr"><span>cache write</span><b>${tok(p.s.cache_write)}</b></div><div class="tr"><span>output</span><b>${tok(p.s.output_tokens)}</b></div><div class="tr"><span>cost</span><b>${usd(p.s.cost)}</b></div><div class="tr"><span>latency</span><b>${ms(p.s.duration_ms)}</b></div>` });
      let cum = 0;
      C.line($("#ch-cost"), llm.map((s, i) => ({ x: i + 1, y: (cum += s.cost || 0) })), { fmt: usd, xfmt: (v) => "turn " + v, height: 130, label: "cumulative cost", color: "var(--s2)",
        refLine: b.cost_p50, refLabel: "baseline p50" });
    } else $("#ch-ctx").innerHTML = `<div class="empty">No model calls</div>`;
    C.stack100($("#task-phase"), phaseParts(t.phase_cost), { fmt: usd });

    // waterfall
    const rows = steps.filter((s) => s.kind !== "prompt");
    const wf = $("#wf");
    wf.innerHTML = rows.map((s, i) => {
      const st = (s.start_ts || s.ts || t0) - t0, en = (s.end_ts || s.ts || t0) - t0;
      const left = (100 * st) / span, width = Math.max(0.25, (100 * Math.max(0, en - st)) / span);
      const col = s.denied ? "var(--critical)" : s.is_error ? "var(--critical)" : s.kind === "llm" ? "var(--text-3)" : s.kind === "notice" ? "var(--critical)" : s.kind === "span" ? "var(--neutral)" : C.phaseColor(s.phase);
      const base = s.kind === "llm" ? `model · ${s.model || ""}` : s.kind === "notice" ? `⚠ ${s.name}` : s.kind === "span" ? `▸ ${s.name}` : (s.name || "").replace(/^mcp__/, "");
      const name = (s.depth ? "\u2002".repeat(Math.min(s.depth, 8)) : "") + base + (s.node && s.kind !== "span" ? ` · ${s.node}` : "");
      const flags = (s.flags || []).map((f) => `<span class="tag warn">${f.replace(/_/g, " ")}</span>`).join("");
      const val = s.denied ? `<span style="color:var(--critical-text)">⛔ denied</span>` : s.kind === "llm" ? usd(s.cost) : s.kind === "tool" ? (s.is_error ? `<span style="color:var(--critical-text)">error</span>` : tok((s.output_chars || 0) / 4) + " tok") : "";
      return `<div class="wf-row" data-i="${i}"><span class="muted">${s.seq}</span><span class="wf-name" title="${esc(s.target || "")}">${esc(name)} ${flags}</span>
        <div class="wf-track"><div class="wf-bar" style="left:${left}%;width:${width}%;background:${col}"></div></div><span class="num small">${ms(s.duration_ms)}</span><span class="num small">${val}</span></div>`;
    }).join("");
    $$(".wf-row", wf).forEach((r) => r.addEventListener("click", () => {
      const nx = r.nextElementSibling;
      if (nx && nx.classList.contains("wf-detail")) { nx.remove(); r.classList.remove("sel"); return; }
      const s = rows[+r.dataset.i];
      const det = document.createElement("div"); det.className = "wf-detail";
      const lines = [];
      if (s.kind === "llm") {
        lines.push(`model: ${s.model}   effort: ${s.effort || "–"}   stop: ${s.stop_reason || "–"}`);
        lines.push(`tokens: input ${num(s.input_tokens)} · cache read ${num(s.cache_read)} · cache write ${num(s.cache_write)} · output ${num(s.output_tokens)} (thinking ${num(s.thinking_tokens)})`);
        lines.push(`context: ${num(s.context_tokens)}   cost: ${usd(s.cost)}   latency: ${ms(s.duration_ms)}   tool calls issued: ${s.tool_calls || 0}`);
        if (s.text) lines.push("\n" + s.text);
      } else if (s.kind === "tool") {
        lines.push(`tool: ${s.name}   phase: ${s.phase}   duration: ${ms(s.duration_ms)}   output: ${num(s.output_chars)} chars   attributed cost: ${usd(s.attributed_cost)}`);
        if ((s.flags || []).length) lines.push(`flags: ${s.flags.join(", ")}`);
        if (s.rule) lines.push(`policy: ${s.denied ? "DENIED" : "allowed"} · rule ${s.rule}${s.guard ? " · guard " + s.guard : ""}${s.agent ? " · agent " + s.agent : ""}`);
        lines.push("\ninput: " + (s.input_preview || ""));
        if (s.error) lines.push("\nERROR: " + s.error);
      } else if (s.kind === "span") {
        lines.push(`span: ${s.name}   kind: ${s.span_kind}   node: ${s.node || "–"}   agent: ${s.agent || "–"}   duration: ${ms(s.duration_ms)}`);
        if (s.error) lines.push("ERROR: " + s.error);
        if (s.input_preview) lines.push("\ninput: " + s.input_preview);
        if (s.text) lines.push("\noutput: " + s.text);
      } else lines.push(`${s.name}: ${s.text || ""}`);
      det.textContent = lines.join("\n");
      r.after(det); r.classList.add("sel");
    }));
    bindRows(host);
  };

  function findingsHtml(t, events, flags) {
    const items = [];
    for (const e of events) items.push(`<tr><td style="width:92px">${pill(e.severity)}</td><td><b>${esc(e.rule)}</b><div class="small muted">${esc(e.message)}</div></td></tr>`);
    const fc = {};
    flags.forEach(({ f }) => (fc[f] = (fc[f] || 0) + 1));
    const FL = { denied: "Tool call refused by the Aegis policy (tokens spent generating it are counted as waste)", duplicate_call: "Identical tool call repeated with no edit in between", redundant_read: "File re-read without having changed", error_streak: "Kept retrying after 2+ consecutive failures", large_output: "Tool returned >40k chars into context" };
    for (const f in fc) if (!events.some((e) => e.rule_id === f)) items.push(`<tr><td>${pill("info", "hotspot")}</td><td><b>${fc[f]}× ${f.replace(/_/g, " ")}</b><div class="small muted">${FL[f] || ""}</div></td></tr>`);
    if (t.code_changed && t.verified) items.push(`<tr><td>${pill("ok", "good")}</td><td><b>Verified</b><div class="small muted">Ran tests/build/app after the final edit</div></td></tr>`);
    if (t.churn_file && t.max_edits_one_file >= 5) items.push(`<tr><td>${pill("info", "churn")}</td><td><b>${t.max_edits_one_file} edits to one file</b><div class="small muted mono">${esc(t.churn_file)}</div></td></tr>`);
    return items.length ? `<table><tbody>${items.join("")}</tbody></table>` : `<div class="empty">No issues detected.</div>`;
  }

  // ------------------------------------------------------------------ sessions
  PAGES.sessions = async (host, _a, _p, alive) => {
    const d = await api("sessions");
    if (!alive()) return;
    host.innerHTML = head("Sessions", "One row per agent session (a conversation). Subagent spend is rolled into its parent session.") +
      card(`${d.sessions.length} sessions`, `<div class="table-wrap"><table><thead><tr><th>Started</th><th>Session</th><th>Project</th><th class="num">Tasks</th><th class="num">Spend</th><th class="num">Apdex</th><th class="num">Clean</th><th class="num">Tool errors</th><th class="num">Avg score</th><th class="num">Events</th><th>Models</th></tr></thead><tbody>
      ${d.sessions.map((s) => `<tr class="click" data-href="#/tasks?run=${encodeURIComponent(s.id)}"><td class="small muted" style="white-space:nowrap">${dt(s.started)}</td>
        <td><div class="truncate" style="max-width:340px">${esc(s.title)}</div><div class="small muted mono">${esc(s.id.slice(0, 8))} · ${esc(s.source)}</div></td><td class="small">${esc(s.project)}</td>
        <td class="num">${s.tasks}</td><td class="num">${usd(s.cost)}</td><td class="num">${s.apdex == null ? "–" : s.apdex.toFixed(2)} </td><td class="num">${pct(s.success_rate)}</td>
        <td class="num">${pct(s.tool_error_rate, 1)}</td><td class="num" style="color:${scoreColor(s.avg_score)}">${s.avg_score == null ? "–" : Math.round(s.avg_score)}</td><td class="num">${s.events}</td><td class="small muted">${esc(s.models)}</td></tr>`).join("")}</tbody></table></div>`);
    bindRows(host);
  };

  // ------------------------------------------------------------------ workflows (agent graphs)
  PAGES.workflows = async (host, _a, _p, alive) => {
    const d = await api("workflows");
    if (!alive()) return;
    host.innerHTML = head("Workflows", "Every agent entry point (LangGraph graph, agent, pipeline, or coding-agent task type) with its execution paths. Graph workflows show real nodes. Coding agents without a graph show process phases (explore → edit → verify).") +
      card(`${d.workflows.length} workflows`, `<div class="table-wrap"><table><thead><tr><th>Workflow</th><th>Framework</th><th class="num">Runs</th><th class="num">Success</th><th class="num">p50 / p95 latency</th>
        <th class="num">Avg cost</th><th class="num">Avg steps</th><th class="num">Path variants</th><th class="num">Loop rate</th><th class="num">Apdex</th><th>Most common path</th></tr></thead><tbody>
        ${d.workflows.map((w) => `<tr class="click" data-href="#/workflow/${encodeURIComponent(w.workflow)}"><td><b>${esc(w.workflow)}</b><div class="small muted">${esc(w.projects.join(", "))}</div></td>
          <td><span class="tag">${esc(w.framework)}</span></td><td class="num">${num(w.runs)}</td>
          <td class="num" style="color:${w.success_rate < 0.9 ? "var(--critical-text)" : "inherit"}">${pct(w.success_rate)}</td>
          <td class="num">${dur(w.p50_s)} / ${dur(w.p95_s)}</td><td class="num">${usd(w.avg_cost)}</td><td class="num">${w.avg_steps}</td><td class="num">${w.variants}</td>
          <td class="num" style="color:${w.loop_rate > 0.05 ? "var(--warning-text)" : "inherit"}">${pct(w.loop_rate)}</td><td class="num">${w.apdex == null ? "–" : w.apdex.toFixed(2)}</td>
          <td class="small">${pathChips(w.top_path, 6)}</td></tr>`).join("")}</tbody></table></div>`);
    bindRows(host);
  };

  function pathChips(path, max = 12) {
    if (!path || !path.length) return `<span class="muted">–</span>`;
    const p = path.length > max ? [...path.slice(0, max - 1), `… +${path.length - max + 1}`] : path;
    return p.map((n) => `<span class="tag">${esc(n)}</span>`).join(`<span class="muted">›</span>`);
  }

  PAGES.workflow = async (host, name, _p, alive) => {
    const d = await api(`workflow?name=${encodeURIComponent(name)}`);
    if (!alive()) return;
    if (!d || !d.summary) { host.innerHTML = `<div class="card empty">Workflow not found in the current filter.</div>`; return; }
    const s = d.summary;
    host.innerHTML = `<div class="small muted" style="margin-bottom:8px"><a href="#/workflows">Workflows</a> / ${esc(name)}</div>` +
      head(esc(name), d.graph ? "Directly-follows graph mined from every run: boxes are graph nodes or agents, arrows are observed transitions. Red arrows lead to runs that failed, were interrupted or had to be corrected." :
        "This workflow has no explicit graph, so the map shows process phases of tool calls (explore → edit → verify …).",
        `<a class="btn" href="#/tasks?type=${encodeURIComponent(name)}">All runs</a>`) +
      `<div class="kpis">${kpi("Runs", num(s.runs))}${kpi("Success", pct(s.success_rate), `${s.failed} failed`)}${kpi("p50 / p95 latency", `${dur(s.p50_s)}`, `p95 ${dur(s.p95_s)}`)}
        ${kpi("Avg cost / run", usd(s.avg_cost), `${usd(s.cost)} total`)}${kpi("Avg steps", s.avg_steps)}${kpi("Path variants", num(d.variant_count))}
        ${kpi("Loop rate", pct(s.loop_rate), "runs with a node ≥5 times")}${kpi("Apdex", s.apdex == null ? "–" : s.apdex.toFixed(2))}</div>` +
      card(d.graph ? "Execution graph" : "Process graph", `<div id="wf-graph" class="flow"></div>`, "edge width = transitions · red = transitions that fail far more often than average") +
      `<div class="grid g2" style="margin-top:14px">
        ${card(d.graph ? "Nodes" : "Phases", `<div class="table-wrap"><table><thead><tr><th>${d.graph ? "Node" : "Phase"}</th><th class="num">Executions</th><th class="num">Per run</th><th class="num">p50</th><th class="num">p95</th>
          <th class="num">Errors</th><th class="num">LLM calls</th><th class="num">Tool calls</th><th class="num">Cost share</th></tr></thead><tbody>
          ${d.nodes.map((n) => `<tr><td><b>${esc(n.node)}</b></td><td class="num">${num(n.executions)}</td><td class="num">${n.per_run}</td><td class="num">${ms(n.p50_ms)}</td><td class="num">${ms(n.p95_ms)}</td>
            <td class="num" style="color:${n.errors ? "var(--critical-text)" : "inherit"}">${n.errors} ${n.errors ? `(${pct(n.error_rate, 1)})` : ""}</td><td class="num">${num(n.llm_calls)}</td><td class="num">${num(n.tool_calls)}</td>
            <td><div class="hbar" style="grid-template-columns:1fr 44px;margin:0"><div class="track"><div class="fill" style="width:${100 * n.cost_share}%"></div></div><div class="v">${pct(n.cost_share)}</div></div></td></tr>`).join("")}</tbody></table></div>`)}
        ${card("Path variants", `<div class="table-wrap"><table><thead><tr><th>Path</th><th class="num">Runs</th><th class="num">Success</th><th class="num">Avg cost</th></tr></thead><tbody>
          ${d.variants.map((v) => `<tr class="click" data-href="#/task/${encodeURIComponent(v.example)}"><td>${pathChips(v.path)}</td><td class="num">${v.runs} <span class="muted small">${pct(v.share)}</span></td>
            <td class="num" style="color:${v.success_rate < 0.8 ? "var(--critical-text)" : "inherit"}">${pct(v.success_rate)}</td><td class="num">${usd(v.avg_cost)}</td></tr>`).join("")}</tbody></table></div>`, "click to open an example run")}
      </div>
      <div class="grid g3" style="margin-top:14px">
        ${card("Loop depth", `<div id="wf-loops"></div><p class="small muted" style="margin:6px 0 0">Max executions of a single node per run.</p>`)}
        ${card("Critical node", `<div id="wf-crit"></div><p class="small muted" style="margin:6px 0 0">Node that took the most time, per run.</p>`)}
        ${card("Agent handoffs", d.handoffs.length ? `<table><tbody>${d.handoffs.map((h) => `<tr><td>${esc(h.from)} → ${esc(h.to)}</td><td class="num">${h.count}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">Single-agent workflow</div>`)}
      </div>
      ${d.failures.length ? `<div style="margin-top:14px">${card("Recent failures", taskTable(d.failures, { compact: true }))}</div>` : ""}`;
    C.columns($("#wf-loops"), Object.entries(d.loops).map(([k, v]) => ({ k: k === "10" ? "10+" : k, v })), { x: "k", keys: [{ key: "v", label: "runs", color: "var(--s1)" }], fmt: num, height: 150 });
    C.hbars($("#wf-crit"), d.critical.map((c) => ({ label: esc(c.node), value: c.runs, color: "var(--s2)" })), { fmt: num });
    drawDFG($("#wf-graph"), d);
    bindRows(host);
  };

  function drawDFG(host, d) {
    const W = Math.max(640, host.clientWidth - 32);
    const nodeStats = Object.fromEntries(d.nodes.map((n) => [n.node, n]));
    // rank = mean position of first occurrence along variant paths (process-mining style layout)
    const posSum = {}, posN = {};
    for (const v of d.variants) v.path.forEach((n, i) => { if (!(n in posSum) || true) { posSum[n] = (posSum[n] || 0) + i * v.runs; posN[n] = (posN[n] || 0) + v.runs; } });
    const names = [...new Set(d.edges.flatMap((e) => [e.from, e.to]))];
    const rank = {};
    for (const n of names) rank[n] = n === "__start__" ? -1 : n === "__end__" ? 1e9 : posN[n] ? posSum[n] / posN[n] : 0;
    const order = names.sort((a, b) => rank[a] - rank[b]);
    const cols = [];
    for (const n of order) {
      const r = n === "__start__" ? -1 : n === "__end__" ? 999 : Math.round(rank[n]);
      let c = cols.find((x) => x.r === r); if (!c) cols.push((c = { r, items: [] })); c.items.push(n);
    }
    cols.sort((a, b) => a.r - b.r);
    const NW = Math.min(170, (W - 40) / Math.max(1, cols.length) - 36), NH = 52, GX = (W - 32 - NW) / Math.max(1, cols.length - 1);
    const maxRows = Math.max(...cols.map((c) => c.items.length));
    const H = Math.max(220, maxRows * (NH + 26) + 110);
    const pos = {};
    cols.forEach((c, ci) => c.items.forEach((n, ri) => { pos[n] = { x: 16 + ci * GX, y: 40 + (H - 90) / 2 - (c.items.length * (NH + 26)) / 2 + ri * (NH + 26) }; }));
    const maxC = Math.max(...d.edges.map((e) => e.count), 1);
    // an edge is "hot" when runs through it fail clearly more often than the workflow overall
    const base = 1 - (d.summary.success_rate ?? 1);
    let svg = `<svg viewBox="0 0 ${W} ${H}" height="${H}"><defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--neutral)"/></marker>
      <marker id="arr-r" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--critical)"/></marker></defs>`;
    for (const e of d.edges) {
      const a = pos[e.from], b = pos[e.to]; if (!a || !b) continue;
      const w = 1 + 5 * Math.sqrt(e.count / maxC);
      const bad = e.count >= 2 && e.fail / e.count >= Math.min(0.9, base + 0.2);
      const tip = `${e.from} → ${e.to}: ${e.count} transitions, ${e.fail} in failing runs`;
      let path, lx, ly;
      if (b.x > a.x) {
        const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x - 2, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
        path = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`; lx = mx; ly = (y1 + y2) / 2 - 5;
      } else if (a === b) {
        path = `M${a.x + NW - 20},${a.y} C${a.x + NW},${a.y - 40} ${a.x + NW + 30},${a.y + 10} ${a.x + NW},${a.y + 18}`; lx = a.x + NW + 18; ly = a.y - 18;
      } else { // back edge (loop): arc below
        const x1 = a.x + NW / 2, x2 = b.x + NW / 2, y = Math.max(a.y, b.y) + NH;
        const dip = y + 30 + Math.abs(x1 - x2) * 0.12;
        path = `M${x1},${a.y + NH} C${x1},${dip} ${x2},${dip} ${x2},${b.y + NH + 2}`; lx = (x1 + x2) / 2; ly = dip - 4;
      }
      svg += `<path class="edge ${bad ? "err" : ""}" d="${path}" stroke-width="${w}" marker-end="url(#${bad ? "arr-r" : "arr"})"><title>${esc(tip)}</title></path>
        <text class="elabel" x="${lx}" y="${ly}" text-anchor="middle">${e.count}</text>`;
    }
    for (const n of order) {
      const p = pos[n]; const st = nodeStats[n];
      const term = n === "__start__" || n === "__end__";
      const er = st ? st.errors / Math.max(1, st.executions + st.llm_calls) : 0;
      const sub = term ? "" : st ? `${num(st.executions)}× · ${ms(st.p50_ms)}${st.errors ? ` · ${st.errors} err` : ""}` : "";
      svg += `<g class="node"><rect x="${p.x}" y="${p.y}" width="${NW}" height="${NH}" rx="${term ? NH / 2 : 8}" style="${term ? "fill:var(--surface-2)" : ""}"/>
        ${term ? "" : `<rect x="${p.x}" y="${p.y}" width="4" height="${NH}" rx="2" style="fill:${statusOfRate(er)};stroke:none"/>`}
        <text x="${p.x + (term ? NW / 2 : 12)}" y="${p.y + (term ? NH / 2 + 4 : 21)}" ${term ? 'text-anchor="middle"' : ""}>${esc(term ? (n === "__start__" ? "start" : "end") : n.length > 22 ? n.slice(0, 21) + "…" : n)}</text>
        ${sub ? `<text class="sub" x="${p.x + 12}" y="${p.y + 38}">${esc(sub)}</text>` : ""}</g>`;
    }
    host.innerHTML = svg + "</svg>";
  }

})();
