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
    var cliData = await fetchJSON("/cli/status");
    if (!cliData) { container.innerHTML = "<p>Failed to load</p>"; return; }

    // Fetch active sessions and recent delegations for activity counts
    var sessionsData = await fetchJSON("/sessions?window=1");
    var delegData = await fetchJSON("/delegations?limit=50");

    var now = Date.now() / 1000;
    var activeSessions = 0;
    if (sessionsData && sessionsData.sessions) {
      activeSessions = sessionsData.sessions.filter(function (s) {
        return (now - s.last_ts) < 300; // active within 5 minutes
      }).length;
    }

    // Count recent delegations per CLI (last 2 hours)
    var delegCounts = {};
    if (delegData && delegData.delegations) {
      delegData.delegations.forEach(function (d) {
        if ((now - d.ts) < 7200) {
          var cli = d.cli_id || "unknown";
          delegCounts[cli] = (delegCounts[cli] || 0) + 1;
        }
      });
    }

    // Map CLI IDs to display names
    var cliIdMap = { "claude-code": "claude", "openai-codex": "codex", "gemini-cli": "gemini" };

    container.innerHTML = cliData.map(function (s) {
      var statusClass = s.usable ? "status-ok" : (s.installed ? "status-warn" : "status-off");
      var authLabel = s.usable ? s.preferred_auth : "none";

      // Activity: sessions for claude, delegations for codex/gemini
      var activityHtml = "";
      var isActive = false;
      if (s.cli_id === "claude-code") {
        activityHtml = '<div class="detail activity">' + activeSessions + ' active session' + (activeSessions !== 1 ? 's' : '') + '</div>';
        isActive = activeSessions > 0;
      } else {
        var count = delegCounts[s.cli_id] || 0;
        activityHtml = '<div class="detail activity">' + count + ' delegation' + (count !== 1 ? 's' : '') + ' (2h)</div>';
        isActive = count > 0;
      }

      var cardClass = "card" + (isActive ? " card-active" : "") + (!s.usable ? " card-inactive" : "");

      return '<div class="' + cardClass + '">' +
        '<h3><span class="status ' + statusClass + '"></span>' + s.binary_name +
        (isActive ? '<span class="pulse"></span>' : '') +
        '</h3>' +
        '<div class="detail">v' + (s.version || "?") + '</div>' +
        '<div class="detail">Auth: ' + authLabel + '</div>' +
        activityHtml +
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
      sessionsEl.innerHTML = '<div class="health-empty">No composition data — enable with LLM_RELAY_HISTORY=1</div>';
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
    var delegations = data.delegations;
    tbody.innerHTML = delegations.map(function (d, i) {
      var statusClass = d.success ? "success" : "failure";
      var statusText = d.success ? "OK" : "FAIL";
      return '<tr class="clickable" data-idx="' + i + '">' +
        "<td>" + timeAgo(d.ts) + "</td>" +
        '<td>' + d.cli_id + "</td>" +
        '<td class="' + statusClass + '">' + statusText + "</td>" +
        "<td>" + Math.round(d.duration_ms) + "ms</td>" +
        "<td>" + (d.prompt_preview || "").substring(0, 60) + "</td>" +
        "</tr>";
    }).join("");

    // Click handler for detail panel
    tbody.querySelectorAll("tr.clickable").forEach(function (row) {
      row.addEventListener("click", function () {
        var d = delegations[parseInt(row.dataset.idx)];
        openDetailPanel(d);
      });
    });
  }

  function openDetailPanel(d) {
    var panel = document.getElementById("detail-panel");
    var overlay = document.getElementById("detail-overlay");
    var body = document.getElementById("panel-body");

    var statusCls = d.success ? "ok" : "fail";
    var statusText = d.success ? "OK" : "FAIL";
    var date = new Date(d.ts * 1000);
    var dateStr = date.toLocaleString(undefined, { hour12: false });

    body.innerHTML =
      '<div class="detail-row">' +
        '<div class="detail-label">Status</div>' +
        '<span class="detail-status ' + statusCls + '">' + statusText + '</span>' +
        (d.exit_code !== 0 ? ' <span style="color:#8b949e;font-size:0.75rem">(exit ' + d.exit_code + ')</span>' : '') +
      '</div>' +
      '<div class="detail-row">' +
        '<div class="detail-label">CLI</div>' +
        '<div class="detail-value">' + d.cli_id + '</div>' +
      '</div>' +
      '<div class="detail-row">' +
        '<div class="detail-label">Strategy</div>' +
        '<div class="detail-value">' + (d.strategy || "direct") + '</div>' +
      '</div>' +
      '<div class="detail-row">' +
        '<div class="detail-label">Model</div>' +
        '<div class="detail-value">' + (d.model || "default") + '</div>' +
      '</div>' +
      '<div class="detail-row">' +
        '<div class="detail-label">Duration</div>' +
        '<div class="detail-value">' + (d.duration_ms / 1000).toFixed(1) + 's (' + Math.round(d.duration_ms) + 'ms)</div>' +
      '</div>' +
      '<div class="detail-row">' +
        '<div class="detail-label">Timestamp</div>' +
        '<div class="detail-value">' + dateStr + '</div>' +
      '</div>' +
      '<div class="detail-row">' +
        '<div class="detail-label">Working Directory</div>' +
        '<div class="detail-value mono">' + (d.working_dir || "—") + '</div>' +
      '</div>' +
      '<div class="detail-row">' +
        '<div class="detail-label">Auth</div>' +
        '<div class="detail-value">' + (d.auth_method || "—") + '</div>' +
      '</div>' +
      '<div class="detail-row">' +
        '<div class="detail-label">Output Size</div>' +
        '<div class="detail-value">' + (d.output_chars || 0).toLocaleString() + ' chars</div>' +
      '</div>' +
      '<div class="detail-row">' +
        '<div class="detail-label">Prompt</div>' +
        '<div class="detail-value mono">' + escapeHtml(d.prompt_preview || "—") + '</div>' +
      '</div>' +
      (d.error ? '<div class="detail-row"><div class="detail-label">Error</div><div class="detail-value mono" style="color:#f85149">' + escapeHtml(d.error) + '</div></div>' : '');

    panel.classList.add("open");
    overlay.classList.add("open");
    panel._data = d;
  }

  function closeDetailPanel() {
    document.getElementById("detail-panel").classList.remove("open");
    document.getElementById("detail-overlay").classList.remove("open");
  }

  function escapeHtml(s) {
    if (!s) return "";
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  document.getElementById("panel-close").addEventListener("click", closeDetailPanel);
  document.getElementById("detail-overlay").addEventListener("click", closeDetailPanel);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeDetailPanel();
  });
  document.getElementById("panel-copy").addEventListener("click", function () {
    var panel = document.getElementById("detail-panel");
    var d = panel._data;
    if (!d) return;
    var text = "CLI: " + d.cli_id + "\nStatus: " + (d.success ? "OK" : "FAIL") +
      "\nDuration: " + (d.duration_ms / 1000).toFixed(1) + "s" +
      "\nDir: " + (d.working_dir || "—") +
      "\nPrompt: " + (d.prompt_preview || "—") +
      (d.error ? "\nError: " + d.error : "");
    navigator.clipboard.writeText(text).then(function () {
      var btn = document.getElementById("panel-copy");
      btn.textContent = "Copied!";
      setTimeout(function () { btn.textContent = "Copy"; }, 1500);
    });
  });

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
