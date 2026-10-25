# PROFIBUS protocol assessment and implementation evidence

Research date: 2026-10-25. Baseline: `96bc401`. The legacy `serial` backend
is disabled; the selected optional `pyprofibus` backend now implements configured cyclic DP I/O; the TCP JSON backend exercises simulated DP-style
I/O and is not a PROFIBUS wire implementation.

## Primary sources

| Source | Version / sections inspected | Design implication |
| --- | --- | --- |
| [PI GSD library documentation](https://www.profibus.com/products/gsd-files) | Public online description, accessed 2026-10-25 | GSD describes device capabilities and diagnostics; parsing it does not communicate with a device. |
| [PI identity allocation](https://www.profibus.com/products/ident-numbers) | Public online documentation, accessed 2026-10-25 | Identity metadata supports engineering and must not be confused with a live liveness result. |
| [pyprofibus hardware guide](https://github.com/mbuesch/pyprofibus/blob/7b6fa3cea6edb162f5cc56c8afe6eae6e0ccda32/doc/hardware.rst) | Commit `7b6fa3c`; serial, USB, ESP32, FPGA and Windows sections | Hardware and scheduling capabilities constrain usable baud rates; USB serial is not a generic timing-safe master. |
| [pyprofibus ET200S example](https://github.com/mbuesch/pyprofibus/blob/7b6fa3cea6edb162f5cc56c8afe6eae6e0ccda32/examples/example_et200s.py) | Same commit; slave descriptions, parameter data and continuous `master.run()` | A live master needs configured slaves and a persistent cycle owner, not independent per-tool serial writes. |
| [pyprofibus project](https://github.com/mbuesch/pyprofibus) | README dependencies and license; accessed 2026-10-25 | A real open-source master alternative exists; Selected as a separate optional runtime; its GPL worker component is explicitly licensed and packaged. |

The full normative PROFIBUS standard was not provided. Public descriptions and
upstream implementation documentation inform the assessment; this is not a
protocol-conformance certification.

## Capability and gap matrix

| Canonical tool / workflow | Implemented behavior | Reliability requirement | Verification / status |
| --- | --- | --- | --- |
| `get_master_status` | Local settings plus machine-readable support/gates added | Serial explicitly unsupported; no port opened | MCP output-schema and installed package tests |
| `scan_bus` | Mock slave inventory | Reject invalid/duplicate station addresses and invalid image lengths | New malformed-response tests; live scan blocked |
| `read_inputs` | Mock or pyprofibus fresh cyclic-image read, direct or alias | Bound whole image to 244 bytes; correlate any echoed address; requested length must match | New oversized/misdirected-response tests; upstream dummy-PHY and real MCP session pass; hardware unverified |
| `write_outputs` | Gated mock output write or pyprofibus full-image update, direct or alias | Single send; wrong-address acknowledgement is unknown, never success | New real loopback reply test plus existing cancellation/retry tests; upstream process-image update tested; physical completion never implied |
| `read_diagnosis` | Mock status/identity or observed native Slave_Diag | Native master remains sole owner; fresh reception, bounded address/SAP/service correlation; raw extension preservation | Actual upstream codec/dummy and MCP tests; hardware unverified |
| `list_slaves` | Local configured aliases | Metadata only | Existing map tests |
| `load_gsd_file` | Local GSD parser | Bounded file, explicit missing/invalid metadata | Existing parser tests |
| DP master startup, parameterization, Check Configuration and continuous Data Exchange | Optional pyprofibus class-1 master worker | Exclusive cycle owner, explicit initial outputs, watchdog, bus gate, stop/latch on loss or cancellation | Actual upstream dummy-PHY state machine tested; physical commissioning remains |
| DP-V1 acyclic records, alarms, PA/PROFIdrive profiles | Not implemented | Profile-specific semantics and device documentation needed | Unsupported; not inferred from mock replies |

## Implemented changes

Local status now reports `capabilities` with canonical operations, backend,
`simulated`, `scope: "backend_support"`, `device_verified: false`, and prerequisites.
Each operation has separate `supported` and `enabled` fields. `enabled` only
reflects native configuration gates, not MCP policy, device permission or
reachability. A serial configuration advertises no bus-operation support even
when writes are enabled. Local metadata remains available.

Scan replies no longer accept impossible or duplicate addresses, or invalid
input/output lengths. Reads without a requested length still enforce the DP
payload limit. Optional echoed addresses must match the requested station for
reads, diagnosis and write acknowledgements. A wrong-address write reply yields
an unknown outcome with no replay. Omitted echoed addresses remain compatible
with existing test/custom backends; their absence is not claimed as identity
verification.

The application/domain/native split, canonical policy mapping, durable audit,
bounded admission and artifact-first verification inherited from OPCUA-MCP are
preserved. The changes strengthen actual behavior rather than rename modules.

## Selected live backend and remaining limitations

The selected implementation is the optional pyprofibus 1.13 class-1 master worker,
using the actual public `PbConf.makeDPM`, `addSlave`, `initialize`, `run`,
`setMasterOutData`, `getMasterInData` and connection-state APIs. The inspected
[master source](https://github.com/mbuesch/pyprofibus/blob/7b6fa3cea6edb162f5cc56c8afe6eae6e0ccda32/pyprofibus/dp_master.py)
shows that setting output data stages a future exchange; the MCP therefore
reports `submitted`, not acknowledged or physically completed.
The [dummy PHY source](https://github.com/mbuesch/pyprofibus/blob/7b6fa3cea6edb162f5cc56c8afe6eae6e0ccda32/pyprofibus/phy_dummy.py)
responds through the real FDL/DP telegram codec and is used by dedicated CI.

The native runtime is installed separately and pinned in CI. The worker component
is GPL-2.0-or-later with its full license included; the MCP host/pipe client remain
MIT. The worker owns the continuous cycle, required initial images and lifecycle.
No user selection of a software stack is outstanding. See [deployment, semantics
and limitations](pyprofibus-backend.md).

Broad discovery and DP-V1/profile operations remain unsupported;
they are not fabricated from connection-state booleans. Physical Linux serial or
FPGA operation requires the selected supported adapter, genuine slave GSD/module
configuration, verified timing/watchdog/output behavior and an isolated authorized
bus. Hardware validation is still outstanding; it does not block the implemented
software adapter or its upstream dummy-PHY tests. A proprietary card SDK was not
chosen because its hardware/API/license artifacts are unnecessary for this
available, locally verifiable software path.

## Verification

Baseline checks passed. The full verifier covers contract/source parity, locked
Python tests and formatting, the mock server suite, wheel/sdist and npm builds,
installed Python/npm MCP status/schema sessions and artifact metadata. New smoke
tests sanitize inherited protocol configuration and use only loopback mock
settings. No serial port or physical PROFIBUS bus was opened.

The separate pinned-runtime CI job additionally validates actual upstream DP state-machine I/O and real MCP stdio. This is software integration evidence using `dummy_slave`, not physical-bus verification.

## Second-pass diagnostic implementation and roadmap

The public [configuration factory](https://github.com/mbuesch/pyprofibus/blob/7b6fa3cea6edb162f5cc56c8afe6eae6e0ccda32/pyprofibus/conf.py)
accepts an existing PHY. The [PHY implementation](https://github.com/mbuesch/pyprofibus/blob/7b6fa3cea6edb162f5cc56c8afe6eae6e0ccda32/pyprofibus/phy.py)
queues one telegram per destination and paces at 11 bits/octet. The
[DP codec](https://github.com/mbuesch/pyprofibus/blob/7b6fa3cea6edb162f5cc56c8afe6eae6e0ccda32/pyprofibus/dp.py)
defines the six standard diagnostic bytes but discards extension bytes when
constructing its diagnostic object. The implemented transparent observer uses
these actual APIs, retains the original DU, and never adds a protocol sender or
reader. Native periodic requests, queue supersession and reception freshness are
covered by independent upstream-codec tests. See [exact diagnostic semantics and
policy migration](pyprofibus-backend.md#observed-native-diagnostics).

Prioritized remaining work:

1. Commission the selected physical PHY and representative slaves; test low/high
   baud timing, diagnostic overflow, bus loss, watchdog behavior and initial outputs.
2. Add bounded, GSD/profile-aware decoding of extended diagnostics only against
   documented device layouts; keep raw bytes available and unknown formats explicit.
3. Investigate input-only slaves and DP-V1 records/alarms against upstream support
   and concrete device profiles before expanding the current configured class-1 scope.
