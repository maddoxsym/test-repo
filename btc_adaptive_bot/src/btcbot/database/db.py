"""SQLite access layer.

Engineered for a long-running single-machine experiment:

* **WAL journal** so the dashboard can read while the engine writes.
* **A single write lock** — SQLite allows one writer; serialising in-process
  avoids ``database is locked`` storms under concurrency.
* **Explicit transactions** via :meth:`Database.transaction`.
* **Online backup** using SQLite's own backup API, which is consistent even
  while the engine is mid-write (a file copy is not).

Async callers use :meth:`Database.run` to push work to a worker thread rather
than blocking the event loop.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, TypeVar

from ..utils.errors import DatabaseError
from ..utils.logging import get_logger
from ..utils.timeutil import now_utc

log = get_logger(__name__)

T = TypeVar("T")


class Database:
    """Thread-safe SQLite wrapper."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=busy_timeout_ms / 1000.0,
            check_same_thread=False,
            isolation_level=None,  # explicit transaction control
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()

    def _configure(self) -> None:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            cursor.execute("PRAGMA foreign_keys=ON")
            # NORMAL is the right trade-off with WAL: durable across process
            # crashes (which is what restart recovery must survive), and only at
            # risk from an OS-level crash.
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.close()

    # --- core operations -------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            try:
                return self._conn.execute(sql, params)
            except sqlite3.Error as exc:
                raise DatabaseError(f"query failed: {exc}\nSQL: {sql.strip()[:400]}") from exc

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            try:
                self._conn.executemany(sql, rows)
            except sqlite3.Error as exc:
                raise DatabaseError(f"bulk query failed: {exc}\nSQL: {sql.strip()[:400]}") from exc

    def executescript(self, script: str) -> None:
        """Run a multi-statement SQL script.

        Note that SQLite's ``executescript`` issues an implicit COMMIT before it
        runs, so this **cannot** be nested inside :meth:`transaction`. Callers
        needing safe replay must make their script idempotent instead; the
        migrations do exactly that with ``IF NOT EXISTS``.
        """
        with self._lock:
            try:
                self._conn.executescript(script)
            except sqlite3.Error as exc:
                raise DatabaseError(f"script failed: {exc}") from exc

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    def query_one(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block atomically; roll back on any exception."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise DatabaseError(f"could not begin transaction: {exc}") from exc
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    async def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Execute a blocking database call off the event loop."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # --- maintenance -----------------------------------------------------

    def backup(self, backup_dir: str | Path) -> Path:
        """Consistent online backup, safe to run while the engine is writing."""
        directory = Path(backup_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
        target = directory / f"btcbot-{stamp}.db"
        # Two backups inside the same second must not overwrite each other —
        # silently losing a backup is the opposite of what this method is for.
        if target.exists():
            for suffix in range(1, 1000):
                candidate = directory / f"btcbot-{stamp}-{suffix:03d}.db"
                if not candidate.exists():
                    target = candidate
                    break
        with self._lock:
            try:
                with sqlite3.connect(target) as destination:
                    self._conn.backup(destination)
            except sqlite3.Error as exc:
                raise DatabaseError(f"backup failed: {exc}") from exc
        log.info("DB", f"Backup written: {target.name}", path=str(target))
        return target

    def prune_backups(self, backup_dir: str | Path, *, keep: int = 20) -> int:
        """Delete all but the ``keep`` most recent backups; returns count removed."""
        directory = Path(backup_dir)
        if not directory.exists():
            return 0
        backups = sorted(directory.glob("btcbot-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        removed = 0
        for stale in backups[keep:]:
            try:
                stale.unlink()
                removed += 1
            except OSError as exc:
                log.warning("DB", f"Could not remove old backup {stale.name}: {exc}")
        return removed

    def restore_from(self, backup_path: str | Path) -> None:
        """Replace the live database with a backup. The engine must not be running."""
        source = Path(backup_path)
        if not source.exists():
            raise DatabaseError(f"backup not found: {source}")
        with self._lock:
            self._conn.close()
            shutil.copy2(source, self.path)
            self._conn = sqlite3.connect(
                self.path,
                timeout=self._busy_timeout_ms / 1000.0,
                check_same_thread=False,
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            self._configure()

    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute("VACUUM")

    def integrity_check(self) -> bool:
        """True when SQLite reports the file structurally sound."""
        row = self.query_one("PRAGMA integrity_check")
        return bool(row) and row[0] == "ok"

    def close(self) -> None:
        with self._lock:
            # Optimisation is best-effort; never block shutdown on it.
            with suppress(sqlite3.Error):
                self._conn.execute("PRAGMA optimize")
            self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
