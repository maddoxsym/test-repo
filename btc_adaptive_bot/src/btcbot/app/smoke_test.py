"""Minimum-size OKX Demo round trip.

The one place in this project that submits an order outside a running
experiment. It exists to prove — with your own credentials, on your own demo
account — that the full order path works end to end before you commit fourteen
days to it.

What it does, in order:

1. Verify the demo environment (all four safety-lock signals).
2. Discover the BTC X-Perp and read its contract specification.
3. Choose the **minimum safe leverage** (1x by default, never above 2x).
4. Set the leverage and read it back to confirm it applied.
5. Submit the **minimum valid size** — ``minSz`` contracts, nothing larger.
6. Confirm the fill.
7. Read the resulting position, including its liquidation price.
8. Close it with a reduce-only order.
9. Confirm the account is flat again.
10. Print every order and fill ID, then PASS or FAIL.

It never starts the 14-day timer: no experiment row is created, and the
experiment manager is not involved at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..config.loader import Credentials, LoadedConfig
from ..exchange.demo_guard import DemoGuard
from ..exchange.instruments import CapabilityDiscovery
from ..exchange.models import (
    OrderRequest,
    OrderType,
    PositionMode,
    PosSide,
    Side,
    TdMode,
)
from ..exchange.rest import OkxDemoClient
from ..utils.errors import ApiError, InstrumentNotFoundError, TransportError
from ..utils.ids import new_uuid
from ..utils.logging import get_logger
from ..utils.numeric import format_qty

log = get_logger(__name__)

# The smoke test is deliberately capped far below the research engine's range.
MAX_SMOKE_LEVERAGE = 2.0
DEFAULT_SMOKE_LEVERAGE = 1.0
# How long to wait for the position to appear/disappear after an order.
SETTLE_TIMEOUT_SECONDS = 20.0
SETTLE_POLL_SECONDS = 1.0


@dataclass(slots=True)
class SmokeStep:
    name: str
    passed: bool
    detail: str = ""


@dataclass(slots=True)
class SmokeReport:
    steps: list[SmokeStep] = field(default_factory=list)
    order_ids: dict[str, str] = field(default_factory=dict)
    fill_ids: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.steps) and all(step.passed for step in self.steps)

    def add(self, name: str, passed: bool, detail: str = "") -> bool:
        self.steps.append(SmokeStep(name, passed, detail))
        marker = "PASS" if passed else "FAIL"
        message = f"[{marker}] {name}" + (f" — {detail}" if detail else "")
        if passed:
            log.info("SMOKE", message)
        else:
            log.error("SMOKE", message)
        return passed


async def _wait_for_position(
    client: OkxDemoClient, inst_id: str, *, want_open: bool
) -> Any | None:
    """Poll until a position exists (or is gone), or the timeout elapses."""
    waited = 0.0
    while waited < SETTLE_TIMEOUT_SECONDS:
        try:
            positions = await client.get_positions(inst_id)
        except (ApiError, TransportError):
            positions = []
        live = [p for p in positions if abs(p.contracts) > 0]
        if want_open and live:
            return live[0]
        if not want_open and not live:
            return None
        await asyncio.sleep(SETTLE_POLL_SECONDS)
        waited += SETTLE_POLL_SECONDS
    return live[0] if (want_open and live) else None


async def run_smoke_test(loaded: LoadedConfig, credentials: Credentials) -> SmokeReport:
    """Run the full round trip. Returns a report; never raises on trade failure."""
    config = loaded.config
    report = SmokeReport()

    client = OkxDemoClient(
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
        passphrase=credentials.passphrase,
        timeout_seconds=config.exchange.request_timeout_seconds,
        max_retries=config.exchange.max_retries,
    )

    try:
        # --- 1. demo safety lock -------------------------------------
        try:
            await client.sync_clock()
        except (ApiError, TransportError) as exc:
            report.add("Reach OKX EEA demo host", False, str(exc))
            return report
        report.add("Reach OKX EEA demo host", True, f"clock offset {client.clock_offset_ms}ms")

        guard = DemoGuard(
            client,
            api_key=credentials.api_key,
            api_secret=credentials.api_secret,
            passphrase=credentials.passphrase,
            run_mainnet_negative_control=config.safety.mainnet_negative_control,
        )
        verification = await guard.verify()
        if not report.add(
            "Demo environment verified",
            verification.verified,
            "all safety-lock signals passed"
            if verification.verified
            else "; ".join(f.detail for f in verification.failures),
        ):
            return report

        position_mode = (
            verification.account_config.position_mode
            if verification.account_config
            else PositionMode.NET
        )
        report.add("Position mode detected", True, position_mode.value)

        # --- 2. instrument discovery ---------------------------------
        discovery = CapabilityDiscovery(
            client,
            base_ccy=config.market.base_currency,
            settle_preference=list(config.market.settle_currency_preference),
        )
        try:
            capabilities = await discovery.discover()
        except (InstrumentNotFoundError, ApiError, TransportError) as exc:
            report.add("Discover BTC X-Perp", False, str(exc))
            return report
        spec = capabilities.primary
        report.add(
            "Discover BTC X-Perp",
            True,
            f"{spec.inst_id} · ctVal {spec.ct_val} {spec.ct_val_ccy} · minSz {spec.min_size}",
        )

        # --- 3–4. set and confirm the minimum safe leverage ------------
        leverage = min(
            DEFAULT_SMOKE_LEVERAGE, MAX_SMOKE_LEVERAGE, float(spec.max_leverage)
        )
        leverage_str = f"{leverage:g}"
        pos_side_arg = None if position_mode is PositionMode.NET else "long"
        try:
            await client.set_leverage(
                spec.inst_id,
                leverage_str,
                mgn_mode=TdMode.ISOLATED.value,
                pos_side=pos_side_arg,
            )
        except (ApiError, TransportError) as exc:
            report.add("Set leverage", False, f"{leverage_str}x rejected: {exc}")
            return report

        try:
            infos = await client.get_leverage_info(
                spec.inst_id, mgn_mode=TdMode.ISOLATED.value
            )
        except (ApiError, TransportError) as exc:
            report.add("Confirm leverage", False, str(exc))
            return report
        confirmed = any(abs(float(i.leverage) - leverage) < 1e-6 for i in infos)
        if not report.add(
            "Set and confirm leverage",
            confirmed,
            f"{leverage_str}x isolated confirmed"
            if confirmed
            else f"exchange reports {[str(i.leverage) for i in infos]}, expected {leverage_str}x",
        ):
            return report

        # --- 5. submit the minimum valid size --------------------------
        size = format_qty(spec.min_size, spec.lot_size)
        entry_id = f"smoke{new_uuid().replace('-', '')[:24]}"
        entry_side, entry_pos_side = (
            (Side.BUY, None)
            if position_mode is PositionMode.NET
            else (Side.BUY, PosSide.LONG)
        )
        entry = OrderRequest(
            inst_id=spec.inst_id,
            td_mode=TdMode.ISOLATED,
            side=entry_side,
            order_type=OrderType.MARKET,
            sz=size,
            client_order_id=entry_id,
            pos_side=entry_pos_side,
        )
        try:
            result = await client.place_order(entry)
        except (ApiError, TransportError) as exc:
            report.add("Place minimum-size demo order", False, str(exc))
            return report
        report.order_ids["entry"] = result.exchange_order_id
        report.order_ids["entry_client"] = result.client_order_id
        report.add(
            "Place minimum-size demo order",
            True,
            f"{size} contract(s) LONG · ordId {result.exchange_order_id}",
        )

        # --- 6–7. confirm the fill and read the position ---------------
        position = await _wait_for_position(client, spec.inst_id, want_open=True)
        if not report.add(
            "Position opened and confirmed",
            position is not None,
            (
                f"{position.contracts:g} contract(s) @ {position.avg_price:,.2f}, "
                f"liq {position.liq_price:,.2f}" if position and position.liq_price
                else f"{position.contracts:g} contract(s) @ {position.avg_price:,.2f}"
                if position
                else "no position appeared within the settle timeout"
            ),
        ):
            return report

        try:
            fills = await client.get_executions(spec.inst_id, limit=10)
            report.fill_ids = [f.exec_id for f in fills if f.client_order_id == entry_id]
            report.add(
                "Fill recorded",
                bool(report.fill_ids),
                f"tradeIds {report.fill_ids}" if report.fill_ids
                else "no fill matched the client order ID (the position exists regardless)",
            )
        except (ApiError, TransportError) as exc:
            report.add("Fill recorded", False, str(exc))

        # --- 8. close it, reduce-only ---------------------------------
        exit_id = f"smoke{new_uuid().replace('-', '')[:24]}"
        if position_mode is PositionMode.NET:
            exit_side, exit_pos_side, reduce_only = Side.SELL, None, True
        else:
            exit_side, exit_pos_side, reduce_only = Side.SELL, PosSide.LONG, None
        close_size = format_qty(spec.round_qty(abs(position.contracts)), spec.lot_size)
        exit_order = OrderRequest(
            inst_id=spec.inst_id,
            td_mode=TdMode.ISOLATED,
            side=exit_side,
            order_type=OrderType.MARKET,
            sz=close_size,
            client_order_id=exit_id,
            pos_side=exit_pos_side,
            reduce_only=reduce_only,
        )
        try:
            close_result = await client.place_order(exit_order)
        except (ApiError, TransportError) as exc:
            report.add(
                "Close the position",
                False,
                f"{exc} — CHECK YOUR OKX DEMO ACCOUNT: a position may still be open",
            )
            return report
        report.order_ids["exit"] = close_result.exchange_order_id
        report.order_ids["exit_client"] = close_result.client_order_id
        report.add(
            "Close the position (reduce-only)",
            True,
            f"{close_size} contract(s) · ordId {close_result.exchange_order_id}",
        )

        # --- 9. confirm flat ------------------------------------------
        await _wait_for_position(client, spec.inst_id, want_open=False)
        try:
            remaining = [
                p for p in await client.get_positions(spec.inst_id) if abs(p.contracts) > 0
            ]
        except (ApiError, TransportError) as exc:
            report.add("Account is flat", False, str(exc))
            return report
        report.add(
            "Account is flat",
            not remaining,
            "no open position remains"
            if not remaining
            else f"{len(remaining)} position(s) still open — CHECK YOUR OKX DEMO ACCOUNT",
        )

    finally:
        await client.close()

    return report


def print_report(report: SmokeReport) -> None:
    """Print the PASS/FAIL summary block."""
    lines = ["OKX DEMO SMOKE TEST", ""]
    for step in report.steps:
        lines.append(f"  [{'PASS' if step.passed else 'FAIL'}] {step.name}")
        if step.detail:
            lines.append(f"         {step.detail}")
    if report.order_ids:
        lines.append("")
        lines.append("  Order IDs:")
        for label, value in report.order_ids.items():
            lines.append(f"    {label}: {value}")
    if report.fill_ids:
        lines.append(f"    fills: {', '.join(report.fill_ids)}")
    lines.append("")
    lines.append("RESULT: PASS" if report.passed else "RESULT: FAIL")
    if report.passed:
        lines.append("The full OKX Demo order path works. The 14-day timer did NOT start.")
    else:
        lines.append("Fix the failing step above before starting the experiment.")
    log.banner(lines, tag="SMOKE")
