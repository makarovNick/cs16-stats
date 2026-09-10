#!/usr/bin/env python3
"""
probe_file.py — probe a list of addresses via A2S and record the live ones.

  python probe_file.py harvest/tsarvar.txt              # prints statistics
  python probe_file.py harvest/*.txt --db --alive alive.txt
Input: any text from which ip:port and host:port are extracted by regex.
"""
import argparse, glob, re, socket, sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
import a2s

ADDR = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3}|(?:[a-z0-9][a-z0-9\-]{1,40}\.)+[a-z]{2,6})[:\s]{1,3}(\d{2,5})\b", re.I)

def parse(text):
    out = set()
    for host, port in ADDR.findall(text):
        p = int(port)
        if not (1024 <= p <= 65535):
            continue
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", host):
            if any(int(o) > 255 for o in host.split(".")):
                continue
            out.add(f"{host}:{p}")
        else:
            out.add(f"{host.lower()}:{p}")
    return out

def resolve(addrs, threads=64):
    def r(a):
        h, _, p = a.rpartition(":")
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", h):
            return f"{h}:{p}"
        try:
            return f"{socket.gethostbyname(h)}:{p}"
        except OSError:
            return None
    with ThreadPoolExecutor(threads) as ex:
        return sorted({x for x in ex.map(r, addrs) if x})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--threads", type=int, default=256)
    ap.add_argument("--timeout", type=float, default=1.5)
    ap.add_argument("--db", action="store_true", help="write live ones to cs16.db")
    ap.add_argument("--alive", help="file for live addresses")
    a = ap.parse_args()

    text = ""
    for pat in a.files:
        for f in glob.glob(pat):
            text += open(f, encoding="utf-8", errors="replace").read() + "\n"
    addrs = resolve(parse(text))
    print(f"addresses after parsing and resolving: {len(addrs)}")
    if not addrs:
        return
    with ThreadPoolExecutor(a.threads) as ex:
        rows = [r for r in ex.map(lambda x: a2s.probe(x, timeout=a.timeout), addrs) if r]
    other = [r for r in rows if not r.get("cs16")]
    rows = [r for r in rows if r.get("cs16")]
    print(f"filtered out non-CS1.6 (CS:S/CS2/TF2 etc.): {len(other)}")
    print(f"LIVE: {len(rows)} | non-steam: {sum(r['pirate'] for r in rows)} | "
          f"players: {sum(r['players'] for r in rows)}")
    if a.db:
        import store; store.init(); store.upsert_many(rows)
        print("written to cs16.db")
    if a.alive:
        open(a.alive, "w", encoding="utf-8").write("\n".join(r["addr"] for r in rows) + "\n")
        print(f"live ones saved to {a.alive}")

if __name__ == "__main__":
    main()
