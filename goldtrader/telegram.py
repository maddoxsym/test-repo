"""Telegram notifier — daily reports and trade alerts from the bot.

Setup (2 minutes, on any phone):
  1. In Telegram, message @BotFather -> /newbot -> pick a name.
     BotFather replies with a BOT TOKEN like 123456789:AAF...xyz
  2. Open your new bot's chat and press START (send it any message).
  3. Get your chat id: message @userinfobot, it replies with your id.
  4. Create data/telegram_config.json (gitignored, stays on your machine):
       {"bot_token": "123456789:AAF...xyz", "chat_id": 111222333}

Every send fails soft: no Telegram config or no network never breaks
trading — the message is simply skipped.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from memetrader.config import DATA_DIR

TELEGRAM_CONFIG_PATH = os.path.join(DATA_DIR, "telegram_config.json")
_TIMEOUT = 10


def _config() -> dict | None:
    if not os.path.exists(TELEGRAM_CONFIG_PATH):
        return None
    try:
        with open(TELEGRAM_CONFIG_PATH) as f:
            cfg = json.load(f)
        return cfg if cfg.get("bot_token") and cfg.get("chat_id") else None
    except (json.JSONDecodeError, OSError):
        return None


def telegram_enabled() -> bool:
    return _config() is not None


def send(text: str) -> bool:
    """Send a message to the configured chat. Returns True on success."""
    cfg = _config()
    if cfg is None:
        return False
    url = (f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage?"
           + urllib.parse.urlencode({"chat_id": cfg["chat_id"], "text": text}))
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:
            return r.status == 200
    except Exception:
        return False


def daily_report(row: dict) -> bool:
    """Send a campaign/day summary row as a readable Telegram message."""
    if row.get("market_closed"):
        return send(f"🥇 Gold bot — {row['date']}: market closed (weekend). "
                    f"Equity ${row['equity']:,.2f}.")
    return send(
        f"🥇 Gold bot day {row['day']} ({row['date']})\n"
        f"${row['start_equity']:,.2f} → ${row['end_equity']:,.2f} "
        f"({row['pnl_pct']:+.2f}%)\n"
        f"{row['trades']} trades, win {row['win_rate']}%"
        + (f"\n⚠️ {row['guard']}" if row.get("guard") else "")
    )
