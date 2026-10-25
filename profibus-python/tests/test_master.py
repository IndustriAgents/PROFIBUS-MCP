"""ProfibusMaster and its backends, tested against an in-process fake mock."""

from __future__ import annotations

import time

import anyio
import pytest
import serial

from profibus_mcp.config import ConfigError, ProfibusConfig
from profibus_mcp.pb_master import (
    BackendNotImplementedError,
    ClientDisconnectedError,
    InvalidArgumentError,
    MockBackend,
    OutcomeUnknownError,
    ProfibusMaster,
    ProfibusMasterError,
    RequestRejectedError,
    SerialBackend,
    WritesDisabledError,
    request_cancel_scope,
)

from .fakes import CLOSE, HANG, RecordingStream, fake_mock_server, free_port

pytestmark = pytest.mark.anyio

SLAVES = [
    {"address": 5, "ident_number": "0x809C", "status": "OK", "input_length": 4, "output_length": 2},
    {"address": 8, "ident_number": "0x80A5", "status": "OK", "input_length": 8, "output_length": 4},
]


def mock_master(port: int, **overrides) -> ProfibusMaster:
    settings = {"backend": "mock", "mock_port": port, "timeout_ms": 300, "retry_backoff_base": 0.0, **overrides}
    return ProfibusMaster(ProfibusConfig(**settings))


async def good_slave(request):
    op = request["op"]
    if op == "scan":
        return {"success": True, "data": SLAVES}
    if request.get("address") != 5:
        return {"success": False, "error": f"Slave not found at address {request.get('address')}", "applied": False}
    if op == "read_inputs":
        return {"success": True, "data": {"address": 5, "raw_data_hex": "0a0b0c0d"[: 2 * request.get("length", 4)]}}
    if op == "write_outputs":
        return {"success": True, "data": {"address": 5, "written_bytes": len(request["data"])}}
    if op == "diagnosis":
        return {"success": True, "data": {"address": 5, "status": "OK", "ident_number": "0x809C"}}
    return {"success": False, "error": "Unknown op", "applied": False}


async def test_mock_backend_round_trip():
    async with fake_mock_server(good_slave) as (port, received):
        master = mock_master(port, writes_enabled=True)
        slaves, meta = await master.scan_bus()
        assert [s["address"] for s in slaves] == [5, 8]
        assert meta["backend"] == "mock" and meta["endpoint"] == f"127.0.0.1:{port}"
        assert meta["operation"] == "scan_bus" and meta["attempts"] == 1 and meta["count"] == 2
        assert "duration_ms" in meta

        payload, meta = await master.read_inputs(5, 2)
        assert payload == b"\x0a\x0b" and meta["slave_address"] == 5
        payload, _ = await master.read_inputs(5)  # whole input image
        assert payload == bytes.fromhex("0a0b0c0d")

        written, meta = await master.write_outputs(5, [1, 255])
        assert written == 2 and meta["operation"] == "write_outputs"
        assert meta["outcome"] == "written" and meta["request_sent"] is True

        diag, meta = await master.read_diagnosis(5)
        assert diag == {"slave_address": 5, "status": "OK", "ident_number": "0x809C"}

    assert received == [
        {"op": "scan"},
        {"op": "read_inputs", "address": 5, "length": 2},
        {"op": "read_inputs", "address": 5},
        {"op": "write_outputs", "address": 5, "data": [1, 255]},
        {"op": "diagnosis", "address": 5},
    ]


@pytest.mark.parametrize(
    "entries",
    [
        [{"address": -1}],
        [{"address": 126}],
        [{"address": True}],
        [{"address": 5}, {"address": 5}],
        [{"address": 5, "input_length": 245}],
        [{"address": 5, "output_length": True}],
    ],
)
async def test_scan_rejects_invalid_or_ambiguous_slave_metadata(entries):
    from .fakes import FakeBackend

    backend = FakeBackend({"scan": entries})
    master = ProfibusMaster(ProfibusConfig(), backend)
    with pytest.raises(ProfibusMasterError):
        await master.scan_bus()
    assert len(backend.calls) == 1


async def test_whole_image_read_still_obeys_dp_payload_bound():
    from .fakes import FakeBackend

    backend = FakeBackend({"read_inputs": {"raw_data_hex": "00" * 245}})
    master = ProfibusMaster(ProfibusConfig(), backend)
    with pytest.raises(ProfibusMasterError, match="244"):
        await master.read_inputs(5)
    assert len(backend.calls) == 1


