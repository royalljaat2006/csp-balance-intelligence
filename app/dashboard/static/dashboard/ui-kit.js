/* CSP Balance Tracker — Eko Operations Intelligence shell foundation
   (2026-09-19). New, separate file on purpose: dashboard.js already has a
   documented scar from a duplicate top-level identifier silently killing
   an entire inline <script> block, and this file is new/unproven enough
   that keeping it isolated (its own IIFE, its own file) limits the blast
   radius while it settles. Nothing in here computes a balance, a slab, a
   trend, or any other business number — every value it renders is passed
   in already-computed from server-rendered data or a domain-layer JSON
   payload, per the "one business logic source" rule.

   Covers: toast, drawer, command palette, count-up, sparkline, sidebar
   collapse, the real (cookie-backed) Tracker/Eko mode switch, and the
   notification/user dropdown. All respect prefers-reduced-motion. */
(function () {
  const REDUCED_MOTION = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- CSV export — serializes already-rendered/already-fetched row data
  // (never recomputes anything) into a client-downloaded .csv file. `rows`
  // is a plain array of objects; `columns` is [{key, label}, ...]. ----
  window.exportRowsToCsv = function (filename, rows, columns) {
    const esc = (v) => {
      if (v == null) return "";
      const s = String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const header = columns.map((c) => esc(c.label)).join(",");
    const lines = rows.map((r) => columns.map((c) => esc(r[c.key])).join(","));
    const csv = [header, ...lines].join("\r\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  // ---- cookies (small helper — no dependency) ----
  function setCookie(name, value, days) {
    const maxAge = days ? `; max-age=${days * 86400}` : "";
    document.cookie = `${name}=${value}; path=/${maxAge}`;
  }

  // ---- Toast ------------------------------------------------------------
  let toastStack = null;
  window.toast = function (message, opts) {
    opts = opts || {};
    const type = opts.type || "info";
    const duration = opts.duration || 4000;
    if (!toastStack) {
      toastStack = document.createElement("div");
      toastStack.className = "toast-stack";
      document.body.appendChild(toastStack);
    }
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    toastStack.appendChild(el);
    requestAnimationFrame(() => el.classList.add("show"));
    setTimeout(() => {
      el.classList.remove("show");
      setTimeout(() => el.remove(), REDUCED_MOTION ? 0 : 200);
    }, duration);
  };

  // ---- Drawer (same open/close shape as dashboard.js's openModal, so a
  // caller can treat them interchangeably depending on how much room the
  // content needs) ----
  window.openDrawer = function (title) {
    let overlay = document.getElementById("drawer-overlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "drawer-overlay";
      overlay.className = "drawer-overlay";
      overlay.innerHTML = `
        <div class="drawer-panel" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
          <div class="drawer-head"><h3 id="drawer-title"></h3><button type="button" class="modal-close" aria-label="Close">&times;</button></div>
          <div class="drawer-body" id="drawer-body"></div>
        </div>`;
      document.body.appendChild(overlay);
      overlay.addEventListener("click", (e) => { if (e.target === overlay) window.closeDrawer(); });
      overlay.querySelector(".modal-close").addEventListener("click", window.closeDrawer);
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && overlay.classList.contains("show")) window.closeDrawer();
      });
    }
    overlay.querySelector("#drawer-title").textContent = title;
    const body = overlay.querySelector("#drawer-body");
    body.innerHTML = "";
    overlay.classList.add("show");
    document.body.style.overflow = "hidden";
    return body;
  };
  window.closeDrawer = function () {
    const overlay = document.getElementById("drawer-overlay");
    if (overlay) overlay.classList.remove("show");
    document.body.style.overflow = "";
  };

  // ---- Count-up (KPI numbers) — respects reduced motion by jumping
  // straight to the final value instead of animating. ----
  window.countUp = function (el, value, opts) {
    opts = opts || {};
    const fmt = opts.format || ((n) => Math.round(n).toLocaleString("en-IN"));
    if (value == null) { el.textContent = "—"; return; }
    if (REDUCED_MOTION) { el.textContent = fmt(value); return; }
    const duration = opts.duration || 600;
    const start = performance.now();
    function tick(now) {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
      el.textContent = fmt(value * eased);
      if (t < 1) requestAnimationFrame(tick);
      else el.textContent = fmt(value);
    }
    requestAnimationFrame(tick);
  };

  // ---- Sparkline — a minimal single-path trend line for inside a KPI
  // card, deliberately simpler than dashboard.js's full renderTrendChart
  // (no axes/tooltip/gridlines — a sparkline's whole point is "shape at a
  // glance," not a readable chart). `values` is a plain number array. ----
  window.renderSparkline = function (container, values, opts) {
    opts = opts || {};
    if (!values || values.length < 2) { container.innerHTML = ""; return; }
    const W = 100, H = 28;
    const min = Math.min(...values), max = Math.max(...values);
    const range = max - min || 1;
    const step = W / (values.length - 1);
    const points = values.map((v, i) => `${(i * step).toFixed(1)},${(H - ((v - min) / range) * H).toFixed(1)}`);
    const path = "M" + points.join(" L");
    const color = opts.color || "var(--brand)";
    container.innerHTML = `<svg class="sparkline" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" preserveAspectRatio="none" aria-hidden="true">
      <path d="${path}" style="stroke:${color}"></path>
    </svg>`;
  };

  // ---- Sidebar collapse (desktop) + mobile slide-in ----
  (function initSidebar() {
    const sidebar = document.getElementById("app-sidebar");
    if (!sidebar) return;
    const collapseBtn = document.getElementById("sidebar-collapse-btn");
    if (collapseBtn) {
      // Icon-only by default (matches the reference's slim nav rail) — a
      // saved "0" means the user explicitly expanded it before, so that
      // choice sticks across visits.
      let collapsed = true;
      try { collapsed = localStorage.getItem("sidebar-collapsed") !== "0"; } catch (e) {}
      if (collapsed) sidebar.classList.add("collapsed");
      collapseBtn.addEventListener("click", () => {
        collapsed = !collapsed;
        sidebar.classList.toggle("collapsed", collapsed);
        try { localStorage.setItem("sidebar-collapsed", collapsed ? "1" : "0"); } catch (e) {}
      });
    }
    const mobileToggle = document.getElementById("sidebar-mobile-toggle");
    const scrim = document.getElementById("sidebar-scrim");
    if (mobileToggle && scrim) {
      const close = () => { sidebar.classList.remove("mobile-open"); scrim.classList.remove("show"); };
      mobileToggle.addEventListener("click", () => {
        sidebar.classList.add("mobile-open");
        scrim.classList.add("show");
      });
      scrim.addEventListener("click", close);
    }
  })();

  // ---- Real Tracker/Eko mode switch — sets BOTH a cookie (so the next
  // server render picks the mode up for terminology/copy, not just CSS)
  // and localStorage (so base.html's pre-paint <head> script can restore
  // the right data-theme before first paint, avoiding a flash). Changing
  // mode reloads the page: server-rendered copy can't update any other
  // way, and a reload is the honest signal that "mode" is a real
  // dimension of the page, not a cosmetic overlay. ----
  (function initModeSwitch() {
    const buttons = document.querySelectorAll(".mode-switch [data-mode]");
    if (!buttons.length) return;
    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const mode = btn.dataset.mode;
        if (btn.classList.contains("active")) return;
        setCookie("csp_mode", mode, 365);
        try {
          if (mode === "eko") localStorage.setItem("eko-theme", "1");
          else localStorage.removeItem("eko-theme");
        } catch (e) {}
        window.location.reload();
      });
    });
  })();

  // ---- Generic dropdown (notifications / user menu) ----
  document.querySelectorAll("[data-dropdown-trigger]").forEach((trigger) => {
    const panel = document.getElementById(trigger.dataset.dropdownTrigger);
    if (!panel) return;
    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      const willShow = !panel.classList.contains("show");
      document.querySelectorAll(".dropdown-panel.show").forEach((p) => p.classList.remove("show"));
      if (willShow) panel.classList.add("show");
    });
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".dropdown")) {
      document.querySelectorAll(".dropdown-panel.show").forEach((p) => p.classList.remove("show"));
    }
  });

  // ---- Command palette — a static, page-supplied list of destinations
  // (real nav routes only, never fabricated ones) with a simple substring
  // filter. Opens on Ctrl+K / Cmd+K or the header button. ----
  window.initCommandPalette = function (items) {
    let overlay = null, input = null, list = null, activeIndex = 0;

    function render(query) {
      const q = query.trim().toLowerCase();
      const matches = items.filter((it) => it.label.toLowerCase().includes(q) || (it.group || "").toLowerCase().includes(q));
      activeIndex = 0;
      list.innerHTML = matches.length
        ? matches.map((it, i) => `
            <a class="cmdk-item${i === 0 ? " active" : ""}" href="${it.href}" data-i="${i}">
              <span>${it.label}</span>
              ${it.group ? `<span class="kbd">${it.group}</span>` : ""}
            </a>`).join("")
        : `<div class="cmdk-empty">No matching page or action.</div>`;
      list.querySelectorAll("a").forEach((a) => a.addEventListener("mouseenter", () => setActive(+a.dataset.i)));
    }
    function setActive(i) {
      const els = list.querySelectorAll(".cmdk-item");
      els.forEach((el) => el.classList.remove("active"));
      if (els[i]) { els[i].classList.add("active"); activeIndex = i; }
    }
    function open() {
      if (!overlay) {
        overlay = document.createElement("div");
        overlay.className = "cmdk-overlay";
        overlay.innerHTML = `
          <div class="cmdk-box">
            <input class="cmdk-input" type="text" placeholder="Jump to a page or action…" autocomplete="off">
            <div class="cmdk-list"></div>
          </div>`;
        document.body.appendChild(overlay);
        input = overlay.querySelector(".cmdk-input");
        list = overlay.querySelector(".cmdk-list");
        overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
        input.addEventListener("input", () => render(input.value));
        input.addEventListener("keydown", (e) => {
          const els = list.querySelectorAll(".cmdk-item");
          if (e.key === "ArrowDown") { e.preventDefault(); setActive(Math.min(activeIndex + 1, els.length - 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setActive(Math.max(activeIndex - 1, 0)); }
          else if (e.key === "Enter" && els[activeIndex]) { window.location.href = els[activeIndex].getAttribute("href"); }
          else if (e.key === "Escape") close();
        });
      }
      render("");
      overlay.classList.add("show");
      input.value = "";
      setTimeout(() => input.focus(), 0);
    }
    function close() { if (overlay) overlay.classList.remove("show"); }

    document.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); open(); }
    });
    document.querySelectorAll("[data-cmdk-trigger]").forEach((btn) => btn.addEventListener("click", open));
  };
})();
