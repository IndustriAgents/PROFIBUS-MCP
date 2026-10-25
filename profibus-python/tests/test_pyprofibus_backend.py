"""Pipe lifecycle checks need no pyprofibus installation or physical device."""

import json
import subprocess
import sys
import threading
import time
from dataclasses import replace

import anyio
import pytest

from profibus_mcp.adapters.pb_master import OutcomeUnknownError, ProfibusMaster, ProfibusMasterError
from profibus_mcp.adapters.pyprofibus_backend import PyprofibusBackend
from profibus_mcp.config import ConfigError, ProfibusConfig
from profibus_mcp.domain.capabilities import capabilities


def config(tmp_path, **options):
    return ProfibusConfig(
        backend="pyprofibus",
        pyprofibus_config=tmp_path / "unused.conf",
        pyprofibus_outputs=tmp_path / "unused.json",
        pyprofibus_phy="dummy_slave",
        bus_enabled=True,
        writes_enabled=True,
        timeout_ms=500,
        **options,
    )


def pipe_backend(tmp_path, monkeypatch, code):
    backend = PyprofibusBackend(config(tmp_path))
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    backend._process = process
    monkeypatch.setattr(backend, "_start", lambda *_: process)
    return backend, process


def test_config_and_static_capabilities(tmp_path):
    with pytest.raises(ConfigError, match="CONFIG"):
        ProfibusConfig(backend="pyprofibus")
    value = config(tmp_path)
    backend = PyprofibusBackend(value)
    assert backend.status()["worker_running"] is False
    assert backend.status()["simulated"] is True
    support = capabilities("pyprofibus", writes_enabled=True, extra_gate=False, phy="dummy_slave")
    assert support["operations"]["read_inputs"] == {
        "supported": True,
        "enabled": False,
        "reason": "Disabled by native configuration gates",
    }
    assert support["operations"]["scan_bus"]["supported"] is False
    assert support["operations"]["read_diagnosis"]["supported"] is True
    assert support["operations"]["read_diagnosis"]["enabled"] is False
    assert support["simulated"] is True


@pytest.mark.anyio
async def test_disabled_bus_and_write_gates_do_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_kw: pytest.fail("must not spawn"))
    master = ProfibusMaster(replace(config(tmp_path), bus_enabled=False))
    with pytest.raises(ProfibusMasterError, match="BUS_ENABLED") as error:
        await master.read_inputs(8)
    assert error.value.meta["request_sent"] is False
    master = ProfibusMaster(replace(config(tmp_path), writes_enabled=False))
    with pytest.raises(ProfibusMasterError, match="Writes are disabled"):
        await master.write_outputs(8, [1, 2])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        '{"id":999,"ok":true,"data":{"written_bytes":2}}',
        '{"id":true,"ok":true}',
        '{"id":1,"ok":"true"}',
        "x" * 16385,
        '{"id":1,"ok":true,"data":{"written_bytes":1}}',
    ],
)
async def test_bad_write_replies_are_unknown_without_retry(tmp_path, monkeypatch, response):
    code = f"import sys\nsys.stdin.readline()\nprint({response!r}, flush=True)\n"
    backend, process = pipe_backend(tmp_path, monkeypatch, code)
    master = ProfibusMaster(backend.config, backend)
    try:
        with pytest.raises(OutcomeUnknownError) as error:
            await master.write_outputs(8, [1, 2])
        assert error.value.meta["request_sent"] is True
        assert backend._sequence == 1
    finally:
        backend.shutdown()
        process.wait(timeout=2)


@pytest.mark.anyio
async def test_timeout_reaps_worker_and_latches_no_restart(tmp_path, monkeypatch):
    backend, process = pipe_backend(tmp_path, monkeypatch, "import sys,time;sys.stdin.readline();time.sleep(30)")
    backend.config = replace(backend.config, timeout_ms=50)
    master = ProfibusMaster(backend.config, backend)
    with pytest.raises(OutcomeUnknownError):
        await master.write_outputs(8, [1, 2])
    assert process.poll() is not None
    assert backend.status()["fault"]
    with pytest.raises(ProfibusMasterError) as error:
        await master.write_outputs(8, [3, 4])
    assert error.value.meta["request_sent"] is False
    assert backend._sequence == 1


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [False, True])
async def test_diagnostic_deadline_or_cancel_reaps_worker_without_replay(tmp_path, monkeypatch, cancel):
    backend, process = pipe_backend(tmp_path, monkeypatch, "import sys,time;sys.stdin.readline();time.sleep(30)")
    backend.config = replace(backend.config, timeout_ms=100 if cancel else 30)
    try:
        if cancel:
            with anyio.move_on_after(0.03):
                await backend.request("diagnosis", {"address": 8}, retry_after_send=True)
        else:
            with pytest.raises(ProfibusMasterError):
                await backend.request("diagnosis", {"address": 8}, retry_after_send=True)
        with anyio.fail_after(2):
            while backend._lease.locked():
                await anyio.sleep(0.001)
        assert process.poll() is not None and backend.status()["fault"]
        assert backend._sequence == 1
    finally:
        backend.shutdown()


@pytest.mark.anyio
async def test_diagnostic_success_without_actual_bytes_is_not_accepted(tmp_path, monkeypatch):
    response = json.dumps({"id": 1, "ok": True, "data": {"address": 8, "status": "ready"}})
    code = f"import sys,time;sys.stdin.readline();print({response!r});time.sleep(30)"
    backend, process = pipe_backend(tmp_path, monkeypatch, code)
    with pytest.raises(ProfibusMasterError, match="diagnostic payload"):
        await backend.request("diagnosis", {"address": 8}, retry_after_send=True)
    assert process.poll() is not None and backend.status()["fault"]


