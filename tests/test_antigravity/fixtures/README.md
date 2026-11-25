Sanitized samples of Antigravity CLI 1.5.13 Code Assist traffic (2026-11-20).

- `req_*.json` / `resp_*.sse`: `POST /v1internal:streamGenerateContent?alt=sse` request bodies and
  SSE response bodies for one conversation: turn 1 (model calls two tools), turn 1 after the tool
  results, turn 2 (conversation continued), plus one `agent_monitoring` side call.
- `quota_summary.json`: `POST /v1internal:retrieveUserQuotaSummary` response.

System prompt, tool declarations and the monitoring prompt are replaced by short placeholders;
ids, paths and signatures are fake. SSE events are separated by CRLF CRLF as on the wire.
