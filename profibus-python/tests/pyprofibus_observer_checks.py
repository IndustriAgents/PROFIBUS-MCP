"""Invoked only in the isolated actual pyprofibus interpreter; no real PHY."""

import importlib.util
import sys
from collections import deque

from pyprofibus.dp import DpTelegram, DpTelegram_SlaveDiag_Con, DpTelegram_SlaveDiag_Req
from pyprofibus.fdl import FdlTelegram_var

spec = importlib.util.spec_from_file_location("worker", sys.argv[1])
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class Phy:
    def __init__(self):
        self.frames = deque()
        self.polls = 0
        self.sends = []
        self.fail_send = False
        self.clears = []

    def poll(self, *args, **kwargs):
        self.polls += 1
        assert args == (0.001,) and kwargs == {}
        return self.frames.popleft()

    def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        if self.fail_send:
            raise OSError("native send failed")
        return "forwarded"

    def clearTxQueueAddr(self, da):
        self.clears.append(da)
        return "cleared"


def frame(*, source=8, destination=2, control=8, service=None, payload=None):
    diag = DpTelegram_SlaveDiag_Con(da=destination, sa=source, fc=control)
    diag.b1 = 4
    diag.masterAddr = 2
    diag.identNumber = 0x1234
    original = diag.toFdlTelegram()
    return FdlTelegram_var(
        da=original.da,
        sa=original.sa,
        fc=control,
        dae=original.dae,
        sae=original.sae if service is None else bytes([service]),
        du=original.du if payload is None else payload,
    ).getRawData()


def exercise(raw, *, request=True, expired=False):
    phy = Phy()
    observer = worker.DiagnosticObserver(phy, 2, {8: 0x1234}, 9600)
    if request:
        telegram = DpTelegram_SlaveDiag_Req(da=8, sa=2).toFdlTelegram()
        assert observer.send(telegram, True, maxReplyLen=255) == "forwarded"
        assert phy.sends == [((telegram, True), {"maxReplyLen": 255})]
        if expired:
            observer.requests[8] -= observer.correlation_window_s + 1
    phy.frames.append(raw)
    assert observer.poll(0.001) is raw  # exact object, no consumed or replaced reply
    assert phy.polls == 1 and len(phy.sends) == int(request)
    return observer, phy


normal = frame()
observer, phy = exercise(normal)
assert observer.samples[8][2]["status"] == "no_standard_fault_flags"
assert observer.samples[8][2]["station_status"] == [0, 4, 0]
assert observer.samples[8][2]["ident_number"] == "0x1234"
phy.frames.append(normal)
observer.poll(0.001)
assert observer.sequence == 1  # no second result without a second native request
assert len(phy.sends) == 1 and phy.polls == 2

extended = bytes([8, 4, 128, 2, 0x12, 0x34, 2, 0x55])
observer, _ = exercise(frame(payload=extended))
assert observer.samples[8][2]["raw_data_hex"] == extended.hex()
assert observer.samples[8][2]["extended_diagnostics_hex"] == "0255"
assert observer.samples[8][2]["diagnostics_overflow"] is True

corrupt = bytearray(normal)
corrupt[-2] ^= 1
bad = [
    corrupt,
    normal + b"\x00",
    frame(source=9),
    frame(destination=3),
    frame(control=0x48),
    frame(control=0x18),
    frame(service=DpTelegram.DSAP_GET_CFG),
    frame(payload=bytes(5)),
    frame(payload=bytes([0, 0, 0, 2, 0x12, 0x34])),
    frame(payload=bytes([0, 0x44, 0, 2, 0x12, 0x34])),
]
for raw in bad:
    assert exercise(raw)[0].samples == {}
assert exercise(normal, request=False)[0].samples == {}
assert exercise(normal, expired=True)[0].samples == {}
# Low-baud maximum replies can take ~0.29 seconds before queueing.
observer, phy = exercise(normal)
request = DpTelegram_SlaveDiag_Req(da=8, sa=2).toFdlTelegram()
observer.send(request, True)
observer.requests[8] -= 0.3
phy.frames.append(frame(payload=bytes([0, 4, 0, 2, 0x12, 0x34]) + bytes(238)))
observer.poll(0.001)
assert observer.sequence == 2 and len(observer.samples[8][2]["raw_data_hex"]) == 488

for master, status in ((255, "master_unassigned"), (3, "master_mismatch")):
    observed, _ = exercise(frame(payload=bytes([0, 4, 0, master, 0x12, 0x34])))
    assert observed.samples[8][2]["status"] == status
    assert observed.samples[8][2]["master_match"] is False
    assert observed.samples[8][2]["expected_master_address"] == 2

# Queue replacement, clearing and failed native submission invalidate correlation.
for action in ("replace", "clear", "fail"):
    observer.send(request, True)
    if action == "replace":
        replacement = FdlTelegram_var(da=8, sa=2, fc=0x48, dae=b"", sae=b"", du=b"x")
        observer.send(replacement, True)
    elif action == "clear":
        assert observer.clearTxQueueAddr(8) == "cleared"
        assert phy.clears == [8]
    else:
        phy.fail_send = True
        try:
            observer.send(request, True)
        except OSError:
            pass
        else:
            raise AssertionError("native send error was swallowed")
        phy.fail_send = False
    assert observer.requests == {}
    phy.frames.append(normal)
    observer.poll(0.001)
    assert observer.sequence == 2
print("observer correlation, malformed frames, extensions and exact forwarding passed")
