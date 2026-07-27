#!/usr/bin/env python
"""FUNDER GRAPH tab (served at /fundergraph): visualizes why deployer- and
funder-reputation don't work on this token class. Each serial-scam family
(same ticker relaunched 100s of times) is drawn as a token -> deployer ->
funder lineage. If reputation worked every line would converge on ONE reused
funder; instead the lineage fans out to a fresh wallet per launch.

Reads reports/funder_graph.json (funder_graph.py). Self-contained inline SVG.
"""
from __future__ import annotations

import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bdusage

GREEN, RED, CYAN, AMBER, INK, INK2, INK3 = ("#00a869", "#ff3355", "#2695c4", "#ffc233",
                                            "#e8e8ea", "#9a9aa8", "#5c5c6c")
GRAPH_PATH = os.path.join("reports", "funder_graph.json")


def _e(s):
    return html.escape(str(s))


def _short(a):
    return f"{a[:4]}…{a[-4:]}" if a and len(a) > 10 else (a or "?")


def svg_lineage(fam) -> str:
    """One family's token->deployer->funder lineage. Shared funders (used by >1
    token = the only reputation-usable case) are highlighted; the point is how
    rare that is."""
    members = [m for m in fam["members"] if m.get("deployer")]
    if not members:
        return '<div class="none">no lineage resolved</div>'
    n = len(members)
    rowH, padT, W = 30, 22, 760
    H = padT * 2 + n * rowH
    xT, xD, xF = 70, 340, 600
    yof = lambda i: padT + i * rowH + rowH / 2

    # group node y-positions by wallet
    dep_rows, fund_rows = {}, {}
    for i, m in enumerate(members):
        dep_rows.setdefault(m["deployer"], []).append(i)
        if m.get("funder"):
            fund_rows.setdefault(m["funder"], []).append(i)
    dep_y = {d: sum(yof(i) for i in rows) / len(rows) for d, rows in dep_rows.items()}
    fund_y = {f: sum(yof(i) for i in rows) / len(rows) for f, rows in fund_rows.items()}
    tx = fam.get("funder_txcounts", {})

    p = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
         f'style="width:100%;height:auto;display:block" font-family="Cascadia Mono,Consolas,monospace">']
    # column headers
    for x, lab in ((xT, "TOKEN (launch)"), (xD, "DEPLOYER"), (xF, "FUNDER")):
        p.append(f'<text x="{x}" y="12" font-size="10" fill="{INK3}" text-anchor="middle" letter-spacing="1">{lab}</text>')
    # edges: token -> deployer -> funder
    for i, m in enumerate(members):
        yt = yof(i)
        yd = dep_y[m["deployer"]]
        p.append(f'<path d="M {xT+8} {yt:.1f} C {(xT+xD)/2} {yt:.1f} {(xT+xD)/2} {yd:.1f} {xD-8} {yd:.1f}" '
                 f'fill="none" stroke="{INK3}" stroke-width="1" opacity="0.55"/>')
        if m.get("funder"):
            yf = fund_y[m["funder"]]
            shared = len(fund_rows[m["funder"]]) > 1
            col = AMBER if shared else "#3a3a48"
            p.append(f'<path d="M {xD+8} {yd:.1f} C {(xD+xF)/2} {yd:.1f} {(xD+xF)/2} {yf:.1f} {xF-8} {yf:.1f}" '
                     f'fill="none" stroke="{col}" stroke-width="{1.6 if shared else 1}" opacity="0.7"/>')
    # token + deployer nodes
    for i, m in enumerate(members):
        yt = yof(i)
        p.append(f'<circle cx="{xT}" cy="{yt:.1f}" r="4" fill="{CYAN}"/>')
    for d, y in dep_y.items():
        p.append(f'<circle cx="{xD}" cy="{y:.1f}" r="4.5" fill="{RED}"/>'
                 f'<text x="{xD+10}" y="{y+3.5:.1f}" font-size="9.5" fill="{INK2}">{_short(d)}</text>')
    # funder nodes (amber if shared)
    for f, y in fund_y.items():
        shared = len(fund_rows[f]) > 1
        big = tx.get(f, 0) >= 1000
        col = AMBER if shared else INK3
        note = " ⚠CEX/router" if big else ""
        p.append(f'<circle cx="{xF}" cy="{y:.1f}" r="{5.5 if shared else 4}" fill="{col}"/>'
                 f'<text x="{xF+10}" y="{y+3.5:.1f}" font-size="9.5" fill="{col if shared else INK2}">'
                 f'{_short(f)}{note}</text>')
    p.append("</svg>")
    return "".join(p)


