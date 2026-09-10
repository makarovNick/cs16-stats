"""store.py — SQLite storage: current server state + online history."""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "cs16.db")
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    addr        TEXT PRIMARY KEY,
    ip          TEXT, port INTEGER,
    name        TEXT, map TEXT, game TEXT,
    players     INTEGER, max_players INTEGER, bots INTEGER,
    password    INTEGER, vac INTEGER, pirate INTEGER,
    ping_ms     INTEGER, environment TEXT, proto_kind TEXT,
    rules       TEXT, player_list TEXT,
    real_players INTEGER, fake_online INTEGER,
    country     TEXT, asn TEXT, asn_name TEXT,
    first_seen  TEXT, last_seen TEXT,
    seen_count  INTEGER DEFAULT 0,
    peak_players INTEGER DEFAULT 0,
    search      TEXT
);
CREATE TABLE IF NOT EXISTS samples (
    addr TEXT, ts TEXT, players INTEGER, map TEXT, ping_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_players ON servers(players DESC);
CREATE INDEX IF NOT EXISTS idx_srv_cc ON servers(country);
-- (pirate, players, max_players) covers the "valid + pirate" filter used by every
-- dashboard query: summary()/query() go from a full table scan to an index range.
CREATE INDEX IF NOT EXISTS idx_srv_pirate ON servers(pirate, players DESC, max_players);
-- covering index for the online-history chart: SUM(players) GROUP BY ts never
-- touches the samples rows themselves (idx_samples(addr, ts) is superseded).
DROP INDEX IF EXISTS idx_samples;
CREATE INDEX IF NOT EXISTS idx_samples_cov ON samples(addr, ts, players);
"""


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init():
    # old databases: add geo-location columns if they don't exist yet
    # (before SCHEMA, which creates an index on country)
    with connect() as con:
        cols = {c[1] for c in con.execute("PRAGMA table_info(servers)")}
        if cols:
            for c in ("country", "asn", "asn_name", "search"):
                if c not in cols:
                    con.execute(f"ALTER TABLE servers ADD COLUMN {c} TEXT")
    with connect() as con:
        con.executescript(SCHEMA)
        # refresh planner statistics when SQLite thinks they are stale —
        # without them the history query picks a plan that is ~3x slower
        con.execute("PRAGMA optimize")
    _backfill_search()


def search_text(name, map_name, addr):
    """What the dashboard search box matches against: name + map + address, casefolded
    (so 'душа' finds 'ДУША' — SQLite's own LIKE only folds ASCII)."""
    return f"{name or ''} {map_name or ''} {addr or ''}".casefold()


def _backfill_search():
    """One-off for databases created before the search column existed."""
    with _lock, connect() as con:
        rows = con.execute(
            "SELECT addr, name, map FROM servers WHERE search IS NULL").fetchall()
        if rows:
            con.executemany(
                "UPDATE servers SET search=? WHERE addr=?",
                [(search_text(r["name"], r["map"], r["addr"]), r["addr"]) for r in rows])


def _like_clauses(q, column="search"):
    """'surf dust' -> every word must occur (AND), % and _ in the input are literal."""
    sql, args = [], []
    for tok in q.casefold().split():
        esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql.append(f" AND {column} LIKE ? ESCAPE '\\'")
        args.append(f"%{esc}%")
    return "".join(sql), args


def upsert_many(rows, with_geo=True):
    """Records poll results and adds points to the online history.

    with_geo=True — also fills in country/asn/asn_name (Team Cymru cache
    in the geo_ip table, the network is only hit for unknown IPs).
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    gmap = {}
    if with_geo and rows:
        try:
            import geo
            gmap = geo.lookup({r["ip"] for r in rows if r.get("ip")})
        except Exception as e:                              # noqa: BLE001
            print(f"geo lookup skipped: {e}")
    with _lock, connect() as con:
        for r in rows:
            g = gmap.get(r.get("ip")) or {}
            con.execute(
                """
                INSERT INTO servers (addr, ip, port, name, map, game, players,
                    max_players, bots, password, vac, pirate, ping_ms,
                    environment, proto_kind, rules, player_list,
                    real_players, fake_online, country, asn, asn_name,
                    first_seen, last_seen, seen_count, peak_players, search)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
                ON CONFLICT(addr) DO UPDATE SET
                    name=excluded.name, map=excluded.map, game=excluded.game,
                    search=excluded.search,
                    real_players=excluded.real_players, fake_online=excluded.fake_online,
                    players=excluded.players, max_players=excluded.max_players,
                    bots=excluded.bots, password=excluded.password,
                    vac=excluded.vac, pirate=excluded.pirate,
                    ping_ms=excluded.ping_ms, environment=excluded.environment,
                    proto_kind=excluded.proto_kind, rules=excluded.rules,
                    player_list=excluded.player_list, last_seen=excluded.last_seen,
                    country=COALESCE(excluded.country, servers.country),
                    asn=COALESCE(excluded.asn, servers.asn),
                    asn_name=COALESCE(excluded.asn_name, servers.asn_name),
                    seen_count=servers.seen_count + 1,
                    peak_players=MAX(servers.peak_players, excluded.players)
                """,
                (r["addr"], r["ip"], r["port"], r.get("name"), r.get("map"),
                 r.get("game"), r.get("players", 0), r.get("max_players", 0),
                 r.get("bots", 0), int(bool(r.get("password"))),
                 None if r.get("vac") is None else int(r["vac"]),
                 int(bool(r.get("pirate"))), r.get("ping_ms"),
                 r.get("environment"), r.get("proto_kind"),
                 json.dumps(r.get("rules", {}), ensure_ascii=False),
                 json.dumps(r.get("player_list", []), ensure_ascii=False),
                 len(r.get("player_list", [])),
                 # inflation: player list came back, but it's noticeably shorter than claimed
                 int(bool(r.get("player_list")) and
                     len(r["player_list"]) < r.get("players", 0) * 0.6),
                 g.get("country"), g.get("asn"), g.get("asn_name"),
                 now, now, r.get("players", 0),
                 search_text(r.get("name"), r.get("map"), r["addr"])),
            )
            con.execute(
                "INSERT INTO samples (addr, ts, players, map, ping_ms) VALUES (?,?,?,?,?)",
                (r["addr"], now, r.get("players", 0), r.get("map"), r.get("ping_ms")),
            )


def update_light(rows, real_players=False):
    """Refresh only what a bare A2S_INFO poll knows (name, map, players, ping) for
    servers that already exist in the DB, plus a history sample. Unlike upsert_many()
    it leaves rules/player_list/vac/environment/geo from the last full scan intact.
    real_players=True — the rows also carry a freshly counted real_players.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    extra = ", real_players=?" if real_players else ""
    with _lock, connect() as con:
        con.executemany(
            f"""UPDATE servers SET name=?, map=?, players=?, max_players=?, ping_ms=?,
                    last_seen=?, seen_count=seen_count + 1,
                    peak_players=MAX(peak_players, ?), search=?{extra}
                WHERE addr=?""",
            [(r.get("name"), r.get("map"), r.get("players", 0), r.get("max_players", 0),
              r.get("ping_ms"), now, r.get("players", 0),
              search_text(r.get("name"), r.get("map"), r["addr"]))
             + ((r.get("real_players") or 0,) if real_players else ())
             + (r["addr"],) for r in rows],
        )
        con.executemany(
            "INSERT INTO samples (addr, ts, players, map, ping_ms) VALUES (?,?,?,?,?)",
            [(r["addr"], now, r.get("players", 0), r.get("map"), r.get("ping_ms")) for r in rows],
        )


def query(only_pirate=True, q="", map_name="", min_players=0, non_empty=False,
          no_password=False, sort="players", limit=300, only_valid=True,
          only_confirmed=False):
    sort_sql = {
        "players": "players DESC, max_players DESC",
        "ping": "ping_ms ASC",
        "name": "name COLLATE NOCASE ASC",
        "peak": "peak_players DESC",
        "real": "real_players DESC",
        "slots": "max_players DESC",
    }.get(sort, "players DESC")

    sql = "SELECT * FROM servers WHERE 1=1"
    args = []
    if only_valid:            # players=255 and similar junk from broken responses
        sql += " AND players <= max_players AND max_players <= 64"
    if only_confirmed:        # online confirmed by a named list of players
        sql += " AND real_players > 0"
    if only_pirate:
        sql += " AND pirate=1"
    if q:                     # any words, in any order, over name + map + address
        part, a = _like_clauses(q)
        sql += part
        args += a
    if map_name:              # substring: "surf" finds surf_ski_2, "dust" both dusts
        part, a = _like_clauses(map_name, column="map")
        sql += part
        args += a
    if min_players:
        sql += " AND players >= ?"
        args.append(int(min_players))
    if non_empty:
        sql += " AND players > 0"
    if no_password:
        sql += " AND password = 0"
    sql += f" ORDER BY {sort_sql} LIMIT ?"
    args.append(int(limit))

    with connect() as con:
        rows = [dict(r) for r in con.execute(sql, args)]
    for r in rows:
        r["rules"] = json.loads(r["rules"] or "{}")
        r["player_list"] = json.loads(r["player_list"] or "[]")
    return rows


def summary(only_pirate=True):
    where = ("WHERE players <= max_players AND max_players <= 64"
             + (" AND pirate=1" if only_pirate else ""))
    with connect() as con:
        s = con.execute(
            f"""SELECT COUNT(*) total, SUM(players) online, SUM(max_players) slots,
                       SUM(players>0) active, SUM(password=1) locked,
                       SUM(real_players) real_online, SUM(fake_online) faked
                FROM servers {where}"""
        ).fetchone()
        maps = con.execute(
            f"""SELECT map, COUNT(*) srv, SUM(players) ppl FROM servers {where}
                GROUP BY map ORDER BY ppl DESC LIMIT 12"""
        ).fetchall()
        hist = con.execute(
            f"""SELECT ts, SUM(players) ppl FROM samples
                WHERE addr IN (SELECT addr FROM servers {where})
                GROUP BY ts ORDER BY ts DESC LIMIT 60"""
        ).fetchall()
    return {
        "total": s["total"] or 0,
        "online": s["online"] or 0,
        "slots": s["slots"] or 0,
        "active": s["active"] or 0,
        "locked": s["locked"] or 0,
        "real_online": s["real_online"] or 0,
        "faked": s["faked"] or 0,
        "maps": [dict(m) for m in maps],
        "history": [dict(h) for h in reversed(hist)],
    }


def known_addrs():
    with connect() as con:
        return [r["addr"] for r in con.execute("SELECT addr FROM servers")]
