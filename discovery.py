"""
discovery.py — sources of CS 1.6 server addresses.

The main problem with non-Steam ("pirate") servers: they are not required to
register with Valve's master server, so a single source is not enough.
There are three here, and they add up:

  1. master  — query the master server via the Valve protocol (the list of hosts
               is set in sources.json; non-Steam builds use their own masters, and
               some pirate servers still show up in Valve's master with VAC off);
  2. seeds   — the seeds.txt file with a list of ip:port (manual, most reliable);
  3. urls    — any monitoring web page/JSON/API: all ip:port are extracted from
               the response with a regex.
"""

import json
import os
import re
import socket
import struct
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(BASE, "sources.json")
SEEDS_FILE = os.path.join(BASE, "seeds.txt")

REGIONS = {
    "world": 0xFF, "us-east": 0x00, "us-west": 0x01, "sa": 0x02,
    "eu": 0x03, "asia": 0x04, "au": 0x05, "me": 0x06, "africa": 0x07,
}

ADDR_RE = re.compile(
    r"\b((?:\d{1,3}\.){3}\d{1,3})[:\s\"',]{1,3}(\d{2,5})\b"
)

DEFAULT_SOURCES = {
    "masters": [
        {"host": "hl1master.steampowered.com", "port": 27011, "enabled": True,
         "note": "historical GoldSrc master; as of 2026 DNS may not resolve"},
        {"host": "hl2master.steampowered.com", "port": 27011, "enabled": True,
         "note": "fallback Valve master"}
    ],
    "urls": [],
    "_note": (
        "Pirate servers are rarely in Valve's master. Working sources for them: "
        "(1) seeds.txt with your own addresses; "
        "(2) urls — links to any monitors/APIs from which ip:port are extracted "
        "with a regex; (3) masters — non-Steam build masters, "
        "if you know their host:port, add them here."
    )
}


def load_sources():
    if not os.path.exists(SOURCES_FILE):
        with open(SOURCES_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_SOURCES, f, ensure_ascii=False, indent=2)
        return dict(DEFAULT_SOURCES)
    with open(SOURCES_FILE, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ master

def query_master(host, port=27011, region=0xFF, filt=r"\gamedir\cstrike",
                 limit=5000, timeout=3.0):
    """Iterative master-server walk: returns a list of 'ip:port'."""
    try:
        addr = (socket.gethostbyname(host), port)
    except OSError:
        return []

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    seed, seen, out = "0.0.0.0:0", set(), []
    try:
        while len(out) < limit:
            pkt = (b"\x31" + bytes([region]) + seed.encode() + b"\x00"
                   + filt.encode() + b"\x00")
            sock.sendto(pkt, addr)
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            body = data[6:]  # header \xFF\xFF\xFF\xFF\x66\x0A
            batch = []
            for i in range(0, len(body) - 5, 6):
                ip = ".".join(str(b) for b in body[i:i + 4])
                p = struct.unpack(">H", body[i + 4:i + 6])[0]
                batch.append(f"{ip}:{p}")
            if not batch:
                break
            for s in batch:
                if s != "0.0.0.0:0" and s not in seen:
                    seen.add(s)
                    out.append(s)
            if batch[-1] == "0.0.0.0:0":  # end-of-list marker
                break
            seed = batch[-1]
            time.sleep(0.15)  # don't hammer the master
    except OSError:
        pass
    finally:
        sock.close()
    return out[:limit]


# ------------------------------------------------------------------ other

def from_url(url, timeout=15):
    """Extracts all ip:port from the URL response (HTML, JSON, plain text)."""
    req = urllib.request.Request(url, headers={"User-Agent": "cs16-stats/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    out, seen = [], set()
    for ip, port in ADDR_RE.findall(text):
        p = int(port)
        if not (1024 <= p <= 65535):
            continue
        if any(int(o) > 255 for o in ip.split(".")):
            continue
        a = f"{ip}:{p}"
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def from_seeds(path=SEEDS_FILE):
    """seeds.txt: one ip:port per line, '#' is a comment."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            m = ADDR_RE.search(line)
            if m:
                out.append(f"{m.group(1)}:{int(m.group(2))}")
    return out


def collect(region="world", limit=5000, use_masters=True, use_seeds=True,
            use_urls=True, extra_urls=None, log=print):
    """Collects a merged list of addresses from all enabled sources."""
    src = load_sources()
    addrs, seen = [], set()

    def add(items, tag):
        added = 0
        for a in items:
            if a not in seen:
                seen.add(a)
                addrs.append(a)
                added += 1
        log(f"[{tag}] +{added} (total {len(addrs)})")

    if use_masters:
        for m in src.get("masters", []):
            if not m.get("enabled", True):
                continue
            got = query_master(m["host"], m.get("port", 27011),
                               REGIONS.get(region, 0xFF), limit=limit)
            add(got, f"master {m['host']}")

    if use_seeds:
        add(from_seeds(), "seeds.txt")

    if use_urls:
        for u in list(src.get("urls", [])) + list(extra_urls or []):
            add(from_url(u), f"url {u[:48]}")

    return addrs
