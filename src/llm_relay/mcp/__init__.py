"""MCP server exposing CLI orchestration tools via FastMCP (stdio transport)."""


def run_server() -> None:
    """Entry point for llm-relay-mcp script."""
    from llm_relay.mcp.server import mcp

    mcp.run(transport="stdio")
