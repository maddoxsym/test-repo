"""CSV export for independent analysis.

Everything the system records can be exported to plain CSV so results can be
re-analysed in a spreadsheet, R, or pandas without this codebase. That is what
makes the experiment auditable by someone who does not trust it.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from ..database.repositories import Repositories
from ..utils.logging import get_logger
from ..utils.timeutil import now_utc

log = get_logger(__name__)

EXPORTS = (
    "signals",
    "shadow_trades",
    "demo_orders",
    "demo_fills",
    "positions",
    "strategy_metrics",
    "daily_metrics",
    "news",
    "champion_rankings",
    "regimes",
    "backtests",
)


class CsvExporter:
    """Writes each research entity to its own CSV file."""

    def __init__(self, repositories: Repositories, output_dir: str | Path) -> None:
        self.repos = repositories
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_all(self, experiment_id: str | None = None) -> list[Path]:
        """Export every entity; returns the files written."""
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        written: list[Path] = []
        for name in EXPORTS:
            try:
                rows = self._fetch(name, experiment_id)
            except Exception as exc:  # noqa: BLE001 - one bad export must not stop the rest
                log.warning("REPORT", f"Export '{name}' failed: {type(exc).__name__}: {exc}")
                continue
            path = self.output_dir / f"{name}_{stamp}.csv"
            if self._write(path, rows):
                written.append(path)
        log.info("REPORT", f"Exported {len(written)} CSV file(s) to {self.output_dir}")
        return written

    def export(self, name: str, experiment_id: str | None = None) -> Path | None:
        if name not in EXPORTS:
            raise ValueError(f"unknown export {name!r}; valid: {', '.join(EXPORTS)}")
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        path = self.output_dir / f"{name}_{stamp}.csv"
        return path if self._write(path, self._fetch(name, experiment_id)) else None

    def _fetch(self, name: str, experiment_id: str | None) -> list[dict[str, Any]]:
        db = self.repos.db
        clause = "WHERE experiment_id = ?" if experiment_id else ""
        params = (experiment_id,) if experiment_id else ()

        queries: dict[str, tuple[str, tuple[Any, ...]]] = {
            "signals": (f"SELECT * FROM signals {clause} ORDER BY ts_utc", params),
            "shadow_trades": (f"SELECT * FROM shadow_trades {clause} ORDER BY entry_ts_utc", params),
            "demo_orders": (f"SELECT * FROM demo_orders {clause} ORDER BY submitted_ts_utc", params),
            "demo_fills": (f"SELECT * FROM demo_fills {clause} ORDER BY exec_ts_utc", params),
            "positions": (f"SELECT * FROM positions {clause} ORDER BY opened_ts_utc", params),
            "strategy_metrics": (
                f"SELECT * FROM performance_snapshots {clause} ORDER BY ts_utc",
                params,
            ),
            "daily_metrics": (
                f"""
                SELECT day_index, strategy_id, layer, ts_utc, score
                FROM performance_snapshots {clause} ORDER BY day_index, strategy_id
                """,
                params,
            ),
            "news": ("SELECT * FROM news_events ORDER BY received_ts_utc", ()),
            "champion_rankings": (
                f"SELECT * FROM champion_history {clause} ORDER BY ts_utc DESC", params
            ),
            "regimes": ("SELECT * FROM market_regimes ORDER BY bar_open_ms DESC LIMIT 20000", ()),
            "backtests": (f"SELECT * FROM backtest_results {clause} ORDER BY run_ts_utc", params),
        }
        sql, sql_params = queries[name]
        return [dict(row) for row in db.query(sql, sql_params)]

    @staticmethod
    def _write(path: Path, rows: list[dict[str, Any]]) -> bool:
        if not rows:
            # Still write a header-less placeholder so the operator can see the
            # export ran and simply had no data.
            path.write_text("# no rows\n", encoding="utf-8")
            return True
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return True


def write_metrics_csv(path: str | Path, rankings: list[dict[str, Any]]) -> Path:
    """Write the final ranking table used by the Day-14 report."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not rankings:
        target.write_text("# no strategies ranked\n", encoding="utf-8")
        return target
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rankings[0].keys()))
        writer.writeheader()
        writer.writerows(rankings)
    return target
