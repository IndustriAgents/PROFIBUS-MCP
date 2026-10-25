# PROFIBUS protocol assessment and implementation evidence

Research date: 2026-10-25. Baseline: `96bc401`. The current live `serial` backend
is explicitly unimplemented; the TCP JSON backend exercises simulated DP-style
I/O and is not a PROFIBUS wire implementation.

## Primary sources

| Source | Version / sections inspected | Design implication |
| --- | --- | --- |
| [PI GSD library documentation](https://www.profibus.com/products/gsd-files) | Public online description, accessed 2026-10-25 | GSD describes device capabilities and diagnostics; parsing it does not communicate with a device. |
| [PI identity allocation](https://www.profibus.com/products/ident-numbers) | Public online documentation, accessed 2026-10-25 | Identity metadata supports engineering and must not be confused with a live liveness result. |
| [pyprofibus hardware guide](https://github.com/mbuesch/pyprofibus/blob/7b6fa3cea6edb162f5cc56c8afe6eae6e0ccda32/doc/hardware.rst) | Commit `7b6fa3c`; serial, USB, ESP32, FPGA and Windows sections | Hardware and scheduling capabilities constrain usable baud rates; USB serial is not a generic timing-safe master. |
| [pyprofibus ET200S example](https://github.com/mbuesch/pyprofibus/blob/7b6fa3cea6edb162f5cc56c8afe6eae6e0ccda32/examples/example_et200s.py) | Same commit; slave descriptions, parameter data and continuous `master.run()` | A live master needs configured slaves and a persistent cycle owner, not independent per-tool serial writes. |
| [pyprofibus project](https://github.com/mbuesch/pyprofibus) | README dependencies and license; accessed 2026-10-25 | A real open-source master alternative exists; adopting it needs a deliberate dependency/distribution and integration decision. |

The full normative PROFIBUS standard was not provided. Public descriptions and
upstream implementation documentation inform the assessment; this is not a
protocol-conformance certification.

## Capability and gap matrix

| Canonical tool / workflow | Implemented behavior | Reliability requirement | Verification / status |
| --- | --- | --- | --- |
| `get_master_status` | Local settings plus machine-readable support/gates added | Serial explicitly unsupported; no port opened | MCP output-schema and installed package tests |
| `scan_bus` | Mock slave inventory | Reject invalid/duplicate station addresses and invalid image lengths | New malformed-response tests; live scan blocked |
| `read_inputs` | Mock cyclic-image read, direct or alias | Bound whole image to 244 bytes; correlate any echoed address; requested length must match | New oversized/misdirected-response tests; live read blocked |
| `write_outputs` | Gated mock output write, direct or alias | Single send; wrong-address acknowledgement is unknown, never success | New real loopback reply test plus existing cancellation/retry tests; live write blocked |
| `read_diagnosis` | Mock status/identity | Correlate any echoed station; do not attribute another slave's status | New correlation tests; live diagnosis blocked |
| `list_slaves` | Local configured aliases | Metadata only | Existing map tests |
| `load_gsd_file` | Local GSD parser | Bounded file, explicit missing/invalid metadata | Existing parser tests |
| DP master startup, parameterization, Check Configuration and continuous Data Exchange | Not implemented in this MCP | Exclusive bus owner, configured safe outputs, watchdog and cancellation semantics | External stack/hardware/configuration decision required |
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

## Live-backend decision and concrete blockers

`pyprofibus` is a technically plausible integration option, including dummy and
real-device examples. It is not the existing pyserial dependency with a different
connection string. Integration requires a selected PHY/driver, master address,
per-slave GSD/module/parameter configuration, cycle ownership, shutdown behavior
and safe initial output image. The repository currently has none of that live
configuration model. Its MIT distribution also needs an explicit project decision
before incorporating a GPL master dependency; this document does not give a
legal compatibility opinion.

A vendor DP master/card SDK is another option, but no supported card, vendor SDK,
firmware/API version or license material was supplied. Implementing speculative
function signatures would create false hardware support. The serial backend
continues to refuse requests before opening the adapter.

To complete a live implementation, provide/select the intended master stack and
hardware, target operating system, slave GSD/module configuration, and an
isolated authorized bus. Then implement its persistent cycle adapter and verify
parameterization, slave diagnostics, watchdog/output behavior and loss recovery
against that stack. This is a visible unfinished capability, not a passed mock test.

## Verification

Baseline checks passed. The full verifier covers contract/source parity, locked
Python tests and formatting, the mock server suite, wheel/sdist and npm builds,
installed Python/npm MCP status/schema sessions and artifact metadata. New smoke
tests sanitize inherited protocol configuration and use only loopback mock
settings. No serial port or physical PROFIBUS bus was opened.
