# Bybit Capabilities — Verified Notes

**Researched:** 2026-07-24 · **Source:** official Bybit V5 documentation repository
(`github.com/bybit-exchange/docs`, branch `master`, files `docs/v5/*.mdx`) — this is the
source that renders <https://bybit-exchange.github.io/docs/v5/>.

Everything below was read from the primary source, not assumed from older Bybit Global
examples. Where the docs are silent, this file says so explicitly and the code performs a
**runtime capability discovery** instead of guessing.

---

## 1. The EU-domain question (important — read this first)

The V5 *Integration Guidance* page lists regional mainnet hosts, verbatim:

> * **Netherlands users:** use `https://api.bybit.nl` for mainnet
> * **EEA users:** use `https://api.bybit.eu` for mainnet **(EU site API only support
>   "Connect to Third-Party Applications" feature for API broker user)**
> * **Turkey users:** `https://api.bybit.tr` … **Kazakhstan:** `https://api.bybit.kz` …

**Confirmed conclusion:** `api.bybit.eu` is *not* a general-purpose trading API host for
individual users. The documentation states its API surface only supports the
"Connect to Third-Party Applications" broker feature. There is **no** documented
`api-demo.bybit.eu` host, and no EU-specific demo module.

The **Demo Trading Service** is a single, separate module documented at `docs/v5/demo.mdx`:

> **Mainnet Demo Trading URL:**
> Rest API: `https://api-demo.bybit.com`
> Websocket: `wss://stream-demo.bybit.com` (note that this only supports the private
> streams; public data is identical to that found on mainnet with `wss://stream.bybit.com`;
> WS Trade is not supported)

**What this means for this project:** the only documented way to run an automated demo BTC
experiment against Bybit is the demo module on `api-demo.bybit.com`. This system therefore
pins that host. A European account holder reaches demo trading by logging into their Bybit
account, switching to **Demo Trading**, and generating an API key there. This project does
not attempt to work around any regional routing, licensing, or eligibility control — see
§8.

> If Bybit later publishes a genuine EEA-specific demo host, it can be added to the
> `ALLOWED_DEMO_HOSTS` frozenset in `src/btcbot/exchange/endpoints.py` — a single,
> auditable place — and the demo guard will accept it. Nothing else needs to change.

---

## 2. Demo Trading Service — confirmed facts

From `docs/v5/demo.mdx`:

| Item | Confirmed value |
|---|---|
| REST host | `https://api-demo.bybit.com` |
| Private WebSocket | `wss://stream-demo.bybit.com` (**private streams only**) |
| Public WebSocket | not hosted on demo — "public data is identical to that found on mainnet with `wss://stream.bybit.com`" |
| WS Trade (order entry over WS) | **not supported** |
| Account type | Demo is an independent account with **its own user ID**; docs describe it as an isolated module |
| Order retention | "Orders generated in demo trading keep **7 days**" |
| Rate limit | "Default rate limit, **not upgradable**" |
| Key creation | Log into mainnet account → switch to `Demo Trading` → avatar → "API" |
| Testnet demo | Explicitly pointless — "do not create a key from Testnet demo trading" |

### Endpoints explicitly listed as available on demo

| Category | Endpoints |
|---|---|
| Market | **all** endpoints |
| Trade | `/v5/order/create`, `/v5/order/amend`, `/v5/order/cancel`, `/v5/order/realtime`, `/v5/order/cancel-all`, `/v5/order/history`, `/v5/execution/list`, plus batch variants (`linear`,`option` only) |
| Position | `/v5/position/list`, `/v5/position/set-leverage`, `/v5/position/switch-mode`, `/v5/position/trading-stop`, `/v5/position/set-auto-add-margin`, `/v5/position/add-margin`, `/v5/position/closed-pnl` |
| Account | `/v5/account/wallet-balance`, `/v5/account/info`, `/v5/account/transaction-log`, `/v5/account/borrow-history`, `/v5/account/collateral-info`, `/v5/account/set-collateral-switch`, `/v5/account/set-margin-mode`, `/v5/account/set-hedging-mode` |
| WS Private | `/v5/private` → topics `order`, `execution`, `position`, `wallet`, `greeks` |

**Not present in the demo list:** every `/v5/asset/*` transfer endpoint except delivery and
settlement records. There is no withdrawal, deposit, or internal-transfer capability in the
demo surface — and this project implements none regardless (§7).

