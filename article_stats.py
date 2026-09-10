#!/usr/bin/env python3
"""
article_stats.py — numbers and charts for the article: nicknames, bots, scores,
frags per minute, server countries/cities, a proxy for player geography.

  python article_stats.py                 # data/article_stats.json + docs/img/*.png
  python article_stats.py --no-charts

Honest caveats baked in:
  * A2S has no deaths → no K/D. What exists is score (frags) and time in game,
    so we report score distribution and frags per minute.
  * A2S has no player IPs → player geography is a proxy: confirmed (named) players
    are attributed to the server's country, and nickname script (Cyrillic/Latin)
    is shown alongside.
  * "bots" comes from the Source-style 'I' reply only; GoldSrc 'm' replies have no
    such field. Bot quotas in cvars (yb_quota, pb_*, bot_quota) are the second signal.
  * Port farms (>20 ports on one IP) are separated everywhere: they hold 94% of the
    "servers" and the bulk of fake rosters.
"""

import argparse
import collections
import json
import os
import re
import sqlite3
import statistics
import sys
import unicodedata

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

DB = os.path.join(BASE, "cs16.db")
IMG = os.path.join(BASE, "docs", "img")
VALID = "players <= max_players AND max_players <= 64"
FARM = 20

AD_RE = re.compile(r"https?://|www\.|t\.me|vk\.com|discord|\.(ru|com|net|org|su|ua|pro)\b", re.I)
TAG_RE = re.compile(r"^[\[\(\{<|].*?[\]\)\}>|]|\|.*\||\[.*?\]")
BOT_KEYS = ("yb_quota", "pb_quota", "bot_quota", "zbot_quota", "sv_bot_quota",
            "yapb_quota", "ebot_quota", "pb_maxbots", "yb_maxbots")


def script_of(nick):
    """cyrillic / latin / mixed / other — by letters only, decorations ignored."""
    cyr = lat = other = 0
    for ch in nick:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        if name.startswith("CYRILLIC"):
            cyr += 1
        elif name.startswith("LATIN"):
            lat += 1
        else:
            other += 1
    if cyr and not lat:
        return "cyrillic"
    if lat and not cyr:
        return "latin"
    if cyr and lat:
        return "mixed"
    return "other" if other else "symbols"


def load():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        f"SELECT addr, ip, name, map, players, max_players, bots, real_players, pirate, "
        f"proto_kind, country, rules, player_list FROM servers WHERE {VALID}")]
    for r in rows:
        r["rules"] = json.loads(r["rules"] or "{}")
        r["player_list"] = json.loads(r["player_list"] or "[]")
    cities = {r["ip"]: dict(r) for r in con.execute("SELECT * FROM geo_city")} \
        if con.execute("SELECT name FROM sqlite_master WHERE name='geo_city'").fetchone() else {}
    return rows, cities


def hist(values, edges):
    out = collections.OrderedDict()
    for lo, hi in zip(edges, edges[1:] + [None]):
        label = f"{lo}–{hi - 1}" if hi else f"{lo}+"
        out[label] = sum(1 for v in values if v >= lo and (hi is None or v < hi))
    return out


