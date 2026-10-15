from __future__ import annotations

from profibus_mcp.domain.model import NOT_SENT, Alias, Envelope, OutputData, SlaveAddress, err


class WriteOperations:
    async def write_outputs(self, slave_address: SlaveAddress, data: OutputData) -> Envelope:
        """Write output bytes to a slave. This changes physical outputs on the equipment."""
        return await self.do_write(slave_address, data)

    async def write_slave_by_alias(self, alias: Alias, data: OutputData) -> Envelope:
        """write_outputs for the slave behind an alias. This changes physical outputs on the equipment."""
        if not self.config.writes_enabled:
            return err(
                "Writes are disabled: set PROFIBUS_WRITES_ENABLED=true to allow them. Nothing was sent.",
                {"alias": alias, "backend": self.backend, "writes_enabled": False, **NOT_SENT},
            )
        address, problem = self.resolve_alias(alias, write=True)
        if problem:
            return problem
        return await self.do_write(address, data, {"alias": alias})
