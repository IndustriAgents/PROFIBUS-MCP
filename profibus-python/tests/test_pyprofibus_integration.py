"""Actual pinned upstream DP state machine, using only its dummy_slave PHY.

Run with PROFIBUS_TEST_PYTHON pointing to the separate pyprofibus interpreter.
CI installs that runtime from its pinned official source in a dedicated job.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from profibus_mcp.adapters.pb_master import ProfibusMaster, ProfibusMasterError
from profibus_mcp.config import ProfibusConfig

RUNTIME = os.environ.get("PROFIBUS_TEST_PYTHON")
pytestmark = [pytest.mark.anyio, pytest.mark.skipif(not RUNTIME, reason="Optional pyprofibus runtime not installed")]


@pytest.fixture
def config(tmp_path):
    fixtures = Path(__file__).resolve().parents[2] / "examples" / "pyprofibus"
    shutil.copytree(fixtures, tmp_path, dirs_exist_ok=True)
    return ProfibusConfig(
        backend="pyprofibus",
        pyprofibus_python=RUNTIME,
        pyprofibus_phy="dummy_slave",
        pyprofibus_config=tmp_path / "dummy.conf",
        pyprofibus_outputs=tmp_path / "outputs.json",
        baudrate=19200,
        timeout_ms=3000,
        bus_enabled=True,
        writes_enabled=True,
    )


async def test_real_upstream_state_machine_round_trip(config):
    master = ProfibusMaster(config)
    try:
        assert master.status()["worker_running"] is False
        sample, meta = await master.read_inputs(8)
        assert sample == b"\xff\xff"  # Upstream dummy inverts actual Data_Exchange DU.
        assert meta["simulated"] is True and meta["fresh_sample"] is True
        count, meta = await master.write_outputs(8, [0x12, 0x34])
        assert count == 2 and meta["outcome"] == "submitted"
        for _ in range(10):
            sample, _ = await master.read_inputs(8)
            if sample == b"\xed\xcb":
                break
        assert sample == b"\xed\xcb"
        assert master.status()["configured_stations"] == [8]
        with pytest.raises(ProfibusMasterError, match="complete configured") as error:
            await master.write_outputs(8, [1])
        assert error.value.meta["outcome"] == "rejected"
        with pytest.raises(ProfibusMasterError, match="not in the configured"):
            await master.read_inputs(9)
        # Known rejections leave the cyclic session usable.
        assert (await master.read_inputs(8))[0] == b"\xed\xcb"
    finally:
        master.shutdown()


async def test_actual_upstream_diagnostics_are_fresh_received_telegrams(config):
    master = ProfibusMaster(config)
    try:
        first, meta = await master.read_diagnosis(8)
        second, _ = await master.read_diagnosis(8)
        assert second["sample_sequence"] > first["sample_sequence"]
        assert first["station_status"] == [0, 4, 0]
        # Upstream dummy returns ident 0 rather than the configured GSD identity.
        assert first["ident_number"] == "0x0000" and first["status"] == "identity_mismatch"
        assert first["raw_data_hex"] == "000400ff0000"
        assert meta["source"] == "observed_slave_diag" and meta["fresh_sample"] is True
        assert meta["correlation"] == "station_and_service_after_native_submission"
        assert meta["acquisition_timestamp_available"] is False
        assert (await master.read_inputs(8))[0] == b"\xff\xff"  # diagnostics did not steal native replies
    finally:
        master.shutdown()


async def test_diagnostics_require_native_periodic_configuration(config):
    config.pyprofibus_config.write_text(config.pyprofibus_config.read_text().replace("diag_period=10", "diag_period=0"))
    master = ProfibusMaster(config)
    try:
        with pytest.raises(ProfibusMasterError, match="diag_period"):
            await master.read_diagnosis(8)
        assert (await master.read_inputs(8))[0] == b"\xff\xff"
    finally:
        master.shutdown()


def test_observer_against_actual_upstream_codec():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            RUNTIME,
            str(root / "tests/pyprofibus_observer_checks.py"),
            str(root / "src/profibus_mcp/adapters/pyprofibus_worker.py"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert "exact forwarding passed" in result.stdout


async def test_real_upstream_over_mcp_stdio(config, tmp_path):
    env_file = tmp_path / "empty.env"
    env_file.touch()
    env = {key: value for key, value in os.environ.items() if not key.startswith("PROFIBUS_")}
    env.update(
        {
            "PROFIBUS_BACKEND": "pyprofibus",
            "PROFIBUS_BUS_ENABLED": "true",
            "PROFIBUS_WRITES_ENABLED": "true",
            "PROFIBUS_BAUDRATE": "19200",
            "PROFIBUS_TIMEOUT": "3000",
            "PROFIBUS_PYPROFIBUS_PHY": "dummy_slave",
            "PROFIBUS_PYPROFIBUS_PYTHON": RUNTIME,
            "PROFIBUS_PYPROFIBUS_CONFIG": str(config.pyprofibus_config),
            "PROFIBUS_PYPROFIBUS_OUTPUTS": str(config.pyprofibus_outputs),
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "profibus_mcp.cli", "--env-file", str(env_file)],
        env=env,
    )
    async with stdio_client(parameters) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        status = (await session.call_tool("get_master_status", {})).structuredContent["data"]
        assert status["worker_running"] is False
        assert status["capabilities"]["simulated"] is True
        target = {"target": "address", "slave_address": 8}
        sample = (await session.call_tool("read_inputs", target)).structuredContent
        assert sample["success"] is True
        result = (await session.call_tool("write_outputs", {**target, "data": [3, 4]})).structuredContent
        assert result["meta"]["outcome"] == "submitted"
        assert result["meta"]["physical_completion_verified"] is False
        diagnostic = (await session.call_tool("read_diagnosis", {"slave_address": 8})).structuredContent
        assert diagnostic["success"] is True and diagnostic["data"]["station_status"] == [0, 4, 0]
        assert diagnostic["meta"]["source"] == "observed_slave_diag"
        result = (await session.call_tool("scan_bus", {})).structuredContent
        assert result["success"] is False and result["meta"]["request_sent"] is False


@pytest.mark.parametrize("case", ["missing_image", "bad_image", "extra_image", "phy", "master", "baud", "watchdog"])
async def test_bad_commissioning_refused_before_opening_phy(config, case):
    if case in ("missing_image", "bad_image", "extra_image"):
        config.pyprofibus_outputs.write_text(
            {
                "missing_image": "{}",
                "bad_image": '{"8":[true,0]}',
                "extra_image": '{"8":[0,0],"9":[0,0]}',
            }[case]
        )
    elif case == "phy":
        # Physical serial is present only in the file; expected PHY remains dummy.
        text = config.pyprofibus_config.read_text().replace("type=dummy_slave", "type=serial")
        config.pyprofibus_config.write_text(text)
    elif case == "master":
        config = replace(config, master_address=3)
    elif case == "baud":
        config = replace(config, baudrate=9600)
    else:
        text = config.pyprofibus_config.read_text().replace("watchdog_ms=300", "watchdog_ms=0")
        config.pyprofibus_config.write_text(text)
    master = ProfibusMaster(config)
    try:
        with pytest.raises(ProfibusMasterError, match="initialization refused") as error:
            await master.read_inputs(8)
        assert error.value.meta["request_sent"] is False
        assert master.status()["fault"]
        assert master.status()["worker_running"] is False
    finally:
        master.shutdown()
