"""Runtime configuration for the PROFIBUS MCP server.

Every setting is read from the environment when :meth:`ProfibusConfig.from_env`
is called, never at import time, so a ``.env`` file loaded by the CLI first
reaches all of them. The master and the tools share one config object, so they
cannot disagree about a flag such as ``writes_enabled``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Backends the server can talk to. ``serial`` is a real DP master interface
#: (not implemented yet); ``mock`` is the TCP JSON-lines protocol spoken by
#: ``profibus-mock-slave``.
BACKENDS = ("serial", "mock")

#: Transmission rates defined for PROFIBUS DP over RS-485, in bit/s.
DP_BAUDRATES = (9600, 19200, 45450, 93750, 187500, 500000, 1500000, 3000000, 6000000, 12000000)

#: Highest station address usable for data exchange (126 is the default
#: address of an unconfigured slave, 127 is broadcast).
MAX_STATION_ADDRESS = 125

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


class ConfigError(ValueError):
    """Raised when a setting cannot be used. The message names the variable."""


@dataclass(frozen=True, slots=True)
class ProfibusConfig:
    """All server settings. Construct with :meth:`from_env` or directly in tests."""

    backend: str = "serial"
    port: str = "/dev/ttyUSB0"
    baudrate: int = 500000
    master_address: int = 2
    timeout_ms: int = 1000
    max_retries: int = 3
    retry_backoff_base: float = 0.1
    writes_enabled: bool = False
    mock_host: str = "127.0.0.1"
    mock_port: int = 7100
    slave_map_path: Path | None = None
    gsd_base_path: Path | None = None
    #: Non-fatal notes about the environment (deprecated or ignored variables).
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.backend not in BACKENDS:
            raise ConfigError(f"PROFIBUS_BACKEND must be one of {', '.join(BACKENDS)}, got {self.backend!r}")
        if not self.port.strip():
            raise ConfigError("PROFIBUS_PORT must not be empty")
        if self.baudrate not in DP_BAUDRATES:
            rates = ", ".join(str(rate) for rate in DP_BAUDRATES)
            raise ConfigError(f"PROFIBUS_BAUDRATE must be a PROFIBUS DP rate ({rates}), got {self.baudrate}")
        if not 0 <= self.master_address <= MAX_STATION_ADDRESS:
            raise ConfigError(
                f"PROFIBUS_MASTER_ADDRESS must be between 0 and {MAX_STATION_ADDRESS}, got {self.master_address}"
            )
        if not 1 <= self.timeout_ms <= 600_000:
            raise ConfigError(f"PROFIBUS_TIMEOUT must be between 1 and 600000 ms, got {self.timeout_ms}")
        if not 0 <= self.max_retries <= 10:
            raise ConfigError(f"PROFIBUS_MAX_RETRIES must be between 0 and 10, got {self.max_retries}")
        if not 0 <= self.retry_backoff_base <= 10:
            raise ConfigError(
                f"PROFIBUS_RETRY_BACKOFF_BASE must be between 0 and 10 seconds, got {self.retry_backoff_base}"
            )
        if not self.mock_host.strip():
            raise ConfigError("PROFIBUS_MOCK_HOST must not be empty")
        if not self.mock_host.isascii():
            try:
                self.mock_host.encode("idna")
            except UnicodeError as exc:
                raise ConfigError(f"PROFIBUS_MOCK_HOST {self.mock_host!r} is not a valid host name: {exc}") from None
        if not 1 <= self.mock_port <= 65535:
            raise ConfigError(f"PROFIBUS_MOCK_PORT must be between 1 and 65535, got {self.mock_port}")

    @property
    def timeout_s(self) -> float:
        return self.timeout_ms / 1000.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProfibusConfig:
        """Build the config from ``environ`` (default: ``os.environ``) at call time."""
        env = os.environ if environ is None else environ
        warnings: list[str] = []

        backend = _get(env, "PROFIBUS_BACKEND")
        if backend is not None:
            backend = backend.lower()

        # Legacy switches from the scaffold, which served invented data
        # in-process. They now select the mock backend, which needs
        # profibus-mock-slave running.
        legacy_mock = _bool(env, "PROFIBUS_MOCK", False)
        port = _get(env, "PROFIBUS_PORT") or "/dev/ttyUSB0"
        legacy_port = port.lower() in {"mock", "none"}
        if legacy_mock or legacy_port:
            source = "PROFIBUS_MOCK=true" if legacy_mock else f"PROFIBUS_PORT={port}"
            if backend not in (None, "mock"):
                raise ConfigError(f"{source} asks for the mock backend but PROFIBUS_BACKEND={backend}")
            backend = "mock"
            warnings.append(
                f"{source} is deprecated; set PROFIBUS_BACKEND=mock instead. The mock backend talks to "
                "profibus-mock-slave over TCP, so start the mock first."
            )
            if legacy_port:
                port = "/dev/ttyUSB0"

        if _get(env, "PROFIBUS_CONFIG_CMDS_ENABLED") is not None:
            warnings.append("PROFIBUS_CONFIG_CMDS_ENABLED is ignored: this server has no configuration commands.")

        slave_map = _get(env, "SLAVE_MAP_FILE")
        gsd_path = _get(env, "PROFIBUS_GSD_PATH")
        return cls(
            backend=backend or "serial",
            port=port,
            baudrate=_int(env, "PROFIBUS_BAUDRATE", 500000),
            master_address=_int(env, "PROFIBUS_MASTER_ADDRESS", 2),
            timeout_ms=_int(env, "PROFIBUS_TIMEOUT", 1000),
            max_retries=_int(env, "PROFIBUS_MAX_RETRIES", 3),
            retry_backoff_base=_float(env, "PROFIBUS_RETRY_BACKOFF_BASE", 0.1),
            writes_enabled=_bool(env, "PROFIBUS_WRITES_ENABLED", False),
            mock_host=_get(env, "PROFIBUS_MOCK_HOST") or "127.0.0.1",
            mock_port=_int(env, "PROFIBUS_MOCK_PORT", 7100),
            slave_map_path=Path(slave_map).expanduser() if slave_map else None,
            gsd_base_path=Path(gsd_path).expanduser() if gsd_path else None,
            warnings=tuple(warnings),
        )


def _get(env: Mapping[str, str], name: str) -> str | None:
    """Return the stripped value, treating unset and empty the same way."""
    value = env.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = _get(env, name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {value!r}")


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    value = _get(env, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from None


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    value = _get(env, name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {value!r}") from None
