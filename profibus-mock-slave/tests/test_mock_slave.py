"""The mock answers every request with one JSON line and never drops the connection on bad input."""

from __future__ import annotations

import asyncio
import json

import pytest

from profibus_mock_slave import MAX_LINE_BYTES, MockConfig, ProfibusMockServer, parse_args


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


def test_invalid_json_gets_an_error_reply():
    server = ProfibusMockServer(MockConfig())
    assert server.handle_line(b"{nope") == {"success": False, "error": "Invalid JSON"}
    assert server.handle_line(b"\xff\xfe") == {"success": False, "error": "Invalid JSON"}


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
