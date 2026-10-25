"""Static backend support; never a probe or an authorization decision."""

from __future__ import annotations

from typing import Any


def capabilities(
    backend: str, *, writes_enabled: bool, extra_gate: bool = False, phy: str = "serial"
) -> dict[str, Any]:
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
        supported = (
            backend == "mock"
            or name in ("list_slaves", "load_gsd_file", "get_master_status")
            or (backend == "pyprofibus" and name in ("read_inputs", "write_outputs"))
        )
        reason = None
        if not supported and reason is None:
            reason = "No supported native backend for this operation"
        enabled = supported
        if backend == "pyprofibus" and name in ("read_inputs", "write_outputs"):
            enabled = supported and extra_gate
        if name in ("write_outputs",):
            enabled = enabled and writes_enabled
        if supported and not enabled:
            reason = "Disabled by native configuration gates"
        operations[name] = {"supported": supported, "enabled": enabled}
        if reason is not None:
            operations[name]["reason"] = reason
    return {
        "protocol": "profibus",
        "backend": backend,
        "simulated": backend == "mock" or (backend == "pyprofibus" and phy == "dummy_slave"),
        "scope": "backend_support",
        "device_verified": False,
        "operations": operations,
        "prerequisites": [
            "Native configuration gates do not grant MCP policy or device permission.",
            "The serial backend has no DP master stack and never opens the port.",
            "pyprofibus requires its separately installed 1.13 runtime, POSIX, GSDs and explicit initial outputs.",
            "PROFIBUS_BUS_ENABLED authorizes cyclic startup/parameterization; writes gate controls image changes only.",
            "pyprofibus supports full configured output-image updates and fresh cyclic inputs, not scan/raw diagnosis.",
            "The upstream dummy_slave PHY is simulation; serial/FPGA timing and commissioning require hardware tests.",
        ],
    }
