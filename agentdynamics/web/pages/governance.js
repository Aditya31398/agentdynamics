// Governance: Aegis decisions, policies in use, tightened-policy export.
// Uses the helpers app.js exports on window.AD; see app.js.
(() => {
  const { $, $$, esc, usd, tok, dur, ms, pct, num, dt, ago, dayLabel, pill, scoreColor, statusOfRate, store, toast, F, qs, authHeaders, api, post, showLogin, NAV, ALIAS, renderNav, initFilters, showRefreshed, persist, route, head, kpi, card, typedBy, coverageBlock, spendCaveats, sourceMark, evidence, healthOfApdex, taskTable, bindRows, PHASE_ORDER, phaseParts, SCORE_HELP, PAGES } = window.AD;

  // ------------------------------------------------------------------ governance (Aegis)
  PAGES.governance = async (host, _a, p, alive) => {
    const d = await api("governance");
    if (!alive()) return;
    const k = d.kpis;
    if (!k.governed_tasks) {
      host.innerHTML = head("Governance", "Policy enforcement from <b>Aegis</b>, seen from the agent's side: what was blocked, why, what it cost, and which grants are never used.") +
        `<div class="card"><h2>No governed runs yet</h2><p class="small muted">Wrap your Aegis kernel. Every decision is then recorded here, model spend is charged to the Aegis budget, and a watchdog can revoke misbehaving agents.</p>
        <div class="code-block">pip install aegis-kernel agentdynamics

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
        ${coverageBlock(r.coverage)}
        <div class="code-block" style="max-height:360px;overflow:auto">${esc(r.yaml)}</div></div>`;
      $("#gv-copy").onclick = async () => { try { await navigator.clipboard.writeText(r.yaml); toast("Copied"); } catch { toast("Select and copy"); } };
      $("#gv-dl").onclick = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([r.yaml], { type: "text/yaml" })); a.download = `${r.policy.name}.yaml`; a.click(); };
    });
    bindRows(host);
  };

})();
