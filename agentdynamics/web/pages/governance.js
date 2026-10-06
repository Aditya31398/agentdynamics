// Governance: Aegis decisions, policies in use, tightened-policy export.
// Uses the helpers app.js exports on window.AD; see app.js.
(() => {
  const { $, $$, esc, usd, tok, dur, ms, pct, num, dt, ago, dayLabel, pill, scoreColor, statusOfRate, store, toast, F, qs, authHeaders, api, post, showLogin, NAV, ALIAS, renderNav, initFilters, showRefreshed, persist, route, head, kpi, card, typedBy, coverageBlock, spendCaveats, sourceMark, evidence, healthOfApdex, taskTable, bindRows, PHASE_ORDER, phaseParts, SCORE_HELP, PAGES } = window.AD;

  // ------------------------------------------------------------------ governance (Aegis)
  PAGES.governance = async (host, _a, p, alive) => {
    const d = await api("governance");
    if (!alive()) return;
    const k = d.kpis;
    const tw = d.tripwires;
    if (!k.governed_tasks) {
      host.innerHTML = head("Governance", "Policy enforcement from <b>Aegis</b>, seen from the agent's side: what was blocked, why, what it cost, and which grants are never used.") +
        tripwireCard(tw) +
        `<div class="card"><h2>No governed runs yet</h2><p class="small muted">Wrap your Aegis kernel. Every decision is then recorded here, model spend is charged to the Aegis budget, and a watchdog can revoke misbehaving agents.</p>
        <div class="code-block">pip install aegis-kernel agentdynamics

import agentdynamics
from agentdynamics.integrations import aegis as governance
from aegis import build_kernel, load_policy

agentdynamics.init(project="support")
kernel, root = build_kernel(load_policy("policy.yaml"), registry)
governance.instrument(kernel, root, watchdog=governance.Watchdog(max_repeated_denials=3))</div>
        <p class="small muted" style="margin-top:10px">Already have Aegis audit logs? Send the JSONL to <code>/api/ingest/records</code> or point an <code>inbox</code> source at it.</p></div>`;
      bindRows(host);
      return;
    }
    host.innerHTML = head("Governance", "Aegis decides what agents may do; AgentDynamics shows what they tried. Denials, budget stops and watchdog revocations are tied back to the task, workflow and node they happened in. <b>Generate tightened policy</b> turns observed behaviour into a least-privilege Aegis policy.") +
      tripwireCard(tw) +
      `<div class="kpis">
        ${kpi("Governed tasks", num(k.governed_tasks), `${k.policies} policy version(s)`)}
        ${kpi("Policy decisions", num(k.decisions), "allowed + denied")}
        ${kpi("Denials", num(k.denials), `${pct(k.denial_rate, 1)} of tool calls`)}
        ${kpi("Budget stops", num(k.budget_stops), `${num(k.spend_denials)} model calls blocked`)}
        ${kpi("Revocations", num(k.revocations), "watchdog / operator kill switch")}
        ${kpi("Boundary probing", num(k.probing_tasks), "tasks where one tool was refused 3+ times in a row")}
        ${kpi("Spend on blocked calls", usd(k.blocked_cost), "tokens used to generate refused calls")}
        ${kpi("Compliance score", k.compliance == null ? "–" : Math.round(k.compliance), "avg over governed tasks")}
        ${kpi("Tripwires touched", num(tw.tasks), `tasks · ${tw.set.tools + tw.set.canaries ? `${tw.set.tools + tw.set.canaries} set on the server` : "set in process only"}`)}
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
        <th>Grants used</th><th>Unused grants</th><th>Ungoverned tools</th><th>Budget headroom (limit ÷ p95 used)</th><th></th></tr></thead><tbody>
        ${d.policies.map((pl, i) => `<tr><td><b>${esc(pl.name || pl.policy)}</b><div class="small muted mono">${esc(pl.policy)}</div><div class="small muted">${esc(pl.workflows.join(", "))}</div></td>
          <td class="num">${pl.tasks}</td><td class="num">${pct(pl.success_rate)}</td><td class="num">${pl.denials}</td><td class="num">${pl.revocations}</td>
          <td class="small">${pl.used.length} of ${pl.granted.length}</td>
          <td>${pl.unused.map((t) => `<span class="tag warn">${esc(t)}</span>`).join("") || `<span class="muted small">none</span>`}</td>
          <td>${(pl.ungoverned || []).map((t) => `<span class="tag warn">${esc(t)}</span>`).join("") || `<span class="muted small">none</span>`}</td>
          <td class="small">${Object.entries(pl.headroom).filter(([, v]) => v).map(([kk, v]) => `<span class="tag ${v > 10 ? "warn" : ""}">${kk} ${v}×</span>`).join("") || "–"}</td>
          <td><button class="primary gv-export" data-i="${i}">Generate tightened policy</button></td></tr>`).join("")}</tbody></table></div>
        <p class="small muted" style="margin:8px 0 0">Unused grants and 10×+ headroom are over-privilege: authority the agents hold but never need. Ungoverned tools ran outside the policy, so nothing mediated them. Compare policy versions side by side under <a href="#/compare?dim=policy">Compare → policy</a>.</p>
        <div id="gv-export"></div>`)}</div>
      <div class="grid g-2-1" style="margin-top:14px">
        ${card("Recent denials", `<div class="table-wrap"><table><thead><tr><th>When</th><th>Rule</th><th>Attempt</th><th>Agent</th><th>Task</th></tr></thead><tbody>
          ${d.recent.map((r) => `<tr class="click" data-href="#/task/${encodeURIComponent(r.task_id)}"><td class="small muted" style="white-space:nowrap">${dt(r.ts)}</td>
            <td><span class="tag bad">${esc(r.rule)}</span></td><td><b>${esc(r.kind === "llm" ? "model call" : r.tool)}</b><div class="small muted">${esc(r.error)}</div></td>
            <td class="small">${esc(r.agent || "–")}</td><td><div class="truncate small" style="max-width:240px">${esc(r.prompt)}</div><span class="tag">${esc(r.workflow || "")}</span></td></tr>`).join("")}</tbody></table></div>`)}
        ${card("Revocations", d.revocations.length ? `<table><tbody>${d.revocations.map((r) => `<tr class="click" data-href="#/task/${encodeURIComponent(r.task_id)}"><td>${pill("critical", "revoked")}</td><td class="small">${esc(r.text)}<div class="muted">${dt(r.ts)}</div></td></tr>`).join("")}</tbody></table>`
          : `<div class="empty">No grants revoked.</div>`, "kill switch")}
      </div>
      <div style="margin-top:14px" id="gv-directives"></div>`;
    directives($("#gv-directives"));
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
        ${coverageBlock(r.coverage)}
        <div class="code-block" style="max-height:360px;overflow:auto">${esc(r.yaml)}</div></div>`;
      $("#gv-copy").onclick = async () => { try { await navigator.clipboard.writeText(r.yaml); toast("Copied"); } catch { toast("Select and copy"); } };
      $("#gv-dl").onclick = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([r.yaml], { type: "text/yaml" })); a.download = `${r.policy.name}.yaml`; a.click(); };
    });
    bindRows(host);
  };

  // ------------------------------------------------------------------ incidents (incidents.py)
  const SEVERITY = { critical: "critical", warning: "warning", info: "unknown" };
  const incStatus = (i) => i.status === "open" ? pill("warning", "open")
    : pill(i.verdict === "real" ? "critical" : "ok", i.verdict === "real" ? "real" : "false alarm");

  // ------------------------------------------------------------------ trust (trust.py)
  const BAND = { trusted: "ok", watch: "warning", low: "critical" };
  const BAR = { trusted: "good", watch: "warning", low: "critical" };
  const trustCell = (a) => `<b>${a.trust.toFixed(1)}</b> ${pill(BAND[a.band], a.band)}
    <div class="bar" style="height:4px;margin-top:4px;background:var(--surface-2);border-radius:2px"><div style="width:${a.trust}%;height:100%;border-radius:2px;background:var(--${BAR[a.band]})"></div></div>`;
  const trustWhy = (a) => [a.tripwire_tasks ? `tripwire in ${a.tripwire_tasks} task(s)` : "", a.probing_tasks ? `probing in ${a.probing_tasks}` : "",
    a.denied ? `${pct(a.denial_rate, 1)} of calls refused` : ""].filter(Boolean).join(" · ") || "nothing against it";
  function trustDetail(a) {
    const p = a.penalty;
    const floored = p.tripwire + p.probing + p.denial_rate > 100;
    return `<div class="small" style="margin-bottom:6px">100 − tripwire ${p.tripwire.toFixed(1)} − probing ${p.probing.toFixed(1)} − refusal rate ${p.denial_rate.toFixed(1)} = <b>${a.trust.toFixed(1)}</b>${floored ? " (it stops at 0)" : ""}
        <span class="muted"> · each piece of evidence counts half as much a week on; a false-alarm verdict removes it, a confirmed one weighs it more</span></div>
      ${a.evidence.length ? `<table><tbody>${a.evidence.map((e) => `<tr class="click" data-href="#/task/${encodeURIComponent(e.task_id)}">
        <td class="small muted" style="white-space:nowrap">${dt(e.ts)}</td><td><span class="tag bad">${esc(e.kind)}</span>${e.verdict ? ` <span class="tag">${e.verdict === "real" ? "confirmed" : esc(e.verdict)}</span>` : ""}</td>
        <td class="num small">−${e.now.toFixed(1)} <span class="muted">of ${e.points}</span></td><td class="small mono">${esc(e.task_id)}</td></tr>`).join("")}</tbody></table>
        ${a.tripwire_tasks + a.probing_tasks > a.evidence.length ? `<div class="small muted">The ${a.evidence.length} weightiest of ${a.tripwire_tasks + a.probing_tasks}.</div>` : ""}`
        : `<div class="small muted">No tripwire or probing evidence${a.denied ? `; ${a.denied} of ${a.calls} calls refused` : ""}.</div>`}`;
  }
  function trustCard(agents) {
    if (!agents.length) return "";
    const shown = agents.slice(0, 15);
    return card("Agent trust", `<div class="table-wrap"><table id="trust-list"><thead><tr><th>Agent</th><th style="width:170px">Trust</th><th>Evidence</th>
        <th class="num">Tasks</th><th class="num">Success</th><th>Last evidence</th></tr></thead><tbody>
      ${shown.map((a, i) => `<tr class="click trust-row" data-i="${i}"><td><b>${esc(a.agent)}</b><div class="small muted">${esc(a.project || "")}</div></td>
        <td>${trustCell(a)}</td><td class="small">${trustWhy(a)}</td><td class="num">${num(a.tasks)}</td>
        <td class="num">${a.success_rate == null ? "–" : pct(a.success_rate)}</td><td class="small muted">${a.last_evidence ? ago(a.last_evidence) : "–"}</td></tr>`).join("")}
      </tbody></table></div>${agents.length > shown.length ? `<p class="small muted">…and ${agents.length - shown.length} more, all trusted more.</p>` : ""}
      <p class="small muted" style="margin:8px 0 0">Behaviour, not competence: failed tasks and tool errors don't lower trust (success is beside it). Click an agent for its evidence.</p>`,
      "lowest first · every task still held");
  }

  PAGES.incidents = async (host, _a, p, alive) => {
    const status = p.status || "";
    const [d, tr] = await Promise.all([api("incidents", status ? { status } : {}), api("trust")]);
    if (!alive()) return;
    const tabs = [["", "All"], ["open", `Open (${d.open})`], ["resolved", "Resolved"]].map(([v, l]) =>
      `<button class="${v === status ? "on" : ""}" data-status="${v}">${l}</button>`).join("");
    const rows = d.incidents.map((i) => `<tr class="click" data-href="#/incident/${encodeURIComponent(i.id)}">
        <td>${pill(SEVERITY[i.severity] || "unknown", i.severity)}</td>
        <td><b>${esc(i.subject)}</b><div class="small muted">${esc(i.project || "every project")}${i.workflows.length ? " · " + esc(i.workflows.join(", ")) : ""}</div></td>
        <td>${Object.entries(i.counts).map(([k, n]) => `<span class="tag ${k === "tripwire" || k === "probing" ? "bad" : ""}">${esc(k)}${n > 1 ? " ×" + n : ""}</span>`).join("")}
          ${i.what.length ? `<div class="small muted">${esc(i.what.join(", "))}</div>` : ""}</td>
        <td class="num">${num(i.tasks)}</td><td class="small muted" style="white-space:nowrap">${dt(i.opened)}</td>
        <td class="small muted" style="white-space:nowrap">${ago(i.updated)}</td><td>${incStatus(i)}</td></tr>`).join("");
    host.innerHTML = head("Incidents", "Security signals about one agent -- a tripwire touched, probing, refused calls, a revocation -- grouped into one thing to judge. Resolve each as <b>real</b> or a <b>false alarm</b>: the verdict is kept, counts in the agent's trust, and alert destinations that take <code>incidents</code> are resolved too.") +
      (tr.agents.length ? `<div style="margin-bottom:14px" id="trust-card">${trustCard(tr.agents)}</div>` : "") +
      `<div class="seg" id="inc-tabs" style="margin-bottom:12px">${tabs}</div>` +
      (rows ? `<div class="card"><div class="table-wrap"><table id="inc-list"><thead><tr><th>Severity</th><th>Agent</th><th>Signals</th><th class="num">Tasks</th><th>Opened</th><th>Last</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>
          <p class="small muted" style="margin:8px 0 0">Open incidents are listed whatever the time window; resolved ones within it.</p></div>`
        : `<div class="card empty">${status === "resolved" ? "No resolved incidents in this window." : "No incidents. One opens when an agent touches a tripwire, probes its policy, has calls refused or its grant revoked, or a directive stops it."}</div>`);
    $$("#inc-tabs button", host).forEach((b) => b.onclick = () => { location.hash = "#/incidents" + (b.dataset.status ? "?status=" + b.dataset.status : ""); });
    $$(".trust-row", host).forEach((r) => r.onclick = () => {
      const nx = r.nextElementSibling;
      if (nx && nx.classList.contains("trust-detail")) { nx.remove(); r.classList.remove("sel"); return; }
      r.classList.add("sel");
      r.insertAdjacentHTML("afterend", `<tr class="trust-detail"><td colspan="6">${trustDetail(tr.agents[+r.dataset.i])}</td></tr>`);
      bindRows(r.nextElementSibling);
    });
    bindRows(host);
  };

  PAGES.incident = async (host, id, _p, alive) => {
    const d = await api(`incident/${encodeURIComponent(id)}`);
    if (!alive()) return;
    const i = d.incident;
    const resolved = i.status === "resolved"
      ? `${i.verdict === "real" ? "Real" : "False alarm"} · ${esc(i.resolved_by || "")} · ${dt(i.resolved_at)}${i.note ? `<div class="small">${esc(i.note)}</div>` : ""}` : "";
    const acts = d.actions.map((a) => {
      if (a.kind === "revoked") return `<div class="small">${pill("critical", "revoked")} until ${dt(a.until)} · <a href="#/governance">directives</a></div>`;
      if (a.kind === "restricted") return `<div class="small">${pill("warning", "restricted")} without ${esc([...a.tools, ...(a.budget != null ? ["part of its budget"] : [])].join(", "))} until ${dt(a.until)}</div>`;
      if (a.kind === "restrict") return `<button class="${a.recommended ? "primary" : ""}" id="inc-restrict">Take ${esc(a.tools.join(", "))} away from ${esc(a.agent)} for ${a.minutes} min</button>
        <span class="small muted">${a.recommended ? "recommended: it keeps everything else" : "milder than revoking: it keeps everything else"}</span>`;
      if (a.kind === "revoke") return `<button class="${a.recommended ? "primary" : ""}" id="inc-revoke">Revoke ${esc(a.agent)} for ${a.minutes} min</button>
        <span class="small muted">${a.recommended ? "recommended: the evidence is of intent" : "optional: refused calls alone may be a policy the agent doesn't know"}</span>`;
      if (a.kind === "tighten") return `<a class="btn" href="#/governance" title="Generate tightened policy">Tighten ${esc(a.policy)}</a>`;
      return "";
    }).join("");
    const verdict = i.status === "open"
      ? `<textarea id="inc-note" rows="2" placeholder="note (optional): what it was, what was done" style="width:100%;margin:8px 0"></textarea>
         <div class="row"><button class="primary" data-verdict="real">Real</button><button data-verdict="false_alarm">False alarm</button></div>`
      : `<div class="row" style="margin-top:8px"><button data-verdict="">Reopen</button></div>`;
    // one row per task (a probing task is also "refused calls" and "revoked"), one per directive
    const groups = [];
    const byTask = {};
    for (const s of d.signals) {
      if (!s.task_id) { groups.push({ ts: s.ts, sigs: [s] }); continue; }
      if (!byTask[s.task_id]) groups.push(byTask[s.task_id] = { ts: s.ts, task_id: s.task_id, held: s.held, prompt: s.prompt, sigs: [] });
      byTask[s.task_id].sigs.push(s);
    }
    const sigs = groups.map((g) => `<tr ${g.task_id && g.held ? `class="click" data-href="#/task/${encodeURIComponent(g.task_id)}"` : ""}>
        <td class="small muted" style="white-space:nowrap">${dt(g.ts)}</td>
        <td>${g.sigs.map((s) => `<span class="tag ${s.severity === "critical" ? "bad" : ""}" title="${esc(s.detail.message || "")}">${esc(s.label)}</span>`).join("")}</td>
        <td class="small">${g.task_id ? (g.held ? `<div class="truncate" style="max-width:340px">${esc(g.prompt)}</div>` : `<span class="muted">task no longer held</span>`)
          : esc(`${g.sigs[0].detail.reason || ""} (${g.sigs[0].detail.source})`)}</td></tr>`).join("");
    const ev = d.evidence.map((s) => `<tr class="click" data-href="#/task/${encodeURIComponent(s.task_id)}">
        <td class="small muted" style="white-space:nowrap">${dt(s.ts)}</td>
        <td><b>${esc(s.kind === "notice" ? "revoked" : s.kind === "llm" ? "model response" : s.name || "")}</b></td>
        <td class="small">${esc(s.agent || "–")}</td>
        <td>${s.tripwire ? `<span class="tag bad">${esc(s.tripwire)}</span>` : ""}${s.denied ? `<span class="tag bad">${esc(s.rule || "denied")}</span>` : ""}
          <div class="small muted">${esc((s.error || (s.kind === "notice" ? s.text : "") || "").slice(0, 200))}</div></td></tr>`).join("");
    host.innerHTML = head(`Incident · ${esc(i.subject)}`, esc(i.title)) +
      `<div class="grid g-2-1"><div class="card" id="inc-summary"><div class="between"><div class="row">${pill(SEVERITY[i.severity] || "unknown", i.severity)} ${incStatus(i)}</div>
          <span class="small muted">${esc(i.project || "every project")}</span></div>
        <p class="small" style="margin:10px 0 4px">${num(i.signals)} signal(s) in ${num(i.tasks)} task(s), ${dt(i.opened)} – ${dt(i.updated)}${i.workflows.length ? " · " + esc(i.workflows.join(", ")) : ""}</p>
        ${i.what.length ? `<div>${i.what.map((w) => `<span class="tag bad">${esc(w)}</span>`).join("")}</div>` : ""}
        ${resolved ? `<p class="small" style="margin-top:8px">${resolved}</p>` : ""}
        ${d.trust ? `<div id="inc-trust" style="margin-top:10px"><span class="small muted">${esc(i.agent)}'s trust</span> ${trustCell(d.trust)}<div class="small muted">${trustWhy(d.trust)}</div></div>` : ""}</div>
      ${card("What to do", `<div id="inc-actions" style="display:flex;flex-direction:column;gap:8px">${acts || `<span class="small muted">No agent name to act on: the signals came from ${esc(i.subject)}.</span>`}</div>${verdict}`, "needs an admin key")}</div>
      <div style="margin-top:14px">${card("Signals", `<div class="table-wrap"><table id="inc-signals"><thead><tr><th>When</th><th>Signals</th><th>Task, or directive</th></tr></thead><tbody>${sigs}</tbody></table></div>`, "by task, oldest first")}</div>
      <div style="margin-top:14px">${card("Evidence", ev ? `<div class="table-wrap"><table><thead><tr><th>When</th><th>Step</th><th>Agent</th><th>Why</th></tr></thead><tbody>${ev}</tbody></table></div>
          ${d.evidence_total > d.evidence.length ? `<p class="small muted">${d.evidence.length} of ${d.evidence_total} steps shown.</p>` : ""}`
        : `<div class="empty">No steps: the tasks are past retention, or the signals are directives.</div>`, "the touching, refused and revoked steps")}</div>`;
    const rs = $("#inc-restrict", host);
    const ract = d.actions.find((a) => a.kind === "restrict");
    if (rs) rs.onclick = async () => {
      try {
        await post("revocations", { agent: ract.agent, project: ract.project || "", minutes: ract.minutes, tools: ract.tools,
          reason: `incident ${i.id}: ${i.title}` });
        toast("Restriction issued"); route();
      } catch (e) { toast("Not issued: " + e.message); }
    };
    const rv = $("#inc-revoke", host);
    const act = d.actions.find((a) => a.kind === "revoke");
    if (rv) rv.onclick = async () => {
      try {
        await post("revocations", { agent: act.agent, project: act.project || "", minutes: act.minutes, reason: `incident ${i.id}: ${i.title}` });
        toast("Directive issued"); route();
      } catch (e) { toast("Not issued: " + e.message); }
    };
    $$("[data-verdict]", host).forEach((b) => b.onclick = async () => {
      try {
        await post(`incidents/${encodeURIComponent(i.id)}/verdict`, { verdict: b.dataset.verdict || null, note: ($("#inc-note", host) || {}).value || "" });
        toast(b.dataset.verdict ? "Resolved" : "Reopened"); route();
      } catch (e) { toast("Not saved: " + e.message); }
    });
    bindRows(host);
  };

  // Tripwires: decoy tools and canary values no legitimate agent touches. Shown first when touched: each touch
  // is certain evidence, not a statistic.
  function tripwireCard(tw) {
    if (!tw || !tw.recent.length) return "";
    const r = tw.directives;
    const policy = (r
      ? `An agent that touches them in ${r.runs} runs within ${r.window_minutes} min is revoked for ${r.revoke_minutes} min wherever it runs. `
      : "The server revokes no agent for them (set <code>[enforcement.tripwires]</code> to revoke one that touches them in several runs). ")
      + "A process using the Aegis integration's tripwires stops the touching run itself, before the call.";
    return `<div class="card" style="margin-bottom:14px;border-color:var(--critical)"><div class="between"><h2 style="margin:0">Tripwires touched</h2>
        <span class="small muted">${num(tw.touches)} touch(es) in ${num(tw.tasks)} task(s)</span></div>
      <p class="small muted" style="margin:6px 0 8px">Decoy tools and planted canary values no legitimate agent uses: each touch means an agent went where it had no reason to. ${policy}</p>
      <div class="table-wrap"><table id="gv-trips"><thead><tr><th>When</th><th>Touched</th><th>Step</th><th>Agent</th><th>Task</th></tr></thead><tbody>
      ${tw.recent.map((r) => `<tr class="click" data-href="#/task/${encodeURIComponent(r.task_id)}"><td class="small muted" style="white-space:nowrap">${dt(r.ts)}</td>
        <td><span class="tag bad">${esc(r.what)}</span></td><td class="small">${esc(r.kind === "llm" ? "model response" : r.step || "–")}</td>
        <td class="small">${esc(r.agent || "–")}<div class="muted">${esc(r.project || "")}</div></td>
        <td><div class="truncate small" style="max-width:260px">${esc(r.prompt)}</div><span class="tag">${esc(r.workflow || "")}</span></td></tr>`).join("")}</tbody></table></div></div>`;
  }

  // Server-side revocation (#8): directives the in-process integration applies through Kernel.revoke
  async function directives(box) {
    const d = await api("revocations").catch(() => null);
    if (!d || !box.isConnected) return;
    const STATUS = { active: "critical", expired: "unknown", cleared: "ok" };
    const what = (r) => r.kind === "restrict"
      ? `<span class="tag warn">restrict</span><div class="small muted">takes ${esc([...(r.spec.tools || []), ...(r.spec.budget != null ? [`all but ${pct(r.spec.budget)} of the budget left`] : [])].join(", "))}</div>`
      : `<span class="tag bad">revoke</span>`;
    const rows = d.revocations.map((r) => `<tr><td>${pill(STATUS[r.status] || "unknown", r.status)}</td>
      <td><b>${esc(r.agent || "every agent")}</b><div class="small muted">${esc(r.project || "every project")}</div></td>
      <td>${what(r)}</td><td class="small">${esc(r.reason)}</td><td class="small muted" style="white-space:nowrap">${ago(r.created)} · ${esc(r.source)}</td>
      <td class="small muted" style="white-space:nowrap">${r.status === "active" ? "until " + dt(r.expires) : ""}</td>
      <td>${r.status === "active" ? `<button class="gv-clear" data-id="${esc(r.id)}">Clear</button>` : ""}</td></tr>`).join("");
    const p = d.probing;
    box.innerHTML = card("Revocation directives", `<p class="small muted" style="margin:0 0 8px">Revoke an agent wherever it runs, or restrict it: take some tools away and let it keep the rest. Processes that call
        <code>instrument(kernel, root, revocations=True)</code> apply a directive through <code>Kernel.revoke</code> within seconds, and revoke new grants of that agent while it lasts.
        Revocation is permanent in Aegis: clearing a directive stops it applying to new grants, and restores nothing.
        ${p ? `Probing detection is on: ${p.denials ?? 10} denied calls across ${p.runs ?? 3} runs within ${p.window_minutes ?? 30} min issue one for ${p.revoke_minutes ?? 60} min.`
            : "Probing detection is off (<code>[enforcement] probing</code> in agentdynamics.toml)."}</p>
      ${rows ? `<div class="table-wrap"><table id="gv-dir-list"><thead><tr><th>Status</th><th>Agent</th><th>Does</th><th>Reason</th><th>Issued</th><th>Applies</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
             : `<div class="empty">No directives.</div>`}
      <div class="row" style="margin-top:10px;flex-wrap:wrap;gap:6px"><input id="gv-r-agent" placeholder="agent (blank: every agent)" style="width:190px">
        <input id="gv-r-project" placeholder="project (blank: all)" style="width:150px"><input id="gv-r-min" type="number" value="60" min="1" style="width:80px" title="minutes">
        <input id="gv-r-tools" placeholder="tools to take away (blank: revoke)" style="width:210px" title="comma-separated: restrict instead of revoke">
        <input id="gv-r-reason" placeholder="reason (goes into the audit log)" style="flex:1;min-width:200px"><button class="primary" id="gv-r-go">Revoke</button></div>`,
      "server-side · needs an admin key to issue or clear");
    $("#gv-r-tools").oninput = () => { $("#gv-r-go").textContent = $("#gv-r-tools").value.trim() ? "Restrict" : "Revoke"; };
    $("#gv-r-go").onclick = async () => {
      const reason = $("#gv-r-reason").value.trim();
      if (!reason) { toast("Say why: the reason goes into the kernel's audit log"); return; }
      const tools = $("#gv-r-tools").value.split(",").map((t) => t.trim()).filter(Boolean);
      try {
        await post("revocations", { agent: $("#gv-r-agent").value.trim(), project: $("#gv-r-project").value.trim(), minutes: +$("#gv-r-min").value || 60, reason,
          ...(tools.length ? { tools } : {}) });
        toast(tools.length ? "Restriction issued" : "Directive issued"); directives(box);
      } catch (e) { toast("Not issued: " + e.message); }
    };
    $$(".gv-clear", box).forEach((b) => b.onclick = async () => {
      try { await post(`revocations/${encodeURIComponent(b.dataset.id)}/clear`, {}); toast("Cleared"); directives(box); }
      catch (e) { toast("Not cleared: " + e.message); }
    });
  }

})();