@pytest.mark.parametrize("reply_address", [6, True, "5"])
async def test_read_and_diagnosis_do_not_attribute_another_slaves_response(reply_address):
    from .fakes import FakeBackend

    backend = FakeBackend(
        {
            "read_inputs": {"address": reply_address, "raw_data_hex": "00"},
            "diagnosis": {"address": reply_address, "status": "OK"},
        }
    )
    master = ProfibusMaster(ProfibusConfig(), backend)
    for action in (master.read_inputs, master.read_diagnosis):
        with pytest.raises(ProfibusMasterError, match="slave address"):
            await action(5)
    assert len(backend.calls) == 2


async def test_ack_for_another_slave_is_an_unknown_write_without_resend():
    async def wrong(request):
        return {"success": True, "data": {"address": 6, "written_bytes": 1}}

    async with fake_mock_server(wrong) as (port, received):
        master = mock_master(port, writes_enabled=True)
        with pytest.raises(OutcomeUnknownError) as caught:
            await master.write_outputs(5, [1])
    assert caught.value.meta["outcome"] == "unknown" and len(received) == 1


async def test_error_reply_is_an_error_and_is_not_retried():
    async with fake_mock_server(good_slave) as (port, received):
        master = mock_master(port)
        with pytest.raises(ProfibusMasterError, match="Slave not found at address 9") as info:
            await master.read_inputs(9, 4)
    assert info.value.meta["backend"] == "mock"
    assert info.value.meta["attempts"] == 1
    assert len(received) == 1


async def test_error_without_a_refusal_marker_is_unknown_for_a_write():
    # Only "applied": false promises nothing changed; an internal error does not.
    async def handler(request):
        return {"success": False, "error": "Internal error: RuntimeError: disk on fire"}

    async with fake_mock_server(handler) as (port, received):
        master = mock_master(port, writes_enabled=True, max_retries=3)
        with pytest.raises(OutcomeUnknownError, match="may have been applied") as info:
            await master.write_outputs(5, [1])
        assert info.value.meta["outcome"] == "unknown" and info.value.meta["request_sent"] is True
        with pytest.raises(ProfibusMasterError, match="Internal error") as info:
            await master.read_inputs(5, 4)
        assert not isinstance(info.value, OutcomeUnknownError)
    assert len(received) == 2  # neither was retried


async def _garbage_forever(stream):
    """A peer that never sends a line end: garbage the reader can never complete."""
    try:
        while True:
            await stream.send(b"\x00garbage\xff")
            await anyio.sleep(0.02)
    except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
        pass


async def test_a_peer_answering_garbage_cannot_hang_a_read_or_a_write():
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(anyio.abc.SocketAttribute.local_port)
    async with listener, anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, _garbage_forever)
        master = mock_master(port, timeout_ms=200, max_retries=1, writes_enabled=True)
        bound = master.backend.deadline_s
        start = time.monotonic()
        with pytest.raises(ProfibusMasterError, match="did not answer"):
            await master.read_inputs(5, 4)
        assert time.monotonic() - start < bound
        start = time.monotonic()
        with pytest.raises(OutcomeUnknownError, match="may have been applied") as info:
            await master.write_outputs(5, [1])
        assert time.monotonic() - start < bound
        assert info.value.meta["request_sent"] is True and info.value.meta["outcome"] == "unknown"
        tg.cancel_scope.cancel()


@pytest.mark.parametrize("write", [False, True])
async def test_overall_deadline_bounds_a_request_that_hangs_anyway(monkeypatch, write):
    # Simulates a stack that swallows a reply without any per-attempt bound firing.
    async def hang(self, payload, state):
        state.mark_sent()
        await anyio.sleep(3600)

    monkeypatch.setattr(MockBackend, "_exchange", hang)
    master = mock_master(free_port(), timeout_ms=100, max_retries=1, writes_enabled=True)
    bound = master.backend.deadline_s
    assert bound < 2
    start = time.monotonic()
    with anyio.fail_after(bound + 3):  # the test fails instead of hanging if the deadline is missing
        if write:
            with pytest.raises(OutcomeUnknownError, match="overall deadline") as info:
                await master.write_outputs(5, [1])
            assert info.value.meta["outcome"] == "unknown"
        else:
            with pytest.raises(ProfibusMasterError, match="overall deadline") as info:
                await master.read_inputs(5, 4)
    assert info.value.meta["request_sent"] is True
    assert bound - 0.1 <= time.monotonic() - start < bound + 1


