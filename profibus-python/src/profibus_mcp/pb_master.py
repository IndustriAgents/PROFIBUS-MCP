"""PROFIBUS DP master facade and the backends behind it.

Two backends exist, selected by ``PROFIBUS_BACKEND``:

``serial``
    A DP master on an RS-485 interface. This server has no DP master stack
    (FDL token handling, parameterisation, Chk_Cfg, Data_Exchange), so every
    bus operation fails with :class:`BackendNotImplementedError`. The port is
    never opened: an RS-485 adapter opened without a stack in control can
    switch its line driver on and disturb a running bus.

``mock``
    The TCP JSON-lines protocol of ``profibus-mock-slave``. One request per
    connection; the reply is real data from the mock, never invented here.

Every result and every error carries ``meta`` naming the backend that answered.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from typing import Any, Protocol

import anyio
import serial
from anyio.streams.buffered import BufferedByteReceiveStream

from .config import MAX_STATION_ADDRESS, ConfigError, ProfibusConfig

log = logging.getLogger(__name__)

#: DP-V0 limit for the input or output data of one slave, in bytes.
MAX_DP_DATA_LEN = 244

#: Upper bound for one reply line from the mock.
MAX_REPLY_BYTES = 1024 * 1024

Meta = dict[str, Any]


class ProfibusMasterError(RuntimeError):
    """A bus operation failed. ``meta`` says which backend and operation."""

    def __init__(self, message: str, meta: Meta | None = None) -> None:
        super().__init__(message)
        self.meta: Meta = dict(meta or {})


class InvalidArgumentError(ProfibusMasterError):
    """An argument is out of range; nothing was sent."""


class WritesDisabledError(ProfibusMasterError):
    """Writes are disabled by configuration; nothing was sent."""


class BackendNotImplementedError(ProfibusMasterError):
    """The configured backend cannot perform bus operations."""


class Backend(Protocol):
    name: str

    def status(self) -> dict[str, Any]: ...

    async def request(self, op: str, params: dict[str, Any], *, retry_after_send: bool) -> tuple[Any, Meta]: ...


# ---------------------------------------------------------------------------
# Argument validation (shared by the tools and the master)
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_slave_address(value: Any) -> int:
    if not _is_int(value) or not 0 <= value <= MAX_STATION_ADDRESS:
        raise InvalidArgumentError(
            f"slave_address must be an integer between 0 and {MAX_STATION_ADDRESS}, got {value!r}"
        )
    return value


def validate_length(value: Any) -> int | None:
    if value is None:
        return None
    if not _is_int(value) or not 1 <= value <= MAX_DP_DATA_LEN:
        raise InvalidArgumentError(f"length must be an integer between 1 and {MAX_DP_DATA_LEN}, got {value!r}")
    return value


def validate_output_data(values: Any) -> bytes:
    if isinstance(values, bytes | bytearray):
        values = list(values)
    if not isinstance(values, Sequence) or isinstance(values, str):
        raise InvalidArgumentError("data must be a list of byte values (0-255)")
    if not 1 <= len(values) <= MAX_DP_DATA_LEN:
        raise InvalidArgumentError(f"data must hold between 1 and {MAX_DP_DATA_LEN} bytes, got {len(values)}")
    for index, item in enumerate(values):
        if not _is_int(item) or not 0 <= item <= 255:
            raise InvalidArgumentError(f"data[{index}] must be an integer between 0 and 255, got {item!r}")
    return bytes(values)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class SerialBackend:
    """Placeholder for a DP master on an RS-485 adapter: never opens the port."""

    name = "serial"

    def __init__(self, config: ProfibusConfig) -> None:
        self.config = config
        # serial_for_url accepts device paths as well as socket://, rfc2217://
        # and loop:// URLs. do_not_open=True only checks the port spec and the
        # character format (11-bit, even parity) without touching the device.
        try:
            serial.serial_for_url(
                config.port,
                do_not_open=True,
                baudrate=config.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_EVEN,
                stopbits=serial.STOPBITS_ONE,
                timeout=config.timeout_s,
            )
        except (ValueError, serial.SerialException) as exc:
            raise ConfigError(f"PROFIBUS_PORT {config.port!r} is not a usable port or URL: {exc}") from exc

    def status(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "port": self.config.port,
            "baudrate": self.config.baudrate,
            "port_opened": False,
            "bus_communication": "not implemented",
        }

    async def request(self, op: str, params: dict[str, Any], *, retry_after_send: bool) -> tuple[Any, Meta]:  # noqa: ARG002
        meta: Meta = {"backend": self.name, "port": self.config.port, "implemented": False}
        raise BackendNotImplementedError(
            f"'{op}' is not implemented for PROFIBUS_BACKEND=serial: this server has no PROFIBUS DP master "
            f"stack yet, so nothing was sent and {self.config.port} was not opened. "
            "Set PROFIBUS_BACKEND=mock to use profibus-mock-slave.",
            meta,
        )


class _TransportError(Exception):
    def __init__(self, message: str, *, sent: bool) -> None:
        super().__init__(message)
        self.sent = sent


class MockBackend:
    """Client for the JSON-lines protocol of ``profibus-mock-slave``."""

    name = "mock"

    def __init__(self, config: ProfibusConfig) -> None:
        self.config = config
        self.host = config.mock_host
        self.port = config.mock_port

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def status(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "endpoint": self.endpoint,
            "bus_communication": "profibus-mock-slave over TCP (JSON lines); no PROFIBUS telegrams",
        }

    async def request(self, op: str, params: dict[str, Any], *, retry_after_send: bool) -> tuple[Any, Meta]:
        """Send one request and return the reply's ``data``.

        Transport failures are retried ``max_retries`` times with exponential
        backoff. When ``retry_after_send`` is false (writes), a request is only
        retried if the connection could not be made, so it is never sent twice.
        """
        payload = json.dumps({"op": op, **params}).encode("utf-8") + b"\n"
        meta: Meta = {"backend": self.name, "endpoint": self.endpoint, "attempts": 0}
        attempt = 0
        while True:
            attempt += 1
            meta["attempts"] = attempt
            try:
                raw = await self._exchange(payload)
                break
            except _TransportError as exc:
                retryable = retry_after_send or not exc.sent
                if attempt > self.config.max_retries or not retryable:
                    raise ProfibusMasterError(str(exc), meta) from exc
                delay = self.config.retry_backoff_base * (2 ** (attempt - 1))
                log.debug("mock request %s failed (%s); retrying in %.3fs", op, exc, delay)
                await anyio.sleep(delay)

        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProfibusMasterError(
                f"The mock slave at {self.endpoint} sent a reply that is not JSON", meta
            ) from None
        if not isinstance(reply, dict) or not isinstance(reply.get("success"), bool):
            raise ProfibusMasterError(f"The mock slave at {self.endpoint} sent an unexpected reply", meta)
        if not reply["success"]:
            raise ProfibusMasterError(f"Mock slave: {reply.get('error') or 'request failed'}", meta)
        return reply.get("data"), meta

    async def _exchange(self, payload: bytes) -> bytes:
        timeout = self.config.timeout_s
        try:
            with anyio.fail_after(timeout):
                stream = await anyio.connect_tcp(self.host, self.port)
        except TimeoutError:
            raise _TransportError(
                f"Timed out after {self.config.timeout_ms} ms connecting to the mock slave at {self.endpoint}",
                sent=False,
            ) from None
        except OSError as exc:
            raise _TransportError(
                f"Cannot reach the PROFIBUS mock slave at {self.endpoint} ({exc}). Start it with "
                "`uv run profibus-mock-slave` or check PROFIBUS_MOCK_HOST / PROFIBUS_MOCK_PORT.",
                sent=False,
            ) from None

        sent = False
        async with stream:
            try:
                with anyio.fail_after(timeout):
                    sent = True
                    await stream.send(payload)
                    return await BufferedByteReceiveStream(stream).receive_until(b"\n", MAX_REPLY_BYTES)
            except TimeoutError:
                raise _TransportError(
                    f"The mock slave at {self.endpoint} did not answer within {self.config.timeout_ms} ms", sent=sent
                ) from None
            except (anyio.EndOfStream, anyio.IncompleteRead, anyio.BrokenResourceError, OSError) as exc:
                reason = str(exc) or type(exc).__name__
                raise _TransportError(
                    f"The connection to the mock slave at {self.endpoint} failed: {reason}", sent=sent
                ) from None
            except anyio.DelimiterNotFound:
                raise _TransportError(
                    f"The mock slave at {self.endpoint} sent a reply longer than {MAX_REPLY_BYTES} bytes", sent=sent
                ) from None


def create_backend(config: ProfibusConfig) -> Backend:
    if config.backend == "mock":
        return MockBackend(config)
    return SerialBackend(config)


# ---------------------------------------------------------------------------
# Master facade
# ---------------------------------------------------------------------------


class ProfibusMaster:
    """Validates arguments, enforces the write gate and checks backend replies."""

    def __init__(self, config: ProfibusConfig, backend: Backend | None = None) -> None:
        self.config = config
        self.backend = backend or create_backend(config)

    async def scan_bus(self) -> tuple[list[dict[str, Any]], Meta]:
        data, meta = await self._call("scan_bus", "scan", {}, retry_after_send=True)
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise ProfibusMasterError("The backend sent a malformed scan reply", meta)
        slaves: list[dict[str, Any]] = []
        for item in data:
            if not _is_int(item.get("address")):
                raise ProfibusMasterError("The backend sent a scan entry without a valid address", meta)
            slaves.append(dict(item))
        meta["count"] = len(slaves)
        return slaves, meta

    async def read_inputs(self, address: int, length: int | None = None) -> tuple[bytes, Meta]:
        address = validate_slave_address(address)
        length = validate_length(length)
        params: dict[str, Any] = {"address": address}
        if length is not None:
            params["length"] = length
        data, meta = await self._call("read_inputs", "read_inputs", params, retry_after_send=True)
        meta["slave_address"] = address
        raw_hex = data.get("raw_data_hex") if isinstance(data, dict) else None
        try:
            payload = bytes.fromhex(raw_hex) if isinstance(raw_hex, str) else None
        except ValueError:
            payload = None
        if payload is None:
            raise ProfibusMasterError("The backend sent a malformed read_inputs reply", meta)
        if length is not None and len(payload) != length:
            raise ProfibusMasterError(
                f"Slave {address} returned {len(payload)} input bytes, {length} were requested", meta
            )
        return payload, meta

    async def write_outputs(self, address: int, data: Sequence[int] | bytes) -> tuple[int, Meta]:
        address = validate_slave_address(address)
        payload = validate_output_data(data)
        if not self.config.writes_enabled:
            raise WritesDisabledError(
                "Writes are disabled: set PROFIBUS_WRITES_ENABLED=true to allow them. Nothing was sent.",
                {"backend": self.backend.name, "writes_enabled": False, "slave_address": address},
            )
        reply, meta = await self._call(
            "write_outputs", "write_outputs", {"address": address, "data": list(payload)}, retry_after_send=False
        )
        meta["slave_address"] = address
        written = reply.get("written_bytes") if isinstance(reply, dict) else None
        if not _is_int(written):
            raise ProfibusMasterError("The backend sent a malformed write_outputs reply", meta)
        if written != len(payload):
            raise ProfibusMasterError(f"Slave {address} accepted {written} of {len(payload)} output bytes", meta)
        return written, meta

    async def read_diagnosis(self, address: int) -> tuple[dict[str, Any], Meta]:
        address = validate_slave_address(address)
        data, meta = await self._call("read_diagnosis", "diagnosis", {"address": address}, retry_after_send=True)
        meta["slave_address"] = address
        if not isinstance(data, dict) or not isinstance(data.get("status"), str):
            raise ProfibusMasterError("The backend sent a malformed diagnosis reply", meta)
        diag = {key: value for key, value in data.items() if key != "address"}
        return {"slave_address": address, **diag}, meta

    def status(self) -> dict[str, Any]:
        return {
            **self.backend.status(),
            "master_address": self.config.master_address,
            "timeout_ms": self.config.timeout_ms,
            "max_retries": self.config.max_retries,
            "retry_backoff_base": self.config.retry_backoff_base,
            "writes_enabled": self.config.writes_enabled,
        }

    async def _call(
        self, operation: str, op: str, params: dict[str, Any], *, retry_after_send: bool
    ) -> tuple[Any, Meta]:
        start = time.perf_counter()
        try:
            data, meta = await self.backend.request(op, params, retry_after_send=retry_after_send)
        except ProfibusMasterError as exc:
            exc.meta.setdefault("backend", self.backend.name)
            exc.meta.update(operation=operation, duration_ms=_elapsed_ms(start))
            raise
        meta = {**meta, "operation": operation, "duration_ms": _elapsed_ms(start)}
        return data, meta


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)
