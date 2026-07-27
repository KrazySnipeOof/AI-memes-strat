"""Birdeye WebSocket feed for the paper bot (Premium plan, opt-in via the
`websocket` block in config; `enabled: false` restores the zero-Birdeye bot).

What it adds:
  * DISCOVERY - SUBSCRIBE_TOKEN_NEW_LISTING puts tokens on a watchlist at
    birth; once they age into the entry window they are promoted into the
    scanner's candidate list via a GeckoTerminal pool lookup.
  * LIVE 1m CANDLES - one complex SUBSCRIBE_PRICE covers open positions plus
    the newest credible listings (<= ws_max_price_subs; plan cap is 100 per
    connection). Tokens subscribed at birth accumulate their FULL candle
    history, which lets try_enter() run backtest.entry_setup_ok() - the
    validated base-entry gate - live, plus the backtest's cumulative
    pre-entry volume floor. Late-discovered candidates are completed with a
    single REST /defi/v3/ohlcv backfill (~110 CU, recorded in bdusage).

Honesty notes: WS 1m volume arrives in base units and is converted at the
candle close (same fallback bdfetch uses); the latest candle is still
forming when the gate runs. Fills remain Jupiter-quoted - this feed only
decides WHICH tokens are eligible, never the fill price.

Persistence: watchlist, candle store and counters snapshot to
reports/ws_state.json (every 60s + on disconnect/shutdown) and are restored
on startup, so a bot restart picks up with a warm funnel instead of losing
listing ages, promoted flags and candle coverage. A hard kill loses at most
the last 60s. Opt out via websocket.persist_state: false.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import List, Optional

import websocket

import backtest  # repo root (run.py sys.path) - the validated setup gate lives there
import strategies  # the 2026-07-27 search families, shared with the backtest
import bdusage

from .config import Config
from .scanner import Candidate, lookup_token

log = logging.getLogger("memebot.ws")

WS_URL = "wss://public-api.birdeye.so/socket/solana"
REST_BASE = "https://public-api.birdeye.so"
STATUS_PATH = os.path.join("reports", "ws_status.json")
STATE_PATH = os.path.join("reports", "ws_state.json")
STATE_SAVE_SEC = 60        # periodic state-snapshot interval
COVERAGE_SLACK_SEC = 180   # first candle must be within this of listing time
MAX_CANDLE_MINTS = 200     # candle-store eviction cap
NUKE_BODY = 0.70           # loop8_eval.NUKE_BODY - body-collapse definition


def state_paths(strategy: str) -> tuple:
    """(status, state) snapshot paths for one strategy family.

    Each runner is its own process with its own watchlist and candle store, so
    they must not share these files - two bots writing one snapshot would keep
    restoring each other's funnel. The "base" family keeps the original
    filenames, so an existing single-bot deployment restarts warm as before.
    """
    if strategy == "base":
        return STATUS_PATH, STATE_PATH
    return (os.path.join("reports", f"ws_status-{strategy}.json"),
            os.path.join("reports", f"ws_state-{strategy}.json"))


class BirdeyeFeed:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.api_key = os.environ.get("BIRDEYE_API_KEY", "")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        self.connected = False
        self.connected_since = 0.0
        self.watch: dict = {}      # mint -> {symbol, listed_ts, liquidity}
        self.candles: dict = {}    # mint -> {ts: [ts,o,h,l,c,v_usd]}
        self._open_mints: List[str] = []
        self._subscribed: tuple = ()
        self.stats = {"msgs": 0, "listings_seen": 0, "backfills": 0,
                      "reconnects": 0, "last_msg_ts": 0.0}
        self._save_lock = threading.Lock()
        self.status_path, self.state_path = state_paths(cfg.strategy)
        if cfg.ws_persist_state:
            self._load_state()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if not self.api_key:
            log.error("BIRDEYE_API_KEY missing - websocket feed disabled")
            return
        self._thread = threading.Thread(target=self._run, name="birdeye-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        self._save_state()

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._connect_and_read()
                backoff = 2.0
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.connected = False
                self.stats["reconnects"] += 1
                log.warning("ws disconnected (%s); reconnecting in %.0fs", exc, backoff)
                self._write_status()
                self._save_state()
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
        self.connected = False
        self._write_status()
        self._save_state()

    def _connect_and_read(self) -> None:
        ws = websocket.create_connection(
            f"{WS_URL}?x-api-key={self.api_key}",
            header=["Origin: ws://public-api.birdeye.so"],
            subprotocols=["echo-protocol"],
            timeout=15,
        )
        self._ws = ws
        self.connected = True
        self.connected_since = time.time()
        log.info("ws connected (%s)", WS_URL)
        ws.send(json.dumps({"type": "SUBSCRIBE_TOKEN_NEW_LISTING",
                            "data": {"meme_platform_enabled": True}}))
        self._subscribed = ()          # force a price re-subscribe
        self._sync_price_subs(ws)
        ws.settimeout(5)
        last_ping = last_status = last_save = time.time()
        while not self._stop.is_set():
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                raw = None
            now = time.time()
            if raw:
                self._handle(raw)
            if now - last_ping >= 25:
                ws.ping()
                last_ping = now
            if now - last_status >= 15:
                self._sync_price_subs(ws)
                self._write_status()
                last_status = now
            if now - last_save >= STATE_SAVE_SEC:
                self._save_state()
                last_save = now
        ws.close()

    # ------------------------------------------------------------- messages
    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        self.stats["msgs"] += 1
        self.stats["last_msg_ts"] = time.time()
        t, d = msg.get("type"), msg.get("data") or {}
        if t == "TOKEN_NEW_LISTING_DATA":
            addr = d.get("address")
            if not addr:
                return
            try:
                liq = float(d.get("liquidity") or 0)
            except (TypeError, ValueError):
                liq = 0.0
            self.stats["listings_seen"] += 1
            if liq < self.cfg.ws_min_listing_liquidity:
                return
            with self._lock:
                # setdefault: listing events can re-broadcast; keep the original
                # listed_ts and the promoted flag instead of resetting them
                entry = self.watch.setdefault(addr, {
                    "symbol": (d.get("symbol") or "?").strip(),
                    "listed_ts": int(time.time()), "liquidity": liq})
                entry["liquidity"] = max(entry.get("liquidity", 0.0), liq)
                self._evict_locked()
        elif t == "PRICE_DATA":
            addr = d.get("address")
            ts = d.get("unixTime")
            if not addr or ts is None:
                return
            try:
                ts = int(ts)
                o, h, l, c = (float(d["o"]), float(d["h"]), float(d["l"]), float(d["c"]))
                v = float(d.get("v") or 0)
            except (KeyError, TypeError, ValueError):
                return
            with self._lock:
                self.candles.setdefault(addr, {})[ts] = [ts, o, h, l, c, v * c]

    def _evict_locked(self) -> None:
        max_age = self.cfg.max_age_min * 60
        now = time.time()
        dead = [m for m, w in self.watch.items() if now - w["listed_ts"] > max_age]
        for m in dead:
            self.watch.pop(m, None)
        if len(self.candles) > MAX_CANDLE_MINTS:
            keep = set(self._open_mints) | set(self.watch)
            for m in list(self.candles):
                if m not in keep and len(self.candles) > MAX_CANDLE_MINTS:
                    self.candles.pop(m, None)

    # --------------------------------------------------------- subscriptions
    def note_open_positions(self, mints: List[str]) -> None:
        with self._lock:
            self._open_mints = list(mints)

    def _sync_price_subs(self, ws) -> None:
        with self._lock:
            newest = sorted(self.watch.items(), key=lambda kv: -kv[1]["listed_ts"])
            want = list(dict.fromkeys(
                self._open_mints +
                [m for m, _ in newest[:max(0, self.cfg.ws_max_price_subs - len(self._open_mints))]]
            ))[:self.cfg.ws_max_price_subs]
        key = tuple(want)
        if key == self._subscribed or not want:
            return
        # complex query is a boolean-expression STRING (docs), not a JSON array
        query = " OR ".join(
            f"(address = {m} AND chartType = 1m AND currency = usd)" for m in want)
        ws.send(json.dumps({"type": "SUBSCRIBE_PRICE",
                            "data": {"queryType": "complex", "query": query}}))
        self._subscribed = key

    # ------------------------------------------------------------ entry gate
    def candle_rows(self, mint: str) -> list:
        with self._lock:
            rows = sorted(self.candles.get(mint, {}).values())
        return rows

    def backfill(self, session, mint: str, time_from: int, time_to: int) -> bool:
        """One REST OHLCV call to complete a late-discovered token's history."""
        bdusage.record("/defi/v3/ohlcv")
        try:
            r = session.get(f"{REST_BASE}/defi/v3/ohlcv",
                            params={"address": mint, "type": "1m", "currency": "usd",
                                    "time_from": time_from, "time_to": time_to},
                            headers={"x-api-key": self.api_key, "x-chain": "solana"},
                            timeout=20)
            items = ((r.json().get("data") or {}).get("items")) or []
        except Exception as exc:
            log.warning("ws backfill failed for %s: %s", mint, exc)
            return False
        self.stats["backfills"] += 1
        with self._lock:
            store = self.candles.setdefault(mint, {})
            for it in items:
                try:
                    ts = int(it.get("unix_time", it.get("unixTime")))
                    o, h, l, c = (float(it["o"]), float(it["h"]), float(it["l"]), float(it["c"]))
                    vol = it.get("v_usd", it.get("vUsd"))
                    vol = float(vol) if vol is not None else float(it.get("v", 0)) * c
                except (KeyError, TypeError, ValueError):
                    continue
                store.setdefault(ts, [ts, o, h, l, c, vol])
        return True

    def _bounce_gate(self, pre: list, now: int) -> tuple:
        """Live translation of loop8_eval.bounce_entry, config P3.

        Same scan the backtest ran: walk the candles from listing, track the
        credible peak (volume floor + close at least half the high, peak capped
        at 2x close), and fire on the FIRST credible green candle sitting
        `drawdown` below that peak with enough volume behind it.

        Two rules exist only live, both tightening:
          * the still-forming candle is dropped - the trigger tests its close
            and its volume, and neither is final until the minute is over;
          * a trigger that fired more than `max_trigger_age_min` ago is
            refused. The backtest bought at that candle's close; buying an
            hour later is a different, worse strategy, and pretending
            otherwise is how a backtest flatters itself.
        """
        cfg = self.cfg
        closed = [r for r in pre if r[0] <= now - 60]
        if len(closed) < 5:
            return False, f"bounce: only {len(closed)} closed candles"
        created = closed[0][0]
        peak, cum_vol = 0.0, 0.0
        nukes = 0
        for ts, o, h, _l, cl, v in closed:
            if (ts - created) / 60 > cfg.bounce_max_wait_min:
                return False, (f"bounce: no trigger within "
                               f"{cfg.bounce_max_wait_min:.0f}m of listing")
            cum_vol += v
            credible = v >= cfg.bounce_credible_vol_usd and cl >= 0.5 * h
            if credible:
                peak = max(peak, min(h, cl * 2))
            if (peak > 0 and credible and cl > o
                    and cl / peak <= 1 - cfg.bounce_drawdown
                    and v >= cfg.bounce_trigger_vol_usd
                    and cum_vol >= cfg.bounce_min_cum_vol_usd):
                if cfg.bounce_max_nukes is not None and nukes > cfg.bounce_max_nukes:
                    return False, (f"bounce: {nukes} collapse candles before trigger "
                                   f"> max {cfg.bounce_max_nukes}")
                age = (now - ts) / 60
                if age > cfg.bounce_max_trigger_age_min:
                    return False, (f"bounce: trigger fired {age:.0f}m ago > max "
                                   f"{cfg.bounce_max_trigger_age_min:.0f}m; not chasing")
                return True, (f"bounce ok ({100 * (1 - cl / peak):.0f}% off peak, "
                              f"trigger {age:.1f}m ago, {len(closed)} candles, "
                              f"cum vol ${cum_vol:,.0f})")
            if o > 0 and cl / o <= NUKE_BODY:
                nukes += 1
        return False, "bounce: no trigger yet"

    def setup_gate(self, cand: Candidate, session, budget: list) -> tuple:
        """Run the backtest's exact base-entry gate on live candles.
        `budget` is a single-element list of remaining REST backfills this
        cycle (mutated). Requires candle coverage from listing; without it
        the trade is skipped - the backtest population always had full
        history, so entering blind would be a different strategy."""
        now = int(time.time())
        wl = self.watch.get(cand.mint)
        created = wl["listed_ts"] if wl else None
        if created is None and cand.age_min is not None:
            created = now - int(cand.age_min * 60)
        if created is None:
            return False, "setup: unknown listing time"
        rows = self.candle_rows(cand.mint)
        # re-backfill when the stored tail is stale (no live sub, or restored
        # from a pre-restart snapshot) - the gate must judge current candles,
        # like the backtest did, not an hours-old pattern
        stale = bool(rows) and now - rows[-1][0] > COVERAGE_SLACK_SEC
        if (not rows or rows[0][0] > created + COVERAGE_SLACK_SEC or stale) and \
                self.cfg.ws_backfill and budget[0] > 0:
            budget[0] -= 1
            self.backfill(session, cand.mint, created, now)
            rows = self.candle_rows(cand.mint)
        if not rows or rows[0][0] > created + COVERAGE_SLACK_SEC:
            return False, "setup: no candle coverage from listing"
        pre = [r for r in rows if r[0] <= now]
        if len(pre) < 8:
            return False, f"setup: only {len(pre)} candles"
        price = pre[-1][4]
        if price <= 0:
            return False, "setup: bad last price"
        cum_vol = sum(r[5] for r in pre)
        if cum_vol < self.cfg.ws_min_cum_vol_usd:
            return False, f"setup: cum vol ${cum_vol:,.0f} < ${self.cfg.ws_min_cum_vol_usd:,.0f}"
        if self.cfg.ws_setup_family == "bounce":
            return self._bounce_gate(pre, now)
        # The 2026-07-27 search families. Same screen_ok() the backtest scored,
        # so the live gate cannot drift from the validated definition.
        if self.cfg.ws_setup_family in strategies.STRATEGIES:
            fam = self.cfg.ws_setup_family
            if not strategies.screen_ok(pre, now, price, fam):
                return False, f"setup: {fam} screen failed"
            return True, f"setup ok ({fam}, {len(pre)} candles, cum vol ${cum_vol:,.0f})"
        if not backtest.entry_setup_ok(pre, now, price, backtest.DEFAULT_SETUP):
            return False, "setup: base-entry pattern failed"
        return True, f"setup ok ({len(pre)} candles, cum vol ${cum_vol:,.0f})"

    # ------------------------------------------------------------- discovery
    def _bd_json(self, session, path: str, params: dict):
        bdusage.record(path)
        try:
            r = session.get(f"{REST_BASE}{path}", params=params,
                            headers={"x-api-key": self.api_key, "x-chain": "solana"},
                            timeout=15)
            if r.status_code != 200:
                log.debug("bd rest %s -> %s", path, r.status_code)
                return None
            return (r.json() or {}).get("data") or None
        except Exception as exc:
            log.debug("bd rest %s failed: %s", path, exc)
            return None

    def qualify(self, session, mint: str, wl: dict) -> Optional[Candidate]:
        """Qualification stats from Birdeye REST: market-data (price /
        liquidity / fdv) + trade-data/single (windowed volume, buy/sell
        counts, price changes). Field names verified against live responses
        2026-07-23. pool=mint follows the bdfetch convention for
        Birdeye-sourced tokens (execution trades by mint)."""
        md = self._bd_json(session, "/defi/v3/token/market-data", {"address": mint})
        td = self._bd_json(session, "/defi/v3/token/trade-data/single", {"address": mint})
        if not md or not td:
            return None

        def f(src, key):
            try:
                return float(src.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        return Candidate(
            mint=mint, pool=mint, symbol=(wl.get("symbol") or "?"),
            price_usd=f(md, "price"),
            liquidity_usd=f(md, "liquidity"),
            fdv_usd=f(md, "fdv") or f(md, "market_cap"),
            age_min=max(0.0, (time.time() - wl["listed_ts"]) / 60),
            vol_m5=f(td, "volume_5m_usd"),
            vol_h1=f(td, "volume_1h_usd"),
            buys_m5=int(f(td, "buy_5m")),
            sells_m5=int(f(td, "sell_5m")),
            chg_m5=f(td, "price_change_5m_percent"),
            chg_h1=f(td, "price_change_1h_percent"),
            source="ws-birdeye",
        )

    def promote_candidates(self, session, known: set,
                           limit: Optional[int] = None) -> List[Candidate]:
        """Watchlist tokens that aged into the entry window, as scanner
        Candidates. Birdeye REST fills the funnel stats; GeckoTerminal is
        the fallback if Birdeye errors."""
        limit = limit if limit is not None else self.cfg.ws_max_promotions_per_cycle
        now = time.time()
        with self._lock:
            ripe = [(m, dict(w)) for m, w in self.watch.items()
                    if m not in known
                    and self.cfg.min_age_min * 60 <= now - w["listed_ts"] <= self.cfg.max_age_min * 60
                    and not w.get("promoted")]
        out = []
        for mint, wl in ripe[:limit]:
            cand = self.qualify(session, mint, wl) or lookup_token(session, mint)
            with self._lock:
                if mint in self.watch:
                    self.watch[mint]["promoted"] = True
            if cand:
                out.append(cand)
            time.sleep(self.cfg.api_pause_sec)
        if out:
            log.info("ws discovery: promoted %s",
                     ", ".join(f"{c.symbol}[{c.source}]" for c in out))
        return out

    # ---------------------------------------------------------------- status
    def _write_status(self) -> None:
        try:
            os.makedirs("reports", exist_ok=True)
            with self._lock:
                snap = {"connected": self.connected, "since": self.connected_since,
                        "watchlist": len(self.watch), "price_subs": len(self._subscribed),
                        "candle_mints": len(self.candles), **self.stats,
                        "updated": time.time()}
            tmp = self.status_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f)
            os.replace(tmp, self.status_path)
        except OSError:
            pass

    # ----------------------------------------------------------- persistence
    def _save_state(self) -> None:
        """Snapshot watchlist/candles/counters so a restart resumes warm.
        Atomic replace; the save lock stops the ws thread and stop() from
        racing on the temp file."""
        if not self.cfg.ws_persist_state:
            return
        with self._lock:
            snap = {"saved_at": time.time(),
                    "watch": {m: dict(w) for m, w in self.watch.items()},
                    "candles": {m: dict(rows) for m, rows in self.candles.items()},
                    "stats": dict(self.stats)}
        try:
            os.makedirs("reports", exist_ok=True)
            with self._save_lock:
                tmp = self.state_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(snap, f, separators=(",", ":"))
                os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _load_state(self) -> None:
        """Restore the previous run's snapshot (called before the ws thread
        starts). Age eviction applies as usual; promoted flags survive so
        restarts never re-promote; JSON string keys go back to int ts."""
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError):
            return
        now = time.time()
        max_age = self.cfg.max_age_min * 60
        watch: dict = {}
        for mint, w in (snap.get("watch") or {}).items():
            try:
                listed = int(w["listed_ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if now - listed > max_age:
                continue
            entry = {"symbol": str(w.get("symbol") or "?"),
                     "listed_ts": listed,
                     "liquidity": float(w.get("liquidity") or 0)}
            if w.get("promoted"):
                entry["promoted"] = True
            watch[mint] = entry
        candles: dict = {}
        for mint, rows in (snap.get("candles") or {}).items():
            store = {}
            for ts, row in (rows or {}).items():
                try:
                    store[int(ts)] = [int(row[0])] + [float(x) for x in row[1:6]]
                except (TypeError, ValueError, IndexError):
                    continue
            if store:
                candles[mint] = store
        with self._lock:
            self.watch = watch
            self.candles = candles
            prev = snap.get("stats") or {}
            for k in ("msgs", "listings_seen", "backfills", "reconnects"):
                try:
                    self.stats[k] = int(prev.get(k) or 0)
                except (TypeError, ValueError):
                    pass
            self._evict_locked()
        saved_at = float(snap.get("saved_at") or now)
        log.info("ws state restored: %d watchlist, %d candle mints (snapshot %.0fs old)",
                 len(watch), len(candles), now - saved_at)
