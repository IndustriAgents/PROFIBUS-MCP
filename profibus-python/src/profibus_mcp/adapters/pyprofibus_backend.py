"""Bounded, single-owner pipe adapter to the optional GPL pyprofibus worker."""

from __future__ import annotations

import json
import math
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import anyio

from ..config import ProfibusConfig
from .pb_master import BackendNotImplementedError, ProfibusMasterError, RequestRejectedError, request_cancel_requested

MAX_LINE = 16384


def validate_diagnostic(data):
    """Check the actual DP diagnostic representation at the worker boundary."""
    raw = data.get("raw_data_hex")
    if not isinstance(raw, str) or not 12 <= len(raw) <= 488:
        raise ValueError("Missing or oversized worker diagnostic payload")
    payload = bytes.fromhex(raw)
    if payload.hex() != raw or len(payload) < 6:
        raise ValueError("Malformed worker diagnostic hex")
    if data.get("diagnostic_format") != "dp_slave_diag" or not isinstance(data.get("status"), str):
        raise ValueError("Malformed worker diagnostic format")
    states = data.get("station_status")
    if not isinstance(states, list) or len(states) != 3 or any(type(item) is not int for item in states):
        raise ValueError("Malformed worker diagnostic status bytes")
    if (
        states != list(payload[:3])
        or type(data.get("master_address")) is not int
        or data["master_address"] != payload[3]
    ):
        raise ValueError("Inconsistent worker diagnostic bytes")
    if data.get("ident_number") != f"0x{int.from_bytes(payload[4:6], 'big'):04X}":
        raise ValueError("Inconsistent worker diagnostic identity")
    expected_ident = data.get("expected_ident_number")
    if (
        not isinstance(expected_ident, str)
        or len(expected_ident) != 6
        or expected_ident != f"0x{int(expected_ident, 16):04X}"
        or type(data.get("identity_match")) is not bool
        or data["identity_match"] != (data["ident_number"] == expected_ident)
    ):
        raise ValueError("Inconsistent configured diagnostic identity")
    expected_master = data.get("expected_master_address")
    if (
        type(expected_master) is not int
        or not 0 <= expected_master <= 125
        or type(data.get("master_match")) is not bool
        or data["master_match"] != (payload[3] == expected_master)
    ):
        raise ValueError("Inconsistent configured diagnostic master")
    if (
        data.get("extended_diagnostics_hex") != payload[6:].hex()
        or data.get("extended_diagnostics_decoded") is not False
    ):
        raise ValueError("Inconsistent worker extended diagnostics")
    if type(data.get("diagnostics_overflow")) is not bool or data["diagnostics_overflow"] != bool(payload[2] & 0x80):
        raise ValueError("Inconsistent worker diagnostic completeness")
    age = data.get("received_age_ms")
    if type(age) not in (int, float) or not math.isfinite(age) or age < 0:
        raise ValueError("Invalid diagnostic reception age")
    if type(data.get("sample_sequence")) is not int or data["sample_sequence"] < 1:
        raise ValueError("Invalid diagnostic sequence")