def build(rows, cities):
    ips = collections.Counter(r["ip"] for r in rows)
    farm_ips = {ip for ip, c in ips.items() if c > FARM}
    for r in rows:
        r["farm"] = r["ip"] in farm_ips
    off = [r for r in rows if not r["farm"]]
    on = [r for r in rows if r["farm"]]

    # ------------------------------------------------------------ nicknames
    nick_all = collections.Counter()
    nick_off = collections.Counter()
    scripts = collections.Counter()
    scripts_off = collections.Counter()
    ads = ads_off = 0
    tags = 0
    lengths = []
    entries = entries_off = 0
    for r in rows:
        for p in r["player_list"]:
            n = p["name"]
            entries += 1
            nick_all[n] += 1
            sc = script_of(n)
            scripts[sc] += 1
            is_ad = bool(AD_RE.search(n))
            ads += is_ad
            if not r["farm"]:
                entries_off += 1
                nick_off[n] += 1
                scripts_off[sc] += 1
                ads_off += is_ad
                tags += bool(TAG_RE.search(n))
                lengths.append(len(n))
    # the same roster copied across many servers = fake list fingerprint
    roster_sig = collections.Counter()
    for r in rows:
        if len(r["player_list"]) >= 4:
            roster_sig[tuple(sorted(p["name"] for p in r["player_list"]))] += 1
    cloned_rosters = sum(c for c in roster_sig.values() if c > 1)
    nick_clean = collections.Counter()      # off-farm, rosters that exist on one server only
    for r in off:
        sig = tuple(sorted(p["name"] for p in r["player_list"]))
        if len(sig) >= 4 and roster_sig[sig] > 1:
            continue
        for p in r["player_list"]:
            nick_clean[p["name"]] += 1
    top_cloned = [(c, list(sig)[:4]) for sig, c in roster_sig.most_common(5) if c > 1]

    # ------------------------------------------------------------ bots
    with_bots = [r for r in rows if (r["bots"] or 0) > 0]
    bots_total = sum(r["bots"] or 0 for r in rows)
    claimed = sum(r["players"] for r in rows)
    quota = collections.Counter()
    quota_servers = 0
    for r in off:
        hit = [k for k in BOT_KEYS if k in r["rules"]]
        if hit:
            quota_servers += 1
            for k in hit:
                quota[k] += 1
    off_with_bots = [r for r in off if (r["bots"] or 0) > 0]
    bots_hist = hist([r["bots"] for r in off_with_bots], [1, 3, 5, 10, 20, 32])
    farm_bots = sum(r["bots"] or 0 for r in on)
    farm_claimed = sum(r["players"] for r in on)

    # ------------------------------------------------------------ scores & time
    scores = []
    fpm = []           # frags per minute for sessions >= 5 min
    zero = 0
    times = []
    for r in off:      # farms fake everything, keep the honest slice
        for p in r["player_list"]:
            s, t = p["score"], p["time_sec"]
            scores.append(s)
            times.append(t)
            zero += s == 0
            # 5 min .. 3 h: enough play to judge, and below the "42-hour session" fakes
            if 300 <= t <= 3 * 3600 and s >= 0:
                fpm.append(s / (t / 60))
    scores_sorted = sorted(scores)

    def pct(v, q):
        return v[int(len(v) * q) - 1] if v else 0

    score_stats = {
        "entries": len(scores),
        "zero_share": zero / len(scores) if scores else 0,
        "median": statistics.median(scores) if scores else 0,
        "mean": statistics.mean(scores) if scores else 0,
        "p90": pct(scores_sorted, 0.9), "p99": pct(scores_sorted, 0.99),
        "max": max(scores) if scores else 0,
        "negative": sum(1 for s in scores if s < 0),
        "hist": hist(scores, [0, 1, 5, 10, 20, 40, 80, 150, 300]),
    }
    fpm_sorted = sorted(fpm)
    fpm_stats = {
        "sessions": len(fpm),
        "median": statistics.median(fpm) if fpm else 0,
        "p90": pct(fpm_sorted, 0.9),
        "hist": {k: v for k, v in zip(
            ["0", "0–0.5", "0.5–1", "1–2", "2–4", "4–8", "8+"],
            [sum(1 for x in fpm if x == 0),
             sum(1 for x in fpm if 0 < x < 0.5), sum(1 for x in fpm if 0.5 <= x < 1),
             sum(1 for x in fpm if 1 <= x < 2), sum(1 for x in fpm if 2 <= x < 4),
             sum(1 for x in fpm if 4 <= x < 8), sum(1 for x in fpm if x >= 8)])},
    }
    time_hist = hist([t / 60 for t in times], [0, 5, 15, 30, 60, 120, 240, 720])
    marathon = sum(1 for t in times if t > 12 * 3600)   # > 12 h "in game" = fake roster

    # ------------------------------------------------------------ geography
    def by_country(subset):
        c = collections.Counter()
        ip_c = collections.defaultdict(set)
        ppl = collections.Counter()
        for r in subset:
            cc = r["country"] or "??"
            c[cc] += 1
            ip_c[cc].add(r["ip"])
            ppl[cc] += r["real_players"] or 0
        return [{"cc": cc, "servers": n, "ips": len(ip_c[cc]), "confirmed_players": ppl[cc]}
                for cc, n in c.most_common()]
    countries_off = by_country(off)
    countries_all = by_country(rows)
    city_srv = collections.Counter()
    city_ips = collections.defaultdict(set)
    city_ppl = collections.Counter()
    for r in off:
        g = cities.get(r["ip"])
        if not g or not g.get("city"):
            continue
        key = f"{g['city']}, {g['country']}"
        city_srv[key] += 1
        city_ips[key].add(r["ip"])
        city_ppl[key] += r["real_players"] or 0
    top_cities = sorted(({"city": k, "servers": n, "ips": len(city_ips[k]),
                          "confirmed_players": city_ppl[k]} for k, n in city_srv.items()),
                        key=lambda c: -c["ips"])[:25]
    isp = collections.Counter()
    for r in off:
        g = cities.get(r["ip"])
        if g and g.get("isp"):
            isp[g["isp"]] += 1

    # player geography proxy: confirmed players by server country x nickname script
    cc_script = collections.defaultdict(collections.Counter)
    for r in off:
        cc = r["country"] or "??"
        for p in r["player_list"]:
            cc_script[cc][script_of(p["name"])] += 1
    players_by_cc = sorted(((sum(v.values()), cc, dict(v)) for cc, v in cc_script.items()),
                           reverse=True)

    return {
        "servers_valid": len(rows), "servers_off_farm": len(off), "servers_on_farm": len(on),
        "farm_ips": len(farm_ips), "unique_ips": len(ips),
        "nicknames": {
            "entries_all": entries, "entries_off_farm": entries_off,
            "unique_all": len(nick_all), "unique_off_farm": len(nick_off),
            "top_all": nick_all.most_common(30), "top_off_farm": nick_off.most_common(30),
            "top_clean": nick_clean.most_common(30),
            "script_all": dict(scripts), "script_off_farm": dict(scripts_off),
            "ads_all": ads, "ads_off_farm": ads_off, "clan_tags_off_farm": tags,
            "length_median": statistics.median(lengths) if lengths else 0,
            "length_hist": hist(lengths, [1, 4, 8, 12, 16, 24, 32]),
            "cloned_rosters": cloned_rosters, "top_cloned_rosters": top_cloned,
        },
        "bots": {
            "servers_reporting_bots": len(with_bots), "bots_claimed_total": bots_total,
            "players_claimed_total": claimed,
            "bots_share_of_claimed": bots_total / claimed if claimed else 0,
            "servers_off_farm_with_bots": len(off_with_bots),
            "bots_off_farm": sum(r["bots"] for r in off_with_bots),
            "players_off_farm": sum(r["players"] for r in off),
            "bots_per_server_hist": bots_hist,
            "bots_on_farms": farm_bots, "players_claimed_on_farms": farm_claimed,
            "quota_cvar_servers_off_farm": quota_servers, "quota_cvars": dict(quota),
            "kind_m_no_bot_field": sum(1 for r in rows if r["proto_kind"] == "m"),
        },
        "scores": score_stats, "frags_per_minute": fpm_stats, "session_minutes_hist": time_hist,
        "sessions_over_12h": marathon, "sessions_total_off_farm": len(times),
        "countries_off_farm": countries_off[:25], "countries_all": countries_all[:25],
        "cities_off_farm": top_cities, "isp_off_farm": isp.most_common(15),
        "players_by_server_country": [
            {"cc": cc, "confirmed_players": n, "scripts": sc} for n, cc, sc in players_by_cc[:20]],
        "kd_note": "A2S_PLAYERS carries frags and time only; deaths are not exposed, so K/D "
                   "cannot be computed from the protocol.",
    }


