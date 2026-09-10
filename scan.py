#!/usr/bin/env python3
"""
scan.py — console scanner (same core as the web front-end, without a browser).

  python scan.py                        # all sources, only non-Steam servers in the report
  python scan.py --all                  # including Steam servers
  python scan.py --servers 1.2.3.4:27015 --json
  python scan.py --no-masters           # only seeds.txt + urls
  python scan.py --csv out.csv
"""

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor

import a2s
import discovery
import store


def main():
    p = argparse.ArgumentParser(description="CS 1.6 server scanner (non-Steam)")
    p.add_argument("--servers", nargs="+", help="query only these ip:port")
    p.add_argument("--region", default="world", choices=list(discovery.REGIONS))
    p.add_argument("--limit", type=int, default=3000)
    p.add_argument("--threads", type=int, default=128)
    p.add_argument("--timeout", type=float, default=1.5)
    p.add_argument("--no-masters", action="store_true")
    p.add_argument("--no-seeds", action="store_true")
    p.add_argument("--no-urls", action="store_true")
    p.add_argument("--url", action="append", help="extra URL source of addresses")
    p.add_argument("--all", action="store_true", help="also show Steam servers")
    p.add_argument("--json", metavar="FILE", nargs="?", const="cs16.json")
    p.add_argument("--csv", metavar="FILE", nargs="?", const="cs16.csv")
    p.add_argument("--no-db", action="store_true", help="do not write to cs16.db")
    a = p.parse_args()

    err = lambda m: print(m, file=sys.stderr)

    if a.servers:
        addrs = a.servers
    else:
        addrs = discovery.collect(
            region=a.region, limit=a.limit,
            use_masters=not a.no_masters, use_seeds=not a.no_seeds,
            use_urls=not a.no_urls, extra_urls=a.url, log=err)
    err(f"addresses to query: {len(addrs)}")
    if not addrs:
        err("empty: add addresses to seeds.txt or sources to sources.json")
        return

    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=a.threads) as ex:
        for r in ex.map(lambda x: a2s.probe(x, timeout=a.timeout), addrs):
            done += 1
            if r:
                rows.append(r)
            if done % 250 == 0:
                err(f"  {done}/{len(addrs)}, alive {len(rows)}")

    if not a.no_db:
        store.init()
        store.upsert_many(rows)

    shown = rows if a.all else [r for r in rows if r["pirate"]]
    shown.sort(key=lambda r: -r.get("players", 0))

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(shown, f, ensure_ascii=False, indent=2)
    if a.csv:
        cols = ["addr", "name", "map", "players", "max_players", "password",
                "vac", "pirate", "ping_ms", "environment", "ts"]
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(shown)

    online = sum(r.get("players", 0) for r in shown)
    slots = sum(r.get("max_players", 0) for r in shown)
    maps = {}
    for r in shown:
        maps[r.get("map") or "?"] = maps.get(r.get("map") or "?", 0) + r.get("players", 0)

    print(f"\n alive servers : {len(rows)} of {len(addrs)}"
          f" (non-Steam {sum(r['pirate'] for r in rows)})")
    print(f" players online : {online} / {slots} slots"
          + (f" ({online / slots * 100:.1f}%)" if slots else ""))
    print("\n TOP maps:")
    for m, c in sorted(maps.items(), key=lambda x: -x[1])[:10]:
        print(f"   {m:<22} {c}")
    print("\n TOP servers:")
    for r in shown[:15]:
        print(f"   {r['players']:>3}/{r['max_players']:<3} {(r.get('map') or ''):<16}"
              f" {r['addr']:<22} {(r.get('name') or '')[:45]}")
    if a.json or a.csv:
        print(f"\n saved: {a.json or ''} {a.csv or ''}")


if __name__ == "__main__":
    main()
