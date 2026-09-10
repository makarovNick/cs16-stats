#!/usr/bin/env python3
"""
fakecheck.py — check whether the players on a server are real or the count is faked.

Pirate servers massively inflate their online count (fake players): A2S_PLAYERS
returns a plausible list that does not actually exist. The only way to tell is a
repeated measurement: for real players the in-game time grows by exactly the
elapsed interval, the score changes, and the roster gets slightly refreshed.

  python fakecheck.py --sample 300 --gap 70

Verdicts:
  real     — time grew roughly by the interval, there is movement
  fake     — the list did not change at all, or time is frozen/jumps around
  suspect  — all scores are zero or all players have the same time
  unknown  — the server did not return the list the second time
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import a2s      # noqa: E402
import store    # noqa: E402


def snapshot(addr, timeout=2.5):
    host, _, port = addr.rpartition(":")
    return a2s.players(host, int(port), timeout=timeout)


def verdict(first, second, gap):
    if not first or not second:
        return "unknown"
    a = {p["name"]: p for p in first}
    b = {p["name"]: p for p in second}
    common = set(a) & set(b)
    if not common:
        return "unknown" if not b else "suspect"

    grew = 0
    for n in common:
        delta = b[n]["time_sec"] - a[n]["time_sec"]
        if gap * 0.5 <= delta <= gap * 2.0:   # time advances as it should
            grew += 1
    frac = grew / len(common)

    scores_all_zero = all(p["score"] == 0 for p in second)
    times_identical = len({round(p["time_sec"]) for p in second}) <= 2 and len(second) > 4

    if frac >= 0.6:
        return "suspect" if (scores_all_zero and len(second) > 6) else "real"
    if frac <= 0.15:
        return "fake"
    return "suspect"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=300, help="how many servers to check")
    ap.add_argument("--gap", type=int, default=70, help="pause between measurements, sec")
    ap.add_argument("--threads", type=int, default=120)
    ap.add_argument("--pirate-only", action="store_true")
    ap.add_argument("--out", default="harvest/fakecheck.json")
    a = ap.parse_args()

    with store.connect() as con:
        sql = ("SELECT addr, name, players, real_players, pirate FROM servers "
               "WHERE real_players > 3 AND players <= max_players")
        if a.pirate_only:
            sql += " AND pirate = 1"
        rows = [dict(r) for r in con.execute(sql + " ORDER BY players DESC")]
    if not rows:
        sys.exit("no servers with confirmed players")

    step = max(1, len(rows) // a.sample)
    sample = rows[::step][:a.sample]
    print(f"checking {len(sample)} servers, interval {a.gap} sec", file=sys.stderr)

    with ThreadPoolExecutor(a.threads) as ex:
        first = list(ex.map(lambda r: snapshot(r["addr"]), sample))
    time.sleep(a.gap)
    with ThreadPoolExecutor(a.threads) as ex:
        second = list(ex.map(lambda r: snapshot(r["addr"]), sample))

    out, counts = [], {}
    for r, f, s in zip(sample, first, second):
        v = verdict(f, s, a.gap)
        counts[v] = counts.get(v, 0) + 1
        out.append({"addr": r["addr"], "name": r["name"], "players": r["players"],
                    "first": len(f), "second": len(s), "verdict": v,
                    "pirate": r["pirate"]})

    with open(os.path.join(BASE, a.out), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)

    total = len(out)
    print("\nRESULT:")
    for k in ("real", "suspect", "fake", "unknown"):
        n = counts.get(k, 0)
        print(f"  {k:<9} {n:>4}  ({n / total * 100:.0f}%)")
    real_players = sum(o["players"] for o in out if o["verdict"] == "real")
    all_players = sum(o["players"] for o in out)
    if all_players:
        print(f"\nshare of players on servers with confirmed activity: "
              f"{real_players / all_players * 100:.0f}%")
    print(f"details: {a.out}")


if __name__ == "__main__":
    main()
