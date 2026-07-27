#!/usr/bin/env python
"""NERV-themed read-only dashboard for the memebot. Stdlib only.

Usage: python dashboard/server.py [--port 8700] [--config config.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bdusage
import blcards
import fundergraph
import walletpage
from backtest import BLOCKLIST_PATH, MANUAL_BLOCKLIST_PATH
from bot import jupiter
from bot import wallets as botwallets
from bot.config import Config, LAMPORTS_PER_SOL
from bot.portfolio import Portfolio
from bot.util import iso_now, load_dotenv, make_session

INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
HOLD_RE = re.compile(r"hold\s+(\S+)\s+([0-9.]+)x")
SCAN_RE = re.compile(r"entry scan: (\d+) candidates, (\d+) passed filters, (\d+) deep-checked, (\d+) entered")

cfg: Config
acfg = None  # config.asym.json, if present (prototype strategy)
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


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


WS_STATE_PATH = os.path.join("reports", "ws_state.json")
_ws_candles_cache = {"mtime": 0.0, "by_mint": {}}


def ws_candles(mint: str, limit: int = 60) -> list:
    """Last `limit` 1m candles [[ts,o,h,l,c,v_usd],...] for a mint, from the
    bot's WS snapshot. Zero Birdeye CU: the feed maintains the store; the
    multi-MB file is re-parsed only when its mtime changes (~every 60s)."""
    try:
        mtime = os.path.getmtime(WS_STATE_PATH)
    except OSError:
        return []
    if mtime != _ws_candles_cache["mtime"]:
        snap = _read_json(WS_STATE_PATH) or {}
        _ws_candles_cache["by_mint"] = snap.get("candles") or {}
        _ws_candles_cache["mtime"] = mtime
    rows = _ws_candles_cache["by_mint"].get(mint)
    if not rows:
        return []
    return sorted(rows.values(), key=lambda r: r[0])[-limit:]


_bl_lock = threading.Lock()


def blacklist_update(req: dict) -> dict:
    """Add/remove a hand-flagged token in reports/bd_blocklist_manual.json.
    Entries record the trade's win/loss at flag time so the /blacklist tab can
    disclose whether manual purging skews toward deleting losses (which would
    inflate every downstream backtest number)."""
    mint = str(req.get("mint") or "").strip()
    if not mint or len(mint) > 64:
        raise ValueError("mint required")
    with _bl_lock:
        data = _read_json(MANUAL_BLOCKLIST_PATH) or {}
        if req.get("action") == "remove":
            data.pop(mint, None)
        else:
            data[mint] = {
                "symbol": str(req.get("symbol") or "?")[:32],
                "reason": "manual",
                "pool": str(req.get("pool") or "")[:64],
                "result": "win" if req.get("result") == "win" else "loss",
                "multiple": round(float(req.get("multiple") or 0), 3),
                "entry_ts": int(req.get("entry_ts") or 0),
                "flagged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        os.makedirs("reports", exist_ok=True)
        with open(MANUAL_BLOCKLIST_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    return {"ok": True, "manual": data}


_mc_lock = threading.Lock()
_mc_state = {"proc": None, "started": 0.0}
MC_LOG = os.path.join("reports", "montecarlo_run.log")


def mc_refresh_start() -> dict:
    """Spawn `python montecarlo.py --refresh-trades` (one at a time). The run
    rebuilds reports/mc_trades.json from the current cleaned sample and
    rewrites reports/montecarlo.html, which this server serves as-is."""
    with _mc_lock:
        p = _mc_state["proc"]
        if p is not None and p.poll() is None:
            return {"ok": True, "already_running": True}
        os.makedirs("reports", exist_ok=True)
        logf = open(MC_LOG, "w", encoding="utf-8")
        _mc_state["proc"] = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "montecarlo.py"), "--refresh-trades"],
            cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT)
        _mc_state["started"] = time.time()
        return {"ok": True, "already_running": False}


def mc_refresh_status() -> dict:
    p = _mc_state["proc"]
    running = p is not None and p.poll() is None
    out = {"running": running,
           "exit_code": None if (p is None or running) else p.returncode,
           "started": _mc_state["started"] or None}
    try:
        with open(MC_LOG, "r", encoding="utf-8") as f:
            out["log_tail"] = f.read()[-2000:].splitlines()[-8:]
    except OSError:
        out["log_tail"] = []
    return out


_ct_lock = threading.Lock()
_ct_state = {"proc": None, "started": 0.0}
CT_LOG = os.path.join("reports", "copytrade_run.log")
CT_PROGRESS = os.path.join("reports", "copytrade_progress.json")


def ct_run_start() -> dict:
    """Spawn `python copytrade_backtest.py` (one at a time). It rewrites
    reports/copytrade_backtest.json + emits copytrade_progress.json for the
    WALLETS-tab progress bar. OHLCV is cached, so re-runs are fast."""
    with _ct_lock:
        p = _ct_state["proc"]
        if p is not None and p.poll() is None:
            return {"ok": True, "already_running": True}
        os.makedirs("reports", exist_ok=True)
        logf = open(CT_LOG, "w", encoding="utf-8")
        _ct_state["proc"] = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "copytrade_backtest.py")],
            cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT)
        _ct_state["started"] = time.time()
        return {"ok": True, "already_running": False}


def ct_progress() -> dict:
    """Live progress for the copy-trade backtest. Merges the process liveness
    with the progress file the script writes; 'stalled' if the file is old and
    no process is tracked."""
    prog = _read_json(CT_PROGRESS) or {"phase": "idle", "pct": 0, "status": "idle"}
    p = _ct_state["proc"]
    running = p is not None and p.poll() is None
    age = time.time() - prog.get("updated", 0) if prog.get("updated") else None
    if prog.get("status") == "done":
        prog["state"] = "done"
    elif running or (age is not None and age < 30):
        prog["state"] = "running"
    elif prog.get("phase") == "idle":
        prog["state"] = "idle"
    else:
        prog["state"] = "stalled"
    prog["age_sec"] = round(age) if age is not None else None
    return prog


def ops_progress():
    """Progress of long-running R&D jobs. Fetchers write progress files; sweeps
    are inferred from their result files' timestamps."""
    out = []
    for path in (os.path.join("reports", "bd_progress.json"),
                 os.path.join("reports", "gt_progress.json")):
        p = _read_json(path)
        if not p:
            continue
        age = time.time() - p.get("updated", 0)
        p["status"] = "done" if p.get("phase") == "done" else ("running" if age < 180 else "stalled")
        p["age_sec"] = round(age)
        out.append(p)
    for name, path in (("EXIT SWEEP", os.path.join("reports", "sweep_results.json")),
                       ("ENTRY SWEEP", os.path.join("reports", "sweep2_results.json"))):
        try:
            mt = os.path.getmtime(path)
            out.append({"name": name, "phase": "done", "status": "done",
                        "age_sec": round(time.time() - mt)})
        except OSError:
            out.append({"name": name, "phase": "pending", "status": "pending"})
    return out


