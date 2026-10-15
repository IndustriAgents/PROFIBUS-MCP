from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from profibus_mcp.domain.model import Envelope, err, ok
from profibus_mcp.gsd_parser import GSDParserError


class MetadataOperations:
    async def list_slaves(self) -> Envelope:
        """List the aliases defined in SLAVE_MAP_FILE (alias, address, description). Does not touch the bus."""
        entries = self.resources.slave_map.entries()
        meta = {
            "source": "slave_map",
            "path": str(self.resources.slave_map.path) if self.resources.slave_map.path else None,
        }
        if self.resources.slave_map.error:
            return err(self.resources.slave_map.error, meta)
        return ok({"slaves": entries, "count": len(entries)}, meta)

    async def load_gsd_file(
        self, filepath: Annotated[str, Field(min_length=1, description="Path or file name")]
    ) -> Envelope:
        """Parse a PROFIBUS DP GSD file (ASCII keyword format): vendor, model, ident number, baud rates, modules."""
        meta: dict[str, Any] = {"source": "gsd_file", "filepath": filepath}
        try:
            data, cached = self.resources.gsd_parser.load_with_status(filepath)
        except GSDParserError as exc:
            return err(str(exc), meta)
        except Exception as exc:
            return self.failure(exc, meta)
        return ok(data, {**meta, "cached": cached})
