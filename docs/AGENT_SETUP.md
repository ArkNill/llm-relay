# Agent Setup Playbook

> **Audience**: an AI coding agent (Claude Code, OpenAI Codex, Gemini CLI)
> automating an llm-relay install on behalf of a human user.
> **This document is not a tutorial for humans** — it is a sequence of
> structured probes and actions an agent can follow without asking the
> user for technical details.

If you are a human, you probably want [`README.md`](../README.md) and the
`llm-relay init` command instead.

---

## What this playbook is

llm-relay ships with two agent-consumable primitives:

- **`llm-relay env-fingerprint --format json`** — describes the *state* of
  the user's environment (which CLIs are installed, what ports are free,
  what env vars are set, etc.). Read-only.
- **`llm-relay verify {install|config|integration|all} --format json`** —
  asserts *expectations* about that state, returning pass/fail/warn/skipped
  per check with optional remediation strings. Read-only.

This playbook sequences those two primitives — plus existing commands like
`llm-relay init` — into an end-to-end install flow.

You do not need to scrape human-friendly output. Every step in this
playbook either:

1. Calls a command that emits structured JSON.
2. Performs a single, idempotent filesystem / shell action.
3. Pauses for explicit user permission via a clearly labelled marker.

## What it is not

- It is not a substitute for the user's permission. Markers like
  **`[PERMISSION: install-package]`** below indicate steps where the user
  must approve a destructive or system-modifying action before you proceed.
- It is not a place to make undocumented decisions. If `env-fingerprint`
  reveals state this playbook does not branch on, **stop and ask the
  user** rather than guessing.

---

## Schema contract

Both primitives use `"schema_version": "1"`. If you see a different
`schema_version`, this playbook may be out of date for your llm-relay
release — fall back to the in-tree
[`docs/AGENT_SETUP.md`](AGENT_SETUP.md) from that release.

`verify` exit codes:
- `0` — overall is `pass` or `warn`. Safe to proceed.
- `1` — overall is `fail`. Do not proceed past the failing step.

---

## Phase 0 — Probe

Goal: understand what the user already has so later phases skip the work
that is already done.

```bash
llm-relay env-fingerprint --format json --no-doctor
```

Parse the result. Treat `_error` markers on any section as "section
unavailable" — do not abort the whole flow; just proceed with the
remaining sections.

Key fields you will need later:

| field | used in |
|---|---|
| `llm_relay.version` | Phase 1 — decide whether to install |
| `llm_relay.db_dir` | Phase 3 — where init writes |
| `clis[*].id`, `clis[*].installed`, `clis[*].auth.preferred` | Phase 5 — per-CLI wiring |
| `ports["8083"]` | Phase 3 — choose `--port` for init |
| `env.ANTHROPIC_BASE_URL` | Phase 5 — already-wired Claude Code |

If `clis[*].installed` is `false` for **every** CLI: stop and ask the user
to install at least one LLM CLI first — llm-relay has no purpose without
one. Do **not** install CLIs on the user's behalf in this playbook.

---

## Phase 1 — Install or upgrade the package

### Decision

- If `llm_relay.version` is `null` → llm-relay is not installed yet.
- If `llm_relay.version` is older than the version you are setting up
  against → upgrade.
- Otherwise → skip to Phase 2.

### Which extras to install

Decide from the fingerprint:

| Condition | Install |
|---|---|
| Always | `llm-relay` (base) |
| Any CLI installed | `llm-relay[proxy]` (the dashboard / proxy needs httpx + uvicorn) |
| `clis[*].id == "claude-code"` and `installed` | `llm-relay[mcp]` (Claude Code can register the MCP server) |
| User says they want PostgreSQL | `llm-relay[pg]` |
| Otherwise | `llm-relay[all]` is a safe default |

### Action

**`[PERMISSION: install-package]`** — installing into the user's Python
environment modifies their site-packages. Ask before running:

```bash
pip install llm-relay[<chosen-extras>]
```

If the user uses a virtual environment that you can detect (e.g. they ran
the agent from inside an activated venv), install there; otherwise default
to `pip install --user` and tell them which Python interpreter received the
install.

### Verify

```bash
llm-relay verify install --format json
```

- `overall == "pass"`: proceed.
- `overall == "warn"`: read the `warn` entries — typically
  `entry_point_mcp` or `proxy_extras` is missing because the user opted
  for a slim install. Acceptable unless the user explicitly asked for
  those features.
- `overall == "fail"`: read the failing check's `remediation`, attempt the
  remediation **once**, then re-verify. If it still fails, stop and report
  to the user.

---

## Phase 2 — Initialize local state

### Decision

- If `verify config` already returns `overall == "pass"` (or `warn` for
  only the optional `knowledge_dir` / `config_file` items) → skip to
  Phase 4.
- Otherwise → init.

### Action

**`[PERMISSION: write-config]`** — `llm-relay init` writes to
`~/.llm-relay/` and edits `~/.claude/settings.json` (if Claude Code is
installed). Ask before running.

Pick `--port`:
- If `ports["8083"]` is `"free"` → default `--port 8083`.
- If it is `"in_use"` → either the relay is already running on it (check
  by hitting `/_health` — Phase 5 covers this) or another process owns it.
  Ask the user before overriding; do not silently move to a new port.

```bash
llm-relay init --port <port>
```

### Verify

```bash
llm-relay verify config --port <port> --format json
```

