#!/usr/bin/env python
"""WALLETS tab: scouted copy-trade candidates + tracked watchlist + live
top-trader harvest (served at /wallets).

Three data sources, all read-only:
  reports/wallet_scout.json   walletscout.py output - wallets ranked by
                              REALIZED >=10x round trips (headline leaderboard
                              pnl is an unrealized-mark mirage; ignored there)
  reports/tracked_wallets.json  the shadow watchlist the bot annotates against
  wallets.sqlite              every top-trader wallet seen by the smart-money
                              gate's deep checks (bot/wallets.py)
"""
from __future__ import annotations

import html
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bdusage
from bot import wallets as botwallets

SCOUT_PATH = os.path.join("reports", "wallet_scout.json")
TRACKED_PATH = os.path.join("reports", "tracked_wallets.json")
COPYTRADE_PATH = os.path.join("reports", "copytrade_backtest.json")
HARVEST_DB = "wallets.sqlite"

GREEN, RED, AMBER, INK2, INK3 = "#00a869", "#ff3355", "#b37c12", "#9a9aa8", "#5c5c6c"


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _e(s) -> str:
    return html.escape(str(s))


def _wlink(wallet: str, label: str | None = None) -> str:
    lab = _e(label or f"{wallet[:6]}…{wallet[-6:]}")
    return (f'<a class="wl" href="https://birdeye.so/profile/{_e(wallet)}?chain=solana"'
            f' target="_blank" title="{_e(wallet)}">{lab}</a>')


def _scout_rows(scout: dict | None) -> str:
    if not scout or not scout.get("wallets"):
        return ('<tr><td colspan="9" class="empty">no scout results yet — run:'
                ' <code>python walletscout.py</code></td></tr>')
    rows = []
    for s in scout["wallets"][:25]:
        wr = s["win_rate"]
        wr_col = GREEN if wr >= 60 else (AMBER if wr >= 45 else RED)
        top_trips = ", ".join(
            f'{_e(t["symbol"])} {t["multiple"]:.1f}x' for t in s.get("trips", [])[:3])
        tenx_col = GREEN if s["tenx"] else INK3
        rows.append(
            f'<tr><td>{_wlink(s["wallet"])}</td>'
            f'<td>{s["round_trips"]}</td>'
            f'<td style="color:{wr_col}">{wr:.0f}%</td>'
            f'<td style="color:{tenx_col};font-weight:bold">{s["tenx"]}</td>'
            f'<td>{s["fivex"]}</td><td>{s["twox"]}</td>'
            f'<td>{s["best_multiple"]:.1f}x</td>'
            f'<td>${s["total_bought_usd"]:,.0f}</td>'
            f'<td style="color:{INK3}">{top_trips or "—"}</td></tr>')
    return "".join(rows)


def _tracked_rows(tracked: dict | None) -> str:
    rows = []
    for w in (tracked or {}).get("wallets", []):
        rows.append(
            f'<tr><td>{_e(w.get("name", "?"))}</td>'
            f'<td>{_wlink(w["wallet"])}</td>'
            f'<td>{_e(w.get("source", "?"))}</td>'
            f'<td>{_e(w.get("added", "?"))}</td>'
            f'<td style="color:{INK3}">{_e(w.get("note", ""))}</td></tr>')
    return "".join(rows) or '<tr><td colspan="5" class="empty">watchlist empty</td></tr>'


def _sb(s: dict | None) -> str:
    """Render a stats_block (n/win_rate/avg/median/total_pnl_sol) as cells."""
    if not s:
        return '<td colspan="4" style="color:#5c5c6c">—</td>'
    avg_col = GREEN if s["avg"] > 1.05 else (AMBER if s["avg"] >= 0.98 else RED)
    return (f'<td>{s["n"]}</td><td>{s["win_rate"]:.0f}%</td>'
            f'<td style="color:{avg_col}">{s["avg"]:.2f}x</td><td>{s["median"]:.2f}x</td>')


