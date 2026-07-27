#!/usr/bin/env python
"""Per-trade journal cards for the adopted strategy over the Birdeye sample.

Renders reports/trade_cards.html: one dark card per simulated trade with a
candlestick chart, a TradingView-style long-position drawing (reward zone
entry->target, risk zone entry->stop, spanning entry->exit time, with an R/R
chip), Entry/Stop/Target/Exit level lines, entry arrow, WIN/LOSS marker and a
position-levels panel. Self-contained inline SVG - no external libraries,
safe to serve from the NERV dashboard (/trades).

Why the drawing lives here and not behind the Birdeye button: birdeye.so URLs
only take ?chain= - the chart is a TradingView embed whose visible range and
drawings are client-side state, so a link cannot jump to a past moment or
inject a position drawing. The card chart IS that view.

Usage:
  python tradecards.py --tokens-json reports/bd_tokens.json --cache-dir .bd_cache
                       [--asym-config config.asym.json] [--entry-age 30]
                       [--min-entry-vol 8000] [--max-cards 80]
                       [--token-supply 1e9] [--sol-usd 185]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
import bdusage
from bot import manip

GREEN, RED, CYAN, AMBER, INK2, INK3 = "#00a869", "#ff3355", "#2695c4", "#ffc233", "#9a9aa8", "#5c5c6c"
POSITION_SOL = 0.25


def fmt_mcap(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e4:
        return f"${v / 1e3:.1f}K"
    if v >= 1e3:
        return f"${v / 1e3:.2f}K"
    return f"${v:,.0f}"


def fetch_sol_usd() -> float | None:
    """SOL/USD spot at generation time; CoinGecko first, Binance fallback."""
    import urllib.request

    for url in ("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT"):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                data = json.load(r)
            return float(data["solana"]["usd"] if "solana" in data else data["price"])
        except Exception:
            continue
    return None


def fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))


def resample(candles: list, bucket_sec: int) -> list:
    out = []
    for ts, o, h, l, c, v in candles:
        b = ts - ts % bucket_sec
        if out and out[-1][0] == b:
            r = out[-1]
            r[2] = max(r[2], h)
            r[3] = min(r[3], l)
            r[4] = c
            r[5] += v
        else:
            out.append([b, o, h, l, c, v])
    return out


def entry_tiles(pre: list, entry_ts: int, entry: float, setup: dict | None,
                min_entry_vol: float, supply: float) -> str:
    """AT-ENTRY tile panel: stats computed ONLY from pre-entry candles at the
    entry timestamp (same time-based windows as backtest.entry_setup_ok).
    Holder/sniper/insider/security stats are deliberately absent - no source
    provides them as-of a historical timestamp, and present-day values labeled
    "at entry" would be lookahead contamination."""
    s = setup or backtest.DEFAULT_SETUP
    base = [c for c in pre if c[0] > entry_ts - s["base_window_min"] * 60]
    rng_pct = (100 * (max(c[2] for c in base) - min(c[3] for c in base)) / entry
               if base else None)
    cred_peak, nukes = 0.0, 0
    for _ts, o, h, _l, cl, v in pre:
        if v >= s["cred_vol_usd"] and cl >= 0.5 * h:
            cred_peak = max(cred_peak, min(h, cl * 2))
        if o > 0 and cl / o <= s["nuke_body"]:
            nukes += 1
    frac = entry / cred_peak if cred_peak > 0 else None
    refs = [c[4] for c in pre if c[0] <= entry_ts - s["trend_window_min"] * 60]
    ref = refs[-1] if refs else pre[0][4]
    trend = entry / ref if ref > 0 else None
    vol_pre = sum(c[5] for c in pre)
    vol5 = sum(c[5] for c in pre if c[0] > entry_ts - 300)
    age_min = (entry_ts - pre[0][0]) / 60

    def tile(val, label, ok, tip):
        col = "#e8e8ea" if ok is None else (GREEN if ok else RED)
        return (f'<div class="ti" title="{tip}"><div class="v" style="color:{col}">{val}</div>'
                f'<div class="l">{label}</div></div>')

    tiles = [
        tile(f"{rng_pct:.1f}%" if rng_pct is not None else "flat",
             f"{s['base_window_min']}m Range",
             rng_pct is None or rng_pct <= s["max_base_range_pct"],
             f"high−low of the last {s['base_window_min']}m as % of entry · gate ≤{s['max_base_range_pct']:.0f}%"),
        tile(str(nukes), "Nukes", nukes <= s["max_nukes"],
             f"collapse candles (close/open ≤{s['nuke_body']:.2f}) over the whole pre-entry life · gate ≤{s['max_nukes']}"),
        tile(f"{100 * frac:.0f}%" if frac is not None else "—", "Of Peak",
             frac is None or frac >= s["min_frac_of_peak"],
             f"entry vs credible peak (candle vol ≥${s['cred_vol_usd']:.0f}, wash-guarded) · gate ≥{s['min_frac_of_peak']:.0%}"),
        tile(f"{trend:.2f}x" if trend is not None else "—",
             f"{s['trend_window_min']}m Trend",
             trend is None or s["trend_lo"] <= trend <= s["trend_hi"],
             f"entry / close {s['trend_window_min']}m earlier · gate {s['trend_lo']:.2f}–{s['trend_hi']:.2f}x"),
        tile(fmt_mcap(vol_pre), "Vol to Entry", vol_pre >= min_entry_vol,
             f"cumulative USD volume, listing → entry · gate ≥${min_entry_vol:,.0f}"),
        tile(fmt_mcap(vol5), "Vol 5m", None, "USD volume in the 5 minutes before entry"),
        tile(f"{age_min:.0f}m", "Age", None, "minutes from first candle to entry"),
        tile(fmt_mcap(entry * supply), "MCap", None, "market cap at entry (assumed supply)"),
    ]
    return ('<div class="sh" style="margin-top:14px">At entry · no hindsight</div>'
            '<div class="tgrid">' + "".join(tiles) + "</div>")


def svg_chart(candles: list, levels: dict, entry_ts: int, end_ts: int, win: bool, supply: float) -> str:
    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 760, 396, 8, 108, 34, 26
    VOL_H, VOL_GAP = 52, 10  # volume lane under the price plot
    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B - VOL_H - VOL_GAP
    vol_top, vol_bot = PAD_T + plot_h + VOL_GAP, H - PAD_B
    lows = [c[3] for c in candles] + [levels["stop"][0], levels["target"][0]]
    highs = [c[2] for c in candles] + [levels["entry"][0]]
    lo, hi = min(lows), max(highs)
    span = (hi - lo) or 1e-12
    lo, hi = max(0.0, lo - span * 0.06), hi + span * 0.10
    span = hi - lo

    def Y(p):
        return PAD_T + (hi - p) / span * plot_h

    def X(i):
        return PAD_L + (i + 0.5) * plot_w / len(candles)

    bw = max(2.0, min(14.0, plot_w / len(candles) * 0.66))
    parts = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
             f'style="width:100%;height:auto;display:block" font-family="Cascadia Mono,Consolas,monospace">']
    # recessive horizontal grid + right market-cap ticks
    for k in range(5):
        p = hi - span * k / 4
        y = Y(p)
        parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="#1e1e2a" stroke-width="1"/>')
        parts.append(f'<text x="{W - PAD_R + 6}" y="{y + 3.5:.1f}" font-size="10" fill="{INK3}">{fmt_mcap(p * supply)}</text>')
    # time ticks
    for frac in (0.08, 0.5, 0.92):
        i = int(frac * (len(candles) - 1))
        parts.append(f'<text x="{X(i):.1f}" y="{H - 8}" font-size="10" fill="{INK3}" text-anchor="middle">'
                     f'{time.strftime("%H:%M", time.gmtime(candles[i][0]))}</text>')
    # candles
    ei = min(range(len(candles)), key=lambda i: abs(candles[i][0] - entry_ts))
    xi = min(range(len(candles)), key=lambda i: abs(candles[i][0] - end_ts))
    for i, (ts, o, h, l, c, v) in enumerate(candles):
        col = GREEN if c >= o else RED
        x = X(i)
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{Y(h):.1f}" y2="{Y(l):.1f}" stroke="{col}" stroke-width="1"/>')
        top, bot = Y(max(o, c)), Y(min(o, c))
        parts.append(f'<rect x="{x - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(1.5, bot - top):.1f}" '
                     f'fill="{col}" rx="1"/>')
    # volume lane: per-candle USD volume, candle-direction color, shared x-slots
    maxv = max((c[5] for c in candles), default=0) or 1
    parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{vol_bot}" y2="{vol_bot}" stroke="#1e1e2a" stroke-width="1"/>')
    parts.append(f'<text x="{W - PAD_R + 6}" y="{vol_top + 8}" font-size="10" fill="{INK3}">VOL {fmt_mcap(maxv)}</text>')
    for i, (ts, o, h, l, c, v) in enumerate(candles):
        if v <= 0:
            continue
        vh = max(1.0, v / maxv * VOL_H)
        parts.append(f'<rect x="{X(i) - bw / 2:.1f}" y="{vol_bot - vh:.1f}" width="{bw:.1f}" '
                     f'height="{vh:.1f}" fill="{GREEN if c >= o else RED}" opacity="0.5" rx="1"/>')
    # long-position drawing (TradingView-style): translucent reward box from
    # entry up to target, risk box from entry down to stop, spanning the trade
    # window (entry candle -> exit candle), with an R/R chip on the entry line
    entry_p, tgt_p, stop_p = levels["entry"][0], levels["target"][0], levels["stop"][0]
    px1 = X(ei) - bw / 2
    px2 = max(X(xi) + bw / 2, px1 + 26)
    y_e = min(max(Y(entry_p), PAD_T), PAD_T + plot_h)
    y_t = max(Y(tgt_p), PAD_T)
    y_s = min(Y(stop_p), PAD_T + plot_h)
    parts.append(f'<rect x="{px1:.1f}" y="{y_t:.1f}" width="{px2 - px1:.1f}" height="{max(1.0, y_e - y_t):.1f}" '
                 f'fill="{GREEN}" fill-opacity="0.16" stroke="{GREEN}" stroke-opacity="0.55" stroke-width="1"/>')
    parts.append(f'<rect x="{px1:.1f}" y="{y_e:.1f}" width="{px2 - px1:.1f}" height="{max(1.0, y_s - y_e):.1f}" '
                 f'fill="{RED}" fill-opacity="0.16" stroke="{RED}" stroke-opacity="0.55" stroke-width="1"/>')
    rr = (tgt_p - entry_p) / max(1e-12, entry_p - stop_p)
    cx = (px1 + px2) / 2
    parts.append(f'<g><rect x="{cx - 40:.1f}" y="{y_e - 8:.1f}" width="80" height="15" rx="3" '
                 f'fill="#101017" stroke="#3d55b8" stroke-width="1"/>'
                 f'<text x="{cx:.1f}" y="{y_e + 3.5:.1f}" font-size="10" fill="#cfd8ff" '
                 f'text-anchor="middle">R/R {rr:.2f}</text></g>')
    # level lines + chips (price plot only)
    for key, (price, label, col) in levels.items():
        y = Y(price)
        dash = ' stroke-dasharray="5 4"'
        parts.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{col}" stroke-width="1.4"{dash} opacity="0.9"/>')
        ly = min(max(y, PAD_T + 8), PAD_T + plot_h - 8)
        parts.append(f'<g><rect x="{W - PAD_R - 96}" y="{ly - 8:.1f}" width="92" height="15" fill="{col}" rx="2"/>'
                     f'<text x="{W - PAD_R - 50}" y="{ly + 3.5:.1f}" font-size="10" fill="#000" text-anchor="middle" '
                     f'font-weight="bold">{label} {fmt_mcap(price * supply)}</text></g>')
    # entry arrow + result marker
    ex = X(ei)
    ey = Y(max(c[2] for c in candles[max(0, ei - 1):ei + 2])) - 6
    res_col = GREEN if win else RED
    parts.append(f'<text x="{ex:.1f}" y="{max(12, ey - 26):.1f}" font-size="11" fill="{res_col}" text-anchor="middle" '
                 f'font-weight="bold">{"WIN" if win else "LOSS"}</text>')
    parts.append(f'<circle cx="{ex:.1f}" cy="{max(20, ey - 18):.1f}" r="5" fill="{res_col}"/>')
    parts.append(f'<text x="{ex:.1f}" y="{max(30, ey - 4):.1f}" font-size="10" fill="{CYAN}" text-anchor="middle">Entry</text>')
    parts.append(f'<path d="M {ex - 5:.1f} {ey:.1f} h 10 l -5 8 z" fill="{CYAN}"/>')
    # exit marker
    parts.append(f'<circle cx="{X(xi):.1f}" cy="{Y(candles[xi][4]):.1f}" r="4" fill="none" stroke="{AMBER}" stroke-width="2"/>')
    parts.append("</svg>")
    return "".join(parts)


class Syn:
    """Lightweight stand-in for a backtest Token, for the copy/cluster
    strategies whose 'tokens' are just a mint + symbol from a wallet's buy."""
    def __init__(self, mint: str, symbol: str, pool: str = ""):
        self.mint, self.symbol, self.pool = mint, symbol, pool


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _load_copytrade_candles(cache_dir: str, mint: str):
    """Candles for a copy/cluster token from the copytrade OHLCV cache
    (files named {mint}_{resolution}.json); prefers the finest/longest series."""
    import glob
    best = None
    for path in glob.glob(os.path.join(cache_dir, mint + "_*.json")):
        c = _read_json(path)
        cs = (c or {}).get("candles")
        if cs and (best is None or len(cs) > len(best)):
            best = cs
    return best


