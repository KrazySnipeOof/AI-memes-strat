#!/usr/bin/env python
"""Publish a static snapshot of the NERV dashboard to GitHub Pages.

GitHub Pages cannot run dashboard/server.py, so this script renders the two
payloads the server exposes (/api/state, /api/bdusage) to static JSON files,
pairs them with a fetch-rewritten copy of dashboard/index.html, and force-
pushes the result as the single-commit gh-pages branch. The published page
carries a staleness badge so a dead publisher is visible on the page itself.

Usage:
  python pages_publish.py              one snapshot, push, exit
  python pages_publish.py --loop 5     snapshot + push every 5 minutes
  python pages_publish.py --no-push    render into .pages_deploy/ only
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "dashboard"))

DEPLOY_DIR = os.path.join(ROOT, ".pages_deploy")
BRANCH = "gh-pages"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's well-known empty tree

FETCH_REWRITES = [
    ("fetch('/api/state', {cache:'no-store'})",
     "fetch('data/state.json?t=' + Date.now(), {cache:'no-store'})"),
    ("fetch('/api/bdusage')",
     "fetch('data/bdusage.json?t=' + Date.now())"),
    ('href="/trades"', 'href="trades.html"'),
    ('href="/wallets"', 'href="wallets.html"'),
]

BADGE = """
<div id="pages-staleness" style="position:fixed;right:10px;bottom:10px;z-index:9999;\
font:11px monospace;padding:4px 8px;background:#000c;border:1px solid #3f6;color:#3f6;\
border-radius:3px;">SNAPSHOT --</div>
<script>
(function () {
  var el = document.getElementById('pages-staleness');
  function tick() {
    fetch('data/state.json?t=' + Date.now(), {cache: 'no-store'})
      .then(function (r) { return r.json(); })
      .then(function (s) {
        var mins = (Date.now() - Date.parse(s.now)) / 60000;
        el.textContent = mins < 1.5 ? 'SNAPSHOT LIVE' : 'SNAPSHOT ' + Math.round(mins) + 'm OLD';
        var c = mins > 12 ? '#f33' : mins > 6 ? '#fc0' : '#3f6';
        el.style.color = c; el.style.borderColor = c;
      })
      .catch(function () { el.textContent = 'SNAPSHOT ?'; });
  }
  tick();
  setInterval(tick, 30000);
})();
</script>
</body>"""


def git(*args: str, cwd: str = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r


def init_server(config_path: str):
    import server  # dashboard/server.py

    os.chdir(ROOT)
    server.load_dotenv()
    server.cfg = server.Config.load(config_path)
    asym = os.path.join(ROOT, "config.asym.json")
    server.acfg = server.Config.load(asym) if os.path.exists(asym) else None
    server.session = server.make_session()
    server.cache = server.QuoteCache(server.session, server.cfg)
    return server


def ensure_worktree() -> None:
    git("worktree", "prune")
    if os.path.exists(os.path.join(DEPLOY_DIR, ".git")):
        return
    if git("rev-parse", "--verify", BRANCH, check=False).returncode != 0:
        sha = git("commit-tree", EMPTY_TREE, "-m", "pages snapshot").stdout.strip()
        git("branch", BRANCH, sha)
    git("worktree", "add", DEPLOY_DIR, BRANCH)


def render(server) -> None:
    state = server.build_state()
    state["wallet"] = None  # public page: never expose wallet details
    snap = server.bdusage.snapshot()
    prog = server._read_json(os.path.join("reports", "bd_progress.json"))
    if prog and prog.get("requests"):
        snap["last_run"] = {"name": prog.get("name"), "phase": prog.get("phase"),
                            "requests": prog.get("requests"), "updated": prog.get("updated")}

    with open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8") as f:
        html = f.read()
    for old, new in FETCH_REWRITES:
        if old not in html:
            print(f"WARN: rewrite target not found in dashboard/index.html: {old}")
        html = html.replace(old, new)
    html = html.replace("</body>", BADGE, 1)

    data_dir = os.path.join(DEPLOY_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(DEPLOY_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(data_dir, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    with open(os.path.join(data_dir, "bdusage.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    open(os.path.join(DEPLOY_DIR, ".nojekyll"), "w").close()

    cards = os.path.join(ROOT, "reports", "trade_cards.html")
    if os.path.exists(cards):
        with open(cards, encoding="utf-8") as f:
            page = f.read()
        page = page.replace('href="/trades"', 'href="trades.html"')
        page = page.replace('href="/wallets"', 'href="wallets.html"')
        page = page.replace('href="/"', 'href="index.html"')
        with open(os.path.join(DEPLOY_DIR, "trades.html"), "w", encoding="utf-8") as f:
            f.write(page)

    try:
        import walletpage
        page = walletpage.render_page()
        page = page.replace('href="/trades"', 'href="trades.html"')
        page = page.replace('href="/"', 'href="index.html"')
        with open(os.path.join(DEPLOY_DIR, "wallets.html"), "w", encoding="utf-8") as f:
            f.write(page)
    except Exception as exc:
        print(f"WARN: wallets page render failed: {exc}")


def push() -> bool:
    git("add", "-A", cwd=DEPLOY_DIR)
    if not git("status", "--porcelain", cwd=DEPLOY_DIR).stdout.strip():
        return False
    git("commit", "--amend", "--no-edit", cwd=DEPLOY_DIR)
    git("push", "--force", "origin", BRANCH, cwd=DEPLOY_DIR)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Static GitHub Pages publisher for the NERV dashboard")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.asym.json"))
    ap.add_argument("--loop", type=float, default=0, metavar="MIN",
                    help="republish every MIN minutes (default: publish once and exit)")
    ap.add_argument("--no-push", action="store_true", help="render into .pages_deploy/ without pushing")
    args = ap.parse_args()

    server = init_server(args.config)
    ensure_worktree()
    while True:
        try:
            render(server)
            if args.no_push:
                print(f"{server.iso_now()} rendered (push skipped)", flush=True)
            elif push():
                print(f"{server.iso_now()} published", flush=True)
            else:
                print(f"{server.iso_now()} no changes", flush=True)
        except Exception as exc:
            print(f"{server.iso_now()} publish failed: {exc}", flush=True)
        if args.loop <= 0:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
