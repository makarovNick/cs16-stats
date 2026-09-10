#!/usr/bin/env python3
"""
harvest_client.py — grab the server list from the memory of a running CS 1.6 client.

Why: non-Steam builds (yours is CSPro) don't use Valve's UDP master, they talk
their own GMS over HTTP: POST / to mscf1.cs-css.com:8880 (fallbacks — mscf2/mscf3),
the body is compressed and serialized, so the request can't be reproduced "from outside".
But once the client has opened the server browser, the whole received list
sits in its memory as ip:port and host:port — that's what we read out.

Usage (the game must be running and the server browser opened at least once):

    python harvest_client.py                 # finds hl.exe itself, appends to seeds.txt
    python harvest_client.py --pid 22232 --probe --db
    python harvest_client.py --out my.txt --no-seeds

Only the process memory is read, nothing is written into the game.
"""

import argparse
import collections
import ctypes
import ctypes.wintypes as wt
import os
import re
import socket
import subprocess
import sys

# Windows consoles default to cp1252 while server names are UTF-8: never crash on print
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_NOACCESS, PAGE_GUARD = 0x01, 0x104

IP_RE = re.compile(rb"\b((?:\d{1,3}\.){3}\d{1,3}):(\d{4,5})\b")
DOM_RE = re.compile(
    rb"\b((?:[a-z0-9][a-z0-9\-]{1,30}\.){1,3}"
    rb"(?:ru|com|net|org|su|ua|info|pro|me|site|online|club|xyz|cc)):(\d{4,5})\b",
    re.I,
)
PORT_LO, PORT_HI = 26900, 28100   # range of GoldSrc game ports


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD),
    ]


def find_pid(name="hl.exe"):
    """PID of the running client (no external dependencies)."""
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True).stdout
    except OSError:
        return None
    pids = [int(m) for m in re.findall(r'"[^"]*","(\d+)"', out)]
    return pids[0] if pids else None


def scan_memory(pid):
    """Pulls all host:port on game ports out of the process memory."""
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        raise OSError(f"OpenProcess({pid}) failed, code {ctypes.GetLastError()}")

    found = collections.Counter()
    addr, mbi, read = 0, MEMORY_BASIC_INFORMATION(), ctypes.c_size_t()
    while k.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        size = mbi.RegionSize
        if (mbi.State == MEM_COMMIT and mbi.Protect not in (PAGE_NOACCESS, PAGE_GUARD)
                and size < 200 * 1024 * 1024):
            buf = ctypes.create_string_buffer(size)
            if k.ReadProcessMemory(h, ctypes.c_void_p(mbi.BaseAddress), buf,
                                   size, ctypes.byref(read)):
                data = buf.raw[:read.value]
                # second pass over the string with nulls stripped — catches UTF-16 strings
                for blob in (data, data.replace(b"\x00", b"")):
                    for ip, port in IP_RE.findall(blob):
                        if PORT_LO <= int(port) <= PORT_HI and all(
                                int(o) < 256 for o in ip.split(b".")):
                            found[f"{ip.decode()}:{int(port)}"] += 1
                    for dom, port in DOM_RE.findall(blob):
                        if PORT_LO <= int(port) <= PORT_HI:
                            found[f"{dom.decode().lower()}:{int(port)}"] += 1
        addr = (mbi.BaseAddress or 0) + size
        if addr > 0x7FFFFFFF:
            break
    k.CloseHandle(h)
    return found


def normalize(raw):
    """Cleans junk prefixes and resolves domains into ip:port."""
    out = set()
    for a in raw:
        host, _, port = a.rpartition(":")
        host = re.sub(r"^[^a-z0-9]+", "", host, flags=re.I)
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", host):
            out.add(f"{host}:{port}")
            continue
        try:
            out.add(f"{socket.gethostbyname(host)}:{port}")
        except OSError:
            pass
    return sorted(out)


def main():
    p = argparse.ArgumentParser(description="Collect server addresses from CS 1.6 client memory")
    p.add_argument("--pid", type=int, help="hl.exe PID (found automatically by default)")
    p.add_argument("--out", help="file with the address list (besides seeds.txt)")
    p.add_argument("--no-seeds", action="store_true", help="don't append to seeds.txt")
    p.add_argument("--probe", action="store_true", help="probe via A2S right away")
    p.add_argument("--db", action="store_true", help="write the probe result into cs16.db")
    p.add_argument("--threads", type=int, default=120)
    a = p.parse_args()

    if sys.platform != "win32":
        sys.exit("works only on Windows")

    pid = a.pid or find_pid()
    if not pid:
        sys.exit("hl.exe not found — start the game and open the server browser")

    print(f"reading memory of PID {pid}…")
    found = scan_memory(pid)
    addrs = normalize(found)
    print(f"addresses in memory: {len(found)} → after normalization: {len(addrs)}")
    if not addrs:
        sys.exit("empty: in the game open \"Find servers\" → \"Internet\" and try again")

    rows = []
    if a.probe:
        import a2s
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(a.threads) as ex:
            rows = [r for r in ex.map(lambda x: a2s.probe(x, timeout=1.5), addrs) if r]
        print(f"alive: {len(rows)} | non-Steam: {sum(r['pirate'] for r in rows)}"
              f" | players online: {sum(r['players'] for r in rows)}")
        for r in sorted(rows, key=lambda r: -r["players"])[:10]:
            print(f"  {r['players']:>3}/{r['max_players']:<3} {(r.get('map') or ''):<16}"
                  f" {r['addr']:<22} {(r.get('name') or '')[:45]}")
        if a.db:
            import store
            store.init()
            store.upsert_many(rows)
            print(f"written to cs16.db: {len(rows)}")

    keep = [r["addr"] for r in rows] if rows else addrs
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + "\n")
        print(f"saved to {a.out}: {len(keep)}")
    if not a.no_seeds:
        seeds = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seeds.txt")
        existing = set()
        if os.path.exists(seeds):
            with open(seeds, encoding="utf-8") as f:
                existing = {l.split("#")[0].strip() for l in f}
        new = [x for x in keep if x not in existing]
        with open(seeds, "a", encoding="utf-8") as f:
            f.write(f"\n# --- from client memory (PID {pid}) ---\n" + "\n".join(new) + "\n")
        print(f"appended to seeds.txt: {len(new)} new")


if __name__ == "__main__":
    main()