def build_follower_trades(args, exits):
    """(copy_all, cluster) trade lists as (Syn, candles, sim) tuples, replaying
    each monitored-wallet buy at trigger + follow-lag through this bot's exit
    doctrine over the cached candles. setup gate is OFF (the follower entered on
    the wallet signal, not the base-entry pattern)."""
    data = _read_json(args.cluster_json)
    copy_all, cluster = [], []
    if not data or "card_trades" not in data:
        return copy_all, cluster
    lag = args.follow_lag * 60
    for key, bucket in (("copy_all", copy_all), ("cluster", cluster)):
        for rec in data["card_trades"].get(key, []):
            candles = _load_copytrade_candles(args.copytrade_cache, rec["mint"])
            if not candles:
                continue
            entry_ts = int(rec["trigger_ts"]) + lag
            age = (entry_ts - candles[0][0]) / 60
            if age < 0:
                continue
            sim = backtest.simulate(candles, exits, age, args.cost_pct, 0.0, setup=None)
            if sim:
                bucket.append((Syn(rec["mint"], rec.get("symbol", "?")), candles, sim))
    return copy_all, cluster


MANIP_COL = {"ORGANIC": GREEN, "RUG": RED, "WASH_RAMP": AMBER, "SPIKE_DUMP": AMBER}