def asym_backtest():
    """Backtest metrics for the asym-runner variant (variant E) from the last
    backtest.py run, if its summary file exists."""
    try:
        with open(os.path.join("reports", "backtest_summary.json"), "r", encoding="utf-8") as f:
            summ = json.load(f)
    except (OSError, ValueError):
        return None
    for v in summ.get("variants", []):
        if v.get("name", "").startswith("E:"):
            return {"generated_at": summ.get("generated_at"), "sample": summ.get("sample"),
                    "overall": v.get("overall"), "fair": v.get("fair"),
                    "cohorts": v.get("cohorts"), "note": summ.get("note")}
    return None


# Live paper strategies. base is the main-tree asym trial; bounce/holder run
# from the parallel worktree with --workdir=ROOT, so their dbs/logs land here.
WORKTREE = os.path.join(ROOT, ".claude", "worktrees", "paper-bounce-holder")
STRATEGIES = [
    ("base", "BASE", os.path.join(ROOT, "config.asym.json")),
    ("bounce", "BOUNCE", os.path.join(WORKTREE, "config.bounce.json")),
    ("holder", "HOLDER", os.path.join(WORKTREE, "config.holder.json")),
    ("homerun", "HOMERUN", os.path.join(ROOT, "config.homerun.json")),
    ("sniper", "SNIPER", os.path.join(ROOT, "config.sniper.json")),
    ("volanomaly", "VOL-ANOM", os.path.join(ROOT, "config.volanomaly.json")),
    ("dip", "DIP", os.path.join(ROOT, "config.dip.json")),
    ("grinder", "GRINDER", os.path.join(ROOT, "config.grinder.json")),
]


