"""PROFIBUS MCP tool definitions.

Every tool returns the suite's ``{success, data, error, meta}`` envelope and
never raises: bus failures, refused writes and bad arguments all come back as
``success: false`` with a readable ``error``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field, Strict

from . import __version__
from .config import MAX_STATION_ADDRESS, ProfibusConfig
from .gsd_parser import GSDParser, GSDParserError
from .pb_master import (
    MAX_DP_DATA_LEN,
    ProfibusMaster,
    ProfibusMasterError,
    request_cancel_scope,
    validate_slave_address,
)

log = logging.getLogger(__name__)

Envelope = dict[str, Any]


def envelope(success: bool, data: Any = None, error: str | None = None, meta: dict[str, Any] | None = None) -> Envelope:
    return {"success": success, "data": data, "error": error, "meta": meta or {}}


def ok(data: Any = None, meta: dict[str, Any] | None = None) -> Envelope:
    return envelope(True, data=data, meta=meta)


def err(message: str, meta: dict[str, Any] | None = None) -> Envelope:
    return envelope(False, error=message, meta=meta)


# Strict integers: pydantic's lax mode would accept true/false as 1/0, "5"
# and 5.0. Addresses, lengths and byte values must be real JSON integers.
SlaveAddress = Annotated[
    int,
    Field(ge=0, le=MAX_STATION_ADDRESS, description=f"DP station address of the slave (0-{MAX_STATION_ADDRESS})"),
    Strict(),
]
ReadLength = Annotated[
    int | None,
    Field(
        ge=1,
        le=MAX_DP_DATA_LEN,
        description="Number of input bytes to read (1-244). Omit to read all of the slave's input bytes.",
    ),
    Strict(),
]
ByteValue = Annotated[int, Field(ge=0, le=255), Strict()]
OutputData = Annotated[
    list[ByteValue],
    Field(
        min_length=1,
        max_length=MAX_DP_DATA_LEN,
        description="Output bytes to write, each 0-255, starting at the first output byte.",
    ),
]
Alias = Annotated[str, Field(min_length=1, description="Alias defined in SLAVE_MAP_FILE")]

#: Tools that change outputs; their failures always say whether anything was sent.
WRITE_TOOLS = frozenset({"write_outputs", "write_slave_by_alias"})
NOT_SENT: dict[str, Any] = {"outcome": "not_sent", "request_sent": False}

_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
_LOCAL = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
# Writes never advertise idempotency: a client must not retry them on its own.
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


class SlaveMap:
    """Alias -> slave map from ``SLAVE_MAP_FILE``, reloaded when the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._slaves: dict[str, Any] = {}
        self._stamp: tuple[int, int] | None = None
        self.error: str | None = None
        self.refresh()

    def refresh(self) -> None:
        if not self.path:
            self._slaves, self._stamp, self.error = {}, None, None
            return
        try:
            stat = self.path.stat()
        except OSError as exc:
            self._slaves, self._stamp = {}, None
            self.error = f"SLAVE_MAP_FILE {self.path} cannot be read: {exc.strerror}"
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self._slaves = {}
            self.error = f"SLAVE_MAP_FILE {self.path} is not valid JSON: {exc}"
            return
        if not isinstance(data, dict):
            self._slaves = {}
            self.error = f"SLAVE_MAP_FILE {self.path} must hold a JSON object of alias -> slave"
            return
        self._slaves = {str(alias): spec for alias, spec in data.items()}
        self.error = None

    def get(self, alias: str) -> Any:
        self.refresh()
        return self._slaves.get(alias)

    def entries(self) -> list[dict[str, Any]]:
        self.refresh()
        listed = []
        for alias, spec in self._slaves.items():
            entry: dict[str, Any] = {"alias": alias}
            if isinstance(spec, dict):
                entry["address"] = spec.get("address")
                entry["description"] = spec.get("description")
            problem = alias_problem(spec)
            if problem:
                entry["error"] = problem
            listed.append(entry)
        return listed

    def count(self) -> int:
        self.refresh()
        return len(self._slaves)


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


