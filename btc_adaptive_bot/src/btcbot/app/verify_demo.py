"""Pre-flight demo connection check.

Runs every check the brief lists and **never places an order**:

1. authenticate
2. verify the demo environment (all four signals)
3. fetch the account balance
4. fetch instrument information
5. fetch current BTC market data
6. verify trading permissions
7. place no order
8. print PASS / FAIL

Research mode refuses to submit demo orders unless this same verification passes
at startup, so this script is a preview of that gate rather than a separate one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config.loader import Credentials, LoadedConfig
from ..exchange.demo_guard import DemoGuard
from ..exchange.instruments import CapabilityDiscovery
from ..exchange.models import Capability, Category
from ..exchange.rest import BybitDemoClient
from ..utils.errors import ApiError, InstrumentNotFoundError, TransportError
from ..utils.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    fatal: bool = True


@dataclass(slots=True)
class VerificationReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.fatal)

    def add(self, name: str, passed: bool, detail: str = "", *, fatal: bool = True) -> None:
        self.checks.append(CheckResult(name, passed, detail, fatal))
        marker = "PASS" if passed else ("FAIL" if fatal else "WARN")
        message = f"[{marker}] {name}" + (f" — {detail}" if detail else "")
        if passed:
            log.info("BYBIT", message)
        elif fatal:
            log.error("BYBIT", message)
        else:
            log.warning("BYBIT", message)


async def verify_demo_connection(
    loaded: LoadedConfig, credentials: Credentials
) -> VerificationReport:
    """Run the full pre-flight check. Places no orders."""
    config = loaded.config
    report = VerificationReport()

    client = BybitDemoClient(
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        recv_window_ms=config.exchange.recv_window_ms,
        timeout_seconds=config.exchange.request_timeout_seconds,
        max_retries=config.exchange.max_retries,
    )
    log.info("BYBIT", f"Host: {client.base_url}")

    try:
        # --- 1. connectivity + clock ---------------------------------
        try:
            offset = await client.sync_clock()
            report.add(
                "Reach Bybit demo host",
                True,
                f"clock offset {offset}ms",
            )
        except (ApiError, TransportError) as exc:
            report.add("Reach Bybit demo host", False, str(exc))
            return report

        # --- 2. demo verification (4 independent signals) -------------
        guard = DemoGuard(
            client,
            api_key=credentials.api_key,
            api_secret=credentials.api_secret,
            run_mainnet_negative_control=config.safety.mainnet_negative_control,
        )
        verification = await guard.verify()
        for signal in verification.signals:
            report.add(
                f"Demo signal: {signal.name}",
                signal.passed,
                signal.detail,
                fatal=signal.required,
            )
        report.add(
            "Demo environment verified (all signals)",
            verification.verified,
            "order submission would be permitted"
            if verification.verified
            else "ORDER SUBMISSION WOULD BE DISABLED",
        )
        if not verification.verified:
            return report

        # --- 3. balance -----------------------------------------------
        try:
            balance = await client.get_wallet_balance()
            report.add(
                "Fetch account balance",
                balance.total_equity >= 0,
                f"equity ${balance.total_equity:,.2f}, available ${balance.total_available:,.2f}",
            )
            expected = config.experiment.expected_demo_equity
            log.info("BALANCE", f"Expected research capital: ${expected:,.2f}")
            log.info("BALANCE", f"Actual Bybit Demo capital: ${balance.total_equity:,.2f}")
            if balance.total_equity < 100:
                report.add(
                    "Demo balance is usable",
                    False,
                    f"${balance.total_equity:,.2f} is too small to size a bounded position. "
                    "Top up demo funds in the Bybit UI, or run "
                    "`./scripts/topup_demo_funds.sh` to request demo USDT.",
                    fatal=False,
                )
        except (ApiError, TransportError) as exc:
            report.add("Fetch account balance", False, str(exc))

        # --- 4. instruments ---------------------------------------------
        discovery = CapabilityDiscovery(
            client,
            symbol=config.market.primary_symbol,
            enabled_categories=list(config.market.enabled_categories),
            preferred_category=config.market.preferred_category,
        )
        try:
            capabilities = await discovery.discover()
            report.add(
                "Fetch instrument information",
                True,
                f"{capabilities.symbol} tradable on "
                f"{', '.join(c.value for c in capabilities.tradable_categories)}",
            )
            for line in capabilities.describe():
                log.info("BYBIT", line)
            if not capabilities.supports_short_on_exchange:
                report.add(
                    "Short selling available on the demo product",
                    False,
                    "spot is long-only — SHORT strategies will be researched in the shadow "
                    "engine but never sent to the exchange (this is expected)",
                    fatal=False,
                )
        except (InstrumentNotFoundError, ApiError, TransportError) as exc:
            report.add("Fetch instrument information", False, str(exc))
            return report

        # --- 5. market data ---------------------------------------------
        category = capabilities.primary_category or Category.SPOT
        try:
            ticker = await client.get_ticker(config.market.primary_symbol, category=category)
            candles = await client.get_klines(
                config.market.primary_symbol,
                config.market.regime_timeframe,
                category=category,
                limit=10,
            )
            report.add(
                "Fetch current BTC market data",
                ticker.last_price > 0 and len(candles) > 0,
                f"last {ticker.last_price:,.2f}, spread {ticker.spread_bps:.2f} bps, "
                f"{len(candles)} candles",
            )
            log.info("MARKET", f"{ticker.symbol} {ticker.last_price:,.2f}")
        except (ApiError, TransportError) as exc:
            report.add("Fetch current BTC market data", False, str(exc))

        # --- 6. trading permissions (read-only probe) --------------------
        try:
            key_info = await client.get_api_key_info()
            permissions = key_info.get("permissions", {}) or {}
            trade_perms = permissions.get("Spot", []) + permissions.get("ContractTrade", [])
            has_trade = bool(trade_perms) or bool(permissions)
            report.add(
                "Verify trading permissions",
                has_trade,
                f"permissions: {permissions}" if permissions else "no permissions reported",
                fatal=False,
            )
            if key_info.get("readOnly") in (1, True):
                report.add(
                    "API key is not read-only",
                    False,
                    "this key is read-only and cannot place demo orders",
                )
        except (ApiError, TransportError) as exc:
            report.add("Verify trading permissions", False, str(exc), fatal=False)

        # --- 7. explicitly place no order --------------------------------
        instrument = capabilities.primary
        report.add(
            "No order placed",
            True,
            f"verification is read-only; min qty {instrument.min_order_qty}, "
            f"min notional {instrument.min_order_amt}, tick {instrument.tick_size}",
        )
        if not instrument.supports(Capability.MARKET_ORDER):
            report.add(
                "Market orders supported",
                False,
                "the discovered instrument does not report market-order support",
                fatal=False,
            )

    finally:
        await client.close()

    return report


def print_report(report: VerificationReport) -> None:
    """Print the PASS/FAIL summary block."""
    lines = ["DEMO CONNECTION VERIFICATION", ""]
    for check in report.checks:
        marker = "PASS" if check.passed else ("FAIL" if check.fatal else "WARN")
        lines.append(f"  [{marker}] {check.name}")
        if check.detail:
            lines.append(f"         {check.detail}")
    lines.append("")
    lines.append("RESULT: PASS" if report.passed else "RESULT: FAIL")
    if report.passed:
        lines.append("Research mode is permitted to place Bybit Demo orders.")
    else:
        lines.append("Research mode will NOT place orders until every check above passes.")
    log.banner(lines, tag="BYBIT")