`fail` here usually means the install partially completed. Read each
failing check's `remediation`. The most common cause is a permissions
issue on `~/.llm-relay/` — do not chmod files on the user's behalf
without **`[PERMISSION: fix-permissions]`**.

---

## Phase 3 — Per-CLI integration

For every CLI where `env-fingerprint` reported `installed: true`, run:

```bash
llm-relay verify integration --cli <cli-id> --format json
```

Then act based on the failing checks below. **Do not run integration
steps for CLIs the user does not have installed.**

### Claude Code (`claude-code`)

| Failing check | Action |
|---|---|
| `binary` | Cannot happen if `env-fingerprint` said `installed: true`; if it does, treat as a transient PATH issue and re-probe. |
| `settings_present` | Have the user open Claude Code once; the binary creates `~/.claude/settings.json` on first run. |
| `proxy_route` | `llm-relay init` should have set this. If `init` already ran and this is still `fail`, inspect `~/.claude/settings.json` `env.ANTHROPIC_BASE_URL` — fix to `http://localhost:<port>` only with **`[PERMISSION: edit-claude-settings]`**. |
| `mcp_server` | Same as above — `init` writes `mcpServers["llm-relay"]`. If missing, re-run `init`; do not edit the JSON directly unless `init` itself fails. |

### OpenAI Codex (`openai-codex`)

The `proxy_route` check is `skipped` by design — Codex does not currently
expose a stable routing knob. Do **not** attempt to monkey-patch Codex's
config to route through llm-relay; that is upstream work, not yours.

`binary` and `config_present` should both pass after the user has run
`codex` at least once.

### Gemini CLI (`gemini-cli`)

`oauth_known_issue` is always `warn` and surfaces upstream
[google-gemini/gemini-cli#25425](https://github.com/google-gemini/gemini-cli/issues/25425).
If the user reports a 403 from Gemini, tell them to set
`GEMINI_API_KEY` instead of relying on oauth-personal.

---

## Phase 4 — Optional: start the server

Only if the user explicitly asked for the dashboard or a background
service. Otherwise leave it for them to start with `llm-relay serve`.

**Linux / macOS (manual):**

```bash
llm-relay serve --port <port>
```

**Windows (background service):**

**`[PERMISSION: install-service]`** — this registers an auto-start entry
in the user's Startup Folder. Ask before running.

```bash
llm-relay service install --port <port>
llm-relay service start --port <port>
```

### Verify

After the server is running:

```bash
llm-relay verify integration --cli all --live --port <port> --format json
```

The `--live` flag adds a `proxy_reachable_live` check that hits
`/_health` on the running server. A `pass` here is the strongest signal
that the install actually works end-to-end.

---

## Phase 5 — Final acceptance

Run the full suite:

```bash
llm-relay verify all --port <port> --format json
```

- `overall == "pass"` or `"warn"` → tell the user the install is
  complete, summarising any `warn` items so they know what is
  intentionally optional.
- `overall == "fail"` → do **not** declare success. Report which check
  failed, what its `remediation` was, and what you attempted. Hand back
  to the user.

A reasonable summary message to the user (text, not JSON):

> Set up llm-relay 0.9.X. Detected `<list of CLIs>`. Claude Code is now
> routed through `http://localhost:<port>` and the llm-relay MCP server
> is registered. Open `http://localhost:<port>/dashboard/` for the
> dashboard. `N warn(s)` — optional pieces you can install later if you
> need them: `<list>`.

---

## Permission markers (collected)

This playbook never assumes user consent for any of the following. Each
must be preceded by a `[PERMISSION: ...]` prompt that names exactly what
you are about to do:

- **`install-package`** — running `pip install`.
- **`write-config`** — running `llm-relay init`, which writes to
  `~/.llm-relay/` and may edit `~/.claude/settings.json`.
- **`edit-claude-settings`** — directly editing `~/.claude/settings.json`
  outside of `init` (rare; only as a last resort if `init` cannot
  resolve a `verify integration` failure).
- **`fix-permissions`** — `chmod` / `chown` on files in the user's home
  directory.
- **`install-service`** — registering the Windows background service.

---

## What not to do

- Do **not** read or modify `~/.llm-relay/usage.db` or
  `~/.claude/*.jsonl` session transcripts as part of setup. Those are
  runtime data; setup should not touch them.
- Do **not** install LLM CLIs on the user's behalf. Each CLI vendor has
  its own install path (`npm install -g @anthropic-ai/claude-code`,
  Codex installer, Gemini CLI install, etc.) and the user should run
  those themselves.
- Do **not** change `~/.bashrc` / `~/.zshrc` to "fix PATH" automatically.
  If the entry point is missing from PATH, report it and ask the user
  how they want to handle it.
- Do **not** retry a failing `verify` more than once with the same
  remediation. If the first attempt did not fix it, the situation needs
  human attention.

---

## When to stop and ask

The agent should fall back to the human user — not improvise — in any of
these situations:

- `env-fingerprint` reports no CLI installed.
- `verify` returns `overall == "fail"` and the failing check has no
  `remediation` string.
- A `remediation` proposes editing a file outside the standard paths
  (`~/.llm-relay/`, `~/.claude/settings.json`).
- Anything in this playbook is ambiguous for the user's specific setup
  (e.g. multiple Python interpreters, multiple Claude Code installs).

A short, specific question to the user is always preferable to a wrong
auto-fix.
