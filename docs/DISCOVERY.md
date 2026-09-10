# How a non-Steam CS 1.6 client finds servers

The analysis was done on the **CSPro** build (`C:\Games\Counter-Strike 1.6 Russian`).
Debug paths in the binaries are `D:\a\CSPro\CSPro\goldsrc-client\...`, settings live
in the registry at `HKCU\Software\CSPro\Half-Life`.

## In short

The game launches as `hl.exe -game cstrike -steam`, but `steam_api.dll` is replaced:
it is not a Steam stub but a full-fledged matchmaking client
(`src\clientdll\matchmaking\matchmaking_client.cpp`, `serverlist.cpp`). It obtains the
server list **not** through Valve's UDP master, but via its own protocol
**GMS over HTTP**.

## What's in the traffic

The request, captured from the process memory, in full:

```
POST / HTTP/1.1
Host: mscf1.cs-css.com:8880
User-Agent: Valve/Steam HTTP Client 1.0
Connection: Close
Content-Length: 245
```

- Hosts are tried in turn when one is unreachable (`Unable to connect to the host %s.
  Trying to another host %s` → `No more hosts to try`): `mscf1.cs-css.com:8880`,
  with fallbacks `mscf2/mscf3.cs-css.com:8880`. `mscf1` resolves to a separate IP,
  `mscf2/mscf3` are behind Cloudflare.
- The request body (~245 bytes) is a compressed and serialized packet
  (flags `GSM_PACKET_COMPRESSED | GSM_PACKET_SERIALIZED`). Inside are the usual fields
  of a Valve master: `region`, `gamedir`, `\gamedir\cstrike`. The format is closed and
  cannot be reproduced "head-on" from the outside.
- The response is a batch of `ip:port` (`GMSQueryResult_t`), after which the client
  **pings each server over A2S itself** — exactly what this project does. In the code
  this is visible from the strings `MaxServerBrowserPingsPerMin`, `[GSM] Server %s:%s query has timed
  out`, `... has password... invalidated`, `... has blacklisted... invalidated`.

## Local lists besides the master

The client does not rely only on the master: `InitTopServersList` reads `toplist.dat`,
there is `InitRandomList`, `InitBlackList`, `InitRedirectWhiteList`,
`InitFavoritesServersList`. So some servers are shown even when the master is
unreachable, and the build is able to redirect players to "its own" servers.

## Practical conclusion

Since the GMS protocol is closed, the easiest way is to take the list from the client
itself. [`harvest_client.py`](../harvest_client.py) reads the memory of a running `hl.exe`
(read-only, nothing is written into the game) and extracts the addresses it has already
received:

```bash
python harvest_client.py --probe --db   # game running, server browser open
```

First run: 506 addresses in memory → 486 after normalization → 357 live
servers. This became the starting point for all the rest of the collection.

## How it scaled up to ~20,000 servers

1. **From client memory** — 357 live servers (starting point).
2. **Monitors and directories** (gametracker, tsarvar, etc.), the open Steam Web
   API, dumps from GitHub — thousands of addresses each.
3. **UDP masters of non-Steam builds** — found ~690 live (191 unique by IP,
   top-60 in [`sources.json`](../sources.json)). They respond with Valve's classic
   protocol `0x31`. The master channel is quickly "drained": almost everything it knows
   is already in the database — a sign of complete coverage.
4. **UDP sweep** ([`sweep.py`](../sweep.py)) — every `/24` around known servers
   on "anchor" ports, then the full port range across known IPs. The rounds
   were repeated until the gain ran out.
5. **ASN expansion** ([`asn_expand.py`](../asn_expand.py)) — from a server's IP to
   all of its host's prefixes via RIPEstat.

Key efficiency lesson: a broad sweep across the entire address space of the
hosters yields little (hundreds of servers over tens of minutes), whereas a `/24` sweep
**around already known ones** and crawling the masters yields thousands in minutes.
CS 1.6 lives in clusters.