def _paper_for(scfg):
    """(paper dict|None, running bool, recent-closes list) for one strategy,
    read-only from its own paper db + log. Never creates the db file."""
    paper, closed_recent = None, []
    dbp = scfg.db_path
    if os.path.exists(dbp):
        adb = Portfolio(dbp, scfg.mode)
        closed = adb.closed_positions(limit=500)
        wins = [p for p in closed if p.sol_received > p.sol_spent]
        mults = [p.sol_received / p.sol_spent for p in closed if p.sol_spent]
        open_rows, unreal_lamports, quotes_missing = [], 0, False
        for pos in adb.open_positions():
            val = cache.value(pos.mint, pos.tokens_raw)
            missing = val is None
            mult = ((pos.sol_received + (val or 0)) / pos.sol_spent) if pos.sol_spent else 0.0
            if missing:  # no live quote: hold the position at its remaining cost basis
                quotes_missing = True
                val = max(0, pos.sol_spent - pos.sol_received)
            unreal_lamports += pos.sol_received + val - pos.sol_spent
            open_rows.append({
                "symbol": pos.symbol, "age_min": round(pos.age_min, 1),
                "spent_sol": round(sol(pos.sol_spent), 4),
                "multiple": None if missing else round(mult, 3),
                "peak": round(pos.peak_multiple, 2), "stage": pos.tp_stage,
            })
        import sqlite3
        oconn = sqlite3.connect(dbp)
        oconn.row_factory = sqlite3.Row
        try:
            frows = oconn.execute(
                "SELECT f.ts, f.side, f.sol_lamports, f.note, p.symbol "
                "FROM fills f JOIN positions p ON p.id = f.position_id "
                "ORDER BY f.ts DESC LIMIT 14").fetchall()
        except sqlite3.OperationalError:
            frows = []
        finally:
            oconn.close()
        orders = [{"ts": r["ts"], "side": r["side"], "symbol": r["symbol"],
                   "sol": round(sol(r["sol_lamports"]), 4), "note": r["note"] or ""}
                  for r in frows]
        # voided (rugged, unsellable) positions: kept in the ledger but excluded
        # from PnL/win-rate/balance above. Tracked separately so the loss is
        # visible without corrupting strategy stats.
        voided = adb.voided_positions()
        start_sol = float(scfg.raw.get("paper_starting_balance_sol", 1.0))
        realized = sum(p.sol_received - p.sol_spent for p in closed)
        paper = {
            "open": len(open_rows), "count": len(closed), "wins": len(wins),
            "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
            "avg_multiple": round(statistics.mean(mults), 2) if mults else None,
            "total_pnl_sol": round(sol(realized), 4), "start_sol": start_sol,
            "balance_sol": round(start_sol + sol(realized + unreal_lamports), 4),
            "balance_est": quotes_missing, "open_rows": open_rows, "orders": orders,
            "voided": len(voided),
            "voided_sol": round(sol(sum(p.sol_spent for p in voided)), 4),
            "realized_today_sol": round(sol(adb.realized_today_lamports()), 4),
        }
        closed_recent = [
            {"closed_at": p.closed_at, "symbol": p.symbol,
             "multiple": round(p.sol_received / p.sol_spent, 2) if p.sol_spent else 0,
             "pnl_sol": round(sol(p.sol_received - p.sol_spent), 4),
             "reason": p.exit_reason or "?"}
            for p in closed[:12]]
        # keep void trades in the log feed (0 PnL, VOID-tagged), newest first
        closed_recent += [
            {"closed_at": p.closed_at, "symbol": p.symbol, "multiple": 0,
             "pnl_sol": 0.0, "reason": "VOID · " + (p.exit_reason or "rug"), "void": True}
            for p in voided[:8]]
        closed_recent.sort(key=lambda r: r.get("closed_at") or "", reverse=True)
        closed_recent = closed_recent[:12]
    try:
        idle = time.time() - os.path.getmtime(scfg.log_path)
        running = idle < max(2 * scfg.scan_interval_sec, 120)
    except OSError:
        running = False
    return paper, running, closed_recent


