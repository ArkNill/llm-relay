// llm-relay Dashboard — vanilla JS, no build step
(function () {
  const API = window.location.origin + "/api/v1";

  // i18n
  var _lang = (navigator.language || "en").startsWith("ko") ? "ko" : "en";
  var _msgs = {
    en: { no_sessions: "No active sessions" },
    ko: { no_sessions: "활성 세션 없음" },
  };
  function msg(key) { return (_msgs[_lang] || _msgs.en)[key] || key; }

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
        container.innerHTML = '<div class="turn-monitor-empty">' + msg("no_sessions") + '</div>';
        lastTurnHash = "EMPTY";
      }
      return;
    }

    // Diff check — skip DOM update if data unchanged (performance optimization)
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
