from __future__ import annotations

from typing import Any as Any

from mcp.server.fastmcp import FastMCP as FastMCP
from mcp.types import ToolAnnotations as ToolAnnotations

_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
_LOCAL = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


def capture_request_cancel_scope(server: FastMCP) -> Any:
    """The cancel scope of the MCP request being served, or None.

    This reads private mcp<2 internals (``session._in_flight[request_id]
    ._cancel_scope``), hence the pin and a test that fails if they move.
    """
    try:
        rc = server.get_context().request_context
    except (LookupError, ValueError):
        return None
    in_flight = getattr(getattr(rc, "session", None), "_in_flight", None)
    if not isinstance(in_flight, dict):
        return None
    responder = in_flight.get(getattr(rc, "request_id", None))
    return getattr(responder, "_cancel_scope", None)