def render_page() -> str:
    d = _read_json(GRAPH_PATH)
    bd_snap = json.dumps(bdusage.snapshot())
    if not d or not d.get("families"):
        body = ('<div class="none">no funder-graph data yet — run '
                '<code>python funder_graph.py</code></div>')
        tiles = ""
    else:
        fams = d["families"]
        dp = d.get("deployer_probe", {})
        dep_rep = f"{dp.get('repeat_deployers', 0)}/{dp.get('sampled', 0)}"
        tot_dep = sum(f["n_deployers"] for f in fams)
        tot_fund = sum(f["n_funders"] for f in fams)
        shared = sum(1 for f in fams for fu, rows in
                     {m["funder"]: [x for x in f["members"] if x.get("funder") == m["funder"]]
                      for m in f["members"] if m.get("funder")}.items() if len(rows) > 1)
        tiles = (
            _tile("DEPLOYER REPEAT", dep_rep + " tokens", "share a deployer in the 180-token probe (~1%)", RED)
            + _tile("SAME-OPERATION FAMILIES", str(len(fams)), "serial-redeploy scams (same ticker, 100s of relaunches)", INK)
            + _tile("DEPLOYERS → FUNDERS", f"{tot_dep} → {tot_fund}", "across the families: near 1:1, no convergence", RED)
            + _tile("VERDICT", "ROTATED", "fresh deployer AND funder per launch — no identity to score", AMBER))
        cards = []
        for f in fams:
            conv = ("full rotation — every launch a fresh funder"
                    if f["n_funders"] >= f["n_deployers"]
                    else f"{f['n_deployers'] - f['n_funders']} funder(s) reused across launches")
            cards.append(f"""
<div class="fam">
  <div class="fh"><span class="sym">{_e(f['symbol'])}</span>
    <span class="meta">{f['launches']} launches · checked {f['checked']} · {f['n_deployers']} deployers → {f['n_funders']} funders</span>
    <span class="conv">{_e(conv)}</span></div>
  <div class="graph">{svg_lineage(f)}</div>
</div>""")
        body = "".join(cards)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FUNDER GRAPH — rug lineage</title>
