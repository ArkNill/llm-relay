/* Session History Viewer */
(function () {
  const $sessions = document.getElementById("sessions");
  const $detail = document.getElementById("detail");
  const $sessionList = document.getElementById("session-list");
  const $sessionCount = document.getElementById("session-count");
  const $detailSid = document.getElementById("detail-session-id");
  const $compactionBar = document.getElementById("compaction-bar");
  const $compChart = document.getElementById("composition-chart");
  const $turns = document.getElementById("turns");
  const $backBtn = document.getElementById("back-btn");

  function fmtBytes(b) {
    if (!b) return "0 B";
    if (b < 1024) return b + " B";
    if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
    return (b / 1048576).toFixed(1) + " MB";
  }

  function fmtTokens(n) {
    if (!n || n === 0) return "0";
    if (n < 1000) return String(n);
    if (n < 1000000) return (n / 1000).toFixed(n >= 100000 ? 0 : 1) + "K";
    return (n / 1000000).toFixed(2) + "M";
  }

  function fmtTime(ts) {
    if (!ts) return "-";
    const d = new Date(ts * 1000);
    return d.toLocaleString("ko-KR", { hour12: false });
  }

  function fmtDuration(firstTs, lastTs) {
    if (!firstTs || !lastTs) return "-";
    const s = lastTs - firstTs;
    if (s < 60) return Math.round(s) + "s";
    if (s < 3600) return Math.round(s / 60) + "m";
    return (s / 3600).toFixed(1) + "h";
  }

  function truncate(text, len) {
    if (!text) return "";
    return text.length > len ? text.slice(0, len) + "..." : text;
  }

  // Extract readable text from message content (string or block array)
  function extractText(content) {
    if (typeof content === "string") return content;
    if (!Array.isArray(content)) return JSON.stringify(content);
    return content.map(function (b) {
      if (typeof b === "string") return b;
      if (b.type === "input_text") return b.text || "";
      if (b.type === "output_text") return b.text || "";
      if (b.type === "text") return b.text || "";
      if (b.type === "thinking") return "[thinking] " + (b.thinking || "");
      if (b.type === "tool_use") return "[tool_use: " + (b.name || "") + "]";
      if (b.type === "tool_result") return "[tool_result]";
      return JSON.stringify(b);
    }).join("\n");
  }

  // ── Composition Chart ──
  var COMP_COLORS = {
    system: "#6e7681", user_text: "#58a6ff", assistant_text: "#3fb950",
    tool_use: "#d29922", tool_result: "#f85149", thinking_overhead: "#8b949e"
  };
  var COMP_ORDER = ["system", "user_text", "assistant_text", "tool_use", "tool_result", "thinking_overhead"];
  var COMP_LABELS = {
    system: "System", user_text: "User", assistant_text: "Asst",
    tool_use: "Call", tool_result: "Result", thinking_overhead: "Think"
  };

  function renderCompositionChart(data) {
    var turns = data.turns;
    if (!turns || turns.length < 2) return;

    var W = 700, H = 200, PAD_L = 40, PAD_R = 10, PAD_T = 10, PAD_B = 25;
    var cw = W - PAD_L - PAD_R;
    var ch = H - PAD_T - PAD_B;
    var n = turns.length;

    // Build stacked percentages per turn
    var stacked = turns.map(function (t) {
      var cats = t.composition.categories;
      var result = {};
      COMP_ORDER.forEach(function (cat) {
        result[cat] = (cats[cat] && cats[cat].pct) || 0;
      });
      return result;
    });

    // X positions
    function xPos(i) { return PAD_L + (i / (n - 1)) * cw; }
    function yPos(pct) { return PAD_T + ch - (pct / 100) * ch; }

    // Build SVG paths (stacked from bottom)
    var paths = "";
    for (var ci = COMP_ORDER.length - 1; ci >= 0; ci--) {
      var cat = COMP_ORDER[ci];
      // Compute cumulative top at each turn
      var topLine = [];
      var botLine = [];
      for (var ti = 0; ti < n; ti++) {
        var cumTop = 0;
        for (var k = 0; k <= ci; k++) {
          cumTop += stacked[ti][COMP_ORDER[k]];
        }
        var cumBot = cumTop - stacked[ti][cat];
        topLine.push({ x: xPos(ti), y: yPos(cumTop) });
        botLine.push({ x: xPos(ti), y: yPos(cumBot) });
      }
      // Build path: top line forward, bottom line backward
      var d = "M" + topLine[0].x + "," + topLine[0].y;
      for (var j = 1; j < n; j++) d += " L" + topLine[j].x + "," + topLine[j].y;
      for (var j = n - 1; j >= 0; j--) d += " L" + botLine[j].x + "," + botLine[j].y;
      d += " Z";
      paths += '<path d="' + d + '" fill="' + COMP_COLORS[cat] + '" opacity="0.8"/>';
    }

    // Compaction markers
    var compactionLines = "";
    turns.forEach(function (t, i) {
      if (t.compacted) {
        var cx = xPos(i);
        compactionLines += '<line x1="' + cx + '" y1="' + PAD_T + '" x2="' + cx + '" y2="' + (H - PAD_B) + '" stroke="#d29922" stroke-width="1" stroke-dasharray="4,3" opacity="0.7"/>';
      }
    });

    // Y axis labels
    var yLabels = "";
    [0, 25, 50, 75, 100].forEach(function (pct) {
      var y = yPos(pct);
      yLabels += '<text x="' + (PAD_L - 5) + '" y="' + (y + 3) + '" text-anchor="end" fill="#6e7681" font-size="9">' + pct + '%</text>';
      yLabels += '<line x1="' + PAD_L + '" y1="' + y + '" x2="' + (W - PAD_R) + '" y2="' + y + '" stroke="#21262d" stroke-width="0.5"/>';
    });

    // X axis turn labels (sampled)
    var xLabels = "";
    var step = Math.max(1, Math.floor(n / 8));
    for (var i = 0; i < n; i += step) {
      xLabels += '<text x="' + xPos(i) + '" y="' + (H - 5) + '" text-anchor="middle" fill="#6e7681" font-size="9">T' + turns[i].turn + '</text>';
    }
    // Always show last
    if ((n - 1) % step !== 0) {
      xLabels += '<text x="' + xPos(n - 1) + '" y="' + (H - 5) + '" text-anchor="middle" fill="#6e7681" font-size="9">T' + turns[n - 1].turn + '</text>';
    }

    // Hover targets — invisible rects per data point
    var hoverWidth = cw / Math.max(n - 1, 1);
    var hoverRects = "";
    for (var hi = 0; hi < n; hi++) {
      var hx = xPos(hi) - hoverWidth / 2;
      hoverRects += '<rect x="' + hx + '" y="' + PAD_T + '" width="' + hoverWidth + '" height="' + ch + '" fill="transparent" data-idx="' + hi + '" class="hover-target"/>';
    }

    // Hover line (vertical indicator)
    var hoverLine = '<line id="chart-hover-line" x1="0" y1="' + PAD_T + '" x2="0" y2="' + (H - PAD_B) + '" stroke="#c9d1d9" stroke-width="0.5" opacity="0" pointer-events="none"/>';

    // Legend
    var legend = '<div class="comp-chart-legend">';
    COMP_ORDER.forEach(function (cat) {
      legend += '<span><b style="color:' + COMP_COLORS[cat] + '">' + COMP_LABELS[cat] + '</b></span>';
    });
    if (data.sampled) legend += '<span class="sampled-badge">sampled</span>';
    legend += '</div>';

    var tooltip = '<div id="chart-tooltip" class="chart-tooltip"></div>';

    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMid meet">' +
      yLabels + paths + compactionLines + xLabels + hoverLine + hoverRects + '</svg>';

    $compChart.innerHTML = '<h3>Context Composition Over Turns</h3>' + svg + legend + tooltip;
    $compChart.style.display = "block";

    // Attach hover events
    var tipEl = document.getElementById("chart-tooltip");
    var lineEl = document.getElementById("chart-hover-line");
    $compChart.querySelectorAll(".hover-target").forEach(function (rect) {
      rect.addEventListener("mouseenter", function () {
        var idx = parseInt(rect.getAttribute("data-idx"), 10);
        var t = turns[idx];
        var s = stacked[idx];
        var estK = Math.round(t.composition.est_tokens / 1000);
        var lines = '<b>Turn ' + t.turn + '</b> · ' + t.msgs + ' msgs · ~' + estK + 'K tok';
        if (t.compacted) lines += ' · <span style="color:#d29922">compacted</span>';
        lines += '<br>';
        COMP_ORDER.forEach(function (cat) {
          var pct = s[cat] || 0;
          if (pct > 0.5) {
            lines += '<span style="color:' + COMP_COLORS[cat] + '">' + COMP_LABELS[cat] + '</span> ' + pct.toFixed(1) + '%  ';
          }
        });
        tipEl.innerHTML = lines;
        tipEl.style.display = "block";
        lineEl.setAttribute("x1", xPos(idx));
        lineEl.setAttribute("x2", xPos(idx));
        lineEl.setAttribute("opacity", "0.5");
      });
      rect.addEventListener("mouseleave", function () {
        tipEl.style.display = "none";
        lineEl.setAttribute("opacity", "0");
      });
    });
  }

  // ── Session List ──
  async function loadSessions() {
    try {
      const resp = await fetch("/api/v1/history?window=72");
      const data = await resp.json();
      $sessionCount.textContent = "(" + data.count + ")";

      if (!data.sessions || data.sessions.length === 0) {
        $sessionList.innerHTML = '<div class="empty">No session history recorded yet.<br>Enable with CC_RELAY_HISTORY=1</div>';
        return;
      }

      $sessionList.innerHTML = data.sessions.map(function (s) {
        var tokenMeta = "";
        var hasTokens = s.current_ctx || s.peak_ctx || s.cumul_unique;
        if (hasTokens) {
          tokenMeta = '<div class="token-meta">'
            + '<span>Current <strong>' + fmtTokens(s.current_ctx) + '</strong>'
            + (s.ceiling ? ' / ' + fmtTokens(s.ceiling) : '') + '</span>'
            + '<span>Peak <strong>' + fmtTokens(s.peak_ctx) + '</strong></span>'
            + '<span>Recent5 <strong>' + fmtTokens(s.recent_peak) + '</strong></span>'
            + '<span>Cumul <strong>' + fmtTokens(s.cumul_unique) + '</strong></span>'
            + (s.official_context_window ? '<span>Official <strong>' + fmtTokens(s.official_context_window) + '</strong></span>' : '')
            + (s.model_window ? '<span>Window <strong>' + fmtTokens(s.model_window) + '</strong></span>' : '')
            + '</div>';
        }
        var sourceBadge = s.history_source === "session_file"
          ? '<span class="source-badge">session file</span>'
          : "";
        return '<div class="session-card" data-sid="' + s.session_id + '">'
          + '<div class="sid">' + s.session_id + '</div>'
          + '<div class="meta">'
          + '<span><strong>' + s.total_turns + '</strong> turns</span>'
          + '<span>' + fmtDuration(s.first_ts, s.last_ts) + '</span>'
          + '<span>' + fmtBytes(s.total_request_bytes + s.total_response_bytes) + '</span>'
          + '<span class="provider-badge">' + (s.provider || "anthropic") + '</span>'
          + sourceBadge
          + '<span>' + fmtTime(s.last_ts) + '</span>'
          + '</div>' + tokenMeta + '</div>';
      }).join("");

      document.querySelectorAll(".session-card").forEach(function (card) {
        card.addEventListener("click", function () {
          loadDetail(card.dataset.sid);
        });
      });
    } catch (e) {
      $sessionList.innerHTML = '<div class="empty">Failed to load: ' + e.message + '</div>';
    }
  }

  // ── Detail View ──
  async function loadDetail(sid) {
    $sessions.style.display = "none";
    $detail.style.display = "block";
    $detailSid.textContent = sid;
    $turns.innerHTML = '<div class="empty">Loading...</div>';
    $compactionBar.style.display = "none";
    $compChart.style.display = "none";

    try {
      const [histResp, compResp, compChartResp] = await Promise.all([
        fetch("/api/v1/history/" + sid + "?include_thinking=1"),
        fetch("/api/v1/history/" + sid + "/compactions"),
        fetch("/api/v1/history/" + sid + "/composition"),
      ]);
      const hist = await histResp.json();
      const comp = await compResp.json();

      // Composition chart
      if (compChartResp.ok) {
        var compChartData = await compChartResp.json();
        if (compChartData.turns && compChartData.turns.length > 1) {
          renderCompositionChart(compChartData);
        }
      }

      // Compaction bar
      if (comp.compaction_count > 0) {
        $compactionBar.style.display = "block";
        $compactionBar.innerHTML = "Compaction detected: " + comp.compaction_count + " event(s) — "
          + comp.compactions.map(function (c) {
            return "turn " + c.turn_number + " (dropped " + c.dropped_count + " msgs, -" + c.token_drop_pct + "% tokens)";
          }).join("; ");
      }

      if (!hist.turns || hist.turns.length === 0) {
        $turns.innerHTML = '<div class="empty">No turns recorded for this session.</div>';
        return;
      }

      var compTurns = {};
      if (comp.compactions) {
        comp.compactions.forEach(function (c) { compTurns[c.turn_number] = true; });
      }

      $turns.innerHTML = hist.turns.map(function (t) {
        var isCompacted = compTurns[t.turn_number];
        var html = '<div class="turn' + (isCompacted ? ' compacted' : '') + '">';
        html += '<div class="turn-header">';
        html += '<span class="turn-number">Turn ' + t.turn_number + '</span>';
        html += '<div class="turn-meta">';
        html += '<span class="turn-mode ' + t.storage_mode + '">' + t.storage_mode + '</span>';
        if (t.model) html += '<span>' + t.model + '</span>';
        html += '<span>' + t.total_message_count + ' msgs</span>';
        html += '<span>' + fmtTime(t.ts) + '</span>';
        html += '</div></div>';

        // Request messages
        if (t.request_messages) {
          try {
            var msgs = JSON.parse(t.request_messages);
            msgs.forEach(function (m) {
              var role = m.role || "unknown";
              var text = extractText(m.content);
              html += '<div class="msg msg-' + role + '">';
              html += '<div class="msg-role ' + role + '">' + role + '</div>';
              html += truncate(text, 2000);
              html += '</div>';
            });
          } catch (e) {}
        }

        // Thinking blocks
        if (t.thinking_blocks) {
          try {
            var thinking = JSON.parse(t.thinking_blocks);
            thinking.forEach(function (tb) {
              html += '<div class="msg msg-thinking">';
              html += '<div class="msg-role thinking">thinking</div>';
              html += truncate(tb.thinking || "", 2000);
              html += '</div>';
            });
          } catch (e) {}
        }

        // Response
        if (t.response_message) {
          try {
            var content = JSON.parse(t.response_message);
            var text = extractText(content);
            html += '<div class="msg msg-assistant">';
            html += '<div class="msg-role assistant">assistant</div>';
            html += truncate(text, 2000);
            html += '</div>';
          } catch (e) {}
        }

        html += '</div>';
        return html;
      }).join("");

    } catch (e) {
      $turns.innerHTML = '<div class="empty">Failed to load: ' + e.message + '</div>';
    }
  }

  // ── Back button ──
  $backBtn.addEventListener("click", function () {
    $detail.style.display = "none";
    $sessions.style.display = "block";
  });

  // Init
  loadSessions();
})();