def _copytrade_html() -> str:
    ct = _read_json(COPYTRADE_PATH)
    if not ct:
        return ('<div class="sum">no copy-trade backtest yet — run:'
                ' <code>python copytrade_backtest.py</code></div>')
    lags = [0, 5, 15, 30, 60]
    mir = ct.get("follower_mirror_by_lag", {})
    strat = ct.get("follower_strategy_by_lag", {})
    lead = ct.get("leader_matched_exact")

    def lag_rows(block):
        out = []
        for L in lags:
            s = block.get(str(L)) or block.get(L)
            out.append(f'<tr><td>+{L}m</td>{_sb(s)}</tr>')
        return "".join(out)

    cl = ct.get("cluster", {})
    lead_rows = "".join(
        f'<tr><td>{_wlink(w)}</td><td>{n} clusters</td></tr>'
        for w, n in sorted((cl.get("lead_counts") or {}).items(), key=lambda kv: -kv[1])[:6]
    ) or '<tr><td colspan="2" class="empty">no co-accumulation clusters found</td></tr>'

    ws = sorted(ct.get("wallets", []),
                key=lambda x: -((x.get("follower_strategy_lag15") or {}).get("avg", 0)))
    wr = []
    for w in ws:
        fs = w.get("follower_strategy_lag15") or {}
        avg = fs.get("avg", 0)
        col = GREEN if avg > 1.05 else (AMBER if avg >= 0.98 else RED)
        verdict = "COPYABLE" if avg > 1.1 else ("MARGINAL" if avg >= 0.98 else "UNCOPYABLE")
        wr.append(
            f'<tr><td>{_wlink(w["wallet"])}</td>'
            f'<td>{w["trips_covered"]}</td>'
            f'<td>{w["median_hold_min"]:.0f}m</td>'
            f'<td>{w["median_age_at_entry_min"]:.0f}m</td>'
            f'<td>{w["median_leader_mult"]:.0f}x</td>'
            f'<td style="color:{col}">{avg:.2f}x</td>'
            f'<td>{fs.get("win_rate", 0):.0f}%</td>'
            f'<td>{w.get("leads_cluster", 0)}</td>'
            f'<td style="color:{col}">{verdict}</td></tr>')
    wrows = "".join(wr) or '<tr><td colspan="9" class="empty">no covered wallets</td></tr>'

    return f"""
<div class="sum">Follower simulated entering N minutes after each scouted wallet's buy, over
{ct.get('trips_covered', '?')} round trips with candle coverage (cost {ct.get('cost_pct', '?')}%).
<b>Leader-matched-exact</b> = matching their exact entry/exit from 1m candles — its collapse
(below) is the tell that the alpha lives <i>inside</i> the launch candle, sub-minute.</div>

<div style="display:flex;gap:24px;flex-wrap:wrap">
<div><h3 style="font-size:12px;color:{INK2};margin:4px 0">MIRROR EXIT (sell when they sold)</h3>
<table style="max-width:440px"><thead><tr><th>LAG</th><th>N</th><th>WIN%</th><th>AVG</th><th>MED</th></tr></thead>
<tbody><tr><td>leader</td>{_sb(lead)}</tr>{lag_rows(mir)}</tbody></table></div>
<div><h3 style="font-size:12px;color:{INK2};margin:4px 0">STRATEGY EXIT (your doctrine)</h3>
<table style="max-width:440px"><thead><tr><th>LAG</th><th>N</th><th>WIN%</th><th>AVG</th><th>MED</th></tr></thead>
<tbody>{lag_rows(strat)}</tbody></table></div>
</div>

<h3 style="font-size:12px;color:{INK2};margin:16px 0 4px">CO-ACCUMULATION — {cl.get('n_clusters', 0)} tokens bought by &ge;2 tracked wallets</h3>
<div class="sum">solo-token avg leader mult <b>{cl.get('solo_token_avg_leader_mult', '—')}x</b>
· cluster-token avg <b>{cl.get('cluster_token_avg_leader_mult', '—')}x</b>. Wallets most
often first-into a shared token — the latency-tolerant trigger to watch:</div>
<table style="max-width:440px"><thead><tr><th>WALLET</th><th>LED</th></tr></thead><tbody>{lead_rows}</tbody></table>

<h3 style="font-size:12px;color:{INK2};margin:16px 0 4px">PER-WALLET COPYABILITY (follower strategy exit @ +15m)</h3>
<table><thead><tr><th>WALLET</th><th>TRIPS</th><th>MED HOLD</th><th>MED AGE</th><th>LEAD MLT</th>
<th>F-AVG</th><th>F-WIN%</th><th>LEADS</th><th>VERDICT</th></tr></thead><tbody>{wrows}</tbody></table>
"""


