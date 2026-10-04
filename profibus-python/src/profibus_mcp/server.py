"""FastMCP wiring for the PROFIBUS tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from . import __version__
from .config import ProfibusConfig
from .gsd_parser import GSDParser
from .pb_master import ProfibusMaster
from .tools import SlaveMap, ToolResources, err, register_tools

SERVER_NAME = "PROFIBUS MCP Server"


class EnvelopeFastMCP(FastMCP):
    """FastMCP that answers invalid tool arguments with a ``success: false`` envelope.

    FastMCP validates arguments against the tool signature before the tool
    runs and turns a failure into a bare protocol error. Catching it here keeps
    the suite's ``{success, data, error, meta}`` contract for bad input too.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            tool = self._tool_manager.get_tool(name)
            if tool is None:
                raise
            envelope = err(_describe(exc), {"tool": name})
            return tool.fn_metadata.convert_result(envelope)


def _describe(exc: ToolError) -> str:
    cause = exc.__cause__
    if isinstance(cause, ValidationError):
        problems = []
        for item in cause.errors():
            where = ".".join(str(part) for part in item["loc"]) or "arguments"
            problems.append(f"{where}: {item['msg']}")
        return "Invalid arguments: " + "; ".join(problems)
    return str(exc)


def _instructions(config: ProfibusConfig) -> str:
    if config.backend == "mock":
        source = (
            f"Backend: mock. Tools talk to profibus-mock-slave at {config.mock_host}:{config.mock_port} "
            "(simulated slaves, not a real bus)."
        )
    else:
        source = (
            "Backend: serial. Bus tools return success=false (not implemented): this server has no "
            "PROFIBUS DP master stack yet."
        )
    writes = (
        "Writes are ENABLED: write_outputs and write_slave_by_alias change physical outputs."
        if config.writes_enabled
        else "Writes are disabled (PROFIBUS_WRITES_ENABLED=false); write tools refuse every call."
    )
    return f"PROFIBUS DP tools. {source} {writes} Every tool returns {{success, data, error, meta}}."


class ProfibusMCPServer:
    def __init__(self, config: ProfibusConfig | None = None, master: ProfibusMaster | None = None) -> None:
        self.config = config or ProfibusConfig.from_env()
        self.master = master or ProfibusMaster(self.config)
        self.slave_map = SlaveMap(self.config.slave_map_path)
        self.gsd_parser = GSDParser(self.config.gsd_base_path)
        self.mcp = EnvelopeFastMCP(name=SERVER_NAME, instructions=_instructions(self.config))
        # FastMCP has no version argument; without this, serverInfo.version
        # reports the mcp SDK's version instead of ours.
        self.mcp._mcp_server.version = __version__
        register_tools(
            self.mcp,
            ToolResources(
                config=self.config,
                master=self.master,
                slave_map=self.slave_map,
                gsd_parser=self.gsd_parser,
            ),
        )

    def run(self) -> None:
        """Serve MCP over stdio until the client disconnects."""
        self.mcp.run()
