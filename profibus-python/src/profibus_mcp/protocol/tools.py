from __future__ import annotations

from typing import Annotated

from pydantic import Field

from profibus_mcp.application.resources import ToolResources
from profibus_mcp.domain.model import Alias, Envelope, OutputData, ReadLength, SlaveAddress

from ..application.operations import Operations
from .support import _LOCAL, _READ, _WRITE, FastMCP, capture_request_cancel_scope


def register_tools(server: FastMCP, resources: ToolResources) -> None:
    operations = Operations(resources, lambda: capture_request_cancel_scope(server))

    @server.tool(annotations=_READ)
    async def scan_bus() -> Envelope:
        """List the DP slaves the backend reports: address, ident number, status and I/O lengths.

        Read-only. With PROFIBUS_BACKEND=serial it returns success=false (not implemented).
        """
        return await operations.scan_bus()

    @server.tool(annotations=_WRITE)
    async def read_inputs(slave_address: SlaveAddress, length: ReadLength = None) -> Envelope:
        """Read a slave's cyclic input bytes, returned as hex in raw_data_hex.

        May start the enabled pyprofibus bus and transmit configured initial outputs.
        Requires PROFIBUS_BUS_ENABLED for pyprofibus; read_only MCP policy denies this tool.
        """
        return await operations.read_inputs(slave_address, length)

    @server.tool(annotations=_WRITE)
    async def write_outputs(slave_address: SlaveAddress, data: OutputData) -> Envelope:
        """Write output bytes to a slave. This changes physical outputs on the equipment.

        Refused unless PROFIBUS_WRITES_ENABLED=true (default false); nothing is sent when refused.
        """
        return await operations.write_outputs(slave_address, data)

    @server.tool(annotations=_READ)
    async def read_diagnosis(slave_address: SlaveAddress) -> Envelope:
        """Read a slave's diagnosis (status and ident number) as the backend reports it. Read-only."""
        return await operations.read_diagnosis(slave_address)

    @server.tool(annotations=_READ)
    async def test_slave_communication(slave_address: SlaveAddress) -> Envelope:
        """Check that a slave answers, by requesting its diagnosis. Read-only.

        success=true means the slave answered; success=false gives the reason.
        """
        return await operations.test_slave_communication(slave_address)

    @server.tool(annotations=_LOCAL)
    async def list_slaves() -> Envelope:
        """List the aliases defined in SLAVE_MAP_FILE (alias, address, description). Does not touch the bus."""
        return await operations.list_slaves()

    @server.tool(annotations=_WRITE)
    async def read_slave_by_alias(alias: Alias, length: ReadLength = None) -> Envelope:
        """read_inputs for the slave behind an alias; may start the explicitly enabled pyprofibus bus."""
        return await operations.read_slave_by_alias(alias, length)

    @server.tool(annotations=_WRITE)
    async def write_slave_by_alias(alias: Alias, data: OutputData) -> Envelope:
        """write_outputs for the slave behind an alias. This changes physical outputs on the equipment.

        Refused unless PROFIBUS_WRITES_ENABLED=true (default false); nothing is sent when refused.
        """
        return await operations.write_slave_by_alias(alias, data)

    @server.tool(annotations=_LOCAL)
    async def load_gsd_file(filepath: Annotated[str, Field(min_length=1, description="Path or file name")]) -> Envelope:
        """Parse a PROFIBUS DP GSD file (ASCII keyword format): vendor, model, ident number, baud rates, modules.

        Each module's I/O lengths are decoded from its configuration bytes. A bare file
        name is also looked up in PROFIBUS_GSD_PATH. Does not touch the bus.
        """
        return await operations.load_gsd_file(filepath)

    @server.tool(annotations=_LOCAL)
    async def ping() -> Envelope:
        """Report server version, backend, write gate, alias count and any SLAVE_MAP_FILE error.

        Does not contact the bus.
        """
        return await operations.ping()

    @server.tool(annotations=_LOCAL)
    async def get_master_status() -> Envelope:
        """Report the master configuration: backend, port or mock endpoint, baud rate, address, timeouts, write gate.

        Does not contact the bus.
        """
        return await operations.get_master_status()

    from .contract import install_contract

    install_contract(server)