def strategy_states():
    """Per-strategy live paper states (BASE/BOUNCE/HOLDER). Strategies whose
    config file is absent are skipped, so the panel degrades gracefully."""
    out = []
    for key, label, cfgpath in STRATEGIES:
        if not os.path.exists(cfgpath):
            continue
        try:
            scfg = acfg if (key == "base" and acfg is not None) else Config.load(cfgpath)
        except Exception:
            continue
        paper, running, recent = _paper_for(scfg)
        for r in recent:
            r["strategy"] = label
        out.append({
            "key": key, "label": label, "running": running,
            "exits": {"stop_loss_pct": scfg.stop_loss_pct, "take_profits": scfg.take_profits,
                      "trailing_stop_pct": scfg.trailing_stop_pct,
                      "hard_tp_multiple": scfg.hard_tp_multiple, "max_hold_min": scfg.max_hold_min},
            "paper": paper, "closed_recent": recent,
        })
    return out


def fleet_totals(strategies):
    """Fleet-wide roll-up across every strategy's own ledger.

    The header tiles used to read the single Portfolio the server was launched
    with (config.asym.json -> BASE), so they reported one book while claiming to
    be the whole fleet - "0 unit(s) currently deployed" while HOLDER and GRINDER
    each held a position. Summing here keeps each book counted exactly once.

    Voids (rugged, unsellable) stay OUT of pnl/win-rate, same as per-strategy,
    but are carried so the UI can show what the ledgers absorbed."""
    books = [s["paper"] for s in strategies if s.get("paper")]
    count = sum(b["count"] for b in books)
    wins = sum(b["wins"] for b in books)
    traded = [s for s in strategies if s.get("paper")
              and (s["paper"]["count"] or s["paper"]["open"] or s["paper"]["voided"])]
    return {
        "count": count, "wins": wins,
        "win_rate": round(100 * wins / count, 1) if count else None,
        "total_pnl_sol": round(sum(b["total_pnl_sol"] for b in books), 4),
        "realized_today_sol": round(sum(b["realized_today_sol"] for b in books), 4),
        "open": sum(b["open"] for b in books),
        "voided": sum(b["voided"] for b in books),
        "voided_sol": round(sum(b["voided_sol"] for b in books), 4),
        "books": len(books), "books_traded": len(traded),
        "books_running": sum(1 for s in strategies if s.get("running")),
    }


def asym_state():
    """State block for the base asym-runner strategy (config.asym.json):
    exit doctrine, paper-trading stats from its own db, and backtest metrics."""
    if acfg is None:
        return None
    paper, running, _ = _paper_for(acfg)
    return {
        "exits": {
            "stop_loss_pct": acfg.stop_loss_pct,
            "take_profits": acfg.take_profits,
            "trailing_stop_pct": acfg.trailing_stop_pct,
            "hard_tp_multiple": acfg.hard_tp_multiple,
            "max_hold_min": acfg.max_hold_min,
        },
        "entry": {
            "signal": {
                "min_chg_m5_pct": acfg.min_chg_m5_pct,
                "max_chg_m5_pct": acfg.max_chg_m5_pct,
                "min_chg_h1_pct": acfg.min_chg_h1_pct,
                "min_buy_sell_edge": acfg.min_buy_sell_edge,
            },
            "funnel": {
                "min_liquidity_usd": acfg.min_liquidity_usd,
                "max_liquidity_usd": acfg.max_liquidity_usd,
                "min_age_min": acfg.min_age_min,
                "max_age_min": acfg.max_age_min,
                "max_fdv_to_liquidity": acfg.max_fdv_to_liquidity,
                "min_vol_m5_usd": acfg.min_vol_m5_usd,
                "min_sells_m5": acfg.min_sells_m5,
                "max_buy_sell_ratio": acfg.max_buy_sell_ratio,
            },
            "safety": {
                "max_rugcheck_score": acfg.max_rugcheck_score,
                "max_insider_pct": acfg.max_insider_pct,
                "max_sniper_pct": acfg.max_sniper_pct,
                "max_top10_pct": acfg.max_top10_pct,
                "max_creator_pct": acfg.max_creator_pct,
                "min_holders": acfg.min_holders,
                "max_bundlers": acfg.max_bundlers,
            },
        },
        "running": running,
        "paper": paper,
        "backtest": asym_backtest(),
    }


REJECT_RE = re.compile(r"^(\S+ \S+) \w+\s+memebot: reject (\S+)\s+(.+)$")


