# data/

Clean slices of the CS 1.6 server database (updated by `export_data.py`).

| file | what it is |
|---|---|
| `servers.csv` | all valid servers, flat table |
| `servers.json.gz` | the same in JSON (gunzip before reading) |
| `pirate.csv` | non-Steam servers only |
| `stats.json` | aggregates for the dashboard and article |
| `fakecheck.json` | result of the inflation check (re-measurement) |
| `cs16.db.gz` | full SQLite database: servers + measurement history + player lists. `gunzip cs16.db.gz` |

Addresses are public endpoints of game servers (the same ones shown by any
server browser or monitoring tool). The `real_players` field is how many names the
server returned via `A2S_PLAYERS`; the names themselves are not exported to CSV/JSON
(they exist only in the full `cs16.db`).