### Demo-only endpoint: `POST /v5/account/demo-apply-money`

Rate limit 1 req/min. Adds (or with `adjustType=1` reduces) demo funds:

```json
{"adjustType": 0, "utaDemoApplyMoney": [{"coin": "USDT", "amountStr": "109"}]}
```

Max per request: `BTC` 15, `ETH` 200, `USDT` 100000, `USDC` 100000.

**This endpoint exists only on the demo module.** The system uses its *reachability* as one
of the demo-verification signals (§3) and offers it as an opt-in top-up so the account can
be brought to ≈ $10,000.

---

## 3. How this system proves it is on Demo

The user requirement is "verify beyond reasonable doubt". A single signal is not enough, so
`src/btcbot/exchange/demo_guard.py` requires **four independent signals to all pass**.
Order submission is structurally impossible until they do.

| # | Signal | What it proves | Failure behaviour |
|---|---|---|---|
| 1 | **Host pin** — the authenticated base URL must be a member of the compile-time `ALLOWED_DEMO_HOSTS` frozenset. Not settable from YAML, env, or CLI. | The client cannot even be constructed against a real-money host. | `MainnetRejectedError` at construction |
| 2 | **Authenticated reachability** — `GET /v5/account/info` and `GET /v5/account/wallet-balance` succeed on the demo host. | The key is valid *for the demo module*. | verification FAIL |
| 3 | **Demo-only endpoint probe** — `POST /v5/account/demo-apply-money` with a zero-amount payload. A demo host resolves the route (any `retCode` other than "route not found"); mainnet does not have this route at all. | We are talking to the demo module specifically, not merely a host that looks demo-ish. Zero amount ⇒ no balance mutation. | verification FAIL |
| 4 | **Mainnet negative control** — the same credentials are sent **once**, read-only, to `GET /v5/user/query-api` on `api.bybit.com`. This call is *required to fail authentication*. | The key is demo-scoped and cannot act on real money. If it authenticates on mainnet, it is a real-money key and we refuse to trade with it. | verification FAIL — hard stop |

Signal 4 deserves a note because it is the one that looks unusual: it is **not** a fallback.
It is a negative control that must fail. It is executed by
`MainnetNegativeControlProbe`, a deliberately crippled read-only client with no order
methods on it at all, against one read-only endpoint. It can be disabled with
`safety.mainnet_negative_control: false` for users who prefer never to transmit the key to
that host — but it defaults **on**, and disabling it is recorded in the experiment metadata
and printed as a warning.

If any signal fails the console shows the required banner and orders stay disabled:

```
==================================================
SAFETY LOCK
BYBIT DEMO ENVIRONMENT COULD NOT BE VERIFIED
ORDER SUBMISSION DISABLED
==================================================
```

Verification is re-run periodically (`safety.reverify_interval_minutes`, default 60) and
after every reconnect. A revoked verification immediately disables order submission.

---

## 4. Authentication — confirmed scheme

From `docs/v5/guide.mdx`:

* Headers: `X-BAPI-API-KEY`, `X-BAPI-TIMESTAMP` (ms), `X-BAPI-SIGN`, `X-BAPI-RECV-WINDOW`
  (default 5000).
* Pre-sign string:
  * GET → `timestamp + api_key + recv_window + queryString`
  * POST → `timestamp + api_key + recv_window + jsonBodyString`
