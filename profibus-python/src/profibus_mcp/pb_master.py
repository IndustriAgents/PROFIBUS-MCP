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
Errors also say whether the request may have reached the slave
(``meta.request_sent``), and write results carry ``meta.outcome``:
``written``, ``rejected``, ``not_sent`` or ``unknown``. A write is never sent
twice: only a connection that could not be made is retried.
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

#: pyserial URL schemes accepted for PROFIBUS_PORT besides plain device paths.
ALLOWED_PORT_SCHEMES = ("socket", "rfc2217", "loop")

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


class RequestRejectedError(ProfibusMasterError):
    """The slave (or mock) answered and refused the request."""


class OutcomeUnknownError(ProfibusMasterError):
    """A write was sent but no valid reply confirmed it; it may have been applied."""


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
        # Some pyserial URL handlers touch devices even with do_not_open=True
        # (hwgrep:// test-opens every matching port), so only plain device
        # paths and the URL schemes below are accepted.
        if "://" in config.port:
            scheme = config.port.split("://", 1)[0].lower()
            if scheme not in ALLOWED_PORT_SCHEMES:
                allowed = ", ".join(f"{name}://" for name in ALLOWED_PORT_SCHEMES)
                raise ConfigError(
                    f"PROFIBUS_PORT scheme {scheme}:// is not allowed; use a device path or one of {allowed}"
                )
        # serial_for_url understands both forms. do_not_open=True only checks
        # the port spec and the character format (11-bit, even parity).
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

    async def request(self, op: str, params: dict[str, Any], *, retry_after_send: bool) -> tuple[Any, Meta]:
        meta: Meta = {"backend": self.name, "port": self.config.port, "implemented": False, "request_sent": False}
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


class _SendState:
    """Whether a request may have left during the current attempt, or any attempt."""

    def __init__(self) -> None:
        self.sent = False
        self.ever_sent = False

    def mark_sent(self) -> None:
        self.sent = self.ever_sent = True


