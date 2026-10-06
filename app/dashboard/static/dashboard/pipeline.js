/* Live Data Pipeline — consumes real SSE events from the server (see
 * dashboard/pipeline_sse.py). Every value rendered here comes from a
 * server-pushed event or the initial server-rendered snapshot; nothing in
 * this file invents a number, a timer, or a status transition. */
(function () {
  "use strict";

  const NODE_KEYS = [
    "calling_sheet", "transactions", "telegram", "postgres", "snapshots",
    "balance_agent", "transaction_agent", "risk_agent", "findings", "actions",
  ];
  const CONNECTOR_MAP = {
    calling_sheet: ["conn-cs-pg"], transactions: ["conn-tx-pg"], telegram: ["conn-tg-pg"],
    postgres: ["conn-pg-snap"], snapshots: ["conn-snap-bal", "conn-snap-tx", "conn-snap-risk"],
    balance_agent: ["conn-bal-find"], transaction_agent: ["conn-tx-find"], risk_agent: ["conn-risk-find"],
    findings: ["conn-find-act"], actions: [],
  };

  let lastEventAt = null;
  let reconnectTimer = null;
  let historyPage = 1;
  let historyNode = null;

  function fmtAgo(iso) {
    if (!iso) return "never";
    const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (seconds < 5) return "just now";
    if (seconds < 60) return seconds + "s ago";
    if (seconds < 3600) return Math.round(seconds / 60) + "m ago";
    return Math.round(seconds / 3600) + "h ago";
  }

  function setConnectionState(state) {
    const pill = document.getElementById("pl-connection");
    const text = document.getElementById("pl-connection-text");
    pill.className = "freshness-pill " + state;
    if (state === "live") {
      text.textContent = "LIVE — last event: " + fmtAgo(lastEventAt);
    } else if (state === "lost") {
      text.innerHTML = "⚠ LIVE CONNECTION LOST — last update: " + fmtAgo(lastEventAt) + " — reconnecting…";
    } else {
      text.textContent = "Connecting…";
    }
  }

  function applyNode(node) {
    const el = document.querySelector('.pl-node[data-node="' + node.key + '"]');
    if (!el) return;
    el.dataset.status = node.status;
    el.className = "pl-node status-" + node.status;
    const statusText = el.querySelector(".pl-status-text");
    const meta = el.querySelector(".pl-node-meta");
    const labels = {
      idle: "Idle", running: "Running", queued: "Queued", completed: "Completed",
      warning: "Warning", failed: "Failed", waiting_approval: "Waiting approval",
    };
    statusText.textContent = labels[node.status] || node.status;

    const parts = [];
    if (node.status === "running" && node.last_run_at) {
      parts.push("started " + fmtAgo(node.last_run_at));
    } else if (node.last_run_at) {
      parts.push("last run " + fmtAgo(node.last_run_at));
      if (node.last_duration_seconds != null) parts.push(node.last_duration_seconds + "s");
    } else {
      parts.push("no execution recorded yet");
    }
    if (node.records) {
      const recordBits = Object.keys(node.records).map((k) => k + ": " + node.records[k]);
      if (recordBits.length) parts.push(recordBits.join(", "));
    }
    if (node.error) parts.push("error: " + node.error);
    meta.textContent = parts.join(" · ");

    (CONNECTOR_MAP[node.key] || []).forEach((connId) => {
      const conn = document.getElementById(connId);
      if (!conn) return;
      conn.classList.toggle("flowing", node.status === "running");
    });
  }

  function applyCounters(counters) {
    document.getElementById("pl-counter-csps").textContent = counters.csps_processed_today;
    document.getElementById("pl-counter-txns").textContent = counters.transactions_processed_today;
    document.getElementById("pl-counter-snapshots").textContent = counters.snapshots_generated_today;
    document.getElementById("pl-counter-jobs").textContent = counters.active_jobs + " / " + counters.queued_jobs;
    document.getElementById("pl-counter-failed").textContent = counters.failed_jobs_today;
    document.getElementById("pl-counter-findings").textContent = counters.ai_findings_today;
    document.getElementById("pl-counter-approvals").textContent = counters.pending_approvals;
    document.getElementById("pl-counter-total-csps").textContent = counters.total_csps;
  }

  function applyCurrentOperation(op) {
    const el = document.getElementById("pl-current-operation");
    if (!op || !op.node) {
      el.innerHTML = '<p class="sub">No active operation.</p>';
      return;
    }
    const rows = [
      ["Operation", op.node],
      ["Status", op.status],
      ["Started", op.started_at ? new Date(op.started_at).toLocaleString() : "—"],
    ];
    if (op.status === "running") {
      rows.push(["Elapsed", (op.elapsed_seconds != null ? op.elapsed_seconds + "s" : "—")]);
    } else {
      rows.push(["Duration", (op.duration_seconds != null ? op.duration_seconds + "s" : "—")]);
    }
    if (op.records) {
      Object.keys(op.records).forEach((k) => rows.push([k, op.records[k]]));
    }
    if (op.error) rows.push(["Error", op.error]);
    el.innerHTML = '<div class="kv-grid">' + rows.map(
      (r) => '<div class="kv"><span class="k">' + r[0] + '</span><span class="v">' + r[1] + '</span></div>'
    ).join("") + "</div>";
  }

  const EVENT_ICONS = {
    ingestion_started: "●", ingestion_completed: "✓", ingestion_failed: "✕",
    agent_started: "●", agent_completed: "✓", agent_failed: "✕",
    agent_finding: "◆", verification_completed: "✓",
    action_created: "●", action_pending_approval: "⏸",
  };

  function prependTimelineEntry(entry) {
    const list = document.getElementById("pl-timeline");
    const row = document.createElement("div");
    row.className = "pipeline-timeline-row";
    const time = new Date(entry.ts).toLocaleTimeString();
    row.innerHTML = '<span class="mono">' + time + '</span><span class="pl-timeline-icon">' +
      (EVENT_ICONS[entry.kind] || "●") + '</span><span>' + entry.text + '</span>';
    list.prepend(row);
    while (list.children.length > 40) list.removeChild(list.lastChild);
  }

  function renderTimeline(entries) {
    const list = document.getElementById("pl-timeline");
    list.innerHTML = "";
    if (!entries.length) {
      list.innerHTML = '<p class="sub">No recent activity.</p>';
      return;
    }
    entries.forEach(prependTimelineEntry);
  }

  function applySnapshot(snapshot) {
    snapshot.nodes.forEach(applyNode);
    applyCounters(snapshot.counters);
    applyCurrentOperation(snapshot.current_operation);
    document.getElementById("pl-generated-at").textContent =
      "as of " + new Date(snapshot.generated_at).toLocaleTimeString();
  }

  function connect() {
    setConnectionState("connecting");
    const source = new EventSource(window.PIPELINE_URLS.events);

    source.addEventListener("sync", (e) => {
      lastEventAt = new Date().toISOString();
      applySnapshot(JSON.parse(e.data));
      setConnectionState("live");
    });
    source.addEventListener("timeline", (e) => renderTimeline(JSON.parse(e.data)));
    source.addEventListener("node_update", (e) => { lastEventAt = new Date().toISOString(); applyNode(JSON.parse(e.data)); });
    source.addEventListener("counters", (e) => { lastEventAt = new Date().toISOString(); applyCounters(JSON.parse(e.data)); });
    source.addEventListener("current_operation", (e) => { lastEventAt = new Date().toISOString(); applyCurrentOperation(JSON.parse(e.data)); });
    source.addEventListener("activity", (e) => { lastEventAt = new Date().toISOString(); prependTimelineEntry(JSON.parse(e.data)); });
    source.addEventListener("heartbeat", () => { lastEventAt = new Date().toISOString(); setConnectionState("live"); });
    source.addEventListener("error", () => {
      // Server-side dependency failure (e.g. DB/Redis unreachable) reported
      // explicitly by pipeline_sse.py — surfaced as-is, never hidden.
      setConnectionState("lost");
    });

    source.onerror = () => {
      // The stream self-terminates every ~20s by design (see
      // pipeline_sse.py) — this fires on that normal close too, not just
      // real failures. EventSource reconnects on its own; we just reflect
      // connection state honestly in the meantime.
      setConnectionState("lost");
      source.close();
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, 1000);
    };
  }

  setInterval(() => {
    if (lastEventAt) {
      const text = document.getElementById("pl-connection-text");
      if (text) text.textContent = document.getElementById("pl-connection").className.includes("lost")
        ? text.textContent : "LIVE — last event: " + fmtAgo(lastEventAt);
    }
  }, 1000);

  // ---- LIVE / HISTORY toggle -------------------------------------------
  function showHistory(node) {
    historyNode = node;
    historyPage = 1;
    document.getElementById("pl-history-panel").style.display = "";
    loadHistory();
  }

  function loadHistory() {
    const params = new URLSearchParams({ page: historyPage });
    if (historyNode) params.set("node", historyNode);
    fetch(window.PIPELINE_URLS.history + "?" + params.toString())
      .then((r) => r.json())
      .then((data) => {
        const tbody = document.getElementById("pl-history-rows");
        tbody.innerHTML = "";
        data.rows.forEach((row) => {
          const tr = document.createElement("tr");
          const statusCell = row.status || "—";
          const detail = row.records
            ? Object.keys(row.records).map((k) => k + ": " + row.records[k]).join(", ")
            : (row.error || "—");
          tr.innerHTML = "<td class=\"mono\">" + new Date(row.started_at).toLocaleString() + "</td>" +
            "<td>" + row.label + "</td><td>" + statusCell + "</td>" +
            "<td class=\"num\">" + (row.duration_seconds != null ? row.duration_seconds + "s" : "—") + "</td>" +
            "<td class=\"hint\">" + detail + "</td>";
          tbody.appendChild(tr);
        });
        document.getElementById("pl-history-page-label").textContent =
          "Page " + data.page + " of " + data.total_pages + " (" + data.total + " total)";
      });
  }

  document.getElementById("pl-mode-live").addEventListener("click", () => {
    document.getElementById("pl-mode-live").classList.add("active");
    document.getElementById("pl-mode-history").classList.remove("active");
    document.getElementById("pl-history-panel").style.display = "none";
    document.getElementById("pipeline-diagram").style.opacity = "1";
  });
  document.getElementById("pl-mode-history").addEventListener("click", () => {
    document.getElementById("pl-mode-history").classList.add("active");
    document.getElementById("pl-mode-live").classList.remove("active");
    showHistory(null);
  });
  document.getElementById("pl-history-prev").addEventListener("click", () => {
    if (historyPage > 1) { historyPage -= 1; loadHistory(); }
  });
  document.getElementById("pl-history-next").addEventListener("click", () => {
    historyPage += 1; loadHistory();
  });

  // ---- node click -> detail modal (reuses the app-wide modal, dashboard.js) ----
  document.querySelectorAll(".pl-node").forEach((el) => {
    el.setAttribute("tabindex", "0");
    el.setAttribute("role", "button");
    const open = () => {
      const key = el.dataset.node;
      const url = window.PIPELINE_URLS.nodeDetail.replace("__NODE__", key);
      fetch(url).then((r) => r.json()).then((data) => {
        const body = window.openModal(el.querySelector(".pl-node-label").textContent);
        if (!data.latest) {
          body.innerHTML = '<p class="sub">No execution recorded for this node yet.</p>';
          return;
        }
        const rows = data.recent.map((r) => {
          const detail = r.records
            ? Object.keys(r.records).map((k) => k + ": " + r.records[k]).join(", ")
            : (r.error || "");
          return "<tr><td class=\"mono\">" + new Date(r.started_at).toLocaleString() + "</td><td>" + r.status +
            "</td><td class=\"num\">" + (r.duration_seconds != null ? r.duration_seconds + "s" : "—") +
            "</td><td class=\"hint\">" + detail + "</td></tr>";
        }).join("");
        body.innerHTML = '<table><thead><tr><th>When</th><th>Status</th><th class="num">Duration</th><th>Detail</th></tr></thead><tbody>' + rows + "</tbody></table>";
      });
    };
    el.addEventListener("click", open);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  });

  // ---- boot: render the server-provided initial state immediately ------
  applySnapshot(window.PIPELINE_INITIAL_SNAPSHOT);
  renderTimeline(window.PIPELINE_INITIAL_TIMELINE);
  connect();
})();
