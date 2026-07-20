#!/usr/bin/env python
"""NERV-themed read-only dashboard for the memebot. Stdlib only.

Usage: python dashboard/server.py [--port 8700] [--config config.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bot import jupiter
from bot.config import Config, LAMPORTS_PER_SOL
from bot.portfolio import Portfolio
from bot.util import iso_now, load_dotenv, make_session

INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
HOLD_RE = re.compile(r"hold\s+(\S+)\s+([0-9.]+)x")
SCAN_RE = re.compile(r"entry scan: (\d+) candidates, (\d+) passed filters, (\d+) deep-checked, (\d+) entered")

cfg: Config
session = None
cache = None


class QuoteCache:
    """Short-TTL cache so browser refreshes don't hammer Jupiter."""

    def __init__(self, sess, config: Config, ttl: float = 8.0):
        self.session = sess
        self.cfg = config
        self.ttl = ttl
        self.data = {}
        self.lock = threading.Lock()

    def value(self, mint: str, tokens: int):
        if tokens <= 0:
            return 0
        key = (mint, tokens)
        with self.lock:
            hit = self.data.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
        q = jupiter.get_quote(self.session, mint, jupiter.SOL_MINT, tokens, self.cfg.slippage_bps)
        val = int(q["outAmount"]) if q else None
        with self.lock:
            self.data[key] = (time.time(), val)
        return val


def sol(lamports: int) -> float:
    return lamports / LAMPORTS_PER_SOL


_wallet_cache = {"ts": 0.0, "sol": None}
_sol_price_cache = {"ts": 0.0, "usd": None}


def sol_usd_price() -> float | None:
    """Cached SOL/USD spot; CoinGecko first, Binance fallback."""
    if time.time() - _sol_price_cache["ts"] < 60 and _sol_price_cache["usd"] is not None:
        return _sol_price_cache["usd"]
    price = None
    try:
        r = session.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            timeout=8,
        )
        price = float(r.json()["solana"]["usd"])
    except Exception:
        try:
            r = session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "SOLUSDT"},
                timeout=8,
            )
            price = float(r.json()["price"])
        except Exception:
            price = _sol_price_cache["usd"]
    _sol_price_cache["usd"] = price
    _sol_price_cache["ts"] = time.time()
    return price


def wallet_info():
    if not cfg.wallet_pubkey:
        return None
    if time.time() - _wallet_cache["ts"] > 30:
        try:
            r = session.post(
                cfg.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [cfg.wallet_pubkey]},
                timeout=10,
            )
            _wallet_cache["sol"] = int(r.json()["result"]["value"]) / LAMPORTS_PER_SOL
        except Exception:
            _wallet_cache["sol"] = None
        _wallet_cache["ts"] = time.time()
    sol_bal = _wallet_cache["sol"]
    px = sol_usd_price()
    usd = round(sol_bal * px, 2) if sol_bal is not None and px is not None else None
    return {
        "pubkey": cfg.wallet_pubkey,
        "sol": sol_bal,
        "usd": usd,
        "sol_price_usd": round(px, 2) if px is not None else None,
    }


def read_log_lines(path: str, max_bytes: int = 65536):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            text = f.read().decode("utf-8", errors="replace")
        lines = text.splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]
        return lines
    except OSError:
        return []


def bot_status() -> str:
    try:
        idle = time.time() - os.path.getmtime(cfg.log_path)
    except OSError:
        return "STANDBY"
    return "ACTIVE" if idle < max(2 * cfg.scan_interval_sec, 120) else "STANDBY"


def build_state() -> dict:
    db = Portfolio(cfg.db_path, cfg.mode)
    lines = read_log_lines(cfg.log_path)

    sparks = {}
    for ln in lines[-800:]:
        m = HOLD_RE.search(ln)
        if m:
            sparks.setdefault(m.group(1), []).append(float(m.group(2)))
    scan = None
    for ln in reversed(lines):
        m = SCAN_RE.search(ln)
        if m:
            scan = {
                "candidates": int(m.group(1)),
                "passed": int(m.group(2)),
                "deep": int(m.group(3)),
                "entered": int(m.group(4)),
            }
            break

    open_out = []
    for p in db.open_positions():
        value = cache.value(p.mint, p.tokens_raw)
        quote_ok = value is not None
        multiple = (p.sol_received + (value or 0)) / p.sol_spent if p.sol_spent else 0.0
        spark = sparks.get(p.symbol, [])[-40:]
        if quote_ok:
            spark = spark + [round(multiple, 4)]
        open_out.append({
            "id": p.id,
            "symbol": p.symbol,
            "multiple": round(multiple, 4),
            "peak": round(p.peak_multiple, 4),
            "stage": p.tp_stage,
            "age_min": round(p.age_min, 1),
            "spent_sol": round(sol(p.sol_spent), 4),
            "banked_sol": round(sol(p.sol_received), 4),
            "value_sol": round(sol(value or 0), 4),
            "quote_ok": quote_ok,
            "spark": spark,
        })

    closed = db.closed_positions(limit=200)
    wins = [p for p in closed if p.sol_received > p.sol_spent]
    closed_out = {
        "count": len(closed),
        "wins": len(wins),
        "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
        "total_pnl_sol": round(sol(sum(p.sol_received - p.sol_spent for p in closed)), 4),
        "recent": [
            {
                "closed_at": p.closed_at,
                "symbol": p.symbol,
                "multiple": round(p.sol_received / p.sol_spent, 2) if p.sol_spent else 0,
                "pnl_sol": round(sol(p.sol_received - p.sol_spent), 4),
                "reason": p.exit_reason or "?",
            }
            for p in closed[:10]
        ],
    }

    return {
        "now": iso_now(),
        "mode": cfg.mode,
        "bot_status": bot_status(),
        "scan": scan,
        "config": {
            "position_size_sol": cfg.position_size_sol,
            "max_positions": cfg.max_positions,
            "stop_loss_pct": cfg.stop_loss_pct,
            "hard_tp_multiple": cfg.hard_tp_multiple,
            "take_profits": cfg.take_profits,
            "trailing_stop_pct": cfg.trailing_stop_pct,
            "max_hold_min": cfg.max_hold_min,
            "daily_loss_limit_sol": cfg.daily_loss_limit_sol,
            "slippage_bps": cfg.slippage_bps,
            "scan_interval_sec": cfg.scan_interval_sec,
        },
        "open": open_out,
        "closed": closed_out,
        "realized_today_sol": round(sol(db.realized_today_lamports()), 4),
        "wallet": wallet_info(),
        "log_tail": lines[-30:],
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "rb") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            except OSError:
                self._send(500, "text/plain", b"index.html missing")
        elif self.path == "/api/state":
            try:
                self._send(200, "application/json", json.dumps(build_state()).encode())
            except Exception as exc:
                self._send(500, "application/json", json.dumps({"error": str(exc)}).encode())
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main() -> None:
    global cfg, session, cache
    ap = argparse.ArgumentParser(description="NERV memebot dashboard (read-only)")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    args = ap.parse_args()

    os.chdir(ROOT)
    load_dotenv()
    cfg = Config.load(args.config)
    session = make_session()
    cache = QuoteCache(session, cfg)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"NERV dashboard up: http://localhost:{args.port}  (mode={cfg.mode}, db={cfg.db_path})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard stopped.")


if __name__ == "__main__":
    main()
