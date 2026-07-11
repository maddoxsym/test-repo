#!/usr/bin/env python3
"""
Start the live trading engine.

    python run_bot.py            # trade on the account in .env (practice!)
    python run_bot.py --paper    # full pipeline, but simulate fills locally

Stop safely at any time with Ctrl+C, or by creating a file named
KILL_SWITCH next to this script.  Open positions always carry a stop loss
and take profit on OANDA's side, so they stay protected even if the bot
(or your machine) dies.
"""

import argparse
import logging
import os
import sys

from oanda_bot.config import load_config
from oanda_bot.engine import TradingEngine


def setup_logging(log_path: str) -> None:
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper", action="store_true",
                        help="decide and journal, but never send real orders")
    args = parser.parse_args()

    cfg = load_config()
    cfg.engine.paper = cfg.engine.paper or args.paper
    setup_logging(cfg.engine.log_path)

    engine = TradingEngine(cfg)
    engine.run()


if __name__ == "__main__":
    main()
