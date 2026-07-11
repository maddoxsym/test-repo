#!/usr/bin/env python3
"""
Backtest the full pipeline on synthetic or real candles.

    # mechanics check on synthetic data (no account needed):
    python backtest.py --synthetic

    # real M5 candles from a CSV (columns: time,open,high,low,close[,volume]):
    python backtest.py --csv my_xauusd_m5.csv --instrument XAU_USD

    # pull real history straight from your OANDA account:
    python backtest.py --oanda --instrument EUR_USD --count 5000

    # warm-start the live bot's learning from a backtest:
    python backtest.py --oanda --instrument XAU_USD --learn-db bot_data.sqlite3
"""

import argparse
import logging
import sys

from oanda_bot.backtester import (Backtester, load_csv_candles,
                                  synthetic_candles)
from oanda_bot.config import load_config
from oanda_bot.journal import Journal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instrument", default="XAU_USD",
                        choices=["XAU_USD", "EUR_USD"])
    parser.add_argument("--synthetic", action="store_true",
                        help="use generated candles (mechanics test only)")
    parser.add_argument("--candles", type=int, default=8000,
                        help="number of synthetic candles")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--csv", help="path to a candle CSV file")
    parser.add_argument("--oanda", action="store_true",
                        help="download recent M5 candles from OANDA")
    parser.add_argument("--count", type=int, default=5000,
                        help="candles to download with --oanda (max 5000)")
    parser.add_argument("--balance", type=float, default=10_000.0)
    parser.add_argument("--learn-db",
                        help="persist learned weights/combos into this sqlite "
                             "db (use bot_data.sqlite3 to warm-start the bot)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)-7s %(name)-10s %(message)s")

    cfg = load_config()
    if args.csv:
        candles = load_csv_candles(args.csv)
        source = args.csv
    elif args.oanda:
        from oanda_bot.oanda import OandaClient
        problems = cfg.oanda.validate()
        if problems:
            sys.exit("--oanda needs credentials:\n  " + "\n  ".join(problems))
        client = OandaClient(cfg.oanda)
        candles = client.candles(args.instrument, "M5",
                                 min(args.count, 5000))
        source = f"OANDA {cfg.oanda.environment}"
    elif args.synthetic:
        candles = synthetic_candles(args.instrument, args.candles, args.seed)
        source = f"synthetic (seed {args.seed})"
    else:
        parser.error("choose a data source: --synthetic, --csv or --oanda")

    if len(candles) < 500:
        sys.exit(f"not enough candles ({len(candles)}); need at least 500")

    print(f"backtesting {args.instrument} on {len(candles)} M5 candles "
          f"from {source} ...")
    journal = Journal(args.learn_db) if args.learn_db else None
    bt = Backtester(cfg, args.instrument, candles,
                    start_balance=args.balance, journal=journal)
    result = bt.run()
    print(result.summary())
    if args.synthetic:
        print("\nNOTE: synthetic data verifies the mechanics, not "
              "profitability. Run on real candles and paper-trade before "
              "trusting anything.")
    if args.learn_db:
        print(f"\nlearned weights and combo stats saved to {args.learn_db}")


if __name__ == "__main__":
    main()