class PyprofibusBackend:
    name = "pyprofibus"

    def __init__(self, config: ProfibusConfig):
        self.config = config
        self._lease = threading.Lock()
        self._state = threading.Lock()
        self._closed = threading.Event()
        self._fault: str | None = None
        self._process: subprocess.Popen | None = None
        self._sequence = 0
        self._stations: list[int] = []
        self._startup_dispatched = False

    def status(self) -> dict[str, Any]:
        with self._state:
            process = self._process
            return {
                "backend": self.name,
                "worker_running": process is not None and process.poll() is None,
                "bus_enabled": self.config.bus_enabled,
                "phy": self.config.pyprofibus_phy,
                "simulated": self.config.pyprofibus_phy == "dummy_slave",
                "configured_stations": list(self._stations),
                "fault": self._fault
                or ("Worker exited; restart required" if process is not None and process.poll() is not None else None),
                "device_verified": False,
            }

    def shutdown(self) -> None:
        self._closed.set()
        self._stop("Server shutdown")

    def _stop(self, reason: str) -> None:
        with self._state:
            self._fault = reason
            process = self._process
            self._process = None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    process.kill()
            process.wait(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()

    def _check(self, cancelled: threading.Event) -> None:
        if self._closed.is_set() or cancelled.is_set() or request_cancel_requested():
            raise OSError("Worker request cancelled or server closed")
        if self._fault:
            raise OSError(f"Worker fault is latched; restart required: {self._fault}")

    def _exchange(self, process, request, cancelled, deadline):
        self._check(cancelled)
        wire = (json.dumps(request, allow_nan=False, separators=(",", ":")) + "\n").encode()
        if len(wire) > MAX_LINE:
            raise ValueError("Oversized worker request")
        process.stdin.write(wire)
        process.stdin.flush()
        data = bytearray()
        while True:
            self._check(cancelled)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Worker response deadline exceeded")
            ready, _, _ = select.select([process.stdout], [], [], min(remaining, 0.02))
            if not ready:
                continue
            chunk = os.read(process.stdout.fileno(), MAX_LINE + 1 - len(data))
            if not chunk:
                raise OSError("Worker exited before replying")
            data.extend(chunk)
            if len(data) > MAX_LINE:
                raise ValueError("Oversized worker response")
            if b"\n" in data:
                if not data.endswith(b"\n") or data.count(b"\n") != 1:
                    raise ValueError("Unexpected worker response frames")
                reply = json.loads(data)
                if not isinstance(reply, dict) or type(reply.get("id")) is not int or reply["id"] != request["id"]:
                    raise ValueError("Uncorrelated worker reply")
                if type(reply.get("ok")) is not bool:
                    raise ValueError("Malformed worker reply")
                return reply

    def _start(self, cancelled, deadline):
        self._check(cancelled)
        if self._process is not None:
            if self._process.poll() is not None:
                raise OSError("Worker exited; automatic restart is disabled")
            return self._process
        script = Path(__file__).with_name("pyprofibus_worker.py")
        process = subprocess.Popen(
            [self.config.pyprofibus_python or sys.executable, "-I", "-u", str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            close_fds=True,
        )
        with self._state:
            self._process = process
        self._startup_dispatched = True
        reply = self._exchange(
            process,
            {
                "id": 0,
                "op": "start",
                "bus_enabled": self.config.bus_enabled,
                "config": str(self.config.pyprofibus_config.resolve()),
                "outputs": str(self.config.pyprofibus_outputs.resolve()),
                "phy": self.config.pyprofibus_phy,
                "master_address": self.config.master_address,
                "baudrate": self.config.baudrate,
            },
            cancelled,
            deadline,
        )
        if reply["ok"] is not True:
            raise ValueError(f"Worker initialization refused: {reply.get('error')}")
        stations = reply.get("data", {}).get("configured_stations")
        if not isinstance(stations, list) or not stations or len(stations) > 125:
            raise ValueError("Malformed configured-station inventory")
        if any(type(item) is not int or not 0 <= item <= 125 for item in stations):
            raise ValueError("Malformed station address")
        with self._state:
            self._stations = stations
        return process

    async def request(self, op, params, *, retry_after_send):
        meta = {"backend": self.name, "request_sent": False, "simulated": self.config.pyprofibus_phy == "dummy_slave"}
        if op not in ("read_inputs", "write_outputs", "diagnosis"):
            raise BackendNotImplementedError(
                "pyprofibus exposes configured cyclic I/O and diagnostics, not broad bus scanning", meta
            )
        if not self.config.bus_enabled:
            raise ProfibusMasterError("PROFIBUS_BUS_ENABLED is false; worker was not started", meta)
        if os.name != "posix":
            raise BackendNotImplementedError("The isolated pyprofibus adapter requires POSIX pipes", meta)
        if request_cancel_requested() or self._closed.is_set():
            raise ProfibusMasterError("Request cancelled or server closed before dispatch", meta)
        if not self._lease.acquire(blocking=False):
            raise ProfibusMasterError("Native master request is still running", meta)
        cancelled = threading.Event()
        ownership = threading.Lock()
        started = False
        released = False

        def execute():
            nonlocal started, released
            with ownership:
                if released:
                    raise ProfibusMasterError("Request cancelled before worker dispatch", meta)
                started = True
            try:
                deadline = time.monotonic() + self.config.timeout_s
                process = self._start(cancelled, deadline)
                self._check(cancelled)
                self._sequence += 1
                # Set before writing: a partial pipe write has an unknown outcome.
                meta["request_sent"] = True
                reply = self._exchange(
                    process,
                    {
                        "id": self._sequence,
                        "op": op,
                        "params": params,
                        "timeout_s": self.config.timeout_s * 0.9,
                    },
                    cancelled,
                    deadline,
                )
                self._check(cancelled)
                if not reply["ok"]:
                    if not isinstance(reply.get("error"), str):
                        raise ValueError("Malformed worker rejection")
                    raise RequestRejectedError(reply["error"], dict(meta))
                data = reply.get("data")
                if not isinstance(data, dict) or type(data.get("address")) is not int:
                    raise ValueError("Malformed worker data")
                if data["address"] != params["address"]:
                    raise ValueError("Worker replied for another station")
                if op == "write_outputs":
                    if type(data.get("written_bytes")) is not int or data["written_bytes"] != len(params["data"]):
                        raise ValueError("Incomplete process-image acknowledgement")
                    meta.update(confirmation="process_image_accepted", physical_completion_verified=False)
                elif op == "diagnosis":
                    validate_diagnostic(data)
                    meta.update(
                        source="observed_slave_diag",
                        fresh_sample=True,
                        correlation="station_and_service_after_native_submission",
                        acquisition_timestamp_available=False,
                        complete=not data.get("diagnostics_overflow", True),
                    )
                else:
                    meta.update(source="cyclic_process_image", fresh_sample=True)
                return reply.get("data"), dict(meta)
            except RequestRejectedError:
                raise
            except (OSError, ValueError, TypeError, TimeoutError) as exc:
                meta["bus_startup_may_have_occurred"] = self._startup_dispatched
                self._stop(str(exc))
                raise ProfibusMasterError(str(exc), dict(meta)) from exc
            finally:
                with ownership:
                    released = True
                    self._lease.release()

        try:
            return await anyio.to_thread.run_sync(execute, abandon_on_cancel=True)
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            self._stop("MCP request cancelled; restart required")
            raise
        finally:
            cancelled.set()
            with ownership:
                if not started and not released:
                    released = True
                    self._lease.release()
