# Optional pyprofibus DP master

The selected software backend is pyprofibus 1.13, tested from official commit
[`7b6fa3cea6edb162f5cc56c8afe6eae6e0ccda32`](https://github.com/mbuesch/pyprofibus/tree/7b6fa3cea6edb162f5cc56c8afe6eae6e0ccda32).
It implements a class-1 master with one persistent cycle owner in a separate
process. `serial` remains the previous disabled compatibility backend;
`pyprofibus` is the opt-in implementation. It is not a hard real-time guarantee,
PI certification, or evidence that a particular adapter/device was tested.

## Installation and licensing

The host and pipe adapter remain MIT. The separately marked worker file
`profibus-python/src/profibus_mcp/adapters/pyprofibus_worker.py` is GPL-2.0-or-later;
the complete GPL notice ships as `profibus-python/LICENSE.pyprofibus-worker`.
Package metadata lists both component licenses. Upstream pyprofibus is not
vendored, automatically downloaded or included in the wheel/npm package. Install
it separately under its GPL-2.0-or-later terms. Process isolation describes the
runtime boundary; it is not a claim that license obligations disappear.

```sh
uv venv --python 3.11 /opt/pyprofibus-runtime
PYPROFIBUS_CYTHON_BUILD=0 uv pip install --python /opt/pyprofibus-runtime pyserial==3.5 \
  'pyprofibus @ git+https://github.com/mbuesch/pyprofibus.git@7b6fa3cea6edb162f5cc56c8afe6eae6e0ccda32'
```

The adapter requires POSIX pipes. The selected deployment for physical use is
Linux with a supported native UART/RS-485 PHY and commissioned baud rate. The
upstream FPGA PHY is also selectable when its SPI implementation and board are
available. USB serial is not the recommended deployment because upstream warns
about its timing. This repository does not ship or flash FPGA firmware.

## Software-only example

From this repository, with absolute paths substituted:

```sh
PROFIBUS_BACKEND=pyprofibus \
PROFIBUS_PYPROFIBUS_PYTHON=/opt/pyprofibus-runtime/bin/python \
PROFIBUS_PYPROFIBUS_CONFIG=/absolute/repo/examples/pyprofibus/dummy.conf \
PROFIBUS_PYPROFIBUS_OUTPUTS=/absolute/repo/examples/pyprofibus/outputs.json \
PROFIBUS_PYPROFIBUS_PHY=dummy_slave \
PROFIBUS_MASTER_ADDRESS=2 PROFIBUS_BAUDRATE=19200 PROFIBUS_TIMEOUT=3000 \
PROFIBUS_BUS_ENABLED=true PROFIBUS_WRITES_ENABLED=false \
uv run --directory profibus-python profibus-mcp
```

This exercises upstream's real DP state machine against its dummy PHY: output
bytes are inverted in the simulated input telegram. Capabilities explicitly mark
this configuration `simulated: true`. `get_master_status` does not start a worker
or communicate. The first supported I/O call lazily starts the configured master.

For hardware, replace the example with the device vendor's real GSDs, exact module
configuration and operator-selected initial images. Never deploy the dummy GSD
onto a physical bus. Paths in the pyprofibus configuration resolve relative to
that configuration's directory. GSD/configuration files are trusted local
engineering inputs, not an MCP tool upload or arbitrary remote source.

## Policy compatibility change

`read_inputs` and `read_diagnosis` advertise `readOnlyHint=false` and `destructiveHint=true` because
starting the enabled pyprofibus master can parameterize slaves and transmit the
initial output images. MCP `read_only` policy therefore denies canonical address,
canonical alias and legacy read/diagnosis entry points (including
`test_slave_communication`) in **every backend**, including mock.
Use an explicit non-read-only policy/allowlist for this workflow after reviewing
its startup effects. `PROFIBUS_BUS_ENABLED` remains a separate required operator
gate; changing annotations does not bypass it. Local status remains read-only.

## Gates, images and outcomes

- `PROFIBUS_BUS_ENABLED` defaults to false. Enabling it permits master startup,
  parameterization, Check Configuration and continuous exchange of the explicitly
  supplied initial output images, even when tool writes are disabled.
- `PROFIBUS_WRITES_ENABLED` separately permits subsequent `write_outputs` calls.
  Neither setting overrides MCP policy or grants device permission.
- `PROFIBUS_PYPROFIBUS_CONFIG` and `PROFIBUS_PYPROFIBUS_OUTPUTS` are required.
  The latter is a JSON object from decimal station-address strings to byte lists.
  Every configured slave needs an exact image, with no extra station entries.
- `PROFIBUS_PYPROFIBUS_PHY` must match the file exactly: `serial`, `fpga`, or
  `dummy_slave`. Master address and baud must match the environment. Class 1,
  unique station addresses 0..125, a distinct master and enabled watchdogs are
  checked before opening the PHY. The supported profile requires 1..244 master
  output bytes and 0..244 master input bytes per slave. Pure input-only slaves
  with zero master output bytes are currently unsupported by this adapter.
- Upstream `input_size` means bytes the **slave receives** (master output);
  `output_size` means bytes the **slave sends** (master input). The same perspective
  is used by the example and configuration validation.
- Reads return an input sample received after the request was accepted; no stale
  cached sample substitutes for a missing reply. Length may select a prefix of
  the configured image. No atomic cross-slave snapshot is promised.
- Writes replace the complete configured output image once. Successful results
  have `meta.outcome="submitted"`, `confirmation="process_image_accepted"` and
  `physical_completion_verified=false`. This is acceptance by the worker's local
  process image, before a confirmed DP exchange; it is not a slave acknowledgement
  or proof of physical effect. The normal cyclic repetition of a held image is
  native DP operation, not a retry of the MCP request.
- Partial write lengths and unknown addresses are definite local rejections.
  After a pipe send, malformed/mismatched replies, timeout or cancellation have an
  unknown update outcome. The worker is terminated and reaped, and its fault is
  latched. No request is retried and no worker is automatically restarted.
  `request_sent` describes the requested image operation; a failed initial call
  can separately report `bus_startup_may_have_occurred` because startup itself
  configures/exchanges the authorized initial images.
- An established slave leaving Data_Exchange stops the worker before reconnection
  can replay its held image. Restart requires explicit server restart and renewed
  commissioning judgment. Shutdown/EOF stops the owned worker. Hardware watchdog
  and safe-output behavior must be tested on the selected slaves; process
  termination alone does not prove the equipment reached a safe physical state.

## Observed native diagnostics

`read_diagnosis` waits for a standard Slave_Diag reply received after admission.
Every configured slave used for diagnosis needs `diag_period > 0`; choose a tool
budget sufficient for that native period and bus cycle. The tool does not change
this period or transmit an additional diagnostic request. A transparent PHY tap,
injected through upstream's public `makeDPM(phy=...)`, forwards every send and
poll unchanged. The native master remains the sole protocol and cycle owner.

The tap correlates slave/master addresses, SAPs and service with a successful
native diagnostic **submission**. A submission can be queued; it does not prove
on-wire dispatch. Failed submissions, same-station replacements and queue clears
invalidate correlation. CRC, exact telegram length, response control and reserved
standard-status bits are checked. There is no DP diagnostic transaction ID, so
this correlation cannot prove unique request identity. The window is bounded by
`0.25 + 11 * 510 * (configured_slave_count + 1) / baudrate` seconds: upstream uses
11 bits per octet and at most one queued request per station; 510 bounds one
maximum request/reply pair, with 250 ms scheduling margin. This supports slow
baud rates without pretending the window is a delivery guarantee. The existing
tool deadline independently bounds waiting and stops/reaps the worker on expiry.

Results include the three standard status bytes, actual/expected ident number,
actual/expected master address, explicit match flags, raw complete diagnostic DU,
raw extended bytes, overflow flag and monotonic sample sequence. An unassigned
master (255) or mismatch is explicit; `no_standard_fault_flags` only describes
these bytes, not device readiness. Extended device/module/channel diagnostic
contents are not interpreted. Overflow reports `meta.complete=false`.
`meta.fresh_sample` and `received_age_ms` refer to reception after admission;
`acquisition_timestamp_available=false` prevents a sensor-time claim. The dummy
PHY actually returns ident 0 and master 255, so its diagnosis truthfully reports
identity mismatch against the example GSD, even though simulated cyclic I/O works.

`scan_bus`, DP-V1 records/alarms and profile-specific commands remain unsupported.
Local aliases/GSD tools remain available. Local status shows configured stations
only after initialization, without claiming reachability.

## Verification and remaining commissioning

`PROFIBUS_TEST_PYTHON=/opt/pyprofibus-runtime/bin/python uv run --directory profibus-python pytest -q`
runs the normal suite plus the actual upstream dummy-PHY tests. Without this
optional runtime, those tests are explicitly skipped. The dedicated CI job
installs the pinned source and runs them, including the real MCP stdio session.

Local tests cover actual state-machine/telegram I/O, invalid initial images,
wrong PHY/master/baud, watchdog validation, gates, rejected updates, response
correlation, timeout/cancel after dispatch, native lease ownership, fault latching,
worker reaping, typed MCP outputs and simulated capability reporting. No test
opens a serial device or FPGA interface.

Physical commissioning still requires the selected Linux adapter and device
GSD/module configuration, tested cycle timing at the chosen baud, physical
watchdog/output behavior, unplug/loss/recovery tests and an authorized isolated
bus. The software backend is implemented; those hardware results are not claimed.
