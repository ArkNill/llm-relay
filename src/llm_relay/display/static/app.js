// Turn Monitor display page — vanilla JS, no build step
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

  function formatDuration(seconds) {
    if (!seconds || seconds < 60) return Math.round(seconds || 0) + "s";
    var m = Math.floor(seconds / 60);
    if (m < 60) return m + "m";
    var h = Math.floor(m / 60);
    return h + "h" + (m % 60) + "m";
  }

  function formatAbsTime(tsIso) {
    if (!tsIso) return "—";
    try {
      var d = new Date(tsIso);
      var h = String(d.getHours()).padStart(2, "0");
      var m = String(d.getMinutes()).padStart(2, "0");
      var s = String(d.getSeconds()).padStart(2, "0");
      var today = new Date();
      var isToday = d.toDateString() === today.toDateString();
      if (isToday) return h + ":" + m + ":" + s;
      var mo = String(d.getMonth() + 1).padStart(2, "0");
      var day = String(d.getDate()).padStart(2, "0");
      return mo + "/" + day + " " + h + ":" + m;
    } catch (e) {
      return "—";
    }
  }

  function formatLastTs(unixTs) {
    if (!unixTs) return "—";
    var d = new Date(unixTs * 1000);
    var h = String(d.getHours()).padStart(2, "0");
    var m = String(d.getMinutes()).padStart(2, "0");
    var s = String(d.getSeconds()).padStart(2, "0");
    return h + ":" + m + ":" + s;
  }

  function formatTokens(n) {
    if (!n || n === 0) return "0";
    if (n < 1000) return String(n);
    if (n < 1_000_000) return (n / 1000).toFixed(n >= 100000 ? 0 : 1) + "K";
    return (n / 1_000_000).toFixed(2) + "M";
  }

  // Map zone → { label, cssClass } for badge rendering
  var ZONE_META = {
    green:  { label: "Green",  cls: "z-green"  },
    yellow: { label: "Yellow", cls: "z-yellow" },
    orange: { label: "Orange", cls: "z-orange" },
    red:    { label: "Red",    cls: "z-red"    },
    hard:   { label: "STOP",   cls: "z-hard"   },
  };

  function zoneBadge(zone, prefix) {
    var meta = ZONE_META[zone] || ZONE_META.green;
    return '<span class="zone-badge ' + meta.cls + '">' +
             (prefix || "") + meta.label +
           '</span>';
  }

  function escapeHtml(s) {
    if (!s) return "";
    return s.replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
  }

  // Provider badge: CC (purple), Codex (green), Gemini (blue)
  var PROVIDER_META = {
    "claude-code":   { label: "Claude Code", cls: "p-claude" },
    "openai-codex":  { label: "Codex",  cls: "p-codex"  },
    "gemini-cli":    { label: "Gemini", cls: "p-gemini" },
  };

  function providerBadge(providerId) {
    var meta = PROVIDER_META[providerId];
    if (!meta) return "";
    return '<span class="provider-badge ' + meta.cls + '">' + meta.label + '</span>';
  }

  var lastHash = "";

  async function load() {
    var container = document.getElementById("session-cards");
    var updated = document.getElementById("updated");
    var countEl = document.getElementById("session-count");
    var data = await fetchJSON("/display?window=4");

    if (!data || !data.sessions || data.sessions.length === 0) {
      if (lastHash !== "EMPTY") {
        container.innerHTML = '<div class="empty-state">활성 세션 없음</div>';
        countEl.textContent = "0 sessions";
        lastHash = "EMPTY";
      }
      updated.textContent = "updated " + new Date().toLocaleTimeString();
      return;
    }

    // Diff hash includes new token metrics + zones so updates trigger redraw
    var hash = data.sessions.map(function (s) {
      return s.session_id + ":" + s.turns + ":" + (s.zone || "") +
             ":" + (s.current_ctx || 0) + ":" + (s.peak_ctx || 0) +
             ":" + (s.last_prompt_ts || "") + ":" + (s.tty || "") +
             ":" + (s.provider || "");
    }).join("|");

    if (hash === lastHash) {
      updated.textContent = "updated " + new Date().toLocaleTimeString();
      return;
    }
    lastHash = hash;

    container.innerHTML = data.sessions.map(function (s) {
      var sidShort = s.session_id.substring(0, 8);
      var duration = s.duration_s || 0;
      var ceiling = s.ceiling || 1000000;
      var currentCtx = s.current_ctx || 0;
      var peakCtx = s.peak_ctx || 0;
      var recentPeak = s.recent_peak || 0;
      var cumul = s.cumul_unique || 0;
      var curPct = Math.min(100, (currentCtx / ceiling) * 100);
      var peakPct = Math.min(100, (peakCtx / ceiling) * 100);

      var promptText = s.last_prompt || "";
      var promptClass = promptText ? "prompt-block" : "prompt-block empty";
      var promptDisplay = promptText ? escapeHtml(promptText) : "(프롬프트 없음)";
      var warn = s.message ? '<div class="warning">' + escapeHtml(s.message) + '</div>' : '';

      // Terminal badge
      var ttyBadge = "";
      if (s.tty) {
        var ttyShort = s.tty.replace("/dev/", "");
        var termLabel = ttyShort;
        if (s.term_name) termLabel += " · " + escapeHtml(s.term_name);
        ttyBadge = '<div class="tty-badge" title="CC PID ' + (s.cc_pid || "?") + '">' + termLabel + '</div>';
      }

      var ceilingLabel = formatTokens(ceiling);

      var zoneClass = s.zone || "green";
      var pBadge = providerBadge(s.provider);

      return '<div class="session-card zone-' + zoneClass + '">' +
        '<div class="card-top">' +
          '<div class="sid-group">' +
            '<div class="sid">' + pBadge + ' ' + sidShort + '</div>' +
            ttyBadge +
          '</div>' +
          '<div class="turn-count turn-plain">' + s.turns +
            '<span class="label">turns</span>' +
          '</div>' +
        '</div>' +

        // Current context row with Zone A/B badges
        '<div class="metric-row">' +
          '<span class="metric-label">Current</span>' +
          '<span class="metric-value">' + formatTokens(currentCtx) + '</span>' +
          '<span class="metric-ceiling">/ ' + ceilingLabel + '</span>' +
          '<span class="zone-badges">' +
            zoneBadge(s.zone_a, "A:") +
            zoneBadge(s.zone_b, "B:") +
          '</span>' +
        '</div>' +
        '<div class="bar"><div class="bar-fill" style="width:' + curPct + '%"></div></div>' +

        // Peak context row with peak-based Zone A/B badges
        '<div class="metric-row metric-row-sub">' +
          '<span class="metric-label">Peak</span>' +
          '<span class="metric-value">' + formatTokens(peakCtx) + '</span>' +
          '<span class="metric-ceiling">/ ' + ceilingLabel + '</span>' +
          '<span class="zone-badges">' +
            zoneBadge(s.zone_a_peak, "A:") +
            zoneBadge(s.zone_b_peak, "B:") +
          '</span>' +
        '</div>' +
        '<div class="bar bar-peak"><div class="bar-fill" style="width:' + peakPct + '%"></div></div>' +

        // Secondary metrics (no zone)
        '<div class="metric-small">' +
          '<span>Recent5 ' + formatTokens(recentPeak) + '</span>' +
          '<span>Cumul ' + formatTokens(cumul) + '</span>' +
        '</div>' +

        '<div class="' + promptClass + '">' + promptDisplay + '</div>' +
        '<div class="meta">' +
          '<span>' + formatDuration(duration) + ' elapsed</span>' +
          '<span class="abs-time">last: ' + formatLastTs(s.last_ts) + '</span>' +
        '</div>' +
        warn +
      '</div>';
    }).join("");

    updated.textContent = "updated " + new Date().toLocaleTimeString();
    countEl.textContent = data.sessions.length + " sessions";
  }

  // Page Visibility API — pause when tab hidden
  var interval = null;

  function start() {
    if (interval === null) {
      load();
      interval = setInterval(load, 2000);
    }
  }

  function stop() {
    if (interval !== null) {
      clearInterval(interval);
      interval = null;
    }
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stop();
      document.body.classList.add("tab-hidden");
    } else {
      document.body.classList.remove("tab-hidden");
      start();
    }
  });

  start();
})();
