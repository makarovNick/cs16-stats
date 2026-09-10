#!/usr/bin/env python3
"""
sweep.py — asynchronous UDP A2S sweep: finding servers near known ones.

Idea: game hosters keep dozens to hundreds of servers in one /24, and a single
server often listens on several ports. So it makes sense to expand from each
known address: first the "anchor" ports across its whole /24, then the full
port range on the IPs that responded.

Packets are sent by one socket with a rate limit (--pps), replies are received
by a separate thread — this is orders of magnitude cheaper than a pool of
threads with blocking recvs, and lets you cover hundreds of thousands of
addresses in minutes.

  python sweep.py --from-db --anchors --pps 3000          # phase A: /24 x anchor ports
  python sweep.py --from-file alive.txt --portsweep       # phase B: full port range
  python sweep.py --nets 46.174.48.0/24 62.122.215.0/24 --anchors
"""

import argparse
import ipaddress
import os
import re
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timezone

# Windows consoles default to cp1252 while server names are UTF-8: never crash on print
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

A2S_INFO = b"\xFF\xFF\xFF\xFF" + b"TSource Engine Query\x00" + b"\xFF\xFF\xFF\xFF"

# ports where the vast majority of CS 1.6 servers sit
ANCHOR_PORTS = [27015, 27016, 27017, 27018, 27019, 27020, 27025, 27030, 27050, 27060]
FULL_PORTS = list(range(27000, 27101)) + [26900, 26950, 27200, 27500, 28015]


class Sweeper:
    def __init__(self, pps=3000, wait=6.0, quiet=False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.sock.settimeout(0.4)
        self.pps = pps
        self.wait = wait
        self.quiet = quiet
        self.found = {}          # "ip:port" -> server name (or "")
        self.challenges = 0
        self.running = True
        self.sent = 0
        self.replies = 0
        self.send_errors = 0

    # -------------------------------------------------------------- receiving

    def _reader(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            self.replies += 1
            key = f"{addr[0]}:{addr[1]}"
            if len(data) < 6 or data[:4] != b"\xFF\xFF\xFF\xFF":
                continue
            kind = data[4:5]
            if kind == b"A":                     # challenge — ask again
                self.challenges += 1
                try:
                    self.sock.sendto(A2S_INFO[:-4] + data[5:9], addr)
                except OSError:
                    pass
                continue
            if kind not in (b"m", b"I"):
                continue
            name = ""
            try:
                if kind == b"m":                 # GoldSrc: address\0 name\0 ...
                    end = data.index(b"\x00", 5)
                    name = data[end + 1:data.index(b"\x00", end + 1)].decode("utf-8", "replace")
                else:                            # Source: protocol byte, then name
                    name = data[6:data.index(b"\x00", 6)].decode("utf-8", "replace")
            except (ValueError, IndexError):
                pass
            if key not in self.found:
                self.found[key] = name
                if not self.quiet:
                    print(f"  + {key:<22} {name[:48]}", flush=True)

    # -------------------------------------------------------------- sending

    def run(self, targets, rounds=2):
        """targets — an iterator of (ip, port). Each address is queried `rounds` times."""
        t = threading.Thread(target=self._reader, daemon=True)
        t.start()
        interval = 1.0 / self.pps
        t0 = time.time()
        for rnd in range(rounds):
            n = 0
            for ip, port in targets():
                if rnd and f"{ip}:{port}" in self.found:
                    continue     # already replied in the first round — no repeat needed
                try:
                    self.sock.sendto(A2S_INFO, (ip, port))
                except OSError:      # kernel send buffer is full
                    self.send_errors += 1
                    time.sleep(0.002)
                    continue
                self.sent += 1
                n += 1
                # soft rate limit: keep the average speed around pps
                expected = t0 + self.sent * interval
                lag = expected - time.time()
                if lag > 0:
                    time.sleep(lag)
                if not self.quiet and n % 20000 == 0:
                    print(f"    [{rnd + 1}/{rounds}] sent {n}, found {len(self.found)}",
                          file=sys.stderr, flush=True)
            if not self.quiet:
                print(f"  round {rnd + 1}: sent {n}, found {len(self.found)}",
                      file=sys.stderr, flush=True)
        time.sleep(self.wait)     # collect the tail of replies
        self.running = False
        return self.found


# ------------------------------------------------------------------ targets

def nets_from_addrs(addrs, prefix=24):
    nets = set()
    for a in addrs:
        ip = a.split(":")[0]
        try:
            nets.add(str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False)))
        except ValueError:
            pass
    return sorted(nets)


def read_addrs_db():
    import store
    with store.connect() as con:
        return [r["addr"] for r in con.execute("SELECT addr FROM servers")]


