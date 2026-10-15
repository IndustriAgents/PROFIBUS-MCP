from __future__ import annotations

from profibus_mcp.domain.model import Alias, Envelope, ReadLength, SlaveAddress, ok


class ReadOperations:
    async def read_inputs(self, slave_address: SlaveAddress, length: ReadLength = None) -> Envelope:
        """Read a slave's cyclic input bytes, returned as hex in raw_data_hex."""
        return await self.do_read(slave_address, length)

    async def read_diagnosis(self, slave_address: SlaveAddress) -> Envelope:
        """Read a slave's diagnosis (status and ident number) as the backend reports it. Read-only."""
        try:
            with self.cancellable():
                diag, meta = await self.master.read_diagnosis(slave_address)
        except Exception as exc:
            return self.failure(exc, {"slave_address": slave_address})
        return ok(diag, meta)

    async def read_slave_by_alias(self, alias: Alias, length: ReadLength = None) -> Envelope:
        """read_inputs for the slave behind an alias from SLAVE_MAP_FILE. Read-only."""
        address, problem = self.resolve_alias(alias)
        if problem:
            return problem
        return await self.do_read(address, length, {"alias": alias})
