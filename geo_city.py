#!/usr/bin/env python3
"""
geo_city.py — city-level location of server IPs via ip-api.com (batch endpoint,
free for non-commercial use, 15 requests x 100 IPs per minute, no key).

Team Cymru (geo.py) gives country + ASN; this adds region/city/ISP, cached in the
geo_city table of cs16.db so every IP is asked exactly once.

  python geo_city.py --backfill        # all IPs from servers that are not cached yet
  python geo_city.py 1.2.3.4 5.6.7.8   # one-off
"""

import json
import os
import sqlite3
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "cs16.db")
URL = ("http://ip-api.com/batch?fields=status,countryCode,regionName,city,"
       "isp,org,as,lat,lon,query")
BATCH = 100
PER_MINUTE = 15

SCHEMA = """
CREATE TABLE IF NOT EXISTS geo_city (
    ip TEXT PRIMARY KEY, country TEXT, region TEXT, city TEXT,
    isp TEXT, org TEXT, asn TEXT, lat REAL, lon REAL, ts TEXT
);
"""


def _con():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _ask(ips):
    req = urllib.request.Request(URL, data=json.dumps(ips).encode(),
                                 headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                ttl = int(r.headers.get("X-Ttl") or 60)
                left = int(r.headers.get("X-Rl") or PER_MINUTE)
                return json.loads(r.read().decode("utf-8")), left, ttl
        except Exception as e:  # noqa: BLE001
            print(f"  ip-api retry ({e})", file=sys.stderr)
            time.sleep(20)
    return [], 0, 60


def lookup(ips, verbose=True):
    """ip -> {country, region, city, isp, org, asn, lat, lon}; cached in geo_city."""
    ips = sorted({i for i in ips if i and i.count(".") == 3})
    res = {}
    with _con() as con:
        for i in range(0, len(ips), 900):
            part = ips[i:i + 900]
            q = ",".join("?" * len(part))
            for r in con.execute(f"SELECT * FROM geo_city WHERE ip IN ({q})", part):
                res[r["ip"]] = dict(r)
    missing = [i for i in ips if i not in res]
    if verbose:
        print(f"cached {len(res)}, to ask {len(missing)}", file=sys.stderr)
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    for i in range(0, len(missing), BATCH):
        chunk = missing[i:i + BATCH]
        rows, left, ttl = _ask(chunk)
        got = {}
        for d in rows:
            ip = d.get("query")
            if not ip:
                continue
            ok = d.get("status") == "success"
            got[ip] = {"ip": ip, "country": d.get("countryCode") if ok else "??",
                       "region": d.get("regionName") if ok else None,
                       "city": d.get("city") if ok else None,
                       "isp": d.get("isp") if ok else None, "org": d.get("org") if ok else None,
                       "asn": (d.get("as") or "").split(" ")[0] if ok else None,
                       "lat": d.get("lat") if ok else None, "lon": d.get("lon") if ok else None}
        for ip in chunk:            # remember failures too, so they are not re-asked
            got.setdefault(ip, {"ip": ip, "country": "??", "region": None, "city": None,
                                "isp": None, "org": None, "asn": None, "lat": None, "lon": None})
        with _con() as con:
            con.executemany(
                "INSERT OR REPLACE INTO geo_city (ip,country,region,city,isp,org,asn,lat,lon,ts) "
                "VALUES (:ip,:country,:region,:city,:isp,:org,:asn,:lat,:lon,:ts)",
                [{**g, "ts": now} for g in got.values()])
        res.update(got)
        if verbose:
            print(f"  ip-api {min(i + BATCH, len(missing))}/{len(missing)} "
                  f"(rate left {left}, reset in {ttl}s)", file=sys.stderr)
        if left <= 0 and i + BATCH < len(missing):
            time.sleep(ttl + 1)
        elif i + BATCH < len(missing):
            time.sleep(60 / PER_MINUTE + 0.2)   # stay under 15 req/min
    return res


def backfill(verbose=True):
    with _con() as con:
        ips = [r[0] for r in con.execute(
            "SELECT DISTINCT ip FROM servers WHERE ip IS NOT NULL "
            "AND ip NOT IN (SELECT ip FROM geo_city)")]
    if verbose:
        print(f"IPs without city: {len(ips)}", file=sys.stderr)
    return lookup(ips, verbose=verbose) if ips else {}


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--backfill" in args or not args:
        backfill()
    else:
        for ip, d in lookup(args).items():
            print(ip, d)
