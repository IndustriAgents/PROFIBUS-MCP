from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import Field, Strict

from profibus_mcp.config import MAX_STATION_ADDRESS
from profibus_mcp.domain.errors import MAX_DP_DATA_LEN, ProfibusMasterError, validate_slave_address

log = logging.getLogger(__name__)
Envelope = dict[str, Any]


def envelope(success: bool, data: Any = None, error: str | None = None, meta: dict[str, Any] | None = None) -> Envelope:
    return {"success": success, "data": data, "error": error, "meta": meta or {}}


def ok(data: Any = None, meta: dict[str, Any] | None = None) -> Envelope:
    return envelope(True, data=data, meta=meta)


def err(message: str, meta: dict[str, Any] | None = None) -> Envelope:
    return envelope(False, error=message, meta=meta)


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
WRITE_TOOLS = frozenset({"write_outputs", "write_slave_by_alias"})
NOT_SENT: dict[str, Any] = {"outcome": "not_sent", "request_sent": False}


def alias_problem(spec: Any) -> str | None:
    if not isinstance(spec, dict):
        return "entry must be an object with an 'address'"
    try:
        validate_slave_address(spec.get("address"))
    except ProfibusMasterError as exc:
        return str(exc).replace("slave_address", "address", 1)
    return None