def test_deadline_covers_every_attempt_and_backoff():
    backend = MockBackend(ProfibusConfig(backend="mock", timeout_ms=500, max_retries=2, retry_backoff_base=0.1))
    # 3 attempts x 0.5 s + backoff 0.1 + 0.2 s + 1 s slack
    assert backend.deadline_s == pytest.approx(1.5 + 0.3 + 1.0)
    assert backend.status()["deadline_s"] == pytest.approx(2.8)


async def test_a_host_name_that_cannot_be_encoded_means_nothing_was_sent():
    # Passes the startup check (stdlib IDNA) but not anyio's stricter IDNA 2008
    # encoding, so connect_tcp raises IDNAError, not OSError, before any send.
    master = mock_master(free_port(), mock_host="\u2603.example", max_retries=0, writes_enabled=True)
    with pytest.raises(ProfibusMasterError) as info:
        await master.write_outputs(5, [1])
    assert not isinstance(info.value, OutcomeUnknownError)
    assert info.value.meta["outcome"] == "not_sent" and info.value.meta["request_sent"] is False
    assert "may have been applied" not in str(info.value)


@pytest.mark.parametrize("error", [UnicodeError("label too long"), ValueError("odd"), RuntimeError("boom")])
async def test_any_error_while_connecting_means_not_sent(monkeypatch, error):
    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(anyio, "connect_tcp", fail)
    master = mock_master(free_port(), max_retries=1, writes_enabled=True)
    with pytest.raises(ProfibusMasterError) as info:
        await master.write_outputs(5, [1])
    assert info.value.meta["outcome"] == "not_sent" and info.value.meta["request_sent"] is False
    assert info.value.meta["attempts"] == 2  # nothing was sent, so connecting may be retried


@pytest.mark.parametrize("sent", [False, True])
async def test_unexpected_error_reports_whether_anything_was_sent(monkeypatch, sent):
    async def broken(self, payload, state):
        if sent:
            state.mark_sent()
        raise RuntimeError("unexpected")

    monkeypatch.setattr(MockBackend, "_exchange", broken)
    master = mock_master(free_port(), writes_enabled=True)
    with pytest.raises(ProfibusMasterError, match="unexpected") as info:
        await master.write_outputs(5, [1])
    assert info.value.meta["request_sent"] is sent
    assert info.value.meta["outcome"] == ("unknown" if sent else "not_sent")
    assert isinstance(info.value, OutcomeUnknownError) is sent


@pytest.mark.parametrize("meta", [{}, {"request_sent": None}])
async def test_backend_error_without_send_evidence_remains_unknown(meta):
    from .fakes import FakeBackend

    backend = FakeBackend({"write_outputs": ProfibusMasterError("backend failed after possible send", meta)})
    master = ProfibusMaster(ProfibusConfig(writes_enabled=True), backend)
    with pytest.raises(OutcomeUnknownError) as caught:
        await master.write_outputs(5, [1])
    assert caught.value.meta["outcome"] == "unknown"
    assert caught.value.meta["request_sent"] is None
    assert len(backend.calls) == 1


async def test_explicit_backend_refusal_reports_the_request_was_sent():
    from .fakes import FakeBackend

    backend = FakeBackend({"write_outputs": RequestRejectedError("device refused")})
    master = ProfibusMaster(ProfibusConfig(writes_enabled=True), backend)
    with pytest.raises(RequestRejectedError) as caught:
        await master.write_outputs(5, [1])
    assert caught.value.meta["outcome"] == "rejected"
    assert caught.value.meta["request_sent"] is True
    assert len(backend.calls) == 1


