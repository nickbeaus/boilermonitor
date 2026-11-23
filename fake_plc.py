#!/usr/bin/env python3
"""
fake_plc.py - Pretends to be the CLICK PLC so you can test the bridge
without hardware. Python stdlib only (no pymodbus needed).

It runs a small Modbus TCP server and emulates the planned ladder logic:
  pressure goes LOW    (rising edge)  -> SET C1
  pressure goes NORMAL (falling edge) -> SET C2
  C101 set by bridge                  -> RST C1, RST C101
  C102 set by bridge                  -> RST C2, RST C102

Commands (type and press Enter):
  l  pressure LOW        n  pressure NORMAL
  o  go OFFLINE (simulate PLC/WiFi loss)   u  come back UP
  s  show status         q  quit

Usage:
  python fake_plc.py                      # listens on 127.0.0.1:5020
  python fake_plc.py --bind 0.0.0.0 --port 5020
Then set "plc": {"host": "127.0.0.1", "port": 5020} in config.json.
"""
import argparse
import asyncio
import struct

C_BASE = 16384                     # CLICK C1 = 0-based Modbus coil 16384
ALARM_ACK_PAIRS = [(1, 101), (2, 102)]


def c(n):
    return C_BASE + n - 1


class FakePLC:
    def __init__(self, bind, port):
        self.bind, self.port = bind, port
        self.coils = {}            # modbus coil address -> bool
        self.pressure_low = False
        self.server = None
        self.writers = set()

    # ------------------------------------------------------------ ladder
    def set_pressure(self, low: bool):
        if low and not self.pressure_low:
            self.coils[c(1)] = True
            print("  ladder: pressure LOW (rising edge) -> SET C1")
        elif not low and self.pressure_low:
            self.coils[c(2)] = True
            print("  ladder: pressure NORMAL (falling edge) -> SET C2")
        else:
            print("  (no change - pressure already", "LOW)" if low else "NORMAL)")
        self.pressure_low = low

    async def scan(self):
        while True:
            for alarm, ack in ALARM_ACK_PAIRS:
                if self.coils.get(c(ack)):
                    self.coils[c(alarm)] = False
                    self.coils[c(ack)] = False
                    print(f"  ladder: bridge ACKed C{ack} -> RST C{alarm}, RST C{ack}")
            await asyncio.sleep(0.05)

    def status(self):
        print(f"  online={self.server is not None}  pressure={'LOW' if self.pressure_low else 'NORMAL'}")
        for alarm, ack in ALARM_ACK_PAIRS:
            print(f"  C{alarm}={int(self.coils.get(c(alarm), False))}  "
                  f"C{ack}={int(self.coils.get(c(ack), False))}")

    # ---------------------------------------------------------- modbus
    def handle_pdu(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        try:
            if fc == 1:                                  # read coils
                addr, qty = struct.unpack(">HH", pdu[1:5])
                if not 1 <= qty <= 2000:
                    return bytes([fc | 0x80, 3])
                bits = [self.coils.get(addr + i, False) for i in range(qty)]
                out = bytearray((qty + 7) // 8)
                for i, b in enumerate(bits):
                    if b:
                        out[i // 8] |= 1 << (i % 8)
                return bytes([fc, len(out)]) + bytes(out)
            if fc == 5:                                  # write single coil
                addr, val = struct.unpack(">HH", pdu[1:5])
                if val not in (0x0000, 0xFF00):
                    return bytes([fc | 0x80, 3])
                self.coils[addr] = val == 0xFF00
                return pdu[:5]
            if fc == 15:                                 # write multiple coils
                addr, qty, nbytes = struct.unpack(">HHB", pdu[1:6])
                data = pdu[6:6 + nbytes]
                for i in range(qty):
                    self.coils[addr + i] = bool(data[i // 8] >> (i % 8) & 1)
                return bytes([fc]) + pdu[1:5]
            if fc in (2, 3, 4):                          # unused: return zeros
                addr, qty = struct.unpack(">HH", pdu[1:5])
                if fc == 2:
                    return bytes([fc, (qty + 7) // 8]) + bytes((qty + 7) // 8)
                return bytes([fc, qty * 2]) + bytes(qty * 2)
        except struct.error:
            return bytes([fc | 0x80, 3])
        return bytes([fc | 0x80, 1])                     # illegal function

    async def client(self, reader, writer):
        self.writers.add(writer)
        try:
            while True:
                hdr = await reader.readexactly(7)
                tid, pid, length, unit = struct.unpack(">HHHB", hdr)
                pdu = await reader.readexactly(length - 1)
                resp = self.handle_pdu(pdu)
                writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()

    async def go_online(self):
        if self.server:
            print("  already online")
            return
        self.server = await asyncio.start_server(self.client, self.bind, self.port)
        print(f"  ONLINE - Modbus TCP listening on {self.bind}:{self.port}")

    async def go_offline(self):
        if not self.server:
            print("  already offline")
            return
        self.server.close()
        for w in list(self.writers):
            w.close()
        await self.server.wait_closed()
        self.server = None
        print("  OFFLINE - bridge should report comms lost after its timeout")

    # ------------------------------------------------------------ main
    async def run(self):
        await self.go_online()
        asyncio.create_task(self.scan())
        loop = asyncio.get_running_loop()
        print(__doc__.split("Commands")[1].split("Usage")[0].strip())
        while True:
            cmd = (await loop.run_in_executor(None, input, "fake-plc> ")).strip().lower()
            if cmd == "l":
                self.set_pressure(True)
            elif cmd == "n":
                self.set_pressure(False)
            elif cmd == "o":
                await self.go_offline()
            elif cmd == "u":
                await self.go_online()
            elif cmd == "s":
                self.status()
            elif cmd == "q":
                break
            elif cmd:
                print("  commands: l n o u s q")


def main():
    p = argparse.ArgumentParser(description="Fake CLICK PLC for bridge testing")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5020)
    a = p.parse_args()
    try:
        asyncio.run(FakePLC(a.bind, a.port).run())
    except (KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    main()