def classify_reject(reason: str) -> str:
    """Bucket a reject log line into its gate. Anchors match the exact verdict
    strings each bot module emits (order matters: 'bundle-sniper wallets'
    contains 'sniper wallets', so smart-money is tested before insiders).
    Substring (not prefix) tests, because a symbol containing whitespace
    pushes part of itself into the reason group."""
    r = reason.lower()
    if "setup:" in r:
        return "setup"
    if "bounce:" in r:
        return "bounce"
    if "non-human token" in r:
        return "human"
    if "moonshot" in r:
        return "moonshot"
    if r.startswith(("sniper:", "volume_anomaly:", "dip:")):
        return "screen"
    if "rugcheck" in r:
        return "rugcheck"
    if "bundle-sniper" in r or "top-trader" in r or "top traders" in r or "wash-trade" in r:
        return "smart_money"
    if ("insider" in r or "sniper wallets hold" in r or "top-10 holders" in r
            or "creator still holds" in r or "rugged" in r or "holders <" in r):
        return "insiders"
    if "roundtrip" in r or "round-trip" in r or "route" in r or "jupiter" in r:
        return "roundtrip"
    return "other"


def parse_rejects(lines, label: str) -> list:
    """Reject events from one strategy's log tail, tagged with its label."""
    evs = []
    for ln in lines:
        m = REJECT_RE.match(ln)
        if not m:
            continue
        ts, sym, reason = m.groups()
        evs.append({"ts": ts, "symbol": sym, "cat": classify_reject(reason),
                    "reason": reason.strip(), "strategy": label})
    return evs


REJECT_TAIL_BYTES = 196608  # per-strategy log window; re-read on every 5s poll


def reject_state() -> dict:
    """Merged rejection feed across every live strategy, so the airlock shows
    what all four bots are filtering rather than only the config this server
    was started with. Counts cover the recent log window, not all time."""
    evs = []
    for key, label, cfgpath in STRATEGIES:
        if not os.path.exists(cfgpath):
            continue
        try:
            scfg = acfg if (key == "base" and acfg is not None) else Config.load(cfgpath)
        except Exception:
            continue
        evs.extend(parse_rejects(read_log_lines(scfg.log_path, REJECT_TAIL_BYTES), label))
    evs.sort(key=lambda e: e["ts"])

    out: dict = {}
    per: dict = {}
    for e in evs:
        cat, lbl = e["cat"], e["strategy"]
        b = out.setdefault(cat, {"n": 0, "by": {}})
        b["n"] += 1
        b["last_symbol"], b["last_reason"] = e["symbol"], e["reason"]
        b["last_ts"], b["last_strategy"] = e["ts"][:16], lbl
        sb = b["by"].setdefault(lbl, {"n": 0})  # per-strategy last, for its own gate table
        sb["n"] += 1
        sb["last_symbol"], sb["last_reason"], sb["last_ts"] = e["symbol"], e["reason"], e["ts"][:16]
        s = per.setdefault(lbl, {"label": lbl, "n": 0, "cats": {}, "_syms": set()})
        s["n"] += 1
        s["cats"][cat] = s["cats"].get(cat, 0) + 1
        s["_syms"].add(e["symbol"])
        s["last_ts"], s["last_symbol"], s["last_cat"] = e["ts"][:16], e["symbol"], cat

    strategies = []
    for _, lbl, _ in STRATEGIES:  # stable BASE/BOUNCE/HOLDER/HOMERUN order
        s = per.get(lbl)
        if not s:
            continue
        s["symbols"] = len(s.pop("_syms"))
        s["top_cat"] = max(s["cats"], key=s["cats"].get)
        strategies.append(s)

    out["recent"] = evs[-14:]  # event feed for the airlock animation + ticker
    out["strategies"] = strategies
    out["window_n"] = len(evs)
    return out


def wallets_state():
    """Harvested top-trader wallet stats (bot/wallets.py sightings db)."""
    c = acfg or cfg
    try:
        return botwallets.snapshot(c.wt_db_path, c.wt_tracked_path)
    except Exception:
        return None


