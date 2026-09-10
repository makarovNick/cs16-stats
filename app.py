#!/usr/bin/env python3
"""
app.py — web frontend for monitoring non-Steam CS 1.6 servers.

Run:     python app.py            → http://127.0.0.1:5000
Options: --host 0.0.0.0 --port 8080 --auto 300  (auto-rescan every 300 sec)
"""

import argparse
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import a2s
import discovery
import store

app = Flask(__name__)

STATE = {
    "running": False,
    "phase": "idle",
    "done": 0,
    "total": 0,
    "alive": 0,
    "pirate": 0,
    "log": [],
    "started": None,
    "finished": None,
}
_scan_lock = threading.Lock()


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
    STATE["log"] = (STATE["log"] + [line])[-200:]
    print(line, flush=True)


def run_scan(region="world", limit=3000, threads=128, timeout=1.5,
             use_masters=True, use_seeds=True, use_urls=True,
             rescan_known=True, extra_urls=None):
    """Full cycle: collect addresses → probe → store into the DB."""
    if not _scan_lock.acquire(blocking=False):
        return
    try:
        STATE.update(running=True, phase="discovery", done=0, total=0,
                     alive=0, pirate=0, started=datetime.now().isoformat(" ", "seconds"),
                     finished=None)
        log("=== scan start ===")

        addrs = discovery.collect(region=region, limit=limit,
                                  use_masters=use_masters, use_seeds=use_seeds,
                                  use_urls=use_urls, extra_urls=extra_urls, log=log)
        if rescan_known:
            known = store.known_addrs()
            merged = list(dict.fromkeys(addrs + known))
            log(f"[db] +{len(merged) - len(addrs)} previously known (total {len(merged)})")
            addrs = merged

        STATE.update(phase="probe", total=len(addrs))
        if not addrs:
            log("no addresses: fill in seeds.txt or sources.json")
            return

        batch = []
        with ThreadPoolExecutor(max_workers=threads) as ex:
            futures = ex.map(
                lambda a: a2s.probe(a, want_players=True, want_rules=True, timeout=timeout),
                addrs,
            )
            for r in futures:
                STATE["done"] += 1
                if r:
                    STATE["alive"] += 1
                    if r["pirate"]:
                        STATE["pirate"] += 1
                    batch.append(r)
                if len(batch) >= 200:
                    store.upsert_many(batch)
                    batch = []
                if STATE["done"] % 250 == 0:
                    log(f"probed {STATE['done']}/{STATE['total']}, "
                        f"alive {STATE['alive']}, pirate {STATE['pirate']}")
        if batch:
            store.upsert_many(batch)

        log(f"=== done: alive {STATE['alive']}, non-Steam {STATE['pirate']} ===")
    except Exception as e:  # the scanner must not crash the web server
        log(f"ERROR: {type(e).__name__}: {e}")
    finally:
        STATE.update(running=False, phase="idle",
                     finished=datetime.now().isoformat(" ", "seconds"))
        _scan_lock.release()


# ------------------------------------------------------------------- routes

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scan", methods=["POST"])
def api_scan():
    if STATE["running"]:
        return jsonify({"ok": False, "error": "scan already running"}), 409
    p = request.get_json(silent=True) or {}
    threading.Thread(
        target=run_scan,
        kwargs=dict(
            region=p.get("region", "world"),
            limit=int(p.get("limit", 3000)),
            threads=int(p.get("threads", 128)),
            timeout=float(p.get("timeout", 1.5)),
            use_masters=bool(p.get("masters", True)),
            use_seeds=bool(p.get("seeds", True)),
            use_urls=bool(p.get("urls", True)),
        ),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    return jsonify(STATE)


@app.route("/api/servers")
def api_servers():
    a = request.args
    rows = store.query(
        only_pirate=a.get("pirate", "1") == "1",
        q=a.get("q", "").strip(),
        map_name=a.get("map", "").strip(),
        min_players=int(a.get("min_players") or 0),
        non_empty=a.get("non_empty") == "1",
        no_password=a.get("no_password") == "1",
        sort=a.get("sort", "players"),
        only_confirmed=a.get("confirmed") == "1",
        limit=int(a.get("limit") or 300),
    )
    return jsonify({"servers": rows, "count": len(rows)})


@app.route("/api/summary")
def api_summary():
    return jsonify(store.summary(only_pirate=request.args.get("pirate", "1") == "1"))


@app.route("/api/server/<path:addr>")
def api_server(addr):
    """Live re-query of a single server (the "refresh" button in the card)."""
    r = a2s.probe(addr, want_players=True, want_rules=True, timeout=2.0)
    if not r:
        return jsonify({"ok": False, "error": "server did not respond"}), 404
    store.upsert_many([r])
    return jsonify({"ok": True, "server": r})


@app.route("/api/seeds", methods=["GET", "POST"])
def api_seeds():
    if request.method == "POST":
        text = (request.get_json(silent=True) or {}).get("text", "")
        with open(discovery.SEEDS_FILE, "w", encoding="utf-8") as f:
            f.write(text)
        return jsonify({"ok": True, "parsed": len(discovery.from_seeds())})
    text = ""
    if os.path.exists(discovery.SEEDS_FILE):
        with open(discovery.SEEDS_FILE, encoding="utf-8") as f:
            text = f.read()
    return jsonify({"text": text})


def auto_loop(interval, kwargs):
    while True:
        run_scan(**kwargs)
        time.sleep(interval)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--auto", type=int, default=0,
                    help="auto-scan interval in seconds (0 = off)")
    ap.add_argument("--scan-now", action="store_true", help="scan immediately on start")
    args = ap.parse_args()

    store.init()
    discovery.load_sources()  # creates sources.json on first run

    if args.auto:
        threading.Thread(target=auto_loop, args=(args.auto, {}), daemon=True).start()
    elif args.scan_now:
        threading.Thread(target=run_scan, daemon=True).start()

    print(f"→ http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)
