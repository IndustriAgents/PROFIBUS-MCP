"""Static backend support; never a probe or an authorization decision."""

from __future__ import annotations

from typing import Any


def capabilities(backend: str, *, writes_enabled: bool, extra_gate: bool = False) -> dict[str, Any]:
    operations: dict[str, dict[str, Any]] = {}
    for name in (
        "read_inputs",
        "write_outputs",
        "scan_bus",
        "read_diagnosis",
        "list_slaves",
        "load_gsd_file",
        "get_master_status",
    ):
        supported = backend in ("mock",) or name in ("list_slaves", "load_gsd_file", "get_master_status")
        reason = None
        if not supported and reason is None:
            reason = "No supported native backend for this operation"
        enabled = supported
        if name in ("write_outputs",):
            enabled = supported and writes_enabled
        if supported and not enabled:
            reason = "Disabled by native configuration gates"
        operations[name] = {"supported": supported, "enabled": enabled}
        if reason is not None:
            operations[name]["reason"] = reason
    return {
        "protocol": "profibus",
        "backend": backend,
        "simulated": backend == "mock",
        "scope": "backend_support",
        "device_verified": False,
        "operations": operations,
        "prerequisites": [
            "Native configuration gates do not grant MCP policy or device permission.",
            "The serial backend has no DP master stack and never opens the port.",
            "Live DP requires a selected master driver, compatible hardware and configured slave data.",
        ],
    }
