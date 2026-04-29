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

  // Composition category colors (dark theme)
  var COMP_COLORS = {
    system:             "#6e7681",
    user_text:          "#58a6ff",
    assistant_text:     "#3fb950",
    tool_use:           "#d29922",
    tool_result:        "#f85149",
    thinking_overhead:  "#8b949e",
  };

  var COMP_LABELS = {
    system: "Sys", user_text: "User", assistant_text: "Asst",
    tool_use: "Call", tool_result: "Result", thinking_overhead: "Think",
  };

  var COMP_TIPS = {
    user_text:          "Percentage of context occupied by user-input prompt text",
    assistant_text:     "Percentage of model-generated response text",
    tool_use:           "Percentage of tool-call definitions (Read, Bash, Edit, etc.)",
    tool_result:        "Percentage of tool execution results (file contents, grep output, etc.). Higher = more context noise.",
    thinking_overhead:  "Model-internal reasoning (thinking) blocks + signature overhead",
    snr:                "Signal-to-Noise Ratio. (User+Asst) / (Result+Think). 1.0+ is ideal; below 0.5 is a warning.",
    dupes:              "Number of times the same file was Read 2+ times. Re-reading after compaction is the main cause.",
  };

  function tipAttr(key) {
    var t = COMP_TIPS[key] || "";
    return t ? ' data-tip="' + escapeHtml(t) + '"' : "";
  }

  function compositionHtml(comp) {
    if (!comp || !comp.categories) return "";
    var cats = comp.categories;

    // Filled pie chart — SVG path arcs
    var SIZE = 140, CX = SIZE / 2, CY = SIZE / 2, R = SIZE / 2 - 2;
    var drawOrder = ["tool_result", "tool_use", "thinking_overhead", "user_text", "assistant_text", "system"];
    var slices = "";
    var angle = -90; // start at 12 o'clock

    for (var i = 0; i < drawOrder.length; i++) {
      var cat = drawOrder[i];
      var pct = (cats[cat] && cats[cat].pct) || 0;
      if (pct < 0.3) continue;
      var sweep = pct / 100 * 360;
      var startRad = angle * Math.PI / 180;
      var endRad = (angle + sweep) * Math.PI / 180;
      var x1 = CX + R * Math.cos(startRad);
      var y1 = CY + R * Math.sin(startRad);
      var x2 = CX + R * Math.cos(endRad);
      var y2 = CY + R * Math.sin(endRad);
      var large = sweep > 180 ? 1 : 0;
      slices +=
        '<path d="M' + CX + ',' + CY + ' L' + x1.toFixed(2) + ',' + y1.toFixed(2) +
        ' A' + R + ',' + R + ' 0 ' + large + ',1 ' + x2.toFixed(2) + ',' + y2.toFixed(2) +
        ' Z" fill="' + COMP_COLORS[cat] + '" opacity="0.85">' +
        '<title>' + COMP_LABELS[cat] + ' ' + pct.toFixed(1) + '%</title></path>';
      angle += sweep;
    }

    var pie =
      '<svg class="comp-pie" viewBox="0 0 ' + SIZE + ' ' + SIZE + '" xmlns="http://www.w3.org/2000/svg">' +
        '<circle cx="' + CX + '" cy="' + CY + '" r="' + R + '" fill="#21262d"/>' +
        slices +
      '</svg>';

    // Metrics grid
    var snr = comp.snr || 0;
    var resultPct = (cats.tool_result && cats.tool_result.pct) || 0;
    var dupes = comp.duplicate_read_count || 0;
    var snrCls = snr < 0.5 ? " comp-warn" : "";
    var resultCls = resultPct > 50 ? " comp-danger" : "";

    var grid =
      '<div class="comp-grid">' +
        '<span class="has-tip"' + tipAttr("user_text") + '><b style="color:' + COMP_COLORS.user_text + '">User</b> ' + ((cats.user_text && cats.user_text.pct) || 0).toFixed(0) + '%</span>' +
        '<span class="has-tip"' + tipAttr("assistant_text") + '><b style="color:' + COMP_COLORS.assistant_text + '">Asst</b> ' + ((cats.assistant_text && cats.assistant_text.pct) || 0).toFixed(0) + '%</span>' +
        '<span class="has-tip"' + tipAttr("tool_use") + '><b style="color:' + COMP_COLORS.tool_use + '">Call</b> ' + ((cats.tool_use && cats.tool_use.pct) || 0).toFixed(0) + '%</span>' +
        '<span class="has-tip' + resultCls + '"' + tipAttr("tool_result") + '><b style="color:' + COMP_COLORS.tool_result + '">Result</b> ' + resultPct.toFixed(0) + '%</span>' +
        '<span class="has-tip"' + tipAttr("thinking_overhead") + '><b style="color:' + COMP_COLORS.thinking_overhead + '">Think</b> ' + ((cats.thinking_overhead && cats.thinking_overhead.pct) || 0).toFixed(0) + '%</span>' +
        '<span class="has-tip' + snrCls + '"' + tipAttr("snr") + '>SNR ' + snr.toFixed(2) + '</span>' +
      '</div>';

    var dupeLine = '';
    if (dupes > 0) {
      var dupeReads = comp.duplicate_reads || {};
      var dupeWarning = comp.duplicate_read_warning || false;
      var dupeCls = "comp-dupes has-tip" + (dupeWarning ? " comp-dupes-warn" : "");
      var topFiles = Object.keys(dupeReads).sort(function (a, b) {
        return dupeReads[b] - dupeReads[a];
      }).slice(0, 3).map(function (p) {
        var name = p.split("/").pop();
        return name + " (" + dupeReads[p] + "x)";
      }).join(", ");
      var dupeText = dupes + ' duplicate reads';
      if (topFiles) dupeText += ': ' + topFiles;
      dupeLine = '<div class="' + dupeCls + '"' + tipAttr("dupes") + '>' + dupeText + '</div>';
    }

    // Per-tool call breakdown
    var toolLine = '';
    var toolCalls = comp.tool_calls || {};
    var toolNames = Object.keys(toolCalls);
    if (toolNames.length > 0) {
      var sorted = toolNames.sort(function (a, b) { return toolCalls[b] - toolCalls[a]; });
      var topTools = sorted.slice(0, 5).map(function (n) { return n + ' ' + toolCalls[n] + 'x'; }).join(' \u00b7 ');
      toolLine += '<div class="comp-tools has-tip" data-tip="Per-tool invocation count">' + topTools + '</div>';
    }

    // Exec stats (Codex/Gemini)
    var execStats = comp.exec_stats;
    if (execStats && execStats.total > 0) {
      var rate = Math.round(execStats.success / execStats.total * 100);
      var execCls = rate < 70 ? " comp-danger" : "";
      var execText = 'Exec ' + execStats.success + '/' + execStats.total + ' (' + rate + '%)';
      if (execStats.avg_duration_ms) execText += ' avg ' + execStats.avg_duration_ms + 'ms';
      toolLine += '<div class="comp-exec' + execCls + '">' + execText + '</div>';
    }

    // Thinking count
    var thinkCount = comp.thinking_count || 0;
    if (thinkCount > 0) {
      toolLine += '<span class="comp-think-count">' + thinkCount + ' think blocks</span>';
    }

    var snrRecLine = comp.snr_recommendation
      ? '<div class="comp-snr-rec">' + comp.snr_recommendation + '</div>'
      : '';

    return '<div class="comp-section">' + pie + '<div class="comp-detail">' + grid + dupeLine + toolLine + snrRecLine + '</div></div>';
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
        container.innerHTML = '<div class="empty-state">No active sessions</div>';
        countEl.textContent = "0 sessions";
        lastHash = "EMPTY";
      }
      updated.textContent = "updated " + new Date().toLocaleTimeString();
      return;
    }

    // Diff hash includes token metrics + zones + composition so updates trigger redraw
    var hash = data.sessions.map(function (s) {
      var compHash = s.composition ? s.composition.snr + ":" + s.composition.duplicate_read_count : "";
      return s.session_id + ":" + s.turns + ":" + (s.zone || "") +
             ":" + (s.current_ctx || 0) + ":" + (s.peak_ctx || 0) +
             ":" + (s.model_window || 0) +
             ":" + (s.last_prompt_ts || "") + ":" + (s.tty || "") +
             ":" + (s.provider || "") + ":" + compHash +
             ":" + (s.cache_hit_rate || 0) + ":" + (s.ttl_tier || "");
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
      var modelWindow = s.model_window || 0;
      var officialCtx = s.official_context_window || 0;
      var isCodex = s.provider === "openai-codex";
      var usagePct = Math.min(100, (cumul / ceiling) * 100);
      var windowBase = modelWindow || officialCtx || ceiling;
      var curPct = Math.min(100, (currentCtx / windowBase) * 100);
      var peakPct = Math.min(100, (peakCtx / windowBase) * 100);

      var promptText = s.last_prompt || "";
      var promptClass = promptText ? "prompt-block" : "prompt-block empty";
      var promptDisplay = promptText ? escapeHtml(promptText) : "(No prompt)";
      var warn = s.message ? '<div class="warning">' + escapeHtml(s.message) + '</div>' : '';

      // Terminal badge + connection type
      var ttyBadge = "";
      if (s.tty) {
        var ttyShort = s.tty.replace("/dev/", "");
        var termLabel = ttyShort;
        if (s.term_name) termLabel += " · " + escapeHtml(s.term_name);
        ttyBadge = '<div class="tty-badge" title="CC PID ' + (s.cc_pid || "?") + '">' + termLabel + '</div>';
      }
      var connType = s.connection_type || "";
      var connBadge = "";
      if (connType && connType !== "unknown") {
        var connCls = "conn-native";
        if (connType.indexOf("tailscale") >= 0) connCls = "conn-tailscale";
        else if (connType.indexOf("ssh") >= 0) connCls = "conn-ssh";
        else if (connType.indexOf("mosh") >= 0) connCls = "conn-mosh";
        else if (connType.indexOf("tmux") >= 0 || connType.indexOf("screen") >= 0) connCls = "conn-mux";
        connBadge = '<span class="conn-badge ' + connCls + '">' + escapeHtml(connType) + '</span>';
      }

      var ceilingLabel = formatTokens(ceiling);
      var windowLabel = formatTokens(windowBase);

      var zoneClass = s.zone || "green";
      var pBadge = providerBadge(s.provider);
      var metricsHtml = "";

      if (isCodex) {
        metricsHtml =
          '<div class="metric-row">' +
            '<span class="metric-label">Current</span>' +
            '<span class="metric-value">' + formatTokens(currentCtx) + '</span>' +
            '<span class="metric-ceiling">/ ' + ceilingLabel + '</span>' +
            '<span class="zone-badges">' +
              zoneBadge(s.zone_a, "A:") +
              zoneBadge(s.zone_b, "B:") +
            '</span>' +
          '</div>' +
          '<div class="bar"><div class="bar-fill" style="width:' + Math.min(100, (currentCtx / ceiling) * 100) + '%"></div></div>' +

          '<div class="metric-row metric-row-sub">' +
            '<span class="metric-label">Peak</span>' +
            '<span class="metric-value">' + formatTokens(peakCtx) + '</span>' +
            '<span class="metric-ceiling">/ ' + ceilingLabel + '</span>' +
            '<span class="zone-badges">' +
              zoneBadge(s.zone_a_peak, "A:") +
              zoneBadge(s.zone_b_peak, "B:") +
            '</span>' +
          '</div>' +
          '<div class="bar bar-peak"><div class="bar-fill" style="width:' + Math.min(100, (peakCtx / ceiling) * 100) + '%"></div></div>' +

          '<div class="metric-small">' +
            '<span>Recent5 ' + formatTokens(recentPeak) + '</span>' +
            '<span>Cumul ' + formatTokens(cumul) + '</span>' +
            (officialCtx ? '<span>Official ' + formatTokens(officialCtx) + '</span>' : '') +
            (modelWindow ? '<span>Window ' + formatTokens(modelWindow) + '</span>' : '') +
          '</div>';
      } else {
        metricsHtml =
          '<div class="metric-row">' +
            '<span class="metric-label">Current</span>' +
            '<span class="metric-value">' + formatTokens(currentCtx) + '</span>' +
            '<span class="metric-ceiling">/ ' + ceilingLabel + '</span>' +
            '<span class="zone-badges">' +
              zoneBadge(s.zone_a, "A:") +
              zoneBadge(s.zone_b, "B:") +
            '</span>' +
          '</div>' +
          '<div class="bar"><div class="bar-fill" style="width:' + Math.min(100, (currentCtx / ceiling) * 100) + '%"></div></div>' +

          '<div class="metric-row metric-row-sub">' +
            '<span class="metric-label">Peak</span>' +
            '<span class="metric-value">' + formatTokens(peakCtx) + '</span>' +
            '<span class="metric-ceiling">/ ' + ceilingLabel + '</span>' +
            '<span class="zone-badges">' +
              zoneBadge(s.zone_a_peak, "A:") +
              zoneBadge(s.zone_b_peak, "B:") +
            '</span>' +
          '</div>' +
          '<div class="bar bar-peak"><div class="bar-fill" style="width:' + Math.min(100, (peakCtx / ceiling) * 100) + '%"></div></div>' +

          '<div class="metric-small">' +
            '<span>Recent5 ' + formatTokens(recentPeak) + '</span>' +
            '<span>Cumul ' + formatTokens(cumul) + '</span>' +
            (officialCtx ? '<span>Official ' + formatTokens(officialCtx) + '</span>' : '') +
            (modelWindow ? '<span>Window ' + formatTokens(modelWindow) + '</span>' : '') +
          '</div>';
      }

      // Cache hit rate + TTL tier badges (appended to metrics)
      var cacheRate = s.cache_hit_rate;
      var ttlTier = s.ttl_tier;
      if (cacheRate !== undefined || (ttlTier && ttlTier !== "unknown")) {
        var cacheBadges = '<div class="metric-badges">';
        if (cacheRate !== undefined) {
          var cacheCls = cacheRate < 50 ? "badge-danger" : (cacheRate < 80 ? "badge-warn" : "badge-ok");
          cacheBadges += '<span class="metric-badge ' + cacheCls + '" title="Cache hit rate">Cache ' + cacheRate + '%</span>';
        }
        if (ttlTier && ttlTier !== "unknown") {
          var ttlCls = ttlTier === "1h" ? "badge-ok" : (ttlTier === "5m" ? "badge-warn" : "badge-info");
          cacheBadges += '<span class="metric-badge ' + ttlCls + '" title="Cache TTL tier">TTL ' + ttlTier + '</span>';
        }
        cacheBadges += '</div>';
        metricsHtml += cacheBadges;
      }

      var compHtml = compositionHtml(s.composition);

      return '<div class="session-card zone-' + zoneClass + '">' +
        '<div class="card-top">' +
          '<div class="sid-group">' +
            '<div class="sid">' + pBadge + ' ' + sidShort + connBadge + '</div>' +
            ttyBadge +
          '</div>' +
          '<div class="turn-count turn-plain">' + s.turns +
            '<span class="label">turns</span>' +
          '</div>' +
        '</div>' +
        '<div class="' + promptClass + '">' + promptDisplay + '</div>' +
        metricsHtml +
        compHtml +
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
