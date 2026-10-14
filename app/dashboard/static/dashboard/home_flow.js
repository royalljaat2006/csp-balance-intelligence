/* Decision dashboard: network-movement chart + CSP heatmap.
   Renders only server-provided, already-computed data (json_script payloads); nothing here
   computes a balance, estimate or flow. Own IIFE/file, same isolation convention as ui-kit.js. */
(function () {
  const $ = (id) => document.getElementById(id);
  const readJson = (id) => { const el = $(id); return el ? JSON.parse(el.textContent) : null; };
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs, text) => {
    const n = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
    if (text != null) n.textContent = text;
    return n;
  };
  const inrShort = (v) => {  // full amount, Indian grouping (name kept for existing call sites)
    if (v == null) return "\u2014";
    const s = v < 0 ? "\u2212" : "";
    return s + "\u20b9" + Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  };
  const num = (v) => (v == null ? "—" : Math.round(v).toLocaleString("en-IN"));
  const dShort = (iso) => { const d = new Date(iso + "T00:00:00"); return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" }); };

  // ---------------------------------------------------------------- chart
  const chartData = readJson("fh-chart-data");
  const box = $("fh-chart");
  let view = "money";

  function scale(min, max, h0, h1) {
    if (min === max) { min -= 1; max += 1; }
    return (v) => h1 - ((v - min) / (max - min)) * (h1 - h0);
  }
  function axis(svg, W, top, bottom, min, max, y, fmt, left) {
    const grid = css("--chart-grid") || "#e7ebf1", ink = css("--muted") || "#64748b";
    for (let i = 0; i <= 3; i++) {
      const v = min + ((max - min) * i) / 3, yy = y(v);
      svg.appendChild(el("line", { x1: left, x2: W - 8, y1: yy, y2: yy, stroke: grid, "stroke-width": 1 }));
      svg.appendChild(el("text", { x: left - 6, y: yy + 4, "text-anchor": "end", "font-size": 11, fill: ink }, fmt(v)));
    }
  }
  function xTicks(svg, series, x, H) {
    const ink = css("--muted") || "#64748b", Wv = svg.viewBox.baseVal.width,
      maxTicks = Math.max(2, Math.floor(Wv / 90)), step = Math.max(1, Math.ceil(series.length / maxTicks));
    series.forEach((p, i) => {
      if (i % step === 0 || i === series.length - 1) {
        svg.appendChild(el("text", { x: x(i), y: H - 6, "text-anchor": "middle", "font-size": 11, fill: ink }, dShort(p.date)));
      }
    });
  }
  function legend(items) {
    const lg = $("fh-chart-legend");
    lg.innerHTML = "";
    items.forEach(([color, label, dashed]) => {
      const s = document.createElement("span");
      s.innerHTML = `<i style="background:${dashed ? "transparent" : color};border:2px ${dashed ? "dashed" : "solid"} ${color}"></i>${label}`;
      lg.appendChild(s);
    });
  }
  function hit(svg, series, x, bw, H, text) {
    series.forEach((p, i) => {
      const r = el("rect", { x: x(i) - bw / 2, y: 0, width: bw, height: H, fill: "transparent" });
      r.appendChild(el("title", {}, text(p)));
      svg.appendChild(r);
    });
  }

  function line(svg, S, x, y, key, color, opts) {
    opts = opts || {};
    const pts = S.map((p, i) => (p[key] == null ? null : [x(i), y(p[key])])).filter(Boolean);
    if (!pts.length) return;
    if (opts.area && pts.length > 1) {
      const base = y(opts.base);
      const d = `M${pts[0][0]},${base} ` + pts.map((q) => `L${q[0]},${q[1]}`).join(" ") + ` L${pts[pts.length - 1][0]},${base} Z`;
      svg.appendChild(el("path", { d, fill: color, "fill-opacity": 0.1 }));
    }
    if (pts.length > 1) {
      svg.appendChild(el("polyline", { points: pts.map((q) => q.join(",")).join(" "), fill: "none", stroke: color, "stroke-width": 2.5, "stroke-linejoin": "round", "stroke-dasharray": opts.dash || "" }));
    }
    pts.forEach((q) => svg.appendChild(el("circle", { cx: q[0], cy: q[1], r: pts.length > 20 ? 1.8 : 3.5, fill: color })));
  }

  function draw() {
    if (!box || !chartData) return;
    const S = chartData.series;
    const W = Math.max(280, box.clientWidth), H = 270, top = 12, bottom = H - 26;
    const left = view === "accounts" ? 62 : (W < 480 ? 92 : 104);  // room for full amounts
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "presentation" });
    const x = (i) => left + ((W - left - 12) * (i + 0.5)) / S.length;
    const bw = Math.max(2, ((W - left - 12) / S.length) * 0.8);
    const brand = css("--brand") || "#3f6fd9", accent = css("--accent") || "#d97706";
    box.innerHTML = "";

    if (view === "money") {
      const has = S.filter((p) => p.withdrawals != null);
      if (!has.length) { box.innerHTML = '<p class="fh-empty">No bank statements cover this period yet.</p>'; legend([]); return; }
      const vals = has.flatMap((p) => [p.withdrawals, p.deposits]);
      const min = 0, max = Math.max(...vals) * 1.08, y = scale(min, max, top, bottom);
      axis(svg, W, top, bottom, min, max, y, inrShort, left);
      line(svg, S, x, y, "deposits", brand, { area: true, base: 0 });
      line(svg, S, x, y, "withdrawals", accent, { area: true, base: 0 });
      xTicks(svg, S, x, H);
      hit(svg, S, x, bw * 2, bottom, (p) => p.withdrawals == null
        ? `${dShort(p.date)} · no bank statement`
        : `${dShort(p.date)} · withdrawn ${inrShort(p.withdrawals)} · deposited ${inrShort(p.deposits)} · deposits minus withdrawals ${inrShort(p.net_customer_flow)}${p.flow_status === "complete" ? "" : " · may be incomplete"}`);
      legend([[brand, "Deposited by customers"], [accent, "Withdrawn by customers"]]);
    } else if (view === "accounts") {
      const has = S.filter((p) => p.total_accounts != null);
      if (!has.length) { box.innerHTML = '<p class="fh-empty">No customer account counts for this period yet.</p>'; legend([]); return; }
      const vals = has.map((p) => p.total_accounts), min = Math.min(...vals), max = Math.max(...vals);
      const pad = (max - min || max * 0.02 || 1) * 0.15, y = scale(min - pad, max + pad, top, bottom);
      axis(svg, W, top, bottom, min - pad, max + pad, y, num, left);
      line(svg, S, x, y, "total_accounts", brand);
      xTicks(svg, S, x, H);
      hit(svg, S, x, bw * 2, bottom, (p) => p.total_accounts == null ? `${dShort(p.date)} · no count` : `${dShort(p.date)} · ${num(p.total_accounts)} customer accounts${p.account_growth ? " (" + (p.account_growth > 0 ? "+" : "") + num(p.account_growth) + " since the previous count)" : ""}`);
      legend([[brand, "Customer accounts" + (chartData.account_source === "pmjdy" ? "" : " (from the Calling Sheet for now)")]]);
    } else {
      const obs = S.filter((p) => p.observed_avg_balance != null), est = S.filter((p) => p.estimated_avg_balance != null);
      if (!obs.length) { box.innerHTML = '<p class="fh-empty">No SBI-reported balances for this period.</p>'; legend([]); return; }
      const vals = obs.map((p) => p.observed_avg_balance).concat(est.map((p) => p.estimated_avg_balance));
      const lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo || hi * 0.05 || 1) * 0.15;
      const y = scale(lo - pad, hi + pad, top, bottom);
      axis(svg, W, top, bottom, lo - pad, hi + pad, y, inrShort, left);
      line(svg, S, x, y, "observed_avg_balance", brand);
      if (est.length) line(svg, S, x, y, "estimated_avg_balance", accent, { dash: "5 4" });
      xTicks(svg, S, x, H);
      hit(svg, S, x, bw * 2, bottom, (p) => `${dShort(p.date)} · SBI-reported ${inrShort(p.observed_avg_balance)}${p.estimated_avg_balance != null ? " · our estimate " + inrShort(p.estimated_avg_balance) : ""}`);
      legend([[brand, "SBI-reported average balance (changes about once a month)"]].concat(est.length ? [[accent, "Our estimate (not confirmed)", true]] : []));
    }
    box.appendChild(svg);
  }

  document.querySelectorAll(".fh-tabs .chip").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".fh-tabs .chip").forEach((o) => o.setAttribute("aria-pressed", String(o === b)));
    view = b.dataset.view;
    draw();
  }));
  let rz; window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(draw, 120); });
  draw();

  // ---------------------------------------------------------------- heatmap
  let heat = [];
  const grid = $("fh-heat"), sel = $("fh-heat-metric");
  // The cells are fetched the first time Advanced details is opened, not shipped with every page.
  let heatRequested = false;
  function loadHeat() {
    if (heatRequested || !grid) return;
    heatRequested = true;
    grid.setAttribute("aria-busy", "true");
    fetch(grid.dataset.url, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : []))
      .then((d) => { heat = d; grid.removeAttribute("aria-busy"); paint(); })
      .catch(() => { heatRequested = false; grid.removeAttribute("aria-busy"); });
  }
  const advanced = $("advanced");
  if (advanced) {
    advanced.addEventListener("toggle", () => { if (advanced.open) loadHeat(); });
    if (advanced.open) loadHeat();
  }
  function mix(color, pct) { return `color-mix(in srgb, ${color} ${Math.round(pct)}%, var(--surface))`; }
  function paint() {
    if (!grid || !heat.length) return;
    const metric = sel.value;
    grid.innerHTML = "";
    const vals = heat.map((c) => (metric === "delta" ? (["VALIDATED", "PROVISIONAL"].includes(c.status) ? c.delta : null) : c[metric])).filter((v) => typeof v === "number");
    const maxAbs = Math.max(1e-9, ...vals.map(Math.abs));
    heat.forEach((c) => {
      const a = document.createElement("a");
      a.className = "fh-cell"; a.setAttribute("role", "listitem");
      a.href = window.cspDetailUrl(c.code);
      let bg = "var(--surface-2)", tip = `${c.name || c.code} (${c.code})`;
      if (metric === "fresh") {
        const m = { fresh: css("--good"), stale: css("--accent"), calling_sheet_only: css("--brand") }[c.fresh];
        bg = m ? mix(m, 55) : bg; tip += ` · data: ${c.fresh}`;
      } else {
        const v = metric === "delta" ? (["VALIDATED", "PROVISIONAL"].includes(c.status) ? c.delta : null) : c[metric];
        if (typeof v === "number") {
          bg = mix(v >= 0 ? css("--good") : css("--nil"), 18 + 62 * Math.min(1, Math.abs(v) / maxAbs));
          tip += ` · ${metric === "growth_pct" ? (v >= 0 ? "+" : "") + v.toFixed(1) + "%" : inrShort(v)}`;
        } else { a.classList.add("na"); tip += " · no data"; }
      }
      a.style.background = bg; a.title = tip; a.setAttribute("aria-label", tip);
      grid.appendChild(a);
    });
    $("fh-heat-legend").textContent = {
      growth_pct: "Green = more customer accounts than before, red = fewer, grey = no earlier count. Darker = bigger change.",
      net_flow: "Green = customers deposited more than they withdrew, red = they withdrew more than they deposited, grey = no bank statement data. This is the cash movement, not a measured change in anyone's balance.",
      fresh: "Green = account count updated in the last 26 hours, amber = older, blue = from the Calling Sheet only, grey = none.",
      delta: "Only estimates that have been checked are coloured. CSPs without a reliable estimate stay grey on purpose.",
    }[metric];
  }
  if (sel) sel.addEventListener("change", paint);
})();
