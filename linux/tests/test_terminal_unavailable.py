#!/usr/bin/env python3
"""COMMIT-T0 (docs/SECURITY_REMEDIATION_PLAN.md, section 14): the terminal is UNAVAILABLE.

Every `pc.term_open` / `pc.term_input` / `pc.term_resize` / `pc.term_close` is refused by `PcControl._refuse_terminal`,
registered outside `_gated`, in every grant state: no approval prompt, no TerminalSession, no pty/fork primitive,
exactly one `access.denied` audit record and exactly one `pc.status` reply per request.

The test server has no ledger, so `server.audit` is replaced by a recorder. Ports 28200/28201 are used by no other test.
Run directly: `python3 tests/test_terminal_unavailable.py`"""
import asyncio
import json
import os
import pty
import re
import ssl
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from phonebridge import crypto, pairing, pc_info, tls_identity, transport  # noqa: E402
from phonebridge.pc_control import PcControl  # noqa: E402
from phonebridge.protocol import Envelope  # noqa: E402
from phonebridge.security import permissions as P  # noqa: E402
from _phone import new_identity, phone_connect  # noqa: E402

PASS = FAIL = 0
PORT, DISCOVERY = 28200, 28201
TYPES = ("pc.term_open", "pc.term_input", "pc.term_resize", "pc.term_close")
REPLY = {"denied": True, "message": "The terminal is not available"}
FP = "cafe"
QUIET = 0.5


