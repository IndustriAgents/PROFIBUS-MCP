"""Mock PROFIBUS DP slaves behind a TCP JSON-lines protocol.

No PROFIBUS telegrams are involved: each request is one JSON object on one
line, and each reply is one JSON line, ``{"success": true, "data": ...}`` or
``{"success": false, "error": "..."}``. A malformed request gets an error
reply and the connection stays open.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import signal
import sys
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console

console = Console(stderr=True)

#: DP-V0 limit for one slave's input or output data, in bytes.
MAX_DP_DATA_LEN = 244
#: Highest station address usable for data exchange.
MAX_STATION_ADDRESS = 125
#: Longest request line accepted; longer lines close the connection.
MAX_LINE_BYTES = 64 * 1024

_TRUE = {"1", "true", "yes", "y", "on"}


@dataclass
class MockConfig:
    host: str = "127.0.0.1"
    port: int = 7100
    update_interval: float = 1.0
    verbose: bool = False


@dataclass
class MockSlave:
    address: int
    ident_number: str
    input_length: int
    output_length: int
    status: str = "OK"
    input_buffer: bytearray = field(init=False)
    output_buffer: bytearray = field(init=False)

    def __post_init__(self) -> None:
        self.input_buffer = bytearray(self.input_length)
        self.output_buffer = bytearray(self.output_length)

    def describe(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "ident_number": self.ident_number,
            "status": self.status,
            "input_length": self.input_length,
            "output_length": self.output_length,
        }


class RequestError(Exception):
    """A request that cannot be served; its message goes back to the client."""


def default_slaves() -> dict[int, MockSlave]:
    # Matching GSD files are in gsd/ next to this module.
    return {
        5: MockSlave(address=5, ident_number="0x809C", input_length=4, output_length=2),
        8: MockSlave(address=8, ident_number="0x80A5", input_length=8, output_length=4),
    }


class ProfibusMockServer:
    def __init__(self, config: MockConfig) -> None:
        self.config = config
        self.slaves = default_slaves()
        self._server: asyncio.Server | None = None
        self._update_task: asyncio.Task[None] | None = None

    @property
    def port(self) -> int:
        """The bound port (useful when started with port 0)."""
        if not self._server or not self._server.sockets:
            return self.config.port
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client, self.config.host, self.config.port, limit=MAX_LINE_BYTES
        )
        addr = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        console.print(f"[bold green]PROFIBUS mock listening on {addr}[/bold green]")
        # Keep a reference: the event loop holds tasks only weakly.
        self._update_task = asyncio.create_task(self._update_loop())

    async def stop(self) -> None:
        if self._update_task is not None:
            task, self._update_task = self._update_task, None
            task.cancel()
            # gather() absorbs the task's own CancelledError but still lets a
            # cancellation of stop() itself propagate (suppress() would hide it).
            await asyncio.gather(task, return_exceptions=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _update_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.update_interval)
            for slave in self.slaves.values():
                for idx in range(len(slave.input_buffer)):
                    slave.input_buffer[idx] = (slave.input_buffer[idx] + random.randint(0, 5)) & 0xFF
            if self.config.verbose:
                console.print("[cyan]Updated mock input data[/cyan]")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if self.config.verbose:
            console.print(f"[yellow]Client connected {peer}[/yellow]")
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    # Longer than MAX_LINE_BYTES: the stream cannot be resynchronised.
                    await self._send(writer, _error(f"Request longer than {MAX_LINE_BYTES} bytes"))
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                await self._send(writer, self.handle_line(line))
        except ConnectionError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            if self.config.verbose:
                console.print(f"[yellow]Client disconnected {peer}[/yellow]")

    def handle_line(self, line: bytes) -> dict[str, Any]:
        """Answer one request line. Never raises."""
        try:
            request = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            return _error("Invalid JSON")
        try:
            return self._dispatch(request)
        except RequestError as exc:
            return _error(str(exc))
        except Exception as exc:  # a bad request must not drop the connection
            return _error(f"Internal error: {type(exc).__name__}: {exc}")

    def _dispatch(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise RequestError("Request must be a JSON object")
        op = request.get("op")
        if op == "scan":
            return _ok([slave.describe() for slave in self.slaves.values()])
        if op == "read_inputs":
            slave = self._slave(request)
            length = request.get("length", slave.input_length)
            if not _is_int(length) or not 1 <= length <= MAX_DP_DATA_LEN:
                raise RequestError(f"length must be an integer between 1 and {MAX_DP_DATA_LEN}")
            if length > slave.input_length:
                raise RequestError(f"Slave {slave.address} has {slave.input_length} input bytes, {length} requested")
            data = bytes(slave.input_buffer[:length])
            return _ok({"address": slave.address, "length": length, "raw_data_hex": data.hex()})
        if op == "write_outputs":
            slave = self._slave(request)
            values = request.get("data")
            if not isinstance(values, list) or not values:
                raise RequestError("data must be a non-empty list of byte values (0-255)")
            if not all(_is_int(value) and 0 <= value <= 255 for value in values):
                raise RequestError("data must contain only integers between 0 and 255")
            if len(values) > slave.output_length:
                raise RequestError(f"Slave {slave.address} has {slave.output_length} output bytes, {len(values)} given")
            slave.output_buffer[: len(values)] = bytes(values)
            return _ok({"address": slave.address, "written_bytes": len(values)})
        if op == "read_outputs":
            slave = self._slave(request)
            return _ok({"address": slave.address, "raw_data_hex": bytes(slave.output_buffer).hex()})
        if op == "diagnosis":
            slave = self._slave(request)
            return _ok({"address": slave.address, "status": slave.status, "ident_number": slave.ident_number})
        raise RequestError(f"Unknown op {op!r}")

    def _slave(self, request: dict[str, Any]) -> MockSlave:
        address = request.get("address")
        if not _is_int(address) or not 0 <= address <= MAX_STATION_ADDRESS:
            raise RequestError(f"address must be an integer between 0 and {MAX_STATION_ADDRESS}")
        slave = self.slaves.get(address)
        if slave is None:
            raise RequestError(f"Slave not found at address {address}")
        return slave

    async def _send(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        writer.write(json.dumps(message).encode("utf-8") + b"\n")
        await writer.drain()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _error(message: str) -> dict[str, Any]:
    return {"success": False, "error": message}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.environ
    parser = argparse.ArgumentParser(description="Mock PROFIBUS DP slaves behind a TCP JSON-lines protocol.")
    parser.add_argument("--host", default=env.get("MOCK_PROFIBUS_HOST", "127.0.0.1"))
    # String defaults go through `type`, so a bad environment value is
    # reported like a bad flag instead of raising a traceback.
    parser.add_argument("--port", type=int, default=env.get("MOCK_PROFIBUS_PORT", "7100"))
    parser.add_argument("--update-interval", type=float, default=env.get("MOCK_PROFIBUS_UPDATE_INTERVAL", "1.0"))
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=env.get("MOCK_PROFIBUS_VERBOSE", "false").strip().lower() in _TRUE,
    )
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.update_interval <= 0:
        parser.error("--update-interval must be positive")
    return args


async def run_server(config: MockConfig) -> None:
    server = ProfibusMockServer(config)
    await server.start()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        console.print("[red]Stopping PROFIBUS mock...[/red]")
        await server.stop()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    config = MockConfig(host=args.host, port=args.port, update_interval=args.update_interval, verbose=args.verbose)
    try:
        asyncio.run(run_server(config))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        console.print(f"[red]Cannot start the PROFIBUS mock: {exc}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