<style>
  body{{background:#0a0a0e;color:{INK};font-family:"Cascadia Mono",Consolas,monospace;font-size:13px;margin:0;padding:20px 24px 60px}}
  h1{{font-size:18px;color:#ff7a1a;letter-spacing:1px;margin:0 0 4px}}
  .sub{{color:{INK2};margin:6px 0 16px;font-size:12px;line-height:1.6;max-width:1050px}}
  .tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;max-width:1150px;margin-bottom:20px}}
  .tile{{background:#101017;border:1px solid #2a2a38;border-radius:8px;padding:12px 14px}}
  .tile .l{{color:{INK3};font-size:10px;letter-spacing:1px}}
  .tile .v{{font-size:22px;font-weight:bold;margin:3px 0}}
  .tile .n{{color:{INK3};font-size:10.5px;line-height:1.4}}
  .fam{{background:#101017;border:1px solid #2a2a38;border-radius:8px;margin-bottom:16px;max-width:1050px;
       clip-path:polygon(0 0,calc(100% - 12px) 0,100% 12px,100% 100%,0 100%)}}
  .fh{{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;padding:11px 16px;border-bottom:1px solid #2a2a38}}
  .sym{{font-size:15px;font-weight:bold;color:{AMBER}}}
  .meta{{color:{INK2};font-size:11.5px}}
  .conv{{margin-left:auto;color:{RED};font-size:11px}}
  .graph{{padding:10px 14px;background:#0d0d13;overflow-x:auto}}
  .legend{{display:flex;gap:18px;flex-wrap:wrap;color:{INK2};font-size:11px;margin:0 0 14px;max-width:1050px}}
  .legend b{{color:{INK}}}
  .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;vertical-align:1px;margin-right:5px}}
  .none{{color:{INK3};padding:30px;text-align:center}}
  code{{color:{AMBER}}}
  .nav{{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:8px;background:#0a0a0e;
       border-bottom:1px solid #2a2a38;margin:-20px -24px 16px;padding:10px 24px}}
  .tab{{background:#14141d;color:{INK2};border:1px solid #2a2a38;padding:6px 16px;font-size:11px;
       letter-spacing:1px;text-decoration:none;white-space:nowrap}}
  .tab:hover{{color:{INK};border-color:{INK3}}}
  .tab.on{{color:{AMBER};border-color:{AMBER}}}
  .bd{{margin-left:auto;min-width:280px;max-width:440px;text-align:right}}
  .bdlab{{color:#ff7a1a;font-weight:bold;font-size:10px;letter-spacing:1px;margin-right:8px}}
  #bdtext{{font-size:10.5px;color:{INK2}}}
  .bdbar{{height:4px;background:#1e1e2a;margin-top:4px;border-radius:2px;overflow:hidden}}
  #bdfill{{height:100%;width:0;background:{GREEN};transition:width .4s}}
</style></head><body>
<div class="nav">
  <a class="tab" href="/" title="NERV dashboard home">&#8962; HOME</a>
  <a class="tab" href="/trades" title="Per-trade journal">TRADE JOURNAL</a>
  <a class="tab" href="/montecarlo" title="Monte Carlo equity paths">MONTE CARLO</a>
  <a class="tab" href="/blacklist" title="Charts purged from the sample">BLACKLIST</a>
  <a class="tab" href="/wallets" title="Scouted wallets + harvest">WALLETS</a>
  <span class="tab on">FUNDER GRAPH</span>
  <div class="bd"><span class="bdlab">BIRDEYE API</span><span id="bdtext">no usage tracked yet</span>
    <div class="bdbar"><div id="bdfill"></div></div></div>
</div>
<h1>FUNDER GRAPH — WHY WALLET REPUTATION CAN'T WORK</h1>
<div class="sub">Deployer- and funder-reputation would let us avoid known scammers — <b>if</b> the same wallet
recurred across their launches. It doesn't. Each family below is one serial scam (same ticker relaunched
100s of times); the graph traces every sampled launch <span style="color:{CYAN}">token</span> →
<span style="color:{RED}">deployer</span> → <span style="color:{AMBER}">funder</span>. If reputation
worked, every line would converge on ONE reused funder on the right. Instead the lineage fans out — a
fresh deployer <i>and</i> a fresh funder per launch. There is no on-chain identity to score.</div>
<div class="tiles">{tiles}</div>
<div class="legend">
  <span><span class="dot" style="background:{CYAN}"></span>token launch</span>
  <span><span class="dot" style="background:{RED}"></span>deployer wallet</span>
  <span><span class="dot" style="background:{AMBER}"></span>funder (amber = reused by &gt;1 launch — the rare, reputation-usable case)</span>
  <span>⚠CEX/router = high-traffic funder (noise, not a private master)</span>
</div>
{body}
<script>
const BD_EMBED = {bd_snap};
function bdFmt(n){{return n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':String(n);}}
function bdRender(s,live){{
  if(!s||s.requests===undefined)return;
  const p=s.pct_of_plan||0;
  document.getElementById('bdtext').textContent=s.requests.toLocaleString()+' req · ~'+bdFmt(s.cu_est)+' / '+bdFmt(s.plan_cu)+' CU ('+p.toFixed(1)+'%) · '+(s.period_label||s.month)+(live?'':' · at page build');
  const f=document.getElementById('bdfill');f.style.width=Math.min(100,p)+'%';
  f.style.background=p>=90?'{RED}':p>=60?'{AMBER}':'{GREEN}';
}}
bdRender(BD_EMBED,false);
fetch('/api/bdusage').then(r=>r.json()).then(s=>bdRender(s,true)).catch(()=>{{}});
</script>
</body></html>"""


def _tile(label, val, note, col):
    return (f'<div class="tile"><div class="l">{_e(label)}</div>'
            f'<div class="v" style="color:{col}">{_e(val)}</div>'
            f'<div class="n">{_e(note)}</div></div>')


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join("reports", "fundergraph_preview.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_page())
    print("preview:", os.path.abspath(out))
