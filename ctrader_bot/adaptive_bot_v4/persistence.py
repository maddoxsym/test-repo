"""
Restart-proof persistence for the 14-day research.

One JSON state file (atomic write: tmp + rename) carries everything that
must survive cTrader/computer restarts: the research start time, strategy
configurations and versions, performance statistics, rankings inputs,
guard state (so daily/weekly limits cannot be bypassed by restarting),
shadow book equity, counters and flags.

CSV files (append-only) record the research trail: shadow trades, real
trades, rejected setups, daily summaries, equity history, learning
decisions and parameter updates.  The final report is written as .txt and
.json.  No secrets are ever written.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Callable, Dict, List, Optional

STATE_FILE = "research_state.json"

CSV_FIELDS: Dict[str, List[str]] = {
    "shadow_trades": [
        "trade_id", "strategy_id", "tf", "direction", "signal_time",
        "entry_time", "exit_time", "entry", "stop", "target", "exit_price",
        "exit_reason", "units", "risk_pct", "risk_money", "profit",
        "r_multiple", "mfe_r", "mae_r", "bars_open", "spread_points",
        "regime", "session", "mgmt_mode", "reason"],
    "real_trades": [
        "trade_id", "position_id", "strategy_id", "direction", "entry_time",
        "exit_time", "entry", "stop", "target", "exit_price", "exit_reason",
        "units", "risk_pct", "risk_money", "profit", "r_multiple", "mfe_r",
        "mae_r", "bars_open", "spread_points", "regime", "session",
        "reason"],
    "rejections": [
        "time", "stage", "strategy_id", "regime", "session",
        "spread_points", "reason"],
    "daily_summary": [
        "date", "day_index", "start_equity", "end_equity", "realised_pl",
        "realised_pct", "trades_real", "trades_shadow", "wins_real",
        "losses_real", "max_daily_dd_pct", "weekly_dd_pct",
        "active_strategies", "benched", "retired", "best_strategy",
        "best_score", "regime_mix", "lock_events"],
    "equity_history": [
        "time", "equity", "balance", "floating", "daily_pl_pct",
        "weekly_dd_pct"],
    "learning_log": ["date", "kind", "sid", "why"],
    "parameter_updates": ["date", "sid", "version", "param", "old", "new",
                          "evidence"],
}


class StateStore:

    def __init__(self, cfg, log: Callable[[str], None]):
        self.cfg = cfg
        self.log = log
        self.directory: Optional[str] = self._prepare_dir()
        self.enabled = self.directory is not None

    def _prepare_dir(self) -> Optional[str]:
        base = self.cfg.state_dir.strip() if self.cfg.state_dir else ""
        home = os.path.expanduser("~")
        candidates = []
        if base:
            candidates.append(base)
        candidates.append(os.path.join(home, "Documents",
                                       "XAUUSD_Adaptive_Bot_V4"))
        candidates.append(os.path.join(home, "XAUUSD_Adaptive_Bot_V4"))
        candidates.append(os.path.join(os.getcwd(),
                                       "XAUUSD_Adaptive_Bot_V4_state"))
        for cand in candidates:
            try:
                os.makedirs(cand, exist_ok=True)
                probe = os.path.join(cand, ".write_probe")
                with open(probe, "w", encoding="utf-8") as fh:
                    fh.write("ok")
                os.remove(probe)
                return os.path.abspath(cand)
            except OSError:
                continue
        self.log("PERSISTENCE: no writable directory — research state "
                 "CANNOT survive restarts. Fix folder permissions before "
                 "starting the 14-day run.")
        return None

    # ------------------------------------------------------------ json state
    def load_state(self) -> Optional[dict]:
        if not self.enabled:
            return None
        path = os.path.join(self.directory, STATE_FILE)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            self.log(f"PERSISTENCE: state file unreadable ({exc}) — "
                     f"starting fresh but NOT deleting the old file")
            try:
                os.replace(path, path + ".corrupt")
            except OSError:
                pass
            return None

    def save_state(self, state: dict) -> None:
        if not self.enabled:
            return
        path = os.path.join(self.directory, STATE_FILE)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=1, default=str)
            os.replace(tmp, path)              # atomic on POSIX/macOS
        except OSError as exc:
            self.log(f"PERSISTENCE: state save failed ({exc})")

    # ------------------------------------------------------------ csv trail
    def csv_append(self, name: str, row: dict) -> None:
        if not self.enabled:
            return
        fields = CSV_FIELDS[name]
        path = os.path.join(self.directory, f"{name}.csv")
        try:
            fresh = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields,
                                        extrasaction="ignore", restval="")
                if fresh:
                    writer.writeheader()
                writer.writerow(row)
        except OSError as exc:
            self.log(f"PERSISTENCE: csv append to {name} failed ({exc})")

    def write_text(self, filename: str, text: str) -> None:
        if not self.enabled:
            return
        try:
            with open(os.path.join(self.directory, filename), "w",
                      encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            self.log(f"PERSISTENCE: write {filename} failed ({exc})")

    def write_json(self, filename: str, obj: dict) -> None:
        if not self.enabled:
            return
        try:
            with open(os.path.join(self.directory, filename), "w",
                      encoding="utf-8") as fh:
                json.dump(obj, fh, indent=1, default=str)
        except OSError as exc:
            self.log(f"PERSISTENCE: write {filename} failed ({exc})")
