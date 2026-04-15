"""Tests for cc_cache_fix — Phase 1: TTL injection, tool ordering, block detection."""

from __future__ import annotations

import json

from llm_relay.proxy.cc_cache_fix import (
    classify_block,
    inject_ttl,
    is_deferred_tools_block,
    is_hooks_block,
    is_mcp_block,
    is_skills_block,
    is_system_reminder,
    normalize_request,
    sort_tools,
)

# ============================================================================
# Block detection
# ============================================================================


class TestBlockDetection:
    def test_is_system_reminder(self):
        assert is_system_reminder("<system-reminder>foo")
        assert is_system_reminder("<system-reminder>\nbar")
        assert not is_system_reminder("not a reminder")
        assert not is_system_reminder("")
        assert not is_system_reminder(None)  # type: ignore[arg-type]

    def test_is_hooks_block(self):
        text = "<system-reminder>\nhook success: ls completed"
        assert is_hooks_block(text)
        # "hook success" must be in first 200 chars
        text_late = "<system-reminder>\n" + ("x" * 200) + "hook success"
        assert not is_hooks_block(text_late)
        assert not is_hooks_block("not a reminder")

    def test_is_skills_block(self):
        text = "<system-reminder>\nThe following skills are available for use"
        assert is_skills_block(text)
        assert not is_skills_block("<system-reminder>\nSomething else")

    def test_is_deferred_tools_block(self):
        text = "<system-reminder>\nThe following deferred tools are now available for use"
        assert is_deferred_tools_block(text)
        assert not is_deferred_tools_block("<system-reminder>\nNot deferred")

    def test_is_mcp_block(self):
        text = "<system-reminder>\n# MCP Server Instructions\n\nThe following MCP servers"
        assert is_mcp_block(text)
        assert not is_mcp_block("<system-reminder>\n# Something Else")

    def test_classify_block(self):
        assert classify_block("<system-reminder>\nhook success: done") == "hooks"
        assert classify_block("<system-reminder>\nThe following skills are available") == "skills"
        assert classify_block("<system-reminder>\nThe following deferred tools are now available") == "deferred"
        assert classify_block("<system-reminder>\n# MCP Server Instructions") == "mcp"
        assert classify_block("<system-reminder>\nSomething generic") is None
        assert classify_block("plain text") is None
        assert classify_block("") is None

    def test_classify_block_priority(self):
        """hooks check runs before skills — ensure a block with 'hook success' is hooks, not generic."""
        text = "<system-reminder>\nhook success: The following skills are available"
        assert classify_block(text) == "hooks"


# ============================================================================
# TTL injection
# ============================================================================