async def test_client_disconnect_stops_a_write_waiting_to_retry():
    # Nothing listens yet, so the write waits in backoff when the client goes away.
    port = free_port()
    master = mock_master(port, max_retries=5, retry_backoff_base=2.0, writes_enabled=True)

    async def disconnect_soon():
        await anyio.sleep(0.3)
        master.shutdown()

    async with anyio.create_task_group() as tg:
        tg.start_soon(disconnect_soon)
        with anyio.fail_after(5), pytest.raises(ClientDisconnectedError, match="nothing was sent") as info:
            await master.write_outputs(5, [1])
    assert info.value.meta["outcome"] == "not_sent" and info.value.meta["request_sent"] is False
    # The mock coming up afterwards receives nothing.
    async with fake_mock_server(good_slave) as (_, received):
        await anyio.sleep(0.2)
    assert received == []


async def test_disconnect_between_connect_and_send_sends_nothing(monkeypatch):
    # Cancellation is only delivered at the next suspension point; the closing
    # flag is checked right before the send so this window cannot leak a write.
    async with fake_mock_server(good_slave) as (port, received):
        master = mock_master(port, writes_enabled=True)
        real_connect = anyio.connect_tcp

        async def connect_then_disconnect(*args, **kwargs):
            stream = await real_connect(*args, **kwargs)
            master.shutdown()
            return stream

        monkeypatch.setattr(anyio, "connect_tcp", connect_then_disconnect)
        with pytest.raises(ClientDisconnectedError) as info:
            await master.write_outputs(5, [1])
        await anyio.sleep(0.1)
    assert info.value.meta["outcome"] == "not_sent" and info.value.meta["request_sent"] is False
    assert received == []


async def test_client_disconnect_while_waiting_for_the_reply_is_unknown():
    async def hang(request):
        return HANG

    async with fake_mock_server(hang) as (port, received):
        master = mock_master(port, timeout_ms=5000, writes_enabled=True)

        async def disconnect_soon():
            await anyio.sleep(0.3)
            master.shutdown()

        async with anyio.create_task_group() as tg:
            tg.start_soon(disconnect_soon)
            with anyio.fail_after(5), pytest.raises(OutcomeUnknownError, match="may have been applied") as info:
                await master.write_outputs(5, [1])
    assert info.value.meta["request_sent"] is True and info.value.meta["outcome"] == "unknown"
    assert len(received) == 1


async def test_no_request_is_sent_after_the_client_disconnected():
    async with fake_mock_server(good_slave) as (port, received):
        master = mock_master(port, writes_enabled=True)
        master.shutdown()
        with pytest.raises(ClientDisconnectedError) as info:
            await master.write_outputs(5, [1])
        with pytest.raises(ClientDisconnectedError):
            await master.read_inputs(5, 4)
    assert info.value.meta["outcome"] == "not_sent"
    assert info.value.meta["attempts"] == 0  # not even a connection attempt
    assert received == []


async def test_cancel_in_the_same_step_as_connect_completion_never_sends(monkeypatch):
    """The CANCEL RACE: the call is cancelled in the same loop step as its connect completes.

    anyio does not interrupt a task whose wait has just finished, so the task
    runs one more step. The pre-send gate must refuse on cancel_called, not
    wait for CancelledError. Looped, and it checks that send() is never even
    attempted, not just that nothing arrived.
    """
    attempted: list[bytes] = []
    async with fake_mock_server(good_slave) as (port, received):
        master = mock_master(port, writes_enabled=True)
        real_connect = anyio.connect_tcp
        for _ in range(100):
            scope = anyio.CancelScope()
            waiting, go = anyio.Event(), anyio.Event()

            async def connect(*args, _go=go, _waiting=waiting, **kwargs):
                stream = await real_connect(*args, **kwargs)
                _waiting.set()
                await _go.wait()  # this wait completes in the same step as the cancel
                return RecordingStream(stream, attempted)

            monkeypatch.setattr(anyio, "connect_tcp", connect)

            async def complete_and_cancel(_go=go, _waiting=waiting, _scope=scope):
                await _waiting.wait()
                _go.set()
                _scope.cancel()  # like RequestResponder.cancel(): synchronous

            async with anyio.create_task_group() as tg:
                tg.start_soon(complete_and_cancel)
                with scope, request_cancel_scope(scope):
                    await master.write_outputs(5, [1])
            assert scope.cancelled_caught
        await anyio.sleep(0.1)
    assert attempted == []
    assert received == []


