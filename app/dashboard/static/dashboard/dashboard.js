/* CSP Balance Tracker — table interactivity + a small hand-rolled SVG line chart.
   No charting library: one series, modest data volume, and it keeps the
   deployed app dependency-free (dataviz skill's mark/interaction spec, applied by hand).

   Wrapped in an IIFE on purpose: classic <script> tags share one global
   lexical scope across the whole page, so a top-level `const`/`let` here
   would collide with an identically-named one in a page's own inline
   <script> (this bit a real deploy: SLAB_ORDER was declared both here and
   in home.html, throwing "Identifier has already been declared" and
   silently killing the *entire* inline script that renders the ladder and
   table). Only the window.* names below are this file's public surface. */
(function () {
  // Tracker/Eko mode switching now lives in ui-kit.js's initModeSwitch()
  // (a real cookie-backed mode, not just this file's old client-only
  // data-theme toggle) — see dashboard/context_processors.py.

  /**
   * Generic modal popup, built once and reused for every KPI card's
   * click-through — used instead of scrolling/navigating to a section so
   * the relevant detail shows up in place. openModal() clears and returns
   * the body element; the caller fills it in (a ranked list, a chart,
   * whatever fits that KPI).
   */
  window.openModal = function (title) {
    let overlay = document.getElementById("modal-overlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "modal-overlay";
      overlay.className = "modal-overlay";
      overlay.innerHTML = `
        <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="modal-title">
          <div class="modal-head"><h3 id="modal-title"></h3><button type="button" class="modal-close" aria-label="Close">&times;</button></div>
          <div class="modal-body" id="modal-body"></div>
        </div>`;
      document.body.appendChild(overlay);
      overlay.addEventListener("click", (e) => { if (e.target === overlay) window.closeModal(); });
      overlay.querySelector(".modal-close").addEventListener("click", window.closeModal);
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && overlay.classList.contains("show")) window.closeModal();
      });
    }
    overlay.querySelector("#modal-title").textContent = title;
    const body = overlay.querySelector("#modal-body");
    body.innerHTML = "";
    overlay.classList.add("show");
    document.body.style.overflow = "hidden";
    return body;
  };
  window.closeModal = function () {
    const overlay = document.getElementById("modal-overlay");
    if (overlay) overlay.classList.remove("show");
    document.body.style.overflow = "";
  };

  window.fmtINR = function (n) {
    if (n == null) return "—";
    n = Math.round(n);
    const s = Math.abs(n).toString();
    const last3 = s.slice(-3), rest = s.slice(0, -3);
    const grouped = rest ? rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + last3 : last3;
    return (n < 0 ? "-₹" : "₹") + grouped;
  };
  window.fmtCr = function (n) {
    if (n == null) return "—";
    if (Math.abs(n) >= 1e7) return "₹" + (n / 1e7).toFixed(2) + "Cr";
    if (Math.abs(n) >= 1e5) return "₹" + (n / 1e5).toFixed(1) + "L";
    return window.fmtINR(n);
  };
  // Always whole numbers — this formats counts (accounts, transactions),
  // never money. A chart gridline value is linearly interpolated between
  // min/max, so it needs rounding here or it prints "3,193.56 transactions".
  window.fmtNum = function (n) { return n == null ? "—" : Math.round(n).toLocaleString("en-IN"); };

  const SLAB_CLASS = { NIL: "nil", S1: "s1", S2: "s2", S3: "s3", S4: "s4" };

  // Monotone-ish smoothing (Catmull-Rom -> cubic Bezier, tension 1/6) so
  // trend lines read as clean curves instead of jagged day-to-day segments —
  // restrained enough not to overshoot/invert real financial movement.
  let _gradSeq = 0;
  function smoothPath(pts) {
    if (pts.length < 3) return pts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)} `;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i === 0 ? i : i - 1], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2 < pts.length ? i + 2 : i + 1];
      const c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6;
      const c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += `C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)} `;
    }
    return d.trim();
  }

  function trendTag(c) {
    if (c.trend_flag === "improving") return `<span class="trend-tag up">&#8599; improving</span>`;
    if (c.trend_flag === "declining") return `<span class="trend-tag down">&#8600; declining</span>`;
    if (c.trend_7d_pct == null) return `<span class="trend-tag">new</span>`;
    return `<span class="trend-tag">stable</span>`;
  }

  // Net new accounts since the CSP's previous CALLING SHEET reading — null
  // means either no second reading yet, or account_count wasn't reported.
  function growthCell(c) {
    if (c.net_new_accounts == null) return `<span class="trend-tag">&mdash;</span>`;
    if (c.net_new_accounts > 0) return `<span class="trend-tag up">+${c.net_new_accounts}</span>`;
    if (c.net_new_accounts < 0) return `<span class="trend-tag down">${c.net_new_accounts}</span>`;
    return `<span class="trend-tag">0</span>`;
  }

  /**
   * Wires a searchable / filterable / sortable / paginated CSP table.
   * `rows` is the full dataset (already fetched server-side and inlined into
   * the page as JSON) — filtering/sorting/paging all happen client-side,
   * which is fine at ~500 rows.
   */
  window.initCspTable = function (rows, opts) {
    opts = opts || {};
    const pageSize = opts.pageSize || 20;
    const state = {
      q: "",
      slab: "ALL",
      sortKey: opts.sortKey || "amount_to_deposit",
      sortDir: opts.sortDir || 1,
      page: 0,
    };

    const tbody = document.getElementById("tbl-body");
    const countEl = document.getElementById("table-count");
    const pagerInfo = document.getElementById("pager-info");
    const prevBtn = document.getElementById("prev");
    const nextBtn = document.getElementById("next");

    function filtered() {
      const q = state.q.trim().toLowerCase();
      let out = rows.filter((c) => {
        if (state.slab !== "ALL" && c.slab !== state.slab) return false;
        if (!q) return true;
        return (c.name || "").toLowerCase().includes(q) || c.csp_code.toLowerCase().includes(q) || (c.mobile || "").includes(q);
      });
      out.sort((a, b) => {
        let av = a[state.sortKey], bv = b[state.sortKey];
        if (av == null) av = state.sortDir === 1 ? Infinity : -Infinity;
        if (bv == null) bv = state.sortDir === 1 ? Infinity : -Infinity;
        if (typeof av === "string") return av.localeCompare(bv) * state.sortDir;
        return (av - bv) * state.sortDir;
      });
      return out;
    }

    function render() {
      const list = filtered();
      countEl.textContent = list.length + " match" + (list.length === 1 ? "" : "es");
      const start = state.page * pageSize;
      const page = list.slice(start, start + pageSize);

      tbody.innerHTML = page.map((c) => `
        <tr>
          <td><a class="name-link" href="/dashboard/csp/${c.csp_code}/">${c.name || c.csp_code}</a><span class="code">${c.csp_code}</span></td>
          <td class="mono">${window.fmtNum(c.account_count)}</td>
          <td class="mono">${growthCell(c)}</td>
          <td class="mono">${window.fmtINR(c.mtd_mab)}</td>
          <td><span class="pill ${SLAB_CLASS[c.slab]}">${c.slab}</span></td>
          <td class="mono">${c.slab === "S4" ? "&mdash;" : window.fmtCr(c.amount_to_deposit)}</td>
          <td>${trendTag(c)}</td>
        </tr>`).join("") || `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px;">No CSPs match this filter.</td></tr>`;

      document.querySelectorAll("thead th[data-key]").forEach((th) => {
        th.classList.toggle("sorted", th.dataset.key === state.sortKey);
        const arrow = th.querySelector(".arrow");
        if (arrow) arrow.textContent = th.dataset.key === state.sortKey ? (state.sortDir === 1 ? "↑" : "↓") : "↓";
      });

      const pages = Math.max(1, Math.ceil(list.length / pageSize));
      pagerInfo.textContent = `Page ${state.page + 1} of ${pages}`;
      prevBtn.disabled = state.page <= 0;
      nextBtn.disabled = state.page >= pages - 1;
    }

    document.getElementById("search").addEventListener("input", (e) => { state.q = e.target.value; state.page = 0; render(); });
    document.querySelectorAll(".chip[data-slab]").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".chip[data-slab]").forEach((b) => b.setAttribute("aria-pressed", "false"));
        btn.setAttribute("aria-pressed", "true");
        state.slab = btn.dataset.slab; state.page = 0; render();
      });
    });
    document.querySelectorAll("thead th[data-key]").forEach((th) => {
      th.addEventListener("click", () => {
        if (state.sortKey === th.dataset.key) state.sortDir *= -1;
        else { state.sortKey = th.dataset.key; state.sortDir = 1; }
        render();
      });
    });
    prevBtn.addEventListener("click", () => { state.page--; render(); });
    nextBtn.addEventListener("click", () => { state.page++; render(); });

    render();
  };

  /**
   * Renders a single-series rounded pill-bar chart into `container` (a div).
   * `series` = [{date: "2026-08-01", value: 1234}, ...]. Hand-rolled SVG:
   * fully-rounded capsule bars, recessive gridlines, the current/last bar
   * emphasized (today vs. history is a real, honest distinction — not a
   * fabricated one), a hover tooltip per bar.
   */
  window.renderTrendChart = function (container, series, opts) {
    opts = opts || {};
    const fmt = opts.valueFormat || window.fmtNum;
    const label = opts.label || "Value";
    const color = opts.color || "var(--brand)";

    if (!series || series.length === 0) {
      container.innerHTML = `<div class="chart-empty">${opts.emptyText || "No data yet."}</div>`;
      return;
    }

    const W = 720, H = 220, PAD = { top: 16, right: 16, bottom: 28, left: 56 };
    const plotW = W - PAD.left - PAD.right, plotH = H - PAD.top - PAD.bottom;
    const n = series.length;

    const values = series.map((d) => d.value);
    let min = Math.min(0, ...values), max = Math.max(...values);
    if (min === max) { max = min + 1; }
    const pad = (max - min) * 0.08;
    max += pad;

    const x = (i) => PAD.left + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
    const y = (v) => PAD.top + plotH - ((v - min) / (max - min)) * plotH;
    const yZero = y(Math.max(min, 0));

    const gridLines = [0, 0.33, 0.66, 1].map((t) => {
      const v = min + (max - min) * t;
      const yy = y(v);
      return `<line class="chart-grid" x1="${PAD.left}" x2="${W - PAD.right}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}"></line>
              <text class="chart-axis" x="${PAD.left - 10}" y="${(yy + 3).toFixed(1)}" text-anchor="end">${fmt(v)}</text>`;
    }).join("");

    const step = Math.max(1, Math.ceil(n / 7));
    const xLabels = series.map((d, i) => {
      if (i % step !== 0 && i !== n - 1) return "";
      return `<text class="chart-axis" x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle">${d.date.slice(5)}</text>`;
    }).join("");

    const slot = n > 1 ? plotW / n : plotW;
    const barW = Math.max(4, Math.min(26, slot * 0.5));
    const bars = series.map((d, i) => {
      const isLast = i === n - 1;
      const top = Math.min(y(d.value), yZero), bottom = Math.max(y(d.value), yZero);
      const h = Math.max(bottom - top, 3);
      const bx = x(i) - barW / 2;
      return `<rect class="chart-bar${isLast ? " current" : ""}" style="fill:${color}" data-i="${i}"
        x="${bx.toFixed(1)}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" rx="${(barW / 2).toFixed(1)}"></rect>`;
    }).join("");

    container.innerHTML = `
      <div class="chart-wrap">
        <svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="${label} over time">
          ${gridLines}
          ${bars}
          ${xLabels}
        </svg>
        <div class="chart-tooltip" id="ct-tooltip"></div>
      </div>`;

    const svg = container.querySelector("svg");
    const tooltip = container.querySelector("#ct-tooltip");
    svg.querySelectorAll(".chart-bar").forEach((bar) => {
      bar.addEventListener("mouseenter", () => {
        const i = +bar.dataset.i, d = series[i];
        const rect = svg.getBoundingClientRect();
        const px = rect.left + (x(i) / W) * rect.width;
        const py = rect.top + (Math.min(y(d.value), yZero) / H) * rect.height;
        tooltip.innerHTML = `<span class="ct-date">${d.date}</span>${fmt(d.value)}`;
        tooltip.style.left = (px - container.getBoundingClientRect().left) + "px";
        tooltip.style.top = (py - container.getBoundingClientRect().top) + "px";
        tooltip.classList.add("show");
        bar.style.opacity = "0.75";
      });
      bar.addEventListener("mouseleave", () => {
        tooltip.classList.remove("show");
        bar.style.opacity = "";
      });
    });
  };

  /**
   * Multi-series line comparison chart (e.g. withdrawals vs deposits over
   * the same date axis) — same visual language as renderTrendChart, plus a
   * legend and a single shared crosshair/tooltip covering every series at
   * the hovered date, the way a Power BI combo-line visual reads.
   * `seriesList` = [{name, color, data:[{date, value}, ...]}, ...] — every
   * series must share the same date axis (caller's responsibility).
   */
  window.renderMultiTrendChart = function (container, seriesList, opts) {
    opts = opts || {};
    const fmt = opts.valueFormat || window.fmtNum;
    const label = opts.label || "Comparison over time";
    const dates = (seriesList[0] && seriesList[0].data) || [];

    if (!dates.length) {
      container.innerHTML = `<div class="chart-empty">${opts.emptyText || "No data yet."}</div>`;
      return;
    }

    const W = 720, H = 240, PAD = { top: 16, right: 16, bottom: 28, left: 56 };
    const plotW = W - PAD.left - PAD.right, plotH = H - PAD.top - PAD.bottom;
    const n = dates.length;

    const allValues = seriesList.flatMap((s) => s.data.map((d) => d.value));
    let min = Math.min(0, ...allValues), max = Math.max(...allValues);
    if (min === max) max = min + 1;
    max += (max - min) * 0.08;

    const x = (i) => PAD.left + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
    const y = (v) => PAD.top + plotH - ((v - min) / (max - min)) * plotH;

    const gridLines = [0, 0.33, 0.66, 1].map((t) => {
      const v = min + (max - min) * t, yy = y(v);
      return `<line class="chart-grid" x1="${PAD.left}" x2="${W - PAD.right}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}"></line>
              <text class="chart-axis" x="${PAD.left - 10}" y="${(yy + 3).toFixed(1)}" text-anchor="end">${fmt(v)}</text>`;
    }).join("");

    const step = Math.max(1, Math.ceil(n / 7));
    const xLabels = dates.map((d, i) => {
      if (i % step !== 0 && i !== n - 1) return "";
      return `<text class="chart-axis" x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle">${d.date.slice(5)}</text>`;
    }).join("");

    const lines = seriesList.map((s) => {
      const path = smoothPath(s.data.map((d, i) => [x(i), y(d.value)]));
      return `<path class="chart-line" style="stroke:${s.color}" d="${path}"></path>`;
    }).join("");

    const colW = n > 1 ? plotW / (n - 1) : plotW;
    const hoverCols = dates.map((d, i) => {
      const cx = x(i) - colW / 2;
      return `<rect class="chart-hover-col" data-i="${i}" x="${cx.toFixed(1)}" y="${PAD.top}" width="${colW.toFixed(1)}" height="${plotH}"></rect>`;
    }).join("");

    const crosshair = `<line class="chart-crosshair" id="mt-crosshair" x1="0" x2="0" y1="${PAD.top}" y2="${PAD.top + plotH}" style="display:none;"></line>`;

    container.innerHTML = `
      <div class="chart-wrap">
        <svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="${label}">
          ${gridLines}
          ${lines}
          ${crosshair}
          ${hoverCols}
          ${xLabels}
        </svg>
        <div class="chart-tooltip" id="mt-tooltip"></div>
      </div>
      <div class="legend-row">
        ${seriesList.map((s) => `<span><span class="sw" style="background:${s.color}"></span>${s.name}</span>`).join("")}
      </div>`;

    const svg = container.querySelector("svg");
    const tooltip = container.querySelector("#mt-tooltip");
    const crosshairEl = container.querySelector("#mt-crosshair");

    svg.querySelectorAll(".chart-hover-col").forEach((col) => {
      col.addEventListener("mouseenter", () => {
        const i = +col.dataset.i;
        const rect = svg.getBoundingClientRect();
        const px = rect.left + (x(i) / W) * rect.width;
        const py = rect.top + (PAD.top / H) * rect.height;
        const lineText = seriesList.map((s) => `<span style="color:${s.color};font-weight:700;">${s.name}</span> ${fmt(s.data[i].value)}`).join("&nbsp;&nbsp;");
        tooltip.innerHTML = `<span class="ct-date">${dates[i].date}</span>${lineText}`;
        tooltip.style.left = (px - container.getBoundingClientRect().left) + "px";
        tooltip.style.top = (py - container.getBoundingClientRect().top) + "px";
        tooltip.classList.add("show");
        crosshairEl.setAttribute("x1", x(i).toFixed(1));
        crosshairEl.setAttribute("x2", x(i).toFixed(1));
        crosshairEl.style.display = "block";
      });
      col.addEventListener("mouseleave", () => {
        tooltip.classList.remove("show");
        crosshairEl.style.display = "none";
      });
    });
  };

  /**
   * Donut chart for a small set of mutually-exclusive categories (slab
   * distribution, balance-trend split) — center total + a legend that
   * cross-highlights its segment on hover, the standard Power BI donut
   * interaction.
   * `segments` = [{label, value, color}, ...].
   */
  window.renderDonutChart = function (container, segments, opts) {
    opts = opts || {};
    const fmt = opts.valueFormat || window.fmtNum;
    const total = segments.reduce((a, s) => a + (s.value || 0), 0);

    if (!total) {
      container.innerHTML = `<div class="chart-empty">${opts.emptyText || "No data yet."}</div>`;
      return;
    }

    const size = 180, cx = size / 2, cy = size / 2, r = 62, strokeW = 20;
    const circumference = 2 * Math.PI * r;
    const gap = Math.min(6, circumference * 0.01);
    let acc = 0;
    const arcs = segments.map((s, i) => {
      const frac = (s.value || 0) / total;
      acc += s.value || 0;
      if (!frac) return "";
      const dash = Math.max(0, frac * circumference - gap);
      const rotation = ((acc - (s.value || 0)) / total) * 360 - 90;
      return `<circle class="donut-seg" data-i="${i}" cx="${cx}" cy="${cy}" r="${r}" fill="none"
        style="stroke:${s.color}" stroke-width="${strokeW}" stroke-linecap="round"
        stroke-dasharray="${dash.toFixed(1)} ${(circumference - dash).toFixed(1)}"
        transform="rotate(${rotation.toFixed(2)} ${cx} ${cy})"></circle>`;
    }).join("");

    container.innerHTML = `
      <div class="donut-wrap">
        <div class="donut-chart" style="width:${size}px;height:${size}px;">
          <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-label="${opts.label || "Distribution"}">
            ${arcs}
          </svg>
          <div class="donut-center">
            <div class="donut-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="7" width="18" height="13" rx="2.5"></rect><path d="M3 10h18M8 7V5.5A1.5 1.5 0 0 1 9.5 4h5A1.5 1.5 0 0 1 16 5.5V7"></path></svg></div>
            <div class="donut-total">${fmt(total)}</div>
            <div class="donut-total-label">${opts.centerLabel || "Total"}</div>
          </div>
        </div>
        <div class="donut-legend">
          ${segments.map((s, i) => `
            <div class="donut-legend-item" data-i="${i}">
              <span class="sw" style="background:${s.color}"></span>
              <span class="lbl">${s.label}</span>
              <span class="val">${fmt(s.value)}</span>
              <span class="pct">${Math.round((s.value / total) * 100)}%</span>
            </div>`).join("")}
        </div>
      </div>`;

    const arcEls = container.querySelectorAll(".donut-seg");
    container.querySelectorAll(".donut-legend-item").forEach((item) => {
      item.addEventListener("mouseenter", () => {
        arcEls.forEach((a) => { a.style.opacity = a.dataset.i === item.dataset.i ? "1" : "0.3"; });
      });
      item.addEventListener("mouseleave", () => {
        arcEls.forEach((a) => { a.style.opacity = "1"; });
      });
    });
  };

  /**
   * CSP heatmap (Trends & Analytics) — one tile per CSP, colored by the
   * comparison engine's own Trend classification. `rows` is the exact flat
   * shape dashboard/views.py's _comparison_row() produces (the same shape
   * CSP Directory/Daily Comparison already consume) — no new computation,
   * no invented health score, just an explainable GROWTH/DECLINE/NO_CHANGE/
   * NO_DATA state per tile with a hover detail card.
   */
  const HEATMAP_TREND_CLASS = { GROWTH: "growth", DECLINE: "decline", NO_CHANGE: "stable", NO_DATA: "no-data" };
  const HEATMAP_TREND_LABEL = { GROWTH: "Growing", DECLINE: "Declining", NO_CHANGE: "Stable", NO_DATA: "No data" };

  window.renderCspHeatmap = function (container, rows, opts) {
    opts = opts || {};
    const fmt = opts.valueFormat || window.fmtINR;

    if (!rows || !rows.length) {
      container.innerHTML = `<div class="chart-empty">${opts.emptyText || "No comparison data yet."}</div>`;
      return;
    }

    const tiles = rows.map((c) => {
      const cls = HEATMAP_TREND_CLASS[c.trend] || "no-data";
      return `<a class="heatmap-tile ${cls}" href="/dashboard/csp/${c.csp_code}/" data-code="${c.csp_code}" aria-label="${c.csp_code}"></a>`;
    }).join("");

    container.innerHTML = `
      <div class="heatmap-wrap">
        <div class="heatmap-grid">${tiles}</div>
        <div class="chart-tooltip heatmap-tooltip" id="hm-tooltip"></div>
      </div>
      <div class="heatmap-legend">
        <span><span class="sw" style="background:var(--good-soft);box-shadow:inset 0 0 0 1px var(--good);"></span>Growing</span>
        <span><span class="sw" style="background:var(--nil-soft);box-shadow:inset 0 0 0 1px var(--nil);"></span>Declining</span>
        <span><span class="sw" style="background:var(--surface-2);box-shadow:inset 0 0 0 1px var(--line-strong);"></span>Stable</span>
        <span><span class="sw" style="background:var(--surface);box-shadow:inset 0 0 0 1px var(--line);opacity:.55;"></span>No data</span>
      </div>`;

    const byCode = {};
    rows.forEach((c) => { byCode[c.csp_code] = c; });
    const wrap = container.querySelector(".heatmap-wrap");
    const tooltip = container.querySelector("#hm-tooltip");

    container.querySelectorAll(".heatmap-tile").forEach((tile) => {
      tile.addEventListener("mouseenter", () => {
        const c = byCode[tile.dataset.code];
        const tileRect = tile.getBoundingClientRect();
        const wrapRect = wrap.getBoundingClientRect();
        const changeLine = c.abs_change != null
          ? `${c.abs_change > 0 ? "+" : ""}${fmt(c.abs_change)}${c.pct_change != null ? ` (${c.pct_change}%)` : ""}`
          : "&mdash;";
        tooltip.innerHTML = `
          <span class="hm-title">${c.csp_name || c.csp_code} &middot; ${c.csp_code}</span>
          <div class="hm-row">${HEATMAP_TREND_LABEL[c.trend] || "No data"} &middot; Slab <strong>${c.current_slab || "&mdash;"}</strong></div>
          <div class="hm-row">Now: <strong>${c.current_value != null ? fmt(c.current_value) : "&mdash;"}</strong> &middot; Prev: <strong>${c.previous_value != null ? fmt(c.previous_value) : "&mdash;"}</strong></div>
          <div class="hm-row">Change: <strong>${changeLine}</strong></div>`;
        tooltip.style.left = (tileRect.left - wrapRect.left + tileRect.width / 2) + "px";
        tooltip.style.top = (tileRect.top - wrapRect.top) + "px";
        tooltip.classList.add("show");
      });
      tile.addEventListener("mouseleave", () => tooltip.classList.remove("show"));
    });
  };
})();
