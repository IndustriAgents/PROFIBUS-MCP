"""PROFIBUS DP MCP server package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("profibus-mcp")
except PackageNotFoundError:  # pragma: no cover - running from a bare source tree
    __version__ = "0.0.0+unknown"

from .server import ProfibusMCPServer  # noqa: E402

__all__ = ["ProfibusMCPServer", "__version__"]