async def test_a_call_cancelled_before_it_starts_never_connects(monkeypatch):
    # cancel_called is already set when the request starts (no checkpoint has
    # delivered it yet): it must not even connect, and it must end as a
    # cancellation (the SDK has already replied), not as a second answer.
    async with fake_mock_server(good_slave) as (port, received):
        master = mock_master(port, writes_enabled=True)
        connects: list[tuple] = []
        real_connect = anyio.connect_tcp

        async def counting_connect(*args, **kwargs):
            connects.append(args)
            return await real_connect(*args, **kwargs)

        monkeypatch.setattr(anyio, "connect_tcp", counting_connect)
        scope = anyio.CancelScope()
        with scope, request_cancel_scope(scope):
            scope.cancel()  # synchronous, like RequestResponder.cancel()
            await master.write_outputs(5, [1])
        assert scope.cancelled_caught
        await anyio.sleep(0.1)
    assert connects == []
    assert received == []


async def test_unreachable_mock_is_retried_then_reported():
    master = mock_master(free_port(), max_retries=2)
    with pytest.raises(ProfibusMasterError, match="Cannot reach the PROFIBUS mock slave") as info:
        await master.scan_bus()
    assert info.value.meta["backend"] == "mock"
    assert info.value.meta["attempts"] == 3
    assert info.value.meta["operation"] == "scan_bus"


async def test_reads_are_retried_after_a_timeout_and_writes_are_not():
    async with fake_mock_server(lambda request: _hang()) as (port, received):
        master = mock_master(port, max_retries=1, timeout_ms=100, writes_enabled=True)
        with pytest.raises(ProfibusMasterError, match="did not answer within 100 ms") as info:
            await master.read_inputs(5, 4)
        assert info.value.meta["attempts"] == 2
        reads = len(received)

        with pytest.raises(OutcomeUnknownError, match="did not answer") as info:
            await master.write_outputs(5, [1])
        # The write reached the slave once; sending it again could repeat it.
        assert info.value.meta["attempts"] == 1
        assert len(received) == reads + 1
        assert info.value.meta["outcome"] == "unknown" and info.value.meta["request_sent"] is True
        assert "may have been applied" in str(info.value) and "before retrying" in str(info.value)


@pytest.mark.parametrize("reply", [CLOSE, b"not json\n", {"data": []}, {"success": True, "data": {}}])
async def test_write_without_a_valid_reply_is_unknown_and_not_resent(reply):
    async def handler(request):
        return reply

    async with fake_mock_server(handler) as (port, received):
        master = mock_master(port, max_retries=3, writes_enabled=True)
        with pytest.raises(OutcomeUnknownError, match="may have been applied") as info:
            await master.write_outputs(5, [1])
    assert len(received) == 1
    assert info.value.meta["outcome"] == "unknown" and info.value.meta["request_sent"] is True


async def test_rejected_write_is_reported_as_rejected():
    async with fake_mock_server(good_slave) as (port, received):
        with pytest.raises(RequestRejectedError, match="Slave not found at address 9") as info:
            await mock_master(port, writes_enabled=True).write_outputs(9, [1])
    assert len(received) == 1
    assert info.value.meta["outcome"] == "rejected" and info.value.meta["request_sent"] is True


async def _hang():
    return HANG


async def test_write_is_retried_when_the_connection_was_never_made():
    master = mock_master(free_port(), max_retries=2, writes_enabled=True)
    with pytest.raises(ProfibusMasterError, match="Cannot reach") as info:
        await master.write_outputs(5, [1])
    assert info.value.meta["attempts"] == 3
    assert info.value.meta["outcome"] == "not_sent" and info.value.meta["request_sent"] is False


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (b"not json\n", "not JSON"),
        ({"data": []}, "unexpected reply"),
        ({"success": True, "data": {"raw_data_hex": "zz"}}, "malformed read_inputs"),
        ({"success": True, "data": {"raw_data_hex": "00"}}, "returned 1 input bytes, 4 were requested"),
        (CLOSE, "connection to the mock slave"),
    ],
)
async def test_bad_replies_are_errors_not_data(reply, message):
    async def handler(request):
        return reply

    async with fake_mock_server(handler) as (port, _):
        with pytest.raises(ProfibusMasterError, match=message):
            await mock_master(port, max_retries=0).read_inputs(5, 4)


