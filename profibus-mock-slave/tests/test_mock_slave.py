"""The mock answers every request with one JSON line and never drops the connection on bad input."""

from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from profibus_mock_slave import MAX_LINE_BYTES, MockConfig, ProfibusMockServer, parse_args, watch_stdin_eof


def dispatch(request) -> dict:
    server = ProfibusMockServer(MockConfig())
    return server.handle_line(json.dumps(request).encode())


def test_scan_lists_both_slaves_with_their_io_lengths():
    reply = dispatch({"op": "scan"})
    assert reply["success"] is True
    assert reply["data"] == [
        {"address": 5, "ident_number": "0x809C", "status": "OK", "input_length": 4, "output_length": 2},
        {"address": 8, "ident_number": "0x80A5", "status": "OK", "input_length": 8, "output_length": 4},
    ]


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        ([1, 2], "Request must be a JSON object"),
        ("scan", "Request must be a JSON object"),
        ({"op": "read_inputs", "address": "five"}, "address must be an integer"),
        ({"op": "read_inputs", "address": 5.5}, "address must be an integer"),
        ({"op": "read_inputs", "address": True}, "address must be an integer"),
        ({"op": "read_inputs"}, "address must be an integer"),
        ({"op": "diagnosis", "address": 9}, "Slave not found at address 9"),
        ({"op": "read_inputs", "address": 5, "length": 5}, "has 4 input bytes, 5 requested"),
        ({"op": "read_inputs", "address": 5, "length": "4"}, "length must be an integer"),
        ({"op": "read_inputs", "address": 5, "length": 0}, "length must be an integer"),
        ({"op": "write_outputs", "address": 5, "data": [1, 2, 3]}, "has 2 output bytes, 3 given"),
        ({"op": "write_outputs", "address": 5, "data": [256]}, "between 0 and 255"),
        ({"op": "write_outputs", "address": 5, "data": ["1"]}, "between 0 and 255"),
        ({"op": "write_outputs", "address": 5, "data": []}, "non-empty list"),
        ({"op": "write_outputs", "address": 5}, "non-empty list"),
        ({"op": "reboot"}, "Unknown op 'reboot'"),
    ],
)
def test_bad_requests_get_an_error_reply(request_, message):
    reply = dispatch(request_)
    assert reply["success"] is False
    assert message in reply["error"]
    # Every check runs before a buffer changes, so these are explicit refusals.
    assert reply["applied"] is False


def test_internal_error_does_not_claim_nothing_was_applied(monkeypatch):
    server = ProfibusMockServer(MockConfig())

    def explode(request):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(server, "_dispatch", explode)
    reply = server.handle_line(b'{"op": "write_outputs", "address": 5, "data": [1]}')
    assert reply["success"] is False and "Internal error" in reply["error"]
    assert "applied" not in reply


def test_invalid_json_gets_an_error_reply():
    server = ProfibusMockServer(MockConfig())
    assert server.handle_line(b"{nope") == {"success": False, "error": "Invalid JSON", "applied": False}
    assert server.handle_line(b"\xff\xfe") == {"success": False, "error": "Invalid JSON", "applied": False}


def test_write_never_grows_the_output_buffer():
    server = ProfibusMockServer(MockConfig())
    slave = server.slaves[5]
    assert server.handle_line(b'{"op": "write_outputs", "address": 5, "data": [1, 2, 3, 4]}')["success"] is False
    assert len(slave.output_buffer) == 2
    reply = server.handle_line(b'{"op": "write_outputs", "address": 5, "data": [7]}')
    assert reply == {"success": True, "data": {"address": 5, "written_bytes": 1}}
    assert bytes(slave.output_buffer) == b"\x07\x00"
    assert server.handle_line(b'{"op": "read_outputs", "address": 5}')["data"]["raw_data_hex"] == "0700"


def test_read_inputs_defaults_to_the_whole_input_image():
    reply = dispatch({"op": "read_inputs", "address": 8})
    assert reply["success"] is True
    assert reply["data"]["length"] == 8 and len(bytes.fromhex(reply["data"]["raw_data_hex"])) == 8


def test_connection_survives_bad_requests_and_update_task_is_kept_and_stopped():
    async def scenario() -> None:
        server = ProfibusMockServer(MockConfig(host="127.0.0.1", port=0, update_interval=0.01))
        await server.start()
        task = server._update_task
        assert task is not None and not task.done()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)

        async def ask(line: bytes) -> dict:
            writer.write(line + b"\n")
            await writer.drain()
            return json.loads(await asyncio.wait_for(reader.readline(), 5))

        try:
            assert (await ask(b"[1,2]"))["success"] is False
            assert (await ask(b'{"op": "read_inputs", "address": "x"}'))["success"] is False
            assert (await ask(b"not json"))["error"] == "Invalid JSON"
            # Same connection, still answering.
            assert (await ask(b'{"op": "diagnosis", "address": 5}'))["data"]["status"] == "OK"
            await asyncio.sleep(0.05)
            assert any(server.slaves[5].input_buffer) or any(server.slaves[8].input_buffer)
        finally:
            writer.close()
            await writer.wait_closed()
            # A stop() that does not cancel the update task would hang here.
            await asyncio.wait_for(server.stop(), 2)
        assert task.done()
        assert server._update_task is None

    asyncio.run(scenario())


def test_oversized_request_line_is_answered_then_closed():
    async def scenario() -> None:
        server = ProfibusMockServer(MockConfig(host="127.0.0.1", port=0))
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        try:
            writer.write(b"x" * (MAX_LINE_BYTES + 10) + b"\n")
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(reader.readline(), 5))
            assert reply["success"] is False and "longer than" in reply["error"]
            assert await asyncio.wait_for(reader.read(), 5) == b""
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(server.stop(), 2)

    asyncio.run(scenario())


def test_environment_is_read_by_parse_args(monkeypatch):
    monkeypatch.setenv("MOCK_PROFIBUS_PORT", "7200")
    monkeypatch.setenv("MOCK_PROFIBUS_VERBOSE", "yes")
    args = parse_args([])
    assert args.port == 7200 and args.verbose is True
    monkeypatch.setenv("MOCK_PROFIBUS_PORT", "not-a-port")
    with pytest.raises(SystemExit):
        parse_args([])


def test_stdin_watch_sets_stop_at_end_of_file():
    async def scenario() -> None:
        stop = asyncio.Event()
        watch_stdin_eof(asyncio.get_running_loop(), stop, io.BytesIO(b"some input, then EOF"))
        await asyncio.wait_for(stop.wait(), 5)

    asyncio.run(scenario())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(sys.platform == "win32", reason="uses SIGKILL")
def test_mock_exits_when_its_parent_is_killed():
    """A parent killed with SIGKILL (like a killed pytest run) must not leave the mock behind."""
    port = _free_port()
    parent_script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-m', 'profibus_mock_slave', '--port', sys.argv[1],"
        " '--exit-on-stdin-eof'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(600)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_script, str(port)], stdout=subprocess.PIPE)
    child_pid = int(parent.stdout.readline())
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "the mock never started listening"
                time.sleep(0.1)
        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=10)
        deadline = time.monotonic() + 10
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(child_pid), "the mock outlived its killed parent"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if _alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)
