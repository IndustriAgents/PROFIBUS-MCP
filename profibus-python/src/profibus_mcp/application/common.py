from __future__ import annotations

from typing import Any

from profibus_mcp.domain.errors import ProfibusMasterError, request_cancel_scope
from profibus_mcp.domain.model import NOT_SENT, Envelope, alias_problem, err, log, ok


class CommonOperations:
    def __init__(self, resources, cancel_scope):
        self.resources = resources
        self.cancel_scope = cancel_scope
        self.config = self.resources.config
        self.master = self.resources.master
        self.backend = self.master.backend.name

    def failure(self, exc: Exception, meta: dict[str, Any] | None = None) -> Envelope:
        if isinstance(exc, ProfibusMasterError):
            return err(str(exc), {**(meta or {}), **exc.meta})
        log.exception("unexpected error in a PROFIBUS tool")
        return err(f"Unexpected error: {exc}", {"backend": self.backend, **(meta or {})})

    def resolve_alias(self, alias: str, *, write: bool = False) -> tuple[int | None, Envelope | None]:
        spec = self.resources.slave_map.get(alias)
        meta = {"alias": alias, "backend": self.backend, **(NOT_SENT if write else {})}
        if self.resources.slave_map.error:
            return (None, err(self.resources.slave_map.error, meta))
        if spec is None:
            return (None, err(f"Unknown alias '{alias}'", meta))
        problem = alias_problem(spec)
        if problem:
            return (None, err(f"Alias '{alias}' in SLAVE_MAP_FILE: {problem}", meta))
        return (spec["address"], None)

    def cancellable(self):
        """Expose this call's MCP cancel scope to the pre-send gate."""
        return request_cancel_scope(self.cancel_scope())

    async def do_read(self, slave_address: int, length: int | None, extra: dict[str, Any] | None = None) -> Envelope:
        try:
            with self.cancellable():
                payload, meta = await self.master.read_inputs(slave_address, length)
        except Exception as exc:
            return self.failure(exc, {"slave_address": slave_address, **(extra or {})})
        data = {"slave_address": slave_address, "length": len(payload), "raw_data_hex": payload.hex(), **(extra or {})}
        return ok(data, meta)

    async def do_write(self, slave_address: int, data: list[int], extra: dict[str, Any] | None = None) -> Envelope:
        try:
            with self.cancellable():
                written, meta = await self.master.write_outputs(slave_address, data)
        except ProfibusMasterError as exc:
            return self.failure(exc, {"slave_address": slave_address, **(extra or {})})
        except Exception as exc:
            log.exception("unexpected error in a PROFIBUS write")
            return err(
                f"Unexpected error: {exc}. The write may have been applied; check the slave's outputs before retrying.",
                {
                    "backend": self.backend,
                    "slave_address": slave_address,
                    "outcome": "unknown",
                    "request_sent": True,
                    **(extra or {}),
                },
            )
        return ok({"slave_address": slave_address, "written_bytes": written, **(extra or {})}, meta)