def alias_problem(spec: Any) -> str | None:
    if not isinstance(spec, dict):
        return "entry must be an object with an 'address'"
    try:
        validate_slave_address(spec.get("address"))
    except ProfibusMasterError as exc:
        return str(exc).replace("slave_address", "address", 1)
    return None


@dataclass(slots=True)
class ToolResources:
    config: ProfibusConfig
    master: ProfibusMaster
    slave_map: SlaveMap
    gsd_parser: GSDParser = field(default_factory=GSDParser)


def register_tools(server: FastMCP, resources: ToolResources) -> None:
    config = resources.config
    master = resources.master
    backend = master.backend.name

    def failure(exc: Exception, meta: dict[str, Any] | None = None) -> Envelope:
        if isinstance(exc, ProfibusMasterError):
            return err(str(exc), {**(meta or {}), **exc.meta})
        log.exception("unexpected error in a PROFIBUS tool")
        return err(f"Unexpected error: {exc}", {"backend": backend, **(meta or {})})

    def resolve_alias(alias: str, *, write: bool = False) -> tuple[int | None, Envelope | None]:
        spec = resources.slave_map.get(alias)
        meta = {"alias": alias, "backend": backend, **(NOT_SENT if write else {})}
        if resources.slave_map.error:
            return None, err(resources.slave_map.error, meta)
        if spec is None:
            return None, err(f"Unknown alias '{alias}'", meta)
        problem = alias_problem(spec)
        if problem:
            return None, err(f"Alias '{alias}' in SLAVE_MAP_FILE: {problem}", meta)
        return spec["address"], None

    def cancellable():
        """Expose this call's MCP cancel scope to the pre-send gate."""
        return request_cancel_scope(capture_request_cancel_scope(server))

    async def do_read(slave_address: int, length: int | None, extra: dict[str, Any] | None = None) -> Envelope:
        try:
            with cancellable():
                payload, meta = await master.read_inputs(slave_address, length)
        except Exception as exc:
            return failure(exc, {"slave_address": slave_address, **(extra or {})})
        data = {"slave_address": slave_address, "length": len(payload), "raw_data_hex": payload.hex(), **(extra or {})}
        return ok(data, meta)

    async def do_write(slave_address: int, data: list[int], extra: dict[str, Any] | None = None) -> Envelope:
        try:
            with cancellable():
                written, meta = await master.write_outputs(slave_address, data)
        except ProfibusMasterError as exc:
            return failure(exc, {"slave_address": slave_address, **(extra or {})})
        except Exception as exc:
            log.exception("unexpected error in a PROFIBUS write")
            return err(
                f"Unexpected error: {exc}. The write may have been applied; check the slave's outputs before retrying.",
                {
                    "backend": backend,
                    "slave_address": slave_address,
                    "outcome": "unknown",
                    "request_sent": True,
                    **(extra or {}),
                },
            )
        return ok({"slave_address": slave_address, "written_bytes": written, **(extra or {})}, meta)

    @server.tool(annotations=_READ)
    async def scan_bus() -> Envelope:
        """List the DP slaves the backend reports: address, ident number, status and I/O lengths.

        Read-only. With PROFIBUS_BACKEND=serial it returns success=false (not implemented).
        """
        try:
            with cancellable():
                slaves, meta = await master.scan_bus()
        except Exception as exc:
            return failure(exc)
        return ok({"slaves": slaves}, meta)

    @server.tool(annotations=_READ)
    async def read_inputs(slave_address: SlaveAddress, length: ReadLength = None) -> Envelope:
        """Read a slave's cyclic input bytes, returned as hex in raw_data_hex.

        Read-only. Fails if the slave does not answer or has fewer input bytes than requested.
        """
        return await do_read(slave_address, length)

    @server.tool(annotations=_WRITE)
    async def write_outputs(slave_address: SlaveAddress, data: OutputData) -> Envelope:
        """Write output bytes to a slave. This changes physical outputs on the equipment.

        Refused unless PROFIBUS_WRITES_ENABLED=true (default false); nothing is sent when refused.
        """
        return await do_write(slave_address, data)

    @server.tool(annotations=_READ)
    async def read_diagnosis(slave_address: SlaveAddress) -> Envelope:
        """Read a slave's diagnosis (status and ident number) as the backend reports it. Read-only."""
        try:
            with cancellable():
                diag, meta = await master.read_diagnosis(slave_address)
        except Exception as exc:
            return failure(exc, {"slave_address": slave_address})
        return ok(diag, meta)

    @server.tool(annotations=_READ)
    async def test_slave_communication(slave_address: SlaveAddress) -> Envelope:
        """Check that a slave answers, by requesting its diagnosis. Read-only.

        success=true means the slave answered; success=false gives the reason.
        """
        try:
            with cancellable():
                diag, meta = await master.read_diagnosis(slave_address)
        except Exception as exc:
            return failure(exc, {"slave_address": slave_address})
        data = {
            "slave_address": slave_address,
            "reachable": True,
            "status": diag.get("status"),
            "ident_number": diag.get("ident_number"),
        }
        return ok(data, meta)

    @server.tool(annotations=_LOCAL)
    async def list_slaves() -> Envelope:
        """List the aliases defined in SLAVE_MAP_FILE (alias, address, description). Does not touch the bus."""
        entries = resources.slave_map.entries()
        meta = {"source": "slave_map", "path": str(resources.slave_map.path) if resources.slave_map.path else None}
        if resources.slave_map.error:
            return err(resources.slave_map.error, meta)
        return ok({"slaves": entries, "count": len(entries)}, meta)

    @server.tool(annotations=_READ)
    async def read_slave_by_alias(alias: Alias, length: ReadLength = None) -> Envelope:
        """read_inputs for the slave behind an alias from SLAVE_MAP_FILE. Read-only."""
        address, problem = resolve_alias(alias)
        if problem:
            return problem
        return await do_read(address, length, {"alias": alias})

    @server.tool(annotations=_WRITE)
    async def write_slave_by_alias(alias: Alias, data: OutputData) -> Envelope:
        """write_outputs for the slave behind an alias. This changes physical outputs on the equipment.

        Refused unless PROFIBUS_WRITES_ENABLED=true (default false); nothing is sent when refused.
        """
        if not config.writes_enabled:
            return err(
                "Writes are disabled: set PROFIBUS_WRITES_ENABLED=true to allow them. Nothing was sent.",
                {"alias": alias, "backend": backend, "writes_enabled": False, **NOT_SENT},
            )
        address, problem = resolve_alias(alias, write=True)
        if problem:
            return problem
        return await do_write(address, data, {"alias": alias})

    @server.tool(annotations=_LOCAL)
    async def load_gsd_file(filepath: Annotated[str, Field(min_length=1, description="Path or file name")]) -> Envelope:
        """Parse a PROFIBUS DP GSD file (ASCII keyword format): vendor, model, ident number, baud rates, modules.

        Each module's I/O lengths are decoded from its configuration bytes. A bare file
        name is also looked up in PROFIBUS_GSD_PATH. Does not touch the bus.
        """
        meta: dict[str, Any] = {"source": "gsd_file", "filepath": filepath}
        try:
            data, cached = resources.gsd_parser.load_with_status(filepath)
        except GSDParserError as exc:
            return err(str(exc), meta)
        except Exception as exc:
            return failure(exc, meta)
        return ok(data, {**meta, "cached": cached})

    @server.tool(annotations=_LOCAL)
    async def ping() -> Envelope:
        """Report server version, backend, write gate, alias count and any SLAVE_MAP_FILE error.

        Does not contact the bus.
        """
        data = {
            "server": "PROFIBUS MCP Server",
            "version": __version__,
            "backend": backend,
            "bus_communication": master.backend.status().get("bus_communication"),
            "writes_enabled": config.writes_enabled,
            "slave_aliases": resources.slave_map.count(),
            # A broken SLAVE_MAP_FILE would otherwise only show up in list_slaves.
            "slave_map_error": resources.slave_map.error,
        }
        return ok(data, {"backend": backend})

    @server.tool(annotations=_LOCAL)
    async def get_master_status() -> Envelope:
        """Report the master configuration: backend, port or mock endpoint, baud rate, address, timeouts, write gate.

        Does not contact the bus.
        """
        return ok(master.status(), {"backend": backend})