def _harvest_html() -> str:
    snap = botwallets.snapshot(HARVEST_DB, TRACKED_PATH)
    if not snap:
        return f'<div class="sum">no harvest yet — sightings accumulate on every deep check</div>'
    top = "".join(
        f'<tr><td>{_wlink(t["wallet"])}'
        + (f' <span style="color:{AMBER}">★{_e(t["tracked"])}</span>' if t.get("tracked") else "")
        + f'</td><td>{t["mints"]}</td><td>{t["sightings"]}</td>'
        f'<td style="color:{INK3}">{_e((t.get("symbols") or "")[:60])}</td>'
        f'<td style="color:{INK3}">{_e((t.get("last_seen") or "")[:16])}</td></tr>'
        for t in snap.get("top", []))
    hits = "".join(
        f'<tr><td>{_e((h.get("ts") or "")[:16])}</td>'
        f'<td style="color:{AMBER}">★{_e(h.get("name") or "?")}</td>'
        f'<td>{_e(h.get("symbol") or "?")}</td><td>{_wlink(h["wallet"])}</td></tr>'
        for h in snap.get("tracked_hits", []))
    return (
        f'<div class="sum"><b>{snap["sightings"]:,}</b> sightings · '
        f'<b>{snap["wallets"]:,}</b> distinct wallets · <b>{snap["tokens"]:,}</b> tokens '
        f'· {snap["tracked_count"]} watched</div>'
        f'<table><thead><tr><th>WALLET</th><th>TOKENS</th><th>SEEN</th><th>ON</th>'
        f'<th>LAST</th></tr></thead><tbody>'
        + (top or '<tr><td colspan="5" class="empty">no sightings yet</td></tr>')
        + '</tbody></table>'
        + (f'<h2>TRACKED-WALLET HITS</h2><table><thead><tr><th>TIME (UTC)</th><th>NAME</th>'
           f'<th>TOKEN</th><th>WALLET</th></tr></thead><tbody>{hits}</tbody></table>' if hits else ""))


