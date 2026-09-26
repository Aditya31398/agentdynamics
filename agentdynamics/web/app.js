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
    // a project-scoped key sees only its projects; say so rather than let "All" imply the whole install
    const all = f.scope ? `All ${f.scope.length === 1 ? "of this key's project" : `${f.scope.length} of this key's projects`}` : "All projects";
    sel.innerHTML = `<option value="">${esc(all)}</option>` + f.projects.map((p) => `<option value="${esc(p.project)}">${esc(p.project)} (${p.n})</option>`).join("");
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
  // How a task type was decided. A workflow name is a fact; a keyword match is a guess, so show the words.
  const typedBy = (t) => {
    const by = t.typed_by || {};
    const n = Object.values(by).reduce((a, b) => a + b, 0) || 1;
    const label = { workflow: "workflow name", "prompt kind": "prompt kind", "follow-up": "follow-up", keywords: "keywords", unmatched: "no keyword matched" };
    const parts = Object.entries(by).sort((a, b) => b[1] - a[1]).map(([k, v]) => {
      const words = k === "keywords" && t.top_matches && t.top_matches.length ? ` (${t.top_matches.map(esc).join(", ")})` : "";
      return `${label[k] || esc(k)}${words}${v < n ? ` ${pct(v / n)}` : ""}`;
    });
    return parts.length ? `typed by ${parts.join(" · ")}` : "";
  };
  // How much of the traffic that actually ran a candidate policy would refuse. ratify and drift
  // compare declarations and cannot answer this; only the recorded calls can.
  const coverageBlock = (cov) => {
    if (!cov) return `<p class="small muted">Install <code>aegis-kernel</code> alongside the server to check this policy against recorded traffic.</p>`;
    if (!cov.calls) return `<p class="small muted">No governed calls in scope to check this policy against.</p>`;
    const head = cov.denied
      ? `<span class="tag bad">would refuse ${num(cov.denied)} of ${num(cov.calls)} calls that were allowed (${pct(cov.denied_fraction)})</span>`
      : `<span class="tag ok">refuses none of the ${num(cov.calls)} calls that were allowed</span>`;
    const rows = cov.by_tool.filter((t) => t.denied);
    const table = rows.length ? `<div class="table-wrap"><table><thead><tr><th>Tool</th><th class="num">Refused</th><th class="num">Share</th><th>Rule</th></tr></thead><tbody>
      ${rows.map((t) => `<tr><td class="mono">${esc(t.tool)}</td><td class="num">${num(t.denied)} of ${num(t.calls)}</td><td class="num">${pct(t.denied_fraction)}</td>
        <td class="small">${Object.entries(t.rules).map(([k, n]) => `${esc(k)} ×${n}`).join(", ")}</td></tr>`).join("")}</tbody></table></div>` : "";
    const note = cov.args_unrecorded ? `<p class="small muted">${num(cov.args_unrecorded)} call(s) had no recorded arguments (content capture off); only their tool grant was checked.</p>` : "";
    return `<h3 style="margin:10px 0 6px">Against recorded traffic</h3><div style="margin-bottom:8px">${head}</div>${table}${note}`;
  };
  // What the spend figure leaves out or guesses at. Both are reported, never silently absorbed.
  const spendCaveats = (k) => {
    const out = [];
    if (k.unpriced) out.push(`${num(k.unpriced)} model calls unpriced`);
    if (k.tokens_unverified) out.push(`<span title="These sources don't document whether input tokens include cache reads and writes, so the split is estimated.">${num(k.tokens_unverified)} with estimated cache accounting</span>`);
    return out.length ? `<br>${out.join(" · ")}` : "";
  };
  const sourceMark = (t, long) => {
    const src = t.outcome_source || "inferred";
    const why = t.outcome_reason ? ` — ${t.outcome_reason}` : "";
    if (src === "inferred") return long ? `<span class="tag" title="Inferred from errors, interrupts and corrections. Grade it with agentdynamics.outcome() or POST /api/tasks/{id}/outcome.">inferred</span>` : "";
    return `<span class="tag" title="${esc(src + why)}">${src === "graded" ? "graded" : "rated"}${long && t.outcome_reason ? `: ${esc(t.outcome_reason)}` : ""}</span>`;
  };
  // "Completed cleanly 88%" means little if most of it is guessed; say how much was stated.
  const evidence = (by, n) => {
    if (!by || !n) return "";
    const stated = (by.graded || 0) + (by.feedback || 0);
    return stated ? `<br>${pct(stated / n)} graded or rated, ${pct((by.inferred || 0) / n)} inferred` : "<br>all inferred from signals";
  };
  const healthOfApdex = (a) => a == null ? "unknown" : a >= 0.85 ? "normal" : a >= 0.7 ? "warning" : "critical";

  function taskTable(tasks, opts = {}) {
    if (!tasks.length) return `<div class="empty">No tasks match.</div>`;
    return `<div class="table-wrap"><table><thead><tr><th>When</th><th>Request</th><th>Type</th><th>Outcome</th><th>Apdex</th><th class="num">Score</th>
      <th class="num">Cost</th><th class="num">vs baseline</th><th class="num">Agent time</th><th class="num">Tools / errors</th>${opts.compact ? "" : `<th class="num">Tokens</th>`}</tr></thead><tbody>
      ${tasks.map((t) => `<tr class="click" data-href="#/task/${encodeURIComponent(t.id)}">
        <td class="muted small" style="white-space:nowrap">${dt(t.started)}</td>
        <td><div class="truncate" title="${esc(t.prompt)}">${t.is_subagent ? `<span class="tag">subagent</span>` : ""}${esc(t.prompt) || `<span class="muted">(no prompt)</span>`}</div><div class="small muted">${esc(t.project)}</div></td>
        <td><span class="tag">${esc(t.task_type)}</span></td>
        <td>${pill(t.outcome)}${sourceMark(t)}</td><td>${pill(t.apdex)}</td>
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
  // Each area of the console lives in web/pages/<area>.js, loaded after this file. They register on
  // PAGES and use the helpers above through window.AD; the router runs once they have all loaded.
  const PAGES = {};
  const SCORE_HELP = {
    efficiency: "Cost vs. the baseline for this task type, minus avoidable spend",
    focus: "Penalized for redundant reads, repeated identical calls, heavy churn on one file, or excessive exploration",
    reliability: "Share of tool calls that succeeded, penalized for retry streaks and API errors",
    verification: "Did the agent run tests, a build, or the app after its last code edit?",
    context: "Prompt-cache reuse and context-window pressure (compactions, >200k context)",
    autonomy: "Finished without you interrupting, rejecting a tool, or correcting it afterwards",
    compliance: "Stayed inside its Aegis policy: no denied calls, no boundary probing, no revocation or budget stop (governed tasks only)",
  };
  window.AD = { $, $$, esc, usd, tok, dur, ms, pct, num, dt, ago, dayLabel, pill, scoreColor, statusOfRate, store, toast, F, qs, authHeaders, api, post, showLogin, NAV, ALIAS, renderNav, initFilters, showRefreshed, persist, route, head, kpi, card, typedBy, coverageBlock, spendCaveats, sourceMark, evidence, healthOfApdex, taskTable, bindRows, PHASE_ORDER, phaseParts, SCORE_HELP, PAGES };

  // ------------------------------------------------------------------ boot
  // On DOMContentLoaded, not here: the page scripts come after this one, and routing before they
  // have registered would render a page that doesn't exist yet.
  const boot = () => initFilters().then(route).catch((e) => { $("#page").innerHTML = `<div class="card empty">Cannot reach the AgentDynamics server: ${esc(e.message)}</div>`; });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