# ------------------------------------------------------------------ charts

def charts(d):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    os.makedirs(IMG, exist_ok=True)
    plt.rcParams.update({"font.family": ["DejaVu Sans", "Microsoft YaHei", "Segoe UI Symbol",
                                         "Segoe UI Emoji"], "figure.dpi": 150,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.grid": True, "grid.alpha": 0.25})
    C1, C2, C3 = "#2f6fdb", "#e0793d", "#8a8f98"

    def save(fig, name):
        fig.tight_layout()
        fig.savefig(os.path.join(IMG, name), bbox_inches="tight")
        plt.close(fig)

    # 1. nickname scripts, farms vs not
    fig, ax = plt.subplots(figsize=(7, 3.2))
    keys = ["latin", "cyrillic", "mixed", "symbols", "other"]
    a = d["nicknames"]["script_off_farm"]; b = d["nicknames"]["script_all"]
    ta = sum(a.values()) or 1; tb = sum(b.values()) or 1
    x = range(len(keys))
    ax.bar([i - 0.2 for i in x], [a.get(k, 0) / ta * 100 for k in keys], 0.4, label="outside port farms", color=C1)
    ax.bar([i + 0.2 for i in x], [b.get(k, 0) / tb * 100 for k in keys], 0.4, label="all rosters", color=C3)
    ax.set_xticks(list(x)); ax.set_xticklabels(keys); ax.set_ylabel("% of nicknames")
    ax.set_title("Nickname alphabet"); ax.legend()
    save(fig, "nick_scripts.png")

    # 2. top nicknames outside farms
    fig, ax = plt.subplots(figsize=(7, 5))
    top = d["nicknames"]["top_clean"][:20][::-1]
    ax.barh([n[:28] or "<empty>" for n, _ in top], [c for _, c in top], color=C1)
    ax.set_title("Most common nicknames (outside port farms, cloned rosters removed)")
    ax.set_xlabel("players")
    save(fig, "nick_top.png")

    # 3. score distribution
    fig, ax = plt.subplots(figsize=(7, 3.2))
    h = d["scores"]["hist"]
    ax.bar(list(h.keys()), [v / d["scores"]["entries"] * 100 for v in h.values()], color=C1)
    ax.set_ylabel("% of players"); ax.set_xlabel("frags (score) at the moment of the poll")
    ax.set_title(f"Score distribution — median {d['scores']['median']:.0f}, "
                 f"p90 {d['scores']['p90']}, zero {d['scores']['zero_share'] * 100:.0f}%")
    save(fig, "score_hist.png")

    # 4. frags per minute
    fig, ax = plt.subplots(figsize=(7, 3.2))
    h = d["frags_per_minute"]["hist"]
    ax.bar(list(h.keys()), [v / max(1, d["frags_per_minute"]["sessions"]) * 100 for v in h.values()], color=C2)
    ax.set_ylabel("% of sessions 5 min – 3 h"); ax.set_xlabel("frags per minute")
    ax.set_title(f"Frags per minute — median {d['frags_per_minute']['median']:.2f}")
    save(fig, "fpm_hist.png")

    # 5. session length
    fig, ax = plt.subplots(figsize=(7, 3.2))
    h = d["session_minutes_hist"]
    tot = sum(h.values()) or 1
    ax.bar(list(h.keys()), [v / tot * 100 for v in h.values()], color=C3)
    ax.set_ylabel("% of players"); ax.set_xlabel("minutes on the server")
    ax.set_title(f"Time on the server — {d['sessions_over_12h']:,} of "
                 f"{d['sessions_total_off_farm']:,} 'players' are in for 12 h+")
    save(fig, "session_hist.png")

    # 6. bots
    fig, ax = plt.subplots(figsize=(7, 3.2))
    h = d["bots"]["bots_per_server_hist"]
    ax.bar(list(h.keys()), list(h.values()), color=C2)
    ax.set_ylabel("servers outside port farms"); ax.set_xlabel("bots reported by the server")
    b = d["bots"]
    ax.set_title(f"Bots outside farms: {b['bots_off_farm']:,} of {b['players_off_farm']:,} "
                 f"claimed ({b['bots_off_farm'] / max(1, b['players_off_farm']) * 100:.0f}%), "
                 f"on farms {b['bots_on_farms']:,} of {b['players_claimed_on_farms']:,} "
                 f"({b['bots_on_farms'] / max(1, b['players_claimed_on_farms']) * 100:.0f}%)",
                 fontsize=9)
    save(fig, "bots_hist.png")

    # 7. countries: servers (IPs) and confirmed players, outside farms
    fig, ax = plt.subplots(figsize=(8, 4.5))
    cs = d["countries_off_farm"][:15]
    x = range(len(cs))
    ax.bar([i - 0.2 for i in x], [c["ips"] for c in cs], 0.4, label="unique IPs", color=C1)
    ax2 = ax.twinx()
    ax2.bar([i + 0.2 for i in x], [c["confirmed_players"] for c in cs], 0.4, label="confirmed players", color=C2)
    ax.set_xticks(list(x)); ax.set_xticklabels([c["cc"] for c in cs])
    ax.set_ylabel("unique server IPs"); ax2.set_ylabel("confirmed (named) players"); ax2.grid(False)
    ax.set_title("Where the servers are (outside port farms)")
    h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper right")
    save(fig, "countries.png")

    # 8. cities
    if d["cities_off_farm"]:
        fig, ax = plt.subplots(figsize=(7, 5))
        cs = d["cities_off_farm"][:20][::-1]
        ax.barh([c["city"] for c in cs], [c["ips"] for c in cs], color=C1)
        ax.set_xlabel("unique server IPs"); ax.set_title("Hosting cities (outside port farms)")
        save(fig, "cities.png")

    # 9. player geography proxy: stacked scripts by server country
    fig, ax = plt.subplots(figsize=(8, 4))
    pc = d["players_by_server_country"][:12]
    labels = [p["cc"] for p in pc]
    bottom = [0] * len(pc)
    for key, color in (("cyrillic", C2), ("latin", C1), ("mixed", "#9b59b6"), ("symbols", C3), ("other", "#444")):
        vals = [p["scripts"].get(key, 0) for p in pc]
        ax.bar(labels, vals, bottom=bottom, label=key, color=color)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_ylabel("confirmed players"); ax.legend(title="nickname alphabet")
    ax.set_title("Players by server country and nickname alphabet (proxy for audience geography)")
    save(fig, "players_geo_proxy.png")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-charts", action="store_true")
    ap.add_argument("--json", default=os.path.join(BASE, "data", "article_stats.json"))
    a = ap.parse_args()
    rows, cities = load()
    d = build(rows, cities)
    with open(a.json, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, default=str)
    n = d["nicknames"]; b = d["bots"]; s = d["scores"]
    print(f"valid servers {d['servers_valid']} (off-farm {d['servers_off_farm']}, "
          f"on {d['farm_ips']} farm IPs {d['servers_on_farm']})")
    print(f"nick entries {n['entries_all']} / unique {n['unique_all']}; off-farm "
          f"{n['entries_off_farm']} / unique {n['unique_off_farm']}; ads {n['ads_off_farm']}, "
          f"clan tags {n['clan_tags_off_farm']}, cloned rosters {n['cloned_rosters']}")
    print(f"scripts off-farm: {n['script_off_farm']}")
    print(f"top nicks off-farm: {n['top_off_farm'][:10]}")
    print(f"bots: {b['servers_reporting_bots']} servers report {b['bots_claimed_total']} bots "
          f"({b['bots_share_of_claimed'] * 100:.1f}% of claimed online); off-farm "
          f"{b['servers_off_farm_with_bots']} servers, {b['bots_off_farm']} bots of "
          f"{b['players_off_farm']}; quota cvars on {b['quota_cvar_servers_off_farm']} "
          f"servers {b['quota_cvars']}")
    print(f"scores (off-farm): n={s['entries']} zero={s['zero_share'] * 100:.0f}% median={s['median']} "
          f"p90={s['p90']} p99={s['p99']} max={s['max']} negative={s['negative']}")
    print(f"frags/min: {d['frags_per_minute']['sessions']} sessions, median "
          f"{d['frags_per_minute']['median']:.2f}, p90 {d['frags_per_minute']['p90']:.2f}")
    print("countries off-farm:", [(c['cc'], c['ips'], c['confirmed_players']) for c in d['countries_off_farm'][:10]])
    print("cities off-farm:", [(c['city'], c['ips']) for c in d['cities_off_farm'][:10]])
    print("players by server country:", [(p['cc'], p['confirmed_players']) for p in d['players_by_server_country'][:8]])
    if not a.no_charts:
        charts(d)
        print(f"charts -> {IMG}")


if __name__ == "__main__":
    main()
