#!/usr/bin/env python
"""Solana memecoin trading bot. Paper-trades by default; live mode is opt-in."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.config import Config
from bot.main import Bot, report, setup_logging
from bot.util import load_dotenv


def main() -> None:
    ap = argparse.ArgumentParser(description="Solana memecoin trading bot (paper mode by default)")
    ap.add_argument("--config", default="config.json", help="path to config file")
    ap.add_argument("--once", action="store_true", help="run a single scan/manage cycle and exit")
    ap.add_argument("--report", action="store_true", help="print portfolio report and exit")
    ap.add_argument("--live", action="store_true",
                    help="trade real funds (also requires MEMEBOT_I_UNDERSTAND_THE_RISKS=yes)")
    ap.add_argument("--workdir", help="run against this data directory instead of the "
                                      "code's own: db, log, reports/ and the Birdeye "
                                      "usage meter all resolve here. Lets a checkout "
                                      "trade into another checkout's trial data.")
    args = ap.parse_args()

    code_dir = os.path.dirname(os.path.abspath(__file__))
    # Resolve the config before moving: it ships with the code, while --workdir
    # points at the data. Relative paths still fall back to the code directory,
    # so `run.py --config config.json` keeps working from anywhere.
    if not os.path.isabs(args.config) and not os.path.exists(args.config):
        args.config = os.path.join(code_dir, args.config)
    args.config = os.path.abspath(args.config)
    os.chdir(os.path.abspath(args.workdir) if args.workdir else code_dir)
    if args.workdir:
        import bdusage
        bdusage.set_data_dir(os.getcwd())
    load_dotenv()

    cfg = Config.load(args.config)
    if args.live:
        cfg.mode = "live"
    elif cfg.mode == "live":
        print("config sets mode=live but --live flag not passed; running in PAPER mode instead.")
        cfg.mode = "paper"

    setup_logging(cfg)
    if args.report:
        report(cfg)
        return

    bot = Bot(cfg)
    try:
        bot.run(once=args.once)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
