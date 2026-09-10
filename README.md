# cs16-stats

Discover and monitor **Counter-Strike 1.6** game servers — Steam and non-Steam
("pirate") alike — over the Valve A2S protocol, then serve the results as a
searchable web dashboard.

The project started from one practical question: *my non-Steam CS 1.6 client
finds servers, so where does it get the list?* Reverse-engineering that answer
turned into a full server-discovery pipeline that, on the last run, mapped
**~112 000 A2S endpoints** — most of which turned out to be inflation (see Findings).

Interactive dashboard: [`docs/dashboard.html`](docs/dashboard.html).

## What it does

- **A2S over UDP** ([`a2s.py`](a2s.py)) — `A2S_INFO` / `A2S_PLAYERS` / `A2S_RULES`,
  both the old GoldSrc `m` reply (typical of non-Steam builds) and the Source `I`
  reply, with challenge handling and split-packet reassembly.
- **Multi-source discovery** ([`discovery.py`](discovery.py)) — seed lists,
  arbitrary monitoring pages/APIs (addresses pulled out by regex), and UDP master
  servers (Valve protocol `0x31`). ~60 live non-Steam master hosts ship in
  [`sources.json`](sources.json).
- **Fast async UDP sweep** ([`sweep.py`](sweep.py)) — one socket firing packets
  with a rate limit while a reader thread collects replies. Expands from known
  servers to their whole `/24` and full port range. ~12 000 packets/s on a home
  connection.
- **Hoster expansion** ([`asn_expand.py`](asn_expand.py)) — from a known server
  IP to every prefix its ASN announces, via RIPEstat (no key).
- **Client memory harvest** ([`harvest_client.py`](harvest_client.py)) — reads the
  already-fetched server list straight out of a running `hl.exe` (read-only),
  for builds whose master protocol is closed (see below).
- **Fake-online detection** ([`fakecheck.py`](fakecheck.py)) — re-probes a server
  after a delay; real players' in-game time advances, faked lists don't.
- **Web dashboard** ([`app.py`](app.py) + [`templates/index.html`](templates/index.html))
  — Flask UI: live counters, top maps, filters, per-server player lists and
  cvars, a "confirmed online" toggle, background scans with a live log.

## Quick start

```bash
pip install flask
python app.py --port 5000       # open http://127.0.0.1:5000, press "Scan"
```

Just want a server to play on right now (no full scan needed):

```bash
python play.py --ping 60 --min 8      # nearest non-empty servers + a connect line
```

Command-line scan and export:

```bash
python scan.py --csv out.csv          # discovery + probe, no browser
python report.py                      # aggregate stats over the DB
python export_data.py                 # clean CSV/JSON slices into data/
```

## How a non-Steam client finds servers (CSPro build)

The client (`C:\Games\Counter-Strike 1.6 Russian`, a **CSPro** build) does *not*
use Valve's UDP master. Its `steam_api.dll` is replaced with a full matchmaking
client that talks a private **GMS-over-HTTP** protocol:

```
POST / HTTP/1.1
Host: mscf1.cs-css.com:8880          (fallbacks mscf2/mscf3.cs-css.com:8880)
User-Agent: Valve/Steam HTTP Client 1.0
```

The request body is compressed and serialized (`GSM_PACKET_COMPRESSED |
GSM_PACKET_SERIALIZED`), so it can't be trivially replayed. The reply is a batch
of `ip:port` which the client then pings over A2S itself. Because the protocol is
closed, `harvest_client.py` takes the pragmatic route and reads the already-parsed
list out of the running process's memory.

Full write-up: [`docs/DISCOVERY.md`](docs/DISCOVERY.md).

## Data

`data/` holds clean slices refreshed by `export_data.py`:
`servers.csv` / `servers.json.gz` (all valid servers), `pirate.csv` (non-Steam only),
`stats.json` (aggregates), `fakecheck.json` (anti-fake sample). Addresses are
public game-server endpoints — the same ones any server browser or monitoring
site shows. `real_players` is the count of names a server returned via
`A2S_PLAYERS`, not the names themselves.

## Findings (last full run)

The raw count is a trap. A2S answered from **112 185** "servers" — but on only
**3 738 distinct IPs**.

- **205 IPs hold 94 % of all records** (port farms — one machine answering A2S on
  dozens–hundreds of ports). The single name `ROMANIA.FULLBOOST.RO V.I.P FREE`
  repeats **3 929 times**.
- Off the farms (≤20 servers/IP): **~6 477** servers on 3 533 IPs — the defensible
  count of independent servers.
- Claimed online **1.49 M** is fiction. Off-farm, where the count is plausible,
  **37 k** players are confirmed name-by-name via `A2S_PLAYERS`. The real CS 1.6
  population is tens of thousands, not millions.
- Anti-fake re-probe of a sample: ~76 % show real activity, ~14 % are provably
  faked, the rest inconclusive.

de_dust2 is still, 25 years on, the single most-hosted map by a wide margin.

## Ethics & scope

Read-only. The tool only asks servers for the public information they already
publish to the in-game server browser. No exploitation, no auth, no writes. It is
meant for building server lists, monitoring, and measuring the CS 1.6 population
— the same job public monitorings do. Respect rate limits and local law.

## License

MIT — see [`LICENSE`](LICENSE).
