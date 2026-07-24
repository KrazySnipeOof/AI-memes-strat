#!/usr/bin/env python
"""BLACKLIST tab: gallery of charts purged from the sample (served at /blacklist).

Shows every hand-flagged chart (TRADE JOURNAL's Blacklist button ->
reports/bd_blocklist_manual.json) plus the most extreme auto-detected
wash-ramps (blocklist.py -> reports/bd_blocklist.json), each with its candle
chart, so the avoided patterns stay visible in one place. This is the
reference set of "what to avoid" going forward.

Manual flags are FORWARD-ONLY (locked honesty rule): flagging a chart never
removes its trade from existing stats - deleting known losers would inflate
every downstream number. A manual flag only excludes tokens LISTED AFTER the
flag time whose mint or symbol matches (serial redeploys of the same scam),
via backtest.load_token_index. The auto detector keeps retroactive removal
because it is outcome-asymmetric by construction: it only ever purges rising
ramps, i.e. fabricated wins - see blocklist.py.
"""
from __future__ import annotations

import html
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest
import bdusage
from tradecards import GREEN, RED, CYAN, AMBER, INK2, INK3, fmt_mcap, fmt_ts, resample

CACHES = (".bd_cache_ext", ".bd_cache")
INDEXES = ("bd_tokens.json", "bd_tokens_60d.json", "bd_tokens_oot.json")
MAX_AUTO_CARDS = 24
SUPPLY = 1e9  # assumed launch supply for market-cap axis, same as tradecards


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _index_map() -> dict:
    """mint -> {pool, symbol, created_ts} from the raw token indexes (raw read
    on purpose - load_token_index would hide already-blocked tokens)."""
    out = {}
    for name in INDEXES:
        for t in _read_json(os.path.join("reports", name)) or []:
            out.setdefault(t["mint"], t)
    return out


def _candles(pool: str):
    if not pool:
        return None
    for cache in CACHES:
        c = backtest.load_cached_candles(cache, pool)
        if c:
            return c
    return None


