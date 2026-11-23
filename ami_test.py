#!/usr/bin/env python3
"""
ami_test.py - Step-by-step FreePBX / Asterisk AMI test. Python stdlib only.

  1. TCP connect to AMI (port 5038)
  2. Login
  3. Ping
  4. Originate a call to an extension that plays a sound when answered
  5. Wait for OriginateResponse and report answered / no answer / busy / failed

Examples:
  # login test only, no call
  AMI_SECRET='xxx' ./ami_test.py --host 127.0.0.1 --user plcbridge --no-call

  # ring ext 1001 and play the built-in hello-world prompt
  AMI_SECRET='xxx' ./ami_test.py --host 127.0.0.1 --user plcbridge --ext 1001

  # test your custom recording
  ./ami_test.py --user plcbridge --ext 1001 --sound custom/plc-alarm
"""
import argparse
import getpass
import os
import socket
import sys
import time
import uuid

REASONS = {
    0: "FAILED - extension doesn't exist, isn't registered, or dialplan error",
    1: "Hung up before answer",
    3: "Rang, NO ANSWER (ring timeout)",
    4: "ANSWERED - sound should have played",
    5: "BUSY",
    8: "CONGESTION / channel unavailable (often: endpoint not registered)",
}


class AMI:
    def __init__(self, host, port, timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        self.banner = self._readline()

    def _fill(self):
        chunk = self.sock.recv(4096)
        if not chunk:
            raise ConnectionError("AMI closed the connection")
        self.buf += chunk

    def _readline(self):
        while b"\r\n" not in self.buf:
            self._fill()
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line.decode(errors="replace")

    def send(self, action, **fields):
        aid = uuid.uuid4().hex[:12]
        lines = [f"Action: {action}", f"ActionID: {aid}"]
        lines += [f"{k}: {v}" for k, v in fields.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        return aid

    def read_message(self):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        raw, self.buf = self.buf.split(b"\r\n\r\n", 1)
        msg = {}
        for line in raw.decode(errors="replace").split("\r\n"):
            if ": " in line:
                k, v = line.split(": ", 1)
                msg[k] = v
        return msg

    def wait_for(self, predicate, timeout, verbose=False):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sock.settimeout(max(0.5, deadline - time.time()))
            try:
                msg = self.read_message()
            except (socket.timeout, TimeoutError):
                break
            if verbose:
                print("    <<", msg)
            if predicate(msg):
                return msg
        return None

    def close(self):
        try:
            self.send("Logoff")
            self.sock.close()
        except OSError:
            pass


def step(n, text):
    print(f"\n[{n}] {text}")


def fail(text, hints=()):
    print(f"    FAIL: {text}")
    for h in hints:
        print(f"      - {h}")
    sys.exit(1)


def main():
    p = argparse.ArgumentParser(description="FreePBX AMI originate test")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5038)
    p.add_argument("--user", required=True, help="AMI manager name")
    p.add_argument("--ext", help="extension to ring")
    p.add_argument("--sound", default="hello-world",
                   help="sound to play, e.g. hello-world or custom/plc-alarm")
    p.add_argument("--ring", type=int, default=30, help="ring timeout seconds")
    p.add_argument("--cid-name", default="PLC Alarm")
    p.add_argument("--cid-num", default="900")
    p.add_argument("--no-call", action="store_true", help="only test login")
    p.add_argument("-v", "--verbose", action="store_true", help="print raw AMI messages")
    a = p.parse_args()

    if not a.no_call and not a.ext:
        p.error("--ext is required unless --no-call")

    secret = os.environ.get("AMI_SECRET") or getpass.getpass("AMI secret: ")

    # 1 ---------------------------------------------------------------
    step(1, f"Connecting to {a.host}:{a.port}")
    try:
        ami = AMI(a.host, a.port)
    except OSError as e:
        fail(str(e), [
            "On the PBX: ss -tlnp | grep 5038  (is Asterisk listening, and on which address?)",
            "FreePBX firewall: add this host's IP to the Trusted zone (Connectivity > Firewall > Networks)",
            "Any firewall in between blocking TCP 5038?",
        ])
    print(f"    OK: {ami.banner}")

    # 2 ---------------------------------------------------------------
    step(2, f"Logging in as '{a.user}'")
    aid = ami.send("Login", Username=a.user, Secret=secret, Events="call")
    r = ami.wait_for(lambda m: m.get("ActionID") == aid, 10, a.verbose)
    if not r or r.get("Response") != "Success":
        fail(r.get("Message", "no response") if r else "no response", [
            "Wrong manager name/secret (did you click Apply Config after saving?)",
            "This host's IP isn't covered by the manager's Permit field",
            "Check on PBX: asterisk -rx 'manager show user " + a.user + "'",
        ])
    print("    OK: authenticated")

    # 3 ---------------------------------------------------------------
    step(3, "Ping")
    aid = ami.send("Ping")
    r = ami.wait_for(lambda m: m.get("ActionID") == aid, 5, a.verbose)
    if not r or r.get("Response") != "Success":
        fail("no Pong")
    print("    OK: Pong")

    if a.no_call:
        ami.close()
        print("\nLogin test passed.")
        return

    # 4 ---------------------------------------------------------------
    channel = f"Local/{a.ext}@from-internal"
    step(4, f"Originate {channel} -> Playback({a.sound})")
    aid = ami.send(
        "Originate",
        Channel=channel,
        Application="Playback",
        Data=a.sound,
        CallerID=f'"{a.cid_name}" <{a.cid_num}>',
        Timeout=str(a.ring * 1000),
        Async="true",
    )
    r = ami.wait_for(lambda m: m.get("ActionID") == aid and "Response" in m, 10, a.verbose)
    if not r or r.get("Response") != "Success":
        msg = r.get("Message", "no response") if r else "no response"
        fail(msg, [
            "'Permission denied' = manager is missing WRITE 'originate' (and 'call')",
        ])
    print(f"    OK: {r.get('Message', 'queued')} - phone should be ringing now")

    # 5 ---------------------------------------------------------------
    step(5, f"Waiting up to {a.ring + 15}s for OriginateResponse")
    r = ami.wait_for(
        lambda m: m.get("Event") == "OriginateResponse" and m.get("ActionID") == aid,
        a.ring + 15, a.verbose)
    ami.close()

    if not r:
        fail("no OriginateResponse event received", [
            "Manager is missing READ 'call' permission (call may still have worked)",
            "Watch the PBX CLI during the test: asterisk -rvvv",
        ])

    reason = int(r.get("Reason", "0") or 0)
    print(f"    Response={r.get('Response')}  Reason={reason}: {REASONS.get(reason, 'unknown')}")
    if reason == 4:
        print("\nCall test passed. If you heard nothing, check the sound name "
              "(ls /var/lib/asterisk/sounds/en/custom/).")
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