class TestTTLInjection:
    def test_inject_system_blocks(self):
        req = {
            "system": [
                {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "world"},
            ],
            "messages": [],
        }
        count = inject_ttl(req)
        assert count == 1
        assert req["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert "cache_control" not in req["system"][1]

    def test_inject_message_content_blocks(self):
        req = {
            "system": [],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "q", "cache_control": {"type": "ephemeral"}},
                        {"type": "text", "text": "r", "cache_control": {"type": "ephemeral", "ttl": "5m"}},
                    ],
                }
            ],
        }
        count = inject_ttl(req)
        assert count == 1
        assert req["messages"][0]["content"][0]["cache_control"]["ttl"] == "1h"
        # Existing ttl should NOT be overwritten
        assert req["messages"][0]["content"][1]["cache_control"]["ttl"] == "5m"

    def test_no_injection_when_ttl_present(self):
        req = {
            "system": [
                {"type": "text", "text": "x", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            ],
            "messages": [],
        }
        count = inject_ttl(req)
        assert count == 0

    def test_no_injection_when_not_ephemeral(self):
        req = {
            "system": [
                {"type": "text", "text": "x", "cache_control": {"type": "persistent"}},
            ],
            "messages": [],
        }
        count = inject_ttl(req)
        assert count == 0

    def test_no_system_key(self):
        req = {"messages": []}
        count = inject_ttl(req)
        assert count == 0

    def test_string_content_messages(self):
        """Messages with string content (not list) should not crash."""
        req = {
            "system": [],
            "messages": [{"role": "user", "content": "hello"}],
        }
        count = inject_ttl(req)
        assert count == 0


# ============================================================================
# Tool ordering
# ============================================================================


class TestToolOrdering:
    def test_sort_tools(self):
        req = {
            "tools": [
                {"name": "Grep", "description": "search"},
                {"name": "Bash", "description": "run"},
                {"name": "Read", "description": "read"},
            ]
        }
        changed = sort_tools(req)
        assert changed is True
        names = [t["name"] for t in req["tools"]]
        assert names == ["Bash", "Grep", "Read"]

    def test_already_sorted(self):
        req = {
            "tools": [
                {"name": "Bash", "description": "run"},
                {"name": "Grep", "description": "search"},
                {"name": "Read", "description": "read"},
            ]
        }
        changed = sort_tools(req)
        assert changed is False

    def test_empty_tools(self):
        assert sort_tools({"tools": []}) is False
        assert sort_tools({}) is False

    def test_missing_names(self):
        req = {
            "tools": [
                {"name": "Zebra"},
                {},  # no name
                {"name": "Apple"},
            ]
        }
        changed = sort_tools(req)
        assert changed is True
        names = [t.get("name", "") for t in req["tools"]]
        assert names == ["", "Apple", "Zebra"]

    def test_non_mutating_original_list_ref(self):
        """sort_tools replaces req['tools'] with a new sorted list."""
        original_tools = [{"name": "B"}, {"name": "A"}]
        req = {"tools": original_tools}
        sort_tools(req)
        # Original list object should be unchanged
        assert original_tools[0]["name"] == "B"
        assert original_tools[1]["name"] == "A"
        # req["tools"] is a new sorted list
        assert req["tools"][0]["name"] == "A"


# ============================================================================
# normalize_request integration
# ============================================================================


class TestNormalizeRequest:
    def _make_request(self):
        return {
            "model": "claude-sonnet-4-20250514",
            "stream": True,
            "system": [
                {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.101.a3f; cc_entrypoint=cli"},
                {"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hello world"},
                    ],
                }
            ],
            "tools": [
                {"name": "Write", "description": "write"},
                {"name": "Bash", "description": "bash"},
                {"name": "Read", "description": "read"},
            ],
        }

    def _make_headers(self):
        return {
            "x-claude-code-session-id": "test-session-123",
        }

    def test_modifies_and_returns_diagnostics(self):
        req = self._make_request()
        headers = self._make_headers()
        modified, diag = normalize_request(req, headers)
        assert modified is True
        assert diag is not None

        # TTL injected (system[1] has cache_control, system[0] is billing header)
        assert req["system"][1]["cache_control"]["ttl"] == "1h"
        assert diag["ttl_injected"] == 1

        # Tools sorted
        assert req["tools"][0]["name"] == "Bash"
        assert diag["tools_reordered"] == 1

        # Version extracted
        assert diag["cc_version"] == "2.1.101"
        assert diag["fingerprint"] == "a3f"

    def test_no_modification_when_already_normalized(self):
        req = self._make_request()
        req["system"][1]["cache_control"]["ttl"] = "1h"
        req["tools"] = sorted(req["tools"], key=lambda t: t.get("name", ""))
        headers = self._make_headers()

        modified, diag = normalize_request(req, headers)
        assert modified is False
        assert diag is not None  # diagnostics still captured

    def test_pass_through_on_bad_input(self):
        """normalize_request should not crash on unexpected input."""
        modified, diag = normalize_request({}, {})
        assert modified is False

    def test_diagnostics_drifted_blocks(self):
        """Detect blocks that drifted from messages[0]."""
        req = {
            "system": [],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "question"}]},
                {"role": "assistant", "content": "answer"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "<system-reminder>\n# MCP Server Instructions\nstuff"},
                        {"type": "text", "text": "follow-up question"},
                    ],
                },
            ],
        }
        headers = {}
        _, diag = normalize_request(req, headers)
        assert diag is not None
        drifted = json.loads(diag["drifted_blocks"])
        assert "mcp" in drifted
        assert 2 in drifted["mcp"]

    def test_version_extraction_3part(self):
        """3-part version (no fingerprint) should still extract."""
        req = self._make_request()
        # Replace billing header with 3-part version
        req["system"][0] = {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.101; cc_entrypoint=cli"}
        req["system"][1]["cache_control"]["ttl"] = "1h"
        req["tools"] = sorted(req["tools"], key=lambda t: t.get("name", ""))
        _, diag = normalize_request(req, {})
        assert diag["cc_version"] == "2.1.101"
        assert diag["fingerprint"] is None
        assert diag["fingerprint"] is None