def read_addrs_file(path):
    text = open(path, encoding="utf-8", errors="replace").read()
    return [f"{ip}:{p}" for ip, p in
            re.findall(r"\b((?:\d{1,3}\.){3}\d{1,3}):(\d{2,5})\b", text)]


def main():
    ap = argparse.ArgumentParser(description="UDP A2S sweep over subnets and ports")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-db", action="store_true", help="take known addresses from cs16.db")
    src.add_argument("--from-file", help="file with known addresses")
    src.add_argument("--nets", nargs="+", help="subnets like 46.174.48.0/24")
    src.add_argument("--nets-file", help="file with a list of subnets, one per line")
    src.add_argument("--targets-file", help="file with ready ip:port targets — query as is")
    mode = ap.add_mutually_exclusive_group(required=False)
    mode.add_argument("--anchors", action="store_true",
                      help="phase A: all IPs of the subnet x anchor ports")
    mode.add_argument("--portsweep", action="store_true",
                      help="phase B: known IPs x full port range")
    ap.add_argument("--pps", type=int, default=3000, help="packets per second")
    ap.add_argument("--rounds", type=int, default=2, help="passes (UDP gets lost)")
    ap.add_argument("--prefix", type=int, default=24)
    ap.add_argument("--max-nets", type=int, default=0, help="limit the number of subnets")
    ap.add_argument("--ports", help="your own set of ports, comma-separated (otherwise anchor ports)")
    ap.add_argument("--out", default="harvest/sweep.txt")
    ap.add_argument("--db", action="store_true", help="append what was found to cs16.db")
    a = ap.parse_args()

    custom = [int(x) for x in a.ports.split(",")] if a.ports else None
    if a.targets_file:
        pairs = [(x.split(":")[0], int(x.split(":")[1]))
                 for x in read_addrs_file(a.targets_file)]
        print(f"direct query of the list: {len(pairs)} targets", file=sys.stderr)

        def targets():
            return iter(pairs)

    elif a.nets_file:
        nets = [l.strip() for l in open(a.nets_file, encoding="utf-8") if l.strip()]
        known = []
    elif a.nets:
        nets, known = a.nets, []
    else:
        known = read_addrs_db() if a.from_db else read_addrs_file(a.from_file)
        nets = nets_from_addrs(known, a.prefix)
    if not a.targets_file and a.max_nets:
        nets = nets[:a.max_nets]

    if a.targets_file:
        pass
    elif a.anchors:
        ports = custom or ANCHOR_PORTS
        total = len(nets) * (2 ** (32 - a.prefix)) * len(ports)
        print(f"phase A: {len(nets)} subnets /{a.prefix} x {len(ports)} ports = {total} targets",
              file=sys.stderr)

        def targets():
            for net in nets:
                obj = ipaddress.ip_network(net)
                base = int(obj.network_address)
                for off in range(obj.num_addresses):
                    v = base + off
                    ip = f"{v >> 24 & 255}.{v >> 16 & 255}.{v >> 8 & 255}.{v & 255}"
                    for p in ports:
                        yield ip, p
    else:
        ips = sorted({x.split(":")[0] for x in known})
        ports = custom or FULL_PORTS
        print(f"phase B: {len(ips)} IPs x {len(ports)} ports = {len(ips) * len(ports)} targets",
              file=sys.stderr)

        def targets():
            for ip in ips:
                for p in ports:
                    yield ip, p

    t0 = time.time()
    sw = Sweeper(pps=a.pps)
    found = sw.run(targets, rounds=a.rounds)
    dt = time.time() - t0

    print(f"\nSENT {sw.sent}, REPLIES {sw.replies}, challenge {sw.challenges}", file=sys.stderr)
    print(f"SERVERS FOUND: {len(found)} in {dt:.0f} sec "
          f"(actually {sw.sent / dt:.0f} packets/sec, send errors {sw.send_errors})",
          file=sys.stderr)

    os.makedirs(os.path.dirname(os.path.join(BASE, a.out)) or ".", exist_ok=True)
    with open(os.path.join(BASE, a.out), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(found)) + "\n")
    print(f"saved to {a.out}", file=sys.stderr)

    if a.db and found:
        import a2s
        import store
        from concurrent.futures import ThreadPoolExecutor
        store.init()
        with ThreadPoolExecutor(300) as ex:
            rows = [r for r in ex.map(lambda x: a2s.probe(x, timeout=1.5), sorted(found)) if r]
        rows = [r for r in rows if r.get("cs16")]
        store.upsert_many(rows)
        print(f"written to cs16.db: {len(rows)} (full probe with players and cvars)",
              file=sys.stderr)


if __name__ == "__main__":
    main()
