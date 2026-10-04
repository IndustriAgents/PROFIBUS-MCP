"""CLI entry point for the PROFIBUS MCP server.

stdout carries the MCP JSON-RPC stream, so everything else goes to stderr.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from . import __version__
from .config import ConfigError, ProfibusConfig
from .server import ProfibusMCPServer

log = logging.getLogger("profibus_mcp")


def is_safety_gate(name: str) -> bool:
    """True for variables that switch on device-changing behaviour (PROFIBUS_*_ENABLED)."""
    return name.startswith("PROFIBUS_") and name.endswith("_ENABLED")


def load_env_file(directory: Path | None = None) -> tuple[Path | None, list[str]]:
    """Load ``.env`` from the working directory, without its safety gates.

    The working directory of a server started by an MCP client is often the
    client's project, which may not be trusted, so an automatically found
    ``.env`` may set connection settings but never a gate such as
    PROFIBUS_WRITES_ENABLED. Gates come only from the real environment or
    from ``--env-file``. Variables already set in the environment win.
    Returns the file loaded (or None) and the gate variables it ignored.
    """
    path = (directory or Path.cwd()) / ".env"
    if not path.is_file():
        return None, []
    ignored: list[str] = []
    for name, value in dotenv_values(path).items():
        if value is None:
            continue
        if is_safety_gate(name):
            ignored.append(name)
            continue
        os.environ.setdefault(name, value)
    return path, ignored


def load_explicit_env_file(path: Path) -> Path:
    """Load a file named with ``--env-file``; it may set every variable, gates included."""
    if not path.is_file():
        raise ConfigError(f"--env-file {path} does not exist or is not a file")
    for name, value in dotenv_values(path).items():
        if value is not None:
            os.environ.setdefault(name, value)
    return path


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="profibus-mcp",
        description="MCP server for PROFIBUS DP over stdio. Settings come from environment variables.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="load settings from this file instead of ./.env; unlike ./.env it may enable writes",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("mcp").setLevel(logging.WARNING)

    ignored: list[str] = []
    try:
        if args.env_file is not None:
            env_file: Path | None = load_explicit_env_file(args.env_file)
        else:
            env_file, ignored = load_env_file()
        config = ProfibusConfig.from_env()
        server = ProfibusMCPServer(config)
    except ConfigError as exc:
        print(f"profibus-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    if env_file:
        log.info("loaded %s", env_file)
    for name in ignored:
        log.warning(
            "%s in %s is ignored: safety switches are only read from the process environment "
            "or a file passed with --env-file",
            name,
            env_file,
        )
    for warning in config.warnings:
        log.warning(warning)
    if config.backend == "mock":
        target = f"mock slave at {config.mock_host}:{config.mock_port}"
    else:
        target = f"serial port {config.port} (bus operations not implemented; the port is not opened)"
    log.info(
        "PROFIBUS MCP server: backend=%s, %s, writes %s",
        config.backend,
        target,
        "ENABLED" if config.writes_enabled else "disabled",
    )
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
