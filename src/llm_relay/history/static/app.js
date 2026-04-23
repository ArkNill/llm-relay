/* Session History Viewer */
(function () {
  const $sessions = document.getElementById("sessions");
  const $detail = document.getElementById("detail");
  const $sessionList = document.getElementById("session-list");
  const $sessionCount = document.getElementById("session-count");
  const $detailSid = document.getElementById("detail-session-id");
  const $compactionBar = document.getElementById("compaction-bar");
  const $turns = document.getElementById("turns");
  const $backBtn = document.getElementById("back-btn");

  function fmtBytes(b) {
    if (!b) return "0 B";
    if (b < 1024) return b + " B";
    if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
    return (b / 1048576).toFixed(1) + " MB";
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
      if (b.type === "text") return b.text || "";
      if (b.type === "thinking") return "[thinking] " + (b.thinking || "");
      if (b.type === "tool_use") return "[tool_use: " + (b.name || "") + "]";
      if (b.type === "tool_result") return "[tool_result]";
      return JSON.stringify(b);
    }).join("\n");
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
        return '<div class="session-card" data-sid="' + s.session_id + '">'
          + '<div class="sid">' + s.session_id + '</div>'
          + '<div class="meta">'
          + '<span><strong>' + s.total_turns + '</strong> turns</span>'
          + '<span>' + fmtDuration(s.first_ts, s.last_ts) + '</span>'
          + '<span>' + fmtBytes(s.total_request_bytes + s.total_response_bytes) + '</span>'
          + '<span class="provider-badge">' + (s.provider || "anthropic") + '</span>'
          + '<span>' + fmtTime(s.last_ts) + '</span>'
          + '</div></div>';
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

    try {
      const [histResp, compResp] = await Promise.all([
        fetch("/api/v1/history/" + sid + "?include_thinking=1"),
        fetch("/api/v1/history/" + sid + "/compactions"),
      ]);
      const hist = await histResp.json();
      const comp = await compResp.json();

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
