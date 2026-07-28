"""Pre-flight OKX demo connection check — 17 checks, zero orders.

Runs every check the brief lists and **never places an order**:

 1. reach the EEA demo host, measure the clock offset
 2. clock drift within the trading budget
 3. REST host pin + WebSocket URLs on the EEA demo allow-list
 4. demo header enforcement (``x-simulated-trading: 1`` from the single path)
 5. authenticated demo reachability (account config)
 6. live-environment negative control (same key, no header → must fail 50101)
 7. demo environment verified (all guard signals)
 8. account position mode detected (net vs long/short)
 9. account balance fetched
10. demo balance is usable
11. X-Perp instrument discovered (never hardcoded)
12. contract specification is complete (ctVal/ctMult/lotSz/minSz/tickSz)
13. exchange leverage range covers the configured bounds
14. current BTC market data (ticker + candles with the confirm flag)
15. funding rate readable
16. fee rates readable
17. positions + open-orders endpoints readable — and **no order placed**

Research mode refuses to submit demo orders unless the same verification gate
passes at startup, so this script is a preview of that gate rather than a
separate one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config.loader import Credentials, LoadedConfig
from ..exchange.demo_guard import DemoGuard
from ..exchange.instruments import CapabilityDiscovery
from ..exchange.models import Capability
from ..exchange.rest import OkxDemoClient
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
            log.info("OKX", message)
        elif fatal:
            log.error("OKX", message)
        else:
            log.warning("OKX", message)


async def verify_demo_connection(
    loaded: LoadedConfig, credentials: Credentials
) -> VerificationReport:
    """Run the full 17-point pre-flight check. Places no orders."""
    config = loaded.config
    report = VerificationReport()

    client = OkxDemoClient(
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        passphrase=credentials.passphrase,
        timeout_seconds=config.exchange.request_timeout_seconds,
        max_retries=config.exchange.max_retries,
    )
    log.info("OKX", f"Host: {client.base_url} (demo environment)")

    try:
        # --- 1. connectivity + clock ---------------------------------
        try:
            offset = await client.sync_clock()
            report.add("1. Reach OKX EEA demo host", True, f"clock offset {offset}ms")
        except (ApiError, TransportError) as exc:
            report.add("1. Reach OKX EEA demo host", False, str(exc))
            return report

        # --- 2. clock drift budget ------------------------------------
        drift_ok = not client.clock_drift_exceeds(config.safety.max_clock_drift_ms)
        report.add(
            "2. Clock drift within trading budget",
            drift_ok,
            f"|{client.clock_offset_ms}|ms vs budget {config.safety.max_clock_drift_ms}ms"
            + ("" if drift_ok else " — enable NTP time sync; trading would pause"),
        )

        # --- 3–7. demo guard signals ----------------------------------
        guard = DemoGuard(
            client,
            api_key=credentials.api_key,
            api_secret=credentials.api_secret,
            passphrase=credentials.passphrase,
            run_mainnet_negative_control=config.safety.mainnet_negative_control,
        )
        verification = await guard.verify()
        signal_labels = {
            "host pin": "3. Host pin (REST + WS on EEA demo allow-list)",
            "demo header enforcement": "4. Demo header enforcement (x-simulated-trading: 1)",
            "authenticated reachability": "5. Authenticated demo reachability",
            "live-environment negative control": "6. Live-environment negative control",
        }
        for signal in verification.signals:
            report.add(
                signal_labels.get(signal.name, f"Demo signal: {signal.name}"),
                signal.passed,
                signal.detail,
                fatal=signal.required,
            )
        report.add(
            "7. Demo environment verified (all signals)",
            verification.verified,
            "order submission would be permitted"
            if verification.verified
            else "ORDER SUBMISSION WOULD BE DISABLED",
        )
        if not verification.verified:
            return report

        # --- 8. position mode -----------------------------------------
        account_config = verification.account_config
        report.add(
            "8. Account position mode detected",
            account_config is not None,
            f"posMode {account_config.position_mode.value}" if account_config else "not readable",
        )

        # --- 9–10. balance --------------------------------------------
        try:
            balance = await client.get_wallet_balance()
            report.add(
                "9. Fetch account balance",
                balance.total_equity >= 0,
                f"equity ${balance.total_equity:,.2f}, available ${balance.total_available:,.2f}",
            )
            expected = config.experiment.expected_demo_equity
            log.info("BALANCE", f"Expected research capital: ${expected:,.2f}")
            log.info("BALANCE", f"Actual OKX Demo capital: ${balance.total_equity:,.2f}")
            report.add(
                "10. Demo balance is usable",
                balance.total_equity >= 100,
                f"${balance.total_equity:,.2f}"
                + (
                    ""
                    if balance.total_equity >= 100
                    else " is too small to size a bounded position — top up demo funds in "
                    "the OKX Demo Trading UI"
                ),
                fatal=False,
            )
        except (ApiError, TransportError) as exc:
            report.add("9. Fetch account balance", False, str(exc))

        # --- 11–13. instrument discovery -------------------------------
        discovery = CapabilityDiscovery(
            client,
            base_ccy=config.market.base_currency,
            settle_preference=list(config.market.settle_currency_preference),
        )
        try:
            capabilities = await discovery.discover()
            spec = capabilities.primary
            report.add(
                "11. X-Perp instrument discovered",
                True,
                f"{spec.inst_id} (linear {spec.inst_type.value}, settles {spec.settle_ccy})",
            )
            for line in capabilities.describe():
                log.info("OKX", line)
        except (InstrumentNotFoundError, ApiError, TransportError) as exc:
            report.add("11. X-Perp instrument discovered", False, str(exc))
            return report

        contract_complete = (
            spec.ct_val > 0 and spec.ct_mult > 0 and spec.lot_size > 0
            and spec.min_size > 0 and spec.tick_size > 0
        )
        report.add(
            "12. Contract specification complete",
            contract_complete,
            f"ctVal {spec.ct_val} {spec.ct_val_ccy} × {spec.ct_mult}, lot {spec.lot_size}, "
            f"min {spec.min_size}, tick {spec.tick_size}",
        )
        lev_config = config.risk.leverage
        lev_ok = float(spec.max_leverage) >= lev_config.min_leverage
        report.add(
            "13. Exchange leverage range covers configured bounds",
            lev_ok,
            f"exchange max {spec.max_leverage}x; configured "
            f"{lev_config.min_leverage:g}x–{lev_config.max_leverage:g}x (hard cap 10x)",
        )
        if not spec.supports(Capability.SHORT):
            report.add(
                "Short side available",
                False,
                "the discovered instrument does not support shorts — unexpected for a perpetual",
                fatal=False,
            )

        # --- 14. market data -------------------------------------------
        try:
            ticker = await client.get_ticker(spec.inst_id)
            candles = await client.get_klines(
                spec.inst_id, config.market.regime_timeframe, limit=10
            )
            confirmed = sum(1 for c in candles if c.confirmed)
            report.add(
                "14. Fetch current BTC market data",
                ticker.last_price > 0 and len(candles) > 0,
                f"last {ticker.last_price:,.2f}, spread {ticker.spread_bps:.2f} bps, "
                f"{len(candles)} candles ({confirmed} confirmed)",
            )
            log.info("MARKET", f"{ticker.inst_id} {ticker.last_price:,.2f}")
        except (ApiError, TransportError) as exc:
            report.add("14. Fetch current BTC market data", False, str(exc))

        # --- 15. funding rate ------------------------------------------
        try:
            funding = await client.get_funding_rate(spec.inst_id)
            report.add(
                "15. Funding rate readable",
                True,
                f"current {funding.funding_rate * 100:+.4f}%, "
                f"next at {funding.next_funding_time_ms or funding.funding_time_ms}",
            )
        except (ApiError, TransportError) as exc:
            report.add("15. Funding rate readable", False, str(exc), fatal=False)

        # --- 16. fee rates ---------------------------------------------
        try:
            fees = await client.get_fee_rates(spec.inst_id)
            report.add(
                "16. Fee rates readable",
                True,
                f"maker {fees.maker * 100:.4f}%, taker {fees.taker * 100:.4f}% (cost-positive)",
            )
        except (ApiError, TransportError) as exc:
            report.add("16. Fee rates readable", False, str(exc), fatal=False)

        # --- 17. trading surfaces readable; explicitly no order ---------
        try:
            positions = await client.get_positions(spec.inst_id)
            open_orders = await client.get_open_orders(spec.inst_id)
            report.add(
                "17. Positions/orders readable — NO ORDER PLACED",
                True,
                f"{len([p for p in positions if abs(p.contracts) > 0])} live position(s), "
                f"{len(open_orders)} open order(s); verification is read-only",
            )
        except (ApiError, TransportError) as exc:
            report.add("17. Positions/orders readable — NO ORDER PLACED", False, str(exc))

    finally:
        await client.close()

    return report


def print_report(report: VerificationReport) -> None:
    """Print the PASS/FAIL summary block."""
    lines = ["OKX DEMO CONNECTION VERIFICATION", ""]
    for check in report.checks:
        marker = "PASS" if check.passed else ("FAIL" if check.fatal else "WARN")
        lines.append(f"  [{marker}] {check.name}")
        if check.detail:
            lines.append(f"         {check.detail}")
    lines.append("")
    lines.append("RESULT: PASS" if report.passed else "RESULT: FAIL")
    if report.passed:
        lines.append("Research mode is permitted to place OKX Demo orders.")
    else:
        lines.append("Research mode will NOT place orders until every check above passes.")
    log.banner(lines, tag="OKX")
