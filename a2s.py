"""
a2s.py — low-level polling of Counter-Strike 1.6 (GoldSrc) game servers
over the A2S protocol on top of UDP. Works the same for Steam and non-Steam (pirate) servers.

Reads only public data that the server itself exposes in the server browser.
"""

import re
import socket
import struct
import time
from datetime import datetime, timezone

_CTRL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")   # everything but TAB


class Reader:
    """Sequential reading of a binary A2S response."""

    def __init__(self, data: bytes):
        self.d, self.i = data, 0

    def byte(self):
        v = self.d[self.i]
        self.i += 1
        return v

    def short(self):
        v = struct.unpack("<h", self.d[self.i:self.i + 2])[0]
        self.i += 2
        return v

    def long(self):
        v = struct.unpack("<i", self.d[self.i:self.i + 4])[0]
        self.i += 4
        return v

    def float(self):
        v = struct.unpack("<f", self.d[self.i:self.i + 4])[0]
        self.i += 4
        return v

    def string(self):
        end = self.d.index(b"\x00", self.i)
        v = self.d[self.i:end].decode("utf-8", "replace")
        self.i = end + 1
        # some (farm) servers pad strings with \r / \n: "de_dust2\r" is not another map;
        # TABs are used as decoration/separators in names, so they become spaces
        return _CTRL.sub("", v.replace("\t", " "))


def _recv(sock: socket.socket) -> bytes:
    """Receive a response, including reassembly of split packets (usually with A2S_PLAYERS/RULES)."""
    data, _ = sock.recvfrom(8192)
    if data[:4] != b"\xFE\xFF\xFF\xFF":
        return data[4:]

    parts, total = {}, None
    while True:
        req_id = struct.unpack("<I", data[4:8])[0]
        if req_id & 0x80000000:
            raise ValueError("compressed split packet is not supported")
        packed = data[8]
        # GoldSrc: a single byte — low nibble is total, high nibble is number
        total, number = packed & 0x0F, packed >> 4
        parts[number] = data[9:]
        if total and len(parts) >= total:
            break
        data, _ = sock.recvfrom(8192)

    body = b"".join(parts[k] for k in sorted(parts))
    return body[4:] if body[:4] == b"\xFF\xFF\xFF\xFF" else body


def _drain(sock: socket.socket):
    """Throw away datagrams already queued on the socket (late replies to a previous
    query on the same socket would otherwise be mistaken for the answer)."""
    sock.setblocking(False)
    try:
        while True:
            sock.recvfrom(8192)
    except (BlockingIOError, OSError):
        pass


def _query(host: str, port: int, payload: bytes, expect: bytes, timeout: float = 1.5,
           sock=None):
    """A single request with automatic handling of the challenge response (0x41 'A').

    expect — the reply header bytes we accept (b"mI" for INFO, b"D" for PLAYERS,
    b"E" for RULES); anything else that arrives within the deadline is skipped.
    sock — an optional already-open UDP socket: probe() reuses one socket for
    INFO/RULES/PLAYERS instead of opening three. Without it a temporary one is made.
    """
    own = sock is None
    if own:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    else:
        _drain(sock)
    try:
        deadline = time.perf_counter() + timeout
        t0 = time.perf_counter()
        sock.settimeout(timeout)
        sock.sendto(b"\xFF\xFF\xFF\xFF" + payload, (host, port))
        challenges = 0
        while True:
            left = deadline - time.perf_counter()
            if left <= 0:
                return None, None
            sock.settimeout(left)
            resp = _recv(sock)
            if resp[:1] == b"A" and challenges < 2:  # server requires a challenge — retry with it
                challenges += 1
                # the trailing \xFF\xFF\xFF\xFF in the request is the placeholder for the challenge:
                # we replace it, for A2S_INFO keeping the whole "TSource Engine Query\0" intact.
                # Ping is measured on the final exchange only, otherwise challenge servers
                # would report a doubled RTT.
                t0 = time.perf_counter()
                sock.sendto(b"\xFF\xFF\xFF\xFF" + payload[:-4] + resp[1:5], (host, port))
                continue
            if resp[:1] in expect:
                return resp, round((time.perf_counter() - t0) * 1000)
            # stale or foreign datagram — keep waiting for the real reply
    except (socket.timeout, OSError, ValueError, IndexError, struct.error):
        return None, None
    finally:
        if own:
            sock.close()