* HMAC-SHA256 → lowercase hex. (RSA keys use RSA-SHA256 → base64; this project implements
  HMAC only, which is what Bybit's own demo key generator produces.)
* Timestamp rule: `server_time - recv_window <= ts < server_time + 1000`. The client tracks
  and applies a measured clock offset against Bybit server time.
* Common envelope: `{retCode, retMsg, result, retExtInfo, time}`; `retCode == 0` is success.
* Docs also note: **requests from US / Mainland China IPs get 403.** Surfaced as a clear
  diagnostic rather than a mystery error.

---

## 5. Instrument discovery — no assumptions

`GET /v5/market/instruments-info?category={spot|linear|inverse}` is queried at runtime for
the configured symbol. Field shapes differ per category, which is why discovery is required
rather than hardcoded:

**Spot** (`lotSizeFilter`): `basePrecision`, `quotePrecision`, `minOrderQty` *(docs mark it
deprecated — "no longer check minOrderQty, check minOrderAmt instead")*, `maxOrderQty`
*(deprecated → use `maxLimitOrderQty` / `maxMarketOrderQty`)*, `minOrderAmt`, `maxOrderAmt`,
`maxLimitOrderQty`, `maxMarketOrderQty`. `priceFilter.tickSize`. Also `marginTrading`
(e.g. `utaOnly`, `none`) and `status` (spot has `Trading` only).

**Linear/Inverse** (`lotSizeFilter`): `minOrderQty`, `maxOrderQty`, `qtyStep`.
`priceFilter.tickSize`. Plus `contractType`, `settleCoin`, `leverageFilter`.

Docs caution: `maxLimitOrderQty`, `maxMarketOrderQty`, `postOnlyMaxLimitOrderSize` are
**adjusted bi-monthly** — "Developers should not assume these values remain constant." The
system therefore refreshes instrument data on a timer
(`exchange.instrument_refresh_minutes`) and before sizing every order.

The resulting `InstrumentSpec` carries a `capabilities` set built from what was actually
returned, and the executor asks it — never a hardcoded assumption — whether an order is
permissible.

---

## 6. Spot vs derivatives — how the system handles it

The demo module is a Unified Trading Account and the docs list both spot and derivative
endpoints. **But** whether a *given* demo account may trade `linear` is an account-level
property this project refuses to assume. `CapabilityDiscovery` probes each configured
category's `instruments-info` and records what is genuinely tradable.

Default configuration is **`spot` only**, which means:

* **Long-only.** Spot has no short side. There is no leverage, no `positionIdx`, no
  `reduceOnly` (docs: those are `linear`/`inverse`/`option` only).
* Strategies that emit SHORT signals still run in the shadow engine and still accumulate
  research evidence — they are simply never routed to a real demo order. Each such skip is
  journaled with reason `short_not_supported_on_spot`, so the final report can distinguish
  "bad strategy" from "untested on the live layer".
* Spot market **buy** orders default to quantity-in-quote-currency; the docs specify
  `marketUnit` (`baseCoin` | `quoteCoin`) to choose. The executor sets `marketUnit`
  explicitly on every spot market order rather than relying on the default.
* Spot minimum is enforced by **`minOrderAmt` (notional)**, not `minOrderQty`.

If `linear` is enabled in config *and* discovery confirms it is tradable, the same
strategies gain access to short orders through the identical adapter — no rewrite.

---

## 7. What is deliberately absent

There is no code in this repository for: mainnet trading, withdrawals, deposits, internal
transfers, sub-account transfers, or a "live mode" flag. `scripts/audit_safety.sh` and
`tests/unit/test_safety_lock.py` fail the build if forbidden endpoint paths or a
non-allow-listed authenticated host string appears anywhere in `src/`.

---

## 8. Eligibility

This project contains nothing that circumvents age, KYC, geographic, or account
restrictions, and nothing that masks origin. It talks to the documented demo host with a key
the account holder generated themselves. Whether the holder may use Bybit is theirs to
establish with Bybit; if the platform declines a request (including the documented 403 for
restricted IP regions), the system reports it plainly and stops.

---

## 9. Other confirmed details used by the implementation

* **Kline** `GET /v5/market/kline` — intervals `1,3,5,15,30,60,120,240,360,720,D,W,M`;
  `limit` max **1000**; list is **sorted in reverse by startTime**; `list[4]` close "is the
  last traded price when the candle is not closed" ⇒ the newest element may be an open
  candle and must be dropped before use. This is the root of the look-ahead guard in
  `market_data/candles.py`.
* **WS public kline** topic `kline.{interval}.{symbol}`, carries `confirm: bool` — the
  system only ever feeds `confirm=true` candles to strategy logic.
* **WS orderbook** topic `orderbook.{depth}.{symbol}`; `snapshot` then `delta`; a new
  `snapshot` means reset the local book; `u`/`seq` used for sequence validation.
* **orderLinkId** — "max of 36 characters", numbers, letters, dashes, underscores, "always
  unique". This caps the client-order-ID encoding; see `utils/ids.py`, which packs
  attribution into 36 chars and stores the full attribution in SQLite.
* **Spot open-order limit** — 500 total, max 30 open TP/SL and 30 conditional per symbol.
* **Rate-limit / risk-control notice** — Bybit reserves the right to restrict accounts whose
  daily order count is excessive. The allocator's cooldowns and per-hour caps exist partly
  for this reason.