#: Extra seconds on top of the per-attempt timeouts and backoff delays.
DEADLINE_SLACK_S = 1.0


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

    @property
    def deadline_s(self) -> float:
        """Overall bound for one request: every attempt, every backoff delay, plus slack."""
        retries = self.config.max_retries
        backoff = self.config.retry_backoff_base * (2**retries - 1)
        return self.config.timeout_s * (retries + 1) + backoff + DEADLINE_SLACK_S

    def status(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "endpoint": self.endpoint,
            "deadline_s": round(self.deadline_s, 3),
            "bus_communication": "profibus-mock-slave over TCP (JSON lines); no PROFIBUS telegrams",
        }

    async def request(self, op: str, params: dict[str, Any], *, retry_after_send: bool) -> tuple[Any, Meta]:
        """Send one request and return the reply's ``data``.

        Each attempt (connect, send, reply) is bounded by PROFIBUS_TIMEOUT, and
        the whole request, retries included, by :attr:`deadline_s`. Transport
        failures are retried ``max_retries`` times with exponential backoff.
        When ``retry_after_send`` is false (writes), a request is only retried
        if the connection could not be made, so it is never sent twice.

        Errors carry ``meta.request_sent``. Only a reply with ``"applied":
        false`` (a refusal before anything changed) raises
        :class:`RequestRejectedError`.
        """
        payload = json.dumps({"op": op, **params}).encode("utf-8") + b"\n"
        meta: Meta = {"backend": self.name, "endpoint": self.endpoint, "attempts": 0}
        state = _SendState()
        deadline = self.deadline_s
        try:
            with anyio.fail_after(deadline):
                raw = await self._attempts(op, payload, meta, state, retry_after_send)
        except TimeoutError:
            # Something hung beyond every per-attempt bound. Whether the
            # request went out cannot be known, so assume it did.
            meta["request_sent"] = True
            raise ProfibusMasterError(
                f"No complete answer from the mock slave at {self.endpoint} within the overall deadline "
                f"of {deadline:.1f} s",
                meta,
            ) from None

        meta["request_sent"] = True
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProfibusMasterError(
                f"The mock slave at {self.endpoint} sent a reply that is not JSON", meta
            ) from None
        if not isinstance(reply, dict) or not isinstance(reply.get("success"), bool):
            raise ProfibusMasterError(f"The mock slave at {self.endpoint} sent an unexpected reply", meta)
        if not reply["success"]:
            message = f"Mock slave: {reply.get('error') or 'request failed'}"
            if reply.get("applied") is False:
                raise RequestRejectedError(message, meta)
            # No promise that nothing changed (e.g. an internal error).
            raise ProfibusMasterError(message, meta)
        return reply.get("data"), meta

    async def _attempts(self, op: str, payload: bytes, meta: Meta, state: _SendState, retry_after_send: bool) -> bytes:
        attempt = 0
        while True:
            attempt += 1
            meta["attempts"] = attempt
            try:
                return await self._exchange(payload, state)
            except _TransportError as exc:
                retryable = retry_after_send or not exc.sent
                if attempt > self.config.max_retries or not retryable:
                    meta["request_sent"] = state.ever_sent
                    raise ProfibusMasterError(str(exc), meta) from exc
                delay = self.config.retry_backoff_base * (2 ** (attempt - 1))
                log.debug("mock request %s failed (%s); retrying in %.3fs", op, exc, delay)
                await anyio.sleep(delay)

    async def _exchange(self, payload: bytes, state: _SendState) -> bytes:
        """One attempt: connect, send, read one reply line, all within PROFIBUS_TIMEOUT."""
        state.sent = False
        try:
            with anyio.fail_after(self.config.timeout_s):
                try:
                    stream = await anyio.connect_tcp(self.host, self.port)
                except OSError as exc:
                    raise _TransportError(
                        f"Cannot reach the PROFIBUS mock slave at {self.endpoint} ({exc}). Start it with "
                        "`uv run profibus-mock-slave` or check PROFIBUS_MOCK_HOST / PROFIBUS_MOCK_PORT.",
                        sent=False,
                    ) from None
                async with stream:
                    state.mark_sent()  # from here on the request may have left
                    await stream.send(payload)
                    return await BufferedByteReceiveStream(stream).receive_until(b"\n", MAX_REPLY_BYTES)
        except TimeoutError:
            if state.sent:
                message = f"The mock slave at {self.endpoint} did not answer within {self.config.timeout_ms} ms"
            else:
                message = f"Timed out after {self.config.timeout_ms} ms connecting to the mock slave at {self.endpoint}"
            raise _TransportError(message, sent=state.sent) from None
        except (anyio.EndOfStream, anyio.IncompleteRead, anyio.BrokenResourceError, OSError) as exc:
            reason = str(exc) or type(exc).__name__
            raise _TransportError(
                f"The connection to the mock slave at {self.endpoint} failed: {reason}", sent=state.sent
            ) from None
        except anyio.DelimiterNotFound:
            raise _TransportError(
                f"The mock slave at {self.endpoint} sent a reply longer than {MAX_REPLY_BYTES} bytes", sent=state.sent
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
        """Write output bytes once. Errors carry ``meta.outcome`` and ``meta.request_sent``."""
        not_sent: Meta = {"backend": self.backend.name, "outcome": "not_sent", "request_sent": False}
        try:
            address = validate_slave_address(address)
            payload = validate_output_data(data)
        except InvalidArgumentError as exc:
            exc.meta.update(not_sent)
            raise
        if not self.config.writes_enabled:
            raise WritesDisabledError(
                "Writes are disabled: set PROFIBUS_WRITES_ENABLED=true to allow them. Nothing was sent.",
                {**not_sent, "writes_enabled": False, "slave_address": address},
            )
        try:
            reply, meta = await self._call(
                "write_outputs", "write_outputs", {"address": address, "data": list(payload)}, retry_after_send=False
            )
        except RequestRejectedError as exc:
            exc.meta.update(outcome="rejected", slave_address=address)
            raise
        except ProfibusMasterError as exc:
            exc.meta["slave_address"] = address
            if not exc.meta.get("request_sent"):
                exc.meta["outcome"] = "not_sent"
                raise
            raise _outcome_unknown(str(exc), exc.meta) from exc
        meta["slave_address"] = address
        written = reply.get("written_bytes") if isinstance(reply, dict) else None
        if not _is_int(written):
            raise _outcome_unknown("The backend sent a malformed write_outputs reply.", meta)
        if written != len(payload):
            raise _outcome_unknown(f"Slave {address} reported {written} of {len(payload)} output bytes written.", meta)
        meta["outcome"] = "written"
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


def _outcome_unknown(message: str, meta: Meta) -> OutcomeUnknownError:
    meta = {**meta, "outcome": "unknown", "request_sent": True}
    return OutcomeUnknownError(
        f"{message.rstrip('.')}. The write was sent and may have been applied; "
        "check the slave's outputs before retrying.",
        meta,
    )


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)
