"""Command-line entry points.

    btcbot verify      # pre-flight demo check (17 checks), places no order
    btcbot research    # start/resume the 14-day experiment
    btcbot champion    # run champion mode on the same demo account
    btcbot dry-run     # real public data, no authenticated orders, no timer
    btcbot status      # print current experiment status
    btcbot report      # regenerate reports
    btcbot export      # export every entity to CSV
    btcbot backup      # back up the database

There is deliberately **no** flag anywhere here that enables real-money
trading, selects a different host, disables the demo header, or bypasses
demo verification. (OKX demo funds are topped up in the OKX Demo Trading
web UI — the API exposes no funds endpoint, so none exists here.)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

from ..config.loader import LoadedConfig, load_config, load_credentials
from ..dashboard.server import DashboardServer
from ..database.db import Database
from ..database.migrations import run_migrations
from ..database.repositories import Repositories
from ..reporting.export import CsvExporter
from ..reporting.reports import ReportGenerator
from ..utils.errors import BtcBotError, ConfigError, CredentialsMissingError
from ..utils.logging import get_logger, setup_logging
from ..utils.timeutil import humanize_duration, iso, now_utc, parse_iso
from ..version import __version__, git_commit
from .experiment import ExperimentMode
from .orchestrator import Orchestrator

log = get_logger(__name__)


def _configure_logging(loaded: LoadedConfig, *, override_level: str | None = None) -> None:
    logging_config = loaded.config.logging
    setup_logging(
        level=override_level or logging_config.level,
        console=logging_config.console,
        file_path=logging_config.file,
        json_path=logging_config.json_file,
        max_bytes=logging_config.max_bytes,
        backup_count=logging_config.backup_count,
    )


def _install_signal_handlers(orchestrator: Orchestrator) -> None:
    """Ctrl+C and SIGTERM trigger a graceful, state-saving shutdown."""
    loop = asyncio.get_running_loop()

    def handler() -> None:
        orchestrator.request_shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, handler)


# ---------------------------------------------------------------- commands


async def cmd_verify(args: argparse.Namespace) -> int:
    from .verify_demo import print_report, verify_demo_connection

    loaded = load_config(args.config)
    _configure_logging(loaded, override_level=args.log_level)
    credentials = load_credentials(required=True)
    assert credentials is not None

    report = await verify_demo_connection(loaded, credentials)
    print_report(report)
    return 0 if report.passed else 1


async def cmd_smoke_test(args: argparse.Namespace) -> int:
    """Minimum-size demo round trip. Does NOT start the 14-day timer."""
    from .smoke_test import print_report, run_smoke_test

    if not args.confirm_demo:
        log.error(
            "SMOKE",
            "This command submits a real (demo) order. Re-run it with --confirm-demo.",
        )
        return 2

    loaded = load_config(args.config)
    _configure_logging(loaded, override_level=args.log_level)
    credentials = load_credentials(required=True)
    assert credentials is not None

    report = await run_smoke_test(loaded, credentials)
    print_report(report)
    return 0 if report.passed else 1


async def _run_engine(args: argparse.Namespace, *, mode: str, dry_run: bool) -> int:
    loaded = load_config(args.config)
    _configure_logging(loaded, override_level=args.log_level)

    credentials = load_credentials(required=not dry_run)
    orchestrator = Orchestrator(loaded, credentials, mode=mode, dry_run=dry_run)
    _install_signal_handlers(orchestrator)

    dashboard: DashboardServer | None = None
    if loaded.config.dashboard.enabled and not args.no_dashboard:
        dashboard = DashboardServer(loaded.config.dashboard, orchestrator.dashboard_state)

    try:
        if dashboard is not None:
            await dashboard.start()
        await orchestrator.start()
        return 0
    except CredentialsMissingError as exc:
        log.error("CONFIG", str(exc))
        return 2
    except BtcBotError as exc:
        log.error("SAFETY", f"{type(exc).__name__}: {exc}")
        return 1
    except asyncio.CancelledError:
        return 0
    finally:
        if dashboard is not None:
            await dashboard.stop()
        await orchestrator.shutdown()


async def cmd_research(args: argparse.Namespace) -> int:
    return await _run_engine(args, mode=ExperimentMode.RESEARCH, dry_run=False)


async def cmd_champion(args: argparse.Namespace) -> int:
    return await _run_engine(args, mode=ExperimentMode.CHAMPION, dry_run=False)


async def cmd_dry_run(args: argparse.Namespace) -> int:
    return await _run_engine(args, mode=ExperimentMode.RESEARCH, dry_run=True)


async def cmd_status(args: argparse.Namespace) -> int:
    loaded = load_config(args.config)
    _configure_logging(loaded, override_level=args.log_level)

    with Database(loaded.config.database.path) as db:
        run_migrations(db)
        repos = Repositories(db)
        experiment = repos.experiments.find_active() or repos.experiments.find_latest_complete()
        if experiment is None:
            log.info("EXPERIMENT", "No experiment found. Start one with ./scripts/run_research.sh")
            return 0

        start = parse_iso(experiment["start_ts_utc"])
        end = parse_iso(experiment["scheduled_end_ts_utc"])
        elapsed = now_utc() - start
        remaining = end - now_utc()
        day = min(int(experiment["duration_days"]), int(elapsed.total_seconds() // 86400) + 1)

        balance = repos.market.latest_balance(experiment["experiment_id"])
        shadow_count = repos.shadow.count(experiment["experiment_id"])
        demo_orders = repos.demo_orders.recent(1000, experiment_id=experiment["experiment_id"])
        champion = repos.champion.current(experiment["experiment_id"])
        outages = repos.system.outages(experiment["experiment_id"])

        lines = [
            f"Experiment      {experiment['experiment_id']}",
            f"Name            {experiment['name']} ({experiment['mode']})",
            f"Status          {experiment['status']}",
            "",
            f"DAY {day} / {experiment['duration_days']}",
            f"TIME ELAPSED    {humanize_duration(elapsed)}",
            f"TIME REMAINING  {humanize_duration(remaining)}",
            f"START TIME      {iso(start)}",
            f"END TIME        {iso(end)}",
            "",
            f"Strategies      {len(experiment['enabled_strategies'])}",
            f"Shadow trades   {shadow_count}",
            f"Demo orders     {len(demo_orders)}",
            f"Outages         {len(outages)}",
        ]
        research = repos.research_equity.latest(experiment["experiment_id"])
        if research:
            lines.append(
                f"Research equity ${float(research['current_equity']):,.2f} "
                f"(start ${float(research['starting_equity']):,.2f}, "
                f"cap ${float(research['cap_usdt']):,.2f})"
            )
        else:
            lines.append(
                "Research equity "
                f"${float(experiment['starting_research_equity_usdt'] or 0.0):,.2f} (start)"
            )
        if balance:
            # The whole account, including assets the experiment never touches.
            lines.append(
                f"OKX total equity ${float(balance['total_equity']):,.2f} (all assets, unused)"
            )
        if champion:
            lines.extend(
                [
                    "",
                    f"CHAMPION        {champion['new_champion']} ({champion['champion_type']})",
                    f"Confidence      {champion['confidence']}",
                ]
            )
        log.banner(lines, tag="EXPERIMENT")
    return 0


async def cmd_report(args: argparse.Namespace) -> int:
    loaded = load_config(args.config)
    _configure_logging(loaded, override_level=args.log_level)

    with Database(loaded.config.database.path) as db:
        run_migrations(db)
        repos = Repositories(db)
        experiment = repos.experiments.find_active() or repos.experiments.find_latest_complete()
        if experiment is None:
            log.error("REPORT", "No experiment found — nothing to report on.")
            return 1

        from ..strategies.registry import StrategyRegistry

        registry = StrategyRegistry.build(
            loaded.config.strategies, available_timeframes=list(loaded.config.market.timeframes)
        )
        generator = ReportGenerator(repos, loaded.config, registry=registry)
        experiment_id = experiment["experiment_id"]
        paths = [
            generator.strategy_report(experiment_id),
            generator.execution_report(experiment_id),
            generator.learning_report(experiment_id),
        ]
        for path in paths:
            log.info("REPORT", f"Written: {path}")
    return 0


async def cmd_export(args: argparse.Namespace) -> int:
    loaded = load_config(args.config)
    _configure_logging(loaded, override_level=args.log_level)

    with Database(loaded.config.database.path) as db:
        run_migrations(db)
        repos = Repositories(db)
        experiment = repos.experiments.find_active() or repos.experiments.find_latest_complete()
        exporter = CsvExporter(repos, loaded.config.reporting.export_dir)
        written = exporter.export_all(experiment["experiment_id"] if experiment else None)
        for path in written:
            log.info("REPORT", f"Exported: {path}")
    return 0


async def cmd_backup(args: argparse.Namespace) -> int:
    loaded = load_config(args.config)
    _configure_logging(loaded, override_level=args.log_level)

    with Database(loaded.config.database.path) as db:
        if not db.integrity_check():
            log.error("DB", "Integrity check FAILED — backing up anyway, but investigate.")
        path = db.backup(loaded.config.database.backup_dir)
        removed = db.prune_backups(loaded.config.database.backup_dir)
        log.info("DB", f"Backup complete: {path} ({removed} old backup(s) pruned)")
    return 0


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btcbot",
        description=(
            "Adaptive BTC trading research system — OKX Demo only "
            "(BTC X-Perp). This build has no real-money trading mode."
        ),
    )
    parser.add_argument("--version", action="version", version=f"btcbot {__version__} ({git_commit()})")
    parser.add_argument("-c", "--config", default=None, help="path to a config YAML file")
    parser.add_argument(
        "--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    parser.add_argument("--no-dashboard", action="store_true", help="do not start the dashboard")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="verify the OKX demo connection (17 checks, places no order)")
    sub.add_parser("research", help="start or resume the 14-day demo research experiment")
    sub.add_parser("champion", help="run champion mode on the same demo account")
    sub.add_parser("dry-run", help="real public data, no authenticated orders, no timer")
    sub.add_parser("status", help="print the current experiment status")
    sub.add_parser("report", help="regenerate strategy/execution/learning reports")
    sub.add_parser("export", help="export all research data to CSV")
    sub.add_parser("backup", help="back up the database")

    smoke = sub.add_parser(
        "smoke-test",
        help="minimum-size demo round trip (places ONE order; no 14-day timer)",
    )
    smoke.add_argument(
        "--confirm-demo",
        action="store_true",
        dest="confirm_demo",
        help="required: acknowledges that a real demo order will be submitted",
    )

    return parser


COMMANDS = {
    "verify": cmd_verify,
    "smoke-test": cmd_smoke_test,
    "research": cmd_research,
    "champion": cmd_champion,
    "dry-run": cmd_dry_run,
    "status": cmd_status,
    "report": cmd_report,
    "export": cmd_export,
    "backup": cmd_backup,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS[args.command]

    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        print("\nInterrupted — state was saved before exit.", file=sys.stderr)
        return 130
    except ConfigError as exc:
        print(f"\nConfiguration error:\n{exc}", file=sys.stderr)
        return 2
    except CredentialsMissingError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    except BtcBotError as exc:
        print(f"\n{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
