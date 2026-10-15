from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ..config import MAX_STATION_ADDRESS

MAX_DP_DATA_LEN = 244
Meta = dict[str, Any]
_REQUEST_CANCEL_SCOPE: ContextVar[Any] = ContextVar("profibus_request_cancel_scope", default=None)


@contextmanager
def request_cancel_scope(scope: Any) -> Iterator[None]:
    """Make the MCP request's cancel scope visible to the pre-send gate for this call."""
    token = _REQUEST_CANCEL_SCOPE.set(scope)
    try:
        yield
    finally:
        _REQUEST_CANCEL_SCOPE.reset(token)


class ProfibusMasterError(RuntimeError):
    """A bus operation failed. ``meta`` says which backend and operation."""

    def __init__(self, message: str, meta: Meta | None = None) -> None:
        super().__init__(message)
        self.meta: Meta = dict(meta or {})


class InvalidArgumentError(ProfibusMasterError):
    """An argument is out of range; nothing was sent."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and (not isinstance(value, bool))


def validate_slave_address(value: Any) -> int:
    if not _is_int(value) or not 0 <= value <= MAX_STATION_ADDRESS:
        raise InvalidArgumentError(
            f"slave_address must be an integer between 0 and {MAX_STATION_ADDRESS}, got {value!r}"
        )
    return value
