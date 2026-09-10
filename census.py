#!/usr/bin/env python3
"""
census.py — one full collection round, end to end:

  1. addresses: everything already in cs16.db + fresh lists from the master servers
     (sources.json) + seeds.txt;
  2. liveness: one async UDP sweep over all of them (sweep.Sweeper, a couple of minutes
     for ~120k targets);
  3. full probe of the live ones — A2S_INFO + A2S_RULES + A2S_PLAYERS, non-CS1.6
     neighbours filtered out, results upserted in batches;
  4. geo backfill for new IPs (Team Cymru, cached in geo_ip).

  python census.py --tag round7                # log goes to stderr, tee it into logs_round7.txt
  python census.py --no-masters --pps 3000     # re-probe known servers only
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# Windows consoles default to cp1252 while server names are UTF-8: never crash on print
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

import a2s        # noqa: E402
import discovery  # noqa: E402
import store      # noqa: E402
import sweep      # noqa: E402

T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:7.0f}s] {msg}", file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser(description="Full CS 1.6 census round")
    ap.add_argument("--tag", default=time.strftime("round_%Y%m%d_%H%M"))
    ap.add_argument("--no-masters", action="store_true", help="skip master-server discovery")
    ap.add_argument("--pps", type=int, default=4000, help="sweep packets per second")
    ap.add_argument("--rounds", type=int, default=2, help="sweep passes (UDP gets lost)")
    ap.add_argument("--threads", type=int, default=300, help="full-probe threads")
    ap.add_argument("--timeout", type=float, default=1.5)
    ap.add_argument("--batch", type=int, default=2000, help="probe/upsert batch size")
    a = ap.parse_args()

    harvest = os.path.join(BASE, "harvest")
    os.makedirs(harvest, exist_ok=True)
    store.init()

    # ---------------------------------------------------------------- 1. addresses
    known = store.known_addrs()
    addrs = set(known)
    log(f"known addresses in cs16.db: {len(known)}")
    if not a.no_masters:
        fresh = discovery.collect(use_masters=True, use_seeds=True, use_urls=False, log=log)
        new = set(fresh) - addrs
        log(f"discovery: {len(fresh)} addresses, {len(new)} new")
        addrs |= new
    pairs = []
    for x in addrs:
        ip, _, port = x.rpartition(":")
        try:
            pairs.append((ip, int(port)))
        except ValueError:
            pass
    with open(os.path.join(harvest, f"{a.tag}_targets.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(addrs)) + "\n")

    # ---------------------------------------------------------------- 2. liveness
    log(f"sweep: {len(pairs)} targets x {a.rounds} rounds at {a.pps} pps")
    sw = sweep.Sweeper(pps=a.pps, quiet=True)
    found = sw.run(lambda: iter(pairs), rounds=a.rounds)
    alive = sorted(found)
    log(f"sweep done: sent {sw.sent}, replies {sw.replies}, alive {len(alive)}")
    with open(os.path.join(harvest, f"{a.tag}_alive.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(alive) + "\n")

    # ---------------------------------------------------------------- 3. full probe
    stats = {"probed": 0, "replied": 0, "cs16": 0, "pirate": 0, "players": 0, "named": 0}
    with ThreadPoolExecutor(a.threads) as ex:
        for i in range(0, len(alive), a.batch):
            chunk = alive[i:i + a.batch]
            rows = []
            for r in ex.map(lambda x: a2s.probe(x, timeout=a.timeout), chunk):
                stats["probed"] += 1
                if not r:
                    continue
                stats["replied"] += 1
                if not r.get("cs16"):
                    continue
                rows.append(r)
            if rows:
                store.upsert_many(rows, with_geo=False)
            stats["cs16"] += len(rows)
            stats["pirate"] += sum(1 for r in rows if r["pirate"])
            stats["players"] += sum(r.get("players", 0) for r in rows)
            stats["named"] += sum(len(r.get("player_list", [])) for r in rows)
            log(f"probe {stats['probed']}/{len(alive)}: replied {stats['replied']}, "
                f"cs16 {stats['cs16']} (non-Steam {stats['pirate']}), "
                f"players claimed {stats['players']}, named {stats['named']}")

    # ---------------------------------------------------------------- 4. geo
    try:
        import geo
        n = geo.backfill(verbose=False)
        log(f"geo: country/ASN filled for {n} rows")
    except Exception as e:  # noqa: BLE001 — geo.py is optional
        log(f"geo skipped: {type(e).__name__}: {e}")

    log("=== ROUND DONE ===")
    log(f"targets {len(pairs)} -> alive {len(alive)} -> replied {stats['replied']} -> "
        f"CS 1.6 {stats['cs16']} (non-Steam {stats['pirate']}); "
        f"players claimed {stats['players']}, named {stats['named']}")


if __name__ == "__main__":
    main()
