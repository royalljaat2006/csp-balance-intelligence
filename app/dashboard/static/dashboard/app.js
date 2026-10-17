/* CSP Balance Intelligence: front-end behaviour. No framework, no build step.
   Nothing here computes a balance, slab or estimate; it formats and draws what the server sends. */
(function () {
  "use strict";
  var doc = document;
  var $ = function (s, r) { return (r || doc).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || doc).querySelectorAll(s)); };
  var NS = "http://www.w3.org/2000/svg";

  /* ---------- formatting (Indian digit grouping, full rupee amounts) ---------- */
  function group(n) {
    var s = String(Math.abs(Math.round(n)));
    if (s.length > 3) {
      var tail = s.slice(-3), head = s.slice(0, -3), parts = [];
      while (head.length > 2) { parts.unshift(head.slice(-2)); head = head.slice(0, -2); }
      if (head) parts.unshift(head);
      s = parts.join(",") + "," + tail;
    }
    return (n < 0 ? "−" : "") + s;
  }
  function fmt(v, unit) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    if (unit === "inr") return (v < 0 ? "−" : "") + "₹" + group(Math.abs(v));
    if (unit === "inr2") { var a = Math.abs(v).toFixed(2).split("."); return (v < 0 ? "−" : "") + "₹" + group(+a[0]) + "." + a[1]; }
    if (unit === "pct") return v.toFixed(1) + "%";
    return group(v);
  }
  function dateLabel(iso, long) {
    var d = new Date(iso + "T00:00:00");
    if (isNaN(d)) return iso;
    return d.toLocaleDateString("en-IN", long ? { weekday: "short", day: "numeric", month: "short", year: "numeric" } : { day: "numeric", month: "short" });
  }
  window.UI = { fmt: fmt, group: group };

  /* ---------- shell ---------- */
  var nav = $("#nav"), scrim = $("#scrim"), menuBtn = $("#menu-btn");
  function setNav(open) {
    if (!nav) return;
    nav.classList.toggle("open", open); scrim.classList.toggle("open", open);
    doc.body.classList.toggle("scroll-lock", open);
    if (menuBtn) menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (menuBtn) menuBtn.addEventListener("click", function () { setNav(!nav.classList.contains("open")); });
  if (scrim) scrim.addEventListener("click", function () { setNav(false); });
  doc.addEventListener("keydown", function (e) { if (e.key === "Escape") { setNav(false); closeMenus(); } });

  function closeMenus(except) {
    $$("[data-menu-panel]").forEach(function (p) {
      if (p !== except) { p.hidden = true; var b = p.parentNode.querySelector("[data-menu-btn]"); if (b) b.setAttribute("aria-expanded", "false"); }
    });
  }
  $$("[data-menu-btn]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var panel = btn.parentNode.querySelector("[data-menu-panel]");
      var willOpen = panel.hidden; closeMenus(panel); panel.hidden = !willOpen;
      btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
    });
  });
  doc.addEventListener("click", function (e) { if (!e.target.closest("[data-menu-panel]")) closeMenus(); });

  var themeBtn = $("#theme-toggle");
  if (themeBtn) themeBtn.addEventListener("click", function () {
    var next = doc.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    doc.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("csp-theme", next); } catch (err) { /* private mode: still applies for this page */ }
    themeBtn.textContent = next === "dark" ? "Switch to light theme" : "Switch to dark theme";
    $$("[data-chart]").forEach(function (el) { el._draw && el._draw(); });
  });

  // the review count is computed after the page when it was not cached yet
  var shellUrl = doc.body.getAttribute("data-shell-url");
  if (shellUrl) {
    fetch(shellUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var c = $("#review-count");
        if (c && d.attention_count) { c.textContent = d.attention_count; c.hidden = false; }
        var slot = $("#dc-slot");
        if (slot && d.strip_html) { slot.outerHTML = d.strip_html; }   // server-rendered, trusted
        else if (slot) { slot.hidden = true; }
      }).catch(function () { /* the badge is a convenience; the page works without it */ });
  }

  if (window.matchMedia && window.matchMedia("(max-width: 860px)").matches) {
    $$("details[data-collapse-on-phone]").forEach(function (d) { d.removeAttribute("open"); });
  }
  $$("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) { if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault(); });
  });
  $$("select[data-autosubmit]").forEach(function (s) { s.addEventListener("change", function () { s.form.submit(); }); });

  /* ---------- charts ---------- */
  function el(name, attrs, parent) {
    var n = doc.createElementNS(NS, name);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(n);
    return n;
  }
  function niceTicks(min, max, n) {
    if (min === max) { max = min + 1; }
    var span = max - min, step = Math.pow(10, Math.floor(Math.log10(span / n)));
    var err = (span / n) / step;
    step *= err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1;
    var lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step, t = [];
    for (var v = lo; v <= hi + step / 2; v += step) t.push(+v.toFixed(6));
    return t;
  }
  function colorVar(c) { return /^s\d$/.test(c) ? "var(--" + c + ")" : c; }

  function drawChart(host, spec, tipBox) {
    var series = spec.series.filter(function (s) { return s.points && s.points.length; });
    host.querySelectorAll("svg").forEach(function (n) { n.remove(); });
    var xs = [];
    series.forEach(function (s) { s.points.forEach(function (p) { if (xs.indexOf(p[0]) < 0) xs.push(p[0]); }); });
    xs.sort();
    var W = Math.max(host.clientWidth, 280), H = spec.height || 260;
    var m = { l: 76, r: 14, t: 10, b: 28 };
    var vals = [];
    series.forEach(function (s) { s.points.forEach(function (p) { if (p[1] !== null && p[1] !== undefined) vals.push(p[1]); }); });
    var bars = spec.type === "bars";
    var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals);
    if (bars || spec.zero) { min = Math.min(min, 0); max = Math.max(max, 0); }
    else { var pad = (max - min) * .08 || Math.abs(max) * .05 || 1; min -= pad; max += pad; }
    var ticks = niceTicks(min, max, 5); min = ticks[0]; max = ticks[ticks.length - 1];
    var iw = W - m.l - m.r, ih = H - m.t - m.b;
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": spec.label || "Chart", tabindex: 0 }, host);
    var y = function (v) { return m.t + ih - (v - min) / (max - min) * ih; };
    var step = xs.length > 1 ? iw / (xs.length - 1) : 0;
    var x = function (i) { return bars ? m.l + (i + .5) * (iw / xs.length) : m.l + (xs.length > 1 ? i * step : iw / 2); };

    ticks.forEach(function (t) {
      el("line", { x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), class: t === 0 && min < 0 ? "axis-line" : "grid-line" }, svg);
      var tx = el("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end" }, svg); tx.textContent = fmt(t, spec.unit);
    });
    var every = Math.max(1, Math.ceil(xs.length / Math.max(2, Math.floor(iw / 78))));
    xs.forEach(function (d, i) {
      if (i % every === 0) { var lt = el("text", { x: x(i), y: H - 8, "text-anchor": "middle" }, svg); lt.textContent = spec.categorical ? d : dateLabel(d); }
    });
    el("line", { x1: m.l, x2: W - m.r, y1: y(Math.max(min, 0)), y2: y(Math.max(min, 0)), class: "axis-line" }, svg);

    var idx = function (xv) { return xs.indexOf(xv); };
    if (bars) {
      var gw = iw / xs.length, bw = Math.max(2, Math.min(28, (gw - 6) / series.length - 2));
      series.forEach(function (s, si) {
        s.points.forEach(function (p) {
          if (p[1] === null || p[1] === undefined) return;
          var i = idx(p[0]), x0 = x(i) - (series.length * (bw + 2) - 2) / 2 + si * (bw + 2);
          var top = Math.min(y(p[1]), y(0)), h = Math.max(1, Math.abs(y(p[1]) - y(0)));
          el("rect", { x: x0, y: top, width: bw, height: h, rx: 2, fill: colorVar(s.color) }, svg);
        });
      });
    } else {
      series.forEach(function (s) {
        var d = "", pen = false, last = null;
        s.points.slice().sort(function (a, b) { return a[0] < b[0] ? -1 : 1; }).forEach(function (p) {
          if (p[1] === null || p[1] === undefined) { pen = false; last = null; return; }
          var px = x(idx(p[0])), py = y(p[1]);
          if (!pen) d += "M" + px + " " + py; else d += (s.step ? "H" + px + "V" + py : "L" + px + " " + py);
          pen = true; last = [px, py];
        });
        el("path", { d: d, class: "line", stroke: colorVar(s.color), "stroke-dasharray": s.dash ? "5 4" : "none" }, svg);
        if (s.points.length === 1 && last) el("circle", { cx: last[0], cy: last[1], r: 4, fill: colorVar(s.color) }, svg);
      });
    }

    var cross = el("line", { y1: m.t, y2: m.t + ih, class: "cross", visibility: "hidden" }, svg);
    var overlay = el("rect", { x: m.l, y: m.t, width: iw, height: ih, class: "hit" }, svg);
    var cur = -1;
    function show(i) {
      if (i < 0 || i >= xs.length) return;
      cur = i; var xv = xs[i];
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", bars ? "hidden" : "visible");
      tipBox.replaceChildren();
      var when = doc.createElement("div"); when.className = "when"; when.textContent = spec.categorical ? xv : dateLabel(xv, true); tipBox.appendChild(when);
      series.forEach(function (s) {
        var p = s.points.filter(function (q) { return q[0] === xv; })[0];
        var row = doc.createElement("div"); row.className = "r";
        var nm = doc.createElement("span"), key = doc.createElement("i"); key.style.background = colorVar(s.color);
        nm.appendChild(key); nm.appendChild(doc.createTextNode(s.name)); row.appendChild(nm);
        var b = doc.createElement("b"); b.textContent = p ? fmt(p[1], spec.unit) : "no value"; row.appendChild(b); tipBox.appendChild(row);
      });
      tipBox.hidden = false;
      var left = Math.min(Math.max(x(i) + 12, 4), W - tipBox.offsetWidth - 4);
      if (x(i) + 12 + tipBox.offsetWidth > W) left = x(i) - tipBox.offsetWidth - 12;
      tipBox.style.left = Math.max(4, left) + "px"; tipBox.style.top = (m.t + 6) + "px";
    }
    function hide() { tipBox.hidden = true; cross.setAttribute("visibility", "hidden"); }
    overlay.addEventListener("pointermove", function (e) {
      var r = svg.getBoundingClientRect(), px = (e.clientX - r.left) * (W / r.width);
      var best = 0, bd = 1e9;
      xs.forEach(function (_, i) { var dd = Math.abs(x(i) - px); if (dd < bd) { bd = dd; best = i; } });
      show(best);
    });
    overlay.addEventListener("pointerleave", hide);
    svg.addEventListener("focus", function () { show(cur < 0 ? xs.length - 1 : cur); });
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft") { show(Math.max(0, (cur < 0 ? xs.length : cur) - 1)); e.preventDefault(); }
      if (e.key === "ArrowRight") { show(Math.min(xs.length - 1, cur + 1)); e.preventDefault(); }
    });
    return xs;
  }

  function buildTable(holder, spec, xs) {
    holder.replaceChildren();
    var t = doc.createElement("table"); t.className = "t";
    var cap = doc.createElement("caption"); cap.textContent = (spec.label || "Chart") + " (values)"; t.appendChild(cap);
    var head = doc.createElement("tr"), th0 = doc.createElement("th"); th0.textContent = spec.categorical ? "Item" : "Date"; head.appendChild(th0);
    spec.series.forEach(function (s) { var th = doc.createElement("th"); th.className = "num"; th.textContent = s.name; head.appendChild(th); });
    var thead = doc.createElement("thead"); thead.appendChild(head); t.appendChild(thead);
    var tb = doc.createElement("tbody");
    xs.forEach(function (xv) {
      var tr = doc.createElement("tr"), td0 = doc.createElement("td"); td0.textContent = spec.categorical ? xv : dateLabel(xv, true); tr.appendChild(td0);
      spec.series.forEach(function (s) {
        var p = (s.points || []).filter(function (q) { return q[0] === xv; })[0];
        var td = doc.createElement("td"); td.className = "num"; td.textContent = p ? fmt(p[1], spec.unit) : "—"; tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    t.appendChild(tb); holder.appendChild(t);
  }

  function initChart(host) {
    var src = doc.getElementById(host.getAttribute("data-chart"));
    var spec; try { spec = JSON.parse(src.textContent); } catch (err) { return; }
    var withData = spec.series.filter(function (s) { return s.points && s.points.length; });
    host.replaceChildren();
    if (!withData.length) {
      var e = doc.createElement("div"); e.className = "chart-empty"; e.textContent = spec.empty || "No data for this period yet."; host.appendChild(e); return;
    }
    if (spec.series.length > 1) {
      var lg = doc.createElement("ul"); lg.className = "legend";
      spec.series.forEach(function (s) {
        var li = doc.createElement("li"), k = doc.createElement("i"); k.style.background = colorVar(s.color);
        if (spec.type === "bars") k.className = "box";
        li.appendChild(k); li.appendChild(doc.createTextNode(s.name)); lg.appendChild(li);
      });
      host.appendChild(lg);
    }
    var wrap = doc.createElement("div"); wrap.className = "chart"; host.appendChild(wrap);
    var tip = doc.createElement("div"); tip.className = "tip"; tip.hidden = true; tip.setAttribute("aria-hidden", "true"); wrap.appendChild(tip);
    var det = doc.createElement("details"); det.className = "chart-table";
    var sm = doc.createElement("summary"); sm.className = "small muted"; sm.style.cursor = "pointer"; sm.textContent = "Show as a table"; det.appendChild(sm);
    var tbl = doc.createElement("div"); tbl.style.overflowX = "auto"; det.appendChild(tbl); host.appendChild(det);
    var xs;
    host._draw = function () { xs = drawChart(wrap, spec, tip); };
    host._draw();
    det.addEventListener("toggle", function () { if (det.open) buildTable(tbl, spec, xs); });
    if (window.ResizeObserver) {
      var last = wrap.clientWidth;
      new ResizeObserver(function () { if (Math.abs(wrap.clientWidth - last) > 8) { last = wrap.clientWidth; host._draw(); } }).observe(wrap);
    }
  }
  $$("[data-chart]").forEach(initChart);
})();
