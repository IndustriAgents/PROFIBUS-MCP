from __future__ import annotations

from profibus_mcp import __version__
from profibus_mcp.domain.capabilities import capabilities
from profibus_mcp.domain.model import Envelope, SlaveAddress, ok


class DiagnosticsOperations:
    async def test_slave_communication(self, slave_address: SlaveAddress) -> Envelope:
        """Check that a slave answers, by requesting its diagnosis. Read-only."""
        try:
            with self.cancellable():
                diag, meta = await self.master.read_diagnosis(slave_address)
        except Exception as exc:
            return self.failure(exc, {"slave_address": slave_address})
        data = {
            "slave_address": slave_address,
            "reachable": True,
            "status": diag.get("status"),
            "ident_number": diag.get("ident_number"),
        }
        return ok(data, meta)

    async def ping(self) -> Envelope:
        """Report server version, backend, write gate, alias count and any SLAVE_MAP_FILE error."""
        data = {
            "server": "PROFIBUS MCP Server",
            "version": __version__,
            "backend": self.backend,
            "bus_communication": self.master.backend.status().get("bus_communication"),
            "writes_enabled": self.config.writes_enabled,
            "slave_aliases": self.resources.slave_map.count(),
            "slave_map_error": self.resources.slave_map.error,
        }
        return ok(data, {"backend": self.backend})

    async def get_master_status(self) -> Envelope:
        """Report the master configuration: backend, port or mock endpoint, baud rate, address, timeouts, write gate."""
        return ok(
            {
                **self.master.status(),
                "version": __version__,
                "slave_aliases": self.resources.slave_map.count(),
                "slave_map_error": self.resources.slave_map.error,
                "capabilities": capabilities(self.backend, writes_enabled=self.config.writes_enabled),
            },
            {"backend": self.backend},
        )
