// llm-relay Dashboard — vanilla JS, no build step
(function () {
  const API = window.location.origin + "/api/v1";

  async function fetchJSON(path) {
    try {
      const resp = await fetch(API + path);
      return await resp.json();
    } catch (e) {
      console.error("Fetch failed:", path, e);
      return null;
    }
  }

  function timeAgo(ts) {
    const diff = Date.now() / 1000 - ts;
    if (diff < 60) return Math.round(diff) + "s ago";
    if (diff < 3600) return Math.round(diff / 60) + "m ago";
    if (diff < 86400) return Math.round(diff / 3600) + "h ago";
    return Math.round(diff / 86400) + "d ago";
  }

  async function loadHealth() {
    const badge = document.getElementById("health-badge");
    const data = await fetchJSON("/health");
    if (!data) { badge.textContent = "error"; badge.className = "badge badge-error"; return; }
    badge.textContent = data.status;
    badge.className = "badge badge-" + (data.status === "ok" ? "ok" : "degraded");
  }

  async function loadCLIStatus() {
    const container = document.getElementById("cli-cards");
    const data = await fetchJSON("/cli/status");
    if (!data) { container.innerHTML = "<p>Failed to load</p>"; return; }
    container.innerHTML = data.map(function (s) {
      var statusClass = s.usable ? "status-ok" : (s.installed ? "status-warn" : "status-off");
      var authLabel = s.usable ? s.preferred_auth : "none";
      return '<div class="card">' +
        '<h3><span class="status ' + statusClass + '"></span>' + s.binary_name + '</h3>' +
        '<div class="detail">v' + (s.version || "?") + '</div>' +
        '<div class="detail">Auth: ' + authLabel + '</div>' +
        '</div>';
    }).join("");
  }

  async function loadStats() {
    const container = document.getElementById("stats-content");
    const data = await fetchJSON("/delegations/stats?window=24");
    if (!data) { container.innerHTML = "<p>No data</p>"; return; }
    var boxes = [
      { value: data.total_delegations || 0, label: "Total (24h)" },
      { value: Math.round(data.total_duration_ms / 1000) + "s", label: "Total Duration" },
    ];
    var perCli = data.per_cli || {};
    Object.keys(perCli).forEach(function (cli) {
      var s = perCli[cli];
      boxes.push({ value: s.success_rate + "%", label: cli.split("-")[0] + " success" });
    });
    container.innerHTML = boxes.map(function (b) {
      return '<div class="stat-box"><div class="value">' + b.value + '</div><div class="label">' + b.label + '</div></div>';
    }).join("");
  }

  function renderContextHealth(sessions) {
    var summaryEl = document.getElementById("health-summary");
    var sessionsEl = document.getElementById("health-sessions");
    if (!summaryEl || !sessionsEl) return;

    // Filter sessions that have composition data
    var withComp = sessions.filter(function (s) { return s.composition; });
    if (withComp.length === 0) {
      summaryEl.innerHTML = "";
      sessionsEl.innerHTML = '<div class="health-empty">No composition data — enable with CC_RELAY_HISTORY=1</div>';
      return;
    }

    // Compute summary stats
    var totalSnr = 0;
    var worstSnr = Infinity;
    var worstSnrSid = "";
    var totalDupes = 0;
    var maxResultPct = 0;
    var maxResultSid = "";

    withComp.forEach(function (s) {
      var c = s.composition;
      var snr = c.snr || 0;
      totalSnr += snr;
      if (snr < worstSnr) {
        worstSnr = snr;
        worstSnrSid = s.session_id;
      }
      totalDupes += c.duplicate_read_count || 0;
      var resultPct = (c.categories && c.categories.tool_result) ? c.categories.tool_result.pct : 0;
      if (resultPct > maxResultPct) {
        maxResultPct = resultPct;
        maxResultSid = s.session_id;
      }
    });

    var avgSnr = totalSnr / withComp.length;
    var avgSnrCls = avgSnr < 0.3 ? "danger" : (avgSnr < 0.5 ? "warn" : "");

    // Summary boxes (reuse stats-grid pattern)
    summaryEl.innerHTML = [
      { value: avgSnr.toFixed(2), label: "Avg SNR", cls: avgSnrCls },
      { value: worstSnr.toFixed(2), label: "Worst (" + worstSnrSid.substring(0, 8) + ")", cls: worstSnr < 0.3 ? "danger" : (worstSnr < 0.5 ? "warn" : "") },
      { value: totalDupes, label: "Dup Reads" },
      { value: maxResultPct.toFixed(1) + "%", label: "Max Result%", cls: maxResultPct > 50 ? "warn" : "" },
    ].map(function (b) {
      var valCls = b.cls ? ' style="color:' + (b.cls === "danger" ? "#da3633" : "#d29922") + '"' : "";
      return '<div class="stat-box"><div class="value"' + valCls + '>' + b.value + '</div><div class="label">' + b.label + '</div></div>';
    }).join("");

    // Per-session rows
    sessionsEl.innerHTML = withComp.map(function (s) {
      var c = s.composition;
      var snr = c.snr || 0;
      var resultPct = (c.categories && c.categories.tool_result) ? c.categories.tool_result.pct : 0;
      var dupes = c.duplicate_read_count || 0;

      var rowCls = "health-row";
      if (snr < 0.3) rowCls += " snr-danger";
      else if (snr < 0.5) rowCls += " snr-warn";

      var snrCls = snr < 0.3 ? "snr-val danger" : (snr < 0.5 ? "snr-val warn" : "snr-val");
      var resultCls = resultPct > 50 ? "result-val warn" : "result-val";
      var dupeWarning = c.duplicate_read_warning || false;
      var dupesHtml = "";
      if (dupes > 0) {
        var dupeReads = c.duplicate_reads || {};
        var topFiles = Object.keys(dupeReads).sort(function (a, b) {
          return dupeReads[b] - dupeReads[a];
        }).slice(0, 3).map(function (p) {
          return p.split("/").pop() + " (" + dupeReads[p] + "x)";
        }).join(", ");
        var dupeCls = "dupes-val" + (dupeWarning ? " dupes-warn" : "");
        dupesHtml = '<span class="metric"><span class="metric-label">dupes</span><span class="' + dupeCls + '" title="' + topFiles + '">' + dupes + '</span></span>';
      }

      if (dupeWarning) rowCls += " dupes-row-warn";

      var snrRecHtml = c.snr_recommendation
        ? '<div class="health-rec">' + c.snr_recommendation + '</div>'
        : '';

      return '<div class="' + rowCls + '">' +
        '<span class="sid">' + s.session_id.substring(0, 8) + '</span>' +
        '<span class="metric"><span class="metric-label">SNR</span><span class="' + snrCls + '">' + snr.toFixed(2) + '</span></span>' +
        '<span class="metric"><span class="metric-label">Result</span><span class="' + resultCls + '">' + resultPct.toFixed(1) + '%</span></span>' +
        dupesHtml +
        snrRecHtml +
        '</div>';
    }).join("");
  }

  function formatDuration(seconds) {
    if (!seconds || seconds < 60) return Math.round(seconds || 0) + "s";
    var m = Math.floor(seconds / 60);
    if (m < 60) return m + "m";
    var h = Math.floor(m / 60);
    return h + "h" + (m % 60) + "m";
  }

  var lastTurnHash = "";

  async function loadTurnMonitor() {
    var container = document.getElementById("turn-cards");
    var updated = document.getElementById("turn-updated");
    var data = await fetchJSON("/turns?window=4");
    if (!data || !data.sessions || data.sessions.length === 0) {
      if (lastTurnHash !== "EMPTY") {
        container.innerHTML = '<div class="turn-monitor-empty">활성 세션 없음</div>';
        renderContextHealth([]);
        lastTurnHash = "EMPTY";
      }
      return;
    }

    // Diff check — skip DOM update if data unchanged (ZBook GPU load mitigation)
    var hash = data.sessions.map(function (s) {
      return s.session_id + ":" + s.turns + ":" + s.zone;
    }).join("|");
    if (hash === lastTurnHash) return;
    lastTurnHash = hash;

    var now = Date.now() / 1000;
    container.innerHTML = data.sessions.map(function (s) {
      var sidShort = s.session_id.substring(0, 8);
      var duration = s.duration_s || 0;
      var idleS = now - (s.last_ts || now);
      var pct = Math.min(100, (s.turns / 300) * 100);
      var msg = s.message ? '<div class="message">' + s.message + '</div>' : '';
      return '<div class="turn-card zone-' + s.zone + '">' +
        '<div class="sid">' + sidShort + '</div>' +
        '<div class="turn-count">' + s.turns + '<span class="label">/ 300</span></div>' +
        '<div class="meta">' +
          '<span>' + formatDuration(duration) + ' elapsed</span>' +
          '<span>idle ' + formatDuration(idleS) + '</span>' +
        '</div>' +
        '<div class="bar"><div class="bar-fill" style="width:' + pct + '%"></div></div>' +
        msg +
      '</div>';
    }).join("");

    renderContextHealth(data.sessions);

    var ts = new Date();
    updated.textContent = "updated " + ts.toLocaleTimeString();
  }

  async function loadHistory() {
    var tbody = document.querySelector("#history-table tbody");
    var data = await fetchJSON("/delegations?limit=20");
    if (!data || !data.delegations) { tbody.innerHTML = "<tr><td colspan=5>No data</td></tr>"; return; }
    tbody.innerHTML = data.delegations.map(function (d) {
      var statusClass = d.success ? "success" : "failure";
      var statusText = d.success ? "OK" : "FAIL";
      return "<tr>" +
        "<td>" + timeAgo(d.ts) + "</td>" +
        '<td>' + d.cli_id + "</td>" +
        '<td class="' + statusClass + '">' + statusText + "</td>" +
        "<td>" + Math.round(d.duration_ms) + "ms</td>" +
        "<td>" + (d.prompt_preview || "").substring(0, 60) + "</td>" +
        "</tr>";
    }).join("");
  }

  // Initial load
  loadHealth();
  loadCLIStatus();
  loadStats();
  loadHistory();
  loadTurnMonitor();

  // Polling intervals — pause when tab is hidden (Page Visibility API)
  // Reduces GPU/CPU load when user isn't actively viewing
  var turnInterval = null;
  var otherInterval = null;

  function startPolling() {
    if (turnInterval === null) {
      turnInterval = setInterval(loadTurnMonitor, 2000);  // 2s (was 1s, GPU mitigation)
    }
    if (otherInterval === null) {
      otherInterval = setInterval(function () {
        loadHealth(); loadStats(); loadHistory();
      }, 30000);
    }
  }

  function stopPolling() {
    if (turnInterval !== null) { clearInterval(turnInterval); turnInterval = null; }
    if (otherInterval !== null) { clearInterval(otherInterval); otherInterval = null; }
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopPolling();
      document.body.classList.add("tab-hidden");
    } else {
      document.body.classList.remove("tab-hidden");
      loadTurnMonitor();  // immediate refresh on return
      startPolling();
    }
  });

  startPolling();
})();