def build_homerun_trades(args):
    """Home-run journal cards: sample tokens passing the moonshot screen
    (low mcap + momentum + organic), simulated with the ride-to-30x exit
    doctrine from config.homerun.json."""
    cfg = _read_json("config.homerun.json") or {}
    hexits = cfg.get("exits")
    scr = cfg.get("homerun_screen") or {}
    if not hexits:
        return []
    max_mcap = float(scr.get("max_entry_mcap_usd", 25000))
    min_mom = float(scr.get("min_momentum_15m", 1.3))
    supply = float(scr.get("supply", 1e9))
    out = []
    for tok in backtest.load_token_index(args.tokens_json):
        candles = backtest.load_cached_candles(args.cache_dir, tok.pool)
        if not candles:
            continue
        entry_ts = candles[0][0] + args.entry_age * 60
        pre = [c for c in candles if c[0] <= entry_ts]
        if not pre or pre[-1][4] <= 0:
            continue
        entry = pre[-1][4]
        ref = [c[4] for c in pre if c[0] <= entry_ts - 15 * 60 and c[4] > 0]
        mom = entry / ref[-1] if ref else 1.0
        if entry * supply > max_mcap or mom < min_mom:
            continue  # cheap screen first; classify only survivors
        if (manip.classify(pre, False) or {}).get("class") != "ORGANIC":
            continue
        sim = backtest.simulate(candles, hexits, args.entry_age, args.cost_pct,
                                args.min_entry_vol, setup=None)
        if sim:
            out.append((tok, candles, sim))
    return out


