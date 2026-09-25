// Diagnose: tools, models, health events.
// Uses the helpers app.js exports on window.AD; see app.js.
(() => {
  const { $, $$, esc, usd, tok, dur, ms, pct, num, dt, ago, dayLabel, pill, scoreColor, statusOfRate, store, toast, F, qs, authHeaders, api, post, showLogin, NAV, ALIAS, renderNav, initFilters, showRefreshed, persist, route, head, kpi, card, typedBy, coverageBlock, spendCaveats, sourceMark, evidence, healthOfApdex, taskTable, bindRows, PHASE_ORDER, phaseParts, SCORE_HELP, PAGES } = window.AD;

  // ------------------------------------------------------------------ tools
  PAGES.tools = async (host, _a, _p, alive) => {
    const d = await api("tools", { sub: "1" });
    if (!alive()) return;
    const tools = d.tools;
    host.innerHTML = head("Tools & Backends", "Everything the agent calls: shell, file system, web, MCP servers, subagents. The agent equivalent of databases and remote services. Includes subagent activity.") +
      `<div class="grid g2">${card("Most used", `<div id="tb-calls"></div>`)}${card("Least reliable", `<div id="tb-err"></div>`, "error rate, at least 5 calls")}</div>` +
      `<div style="margin-top:14px">${card("All tools", `<div class="table-wrap"><table><thead><tr><th>Tool</th><th>Phase</th><th class="num">Calls</th><th class="num">Errors</th><th class="num">Error rate</th><th class="num">Avg</th><th class="num">p95</th><th class="num">Avg output</th><th class="num">Tokens into context</th><th class="num">Attributed cost</th><th>Hotspots</th></tr></thead><tbody>
      ${tools.map((t, i) => `<tr class="click tool-row" data-i="${i}"><td><b>${esc(t.name.replace(/^mcp__/, "mcp: ").replace(/__/g, " › "))}</b></td><td><span class="tag" style="border-color:${C.phaseColor(t.phase)}">${esc(t.phase)}</span></td>
        <td class="num">${num(t.calls)}</td><td class="num">${num(t.errors)}</td><td class="num" style="color:${t.error_rate >= 0.1 ? "var(--critical-text)" : "inherit"}">${pct(t.error_rate, 1)}</td>
        <td class="num">${ms(t.avg_ms)}</td><td class="num">${ms(t.p95_ms)}</td><td class="num">${tok(t.avg_output_chars / 4)} tok</td><td class="num">${tok(t.output_tokens_est)}</td><td class="num">${usd(t.cost)}</td>
        <td>${Object.entries(t.flags).map(([f, n]) => `<span class="tag warn">${n} ${f.replace(/_/g, " ")}</span>`).join("")}</td></tr>`).join("")}</tbody></table></div>`, "click a row for recent errors")}</div>`;
    C.hbars($("#tb-calls"), tools.slice(0, 10).map((t) => ({ label: esc(t.name.replace(/^mcp__[^_]+__/, "")), value: t.calls, color: C.phaseColor(t.phase) })), { fmt: num });
    C.hbars($("#tb-err"), tools.filter((t) => t.calls >= 5 && t.errors).sort((a, b) => b.error_rate - a.error_rate).slice(0, 10).map((t) => ({ label: esc(t.name.replace(/^mcp__[^_]+__/, "")), value: t.error_rate, color: statusOfRate(t.error_rate) })), { fmt: (v) => pct(v, 1) });
    $$(".tool-row", host).forEach((r) => r.addEventListener("click", () => {
      const nx = r.nextElementSibling;
      if (nx && nx.classList.contains("err-detail")) { nx.remove(); return; }
      const t = tools[+r.dataset.i];
      const tr = document.createElement("tr"); tr.className = "err-detail";
      tr.innerHTML = `<td colspan="11">${t.sample_errors.length ? t.sample_errors.map((e) => `<div class="wf-detail" style="margin:4px 0">${esc(e.error)}\n<a href="#/task/${encodeURIComponent(e.task_id)}">open task →</a></div>`).join("") : `<span class="muted">No errors recorded.</span>`}</td>`;
      r.after(tr);
    }));
  };

  // ------------------------------------------------------------------ models
  PAGES.models = async (host, _a, _p, alive) => {
    const d = await api("models", { sub: "1" });
    if (!alive()) return;
    const ms_ = d.models.filter((m) => m.calls);
    host.innerHTML = head("Models", "The agent's infrastructure: spend, latency, token mix, context pressure and cache efficiency for each model. Includes subagents.") +
      card("Daily spend by model", `<div id="ch-mdaily"></div>`) +
      `<div style="margin-top:14px">${card("Model details", `<div class="table-wrap"><table><thead><tr><th>Model</th><th class="num">Calls</th><th class="num">Spend</th><th class="num">Avg latency</th><th class="num">p95</th><th class="num">Avg context</th><th class="num">Max context</th><th class="num">Cache hit</th><th class="num">Avg output</th><th class="num">Thinking</th><th class="num">Errors</th><th class="num">Rate-limited</th><th class="num">Truncated</th><th class="num">TTFT p50 / p95</th><th class="num">Tokens/s</th><th style="min-width:220px">Token mix (input side + output)</th></tr></thead><tbody>
      ${ms_.map((m, i) => `<tr><td><b>${esc(m.model)}</b><div class="small muted">${Object.entries(m.effort).map(([k, v]) => `${k}:${v}`).join(" ")}</div></td><td class="num">${num(m.calls)}</td><td class="num">${usd(m.cost)}</td><td class="num">${ms(m.avg_ms)}</td><td class="num">${ms(m.p95_ms)}</td>
        <td class="num">${tok(m.avg_context)}</td><td class="num">${tok(m.max_context)}</td><td class="num">${pct(m.cache_hit)}</td><td class="num">${tok(m.avg_output)}</td><td class="num">${tok(m.thinking_tokens)}</td>
        <td class="num" style="color:${m.errors ? "var(--critical-text)" : "inherit"}">${num(m.errors)}</td><td class="num">${num(m.rate_limited)}</td><td class="num">${pct(m.truncation_rate, 1)}</td>
        <td class="num">${m.ttft_p50 == null ? "–" : ms(m.ttft_p50) + " / " + ms(m.ttft_p95)}</td><td class="num">${m.tps_p50 == null ? "–" : Math.round(m.tps_p50)}</td><td><div id="mix-${i}"></div></td></tr>`).join("")}</tbody></table></div>`)}</div>
      <p class="small muted">Latency is measured from the previous event to the model's response in the transcript, so it includes streaming time.</p>`;
    const keys = ms_.map((m, i) => ({ key: m.model, label: m.model, color: C.color(i) }));
    C.columns($("#ch-mdaily"), d.daily, { x: "day", keys, fmt: usd, xfmt: dayLabel, height: 220 });
    ms_.forEach((m, i) => C.stack100($(`#mix-${i}`), [
      { key: "cr", label: "cache read", value: m.cache_read, color: "var(--s1)" }, { key: "cw", label: "cache write", value: m.cache_write, color: "var(--s2)" },
      { key: "in", label: "uncached input", value: m.input_tokens, color: "var(--s3)" }, { key: "out", label: "output", value: m.output_tokens, color: "var(--s4)" }], { legend: false, fmt: tok }));
    host.querySelector("table").insertAdjacentHTML("afterend", `<div class="legend"><span><i style="background:var(--s1)"></i>cache read</span><span><i style="background:var(--s2)"></i>cache write</span><span><i style="background:var(--s3)"></i>uncached input</span><span><i style="background:var(--s4)"></i>output</span></div>`);
  };

  // ------------------------------------------------------------------ events
  PAGES.events = async (host, _a, p, alive) => {
    const d = await api("events", p);
    if (!alive()) return;
    const set = (k, v) => { const q = new URLSearchParams(p); if (v) q.set(k, v); else q.delete(k); location.hash = "#/events?" + q.toString(); };
    host.innerHTML = head("Events", "Health-rule violations raised when a task crosses a threshold. Tune thresholds under <a href='#/rules'>Health Rules</a>.") +
      `<div class="grid g-2-1"><div class="card"><div class="row" style="margin-bottom:10px">
        <select id="e-sev"><option value="">All severities</option>${["critical", "warning", "info"].map((s) => `<option ${p.severity === s ? "selected" : ""}>${s}</option>`).join("")}</select>
        <select id="e-rule"><option value="">All rules</option>${d.rules.map((r) => `<option value="${r.rule_id}" ${p.rule_id === r.rule_id ? "selected" : ""}>${esc(r.rule)}</option>`).join("")}</select>
        <span class="small muted">${d.events.length} events</span></div>
        <div class="table-wrap"><table><thead><tr><th>When</th><th>Severity</th><th>Rule</th><th>Detail</th><th>Task</th></tr></thead><tbody>
        ${d.events.map((e) => `<tr class="click" data-href="#/task/${encodeURIComponent(e.task_id)}"><td class="small muted" style="white-space:nowrap">${dt(e.ts)}</td><td>${pill(e.severity)}</td><td><b>${esc(e.rule)}</b></td><td class="small">${esc(e.message)}</td><td><div class="truncate small" style="max-width:280px">${esc(e.prompt)}</div><span class="tag">${esc(e.task_type)}</span></td></tr>`).join("") || `<tr><td colspan="5" class="empty">No events</td></tr>`}</tbody></table></div></div>
        ${card("Violations by rule", `<div id="ev-rules"></div>`, "all time")}</div>`;
    $("#e-sev").onchange = (e) => set("severity", e.target.value);
    $("#e-rule").onchange = (e) => set("rule_id", e.target.value);
    C.hbars($("#ev-rules"), d.rules.map((r) => ({ label: esc(r.rule), value: r.n, color: r.severity === "critical" ? "var(--critical)" : r.severity === "warning" ? "var(--warning)" : "var(--s1)" })), { fmt: num });
    bindRows(host);
  };

})();
