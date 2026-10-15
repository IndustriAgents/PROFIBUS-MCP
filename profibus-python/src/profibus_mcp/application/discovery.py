from __future__ import annotations

from profibus_mcp.domain.model import Envelope, ok


class DiscoveryOperations:
    async def scan_bus(self) -> Envelope:
        """List the DP slaves the backend reports: address, ident number, status and I/O lengths."""
        try:
            with self.cancellable():
                slaves, meta = await self.master.scan_bus()
        except Exception as exc:
            return self.failure(exc)
        return ok({"slaves": slaves}, meta)