def build_screen_trades(key):
    """Journal cards for a family strategy (sniper/volume_anomaly/dip) from the
    per-trade records in newstrat_backtest.json: load each token's candles and
    re-simulate at its recorded entry with the strategy's exits."""
    data = _read_json(os.path.join("reports", "newstrat_backtest.json")) or {}
    sp = (data.get("strategies") or {}).get(key)
    if not sp:
        return [], None
    exits = sp["exits"]
    out = []
    for rec in sp.get("card_trades", []):
        candles = (backtest.load_cached_candles(".bd_cache_ext", rec["pool"])
                   or backtest.load_cached_candles(".bd_cache", rec["pool"]))
        if not candles:
            continue
        age = (int(rec["entry_ts"]) - candles[0][0]) / 60
        if age < 0:
            continue
        sim = backtest.simulate(candles, exits, age, 4.0, 1500, setup=None)
        if sim:
            out.append((Syn(rec["mint"], rec.get("symbol", "?"), rec.get("pool", "")), candles, sim))
    return out, exits


def build_card(idx, tok, candles, sim, exits, tp1, setup, supply, min_entry_vol,
               sol_usd, strat, verdict=None) -> str:
    """One journal card. `strat` tags it (data-strat) for the strategy filter;
    `verdict` (manipscan) tags it (data-manip) + adds a scam/organic badge."""
    entry_p, entry_ts, end_ts = sim["entry_price"], sim["entry_ts"], sim["end_ts"]
    m = sim["multiple"]
    win = m > 1.0
    stop_p = entry_p * (1 - exits["stop_loss_pct"] / 100)
    tgt_p = entry_p * float(tp1["multiple"])
    exit_p = entry_p * sim["events"][-1]["mult"] if sim["events"] else entry_p
    net = POSITION_SOL * (m - 1)
    window = [c for c in candles if entry_ts - 5400 <= c[0] <= end_ts + 3600]
    if len(window) < 8:
        window = candles[:120]
    bucket = max(60, int((window[-1][0] - window[0][0]) / 55 // 60 * 60) or 60)
    view = resample(window, bucket)
    levels = {
        "stop":   (stop_p, "Stop", RED),
        "exit":   (exit_p, "Exit", AMBER),
        "entry":  (entry_p, "Entry", CYAN),
        "target": (tgt_p, "Target", GREEN),
    }
    chart = svg_chart(view, levels, entry_ts, end_ts, win, supply)
    pre = [c for c in candles if c[0] <= entry_ts]
    at_entry = entry_tiles(pre, int(entry_ts), entry_p, setup, min_entry_vol, supply)
    sym = html.escape(tok.symbol or "?")
    res_cls = "win" if win else "loss"
    if verdict:
        mcls = verdict["class"]
        mcol = MANIP_COL.get(mcls, INK2)
        mtip = (f"human {verdict['human']} · manip {verdict['manip']} · sweet {verdict['sweet']}"
                f" · life {verdict['lifespan_min']:.0f}m · rise {verdict['rise']}x"
                f" · two-sided vol {verdict['red_vol_frac']:.0%} · ER {verdict['er']}")
        badge = f' · <span style="color:{mcol}" title="{mtip}">{mcls}</span>'
        data_manip = mcls
    else:
        badge, data_manip = "", "clean"
    rows = "".join(
        f'<div class="lv"><span>{k}</span><span style="color:{c}">{fmt_mcap(p * supply)}</span></div>'
        for k, p, c in (("Entry", entry_p, CYAN), ("Stop", stop_p, RED),
                        ("Target", tgt_p, GREEN), ("Exit", exit_p, AMBER)))
    if sol_usd is not None:
        size_usd = f'<span class="usd">≈ ${POSITION_SOL * sol_usd:,.2f}</span>'
        res_usd = (f'<span class="usd">≈ ${POSITION_SOL * sol_usd:,.2f} → '
                   f'${POSITION_SOL * m * sol_usd:,.2f} '
                   f'({"+" if net >= 0 else "-"}${abs(net * sol_usd):,.2f})</span>')
    else:
        size_usd = res_usd = ""
    pool_attr = html.escape(getattr(tok, "pool", "") or "")
    return f"""
<div class="card {res_cls}" data-strat="{strat}" data-manip="{data_manip}" data-mint="{html.escape(tok.mint)}">
  <div class="chead">
    <div>
      <div class="t1">#{idx} {sym} LONG · <span class="{res_cls}-t">{"WIN" if win else "LOSS"}</span>{badge}</div>
      <div class="t2">{fmt_ts(entry_ts)} → {fmt_ts(end_ts)} UTC · {m:.2f}x · net {net:+.4f} SOL
        · {html.escape(sim["reason"])}</div>
    </div>
    <div class="actions">
      <button class="btn blkbtn" data-mint="{html.escape(tok.mint)}" data-symbol="{sym}"
        data-pool="{pool_attr}" data-result="{"win" if win else "loss"}"
        data-mult="{m:.3f}" data-entry="{int(entry_ts)}"
        title="Flag this chart as an avoid-pattern (wash ramp / painted prices / scam). FORWARD-ONLY: this trade stays in the current stats — flags never edit existing results. Future listings matching the mint/symbol are excluded, and the chart is filed on the BLACKLIST tab.">Blacklist</button>
      <a class="btn" href="https://birdeye.so/token/{html.escape(tok.mint)}?chain=solana" target="_blank"
       title="Opens the live Birdeye token page. Birdeye URLs can't deep-link a past chart moment or draw on their chart (TradingView embed, client-side state) — the long-position drawing for this exact trade is on the card chart, entry {fmt_ts(entry_ts)} UTC.">View on Birdeye</a>
    </div>
  </div>
  <div class="cbody">
    <div class="chart">{chart}</div>
    <div class="side">
      <div class="sh">Position levels</div>
      {rows}
      <div class="lv"><span>Size</span><span>{POSITION_SOL} SOL{size_usd}</span></div>
      <div class="lv"><span>Result</span><span class="{res_cls}-t">{m:.2f}x ({POSITION_SOL}→{POSITION_SOL * m:.3f}){res_usd}</span></div>
      {at_entry}
    </div>
  </div>
</div>"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Render per-trade journal cards")
    ap.add_argument("--tokens-json", default=os.path.join("reports", "bd_tokens.json"))
    ap.add_argument("--cache-dir", default=".bd_cache")
    ap.add_argument("--asym-config", default="config.asym.json")
    ap.add_argument("--entry-age", type=float, default=30)
    ap.add_argument("--cost-pct", type=float, default=4)
    ap.add_argument("--min-entry-vol", type=float, default=8000)
    ap.add_argument("--max-cards", type=int, default=80)
    ap.add_argument("--token-supply", type=float, default=1e9,
                    help="assumed token supply for market-cap display (1B = standard launch)")
    ap.add_argument("--sol-usd", type=float, default=None,
                    help="SOL/USD for USD figures (default: fetch spot from CoinGecko/Binance)")
    ap.add_argument("--no-setup-filter", action="store_true",
                    help="disable the base-entry setup filter and show every age/volume entry")
    ap.add_argument("--cluster-json", default=os.path.join("reports", "cluster_backtest.json"),
                    help="source of copy/cluster per-trade records + cached candles")
    ap.add_argument("--copytrade-cache", default=".copytrade_cache",
                    help="OHLCV cache dir for the copy/cluster strategies")
    ap.add_argument("--follow-lag", type=int, default=5,
                    help="entry lag (min) applied to copy/cluster trigger buys")
    ap.add_argument("--out", default=os.path.join("reports", "trade_cards.html"))
    args = ap.parse_args()
    setup = None if args.no_setup_filter else backtest.DEFAULT_SETUP

    sol_usd = args.sol_usd if args.sol_usd is not None else fetch_sol_usd()
    if sol_usd is None:
        print("warning: SOL/USD unavailable (offline?) - USD figures omitted; pass --sol-usd to set one")

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with open(args.asym_config, "r", encoding="utf-8") as f:
        exits = json.load(f)["exits"]

    tp1 = exits["take_profits"][0]
    # --- base strategy: backtest over the Birdeye sample ---
    base_trades = []
    for tok in backtest.load_token_index(args.tokens_json):
        candles = backtest.load_cached_candles(args.cache_dir, tok.pool)
        if not candles:
            continue
        sim = backtest.simulate(candles, exits, args.entry_age, args.cost_pct,
                                args.min_entry_vol, setup=setup)
        if sim:
            base_trades.append((tok, candles, sim))

    # --- copy / cluster strategies (from cluster_backtest.py records) ---
    copy_trades, cluster_trades = build_follower_trades(args, exits)

    # --- home-run strategy (moonshot screen + ride-to-30x, config.homerun.json) ---
    homerun_trades = build_homerun_trades(args)
    hr_exits = (_read_json("config.homerun.json") or {}).get("exits") or exits

    # --- family strategies (sniper / volume_anomaly / dip) from newstrat_backtest.json ---
    sniper_trades, sniper_ex = build_screen_trades("sniper")
    vol_trades, vol_ex = build_screen_trades("volanomaly")
    dip_trades, dip_ex = build_screen_trades("dip")

    # manipulation verdicts (manipscan.py): scam vs human/organic, per mint
    manip = _read_json(os.path.join("reports", "manipscan.json")) or {}

    def _tp(ex):  # card target line: the bank level, or hard_tp for ride-through exits
        tps = ex.get("take_profits") or []
        if tps and float(tps[0].get("sell_fraction_of_remaining", 0)) > 0:
            return tps[0]
        return {"multiple": ex.get("hard_tp_multiple", 2)}

    RANK_BY_MULT = ("homerun", "sniper", "volanomaly", "dip")
    strat_defs = [
        ("base", "Base-entry", base_trades, setup, exits),
        ("copy_all", "Copy-all", copy_trades, None, exits),
        ("cluster", "Cluster-confirmed", cluster_trades, None, exits),
        ("homerun", "Home-run", homerun_trades, None, hr_exits),
        ("sniper", "Sniper", sniper_trades, None, sniper_ex or exits),
        ("volanomaly", "Volume-anomaly", vol_trades, None, vol_ex or exits),
        ("dip", "Dip-buy", dip_trades, None, dip_ex or exits),
    ]
    strat_meta, cards = {}, []
    for key, label, tr, st, sexits in strat_defs:
        cnt = len(tr)
        w = sum(1 for _, _, s in tr if s["multiple"] > 1.0)
        av = sum(s["multiple"] for _, _, s in tr) / cnt if cnt else 0
        organic = sum(1 for tok, _, _ in tr if (manip.get(tok.mint) or {}).get("class") == "ORGANIC")
        strat_meta[key] = {"label": label, "n": cnt,
                           "wr": round(100 * w / cnt, 1) if cnt else 0, "avg": round(av, 3),
                           "organic": organic if key in ("copy_all", "cluster") else None}
        # base: newest first. home-run/family: biggest realized multiple first.
        # copy/cluster: human x manip "sweet" score.
        if key == "base":
            ordered = sorted(tr, key=lambda t: -t[2]["entry_ts"])
        elif key in RANK_BY_MULT:
            ordered = sorted(tr, key=lambda t: -t[2]["multiple"])
        else:
            def _rank(t):
                v = manip.get(t[0].mint) or {}
                return (0 if v.get("class") == "ORGANIC" else 1, -v.get("sweet", -1.0))
            ordered = sorted(tr, key=_rank)
        stp1 = _tp(sexits)
        for idx, (tok, candles, sim) in enumerate(ordered[:args.max_cards], 1):
            verdict = manip.get(tok.mint) if key in ("copy_all", "cluster") else None
            cards.append(build_card(idx, tok, candles, sim, sexits, stp1, st,
                                    args.token_supply, args.min_entry_vol, sol_usd, key, verdict))

    # base-strategy figures drive the header prose + the SORTIE-LOG feed
    n = strat_meta["base"]["n"]
    wins = round(strat_meta["base"]["wr"] * n / 100)
    avg = strat_meta["base"]["avg"]
    shown = sorted(base_trades, key=lambda t: -t[2]["entry_ts"])[:args.max_cards]

    bd_snap_json = json.dumps(bdusage.snapshot())
    strat_meta_json = json.dumps(strat_meta)
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TRADE JOURNAL — asym-runner v2</title>
<style>
  body{{background:#0a0a0e;color:#e8e8ea;font-family:"Cascadia Mono",Consolas,monospace;
       font-size:13px;margin:0;padding:20px 24px 60px}}
  h1{{font-size:18px;color:#ff7a1a;letter-spacing:1px}}
  .sum{{color:{INK2};margin:6px 0 14px;font-size:12px;line-height:1.6}}
  .filters{{margin:0 0 10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
  .filters .flab{{color:{INK3};font-size:10px;letter-spacing:1px;margin-right:2px}}
  .filters button{{background:#14141d;color:{INK2};border:1px solid #2a2a38;padding:5px 14px;
       font-family:inherit;font-size:11px;letter-spacing:1px;cursor:pointer}}
  .filters button.on{{color:#ffc233;border-color:#ffc233}}
  .card{{background:#101017;border:1px solid #2a2a38;margin-bottom:18px;max-width:1150px;
       clip-path:polygon(0 0,calc(100% - 14px) 0,100% 14px,100% 100%,0 100%)}}
  .chead{{display:flex;justify-content:space-between;align-items:center;gap:12px;
       padding:12px 16px;border-bottom:1px solid #2a2a38}}
  .t1{{font-size:15px;font-weight:bold}}
  .t2{{color:{INK2};font-size:11.5px;margin-top:3px}}
  .win-t{{color:{GREEN}}} .loss-t{{color:{RED}}}
  .btn{{background:#1b2a5e;color:#cfd8ff;text-decoration:none;font-size:11px;
       padding:6px 12px;border:1px solid #3d55b8;border-radius:4px;white-space:nowrap}}
  .actions{{display:flex;gap:8px;align-items:center}}
  .blkbtn{{background:#2a1420;color:#ffb3c0;border-color:#b83d55;font-family:inherit;cursor:pointer}}
  .card.blked{{border-color:#b83d55}}
  .card.blked .blkbtn{{background:#b83d55;color:#0a0a0e;font-weight:bold}}
  .cbody{{display:grid;grid-template-columns:1fr 240px;gap:14px;padding:14px 16px}}
  @media(max-width:900px){{.cbody{{grid-template-columns:1fr}}}}
  .chart{{background:#0d0d13;border:1px solid #1e1e2a;padding:6px;overflow-x:auto}}
  .side{{background:#0d0d13;border:1px solid #1e1e2a;padding:12px 14px;height:fit-content}}
  .sh{{font-weight:bold;margin-bottom:8px;letter-spacing:1px;font-size:12px}}
  .lv{{display:flex;justify-content:space-between;padding:3.5px 0;border-bottom:1px dotted #1e1e2a;
       font-size:12px;color:{INK2}}}
  .lv:last-child{{border-bottom:none}}
  .tgrid{{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:8px}}
  .ti{{background:#14141d;border:1px solid #1e1e2a;border-radius:6px;padding:8px 4px;
       text-align:center;cursor:help}}
  .ti .v{{font-weight:bold;font-size:12.5px}}
  .ti .l{{color:{INK3};font-size:9.5px;margin-top:3px;letter-spacing:.3px}}
  .usd{{display:block;font-size:10.5px;color:{INK3};text-align:right;font-weight:normal}}
  a{{color:#2695c4}}
  .nav{{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:8px;
       background:#0a0a0e;border-bottom:1px solid #2a2a38;margin:-20px -24px 16px;
       padding:10px 24px}}
  .tab{{background:#14141d;color:{INK2};border:1px solid #2a2a38;padding:6px 16px;
       font-size:11px;letter-spacing:1px;text-decoration:none;white-space:nowrap}}
  .tab:hover{{color:#e8e8ea;border-color:#5c5c6c}}
  .tab.on{{color:#ffc233;border-color:#ffc233}}
  .bd{{margin-left:auto;min-width:280px;max-width:440px;text-align:right}}
  .bdlab{{color:#ff7a1a;font-weight:bold;font-size:10px;letter-spacing:1px;margin-right:8px}}
  #bdtext{{font-size:10.5px;color:{INK2}}}
  .bdbar{{height:4px;background:#1e1e2a;margin-top:4px;border-radius:2px;overflow:hidden}}
  #bdfill{{height:100%;width:0;background:{GREEN};transition:width .4s}}
</style></head><body>
<div class="nav">
  <a class="tab" href="/" title="NERV dashboard home">&#8962; HOME</a>
  <span class="tab on">TRADE JOURNAL</span>
  <a class="tab" href="/montecarlo" title="Monte Carlo equity-path simulation">MONTE CARLO</a>
  <a class="tab" href="/blacklist" title="Charts purged from the sample — manual flags + auto wash-ramps">BLACKLIST</a>
  <a class="tab" href="/wallets" title="Scouted copy-trade wallets + tracked watchlist + live harvest">WALLETS</a>
  <a class="tab" href="/fundergraph" title="Rug lineage: why deployer/funder reputation can't work">FUNDER GRAPH</a>
  <div class="bd" title="Lite plan: 2.5M CU/cycle (anchored the 20th), 15 RPS, overage $15/1M CU. Local ledger + reconstructed baseline (Birdeye has no usage API - their Usages/Metrics page is authoritative). OHLCV CU cost is an estimate.">
    <span class="bdlab">BIRDEYE API</span><span id="bdtext">no usage tracked yet</span>
    <div class="bdbar"><div id="bdfill"></div></div>
  </div>
</div>
<h1>TRADE JOURNAL — ASYM-RUNNER V2</h1>
<div class="sum">Backtested on the Birdeye unbiased sample · exits: stop {exits["stop_loss_pct"]:.0f}% ·
bank {int(float(tp1["sell_fraction_of_remaining"]) * 100)}% @{tp1["multiple"]}x · trail {exits["trailing_stop_pct"]:.0f}% ·
cap {exits["hard_tp_multiple"]:.0f}x · entry age {args.entry_age:.0f}m · min pre-entry vol ${args.min_entry_vol:,.0f} ·
{args.cost_pct:.0f}% cost haircut · levels shown as market cap (assumes {args.token_supply / 1e9:g}B supply){f" · SOL @ ${sol_usd:,.2f}" if sol_usd is not None else ""}<br>
{"<b>base-entry setup filter:</b> " + f"{setup['base_window_min']}m range ≤{setup['max_base_range_pct']:.0f}% · ≤{setup['max_nukes']} collapse candle · ≥{setup['min_frac_of_peak']:.0%} of credible peak · {setup['trend_window_min']}m trend {setup['trend_lo']:.2f}–{setup['trend_hi']:.2f}x<br>" if setup else "setup filter OFF (every age/volume entry shown)<br>"}
<b>{n:,} trades · {100 * wins / n if n else 0:.0f}% win rate · {avg:.2f}x avg</b> —
showing the {len(shown)} most recent (regenerate with --max-cards for more).
Assumes fills at trigger prices; "data_end" trades value the remainder at the last traded price.<br>
<b>AT ENTRY panel:</b> computed only from pre-entry candles at the entry timestamp (green = passes
its gate; hover a tile for the definition). Holder/sniper/insider/security stats are not shown for
backtest trades — no source has them as-of a past timestamp, and today's values would be hindsight.<br>
<b>Blacklist button (forward-only):</b> the flagged trade STAYS in the stats above — hand-flags never
edit existing results (deleting losers would inflate them). A flag excludes future listings matching
the mint/symbol and files the chart on the BLACKLIST tab as an avoid-pattern reference.</div>
<div class="filters" id="stratf">
  <span class="flab">STRATEGY</span>
  <button class="on" data-s="all">ALL</button>
  <button data-s="base">BASE-ENTRY</button>
  <button data-s="copy_all">COPY-ALL</button>
  <button data-s="cluster">CLUSTER-CONFIRMED</button>
  <button data-s="homerun">HOME-RUN</button>
  <button data-s="sniper">SNIPER</button>
  <button data-s="volanomaly">VOL-ANOMALY</button>
  <button data-s="dip">DIP-BUY</button>
</div>
<div class="sum" id="strat-stats" style="margin:0 0 12px"></div>
<div class="filters" id="resf">
  <span class="flab">RESULT</span>
  <button class="on" data-f="all">ALL</button>
  <button data-f="win">WINS</button>
  <button data-f="loss">LOSSES</button>
</div>
<div class="filters" id="qualf">
  <span class="flab">QUALITY</span>
  <button class="on" data-q="all">ALL</button>
  <button data-q="human" title="ORGANIC copy/cluster tokens (real two-sided volume, sustained trading, natural pullbacks) + the clean base sample">HUMAN-LOOKING</button>
  <button data-q="scam" title="RUG / WASH-RAMP / SPIKE-DUMP — bot artefacts with no real crowd">SCAM-LIKE</button>
</div>
<div class="sum" style="margin:0 0 12px;font-size:11px;color:{INK3}">
  QUALITY tags copy/cluster tokens (manipscan.py): <span style="color:{GREEN}">ORGANIC</span> = human +
  manipulated (tradeable) · <span style="color:{RED}">RUG</span> = instant collapse ·
  <span style="color:{AMBER}">WASH_RAMP / SPIKE_DUMP</span> = bot-painted. Base-entry cards are the
  pre-cleaned validated sample. Hover a tag for its human/manip scores.</div>
{"".join(cards)}
<script>
const BD_EMBED = {bd_snap_json};
function bdFmt(n) {{
  return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : String(n);
}}
function bdRender(s, live) {{
  if (!s || s.requests === undefined) return;
  const pct = s.pct_of_plan || 0;
  document.getElementById('bdtext').textContent =
    s.requests.toLocaleString() + ' req · ~' + bdFmt(s.cu_est) + ' / ' + bdFmt(s.plan_cu) +
    ' CU (' + pct.toFixed(1) + '%) · ' + (s.period_label || s.month) +
    (s.overage_usd_est ? ' · overage ~$' + s.overage_usd_est.toFixed(2) : '') +
    (live ? '' : ' · at page build');
  const f = document.getElementById('bdfill');
  f.style.width = Math.min(100, pct) + '%';
  f.style.background = pct >= 90 ? '{RED}' : pct >= 60 ? '{AMBER}' : '{GREEN}';
}}
bdRender(BD_EMBED, false);
function bdPoll() {{
  fetch('/api/bdusage').then(r => r.json()).then(s => bdRender(s, true)).catch(() => {{}});
}}
bdPoll();
setInterval(bdPoll, 60000);
const STRAT_META = {strat_meta_json};
const STRAT_NOTE = {{
  base: 'The bot\\'s own validated backtest — the live-arbiter strategy (audit applies).',
  copy_all: 'Enter every monitored-wallet buy (+{args.follow_lag}m). Unbiased at the token level but WALLET-selection biased + inherits backtest fill optimism — an upper bound, not proven live.',
  cluster: 'Enter only on a 2nd-wallet confirmation (+{args.follow_lag}m). The unbiased test found this does NOT beat copy-all — shown for comparison.',
  homerun: 'Moonshot screen (low mcap + momentum + organic) + ride-to-30x exit. Ranked by biggest realized multiple. High-variance: the edge rides on the rare 10x+ runs. Now paper-trading.',
  sniper: 'Ultra-early listings with real two-sided volume, ORGANIC-only, quick scalp exits (bank 50% @1.5x, ≤2h hold). High win rate, modest per-trade edge. Now paper-trading.',
  volanomaly: 'Enter on an abnormal volume spike vs the token\\'s own baseline (scan for the signal), ORGANIC-only, ≤2h hold. Now paper-trading.',
  dip: 'Mean-reversion: pullback off a local peak + green volume bounce, ORGANIC-only, ≤3h hold. Lower win rate, positive expectancy. Now paper-trading.'
}};
let curStrat = 'all', curRes = 'all', curQual = 'all';
const SCAMISH = ['RUG', 'WASH_RAMP', 'SPIKE_DUMP'];
function applyFilters() {{
  document.querySelectorAll('.card').forEach(c => {{
    const okS = curStrat === 'all' || c.dataset.strat === curStrat;
    const okR = curRes === 'all' || c.classList.contains(curRes);
    const dm = c.dataset.manip;
    const okQ = curQual === 'all'
      || (curQual === 'human' && (dm === 'ORGANIC' || dm === 'clean'))
      || (curQual === 'scam' && SCAMISH.includes(dm));
    c.style.display = (okS && okR && okQ) ? '' : 'none';
  }});
  const el = document.getElementById('strat-stats');
  if (curStrat === 'all') {{
    const parts = Object.keys(STRAT_META).map(k => {{
      const m = STRAT_META[k];
      return m.label + ' ' + m.n + ' trades / ' + m.wr + '% WR / ' + m.avg + 'x';
    }});
    el.innerHTML = '<b>All strategies:</b> ' + parts.join(' &nbsp;·&nbsp; ') +
      ' — pick one above to browse its cards.';
  }} else {{
    const m = STRAT_META[curStrat];
    const org = (m.organic != null)
      ? ' · <span style="color:{GREEN}">' + m.organic + ' of ' + m.n +
        ' organic/human</span> (' + Math.round(100 * m.organic / m.n) + '%)'
      : '';
    el.innerHTML = '<b>' + m.label + ':</b> ' + m.n + ' trades · ' + m.wr + '% WR · ' +
      m.avg + 'x avg' + org + '. ' + (STRAT_NOTE[curStrat] || '');
  }}
}}
document.querySelectorAll('#stratf button').forEach(b => b.onclick = () => {{
  document.querySelectorAll('#stratf button').forEach(x => x.classList.remove('on'));
  b.classList.add('on'); curStrat = b.dataset.s; applyFilters();
}});
document.querySelectorAll('#resf button').forEach(b => b.onclick = () => {{
  document.querySelectorAll('#resf button').forEach(x => x.classList.remove('on'));
  b.classList.add('on'); curRes = b.dataset.f; applyFilters();
}});
document.querySelectorAll('#qualf button').forEach(b => b.onclick = () => {{
  document.querySelectorAll('#qualf button').forEach(x => x.classList.remove('on'));
  b.classList.add('on'); curQual = b.dataset.q; applyFilters();
}});
applyFilters();
// ---- blacklist button: persists via the dashboard (POST /api/blacklist) ----
function setBlk(card, on) {{
  card.classList.toggle('blked', on);
  const b = card.querySelector('.blkbtn');
  if (b) b.textContent = on ? 'Blacklisted ✓ (undo)' : 'Blacklist';
}}
fetch('/api/blacklist').then(r => r.json()).then(d => {{
  document.querySelectorAll('.card[data-mint]').forEach(c => {{
    if (d.manual && d.manual[c.dataset.mint]) setBlk(c, true);
  }});
}}).catch(() => {{}});
document.querySelectorAll('.blkbtn').forEach(b => b.onclick = () => {{
  const card = b.closest('.card');
  const on = !card.classList.contains('blked');
  const body = on
    ? {{mint: b.dataset.mint, symbol: b.dataset.symbol, pool: b.dataset.pool,
       result: b.dataset.result, multiple: +b.dataset.mult, entry_ts: +b.dataset.entry}}
    : {{mint: b.dataset.mint, action: 'remove'}};
  fetch('/api/blacklist', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body)}})
    .then(r => {{ if (r.ok) setBlk(card, on);
                  else alert('blacklist failed — is the NERV dashboard serving this page?'); }})
    .catch(() => alert('blacklist API unreachable — open this page via the dashboard (/trades)'));
}});
</script>
</body></html>"""
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(doc)

    # machine-readable feed for the dashboard's SORTIE LOG fallback - newest
    # CLOSE first, since that is the timestamp the panel displays
    jrows = [{"closed_at": time.strftime("%Y-%m-%d %H:%M", time.gmtime(s["end_ts"])),
              "symbol": tok.symbol or "?", "multiple": round(s["multiple"], 2),
              "pnl_sol": round(POSITION_SOL * (s["multiple"] - 1), 4),
              "reason": s["reason"]}
             for tok, _c, s in sorted(shown, key=lambda t: -t[2]["end_ts"])[:50]]
    jpath = os.path.join("reports", "trade_journal.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "strategy": "asym-runner v2 + base-entry filter (backtest, simulated fills)"
                               if setup else "asym-runner v2 (backtest, simulated fills)",
                   "n_total": n, "rows": jrows}, f, indent=1)
    print(f"{n} trades simulated; {len(shown)} cards written to {os.path.abspath(args.out)}; "
          f"journal feed: {os.path.abspath(jpath)}")


if __name__ == "__main__":
    main()