def info(host: str, port: int, timeout: float = 1.5, sock=None):
    """A2S_INFO — name, map, online count. Both formats supported: 'm' (GoldSrc) and 'I'."""
    resp, ping = _query(host, port, b"TSource Engine Query\x00\xFF\xFF\xFF\xFF", b"mI", timeout, sock)
    if not resp:
        return None
    try:
        r = Reader(resp)
        header = chr(r.byte())
        d = {"ping_ms": ping, "proto_kind": header}
        if header == "m":  # old GoldSrc response, typical for pirate servers
            d["address"] = r.string()
            d["name"] = r.string()
            d["map"] = r.string().strip()
            d["folder"] = r.string()
            d["game"] = r.string()
            d["players"] = r.byte()
            d["max_players"] = r.byte()
            d["protocol"] = r.byte()
            d["server_type"] = chr(r.byte())
            d["environment"] = chr(r.byte())
            d["password"] = bool(r.byte())
            d["bots"] = 0
            d["vac"] = None
        elif header == "I":
            d["protocol"] = r.byte()
            d["name"] = r.string()
            d["map"] = r.string().strip()
            d["folder"] = r.string()
            d["game"] = r.string()
            r.short()  # appid
            d["players"] = r.byte()
            d["max_players"] = r.byte()
            d["bots"] = r.byte()
            d["server_type"] = chr(r.byte())
            d["environment"] = chr(r.byte())
            d["password"] = bool(r.byte())
            d["vac"] = bool(r.byte())
        else:
            return None
        return d
    except (IndexError, ValueError, struct.error):
        return None


def players(host: str, port: int, timeout: float = 1.5, sock=None):
    """A2S_PLAYERS — nicknames, frags, time in game."""
    resp, _ = _query(host, port, b"\x55\xFF\xFF\xFF\xFF", b"D", timeout, sock)
    if not resp:
        return []
    r = Reader(resp)
    r.byte()
    out = []
    try:
        n = r.byte()
        for _ in range(n):
            r.byte()  # index (with GoldSrc almost always 0)
            out.append({
                "name": r.string(),
                "score": r.long(),
                "time_sec": round(r.float(), 1),
            })
    except (IndexError, ValueError, struct.error):
        pass  # truncated response — return what we managed to parse
    return out


def rules(host: str, port: int, timeout: float = 1.5, sock=None):
    """A2S_RULES — server cvars (sv_gravity, amx_*, useful for detecting mods)."""
    resp, _ = _query(host, port, b"\x56\xFF\xFF\xFF\xFF", b"E", timeout, sock)
    if not resp:
        return {}
    r = Reader(resp)
    r.byte()
    out = {}
    try:
        n = r.short()
        for _ in range(n):
            k = r.string()
            out[k] = r.string()
    except (IndexError, ValueError, struct.error):
        pass
    return out


def is_pirate(inf: dict, rls: dict) -> bool:
    """
    Heuristic for a "non-Steam server".

    Steam servers respond with the Source format 'I' with the VAC flag and almost always
    have sv_steamgroup/steam cvars. Pirate servers: old format 'm', VAC disabled,
    protocol 47/48 and cvars characteristic of non-Steam builds.
    """
    if inf.get("vac") is True:
        return False
    score = 0
    if inf.get("proto_kind") == "m":
        score += 2
    if inf.get("vac") is False:
        score += 1
    if str(rls.get("sv_lan", "")) == "0" and "sv_steamgroup" not in rls:
        score += 1
    if rls.get("sv_secure") in ("0", 0):
        score += 1
    return score >= 2


def is_cs16(inf: dict) -> bool:
    """
    Filtering out subnet neighbors: CS:S/CS2/TF2/Rust respond to the same A2S.
    CS 1.6 is GoldSrc: the cstrike folder, the game is exactly "Counter-Strike"
    (Source has "Counter-Strike: Source", CS2 uses the csgo folder), protocol 47/48
    or the old 'm' response format.
    """
    folder = (inf.get("folder") or "").lower()
    game = (inf.get("game") or "")
    if folder != "cstrike":
        return False
    if "source" in game.lower():
        return False
    return inf.get("proto_kind") == "m" or inf.get("protocol") in (46, 47, 48)


def probe(addr: str, want_players=True, want_rules=True, timeout=1.5):
    """Full poll of a single 'ip:port' address. Returns a dict or None."""
    host, _, port = addr.rpartition(":")
    if not host:
        return None
    try:
        port = int(port)
    except ValueError:
        return None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        inf = info(host, port, timeout, sock)
        if not inf:
            return None

        row = {
            "addr": f"{host}:{port}",
            "ip": host,
            "port": port,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **inf,
        }
        row["rules"] = rules(host, port, timeout, sock) if want_rules else {}
        row["player_list"] = (
            players(host, port, timeout, sock)
            if want_players and inf.get("players", 0) > 0 else []
        )
    finally:
        sock.close()
    row["pirate"] = is_pirate(inf, row["rules"])
    row["cs16"] = is_cs16(inf)
    return row
