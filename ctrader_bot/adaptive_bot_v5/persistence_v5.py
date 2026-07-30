"""
Restart-proof persistence and the CSV research trail.

One JSON state file, written atomically (tmp file + os.replace, which is
atomic on macOS), carries everything that must survive a cTrader restart, a
computer sleep or a crash: the research clock, the strategy population,
per-variant and per-family statistics, guard state (so daily and weekly limits
cannot be bypassed by restarting), virtual book equity and counters.

A corrupt state file is never deleted — it is renamed to .corrupt so it can be
inspected, and the bot starts fresh rather than acting on unreadable data.

The state carries a SCHEMA VERSION.  State written by a different schema is
preserved and ignored rather than half-loaded, which is what prevents the
"duplicate restored trades" class of bug: restoration is all-or-nothing per
section, statistics are counters (not trade lists), and the research clock
merges by taking the maximum minutes per date.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Callable, Dict, List, Optional

STATE_FILE = "research_state.json"

CSV_FIELDS: Dict[str, List[str]] = {
    "shadow_trades": [
        "trade_id", "sid", "family", "version", "direction", "signal_time",
        "entry_time", "exit_time", "entry_ref", "entry", "initial_stop",
        "final_stop", "tp1", "target", "exit_price", "exit_label",
        "units_initial", "units_partial", "risk_pct", "risk_money",
        "confluence_score", "confluence_detail", "gross_money", "commission",
        "net_money", "gross_r", "net_r", "mfe_r", "mae_r", "bars_open",
        "partial_taken", "partial_price", "partial_reason", "be_activated",
        "be_reason", "trail_updates", "early_exit_reason", "stop_reason",
        "tp1_reason", "target_reason", "sweep_kind", "regime", "session",
        "htf_bias", "location", "spread_points", "status", "discard_reason",
        "suspect", "suspect_reason"],
    "real_trades": [
        "trade_id", "position_id", "sid", "family", "version", "direction",
        "entry_time", "exit_time", "entry", "initial_stop", "final_stop",
        "tp1", "target", "exit_price", "exit_label", "units_initial",
        "units_partial", "risk_pct", "risk_money", "confluence_score",
        "confluence_detail", "gross_money", "commission", "net_money",
        "gross_r", "net_r", "mfe_r", "mae_r", "bars_open", "partial_taken",
        "partial_price", "partial_reason", "be_activated", "be_reason",
        "trail_updates", "early_exit_reason", "stop_reason", "tp1_reason",
        "target_reason", "sweep_kind", "regime", "session", "htf_bias",
        "location", "spread_points", "broker_profit"],
    "rejected_setups": [
        "time", "stage", "sid", "family", "direction", "regime", "session",
        "htf_bias", "spread_points", "confluence_score", "reason",
        "sequence"],
    "strategy_rankings": [
        "date", "rank", "sid", "family", "version", "status", "trades",
        "real_trades", "confidence", "net_expectancy", "shrunk_expectancy",
        "score", "win_rate", "profit_factor", "avg_win_r", "avg_loss_r",
        "max_dd_r", "std_r", "avg_mfe_r", "avg_mae_r", "stop_too_tight_rate",
        "partials", "be_activations", "trail_uses", "early_exits",
        "suspect_excluded", "best_regime", "params"],
    "family_rankings": [
        "date", "rank", "family", "trades", "real_trades", "confidence",
        "net_expectancy", "shrunk_expectancy", "score", "win_rate",
        "profit_factor", "max_dd_r", "avg_mfe_r", "avg_mae_r", "exit_mix"],
    "daily_summary": [
        "date", "research_day", "active_minutes", "start_equity",
        "end_equity", "realised_pl", "realised_pct", "real_trades",
        "shadow_trades_closed", "shadow_setups_discarded", "wins_real",
        "losses_real", "daily_combined_pct", "weekly_dd_pct",
        "active_variants", "benched", "retired", "best_variant",
        "best_variant_score", "best_family", "best_family_score",
        "regime_mix", "lock_events"],
    "equity_history": [
        "time", "equity", "balance", "floating", "daily_combined_pct",
        "weekly_dd_pct", "open_real", "open_shadow"],
    "learning_log": ["date", "kind", "sid", "param", "old", "new", "why"],
    "parameter_updates": ["date", "sid", "family", "version", "param", "old",
                          "new", "evidence"],
    "management_events": [
        "time", "book", "trade_id", "sid", "family", "action", "price",
        "units", "stop_stage", "reason"],
    "suspect_trades": [
        "time", "trade_id", "sid", "family", "exit_label", "gross_r", "net_r",
        "mfe_r", "mae_r", "suspect_reason"],
    "stop_watch": [
        "time", "trade_id", "sid", "family", "net_r", "target_hit_after_stop",
        "bars_watched"],
    "heartbeat": [
        "time", "research_day", "session", "regime", "htf_bias", "spread",
        "news", "shadow_open", "shadow_pending", "real_status", "stop_stage",
        "daily_pct", "weekly_dd_pct", "top_strategies"],
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
        candidates: List[str] = []
        if base:
            candidates.append(base)
        candidates.append(os.path.join(home, "Documents",
                                       "XAUUSD_Adaptive_Bot_V5"))
        candidates.append(os.path.join(home, "XAUUSD_Adaptive_Bot_V5"))
        candidates.append(os.path.join(os.getcwd(),
                                       "XAUUSD_Adaptive_Bot_V5_state"))
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
        self.log("PERSISTENCE: no writable directory — research state CANNOT "
                 "survive restarts. Fix folder permissions before starting "
                 "the research run.")
        return None

    def path_for(self, filename: str) -> Optional[str]:
        if not self.enabled:
            return None
        return os.path.join(self.directory, filename)

    # ----------------------------------------------------------- json state
    def load_state(self) -> Optional[dict]:
        if not self.enabled:
            return None
        path = os.path.join(self.directory, STATE_FILE)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError) as exc:
            self.log(f"PERSISTENCE: state file unreadable ({exc}) — starting "
                     f"fresh, and NOT deleting the old file")
            try:
                os.replace(path, path + ".corrupt")
            except OSError:
                pass
            return None
        if not isinstance(state, dict):
            self.log("PERSISTENCE: state file is not an object — ignored")
            return None
        schema = int(state.get("schema", 0))
        if schema != self.cfg.state_schema:
            self.log(f"PERSISTENCE: state schema {schema} does not match "
                     f"{self.cfg.state_schema} — the file is preserved and "
                     f"ignored rather than partially loaded")
            try:
                os.replace(path, f"{path}.schema{schema}")
            except OSError:
                pass
            return None
        return state

    def save_state(self, state: dict) -> None:
        if not self.enabled:
            return
        payload = dict(state)
        payload["schema"] = self.cfg.state_schema
        path = os.path.join(self.directory, STATE_FILE)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1, default=str)
            os.replace(tmp, path)              # atomic on macOS/POSIX
        except OSError as exc:
            self.log(f"PERSISTENCE: state save failed ({exc})")

    # ------------------------------------------------------------- csv trail
    def csv_append(self, name: str, row: dict) -> None:
        if not self.enabled:
            return
        fields = CSV_FIELDS.get(name)
        if fields is None:
            return
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

    def csv_rewrite(self, name: str, rows: List[dict]) -> None:
        """Replace a whole file — used for the rankings snapshot."""
        if not self.enabled:
            return
        fields = CSV_FIELDS.get(name)
        if fields is None:
            return
        path = os.path.join(self.directory, f"{name}.csv")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fields,
                                        extrasaction="ignore", restval="")
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
            os.replace(tmp, path)
        except OSError as exc:
            self.log(f"PERSISTENCE: csv rewrite of {name} failed ({exc})")

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

    def exists(self, filename: str) -> bool:
        if not self.enabled:
            return False
        return os.path.exists(os.path.join(self.directory, filename))