def _svg(candles: list, entry_ts: int | None) -> str:
    """Small self-contained candle chart with market-cap ticks and an optional
    entry marker - the same visual language as the trade journal."""
    window = candles[:360]
    bucket = max(60, int((window[-1][0] - window[0][0]) / 80 // 60 * 60) or 60)
    view = resample(window, bucket)
    W, H, PL, PR, PT, PB = 560, 190, 8, 84, 14, 20
    pw, ph = W - PL - PR, H - PT - PB
    lo = min(c[3] for c in view)
    hi = max(c[2] for c in view)
    span = (hi - lo) or 1e-12
    lo, hi = max(0.0, lo - span * 0.05), hi + span * 0.08
    span = hi - lo

    def Y(p):
        return PT + (hi - p) / span * ph

    def X(i):
        return PL + (i + 0.5) * pw / len(view)

    bw = max(1.6, min(10.0, pw / len(view) * 0.66))
    parts = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
             f'style="width:100%;height:auto;display:block" font-family="Cascadia Mono,Consolas,monospace">']
    for k in range(4):
        p = hi - span * k / 3
        y = Y(p)
        parts.append(f'<line x1="{PL}" x2="{W - PR}" y1="{y:.1f}" y2="{y:.1f}" stroke="#1e1e2a"/>')
        parts.append(f'<text x="{W - PR + 6}" y="{y + 3.5:.1f}" font-size="9.5" fill="{INK3}">{fmt_mcap(p * SUPPLY)}</text>')
    for frac in (0.1, 0.9):
        i = int(frac * (len(view) - 1))
        parts.append(f'<text x="{X(i):.1f}" y="{H - 6}" font-size="9.5" fill="{INK3}" text-anchor="middle">'
                     f'{time.strftime("%m-%d %H:%M", time.gmtime(view[i][0]))}</text>')
    for i, (ts, o, h, l, c, v) in enumerate(view):
        col = GREEN if c >= o else RED
        x = X(i)
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{Y(h):.1f}" y2="{Y(l):.1f}" stroke="{col}" stroke-width="1"/>')
        top, bot = Y(max(o, c)), Y(min(o, c))
        parts.append(f'<rect x="{x - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                     f'height="{max(1.2, bot - top):.1f}" fill="{col}" rx="1"/>')
    if entry_ts:
        ei = min(range(len(view)), key=lambda i: abs(view[i][0] - entry_ts))
        if abs(view[ei][0] - entry_ts) <= 2 * bucket:
            ex, eyy = X(ei), Y(view[ei][2]) - 5
            parts.append(f'<text x="{ex:.1f}" y="{max(10, eyy - 8):.1f}" font-size="9.5" fill="{CYAN}" '
                         f'text-anchor="middle">entry</text>')
            parts.append(f'<path d="M {ex - 4:.1f} {eyy:.1f} h 8 l -4 6 z" fill="{CYAN}"/>')
    parts.append("</svg>")
    return "".join(parts)


def _card(mint: str, meta: dict, idx_row: dict | None) -> str:
    sym = html.escape(str(meta.get("symbol") or (idx_row or {}).get("symbol") or "?"))
    reason = meta.get("reason", "?")
    pool = meta.get("pool") or (idx_row or {}).get("pool") or ""
    candles = _candles(pool)
    entry_ts = meta.get("entry_ts") or None
    chart = (_svg(candles, entry_ts) if candles
             else f'<div class="noch">no cached candles for this pool</div>')
    if reason == "manual":
        res = meta.get("result", "?")
        res_col = GREEN if res == "win" else RED
        chip = '<span class="chip manual">MANUAL · FORWARD-ONLY</span>'
        detail = (f'flagged {html.escape(str(meta.get("flagged_at", "?")))} · '
                  f'<b style="color:{res_col}">{res.upper()} {meta.get("multiple", 0):.2f}x</b> — '
                  f'stays in current stats; only listings after the flag matching this mint/symbol are excluded'
                  + (f' · entry {fmt_ts(int(entry_ts))} UTC' if entry_ts else ""))
        action = (f'<button class="btn restore" data-mint="{html.escape(mint)}">Restore</button>')
    elif reason == "wash_ramp":
        chip = '<span class="chip ramp">WASH RAMP</span>'
        detail = (f'efficiency {meta.get("er", "?")} · green {meta.get("green_frac", "?")} · '
                  f'rise {meta.get("rise", "?")}x over {meta.get("n_candles", "?")} candles '
                  f'(gates: er≥0.70, green≥0.70, rise≥1.5x)')
        action = ""
    else:
        chip = '<span class="chip dup">SERIAL REDEPLOY</span>'
        detail = f'symbol launched {meta.get("deployments", "?")}× with ≥1 wash-ramp family member'
        action = ""
    be = (f'<a class="btn" href="https://birdeye.so/token/{html.escape(mint)}?chain=solana" '
          f'target="_blank">Birdeye</a>')
    return f"""
<div class="card">
  <div class="chead">
    <div><div class="t1">{sym} {chip}</div><div class="t2">{detail}</div></div>
    <div class="actions">{action}{be}</div>
  </div>
  <div class="chart">{chart}</div>
</div>"""


def render_page() -> str:
    auto = _read_json(backtest.BLOCKLIST_PATH) or {}
    manual = _read_json(backtest.MANUAL_BLOCKLIST_PATH) or {}
    idx = _index_map()
    n_ramp = sum(1 for v in auto.values() if v.get("reason") == "wash_ramp")
    n_dup = len(auto) - n_ramp
    man_wins = sum(1 for v in manual.values() if v.get("result") == "win")
    man_losses = sum(1 for v in manual.values() if v.get("result") == "loss")

    man_cards = [_card(m, v, idx.get(m)) for m, v in
                 sorted(manual.items(), key=lambda kv: kv[1].get("flagged_at", ""), reverse=True)]
    ramps = sorted(((m, v) for m, v in auto.items() if v.get("reason") == "wash_ramp"),
                   key=lambda kv: -(kv[1].get("rise") or 0))[:MAX_AUTO_CARDS]
    auto_cards = [_card(m, v, idx.get(m)) for m, v in ramps]

    skew = ""  # manual flags are forward-only: they cannot edit existing stats,
    # so the old deleting-losers inflation warning no longer applies

    bd_snap = json.dumps(bdusage.snapshot())
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BLACKLIST — purged charts</title>
<style>
  body{{background:#0a0a0e;color:#e8e8ea;font-family:"Cascadia Mono",Consolas,monospace;
       font-size:13px;margin:0;padding:20px 24px 60px}}
  h1{{font-size:18px;color:#ff7a1a;letter-spacing:1px}}
  h2{{font-size:13px;color:#e8e8ea;letter-spacing:1px;margin:26px 0 10px}}
  .sum{{color:{INK2};margin:6px 0 14px;font-size:12px;line-height:1.6;max-width:1150px}}
  .warn{{background:#2a1420;border:1px solid #b83d55;color:#ffb3c0;padding:10px 14px;
       margin:0 0 16px;max-width:1120px;font-size:12px;line-height:1.6}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:16px;max-width:1150px}}
  .card{{background:#101017;border:1px solid #2a2a38;
       clip-path:polygon(0 0,calc(100% - 12px) 0,100% 12px,100% 100%,0 100%)}}
  .chead{{display:flex;justify-content:space-between;align-items:center;gap:10px;
       padding:10px 14px;border-bottom:1px solid #2a2a38}}
  .t1{{font-size:14px;font-weight:bold}}
  .t2{{color:{INK2};font-size:11px;margin-top:3px}}
  .chip{{font-size:9.5px;padding:2px 7px;border-radius:3px;vertical-align:2px;letter-spacing:.5px}}
  .chip.manual{{background:#1b2a5e;color:#cfd8ff;border:1px solid #3d55b8}}
  .chip.ramp{{background:#2a1420;color:#ffb3c0;border:1px solid #b83d55}}
  .chip.dup{{background:#241a0e;color:#ffd9a8;border:1px solid #8a6a2f}}
  .actions{{display:flex;gap:8px;align-items:center}}
  .btn{{background:#1b2a5e;color:#cfd8ff;text-decoration:none;font-size:11px;
       padding:5px 11px;border:1px solid #3d55b8;border-radius:4px;white-space:nowrap;
       font-family:inherit;cursor:pointer}}
  .btn.restore{{background:#0e2418;color:#a8e8c8;border-color:#2f8a5a}}
  .chart{{background:#0d0d13;padding:6px}}
  .noch{{color:{INK3};padding:30px;text-align:center;font-size:11px}}
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
  <a class="tab" href="/trades" title="Per-trade journal cards">TRADE JOURNAL</a>
  <a class="tab" href="/montecarlo" title="Monte Carlo equity-path simulation">MONTE CARLO</a>
  <span class="tab on">BLACKLIST</span>
  <div class="bd"><span class="bdlab">BIRDEYE API</span><span id="bdtext">no usage tracked yet</span>
    <div class="bdbar"><div id="bdfill"></div></div></div>
</div>
<h1>BLACKLIST — CHARTS PURGED FROM THE SAMPLE</h1>
<div class="sum"><b>{len(manual)} manual</b> (journal Blacklist button: {man_wins} wins / {man_losses} losses at flag time)
 · <b>{n_ramp:,} auto wash-ramps</b> · <b>{n_dup:,} serial redeploys</b>.<br>
<b>Manual flags are forward-only:</b> the flagged trade STAYS in every existing stat — deleting known
losers would inflate the numbers (locked honesty rule). A flag only excludes tokens <i>listed after
the flag time</i> that match the flagged mint or symbol (serial redeploys of the same scam), and files
the chart here as an avoid-pattern reference.<br>
<b>Auto detections are retroactive:</b> the detector (<code>blocklist.py</code>) only ever purges
<i>rising</i> ramps — fabricated wins — so the automated cleanup can only lower results, never
inflate them. Both exclusions apply everywhere the token index is consumed (backtest, sweeps, evals,
trade journal; Monte Carlo pool after <code>--refresh-trades</code>); static pages reflect changes
after their next regeneration.</div>
{skew}
<h2>MANUAL FLAGS ({len(manual)})</h2>
<div class="grid">{"".join(man_cards) or '<div class="sum">none yet — use the Blacklist button on a trade card</div>'}</div>
<h2>AUTO-DETECTED WASH RAMPS — {MAX_AUTO_CARDS} most extreme of {n_ramp:,}</h2>
<div class="grid">{"".join(auto_cards)}</div>
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
document.querySelectorAll('.restore').forEach(b => b.onclick = () => {{
  fetch('/api/blacklist', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{mint: b.dataset.mint, action: 'remove'}})}})
    .then(r => {{ if (r.ok) location.reload(); }});
}});
</script>
</body></html>"""


if __name__ == "__main__":
    out = os.path.join("reports", "blacklist_preview.html")
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_page())
    print(f"preview written: {os.path.abspath(out)}")
