"""
Thin REST client for the OANDA v20 API.

Docs: https://developer.oanda.com/rest-live-v20/introduction/
Hosts: practice = api-fxpractice.oanda.com, live = api-fxtrade.oanda.com
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from .config import OandaConfig
from .indicators import Candle

log = logging.getLogger("oanda")


class OandaError(RuntimeError):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"OANDA API error {status}: {body}")


def parse_oanda_time(ts: str) -> float:
    """OANDA returns RFC3339 with nanoseconds, e.g. 2026-07-11T12:00:00.000000000Z"""
    ts = ts.rstrip("Z")
    if "." in ts:
        head, frac = ts.split(".", 1)
        ts = f"{head}.{frac[:6]}"          # trim to microseconds for fromisoformat
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp()


@dataclass
class InstrumentSpec:
    name: str
    display_precision: int
    trade_units_precision: int
    minimum_trade_size: float
    margin_rate: float


@dataclass
class PriceQuote:
    instrument: str
    bid: float
    ask: float
    tradeable: bool
    time: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


# sensible fallbacks if the instruments endpoint is unavailable
FALLBACK_SPECS = {
    "EUR_USD": InstrumentSpec("EUR_USD", 5, 0, 1, 0.0333),
    "XAU_USD": InstrumentSpec("XAU_USD", 3, 0, 1, 0.05),
}


class OandaClient:
    def __init__(self, cfg: OandaConfig):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {cfg.api_token}",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339",
        })
        self._specs: dict[str, InstrumentSpec] = {}

    # ---------------------------------------------------------------- http

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json: dict | None = None, retries: int = 3) -> dict:
        url = f"{self.cfg.rest_host}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.request(
                    method, url, params=params, json=json,
                    timeout=self.cfg.request_timeout)
                if resp.status_code in (429, 502, 503, 504):
                    raise OandaError(resp.status_code, resp.text[:300])
                if resp.status_code >= 400:
                    # client errors are not retryable -- raise immediately
                    try:
                        body = resp.json()
                    except ValueError:
                        body = resp.text[:300]
                    raise OandaError(resp.status_code, body)
                return resp.json()
            except OandaError as exc:
                if exc.status in (429, 502, 503, 504) and attempt < retries:
                    last_exc = exc
                else:
                    raise
            except requests.RequestException as exc:
                last_exc = exc
            if attempt < retries:
                wait = 2 ** attempt
                log.warning("OANDA request failed (%s), retrying in %ss", last_exc, wait)
                time.sleep(wait)
        raise OandaError(0, f"request failed after retries: {last_exc}")

    # ------------------------------------------------------------- account

    def account_summary(self) -> dict:
        data = self._request("GET", f"/v3/accounts/{self.cfg.account_id}/summary")
        return data["account"]

    def balance(self) -> float:
        return float(self.account_summary()["balance"])

    # --------------------------------------------------------- instruments

    def instrument_specs(self, instruments: list[str]) -> dict[str, InstrumentSpec]:
        missing = [i for i in instruments if i not in self._specs]
        if missing:
            try:
                data = self._request(
                    "GET", f"/v3/accounts/{self.cfg.account_id}/instruments",
                    params={"instruments": ",".join(missing)})
                for ins in data.get("instruments", []):
                    self._specs[ins["name"]] = InstrumentSpec(
                        name=ins["name"],
                        display_precision=int(ins["displayPrecision"]),
                        trade_units_precision=int(ins["tradeUnitsPrecision"]),
                        minimum_trade_size=float(ins.get("minimumTradeSize", 1)),
                        margin_rate=float(ins.get("marginRate", 0.05)),
                    )
            except (OandaError, KeyError, ValueError) as exc:
                log.warning("instrument spec fetch failed (%s); using fallbacks", exc)
            for name in missing:
                if name not in self._specs and name in FALLBACK_SPECS:
                    self._specs[name] = FALLBACK_SPECS[name]
        return {i: self._specs[i] for i in instruments if i in self._specs}

    def fmt_price(self, instrument: str, price: float) -> str:
        spec = self._specs.get(instrument) or FALLBACK_SPECS.get(instrument)
        precision = spec.display_precision if spec else 5
        return f"{price:.{precision}f}"

    # ------------------------------------------------------------- candles

    def candles(self, instrument: str, granularity: str = "M5",
                count: int = 400, only_complete: bool = True) -> list[Candle]:
        data = self._request(
            "GET", f"/v3/instruments/{instrument}/candles",
            params={"granularity": granularity, "count": count, "price": "M"})
        out: list[Candle] = []
        for c in data.get("candles", []):
            if only_complete and not c.get("complete", False):
                continue
            mid = c["mid"]
            out.append(Candle(
                time=parse_oanda_time(c["time"]),
                open=float(mid["o"]), high=float(mid["h"]),
                low=float(mid["l"]), close=float(mid["c"]),
                volume=float(c.get("volume", 0)),
                complete=bool(c.get("complete", False)),
            ))
        return out

    # ------------------------------------------------------------- pricing

    def pricing(self, instruments: list[str]) -> dict[str, PriceQuote]:
        data = self._request(
            "GET", f"/v3/accounts/{self.cfg.account_id}/pricing",
            params={"instruments": ",".join(instruments)})
        out = {}
        for p in data.get("prices", []):
            if not p.get("bids") or not p.get("asks"):
                continue
            out[p["instrument"]] = PriceQuote(
                instrument=p["instrument"],
                bid=float(p["bids"][0]["price"]),
                ask=float(p["asks"][0]["price"]),
                tradeable=bool(p.get("tradeable", False)),
                time=parse_oanda_time(p["time"]),
            )
        return out

    # -------------------------------------------------------------- trades

    def open_trades(self) -> list[dict]:
        data = self._request("GET", f"/v3/accounts/{self.cfg.account_id}/openTrades")
        return data.get("trades", [])

    def get_trade(self, trade_id: str) -> dict:
        data = self._request(
            "GET", f"/v3/accounts/{self.cfg.account_id}/trades/{trade_id}")
        return data["trade"]

    def market_order(self, instrument: str, units: float,
                     stop_loss: float, take_profit: float) -> dict:
        """Places a market order with SL/TP attached. units>0 long, <0 short.
        Returns {'trade_id': str|None, 'fill_price': float|None, 'raw': dict}."""
        order = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(int(units)) if float(units).is_integer() else str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": self.fmt_price(instrument, stop_loss),
                               "timeInForce": "GTC"},
            "takeProfitOnFill": {"price": self.fmt_price(instrument, take_profit),
                                 "timeInForce": "GTC"},
        }
        data = self._request("POST", f"/v3/accounts/{self.cfg.account_id}/orders",
                             json={"order": order})
        fill = data.get("orderFillTransaction")
        cancel = data.get("orderCancelTransaction")
        if cancel and not fill:
            log.warning("order cancelled by OANDA: %s", cancel.get("reason"))
            return {"trade_id": None, "fill_price": None, "raw": data}
        trade_id = None
        fill_price = None
        if fill:
            fill_price = float(fill.get("price", 0)) or None
            opened = fill.get("tradeOpened")
            if opened:
                trade_id = opened.get("tradeID")
        return {"trade_id": trade_id, "fill_price": fill_price, "raw": data}

    def set_stop_loss(self, trade_id: str, instrument: str, price: float) -> dict:
        return self._request(
            "PUT", f"/v3/accounts/{self.cfg.account_id}/trades/{trade_id}/orders",
            json={"stopLoss": {"price": self.fmt_price(instrument, price),
                               "timeInForce": "GTC"}})

    def close_trade(self, trade_id: str) -> dict:
        return self._request(
            "PUT", f"/v3/accounts/{self.cfg.account_id}/trades/{trade_id}/close",
            json={"units": "ALL"})
