# SPDX-License-Identifier: GPL-2.0-or-later
"""Optional process component for pyprofibus 1.13; no hardware access on import.

This file is licensed separately from the MIT MCP host. It uses the upstream
public API, and neither embeds nor installs pyprofibus. stdin/stdout is a private,
bounded protocol. Debug output is redirected to stderr. The DP master and its
process image have exactly one owner: the main worker thread.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import sys
import threading
import time

MAX_LINE = 16384


def emit(reply):
    sys.stdout.write(json.dumps(reply, allow_nan=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def receive():
    line = sys.stdin.buffer.readline(MAX_LINE + 1)
    if not line or len(line) > MAX_LINE or not line.endswith(b"\n"):
        raise ValueError("Closed or oversized worker request")
    value = json.loads(line)
    if not isinstance(value, dict) or type(value.get("id")) is not int:
        raise ValueError("Invalid worker request")
    return value


def prepare(request):
    # Configuration is operator-owned. Validate all images and addresses BEFORE
    # makeDPM(): that method opens the PHY and can configure the physical bus.
    import pyprofibus
    from pyprofibus.version import VERSION_STRING

    if VERSION_STRING != "1.13":
        raise ValueError("This adapter supports pyprofibus 1.13")
    if request.get("bus_enabled") is not True:
        raise ValueError("Bus operation is disabled")
    config_path = os.path.abspath(request["config"])
    with open(request["outputs"], encoding="utf-8") as stream:
        raw = stream.read(MAX_LINE + 1)
    if len(raw) > MAX_LINE:
        raise ValueError("Initial output file exceeds 16384 characters")
    images = json.loads(raw)
    if not isinstance(images, dict):
        raise ValueError("Initial outputs must be an object keyed by station address")
    os.chdir(os.path.dirname(config_path))
    config = pyprofibus.PbConf.fromFile(config_path)
    if config.phyType != request["phy"]:
        raise ValueError("Configured PHY does not match PROFIBUS_PYPROFIBUS_PHY")
    if config.dpMasterClass != 1 or config.dpMasterAddr != request["master_address"]:
        raise ValueError("Requires class-1 master with matching PROFIBUS_MASTER_ADDRESS")
    if config.phyBaud != request["baudrate"]:
        raise ValueError("Config baud must match PROFIBUS_BAUDRATE")
    if not 1 <= len(config.slaveConfs) <= 125:
        raise ValueError("Configure 1..125 slaves")
    addresses = set()
    outputs = {}
    descriptors = []
    for slave in config.slaveConfs:
        address = slave.addr
        if not 0 <= address <= 125 or address == config.dpMasterAddr or address in addresses:
            raise ValueError("Duplicate, reserved or master slave address")
        addresses.add(address)
        if not 0 <= slave.outputSize <= 244 or not 1 <= slave.inputSize <= 244:
            raise ValueError("Requires 1..244 master output bytes and 0..244 master input bytes per slave")
        if slave.watchdogMs <= 0:
            raise ValueError("Each slave must have a finite enabled watchdog")
        value = images.get(str(address))
        if not isinstance(value, list) or len(value) != slave.inputSize:
            raise ValueError("Each slave needs an exact initial master output image")
        if any(type(item) is not int or not 0 <= item <= 255 for item in value):
            raise ValueError("Initial output image must contain byte integers")
        outputs[address] = bytearray(value)
        descriptors.append(slave.makeDpSlaveDesc())
    if set(images) != {str(address) for address in addresses}:
        raise ValueError("Initial outputs contain unconfigured stations")
    master = config.makeDPM()
    try:
        for slave in descriptors:
            master.addSlave(slave)
        master.initialize()
    except BaseException:
        master.destroy()
        raise
    return master, {slave.slaveAddr: slave for slave in descriptors}, outputs


def run(master, slaves, outputs):
    requests = queue.Queue(maxsize=8)

    def reader():
        try:
            while True:
                requests.put(receive())
        except (ValueError, OSError):
            requests.put(None)

    threading.Thread(target=reader, daemon=True).start()
    connected = set()
    pending = None
    while True:
        try:
            request = requests.get_nowait()
        except queue.Empty:
            request = {}
        if request is None:
            return
        if request:
            ident = request["id"]
            try:
                if pending is not None:
                    raise ValueError("A request is already pending")
                params = request["params"]
                address = params["address"]
                if type(address) is not int or address not in slaves:
                    raise ValueError("Station is not in the configured master")
                slave = slaves[address]
                op = request["op"]
                if op == "read_inputs":
                    length = params.get("length", slave.outputSize)
                    if type(length) is not int or not 1 <= length <= slave.outputSize:
                        raise ValueError("Read length exceeds configured master input image")
                    pending = (ident, address, length, time.monotonic() + request["timeout_s"])
                elif op == "write_outputs":
                    value = params["data"]
                    if not isinstance(value, list) or len(value) != slave.inputSize:
                        raise ValueError("Write must replace the complete configured master output image")
                    if any(type(item) is not int or not 0 <= item <= 255 for item in value):
                        raise ValueError("Output image must contain byte integers")
                    if not slave.isConnected():
                        raise ValueError("Slave is not in Data_Exchange")
                    outputs[address] = bytearray(value)
                    # This confirms only a process-image update, before a cycle.
                    emit({"id": ident, "ok": True, "data": {"address": address, "written_bytes": len(value)}})
                else:
                    raise ValueError("Unsupported operation")
            except (ValueError, KeyError, TypeError) as exc:
                emit({"id": ident, "ok": False, "error": str(exc)})
        for slave in slaves.values():
            if slave.slaveAddr in connected and not slave.isConnected():
                # Do not reconnect/replay the held output image after link loss.
                raise RuntimeError("Data_Exchange lost; restart and recommission required")
            slave.setMasterOutData(outputs[slave.slaveAddr])
        handled = master.run()
        if handled is not None:
            address = handled.slaveAddr
            if handled.isConnected():
                connected.add(address)
            sample = handled.getMasterInData()
            if pending is not None and pending[1] == address and sample is not None:
                ident, _, length, _ = pending
                emit(
                    {
                        "id": ident,
                        "ok": True,
                        "data": {"address": address, "raw_data_hex": bytes(sample[:length]).hex()},
                    }
                )
                pending = None
        if pending is not None and time.monotonic() >= pending[3]:
            emit({"id": pending[0], "ok": False, "error": "No fresh input sample before deadline"})
            pending = None
        # Deliberately no Python promise of deterministic cycle timing. The
        # serial PHY controls wire pacing; yield so dummy/idle loops do not spin.
        time.sleep(0.0001)


def main():
    global emit
    master = None
    try:
        request = receive()
        if request.get("op") != "start" or request["id"] != 0:
            raise ValueError("First request must start the configured master")
        with contextlib.redirect_stdout(sys.stderr):
            master, slaves, outputs = prepare(request)
        emit({"id": 0, "ok": True, "data": {"configured_stations": sorted(slaves)}})
        # Upstream debug prints are redirected without swallowing protocol output.
        original_stdout = sys.stdout

        def send(reply):
            original_stdout.write(json.dumps(reply, allow_nan=False, separators=(",", ":")) + "\n")
            original_stdout.flush()

        emit = send
        with contextlib.redirect_stdout(sys.stderr):
            run(master, slaves, outputs)
    except Exception as exc:
        emit({"id": 0, "ok": False, "error": str(exc)})
        return 1
    finally:
        if master is not None:
            with contextlib.redirect_stdout(sys.stderr):
                master.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
