/* AgentDynamics console */
(() => {
  // ------------------------------------------------------------------ utils
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const usd = (v) => { v = +v || 0; if (v === 0) return "$0"; if (Math.abs(v) >= 1000) return "$" + Math.round(v).toLocaleString(); if (Math.abs(v) >= 1) return "$" + v.toFixed(2); if (Math.abs(v) >= 0.01) return "$" + v.toFixed(3); return "$" + v.toFixed(4); };
  const tok = (v) => { v = +v || 0; if (v >= 1e9) return (v / 1e9).toFixed(2) + "B"; if (v >= 1e6) return (v / 1e6).toFixed(1) + "M"; if (v >= 1e3) return (v / 1e3).toFixed(1) + "k"; return String(Math.round(v)); };
  const dur = (s) => { s = +s || 0; if (s < 1) return Math.round(s * 1000) + "ms"; if (s < 60) return s.toFixed(s < 10 ? 1 : 0) + "s"; if (s < 3600) return Math.floor(s / 60) + "m " + Math.round(s % 60) + "s"; return Math.floor(s / 3600) + "h " + Math.round((s % 3600) / 60) + "m"; };
  const ms = (v) => (v == null ? "–" : dur(v / 1000));
  const pct = (v, d = 0) => (v == null ? "–" : (v * 100).toFixed(d) + "%");
  const num = (v) => (v == null ? "–" : (+v).toLocaleString());
  const dt = (ts) => { if (!ts) return "–"; const d = new Date(ts * 1000); return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); };
  const ago = (ts) => { if (!ts) return ""; const s = Date.now() / 1000 - ts; if (s < 60) return "just now"; if (s < 3600) return Math.floor(s / 60) + "m ago"; if (s < 86400) return Math.floor(s / 3600) + "h ago"; return Math.floor(s / 86400) + "d ago"; };
  const dayLabel = (d) => { const [y, m, dd] = d.split("-"); return new Date(+y, +m - 1, +dd).toLocaleDateString(undefined, { month: "short", day: "numeric" }); };
  const pill = (v, label) => v ? `<span class="pill ${esc(String(v).replace(/\s+/g, "-"))}">${esc(label || v)}</span>` : "–";
  const scoreColor = (v) => v == null ? "var(--neutral)" : v >= 80 ? "var(--good)" : v >= 60 ? "var(--warning)" : v >= 40 ? "var(--serious)" : "var(--critical)";
  const statusOfRate = (r) => r < 0.02 ? "var(--good)" : r < 0.1 ? "var(--warning)" : "var(--critical)";
  const store = { get(k, d) { try { const v = localStorage.getItem("ad." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("ad." + k, JSON.stringify(v)); } catch {} } };
  const toast = (m) => { const t = $("#toast"); t.textContent = m; t.classList.add("show"); setTimeout(() => t.classList.remove("show"), 2200); };

  // ------------------------------------------------------------------ state & api
  const F = store.get("filters", { project: "", days: "30", sub: "0" });
  function qs(extra = {}) {
    const p = new URLSearchParams();
    const all = { ...F, ...extra };
    for (const k in all) if (all[k] !== "" && all[k] != null) p.set(k, all[k]);
    return p.toString();
  }
  const authHeaders = () => { const k = store.get("apikey", ""); return k ? { Authorization: `Bearer ${k}` } : {}; };
  async function api(path, extra, opts = {}) {
    const r = await fetch(`/api/${path}${path.includes("?") ? "&" : "?"}${qs(extra)}`, { ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) } });
    if (r.status === 401) { showLogin(); throw new Error("API key required"); }
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    return r.json();
  }
  async function post(path, body) {
    const r = await fetch(`/api/${path}`, { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    return r.json();
  }
  function showLogin() {
    $("#page").innerHTML = `<div class="card" style="max-width:420px;margin:60px auto"><h2>Sign in</h2>
      <p class="small muted">This AgentDynamics server requires an API key with the <b>read</b> or <b>admin</b> role.</p>
      <input type="password" id="apikey" placeholder="API key" style="width:100%;margin:8px 0">
      <button class="primary" id="apikey-go">Continue</button></div>`;
    $("#apikey-go").onclick = () => { store.set("apikey", $("#apikey").value.trim()); location.reload(); };
  }

  // ------------------------------------------------------------------ nav
  const NAV = [
    ["Start", [["start", "Get started", "Connect an agent"]]],
    ["Monitor", [["overview", "Overview", "Dashboard"], ["flow", "Flow Map", "Topology"], ["workflows", "Workflows", "Agent graphs & paths"], ["types", "Task Types", "Business txns"], ["tasks", "Tasks", "Snapshots"], ["sessions", "Sessions", ""]]],
    ["Diagnose", [["tools", "Tools", "Backends"], ["models", "Models", "Infrastructure"], ["events", "Events", "Health violations"]]],
    ["Assess", [["governance", "Governance", "Aegis policy enforcement"], ["process", "Process Review", "How the agent works"], ["slos", "SLOs", "Objectives & error budgets"], ["analytics", "Analytics", "Query"], ["compare", "Compare", ""]]],
    ["Configure", [["integrations", "Integrations", "Sources & setup"], ["rules", "Health Rules", ""], ["settings", "Settings", ""]]],
  ];
  const ALIAS = { task: "tasks", workflow: "workflows", integrate: "integrations" };
  function renderNav(route) {
    $("#nav").innerHTML = NAV.map(([g, items]) => `<div class="nav-group"><div>${g}</div>${items.map(([id, label, sub]) =>
      `<a href="#/${id}" class="${route === id || ALIAS[route] === id ? "active" : ""}" title="${esc(sub)}">${label}</a>`).join("")}</div>`).join("");
  }

  // ------------------------------------------------------------------ filters bar
  async function initFilters() {
    const f = await api("filters", { project: "", days: "" });
    const sel = $("#f-project");
    sel.innerHTML = `<option value="">All projects</option>` + f.projects.map((p) => `<option value="${esc(p.project)}">${esc(p.project)} (${p.n})</option>`).join("");
    sel.value = F.project;
    sel.onchange = () => { F.project = sel.value; persist(); };
    const envs = (f.environments || []).filter(Boolean);
    if (envs.length > 1 || (envs.length === 1 && envs[0] !== "default")) {
      $("#f-env-wrap").hidden = false;
      $("#f-env").innerHTML = `<option value="">All environments</option>` + envs.map((e) => `<option>${esc(e)}</option>`).join("");
      $("#f-env").value = F.environment || "";
      $("#f-env").onchange = (e) => { F.environment = e.target.value; persist(); };
    }
    $$("#f-days button").forEach((b) => {
      b.classList.toggle("on", b.dataset.v === F.days);
      b.onclick = () => { F.days = b.dataset.v; $$("#f-days button").forEach((x) => x.classList.toggle("on", x === b)); persist(); };
    });
    $("#f-sub").checked = F.sub === "1";
    $("#f-sub").onchange = (e) => { F.sub = e.target.checked ? "1" : "0"; persist(); };
    $("#btn-refresh").onclick = async () => { $("#btn-refresh").disabled = true; const r = await post("refresh", {}).catch((e) => ({ seconds: "?", error: e.message })); $("#btn-refresh").disabled = false; toast(`Re-indexed in ${r.seconds}s`); route(); };
    $("#btn-theme").onclick = () => {
      const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      document.documentElement.dataset.theme = cur === "dark" ? "light" : "dark"; store.set("theme", document.documentElement.dataset.theme); route();
    };
    showRefreshed(f);
  }
  function showRefreshed(f) {
    $("#refreshed").innerHTML = f.refreshed ? `<span class="status-dot"></span> ${f.runs} runs · indexed ${ago(f.refreshed)}` : "";
  }
  function persist() { store.set("filters", F); route(); }
  const th = store.get("theme", null); if (th) document.documentElement.dataset.theme = th;

  // ------------------------------------------------------------------ router
  let current = null, timer = null;
  async function route() {
    const h = location.hash.replace(/^#\/?/, "") || "overview";
    const [pathPart, query] = h.split("?");
    const [name, ...rest] = pathPart.split("/");
    const arg = rest.length ? decodeURIComponent(rest.join("/")) : null;
    const params = Object.fromEntries(new URLSearchParams(query || ""));
    renderNav(name);
    const page = PAGES[name] || PAGES.overview;
    const token = (current = {});
    const host = $("#page");
    if (!host.dataset.route || host.dataset.route !== h) host.innerHTML = `<div class="loading">Loading…</div>`;
    host.dataset.route = h;
    try {
      await page(host, arg, params, () => token === current);
    } catch (e) {
      host.innerHTML = `<div class="card empty">Failed to load: ${esc(e.message)}</div>`;
      console.error(e);
    }
    api("filters", { project: "", days: "" }).then(showRefreshed).catch(() => {});
    clearTimeout(timer);
    if (["overview", "events", "tasks", "sessions"].includes(name)) timer = setTimeout(async () => {
      if (token !== current) return;
      const f = await api("filters", { project: "", days: "" }).catch(() => null);
      if (f) showRefreshed(f);
      route();
    }, 30000);
  }
  window.addEventListener("hashchange", route);
  let rs; window.addEventListener("resize", () => { clearTimeout(rs); rs = setTimeout(route, 250); });

  const head = (title, desc, right = "") => `<div class="page-head"><div><h1>${title}</h1>${desc ? `<p>${desc}</p>` : ""}</div><div class="row">${right}</div></div>`;
  const kpi = (label, value, hint = "", extra = "") => `<div class="kpi"><div class="label">${label}${extra}</div><div class="value">${value}</div>${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
  const card = (title, body, sub = "", cls = "") => `<div class="card ${cls}"><div class="card-head"><h2>${title}</h2><span class="sub">${sub}</span></div>${body}</div>`;
  const healthOfApdex = (a) => a == null ? "unknown" : a >= 0.85 ? "normal" : a >= 0.7 ? "warning" : "critical";

  function taskTable(tasks, opts = {}) {
    if (!tasks.length) return `<div class="empty">No tasks match.</div>`;
    return `<div class="table-wrap"><table><thead><tr><th>When</th><th>Request</th><th>Type</th><th>Outcome</th><th>Apdex</th><th class="num">Score</th>
      <th class="num">Cost</th><th class="num">vs baseline</th><th class="num">Agent time</th><th class="num">Tools / errors</th>${opts.compact ? "" : `<th class="num">Tokens</th>`}</tr></thead><tbody>
      ${tasks.map((t) => `<tr class="click" data-href="#/task/${encodeURIComponent(t.id)}">
        <td class="muted small" style="white-space:nowrap">${dt(t.started)}</td>
        <td><div class="truncate" title="${esc(t.prompt)}">${t.is_subagent ? `<span class="tag">subagent</span>` : ""}${esc(t.prompt) || `<span class="muted">(no prompt)</span>`}</div><div class="small muted">${esc(t.project)}</div></td>
        <td><span class="tag">${esc(t.task_type)}</span></td>
        <td>${pill(t.outcome)}</td><td>${pill(t.apdex)}</td>
        <td class="num" style="color:${scoreColor(t.score)};font-weight:600">${t.score == null ? "–" : Math.round(t.score)}</td>
        <td class="num">${usd(t.cost + (t.subagent_cost || 0))}${t.subagent_cost ? `<div class="small muted">${usd(t.subagent_cost)} sub</div>` : ""}</td>
        <td class="num" style="color:${t.cost_vs_baseline > 3 ? "var(--critical-text)" : "inherit"}">${t.cost_vs_baseline == null ? "–" : t.cost_vs_baseline.toFixed(1) + "×"}</td>
        <td class="num">${dur(t.duration_s)}</td>
        <td class="num">${t.tool_calls}${t.tool_errors ? ` / <span style="color:var(--critical-text)">${t.tool_errors}</span>` : ""}</td>
        ${opts.compact ? "" : `<td class="num">${tok(t.total_tokens)}</td>`}</tr>`).join("")}</tbody></table></div>`;
  }
  function bindRows(host) { $$("tr[data-href]", host).forEach((tr) => tr.addEventListener("click", (e) => { if (e.target.closest("a")) return; location.hash = tr.dataset.href; })); }

  const PHASE_ORDER = ["explore", "plan", "edit", "verify", "execute", "vcs", "delegate", "communicate", "respond", "other"];
  const phaseParts = (obj) => PHASE_ORDER.filter((p) => obj && obj[p]).map((p) => ({ key: p, label: p, value: obj[p], color: C.phaseColor(p) }));

  // ------------------------------------------------------------------ pages
  const PAGES = {};

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
        ${kpi("Spend", usd(k.cost), `median ${usd(k.median_cost)} / task`)}
        ${kpi("Agent Apdex", k.apdex == null ? "–" : k.apdex.toFixed(2), "satisfied + ½ tolerating", " " + pill(healthOfApdex(k.apdex)))}
        ${kpi("Completed cleanly", pct(k.success_rate), `${pct(k.rework_rate)} interrupted or corrected`)}
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
        ${kpi("User feedback", k.feedback_avg == null ? "–" : k.feedback_avg.toFixed(2), k.unpriced ? `${num(k.unpriced)} unpriced model calls` : "avg score where collected")}
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
        <td>${pill(t.health)}</td><td><b>${esc(t.type)}</b></td><td class="num">${t.tasks}</td><td class="num">${usd(t.cost)}</td>
        <td class="num">${t.baseline ? usd(t.baseline.cost_p50) + " / " + usd(t.baseline.cost_p90) : "–"}</td>
        <td class="num">${t.baseline ? dur(t.baseline.duration_p50) : "–"}</td>
        <td class="num">${t.apdex == null ? "–" : t.apdex.toFixed(2)}</td><td class="num">${pct(t.success_rate)}</td><td class="num">${pct(t.tool_error_rate, 1)}</td>
        <td class="num">${pct(t.verification_rate)}</td><td class="num" style="color:${scoreColor(t.avg_score)};font-weight:600">${t.avg_score == null ? "–" : Math.round(t.avg_score)}</td>
        <td class="num">${t.events}</td><td>${C.sparkline(t.daily.map((x) => x.cost))}</td></tr>`).join("")}</tbody></table></div>`) +
      `<p class="small muted">Types are assigned by intent keywords in the request (fix/bug → bugfix, create/build → feature, and so on). Baselines need at least 3 tasks; smaller groups fall back to the global baseline.</p>`;
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
  const SCORE_HELP = {
    efficiency: "Cost vs. the baseline for this task type, minus avoidable spend",
    focus: "Penalized for redundant reads, repeated identical calls, heavy churn on one file, or excessive exploration",
    reliability: "Share of tool calls that succeeded, penalized for retry streaks and API errors",
    verification: "Did the agent run tests, a build, or the app after its last code edit?",
    context: "Prompt-cache reuse and context-window pressure (compactions, >200k context)",
    autonomy: "Finished without you interrupting, rejecting a tool, or correcting it afterwards",
    compliance: "Stayed inside its Aegis policy: no denied calls, no boundary probing, no revocation or budget stop (governed tasks only)",
  };
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
      `<div class="card" style="margin-bottom:14px"><div class="row" style="margin-bottom:8px"><span class="tag">${esc(t.task_type)}</span>${t.framework && t.framework !== "claude-code" ? `<span class="tag">${esc(t.framework)}</span>` : ""}${t.environment && t.environment !== "default" ? `<span class="tag">env: ${esc(t.environment)}</span>` : ""}${t.policy_version ? `<span class="tag">🛡 ${esc(t.policy_version)}</span>` : ""}${t.policy_denials ? `<span class="tag bad">${t.policy_denials} denied</span>` : ""}${t.revocations ? `<span class="tag bad">revoked</span>` : ""}${pill(t.outcome)}${pill(t.apdex)}${t.is_subagent ? `<span class="tag">subagent</span>` : ""}
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

  // ------------------------------------------------------------------ governance (Aegis)
  PAGES.governance = async (host, _a, p, alive) => {
    const d = await api("governance");
    if (!alive()) return;
    const k = d.kpis;
    if (!k.governed_tasks) {
      host.innerHTML = head("Governance", "Policy enforcement from <b>Aegis</b>, seen from the agent's side: what was blocked, why, what it cost, and which grants are never used.") +
        `<div class="card"><h2>No governed runs yet</h2><p class="small muted">Wrap your Aegis kernel. Every decision is then recorded here, model spend is charged to the Aegis budget, and a watchdog can revoke misbehaving agents.</p>
        <div class="code-block">pip install aegis-guard agentdynamics

import agentdynamics
from agentdynamics.integrations import aegis as governance
from aegis import build_kernel, load_policy

agentdynamics.init(project="support")
kernel, root = build_kernel(load_policy("policy.yaml"), registry)
governance.instrument(kernel, root, watchdog=governance.Watchdog(max_repeated_denials=3))</div>
        <p class="small muted" style="margin-top:10px">Already have Aegis audit logs? Send the JSONL to <code>/api/ingest/records</code> or point an <code>inbox</code> source at it.</p></div>`;
      return;
    }
    host.innerHTML = head("Governance", "Aegis decides what agents may do; AgentDynamics shows what they tried. Denials, budget stops and watchdog revocations are tied back to the task, workflow and node they happened in. <b>Generate tightened policy</b> turns observed behaviour into a least-privilege Aegis policy.") +
      `<div class="kpis">
        ${kpi("Governed tasks", num(k.governed_tasks), `${k.policies} policy version(s)`)}
        ${kpi("Policy decisions", num(k.decisions), "allowed + denied")}
        ${kpi("Denials", num(k.denials), `${pct(k.denial_rate, 1)} of tool calls`)}
        ${kpi("Budget stops", num(k.budget_stops), `${num(k.spend_denials)} model calls blocked`)}
        ${kpi("Revocations", num(k.revocations), "watchdog / operator kill switch")}
        ${kpi("Boundary probing", num(k.probing_tasks), "tasks where one tool was refused 3+ times in a row")}
        ${kpi("Spend on blocked calls", usd(k.blocked_cost), "tokens used to generate refused calls")}
        ${kpi("Compliance score", k.compliance == null ? "–" : Math.round(k.compliance), "avg over governed tasks")}
      </div>
      <div class="grid g2">
        ${card("Denials by rule", `<div id="gv-rule"></div>`, "Aegis rule ids")}
        ${card("Denials over time", `<div id="gv-daily"></div>`, "by guard")}
      </div>
      <div class="grid g2" style="margin-top:14px">
        ${card("Blocked tools & model calls", `<div id="gv-tool"></div>`)}
        ${card("By agent", `<div id="gv-agent"></div>`, "who attempted the blocked actions")}
      </div>
      <div style="margin-top:14px">${card("Policies in use", `<div class="table-wrap"><table><thead><tr><th>Policy</th><th class="num">Tasks</th><th class="num">Success</th><th class="num">Denials</th><th class="num">Revoked</th>
        <th>Grants used</th><th>Unused grants</th><th>Budget headroom (limit ÷ p95 used)</th><th></th></tr></thead><tbody>
        ${d.policies.map((pl, i) => `<tr><td><b>${esc(pl.name || pl.policy)}</b><div class="small muted mono">${esc(pl.policy)}</div><div class="small muted">${esc(pl.workflows.join(", "))}</div></td>
          <td class="num">${pl.tasks}</td><td class="num">${pct(pl.success_rate)}</td><td class="num">${pl.denials}</td><td class="num">${pl.revocations}</td>
          <td class="small">${pl.used.length} of ${pl.granted.length}</td>
          <td>${pl.unused.map((t) => `<span class="tag warn">${esc(t)}</span>`).join("") || `<span class="muted small">none</span>`}</td>
          <td class="small">${Object.entries(pl.headroom).filter(([, v]) => v).map(([kk, v]) => `<span class="tag ${v > 10 ? "warn" : ""}">${kk} ${v}×</span>`).join("") || "–"}</td>
          <td><button class="primary gv-export" data-i="${i}">Generate tightened policy</button></td></tr>`).join("")}</tbody></table></div>
        <p class="small muted" style="margin:8px 0 0">Unused grants and 10×+ headroom are over-privilege: authority the agents hold but never need. Compare policy versions side by side under <a href="#/compare?dim=policy">Compare → policy</a>.</p>
        <div id="gv-export"></div>`)}</div>
      <div class="grid g-2-1" style="margin-top:14px">
        ${card("Recent denials", `<div class="table-wrap"><table><thead><tr><th>When</th><th>Rule</th><th>Attempt</th><th>Agent</th><th>Task</th></tr></thead><tbody>
          ${d.recent.map((r) => `<tr class="click" data-href="#/task/${encodeURIComponent(r.task_id)}"><td class="small muted" style="white-space:nowrap">${dt(r.ts)}</td>
            <td><span class="tag bad">${esc(r.rule)}</span></td><td><b>${esc(r.kind === "llm" ? "model call" : r.tool)}</b><div class="small muted">${esc(r.error)}</div></td>
            <td class="small">${esc(r.agent || "–")}</td><td><div class="truncate small" style="max-width:240px">${esc(r.prompt)}</div><span class="tag">${esc(r.workflow || "")}</span></td></tr>`).join("")}</tbody></table></div>`)}
        ${card("Revocations", d.revocations.length ? `<table><tbody>${d.revocations.map((r) => `<tr class="click" data-href="#/task/${encodeURIComponent(r.task_id)}"><td>${pill("critical", "revoked")}</td><td class="small">${esc(r.text)}<div class="muted">${dt(r.ts)}</div></td></tr>`).join("")}</tbody></table>`
          : `<div class="empty">No grants revoked.</div>`, "kill switch")}
      </div>`;
    C.hbars($("#gv-rule"), d.by_rule.map((r) => ({ label: esc(r.rule), value: r.n, color: r.rule.startsWith("budget") ? "var(--s4)" : r.rule.startsWith("grant") ? "var(--critical)" : "var(--s2)" })), { fmt: num });
    C.hbars($("#gv-tool"), d.by_tool.map((r) => ({ label: esc(r.tool), value: r.n, color: "var(--s2)" })), { fmt: num });
    C.hbars($("#gv-agent"), d.by_agent.map((r) => ({ label: esc(r.agent), value: r.n, color: "var(--s7)" })), { fmt: num });
    const GCOL = { capability: "var(--s2)", budget: "var(--s4)", grant: "var(--critical)", data: "var(--s5)", spawn: "var(--s7)", kernel: "var(--s1)", registry: "var(--neutral)" };
    C.columns($("#gv-daily"), d.daily, { x: "day", keys: d.guards.map((g, i) => ({ key: g, label: g, color: GCOL[g] || C.color(i) })), fmt: num, xfmt: dayLabel, height: 200 });
    $$(".gv-export", host).forEach((b) => b.onclick = async () => {
      const pl = d.policies[+b.dataset.i];
      const box = $("#gv-export");
      box.innerHTML = `<div class="loading">Synthesizing from observed behaviour…</div>`;
      const r = await api(`governance/policy?policy=${encodeURIComponent(pl.policy)}`);
      if (r.error) { box.innerHTML = `<div class="empty">${esc(r.error)}</div>`; return; }
      box.innerHTML = `<div class="card" style="margin-top:12px;background:var(--surface-2)"><div class="between"><h2 style="margin:0">Tightened policy · ${esc(r.policy.name)} v${r.policy.version}</h2>
        <div class="row"><button id="gv-copy">Copy</button><button class="primary" id="gv-dl">Download YAML</button></div></div>
        <p class="small muted">From ${r.stats.tasks} tasks and ${r.stats.tool_calls} allowed calls. ${r.changes.length} change(s). The result can only be tighter than <code>${esc(r.base || "none")}</code>. Verify with <code>aegis ratify</code> and <code>aegis drift --baseline &lt;base&gt; --candidate &lt;this&gt;</code>.</p>
        <ul class="small" style="margin:6px 0 10px;padding-left:18px">${r.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
        <div class="code-block" style="max-height:360px;overflow:auto">${esc(r.yaml)}</div></div>`;
      $("#gv-copy").onclick = async () => { try { await navigator.clipboard.writeText(r.yaml); toast("Copied"); } catch { toast("Select and copy"); } };
      $("#gv-dl").onclick = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([r.yaml], { type: "text/yaml" })); a.download = `${r.policy.name}.yaml`; a.click(); };
    });
    bindRows(host);
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

  // ------------------------------------------------------------------ boot
  initFilters().then(route).catch((e) => { $("#page").innerHTML = `<div class="card empty">Cannot reach the AgentDynamics server: ${esc(e.message)}</div>`; });
})();