async def test_partial_write_is_an_error():
    async def handler(request):
        return {"success": True, "data": {"written_bytes": 1}}

    async with fake_mock_server(handler) as (port, _):
        with pytest.raises(OutcomeUnknownError, match="reported 1 of 2 output bytes written") as info:
            await mock_master(port, writes_enabled=True).write_outputs(5, [1, 2])
    assert info.value.meta["outcome"] == "unknown"


async def test_writes_are_refused_by_default_before_anything_is_sent():
    async with fake_mock_server(good_slave) as (port, received):
        master = mock_master(port)
        assert master.config.writes_enabled is False
        with pytest.raises(WritesDisabledError, match="PROFIBUS_WRITES_ENABLED=true") as info:
            await master.write_outputs(5, [1])
    assert received == []
    assert info.value.meta == {
        "backend": "mock",
        "writes_enabled": False,
        "slave_address": 5,
        "outcome": "not_sent",
        "request_sent": False,
    }


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda m: m.read_inputs(126, 1), "slave_address"),
        (lambda m: m.read_inputs(True, 1), "slave_address"),
        (lambda m: m.read_inputs(5, 0), "length"),
        (lambda m: m.read_inputs(5, 245), "length"),
        (lambda m: m.write_outputs(5, [256]), r"data\[0\]"),
        (lambda m: m.write_outputs(5, [-1]), r"data\[0\]"),
        (lambda m: m.write_outputs(5, []), "between 1 and 244"),
        (lambda m: m.write_outputs(5, "ab"), "list of byte values"),
        (lambda m: m.read_diagnosis(-1), "slave_address"),
    ],
)
async def test_invalid_arguments_are_rejected_before_sending(call, message):
    async with fake_mock_server(good_slave) as (port, received):
        with pytest.raises(InvalidArgumentError, match=message) as info:
            await call(mock_master(port, writes_enabled=True))
    assert received == []
    if "data" in message or "244" in message or "byte values" in message:
        assert info.value.meta["outcome"] == "not_sent"


async def test_serial_backend_is_not_implemented_and_never_opens_the_port(monkeypatch):
    def refuse_open(self):
        raise AssertionError("the serial port must not be opened")

    monkeypatch.setattr(serial.Serial, "open", refuse_open)
    master = ProfibusMaster(ProfibusConfig(port="/dev/ttyUSB0", writes_enabled=True))
    assert isinstance(master.backend, SerialBackend)
    for call in (
        master.scan_bus(),
        master.read_inputs(5, 4),
        master.write_outputs(5, [1]),
        master.read_diagnosis(5),
    ):
        with pytest.raises(BackendNotImplementedError, match="not implemented") as info:
            await call
        assert info.value.meta["backend"] == "serial"
        assert info.value.meta["implemented"] is False
        assert info.value.meta["request_sent"] is False
    status = master.status()
    assert status["port_opened"] is False and status["bus_communication"] == "not implemented"


@pytest.mark.parametrize("port", ["socket://127.0.0.1:7100", "rfc2217://gateway:4000", "loop://", "COM3"])
def test_serial_backend_accepts_pyserial_urls(port):
    # serial.Serial(port=...) rejected URLs; serial_for_url accepts them.
    assert SerialBackend(ProfibusConfig(port=port)).status()["port"] == port


@pytest.mark.parametrize(
    "port",
    [
        "sockt://127.0.0.1:7100",  # typo
        "hwgrep://.*",  # test-opens every matching port even with do_not_open=True
        "spy:///dev/ttyUSB0",
        "alt:///dev/ttyUSB0?class=PosixPollSerial",
        "cp2110://0001:0002:00",
    ],
)
def test_serial_backend_rejects_url_schemes_outside_the_allow_list(port, monkeypatch):
    def must_not_be_called(*args, **kwargs):
        raise AssertionError("serial_for_url must not see a refused URL")

    monkeypatch.setattr(serial, "serial_for_url", must_not_be_called)
    with pytest.raises(ConfigError, match="PROFIBUS_PORT scheme"):
        SerialBackend(ProfibusConfig(port=port))


def test_status_reports_one_writes_flag():
    master = ProfibusMaster(ProfibusConfig(backend="mock", writes_enabled=True))
    status = master.status()
    assert status["backend"] == "mock" and status["endpoint"] == "127.0.0.1:7100"
    assert status["writes_enabled"] is True
    assert isinstance(master.backend, MockBackend)
