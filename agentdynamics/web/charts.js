/* Minimal SVG chart kit: columns (stacked), line/area, hbars, 100% stack, sparkline. */
const C = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];
  // Phase colors follow the entity, never rank.
  const PHASE = { explore: "--s1", edit: "--s2", verify: "--s3", execute: "--s4", plan: "--s7", delegate: "--s5",
    vcs: "--s6", communicate: "--s8", respond: "--neutral", other: "--neutral" };
  const color = (i) => `var(${SERIES[i % SERIES.length]})`;
  const phaseColor = (p) => `var(${PHASE[p] || "--neutral"})`;
  const tipEl = () => document.getElementById("tip");

  function tip(html, ev) {
    const t = tipEl();
    if (!html) { t.style.display = "none"; return; }
    t.innerHTML = html;
    t.style.display = "block";
    const w = t.offsetWidth, h = t.offsetHeight;
    let x = ev.clientX + 14, y = ev.clientY + 14;
    if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
    if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
    t.style.left = x + "px"; t.style.top = y + "px";
  }
  const hideTip = () => tip(null);

  function el(tag, attrs = {}, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function niceMax(v) {
    if (!v || v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }
  // top-rounded rect path anchored to baseline
  function colPath(x, y, w, h, r) {
    r = Math.min(r, w / 2, h);
    if (h <= 0) return "";
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }

  /** columns(el, rows, {x, keys:[{key,label,color}], fmt, height, xfmt}) — stacked when several keys */
  function columns(host, rows, o) {
    host.innerHTML = "";
    host.classList.add("chart");
    const H = o.height || 200, W = host.clientWidth || 600;
    const m = { l: 48, r: 8, t: 8, b: 24 };
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H }, host);
    const keys = o.keys;
    const totals = rows.map((r) => keys.reduce((s, k) => s + (+r[k.key] || 0), 0));
    const max = niceMax(Math.max(...totals, 0));
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    for (let i = 0; i <= 4; i++) {
      const y = m.t + ih - (ih * i) / 4;
      el("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, class: "gridline" }, svg);
      el("text", { x: m.l - 6, y: y + 4, "text-anchor": "end" }, svg).textContent = o.fmt((max * i) / 4);
    }
    if (!rows.length) return;
    const bw = iw / rows.length, gap = Math.max(1, Math.min(6, bw * 0.25)), w = Math.max(1, bw - gap);
    const every = Math.ceil(rows.length / Math.max(1, Math.floor(iw / 70)));
    rows.forEach((r, i) => {
      const x = m.l + i * bw + gap / 2;
      let base = m.t + ih;
      keys.forEach((k, ki) => {
        const v = +r[k.key] || 0;
        const h = (v / max) * ih;
        if (h <= 0) return;
        const top = ki === keys.length - 1 || keys.slice(ki + 1).every((kk) => !(+r[kk.key]));
        const hh = Math.max(0, h - (ki ? 2 : 0));
        const p = top ? colPath(x, base - h, w, hh, 3) : `M${x},${base - h}h${w}v${hh}h${-w}Z`;
        el("path", { d: p, fill: k.color }, svg);
        base -= h;
      });
      if (i % every === 0) el("text", { x: x + w / 2, y: H - 6, "text-anchor": "middle" }, svg).textContent = (o.xfmt || ((d) => d))(r[o.x]);
      const hit = el("rect", { x: m.l + i * bw, y: m.t, width: bw, height: ih, fill: "transparent" }, svg);
      hit.addEventListener("mousemove", (ev) => {
        let h = `<b>${(o.xfmt || ((d) => d))(r[o.x])}</b>`;
        [...keys].reverse().forEach((k) => {
          if (keys.length > 1 && !(+r[k.key])) return;
          h += `<div class="tr"><span><i style="background:${k.color}"></i>${k.label}</span><b>${o.fmt(+r[k.key] || 0)}</b></div>`;
        });
        if (keys.length > 1) h += `<div class="tr"><span>Total</span><b>${o.fmt(totals[i])}</b></div>`;
        tip(h, ev);
      });
      hit.addEventListener("mouseleave", hideTip);
      if (o.onClick) { hit.style.cursor = "pointer"; hit.addEventListener("click", () => o.onClick(r)); }
    });
    if (keys.length > 1) legend(host, keys);
  }

  function legend(host, keys) {
    const lg = document.createElement("div");
    lg.className = "legend";
    lg.innerHTML = keys.map((k) => `<span><i style="background:${k.color}"></i>${k.label}</span>`).join("");
    host.appendChild(lg);
  }

  /** line(el, pts:[{x,y,...}], {fmt, xfmt, height, area, color, label, tipExtra, refLine}) */
  function line(host, pts, o) {
    host.innerHTML = "";
    host.classList.add("chart");
    const H = o.height || 180, W = host.clientWidth || 600;
    const m = { l: 52, r: 10, t: 10, b: 24 };
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H }, host);
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const max = niceMax(Math.max(...pts.map((p) => p.y), o.refLine || 0, 0));
    const xs = pts.map((p) => p.x);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const sx = (v) => m.l + (x1 === x0 ? iw / 2 : ((v - x0) / (x1 - x0)) * iw);
    const sy = (v) => m.t + ih - (v / max) * ih;
    for (let i = 0; i <= 4; i++) {
      const y = m.t + ih - (ih * i) / 4;
      el("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, class: "gridline" }, svg);
      el("text", { x: m.l - 6, y: y + 4, "text-anchor": "end" }, svg).textContent = o.fmt((max * i) / 4);
    }
    if (!pts.length) return;
    const col = o.color || "var(--s1)";
    const d = pts.map((p, i) => `${i ? "L" : "M"}${sx(p.x)},${sy(p.y)}`).join("");
    if (o.area) el("path", { d: `${d}L${sx(pts.at(-1).x)},${m.t + ih}L${sx(pts[0].x)},${m.t + ih}Z`, fill: col, opacity: 0.12 }, svg);
    if (o.refLine) {
      el("line", { x1: m.l, x2: W - m.r, y1: sy(o.refLine), y2: sy(o.refLine), stroke: "var(--critical)", "stroke-dasharray": "4 4", "stroke-width": 1 }, svg);
      el("text", { x: W - m.r, y: sy(o.refLine) - 4, "text-anchor": "end" }, svg).textContent = o.refLabel || "";
    }
    el("path", { d, fill: "none", stroke: col, "stroke-width": 2, "stroke-linejoin": "round" }, svg);
    const ticks = Math.min(6, pts.length);
    for (let i = 0; i < ticks; i++) {
      const p = pts[Math.round((i * (pts.length - 1)) / Math.max(1, ticks - 1))];
      el("text", { x: sx(p.x), y: H - 6, "text-anchor": "middle" }, svg).textContent = (o.xfmt || ((v) => v))(p.x);
    }
    const cross = el("line", { y1: m.t, y2: m.t + ih, stroke: "var(--text-3)", "stroke-width": 1, opacity: 0 }, svg);
    const dot = el("circle", { r: 4, fill: col, stroke: "var(--surface)", "stroke-width": 2, opacity: 0 }, svg);
    const hit = el("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);
    hit.addEventListener("mousemove", (ev) => {
      const r = svg.getBoundingClientRect();
      const mx = ((ev.clientX - r.left) / r.width) * W;
      let best = pts[0];
      for (const p of pts) if (Math.abs(sx(p.x) - mx) < Math.abs(sx(best.x) - mx)) best = p;
      cross.setAttribute("x1", sx(best.x)); cross.setAttribute("x2", sx(best.x)); cross.setAttribute("opacity", 0.5);
      dot.setAttribute("cx", sx(best.x)); dot.setAttribute("cy", sy(best.y)); dot.setAttribute("opacity", 1);
      tip(`<b>${(o.xfmt || ((v) => v))(best.x)}</b><div class="tr"><span>${o.label || "value"}</span><b>${o.fmt(best.y)}</b></div>${o.tipExtra ? o.tipExtra(best) : ""}`, ev);
    });
    hit.addEventListener("mouseleave", () => { hideTip(); cross.setAttribute("opacity", 0); dot.setAttribute("opacity", 0); });
    if (o.onClick) hit.addEventListener("click", (ev) => {
      const r = svg.getBoundingClientRect(); const mx = ((ev.clientX - r.left) / r.width) * W;
      let best = pts[0]; for (const p of pts) if (Math.abs(sx(p.x) - mx) < Math.abs(sx(best.x) - mx)) best = p;
      o.onClick(best);
    });
  }

  /** hbars(el, items:[{label, value, color?, sub?, href?}], {fmt}) */
  function hbars(host, items, o = {}) {
    const max = Math.max(...items.map((i) => i.value), 0) || 1;
    host.innerHTML = items.map((i) => `
      <div class="hbar" ${i.title ? `title="${i.title}"` : ""}>
        <div class="k">${i.href ? `<a href="${i.href}">${i.label}</a>` : i.label}</div>
        <div class="track"><div class="fill" style="width:${(100 * i.value) / max}%;background:${i.color || "var(--s1)"}"></div></div>
        <div class="v">${(o.fmt || ((v) => v))(i.value)}</div>
      </div>`).join("") || `<div class="empty">No data</div>`;
  }

  /** stack100(el, parts:[{key,label,value,color}]) with legend */
  function stack100(host, parts, o = {}) {
    const tot = parts.reduce((s, p) => s + p.value, 0) || 1;
    const bar = parts.filter((p) => p.value > 0).map((p) =>
      `<div style="width:${(100 * p.value) / tot}%;background:${p.color}" data-l="${p.label}" data-v="${p.value}"></div>`).join("");
    host.innerHTML = `<div class="stack100">${bar}</div>` + (o.legend === false ? "" :
      `<div class="legend">${parts.filter((p) => p.value > 0).map((p) => `<span><i style="background:${p.color}"></i>${p.label} ${Math.round((100 * p.value) / tot)}%</span>`).join("")}</div>`);
    host.querySelectorAll(".stack100 > div").forEach((d) => {
      d.addEventListener("mousemove", (ev) => tip(`<b>${d.dataset.l}</b><div class="tr"><span>share</span><b>${Math.round((100 * d.dataset.v) / tot)}%</b></div>${o.fmt ? `<div class="tr"><span>value</span><b>${o.fmt(+d.dataset.v)}</b></div>` : ""}`, ev));
      d.addEventListener("mouseleave", hideTip);
    });
  }

  function sparkline(values, w = 90, h = 22, col = "var(--s1)") {
    if (!values.length) return "";
    const max = Math.max(...values) || 1;
    const pts = values.map((v, i) => `${values.length === 1 ? w / 2 : (i * w) / (values.length - 1)},${h - 2 - (v / max) * (h - 4)}`).join(" ");
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
  }

  return { columns, line, hbars, stack100, sparkline, color, phaseColor, tip, hideTip, css, PHASE };
})();