def check(label, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok ' if cond else 'FAIL'} {label}")


class Client:
    """A fake phone connection."""

    def __init__(self, reader, writer, cipher):
        self.reader, self.writer, self.cipher, self.seq = reader, writer, cipher, 0

    async def send_raw(self, obj: dict) -> None:
        self.writer.write(crypto.pack_frame(self.cipher.encrypt(json.dumps(obj, separators=(",", ":")).encode())))
        await self.writer.drain()

    async def send(self, type_: str, payload=None, **extra) -> None:
        self.seq += 1
        await self.send_raw({"ch": "control", "type": type_, "seq": self.seq, "payload": payload, **extra})

    async def collect(self, quiet=QUIET) -> list:
        """Every envelope that arrives until nothing has arrived for `quiet` seconds."""
        out = []
        while True:
            try:
                n = crypto.unpack_frame_len(await asyncio.wait_for(self.reader.readexactly(4), quiet))
                out.append(Envelope.from_json(self.cipher.decrypt(await self.reader.readexactly(n))))
            except asyncio.TimeoutError:
                return out

    def close(self):
        self.writer.close()


class Rig:
    async def start(self):
        self.tmp = Path(tempfile.mkdtemp())
        pairing.DEVICES_DIR = self.tmp / "devices"
        tls_identity.CONFIG_DIR, tls_identity.CERT_PATH, tls_identity.KEY_PATH = self.tmp, self.tmp / "c.pem", self.tmp / "k.pem"
        transport.DISCOVERY_PORT = DISCOVERY
        pc_info.HOME = self.tmp / "home"
        self.ident = new_identity()
        self.phone = pairing.PairedDevice(FP, "Test Phone", os.urandom(32), identity_pub=self.ident.public_b64)
        self.phone.save()
        self.server = transport.ControlChannelServer(port=PORT)
        await self.server.start()
        self.events = []
        self.server.audit = lambda event, device=None, **meta: self.events.append((event, device, meta))
        self.pc = PcControl(self.server, asyncio.get_running_loop(), clipboard_text=lambda: "")
        self.prompts = []

        async def never(device):
            self.prompts.append(device.name)
            return 15

        self.pc.approval_callback = never
        self.serve = asyncio.ensure_future(self.server.serve_forever())
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ctx.check_hostname, self.ctx.verify_mode = False, ssl.CERT_NONE

    async def connect(self) -> Client:
        reader, writer = await asyncio.open_connection("127.0.0.1", PORT, ssl=self.ctx)
        session = await phone_connect(reader, writer, FP, self.phone.session_key, self.ident, self.server.cert_fingerprint)
        c = Client(reader, writer, session.cipher)
        await c.collect(0.3)           # handshake chatter
        self.events.clear()
        return c

    def set_state(self, state: str):
        """no_grant | denied | approved | expired"""
        d = pairing.PairedDevice.load(FP)
        d.permissions = {k: v for k, v in d.permissions.items() if k != "remote_control"}
        if state == "no_grant":
            d.allow_pc_control, d.pc_control_until = False, None
        elif state == "denied":
            d.allow_pc_control, d.pc_control_until = False, None
            d.permissions["remote_control"] = P.DENY
        elif state == "approved":
            d.allow_pc_control, d.pc_control_until = True, time.time() + 3600
        elif state == "expired":
            d.allow_pc_control, d.pc_control_until = True, time.time() - 5
        else:
            raise ValueError(state)
        d.save()
        self.pc._gate_cache.clear()

    def denials(self):
        return [e for e in self.events if e[0] == "access.denied"]

    @staticmethod
    def is_expiry_sweep_record(e) -> bool:
        """The record the (unchanged, still running) 5-second expiry sweep writes when it clears an expired grant. It is
        unrelated to terminal requests and may land in any window that contains an expired grant, so it is the ONE event
        the terminal assertions tolerate."""
        return e[0] == "permission.changed" and e[2].get("action") == "pc_control.expired" and e[2].get("feature") == "remote_control"

    MAX_SWEEP_RECORDS = 1     # one expired grant is cleared once; a window with one expired grant holds at most one record

    @classmethod
    def others_of(cls, events) -> list:
        """Every audit record that is neither a terminal denial nor the (single) expiry-sweep record: a prompt request, an
        approval, a refusal, a revocation, a foreign event, a sweep-style record with another feature or action, or a
        SECOND matching sweep record (anything beyond MAX_SWEEP_RECORDS). Must stay empty."""
        sweeps = [e for e in events if cls.is_expiry_sweep_record(e)]
        rest = [e for e in events if e[0] != "access.denied" and not cls.is_expiry_sweep_record(e)]
        return rest + sweeps[cls.MAX_SWEEP_RECORDS:]

    @staticmethod
    def terminal_problems(events, requested) -> list:
        """Checks the access.denied records ONE BY ONE against the terminal requests that were sent (`requested`, a list of
        message types): the same number of records; each recorded for this device with exactly the keys
        {feature, message, mode}, feature "terminal" and mode "unavailable"; and the recorded message types equal the
        requested ones as a multiset (so an unrelated denial cannot stand in for a terminal one, and nothing is missing or
        duplicated). Returns the list of problems (empty = correct)."""
        den = [e for e in events if e[0] == "access.denied"]
        probs = []
        if len(den) != len(requested):
            probs.append(f"{len(den)} access.denied records for {len(requested)} requests")
        for e in den:
            m = e[2]
            if e[1] != FP:
                probs.append(f"denial for device {e[1]!r}")
            if set(m) != {"feature", "message", "mode"}:
                probs.append(f"denial fields {sorted(m)}")
            if m.get("feature") != "terminal":
                probs.append(f"denial feature {m.get('feature')!r}")
            if m.get("mode") != "unavailable":
                probs.append(f"denial mode {m.get('mode')!r}")
        if sorted(str(e[2].get("message")) for e in den) != sorted(requested):
            probs.append("recorded message types differ from the requested types")
        return probs

    @classmethod
    def selftest_validators(cls) -> bool:
        """The validators above must reject every wrong event set (run on synthetic events, no server involved)."""
        def den(t, feature="terminal", mode="unavailable", device=FP):
            return ("access.denied", device, {"feature": feature, "message": t, "mode": mode})
        sweep = ("permission.changed", FP, {"feature": "remote_control", "action": "pc_control.expired"})
        good = [den(t) for t in TYPES]
        wrong_sweep_feature = ("permission.changed", FP, {"feature": "files", "action": "pc_control.expired"})
        wrong_sweep_action = ("permission.changed", FP, {"feature": "remote_control", "action": "pc_control.revoked"})
        approved = ("permission.changed", FP, {"feature": "remote_control", "action": "pc_control.approved"})
        cases = [
            ("correct set accepted", not cls.terminal_problems(good, TYPES), True),
            ("one terminal denial replaced by an unrelated denial (feature files)", bool(cls.terminal_problems(good[:3] + [den("pc.term_close", feature="files")], TYPES)), True),
            ("one terminal denial replaced by an unrelated denial (message of another request)", bool(cls.terminal_problems(good[:3] + [den("files.get", feature="files", mode="ask")], TYPES)), True),
            ("one denial with the wrong mode", bool(cls.terminal_problems(good[:3] + [den("pc.term_close", mode="denied")], TYPES)), True),
            ("a duplicate terminal denial (one type twice, one missing)", bool(cls.terminal_problems(good[:3] + [den("pc.term_input")], TYPES)), True),
            ("an extra terminal denial", bool(cls.terminal_problems(good + [den("pc.term_open")], TYPES)), True),
            ("a missing terminal denial", bool(cls.terminal_problems(good[:3], TYPES)), True),
            ("a denial recorded for another device", bool(cls.terminal_problems(good[:3] + [den("pc.term_close", device="beef")], TYPES)), True),
            ("one legitimate sweep record is tolerated", cls.others_of(good + [sweep]) == [], True),
            ("two legitimate sweep records are rejected", len(cls.others_of(good + [sweep, sweep])) == 1, True),
            ("a sweep record with the wrong feature is rejected", cls.others_of(good + [wrong_sweep_feature]) == [wrong_sweep_feature], True),
            ("a sweep record with the wrong action is rejected", cls.others_of(good + [wrong_sweep_action]) == [wrong_sweep_action], True),
            ("an approval event is rejected", cls.others_of(good + [approved]) == [approved], True),
            ("one sweep record plus a wrong-feature record: only the wrong one is reported", cls.others_of(good + [sweep, wrong_sweep_feature]) == [wrong_sweep_feature], True),
        ]
        bad = [label for label, got, want in cases if got != want]
        if bad:
            print("       validator self-test failed:", bad)
        return not bad

    def others(self) -> list:
        return self.others_of(self.events)

    def clean(self, n_requests: int) -> bool:
        """Exactly n access.denied records (each feature=terminal), no other record except the expiry-sweep record, no
        prompt, approval state untouched."""
        return (len(self.denials()) == n_requests
                and all(e[2].get("feature") == "terminal" and e[2].get("mode") == "unavailable"
                        and set(e[2]) == {"feature", "message", "mode"} and e[2].get("message") in TYPES for e in self.denials())
                and not self.others() and not self.prompts and not self.pc._approval_cooldown)

    async def refuse(self, c: Client, type_: str, payload=None, **extra) -> list:
        await c.send(type_, payload if payload is not None else {}, **extra)
        return await c.collect()


STATES = ("no_grant", "denied", "approved", "expired")


async def run() -> int:
    r = Rig()
    await r.start()
    pc = r.pc

    print("1-5. one request of each type in every grant state")
    results = {}
    for state in STATES:
        r.set_state(state)
        c = await r.connect()
        replies = []
        for t in TYPES:
            replies += await r.refuse(c, t, {"cols": 80, "rows": 24, "data": "id\r"})
        results[state] = (list(replies), list(r.events), list(r.prompts), dict(pc._approval_cooldown))
        ok = (len(replies) == 4 and all(x.type == "pc.status" and x.payload == REPLY for x in replies)
              and not r.terminal_problems(r.events, list(TYPES)) and not r.others() and not r.prompts and not pc._approval_cooldown)
        label = {"no_grant": "test_terminal_refused_no_grant", "denied": "test_terminal_refused_denied_grant",
                 "approved": "test_terminal_refused_approved_grant", "expired": "test_terminal_refused_expired_grant"}[state]
        if state == "denied":      # the reply to a *non-terminal* request would also have to stay untouched; here only terminal
            pass
        results[state] += (ok,)
        if state == "expired":
            # Deterministic proof that the tolerance is exact: run the REAL sweep right now (no await between setting the
            # state and the call, so no timer tick can interleave), then refuse one more request on the same connection.
            # (the timer sweep may already have written its single record in the window above: drop it, keep the denials,
            # so the forced sweep below produces exactly the one record this step then counts)
            r.events[:] = [e for e in r.events if not r.is_expiry_sweep_record(e)]
            r.set_state("expired")
            before = len(r.events)
            pc._sweep_expired()
            noise = [e for e in r.events[before:] if r.is_expiry_sweep_record(e)]
            more = await r.refuse(c, "pc.term_open", {"new": True})
            foreign = list(r.events) + [("permission.changed", FP, {"feature": "remote_control", "action": "pc_control.requested"}),
                                        ("permission.changed", FP, {"feature": "remote_control", "action": "pc_control.approved"})]
            sweep_ok = (len(noise) == 1 and len(more) == 1 and more[0].payload == REPLY
                        and not r.terminal_problems(r.events, list(TYPES) + ["pc.term_open"])
                        and not r.others() and len(Rig.others_of(foreign)) == 2)
            ok = ok and sweep_ok
        if state not in ("approved",):
            check(f"{label}: 4 types refused, 4 access.denied records, no prompt", ok)
        else:
            check(f"{label}: 4 types refused, 4 access.denied records, no prompt, grant untouched",
                  ok and pairing.PairedDevice.load(FP).pc_control_active())
        c.close()
        await asyncio.sleep(0.2)
        r.events.clear()

    print("3. pending approval")
    r.set_state("no_grant")
    pc._approval_pending.add(FP)
    c = await r.connect()
    before = (set(pc._approval_pending), dict(pc._approval_cooldown))
    replies = await r.refuse(c, "pc.term_open", {"cols": 80, "rows": 24})
    check("test_terminal_refused_during_pending_approval: refused, pending prompt untouched, no second prompt",
          len(replies) == 1 and replies[0].payload == REPLY and r.clean(1)
          and (set(pc._approval_pending), dict(pc._approval_cooldown)) == before == ({FP}, {}))
    pc._approval_pending.discard(FP)
    c.close()
    await asyncio.sleep(0.2)

    print("6. reconnection")
    r.set_state("approved")
    outcomes = []
    for _ in range(2):
        c = await r.connect()
        replies = await r.refuse(c, "pc.term_open", {"new": True})
        outcomes.append((len(replies), replies[0].payload if replies else None, len(r.denials()), len(r.others())))
        c.close()
        await asyncio.sleep(0.3)
        r.events.clear()
    check("test_terminal_refused_after_reconnection: identical refusal on a new connection, no carried-over state",
          outcomes[0] == outcomes[1] == (1, REPLY, 1, 0) and not pc._approval_pending and not r.prompts)

    print("7. duplicates")
    r.set_state("no_grant")
    c = await r.connect()
    for i in range(50):
        await c.send(TYPES[i % 4], {"cols": 80, "rows": 24})
    replies = await c.collect(1.0)
    check("test_terminal_duplicate_requests_each_refused_once: 50 requests, 50 replies, 50 records, no prompt",
          len(replies) == 50 and all(x.payload == REPLY for x in replies) and r.clean(50))
    r.events.clear()

    print("8. malformed payloads")
    big = "A" * (4 * 1024 * 1024)
    payloads = [None, [], [1, 2], "x", 5, big, {"cols": -5, "rows": -1}, {"cols": 10 ** 30, "rows": 10 ** 30},
                {"cols": "wide"}, {"new": "yes"}, {"new": [1]}, {}, {"data": 7}, {"data": None}]
    for i, p in enumerate(payloads):
        await c.send(TYPES[i % 4], p)
    replies = await c.collect(1.5)
    await c.send("pc.term_open", {})
    still = await c.collect()
    check("test_terminal_malformed_payloads_refused: every malformed payload refused, handler never raises, session survives",
          len(replies) == len(payloads) and all(x.payload == REPLY for x in replies)
          and len(still) == 1 and len(r.denials()) == len(payloads) + 1 and not r.prompts)
    r.events.clear()

    print("15. envelope variants")
    await c.send_raw({"ch": "control", "type": "pc.term_open", "seq": c.seq + 1, "payload": {}})
    c.seq += 1
    await c.send("pc.term_input", {"data": "x"}, req="AAAAAAAAAAAAAAAAAAAAAA")
    await c.send("pc.term_resize", {"cols": 1, "rows": 1}, re="BBBBBBBBBBBBBBBBBBBBBB")
    await c.send("pc.term_close", {}, req="CCCCCCCCCCCCCCCCCCCCCC", extra_field=[1])
    replies = await c.collect()
    check("test_terminal_envelope_variants_refused: v4-shaped and v5-shaped envelopes (req, re, extra keys) take the same path",
          len(replies) == 4 and all(x.payload == REPLY for x in replies) and r.clean(4))
    r.events.clear()
    c.close()
    await asyncio.sleep(0.2)

    print("9, 11, 12. no approval workflow, no TerminalSession, no pty/fork primitive in any state")
    from phonebridge import terminal as terminal_mod

    called = []

    def boom(name):
        def f(*a, **k):
            called.append(name)
            raise AssertionError(name + " must never be called")
        return f

    saved = (pc._maybe_request_approval, pc._run_approval, terminal_mod.TerminalSession.__init__, pty.fork, pty.openpty,
             os.fork, os.forkpty, os.execvpe)
    pc._maybe_request_approval, pc._run_approval = boom("_maybe_request_approval"), boom("_run_approval")
    terminal_mod.TerminalSession.__init__ = boom("TerminalSession.__init__")
    pty.fork, pty.openpty, os.fork, os.forkpty, os.execvpe = (boom("pty.fork"), boom("pty.openpty"), boom("os.fork"),
                                                              boom("os.forkpty"), boom("os.execvpe"))
    try:
        replies_all = []
        for state in STATES:
            r.set_state(state)
            c = await r.connect()
            for t in TYPES:
                replies_all += await r.refuse(c, t, {"cols": 80, "rows": 24, "new": True})
            c.close()
            await asyncio.sleep(0.2)
    finally:
        (pc._maybe_request_approval, pc._run_approval, terminal_mod.TerminalSession.__init__, pty.fork, pty.openpty,
         os.fork, os.forkpty, os.execvpe) = saved
    ok = len(replies_all) == 16 and all(x.payload == REPLY for x in replies_all)
    check("test_terminal_never_requests_approval: _maybe_request_approval and _run_approval patched to raise, never called",
          ok and not any(n.startswith("_") for n in called) and not r.prompts)
    check("test_terminal_never_constructs_session: TerminalSession.__init__ patched to raise, never called, all states",
          ok and "TerminalSession.__init__" not in called)
    check("test_terminal_primitives_never_called: pty.fork, pty.openpty, os.fork, os.forkpty, os.execvpe never called, all states",
          ok and not [n for n in called if n.split(".")[0] in ("pty", "os")])

    print("10. handlers removed and not wrapped by _gated")
    handlers_ok = all(len(r.server._handlers.get(t, [])) == 1
                      and r.server._handlers[t][0] == pc._refuse_terminal
                      and getattr(r.server._handlers[t][0], "__name__", "") == "_refuse_terminal" for t in TYPES)
    gone = not any(hasattr(pc, n) for n in ("_on_term_open", "_on_term_input", "_on_term_resize", "_on_term_close", "_terms"))
    check("test_terminal_handlers_removed_static: no _on_term_*/_terms, exactly one handler per type, _refuse_terminal, not _gated",
          handlers_ok and gone)

    print("13. audit record shape, 14. response")
    r.events.clear()
    shapes = []
    for state in ("no_grant", "denied", "approved", "expired"):
        r.set_state(state)
        c = await r.connect()
        await c.send("pc.term_open", {"cols": 80, "rows": 24, "secret": "payload-data"})
        rep = await c.collect()
        shapes.append((len(r.denials()), [(e[0], e[1], sorted(e[2]), e[2].get("feature"), e[2].get("message"), e[2].get("mode"))
                                          for e in r.denials()], rep, not r.others()))
        c.close()
        await asyncio.sleep(0.2)
        r.events.clear()
    want = [("access.denied", FP, ["feature", "message", "mode"], "terminal", "pc.term_open", "unavailable")]
    check("test_terminal_audit_event_shape: exactly one access.denied {feature, message, mode}, no payload data, every state",
          all(n == 1 and ev == want and quiet for n, ev, _, quiet in shapes) and not any("payload-data" in repr(ev) for _, ev, _, _ in shapes)
          and Rig.selftest_validators())
    check("test_terminal_response_is_documented_unavailable: pc.status exactly {denied: true, message: 'The terminal is not available'}, sent once",
          all(len(rep) == 1 and rep[0].type == "pc.status" and rep[0].payload == REPLY for _, _, rep, _ in shapes))

    print("16. static")
    root = Path(__file__).resolve().parents[1] / "phonebridge"
    bad = []
    for f in root.rglob("*.py"):
        if f.name == "terminal.py":
            continue
        src = f.read_text(encoding="utf-8")
        if re.search(r"TerminalSession\s*\(", src) or re.search(r"^\s*(from\s+\.+\s*terminal\b|from\s+phonebridge(\.\w+)*\.terminal\b|from\s+\.+\s+import\s+.*\bterminal\b|import\s+.*\bterminal\b)", src, re.M):
            bad.append(str(f.relative_to(root)))
    check("test_terminal_construction_static: TerminalSession( is constructed nowhere and no module imports terminal.py", not bad)
    if bad:
        print("       offending:", bad)

    r.serve.cancel()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
