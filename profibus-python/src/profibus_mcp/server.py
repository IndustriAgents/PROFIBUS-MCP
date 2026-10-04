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
from .tools import NOT_SENT, WRITE_TOOLS, SlaveMap, ToolResources, err, register_tools

SERVER_NAME = "PROFIBUS MCP Server"


class EnvelopeFastMCP(FastMCP):
    """FastMCP that answers invalid tool arguments with a ``success: false`` envelope.

    FastMCP validates arguments against the tool signature before the tool
    runs and turns a failure into a bare protocol error. Catching it here keeps
    the suite's ``{success, data, error, meta}`` contract for bad input too.
    """

    #: Name of the configured backend, reported in argument-error envelopes.
    backend_name: str = "unknown"

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            tool = self._tool_manager.get_tool(name)
            if tool is None:
                raise
            meta: dict[str, Any] = {"tool": name, "backend": self.backend_name}
            if name in WRITE_TOOLS:
                meta.update(NOT_SENT)
            envelope = err(_describe(exc), meta)
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
    if not config.writes_enabled:
        writes = "Writes are disabled (PROFIBUS_WRITES_ENABLED=false); write tools refuse every call."
    elif config.backend == "mock":
        writes = "Writes are enabled: write_outputs and write_slave_by_alias change the mock slave's outputs."
    else:
        writes = "Writes are enabled, but the serial backend is not implemented, so nothing is sent."
    return f"PROFIBUS DP tools. {source} {writes} Every tool returns {{success, data, error, meta}}."


class ProfibusMCPServer:
    def __init__(self, config: ProfibusConfig | None = None, master: ProfibusMaster | None = None) -> None:
        self.config = config or ProfibusConfig.from_env()
        self.master = master or ProfibusMaster(self.config)
        self.slave_map = SlaveMap(self.config.slave_map_path)
        self.gsd_parser = GSDParser(self.config.gsd_base_path)
        self.mcp = EnvelopeFastMCP(name=SERVER_NAME, instructions=_instructions(self.config))
        self.mcp.backend_name = self.master.backend.name
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
