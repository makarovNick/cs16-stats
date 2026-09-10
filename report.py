#!/usr/bin/env python3
"""
report.py — summary analytics over the collected CS 1.6 server database.

  python report.py                 # prints the report
  python report.py --json out.json # exports data for the web report
"""

import argparse
import collections
import ipaddress
import json
import os
import sys

# Windows consoles default to cp1252 while server names are UTF-8: never crash on print
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import store  # noqa: E402

VALID = "players <= max_players AND max_players <= 64"


def build():
    con = store.connect()
    rows = [dict(r) for r in con.execute(
        f"SELECT addr, ip, port, name, map, players, max_players, real_players, "
        f"pirate, password, ping_ms, environment, proto_kind, first_seen "
        f"FROM servers WHERE {VALID}")]
    raw_total = con.execute("SELECT COUNT(*) FROM servers").fetchone()[0]

    online = sum(r["players"] for r in rows)
    confirmed = sum(r["real_players"] or 0 for r in rows)
    slots = sum(r["max_players"] for r in rows)
    pirate = [r for r in rows if r["pirate"]]

    maps = collections.Counter()
    map_srv = collections.Counter()
    for r in rows:
        maps[r["map"] or "?"] += r["players"]
        map_srv[r["map"] or "?"] += 1

    nets = collections.Counter()
    for r in rows:
        try:
            nets[str(ipaddress.ip_network(f"{r['ip']}/24", strict=False))] += 1
        except ValueError:
            pass

    ips = collections.Counter(r["ip"] for r in rows)
    ports = collections.Counter(r["port"] for r in rows)
    env = collections.Counter(r["environment"] for r in rows)

    # port farms: a single IP answers A2S on dozens-to-hundreds of ports, often with a fake
    # player list. Counting each port as a separate server is dishonest — it
    # inflates the statistics several times over. We separate the "farms" (>20 ports per IP) from the rest.
    FARM = 20
    farm_ips = {ip for ip, c in ips.items() if c > FARM}
    on_farm = [r for r in rows if r["ip"] in farm_ips]
    off_farm = [r for r in rows if r["ip"] not in farm_ips]
    ip_name = len({(r["ip"], r["name"]) for r in rows})

    # a "project" = a common name prefix: often dozens of servers from one community
    def brand(name):
        n = (name or "").upper()
        for token in ("|", "[", "]", "#", "-", ":", "©", "»", "«"):
            n = n.replace(token, " ")
        words = [w for w in n.split() if len(w) > 3 and not w.isdigit()]
        return words[0] if words else "?"

    brands = collections.Counter(brand(r["name"]) for r in rows)

    # clone farms: dozens-to-hundreds of servers with the exact same name — inflating
    # monitoring stats and redirect farms; real projects are far fewer than the addresses
    names = collections.Counter(r["name"] or "" for r in rows)
    clones = [(n, c) for n, c in names.most_common(20) if c > 5]
    cloned_servers = sum(c for n, c in names.items() if c > 5)

    fake = {}
    fc = os.path.join(BASE, "harvest", "fakecheck.json")
    if os.path.exists(fc):
        data = json.load(open(fc, encoding="utf-8"))
        fake = collections.Counter(d["verdict"] for d in data)
        fake["sample"] = len(data)

    return {
        "total_valid": len(rows),
        "total_raw": raw_total,
        "online_claimed": online,
        "online_confirmed": confirmed,
        "slots": slots,
        "active": sum(1 for r in rows if r["players"] > 0),
        "empty": sum(1 for r in rows if r["players"] == 0),
        "locked": sum(1 for r in rows if r["password"]),
        "pirate_count": len(pirate),
        "pirate_online": sum(r["players"] for r in pirate),
        "unique_ips": len(ips),
        "unique_nets": len(nets),
        "unique_ip_name": ip_name,
        "farm_ips": len(farm_ips),
        "servers_on_farms": len(on_farm),
        "servers_off_farms": len(off_farm),
        "ips_off_farms": len({r["ip"] for r in off_farm}),
        "online_off_farm_claimed": sum(r["players"] for r in off_farm),
        "online_off_farm_confirmed": sum(r["real_players"] or 0 for r in off_farm),
        "online_farm_claimed": sum(r["players"] for r in on_farm),
        "top_maps": maps.most_common(15),
        "top_map_servers": map_srv.most_common(15),
        "top_nets": nets.most_common(15),
        "top_ips": ips.most_common(10),
        "top_ports": ports.most_common(12),
        "os": dict(env),
        "top_brands": brands.most_common(20),
        "fakecheck": dict(fake),
        "unique_names": len(names),
        "cloned_servers": cloned_servers,
        "top_clones": clones,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="save data to JSON")
    a = ap.parse_args()
    d = build()

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

    print(f"""
===== CS 1.6 SERVERS — SUMMARY =====
total records in db  : {d['total_raw']}
of them valid        : {d['total_valid']}   (the rest are broken responses like players=255)
unique IPs           : {d['unique_ips']}  in {d['unique_nets']} /24 subnets
--- honest count (port farms distort everything above) ---
port farms (>20/IP)  : {d['farm_ips']} IPs hold {d['servers_on_farms']} \"servers\"
outside farms        : {d['servers_off_farms']} servers on {d['ips_off_farms']} IPs
unique (IP,name)     : {d['unique_ip_name']}
non-empty            : {d['active']}   empty: {d['empty']}   with password: {d['locked']}
online claimed       : {d['online_claimed']}  out of {d['slots']} slots
online confirmed     : {d['online_confirmed']}  (by name via A2S_PLAYERS)
non-steam (pirated)  : {d['pirate_count']}  with online {d['pirate_online']}
""")
    if d["fakecheck"]:
        f = d["fakecheck"]
        print(f"padding check (sample {f.get('sample')}): "
              f"real={f.get('real',0)} suspect={f.get('suspect',0)} "
              f"fake={f.get('fake',0)} unknown={f.get('unknown',0)}\n")
    print(f"unique names         : {d['unique_names']}  "
          f"(in clone farms: {d['cloned_servers']} servers)")
    print()
    print("TOP clones:")
    for n, c in d["top_clones"][:6]:
        print(f"   {c:>4}x  {n[:56]}")
    print()
    print("TOP maps by players:")
    for m, c in d["top_maps"][:10]:
        print(f"   {m:<24} {c}")
    print("\nTOP subnets (servers):")
    for n, c in d["top_nets"][:10]:
        print(f"   {n:<20} {c}")
    print("\nTOP ports:")
    print("  ", ", ".join(f"{p}:{c}" for p, c in d["top_ports"]))
    print("\nServer OS:", d["os"])
    if a.json:
        print(f"\ndata saved to {a.json}")


if __name__ == "__main__":
    main()
