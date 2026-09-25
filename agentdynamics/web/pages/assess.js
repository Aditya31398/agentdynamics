// Assess: process review, analytics, compare, SLOs.
// Uses the helpers app.js exports on window.AD; see app.js.
(() => {
  const { $, $$, esc, usd, tok, dur, ms, pct, num, dt, ago, dayLabel, pill, scoreColor, statusOfRate, store, toast, F, qs, authHeaders, api, post, showLogin, NAV, ALIAS, renderNav, initFilters, showRefreshed, persist, route, head, kpi, card, typedBy, coverageBlock, spendCaveats, sourceMark, evidence, healthOfApdex, taskTable, bindRows, PHASE_ORDER, phaseParts, SCORE_HELP, PAGES } = window.AD;

  // ------------------------------------------------------------------ process review
  PAGES.process = async (host, _a, _p, alive) => {
    const d = await api("process");
    if (!alive()) return;
    const dims = ["efficiency", "focus", "reliability", "verification", "context", "autonomy", "compliance"];
    host.innerHTML = head("Process Review", "How the agent works, not just what it costs. Each task is scored on six process dimensions. These findings are meant to help you judge the agent's habits and fix them (usually with better prompts or a CLAUDE.md).") +
      `<div class="kpis">${kpi("Overall process score", d.avg.overall == null ? "–" : Math.round(d.avg.overall), "average across tasks", "")}${dims.map((k) => kpi(k[0].toUpperCase() + k.slice(1), `<span style="color:${scoreColor(d.avg[k])}">${d.avg[k] == null ? "–" : Math.round(d.avg[k])}</span>`, esc(SCORE_HELP[k]))).join("")}</div>` +
      `<h2 style="margin:18px 0 10px">Findings</h2><div class="grid g3">${d.insights.map((i) => `<div class="insight ${i.severity}"><div class="between"><b>${esc(i.title)}</b>${pill(i.severity === "ok" ? "ok" : i.severity, i.severity)}</div>
        <div class="metric">${esc(i.metric)}</div><div class="small">${esc(i.detail)}</div><div class="advice">💡 ${esc(i.advice)}</div>
        ${i.examples.length ? `<div class="small" style="margin-top:6px">Examples: ${i.examples.map((e, n) => `<a href="#/task/${encodeURIComponent(e)}">#${n + 1}</a>`).join(" · ")}</div>` : ""}</div>`).join("")}</div>` +
      `<div class="grid g2" style="margin-top:14px">
        ${card("Scorecard by task type", `<div class="table-wrap"><table><thead><tr><th>Type</th><th class="num">n</th>${dims.map((k) => `<th class="num" title="${esc(SCORE_HELP[k])}">${k.slice(0, 5)}</th>`).join("")}<th class="num">overall</th></tr></thead><tbody>
          ${d.per_type.map((r) => `<tr class="click" data-href="#/tasks?type=${encodeURIComponent(r.type)}&sort=score"><td>${esc(r.type)}</td><td class="num">${r.n}</td>${[...dims, "overall"].map((k) => `<td class="num" style="color:${scoreColor(r[k])};font-weight:600">${r[k] == null ? "–" : Math.round(r[k])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`)}
        ${card("Phase mix by task type", `<div id="pmix"></div>`, "share of model spend")}
      </div>
      <div style="margin-top:14px">${card("Process score over time", `<div id="ch-score"></div>`)}</div>
      <div style="margin-top:14px">${card("Lowest-scoring tasks: start your review here", taskTable(d.worst, { compact: true }))}</div>`;
    $("#pmix").innerHTML = d.per_type.map((r, i) => `<div style="margin:8px 0"><div class="small" style="margin-bottom:3px">${esc(r.type)} <span class="muted">(${r.n})</span></div><div id="pm-${i}"></div></div>`).join("") +
      `<div class="legend">${PHASE_ORDER.map((p) => `<span><i style="background:${C.phaseColor(p)}"></i>${p}</span>`).join("")}</div>`;
    d.per_type.forEach((r, i) => C.stack100($(`#pm-${i}`), phaseParts(r.phase_mix), { legend: false }));
    const daily = d.daily.filter((x) => x.score);
    if (daily.length) C.line($("#ch-score"), daily.map((x, i) => ({ x: i, y: x.score, day: x.day, n: x.tasks })), { fmt: (v) => Math.round(v), xfmt: (i) => dayLabel(daily[i].day), height: 180, label: "avg score", color: "var(--s3)", tipExtra: (p) => `<div class="tr"><span>tasks</span><b>${p.n}</b></div>` });
    bindRows(host);
  };

  // ------------------------------------------------------------------ analytics
  PAGES.analytics = async (host, _a, p, alive) => {
    const group = p.group || "task_type";
    const metrics = (p.metrics || "tasks,cost,avg_cost,avg_score").split(",");
    const d = await api("analytics", { group, metrics: metrics.join(",") });
    if (!alive()) return;
    const LBL = { tasks: "Tasks", cost: "Spend", avg_cost: "Avg cost", tokens: "Tokens", output_tokens: "Output tokens", avg_duration: "Avg agent time", avg_score: "Avg score", tool_calls: "Tool calls", tool_errors: "Tool errors", error_rate: "Error rate", waste: "Avoidable spend", rework_rate: "Rework rate", verification_rate: "Verification rate", avg_context: "Avg peak context", cache_hit: "Cache hit" };
    const FMT = { cost: usd, avg_cost: usd, waste: usd, tokens: tok, output_tokens: tok, avg_context: tok, avg_duration: dur, error_rate: (v) => pct(v, 1), rework_rate: pct, verification_rate: pct, cache_hit: pct, avg_score: (v) => (v == null ? "–" : Math.round(v)) };
    const f = (m) => FMT[m] || num;
    host.innerHTML = head("Analytics", "Slice any metric by any dimension, the equivalent of AppDynamics Analytics. Respects the global filters above.",
      `<button id="csv">Export CSV</button>`) +
      `<div class="card"><div class="row"><label class="small muted">Group by <select id="a-group">${d.available.groups.map((g) => `<option ${g === group ? "selected" : ""}>${g}</option>`).join("")}</select></label>
        <span class="small muted">Metrics</span>${d.available.metrics.map((m) => `<label class="small"><input type="checkbox" class="a-m" value="${m}" ${metrics.includes(m) ? "checked" : ""}> ${LBL[m] || m}</label>`).join("")}</div></div>
      <div class="card" style="margin-top:14px"><div class="card-head"><h2>${LBL[d.metrics[0]] || d.metrics[0]} by ${esc(group)}</h2></div><div id="a-chart"></div></div>
      <div class="card" style="margin-top:14px"><div class="table-wrap"><table><thead><tr><th>${esc(group)}</th>${d.metrics.map((m) => `<th class="num">${LBL[m] || m}</th>`).join("")}</tr></thead><tbody>
        ${d.rows.map((r) => `<tr><td>${esc(r.grp ?? "(none)")}</td>${d.metrics.map((m) => `<td class="num">${f(m)(r[m])}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div>`;
    const go = () => { const ms_ = $$(".a-m").filter((c) => c.checked).map((c) => c.value); location.hash = `#/analytics?group=${$("#a-group").value}&metrics=${ms_.join(",") || "tasks"}`; };
    $("#a-group").onchange = go; $$(".a-m").forEach((c) => (c.onchange = go));
    const m0 = d.metrics[0];
    const timeLike = ["day", "week", "hour"].includes(group);
    if (timeLike) C.columns($("#a-chart"), d.rows, { x: "grp", keys: [{ key: m0, label: LBL[m0], color: "var(--s1)" }], fmt: f(m0), xfmt: group === "day" ? dayLabel : (v) => v, height: 220 });
    else C.hbars($("#a-chart"), d.rows.slice(0, 25).map((r) => ({ label: esc(r.grp ?? "(none)"), value: +r[m0] || 0 })), { fmt: f(m0) });
    $("#csv").onclick = () => {
      const csv = [[group, ...d.metrics].join(","), ...d.rows.map((r) => [JSON.stringify(r.grp ?? ""), ...d.metrics.map((m) => r[m] ?? "")].join(","))].join("\n");
      const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = `agentdynamics-${group}.csv`; a.click();
    };
  };

  // ------------------------------------------------------------------ compare
  PAGES.compare = async (host, _a, p, alive) => {
    const dim = p.dim || "models";
    const d = await api("compare", { dim, a: p.a, b: p.b });
    if (!alive()) return;
    const opts = d.options;
    const A = p.a || (dim !== "period" ? opts[0] : ""), B = p.b || (dim !== "period" ? opts[1] : "");
    if (dim !== "period" && (!p.a || !p.b) && opts.length >= 2) { location.hash = `#/compare?dim=${dim}&a=${encodeURIComponent(A)}&b=${encodeURIComponent(B)}`; return; }
    const sel = (id, v) => dim === "period" ? `<input type="text" id="${id}" placeholder="2026-09-01..2026-09-10" value="${esc(v || "")}">` :
      `<select id="${id}">${opts.map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    const rowsDef = [["Tasks", "tasks", num, 0], ["Spend", "cost", usd, -1], ["Avg cost / task", "avg_cost", usd, -1], ["Median cost / task", "median_cost", usd, -1], ["Avg agent time", "avg_duration", dur, -1],
      ["Agent Apdex", "apdex", (v) => (v == null ? "–" : v.toFixed(2)), 1], ["Clean completion", "success_rate", pct, 1], ["Rework rate", "rework_rate", pct, -1], ["Tool error rate", "tool_error_rate", (v) => pct(v, 1), -1],
      ["Verification rate", "verification_rate", pct, 1], ["Avoidable spend", "waste_cost", usd, -1], ["Cache hit", "cache_hit", pct, 1], ["Avg process score", "avg_score", (v) => (v == null ? "–" : v.toFixed(1)), 1]];
    const a = d.a, b = d.b;
    host.innerHTML = head("Compare", "Side-by-side comparison of two models, task types, projects or time periods: the agent equivalent of comparing releases. Use it to check whether a model, prompt or CLAUDE.md change helped.") +
      `<div class="card"><div class="row"><label class="small muted">Dimension <select id="c-dim">${["models", "task_type", "workflow", "project", "source", "policy", "period"].map((x) => `<option ${x === dim ? "selected" : ""}>${x}</option>`).join("")}</select></label>
        <label class="small muted">A ${sel("c-a", A)}</label><label class="small muted">B ${sel("c-b", B)}</label><button class="primary" id="c-go">Compare</button></div></div>` +
      (a && b ? `<div class="grid g2" style="margin-top:14px">${card("Key metrics", `<table><thead><tr><th>Metric</th><th class="num">A</th><th class="num">B</th><th class="num">B vs A</th></tr></thead><tbody>${rowsDef.map(([l, k, f, dir]) => {
        const va = a[k], vb = b[k];
        let delta = "–", col = "inherit";
        if (va != null && vb != null && va !== 0) { const r = (vb - va) / Math.abs(va); delta = (r > 0 ? "+" : "") + (r * 100).toFixed(0) + "%"; if (dir && Math.abs(r) > 0.05) col = r * dir > 0 ? "var(--good-text)" : "var(--critical-text)"; }
        return `<tr><td>${l}</td><td class="num">${f(va)}</td><td class="num">${f(vb)}</td><td class="num" style="color:${col};font-weight:600">${delta}</td></tr>`; }).join("")}</tbody></table>`, "green = better")}
        ${card("Process profile", `<h3>A: ${esc(A)}</h3><div id="c-pa"></div><h3 style="margin-top:12px">B: ${esc(B)}</h3><div id="c-pb"></div>
          <h3 style="margin-top:16px">Scores (A vs B)</h3>${["efficiency", "focus", "reliability", "verification", "context", "autonomy"].map((k) => `<div class="score-row"><span>${k}</span><div><div class="track" style="margin-bottom:2px"><div class="fill" style="width:${a.scores[k] || 0}%;background:var(--s1)"></div></div><div class="track"><div class="fill" style="width:${b.scores[k] || 0}%;background:var(--s2)"></div></div></div><span class="num small">${a.scores[k] == null ? "–" : Math.round(a.scores[k])}/${b.scores[k] == null ? "–" : Math.round(b.scores[k])}</span></div>`).join("")}
          <div class="legend"><span><i style="background:var(--s1)"></i>A</span><span><i style="background:var(--s2)"></i>B</span></div>`)}</div>` : `<div class="empty">Pick two values to compare.</div>`);
    if (a && b) { C.stack100($("#c-pa"), phaseParts(a.phase_mix)); C.stack100($("#c-pb"), phaseParts(b.phase_mix)); }
    $("#c-dim").onchange = (e) => (location.hash = `#/compare?dim=${e.target.value}`);
    $("#c-go").onclick = () => (location.hash = `#/compare?dim=${dim}&a=${encodeURIComponent($("#c-a").value)}&b=${encodeURIComponent($("#c-b").value)}`);
  };

  // ------------------------------------------------------------------ SLOs
  PAGES.slos = async (host, _a, _p, alive) => {
    const d = await api("slos");
    if (!alive()) return;
    const fmtM = (m, v) => v == null ? "–" : ["success_rate", "tool_error_rate"].includes(m) ? pct(v, 1) : m === "apdex" ? v.toFixed(2) : m === "median_cost" ? usd(v) : m === "p95_seconds" ? dur(v) : v;
    host.innerHTML = head("Service Level Objectives", "Objectives over a rolling window, evaluated against the global project and environment filters. Ratio objectives (success rate, Apdex) get an error budget and a 24-hour burn rate. A burn rate above 1 means the budget will run out before the window ends.",
      `<button id="slo-edit">Edit objectives</button>`) +
      `<div class="grid g3">${d.slos.map((s, i) => `<div class="card"><div class="between"><b>${esc(s.name)}</b>${pill(s.status === "ok" ? "ok" : s.status === "breached" ? "critical" : s.status === "at risk" ? "warning" : "unknown", s.status)}</div>
        <div class="row" style="align-items:baseline;margin:6px 0"><span class="big-score" style="font-size:30px">${fmtM(s.metric, s.value)}</span><span class="muted small">target ${s.op} ${fmtM(s.metric, s.target)} · ${s.window_days}d · ${s.n} tasks</span></div>
        ${s.budget_remaining != null ? `<div class="small muted">Error budget remaining</div><div class="score-row" style="grid-template-columns:1fr 50px;margin:2px 0 6px"><div class="track"><div class="fill" style="width:${Math.max(0, Math.min(100, s.budget_remaining * 100))}%;background:${s.budget_remaining > 0.5 ? "var(--good)" : s.budget_remaining > 0 ? "var(--warning)" : "var(--critical)"}"></div></div><span class="num small">${pct(s.budget_remaining)}</span></div>
          <div class="small">Burn rate (24h): <b style="color:${(s.burn_rate_24h || 0) > 1 ? "var(--critical-text)" : "inherit"}">${s.burn_rate_24h == null ? "–" : s.burn_rate_24h + "×"}</b></div>` : ""}
        <div class="small muted" style="margin-top:4px">Days meeting target: ${s.days_met || "–"} ${Object.entries(s.scope || {}).filter(([, v]) => v).map(([k, v]) => `<span class="tag">${k}: ${esc(v)}</span>`).join("")}</div>
        <div id="slo-${i}" style="margin-top:8px"></div></div>`).join("")}</div>
      <div id="slo-editor"></div>`;
    d.slos.forEach((s, i) => { if (s.daily && s.daily.length > 1) C.line($(`#slo-${i}`), s.daily.map((x, j) => ({ x: j, y: x.value || 0 })), { fmt: (v) => fmtM(s.metric, v), xfmt: (j) => dayLabel(s.daily[j].day), height: 110, refLine: s.target, refLabel: "target", color: "var(--s1)", label: s.metric }); });
    $("#slo-edit").onclick = () => {
      const rows = d.slos.map((s) => ({ id: s.id, name: s.name, metric: s.metric, op: s.op, target: s.target, window_days: s.window_days, scope: s.scope || {} }));
      $("#slo-editor").innerHTML = card("Edit objectives", `<p class="small muted">Scope limits an objective to one workflow, project, environment or task type (leave blank for all). Metrics: success_rate, apdex, p95_seconds, median_cost, tool_error_rate. Needs an admin key.</p>
        <textarea id="slo-json" style="width:100%;height:280px;font-family:var(--mono);font-size:12px">${esc(JSON.stringify(rows, null, 2))}</textarea><div class="row" style="margin-top:8px"><button class="primary" id="slo-save">Save</button></div>`);
      $("#slo-save").onclick = async () => { try { await post("slos", { slos: JSON.parse($("#slo-json").value) }); toast("Objectives saved"); route(); } catch (e) { toast("Save failed: " + e.message); } };
    };
  };

})();