def render_page() -> str:
    scout = _read_json(SCOUT_PATH)
    tracked = _read_json(TRACKED_PATH)
    gen = _e((scout or {}).get("generated", "never"))
    days = (scout or {}).get("days", "?")
    bd_snap = json.dumps(bdusage.snapshot())
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WALLETS — copy-trade intel</title>
<style>
  body{{background:#0a0a0e;color:#e8e8ea;font-family:"Cascadia Mono",Consolas,monospace;
       font-size:13px;margin:0;padding:20px 24px 60px}}
  h1{{font-size:18px;color:#ff7a1a;letter-spacing:1px}}
  h2{{font-size:13px;color:#e8e8ea;letter-spacing:1px;margin:26px 0 10px}}
  .sum{{color:{INK2};margin:6px 0 14px;font-size:12px;line-height:1.6;max-width:1150px}}
  table{{width:100%;max-width:1150px;border-collapse:collapse;font-size:12px}}
  th{{text-align:left;color:{INK3};font-size:10.5px;letter-spacing:1px;padding:4px 10px 4px 0;
      border-bottom:1px solid #2a2a38}}
  td{{padding:5px 10px 5px 0;border-bottom:1px solid #16161f;vertical-align:top}}
  .empty{{color:{INK3};padding:14px 0}}
  .wl{{color:#7fc9e8;text-decoration:none}}
  .wl:hover{{text-decoration:underline}}
  code{{color:#ffc233}}
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
  .ctprog{{max-width:1150px;margin:0 0 16px}}
  .ctprog-head{{display:flex;justify-content:space-between;font-size:11px;color:{INK2};
       letter-spacing:1px;margin-bottom:5px}}
  .ctprog-track{{height:14px;background:#14141d;border:1px solid #2a2a38;border-radius:3px;
       overflow:hidden}}
  .ctprog-fill{{height:100%;width:0;background:linear-gradient(90deg,#ff7a1a,#ffc233);
       transition:width .5s ease;box-shadow:0 0 8px #ff7a1a88}}
  .ctprog.done .ctprog-fill{{background:{GREEN};box-shadow:0 0 8px {GREEN}88}}
  .ctprog.stalled .ctprog-fill{{background:{RED}}}
  #ct-run:hover{{color:#ffc233;border-color:#ffc233}}
</style></head><body>
<div class="nav">
  <a class="tab" href="/" title="NERV dashboard home">&#8962; HOME</a>
  <a class="tab" href="/trades" title="Per-trade journal cards">TRADE JOURNAL</a>
  <a class="tab" href="/montecarlo" title="Monte Carlo equity-path simulation">MONTE CARLO</a>
  <a class="tab" href="/blacklist" title="Charts purged from the sample">BLACKLIST</a>
  <span class="tab on">WALLETS</span>
  <a class="tab" href="/fundergraph" title="Rug lineage: why deployer/funder reputation can't work">FUNDER GRAPH</a>
  <div class="bd"><span class="bdlab">BIRDEYE API</span><span id="bdtext">no usage tracked yet</span>
    <div class="bdbar"><div id="bdfill"></div></div></div>
</div>
<h1>WALLETS — COPY-TRADE INTEL</h1>
<div class="sum">Three layers, least→most trustworthy: seeded watchlist (public leaderboards, unverified)
→ scouted wallets (<b>realized</b> round-trip multiples reconstructed from on-chain swap history)
→ our own harvest (wallets that actually trade this bot's universe). Scout ranks by completed
&ge;10x round trips over the last <b>{days}</b> days; leaderboard "pnl" is ignored because the top
gainers are unrealized-mark mirages. Everything here is observational — nothing gates an entry.</div>

<h2>SCOUTED WALLETS — REALIZED ROUND TRIPS, LAST {days} DAYS <span style="color:{INK3}">(generated {gen} · <code>python walletscout.py</code>)</span></h2>
<table><thead><tr><th>WALLET</th><th>TRIPS</th><th>WIN%</th><th>&ge;10x</th><th>&ge;5x</th>
<th>&ge;2x</th><th>BEST</th><th>BOUGHT</th><th>TOP TRIPS</th></tr></thead>
<tbody>{_scout_rows(scout)}</tbody></table>

<h2>TRACKED WATCHLIST ({len((tracked or {}).get("wallets", []))}) — reports/tracked_wallets.json, bot reloads on edit</h2>
<table><thead><tr><th>NAME</th><th>WALLET</th><th>SOURCE</th><th>ADDED</th><th>NOTE</th></tr></thead>
<tbody>{_tracked_rows(tracked)}</tbody></table>

<h2 style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">COPY-TRADE BACKTEST — CAN A FOLLOWER CAPTURE THIS?
  <button id="ct-run" class="tab" style="cursor:pointer" title="Re-run copytrade_backtest.py. OHLCV is cached so re-runs are fast.">&#8635; RE-RUN</button></h2>
<div id="ct-progress" class="ctprog" style="display:none">
  <div class="ctprog-head"><span id="ct-phase">idle</span><span id="ct-pct">0%</span></div>
  <div class="ctprog-track"><div id="ct-fill" class="ctprog-fill"></div></div>
</div>
{_copytrade_html()}

<h2>LIVE HARVEST — TOP-TRADER WALLETS SEEN BY THE SMART-MONEY GATE</h2>
{_harvest_html()}
<script>
const BD_EMBED = {bd_snap};
function bdFmt(n) {{ return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : String(n); }}
function bdRender(s, live) {{
  if (!s || s.requests === undefined) return;
  const pct = s.pct_of_plan || 0;
  document.getElementById('bdtext').textContent =
    s.requests.toLocaleString() + ' req · ~' + bdFmt(s.cu_est) + ' / ' + bdFmt(s.plan_cu) +
    ' CU (' + pct.toFixed(1) + '%) · ' + (s.period_label || s.month) +
    (s.overage_usd_est ? ' · overage ~$' + s.overage_usd_est.toFixed(2) : '') + (live ? '' : ' · at page build');
  const f = document.getElementById('bdfill');
  f.style.width = Math.min(100, pct) + '%';
  f.style.background = pct >= 90 ? '{RED}' : pct >= 60 ? '{AMBER}' : '{GREEN}';
}}
bdRender(BD_EMBED, false);
fetch('/api/bdusage').then(r => r.json()).then(s => bdRender(s, true)).catch(() => {{}});

/* ---- copy-trade backtest progress bar ---- */
(function () {{
  var box = document.getElementById('ct-progress');
  var fill = document.getElementById('ct-fill');
  var phaseEl = document.getElementById('ct-phase');
  var pctEl = document.getElementById('ct-pct');
  var runBtn = document.getElementById('ct-run');
  var wasRunning = false, poll = null;
  var PHASE = {{starting: 'STARTING', fetch: 'FETCHING OHLCV', simulate: 'SIMULATING',
               done: 'COMPLETE', idle: 'IDLE', stalled: 'STALLED'}};
  function render(p) {{
    var state = p.state || 'idle';
    if (state === 'idle') {{ box.style.display = 'none'; return; }}
    box.style.display = 'block';
    box.className = 'ctprog' + (state === 'done' ? ' done' : state === 'stalled' ? ' stalled' : '');
    var pct = state === 'done' ? 100 : (p.pct || 0);
    fill.style.width = pct + '%';
    var label = PHASE[p.phase] || (p.phase || '').toUpperCase();
    if (p.phase === 'fetch' || p.phase === 'simulate')
      label += ' ' + (p.done || 0) + '/' + (p.total || 0);
    phaseEl.textContent = label;
    pctEl.textContent = pct.toFixed(0) + '%';
    if (state === 'running') wasRunning = true;
    if (wasRunning && state === 'done') {{  // finished a run we watched -> show fresh results
      wasRunning = false;
      phaseEl.textContent = 'COMPLETE — reloading…';
      setTimeout(function () {{ location.reload(); }}, 1200);
    }}
  }}
  function tick() {{
    fetch('/api/copytrade').then(function (r) {{ return r.json(); }})
      .then(render).catch(function () {{}});
  }}
  function start() {{ if (!poll) {{ tick(); poll = setInterval(tick, 1500); }} }}
  runBtn.onclick = function () {{
    runBtn.textContent = '\\u21bb RUNNING…';
    fetch('/api/copytrade/run', {{method: 'POST'}}).then(function () {{ wasRunning = true; start(); }});
  }};
  start();
}})();
</script>
</body></html>"""


if __name__ == "__main__":
    out = os.path.join("reports", "wallets_preview.html")
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_page())
    print(f"preview written: {os.path.abspath(out)}")
