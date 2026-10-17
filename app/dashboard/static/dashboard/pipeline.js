/* Live pipeline: consumes the real server-sent events (dashboard/pipeline_sse.py).
   Every value shown comes from a pushed event or the server-rendered first snapshot; nothing here
   invents a number, a timer or a status change. All text goes in through textContent. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var lastEventAt = null, reconnectTimer = null, historyPage = 1, historyNode = null;
  var LABELS = { idle: "Idle", running: "Running", queued: "Queued", completed: "Completed", warning: "Warning",
    failed: "Failed", waiting_approval: "Waiting for approval", not_configured: "Not configured" };
  var TONE = { running: "info", queued: "info", completed: "ok", warning: "warn", failed: "bad",
    waiting_approval: "warn", not_configured: "", idle: "" };

  function ago(iso) {
    if (!iso) return "never";
    var s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (s < 5) return "just now";
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    return Math.round(s / 3600) + "h ago";
  }
  function connection(state) {
    var pill = $("pl-connection"), text = $("pl-connection-text");
    pill.className = "fresh " + (state === "live" ? "ok" : state === "lost" ? "bad" : "warn");
    text.textContent = state === "live" ? "Live. Last event " + ago(lastEventAt)
      : state === "lost" ? "Connection lost. Last update " + ago(lastEventAt) + ". Reconnecting..." : "Connecting...";
  }
  function records(r) { return Object.keys(r || {}).map(function (k) { return k + ": " + r[k]; }).join(", "); }

  function applyNode(n) {
    var el = document.querySelector('.node[data-node="' + n.key + '"]');
    if (!el) return;
    el.className = "node status-" + n.status;
    var badge = el.querySelector(".pl-status-badge");
    badge.className = "badge pl-status-badge " + (TONE[n.status] || "");
    el.querySelector(".pl-status-text").textContent = LABELS[n.status] || n.status;
    var parts = [];
    if (n.status === "not_configured") parts.push("Disabled: no credentials configured");
    else if (n.status === "running" && n.last_run_at) parts.push("Started " + ago(n.last_run_at));
    else if (n.last_run_at) { parts.push("Last run " + ago(n.last_run_at)); if (n.last_duration_seconds != null) parts.push(n.last_duration_seconds + "s"); }
    else parts.push("No run recorded yet");
    if (n.records && records(n.records)) parts.push(records(n.records));
    if (n.error) parts.push("Error: " + n.error);
    el.querySelector(".pl-node-meta").textContent = parts.join(" · ");
    var bar = el.querySelector(".bar");
    if (n.status === "running" && n.progress && n.progress.total) {
      var pct = Math.min(100, Math.round(n.progress.processed / n.progress.total * 100));
      if (!bar) { bar = document.createElement("span"); bar.className = "bar"; bar.appendChild(document.createElement("i")); el.appendChild(bar); }
      bar.firstChild.style.width = pct + "%";
      bar.title = n.progress.processed.toLocaleString() + " of " + n.progress.total.toLocaleString() + " (" + pct + "%)";
    } else if (bar) bar.remove();
    if (n.key === "actions") {
      var pending = n.status === "waiting_approval" && n.records ? n.records.pending_approval : 0;
      $("pl-approval-banner").hidden = !pending;
      if (pending) $("pl-approval-banner-text").textContent = pending + " action" + (pending === 1 ? "" : "s") + " waiting.";
    }
  }
  function counters(c) {
    $("pl-counter-csps").textContent = c.csps_processed_today;
    $("pl-counter-txns").textContent = c.transactions_processed_today;
    $("pl-counter-snapshots").textContent = c.snapshots_generated_today;
    $("pl-counter-jobs").textContent = c.active_jobs + " / " + c.queued_jobs;
    $("pl-counter-failed").textContent = c.failed_jobs_today;
    $("pl-counter-findings").textContent = c.ai_findings_today;
    $("pl-counter-approvals").textContent = c.pending_approvals;
    $("pl-counter-total-csps").textContent = c.total_csps;
  }
  function currentOperation(op) {
    var box = $("pl-current-operation");
    box.replaceChildren();
    if (!op || !op.node) { var p = document.createElement("p"); p.className = "muted"; p.textContent = "No active operation."; box.appendChild(p); return; }
    var rows = [["Operation", op.node], ["Status", op.status], ["Started", op.started_at ? new Date(op.started_at).toLocaleString() : "—"]];
    if (op.status === "running") {
      rows.push(["Elapsed", op.elapsed_seconds != null ? op.elapsed_seconds + "s" : "—"]);
      if (op.progress && op.progress.total) rows.push(["Progress", op.progress.processed.toLocaleString() + " of " + op.progress.total.toLocaleString()]);
    } else rows.push(["Duration", op.duration_seconds != null ? op.duration_seconds + "s" : "—"]);
    Object.keys(op.records || {}).forEach(function (k) { rows.push([k, op.records[k]]); });
    if (op.error) rows.push(["Error", op.error]);
    var dl = document.createElement("dl"); dl.className = "kv";
    rows.forEach(function (r) { var dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = r[0]; dd.textContent = r[1]; dl.appendChild(dt); dl.appendChild(dd); });
    box.appendChild(dl);
  }
  var ICON = { ingestion_started: "●", ingestion_completed: "✓", ingestion_failed: "✕", agent_started: "●", agent_completed: "✓",
    agent_failed: "✕", agent_finding: "◆", verification_completed: "✓", action_created: "●", action_pending_approval: "⏸" };
  function addEntry(e) {
    var list = $("pl-timeline"), li = document.createElement("li");
    var t = document.createElement("span"); t.className = "mono"; t.textContent = new Date(e.ts).toLocaleTimeString();
    var i = document.createElement("span"); i.textContent = ICON[e.kind] || "●"; i.setAttribute("aria-hidden", "true");
    var x = document.createElement("span"); x.textContent = e.text;
    li.appendChild(t); li.appendChild(i); li.appendChild(x); list.prepend(li);
    while (list.children.length > 40) list.removeChild(list.lastChild);
  }
  function timeline(entries) {
    var list = $("pl-timeline"); list.replaceChildren();
    if (!entries.length) { var li = document.createElement("li"); li.className = "muted"; li.textContent = "No recent activity."; list.appendChild(li); return; }
    entries.forEach(addEntry);
  }
  function snapshot(s) {
    s.nodes.forEach(applyNode); counters(s.counters); currentOperation(s.current_operation);
    $("pl-generated-at").textContent = "As of " + new Date(s.generated_at).toLocaleTimeString() + ".";
  }
  function connect() {
    connection("connecting");
    var source = new EventSource(window.PIPELINE_URLS.events);
    function seen() { lastEventAt = new Date().toISOString(); }
    source.addEventListener("sync", function (e) { seen(); snapshot(JSON.parse(e.data)); connection("live"); });
    source.addEventListener("timeline", function (e) { timeline(JSON.parse(e.data)); });
    source.addEventListener("node_update", function (e) { seen(); applyNode(JSON.parse(e.data)); });
    source.addEventListener("counters", function (e) { seen(); counters(JSON.parse(e.data)); });
    source.addEventListener("current_operation", function (e) { seen(); currentOperation(JSON.parse(e.data)); });
    source.addEventListener("activity", function (e) { seen(); addEntry(JSON.parse(e.data)); });
    source.addEventListener("heartbeat", function () { seen(); connection("live"); });
    source.onerror = function () {
      // the stream closes itself every ~20 s by design; the browser reconnects, we only say so
      connection("lost"); source.close(); clearTimeout(reconnectTimer); reconnectTimer = setTimeout(connect, 1000);
    };
  }
  setInterval(function () { if (lastEventAt && $("pl-connection").className.indexOf("bad") < 0) connection("live"); }, 1000);

  function loadHistory() {
    var params = new URLSearchParams({ page: historyPage }); if (historyNode) params.set("node", historyNode);
    fetch(window.PIPELINE_URLS.history + "?" + params.toString(), { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
      var tb = $("pl-history-rows"); tb.replaceChildren();
      d.rows.forEach(function (row) {
        var tr = document.createElement("tr");
        [new Date(row.started_at).toLocaleString(), row.label, row.status || "—", row.duration_seconds != null ? row.duration_seconds + "s" : "—",
         row.records ? records(row.records) : (row.error || "—")].forEach(function (v, i) {
          var td = document.createElement("td"); td.textContent = v; if (i === 3) td.className = "num"; tr.appendChild(td);
        });
        tb.appendChild(tr);
      });
      $("pl-history-page-label").textContent = "Page " + d.page + " of " + d.total_pages + " (" + d.total + " total)";
    });
  }
  function mode(history) {
    $("pl-mode-live").setAttribute("aria-pressed", history ? "false" : "true");
    $("pl-mode-history").setAttribute("aria-pressed", history ? "true" : "false");
    $("pl-history-panel").hidden = !history;
    if (history) { historyNode = null; historyPage = 1; loadHistory(); }
  }
  $("pl-mode-live").addEventListener("click", function () { mode(false); });
  $("pl-mode-history").addEventListener("click", function () { mode(true); });
  $("pl-history-prev").addEventListener("click", function () { if (historyPage > 1) { historyPage -= 1; loadHistory(); } });
  $("pl-history-next").addEventListener("click", function () { historyPage += 1; loadHistory(); });

  var dialog = $("pl-dialog");
  $("pl-dialog-close").addEventListener("click", function () { dialog.close(); });
  document.querySelectorAll(".node").forEach(function (el) {
    el.addEventListener("click", function () {
      var url = window.PIPELINE_URLS.nodeDetail.replace("__NODE__", el.dataset.node);
      fetch(url, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (data) {
        $("pl-dialog-title").textContent = el.querySelector(".pl-node-label").textContent;
        var body = $("pl-dialog-body"); body.replaceChildren();
        if (!data.latest) { var p = document.createElement("p"); p.className = "muted"; p.textContent = "No run recorded for this job yet."; body.appendChild(p); }
        else {
          var t = document.createElement("table"); t.className = "t";
          var head = document.createElement("tr"); ["When", "Status", "Duration", "Detail"].forEach(function (h) { var th = document.createElement("th"); th.textContent = h; head.appendChild(th); });
          var thead = document.createElement("thead"); thead.appendChild(head); t.appendChild(thead);
          var tb = document.createElement("tbody");
          data.recent.forEach(function (r) {
            var tr = document.createElement("tr");
            [new Date(r.started_at).toLocaleString(), r.status, r.duration_seconds != null ? r.duration_seconds + "s" : "—", r.records ? records(r.records) : (r.error || "")]
              .forEach(function (v) { var td = document.createElement("td"); td.textContent = v; tr.appendChild(td); });
            tb.appendChild(tr);
          });
          t.appendChild(tb); var w = document.createElement("div"); w.className = "table-wrap"; w.appendChild(t); body.appendChild(w);
        }
        dialog.showModal();
      });
    });
  });

  snapshot(JSON.parse($("pipeline-initial-snapshot").textContent));
  timeline(JSON.parse($("pipeline-initial-timeline").textContent));
  connect();
})();