@pytest.mark.anyio
async def test_cancel_after_dispatch_holds_lease_then_reaps(tmp_path, monkeypatch):
    marker = tmp_path / "dispatched"
    code = f"import sys,time,pathlib;sys.stdin.readline();pathlib.Path({str(marker)!r}).touch();time.sleep(30)"
    backend, process = pipe_backend(tmp_path, monkeypatch, code)
    scope = anyio.CancelScope()

    async def call():
        with scope:
            await backend.request("write_outputs", {"address": 8, "data": [1, 2]}, retry_after_send=False)

    async with anyio.create_task_group() as group:
        group.start_soon(call)
        with anyio.fail_after(2):
            while not marker.exists():
                await anyio.sleep(0.001)
        scope.cancel()
    with anyio.fail_after(2):
        while backend._lease.locked():
            await anyio.sleep(0.005)
    assert process.poll() is not None
    assert backend.status()["fault"]
    assert backend._sequence == 1


@pytest.mark.anyio
async def test_cancel_before_thread_start_cannot_release_future_lease(tmp_path, monkeypatch):
    backend = PyprofibusBackend(config(tmp_path))
    captured = []

    async def cancel_before_run(function, **_kwargs):
        captured.append(function)
        raise anyio.get_cancelled_exc_class()()

    monkeypatch.setattr(anyio.to_thread, "run_sync", cancel_before_run)
    with pytest.raises(anyio.get_cancelled_exc_class()):
        await backend.request("write_outputs", {"address": 8, "data": [1, 2]}, retry_after_send=False)
    assert backend._lease.acquire(blocking=False)
    try:
        with pytest.raises(ProfibusMasterError, match="before worker dispatch"):
            captured[0]()
        assert backend._lease.locked()
        assert backend._process is None
    finally:
        backend._lease.release()


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["spawn", "startup_exchange"])
async def test_cancel_during_start_always_reaps_owned_child(tmp_path, monkeypatch, phase):
    backend = PyprofibusBackend(config(tmp_path))
    original_popen = subprocess.Popen
    spawned = threading.Event()
    release = threading.Event()
    marker = tmp_path / "startup_request"
    processes = []
    code = f"import sys,time,pathlib;sys.stdin.readline();pathlib.Path({str(marker)!r}).touch();time.sleep(30)"

    def create(*_args, **kwargs):
        child = original_popen([sys.executable, "-u", "-c", code], **kwargs)
        processes.append(child)
        spawned.set()
        if phase == "spawn":
            release.wait(2)
        return child

    monkeypatch.setattr(subprocess, "Popen", create)
    scope = anyio.CancelScope()

    async def call():
        with scope:
            await backend.request("read_inputs", {"address": 8}, retry_after_send=True)

    try:
        async with anyio.create_task_group() as group:
            group.start_soon(call)
            with anyio.fail_after(2):
                while not (spawned.is_set() if phase == "spawn" else marker.exists()):
                    await anyio.sleep(0.001)
            scope.cancel()
        release.set()
        with anyio.fail_after(2):
            while backend._lease.locked():
                await anyio.sleep(0.001)
        assert len(processes) == 1 and processes[0].poll() is not None
        assert backend.status()["fault"]
        if phase == "startup_exchange":
            assert backend._startup_dispatched is True
        else:
            assert not marker.exists()  # Cancelled before any start command.
    finally:
        release.set()
        backend.shutdown()


@pytest.mark.anyio
async def test_process_image_ack_is_submitted_not_physical(tmp_path, monkeypatch):
    reply = json.dumps({"id": 1, "ok": True, "data": {"address": 8, "written_bytes": 2}})
    backend, process = pipe_backend(
        tmp_path, monkeypatch, f"import sys,time;sys.stdin.readline();print({reply!r});time.sleep(5)"
    )
    try:
        count, meta = await ProfibusMaster(backend.config, backend).write_outputs(8, [1, 2])
        assert count == 2
        assert meta["outcome"] == "submitted"
        assert meta["physical_completion_verified"] is False
        assert meta["confirmation"] == "process_image_accepted"
    finally:
        backend.shutdown()
        process.wait(timeout=2)


def test_shutdown_reaps_idle_worker(tmp_path, monkeypatch):
    backend, process = pipe_backend(tmp_path, monkeypatch, "import time;time.sleep(30)")
    start = time.monotonic()
    backend.shutdown()
    assert process.poll() is not None
    assert time.monotonic() - start < 2


def test_worker_stops_before_reconnection_can_restage_outputs(monkeypatch):
    from profibus_mcp.adapters import pyprofibus_worker as worker

    end = threading.Event()

    def receive():
        end.wait(2)
        raise ValueError("EOF")

    class Slave:
        slaveAddr = 8
        connected = True
        stages = 0

        def isConnected(self):
            return self.connected

        def setMasterOutData(self, _value):
            self.stages += 1

        def getMasterInData(self):
            return None

    slave = Slave()

    class Master:
        cycles = 0

        def run(self):
            self.cycles += 1
            if self.cycles == 2:
                slave.connected = False
            return slave

    master = Master()
    monkeypatch.setattr(worker, "receive", receive)
    try:
        with pytest.raises(RuntimeError, match="Data_Exchange lost"):
            worker.run(master, {8: slave}, {8: bytearray([0, 0])})
        assert master.cycles == 2 and slave.stages == 2
    finally:
        end.set()
