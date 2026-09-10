#!/usr/bin/env python3
"""
play.py — "where to go play right now".

A full database scan (21k servers) isn't needed for this: we take from cs16.db only
suitable candidates (low ping, has people, no password), re-query
exactly those and show a list with a ready-made connect command.

  python play.py                          # top 20 non-empty servers with ping up to 80 ms
  python play.py --map de_dust2 --ping 60
  python play.py --pirate --confirm       # only non-Steam, with a check for live players
  python play.py --min 8 --max-full 0.9   # where a game is already going, but free slots remain
"""

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor

# Windows consoles default to cp1252 while server names are UTF-8: never crash on print
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import a2s    # noqa: E402
import store  # noqa: E402


def candidates(args):
    """Selection from the database — cheap, without a single network request."""
    sql = ["SELECT addr, name, map, players, max_players, real_players, ping_ms, pirate",
           "FROM servers WHERE max_players <= 64 AND players <= max_players",
           "AND password = 0"]
    p = []
    if args.ping:
        # the ping in the database was taken during mass runs under load and is heavily inflated,
        # so here it's only a rough pre-filter — the final selection is done
        # by a fresh measurement in refresh()
        sql.append("AND (ping_ms IS NULL OR ping_ms <= ?)")
        p.append(args.ping * 4)
    if args.map:
        sql.append("AND map = ?")
        p.append(args.map)
    if args.pirate:
        sql.append("AND pirate = 1")
    if args.steam:
        sql.append("AND pirate = 0")
    # last time someone was there — empty servers are useless for playing
    sql.append("AND (players > 0 OR peak_players > 0)")
    sql.append("ORDER BY real_players DESC, players DESC LIMIT ?")
    p.append(args.pool)
    with store.connect() as con:
        return [dict(r) for r in con.execute(" ".join(sql), p)]


def refresh(rows, confirm, threads, timeout):
    """Fresh poll of candidates: players and ping "as of now"."""
    def one(r):
        host, _, port = r["addr"].rpartition(":")
        port = int(port)
        inf = a2s.info(host, port, timeout=timeout)
        if not inf or not a2s.is_cs16(inf):
            return None
        out = dict(r)
        out.update(name=inf["name"], map=inf["map"], players=inf["players"],
                   max_players=inf["max_players"], ping_ms=inf["ping_ms"])
        if confirm:
            out["real_players"] = len(a2s.players(host, port, timeout=timeout))
        return out

    with ThreadPoolExecutor(threads) as ex:
        return [r for r in ex.map(one, rows) if r]


def main():
    ap = argparse.ArgumentParser(description="Pick a CS 1.6 server to play on")
    ap.add_argument("--ping", type=int, default=80, help="maximum ping, ms")
    ap.add_argument("--map", help="this map only")
    ap.add_argument("--min", type=int, default=4, help="minimum players")
    ap.add_argument("--max-full", type=float, default=0.95,
                    help="don't show servers filled tighter than this fraction of slots")
    ap.add_argument("--pirate", action="store_true", help="only non-Steam")
    ap.add_argument("--steam", action="store_true", help="only Steam")
    ap.add_argument("--confirm", action="store_true",
                    help="cross-check the online count against the player list (slower, but honest)")
    ap.add_argument("--pool", type=int, default=1500, help="how many candidates to re-query")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--threads", type=int, default=300)
    ap.add_argument("--timeout", type=float, default=1.2)
    ap.add_argument("--save", action="store_true", help="write fresh data to cs16.db")
    a = ap.parse_args()

    pool = candidates(a)
    if not pool:
        sys.exit("no suitable candidates in the database — run a scan")
    print(f"candidates from the database: {len(pool)} — re-querying…", file=sys.stderr)

    live = refresh(pool, a.confirm, a.threads, a.timeout)
    good = [r for r in live
            if r["players"] >= a.min
            and r["max_players"]
            and r["players"] / r["max_players"] <= a.max_full
            and r["ping_ms"] is not None and r["ping_ms"] <= a.ping]
    key = (lambda r: (-(r.get("real_players") or 0), r["ping_ms"])) if a.confirm \
        else (lambda r: (-r["players"], r["ping_ms"]))
    good.sort(key=key)

    print(f"replied {len(live)} of {len(pool)}, suitable {len(good)}\n", file=sys.stderr)
    print(f"{'players':>9}  {'ping':>5}  {'map':<16} {'server':<40} command")
    for r in good[:a.top]:
        who = (f"{r['real_players']}/{r['max_players']}" if a.confirm
               else f"{r['players']}/{r['max_players']}")
        tag = "" if r["pirate"] else " [steam]"
        print(f"{who:>9}  {r['ping_ms']:>4}ms  {(r['map'] or '')[:16]:<16} "
              f"{((r['name'] or '')[:38] + tag):<40} connect {r['addr']}")

    if a.save and live:
        # only the fields measured here; a full upsert_many() would wipe rules,
        # player_list, vac, environment etc. that the last full scan collected
        store.update_light(live, real_players=a.confirm)


if __name__ == "__main__":
    main()
