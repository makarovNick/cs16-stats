#!/usr/bin/env python3
"""
asn_expand.py — from known servers to the full IP ranges of their hosters.

Logic: CS 1.6 servers cluster around a dozen or so game hosting providers (MyArena,
Fozzy, Aeza, Hostkey, etc.). Knowing the IP of even one server, via RIPEstat
(public API, no key) we get the ASN of that address and ALL prefixes it
announces — this is the map of where to look for the remaining servers.

  python asn_expand.py                       # ASN by addresses from cs16.db
  python asn_expand.py --top 12 --max-nets 400 --out harvest/asn_nets.txt

Next, the found /24 ranges are fed to sweep.py:
  python sweep.py --nets $(cat harvest/asn_nets.txt) --anchors
"""

import argparse
import collections
import ipaddress
import json
import os
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

UA = {"User-Agent": "cs16-stats/1.0 (research)"}


def get_json(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"  ! {type(e).__name__} on {url[:70]}", file=sys.stderr)
        return None


def asn_of(ip):
    """ASN and prefix holder for an IP (RIPEstat prefix-overview)."""
    d = get_json(f"https://stat.ripe.net/data/prefix-overview/data.json?resource={ip}")
    if not d:
        return None, None, None
    data = d.get("data", {})
    asns = data.get("asns") or []
    if not asns:
        return None, None, None
    return asns[0].get("asn"), asns[0].get("holder"), data.get("resource")


def prefixes_of(asn):
    """All IPv4 prefixes announced by the ASN."""
    d = get_json(f"https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS{asn}")
    if not d:
        return []
    out = []
    for p in d.get("data", {}).get("prefixes", []):
        pfx = p.get("prefix", "")
        if ":" in pfx:
            continue  # we don't need IPv6: CS 1.6 doesn't support it
        try:
            out.append(ipaddress.ip_network(pfx, strict=False))
        except ValueError:
            pass
    return out


def main():
    ap = argparse.ArgumentParser(description="Expand the search to all hoster prefixes")
    ap.add_argument("--top", type=int, default=15, help="how many ASN to take (by number of servers)")
    ap.add_argument("--max-nets-per-asn", type=int, default=256,
                    help="max /24 from a single ASN (protection against huge prefixes)")
    ap.add_argument("--max-nets", type=int, default=3000, help="overall /24 limit")
    ap.add_argument("--out", default="harvest/asn_nets.txt")
    ap.add_argument("--report", default="harvest/asn_report.txt")
    a = ap.parse_args()

    import store
    with store.connect() as con:
        rows = [(r["ip"], r["players"]) for r in
                con.execute("SELECT ip, players FROM servers")]
    if not rows:
        sys.exit("cs16.db is empty — collect at least some addresses first")

    # one representative per /24, so as not to hit RIPEstat more than necessary
    by_net = {}
    for ip, players in rows:
        try:
            net = str(ipaddress.ip_network(f"{ip}/24", strict=False))
        except ValueError:
            continue
        by_net.setdefault(net, ip)
    print(f"known /24: {len(by_net)} — determining ASN", file=sys.stderr)

    asn_info, asn_count = {}, collections.Counter()
    for i, (net, ip) in enumerate(sorted(by_net.items()), 1):
        asn, holder, pfx = asn_of(ip)
        if asn:
            asn_count[asn] += 1
            asn_info[asn] = holder
            print(f"  [{i}/{len(by_net)}] {net:<20} AS{asn}  {holder}", file=sys.stderr)
        time.sleep(0.25)  # be polite to the public API

    top = asn_count.most_common(a.top)
    print(f"\nTOP ASN by number of subnets with servers:", file=sys.stderr)
    for asn, cnt in top:
        print(f"  AS{asn:<8} {cnt:>3} subnets  {asn_info.get(asn)}", file=sys.stderr)

    nets, report = [], []
    for asn, cnt in top:
        pfxs = prefixes_of(asn)
        got = []
        for p in sorted(pfxs, key=lambda x: x.num_addresses, reverse=True):
            if p.prefixlen >= 24:
                got.append(str(p if p.prefixlen == 24 else p.supernet(new_prefix=24)))
            else:
                # split a large prefix into /24, but no more than the limit
                for sub in p.subnets(new_prefix=24):
                    got.append(str(sub))
                    if len(got) >= a.max_nets_per_asn:
                        break
            if len(got) >= a.max_nets_per_asn:
                break
        got = sorted(set(got))[:a.max_nets_per_asn]
        report.append(f"AS{asn} {asn_info.get(asn)}: prefixes {len(pfxs)}, taken /24: {len(got)}")
        print(f"  AS{asn}: {len(pfxs)} prefixes → {len(got)} subnets /24", file=sys.stderr)
        nets.extend(got)
        time.sleep(0.25)

    nets = sorted(set(nets))[:a.max_nets]
    os.makedirs(os.path.join(BASE, "harvest"), exist_ok=True)
    with open(os.path.join(BASE, a.out), "w", encoding="utf-8") as f:
        f.write("\n".join(nets) + "\n")
    with open(os.path.join(BASE, a.report), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    print(f"\ntotal subnets /24: {len(nets)} → {a.out}", file=sys.stderr)
    print(f"targets at 10 anchor ports: {len(nets) * 256 * 10}", file=sys.stderr)


if __name__ == "__main__":
    main()
