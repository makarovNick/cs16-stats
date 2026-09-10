#!/usr/bin/env python3
"""
export_data.py — clean database slices for the repository and the article.

Puts into data/:
  servers.csv        — all valid servers (without player lists and cvars)
  servers.json       — the same in JSON
  pirate.csv         — non-Steam only
  stats.json         — aggregates for the dashboard/article (from report.py)
  fakecheck.json     — a copy of the fake-count check result, if present

Player lists (nicknames) are NOT exported into the public slices — only the
real_players aggregate (how many names the server returned). The full cs16.db
with player_list is placed into the repository separately as-is, if so decided.
"""

import csv
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import store    # noqa: E402
import report   # noqa: E402

DATA = os.path.join(BASE, "data")
COLS = ["addr", "ip", "port", "name", "map", "game", "players", "max_players",
        "real_players", "bots", "password", "vac", "pirate", "ping_ms",
        "environment", "proto_kind", "first_seen", "last_seen", "seen_count",
        "peak_players"]
VALID = "players <= max_players AND max_players <= 64"


def main():
    os.makedirs(DATA, exist_ok=True)
    con = store.connect()
    rows = [dict(r) for r in con.execute(
        f"SELECT {','.join(COLS)} FROM servers WHERE {VALID} ORDER BY players DESC")]

    with open(os.path.join(DATA, "servers.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(DATA, "servers.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    with open(os.path.join(DATA, "pirate.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows([r for r in rows if r["pirate"]])

    stats = report.build()
    with open(os.path.join(DATA, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    fc = os.path.join(BASE, "harvest", "fakecheck.json")
    if os.path.exists(fc):
        with open(fc, encoding="utf-8") as src, \
             open(os.path.join(DATA, "fakecheck.json"), "w", encoding="utf-8") as dst:
            dst.write(src.read())

    print(f"exported: {len(rows)} servers "
          f"({sum(1 for r in rows if r['pirate'])} non-Steam) → {DATA}")


if __name__ == "__main__":
    main()
