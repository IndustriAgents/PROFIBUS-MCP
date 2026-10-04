"""CLI entry point for the PROFIBUS MCP server.

stdout carries the MCP JSON-RPC stream, so everything else goes to stderr.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import ConfigError, ProfibusConfig
from .server import ProfibusMCPServer

log = logging.getLogger("profibus_mcp")


def load_env_file(directory: Path | None = None) -> Path | None:
    """Load ``.env`` from the working directory only.

    Called without a path, python-dotenv searches upward from the calling
    module's directory, which can pick up an unrelated ``.env`` from a parent
    folder. Variables already set in the environment win over the file.
    """
    path = (directory or Path.cwd()) / ".env"
    if not path.is_file():
        return None
    load_dotenv(path, override=False)
    return path


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("mcp").setLevel(logging.WARNING)

    env_file = load_env_file()
    try:
        config = ProfibusConfig.from_env()
        server = ProfibusMCPServer(config)
    except ConfigError as exc:
        print(f"profibus-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    if env_file:
        log.info("loaded %s", env_file)
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