def build_state() -> dict:
    db = Portfolio(cfg.db_path, cfg.mode)
    lines = read_log_lines(cfg.log_path, max_bytes=524288)

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
            "candles": ws_candles(p.mint),
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

    journal = _read_json(os.path.join("reports", "trade_journal.json")) or {}
    px = sol_usd_price()
    strategies_live = strategy_states()
    live_sorties = sorted(
        (r for s in strategies_live for r in s["closed_recent"]),
        key=lambda r: r.get("closed_at") or "", reverse=True)[:12]
    return {
        "sol_price_usd": round(px, 2) if px is not None else None,
        "now": iso_now(),
        "mode": cfg.mode,
        "bot_status": bot_status(),
        "ws": _read_json(os.path.join("reports", "ws_status.json")),
        "rejects": reject_state(),
        "updates": (_read_json(os.path.join("reports", "updates.json")) or {}).get("rows", []),
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
        # `open`/`closed`/`realized_today_sol` are the LAUNCH CONFIG's book only
        # (the units panel and its charts are scoped to it). `fleet` is the
        # roll-up across every strategy ledger - the header tiles use that.
        "open": open_out,
        "closed": closed_out,
        "realized_today_sol": round(sol(db.realized_today_lamports()), 4),
        "fleet": fleet_totals(strategies_live),
        "wallet": wallet_info(),
        "asym": asym_state(),
        "strategies": strategies_live,
        "live_sorties": live_sorties,
        "wallets": wallets_state(),
        "ops": ops_progress(),
        "strategy_lab": _read_json(os.path.join("reports", "strategy_lab.json")),
        "backtest_sorties": journal.get("rows", [])[:10],
        "backtest_strategy": journal.get("strategy"),
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
        elif self.path == "/trades":
            try:
                with open(os.path.join("reports", "trade_cards.html"), "rb") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            except OSError:
                self._send(404, "text/plain", b"no trade journal yet - run: python tradecards.py")
        elif self.path == "/montecarlo":
            try:
                with open(os.path.join("reports", "montecarlo.html"), "rb") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            except OSError:
                self._send(404, "text/plain", b"no monte carlo yet - run: python montecarlo.py")
        elif self.path == "/blacklist":
            try:
                self._send(200, "text/html; charset=utf-8", blcards.render_page().encode("utf-8"))
            except Exception as exc:
                self._send(500, "text/plain", f"blacklist page error: {exc}".encode())
        elif self.path == "/wallets":
            try:
                self._send(200, "text/html; charset=utf-8", walletpage.render_page().encode("utf-8"))
            except Exception as exc:
                self._send(500, "text/plain", f"wallets page error: {exc}".encode())
        elif self.path == "/fundergraph":
            try:
                self._send(200, "text/html; charset=utf-8", fundergraph.render_page().encode("utf-8"))
            except Exception as exc:
                self._send(500, "text/plain", f"fundergraph page error: {exc}".encode())
        elif self.path == "/api/blacklist":
            self._send(200, "application/json", json.dumps({
                "manual": _read_json(MANUAL_BLOCKLIST_PATH) or {},
                "auto_count": len(_read_json(BLOCKLIST_PATH) or {}),
            }).encode())
        elif self.path == "/api/mc/refresh":
            self._send(200, "application/json", json.dumps(mc_refresh_status()).encode())
        elif self.path == "/api/copytrade":
            self._send(200, "application/json", json.dumps(ct_progress()).encode())
        elif self.path == "/api/bdusage":
            try:
                snap = bdusage.snapshot()
                prog = _read_json(os.path.join("reports", "bd_progress.json"))
                if prog and prog.get("requests"):
                    snap["last_run"] = {"name": prog.get("name"), "phase": prog.get("phase"),
                                        "requests": prog.get("requests"),
                                        "updated": prog.get("updated")}
                self._send(200, "application/json", json.dumps(snap).encode())
            except Exception as exc:
                self._send(500, "application/json", json.dumps({"error": str(exc)}).encode())
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if self.path == "/api/blacklist":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                self._send(200, "application/json", json.dumps(blacklist_update(req)).encode())
            except Exception as exc:
                self._send(400, "application/json", json.dumps({"error": str(exc)}).encode())
        elif self.path == "/api/mc/refresh":
            try:
                self._send(200, "application/json", json.dumps(mc_refresh_start()).encode())
            except Exception as exc:
                self._send(500, "application/json", json.dumps({"error": str(exc)}).encode())
        elif self.path == "/api/copytrade/run":
            try:
                self._send(200, "application/json", json.dumps(ct_run_start()).encode())
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
    global cfg, acfg, session, cache
    ap = argparse.ArgumentParser(description="NERV memebot dashboard (read-only)")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    args = ap.parse_args()

    os.chdir(ROOT)
    load_dotenv()
    cfg = Config.load(args.config)
    asym_path = os.path.join(ROOT, "config.asym.json")
    acfg = Config.load(asym_path) if os.path.exists(asym_path) else None
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
