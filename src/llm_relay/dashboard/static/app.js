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

  // Auto-refresh every 30s
  setInterval(function () { loadHealth(); loadStats(); loadHistory(); }, 30000);
})();
