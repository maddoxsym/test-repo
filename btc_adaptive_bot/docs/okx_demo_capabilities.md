# OKX Europe Demo Capabilities — Verified Notes

**Researched:** 2026-07-28 · **Target:** OKX Europe (EEA entity, MiCA-regulated) **Demo
Trading**, BTC USD-margined perpetual ("BTCUSD UM X-Perp" in the OKX Europe UI).

## 0. Source honesty (read this first)

The primary documentation site `https://www.okx.com/docs-v5/en/` returned **HTTP 403** from
this build environment (egress proxy), and `https://my.okx.com/docs-v5/en/` plus the legacy
`okex.com` mirror were unreachable. Unlike the Bybit build — where the docs' own source
repository was readable — OKX does not publish its docs in a public repo.

Every fact below was therefore corroborated from **two independent, actively maintained
SDKs plus web search**, and is marked accordingly:

| Source | What it provided |
|---|---|
| `github.com/okxapi/python-okx` (OKX's official Python SDK), `okx/utils.py` + `okx/consts.py` @ master | exact signing procedure, header names, timestamp format, all `/api/v5/*` endpoint paths |
| `github.com/tiagosiebler/okx-api` (maintained TypeScript SDK), `websocket-util.ts` + `requestUtils.ts` @ master | the EEA REST base URL and the complete live/demo WebSocket URL matrix per region |
| Web search (current pages, July 2026) | EEA entity host confirmation, demo header behaviour, error-code semantics |

Where a value could **not** be corroborated to this standard (e.g. the exact `instId` and
contract parameters of the EEA "BTCUSD UM X-Perp"), this document says so explicitly and
the code performs **runtime discovery** instead of guessing. That is the same policy the
Bybit build used (`docs/bybit_capabilities.md` §5), applied more aggressively because the
primary docs were unreachable.

> If any value below turns out to differ from the current official documentation, the
> single place to correct hosts is `src/btcbot/exchange/endpoints.py` and the single place
> to correct paths is `src/btcbot/exchange/rest.py` — nothing is duplicated elsewhere.

---

## 1. The environment model — OKX differs structurally from Bybit

Bybit's demo is a **separate host** (`api-demo.bybit.com`). OKX's demo is the **same host
as production, switched per-request by a header**:

```
x-simulated-trading: 1
```

This is the single most safety-critical fact of the migration. A missing header on an
authenticated call would target the live environment. Consequences for the design:

1. The header is injected by the **transport layer** (`OkxDemoClient._headers`), not by
   call sites. There is no code path that builds authenticated headers without it.
2. A demo-scoped API key sent **without** the header is rejected by OKX with error
   `50101` ("APIKey does not match current environment"). The negative-control probe
   (§4, signal 4) exploits exactly this: the key **must fail** against the live
   environment for verification to pass.
3. `scripts/audit_safety.sh` asserts the header constant is referenced by the transport
   layer and that no second header-building code path exists.

### Hosts

| Purpose | Value | Corroboration |
|---|---|---|
| REST (EEA entity) | `https://eea.okx.com` | tiagosiebler SDK `EEA` market → `https://eea.okx.com`; matches the user-confirmed OKX Europe account entity |
| WS public, demo (EEA) | `wss://wseeapap.okx.com:8443/ws/v5/public` | tiagosiebler SDK `EEA.demo.public` |
| WS private, demo (EEA) | `wss://wseeapap.okx.com:8443/ws/v5/private` | tiagosiebler SDK `EEA.demo.private` |
| WS business, demo (EEA) | `wss://wseeapap.okx.com:8443/ws/v5/business?brokerId=9999` | tiagosiebler SDK `EEA.demo.business` — note the `brokerId=9999` query, present on every demo business URL in the SDK matrix |

**Hosts that must be rejected** (compile-time forbidden — connecting to any of these is a
bug, and `MainnetRejectedError` is raised before a socket is opened):

| Host | Why it exists | Why we reject it |
|---|---|---|
| `https://www.okx.com` | OKX Global REST | wrong entity for an EEA account; also the global live host |
| `https://us.okx.com`, `https://openapi.okx.com` | US / OpenAPI entities | wrong entity |
| `wss://wseea.okx.com:8443/...` | **EEA live** WebSockets | live trading environment — one dropped `pap` infix away from the demo host, so the check is an exact-host allow-list, never a substring match |
| `wss://ws.okx.com:8443/...` | Global live WS | live |
| `wss://wspap.okx.com:8443/...` | Global demo WS | wrong entity (demo, but not EEA) |
| `wss://wsuspap.okx.com:8443/...` | US demo WS | wrong entity |

The demo/live WS distinction is a *hostname* distinction (`wseeapap` vs `wseea`), unlike
REST where it is a *header* distinction. Both mechanisms are enforced.

---

## 2. Authentication — confirmed scheme (official SDK, `okx/utils.py`)

Three credentials, read only from the environment / local `.env` (never from YAML, never
logged, never echoed):

```
OKX_DEMO_API_KEY
OKX_DEMO_API_SECRET
OKX_DEMO_PASSPHRASE
```

Headers on every authenticated request:

| Header | Value |
|---|---|
| `OK-ACCESS-KEY` | the API key |
| `OK-ACCESS-SIGN` | signature (below) |
| `OK-ACCESS-TIMESTAMP` | ISO-8601 UTC with milliseconds, e.g. `2026-07-28T17:04:05.123Z` |
| `OK-ACCESS-PASSPHRASE` | the API-key passphrase, plaintext |
| `Content-Type` | `application/json` |
| `x-simulated-trading` | `1` (always — injected centrally) |

Signature (verbatim from the official SDK):

```
pre_hash  = timestamp + METHOD.upper() + request_path + body
signature = base64( HMAC-SHA256(secret, pre_hash) )
```

* `request_path` **includes the query string** for GET requests
  (e.g. `/api/v5/account/balance?ccy=USDT`).
* `body` is the exact JSON string sent; for GET / empty-body requests it is the empty
  string (the SDK normalises `{}` / `None` → `""`).
* **base64 output** — not lowercase hex. This is one of three material differences from
  Bybit signing (the others: the pre-hash composition and the ISO timestamp), which is
  why `signing.py` was rewritten rather than adapted.

WebSocket login differs in one respect: the `op: login` frame signs
`timestamp + 'GET' + '/users/self/verify'` where `timestamp` is **Unix epoch seconds**
(as a string), not the ISO form. Same base64 HMAC-SHA256.

Timestamp tolerance: requests are rejected when the timestamp deviates too far from OKX
server time (commonly documented as 30 s; not independently verifiable here). The client
measures drift against `GET /api/v5/public/time` (returns `ts` in ms) and **pauses
authenticated trading** when measured drift exceeds `safety.max_clock_drift_ms` — a
paused-trading state, not a silent correction, because a machine with a wandering clock
cannot be trusted to stamp orders.

Common envelope: `{"code": "0", "msg": "", "data": [...]}` — `code == "0"` is success,
everything else is an error. Order endpoints additionally carry per-item `sCode`/`sMsg`
inside `data`, and can return outer `code "1"` (all failed) or `"2"` (partial success);
the client checks **both** levels.

Relevant error codes (from the public error-code documentation, via search):

| Code | Meaning | How the system reacts |
|---|---|---|
| `50101` | APIKey does not match current environment | expected & required in the negative control; fatal anywhere else |
| `50102` | Timestamp expired | triggers clock-drift re-measurement; trading pauses if drift confirmed |
| `50111` / `50113` | Invalid key / invalid signature | credential problem — safety lock, orders disabled |
| `50004` / HTTP 429 | Timeout / rate limit | backoff + retry per the standard retry policy |
| `51xxx` | Order-level rejections (balance, size, state…) | journaled to `rejected_signals` with the exchange reason |

---

## 3. Endpoints used (paths from the official SDK's `consts.py`)

Public (no auth, but still sent with the demo header for consistency):

| Purpose | Endpoint |
|---|---|
| Server time | `GET /api/v5/public/time` |
| Instruments | `GET /api/v5/public/instruments?instType=SWAP` |
| Funding rate (current + next) | `GET /api/v5/public/funding-rate?instId=…` |
| Funding rate history | `GET /api/v5/public/funding-rate-history?instId=…` |
| Mark price | `GET /api/v5/public/mark-price?instType=SWAP&instId=…` |
| Position tiers (leverage brackets / MMR) | `GET /api/v5/public/position-tiers` |
| Candles (recent) | `GET /api/v5/market/candles?instId=…&bar=…` (newest-first) |
| Candles (deep history) | `GET /api/v5/market/history-candles` |
| Ticker | `GET /api/v5/market/ticker?instId=…` |
| Order book | `GET /api/v5/market/books?instId=…` |
| Recent trades | `GET /api/v5/market/trades?instId=…` |

**Candle look-ahead guard, OKX form:** candle rows are
`[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]` and the newest row can carry
`confirm == "0"` (uncompleted). The market-data layer drops unconfirmed rows before any
strategy sees them — the direct equivalent of the Bybit rule that made this guard exist.

Authenticated (demo header + signature):

| Purpose | Endpoint |
|---|---|
| Account configuration (incl. `posMode`, `acctLv`) | `GET /api/v5/account/config` |
| Balance | `GET /api/v5/account/balance` |
| Positions | `GET /api/v5/account/positions` |
| Position history | `GET /api/v5/account/positions-history` |
| Set position mode | `POST /api/v5/account/set-position-mode` |
| Set leverage | `POST /api/v5/account/set-leverage` |
| **Confirm** leverage | `GET /api/v5/account/leverage-info` |
| Max order size | `GET /api/v5/account/max-size` |
| Fee rates | `GET /api/v5/account/trade-fee` |
| Bills (funding fees appear here, `type=8`) | `GET /api/v5/account/bills` |
| Place order | `POST /api/v5/trade/order` |
| Cancel order | `POST /api/v5/trade/cancel-order` |
| Order detail | `GET /api/v5/trade/order` |
| Open orders | `GET /api/v5/trade/orders-pending` |
| Order history | `GET /api/v5/trade/orders-history` |
| Fills (fees per fill) | `GET /api/v5/trade/fills` |
| Close position | `POST /api/v5/trade/close-position` |

**Deliberately absent** (exists in the API, never in this codebase): everything under
`/api/v5/asset/*` (withdrawals, deposits, transfers), sub-account endpoints, convert,
loans, staking. `scripts/audit_safety.sh` fails the build if any of these paths appear in
`src/`.

### WebSocket channels

| Stream | Channels used |
|---|---|
| public (demo EEA) | `tickers`, `trades`, `books`, `open-interest`, `funding-rate`, `mark-price` |
| business (demo EEA) | `candle1m` / `candle5m` / … — **candlestick channels live on the business endpoint**, which is why the business URL (with `brokerId=9999`) is part of the required host set |
| private (demo EEA) | `account`, `positions`, `orders`, `balance_and_position` |

WS candle payloads carry the same `confirm` flag; only confirmed candles reach strategies.

---

## 4. How this system proves it is on Demo

Same four-signal structure as the Bybit build — order submission is structurally
impossible until **all four** pass, and verification re-runs periodically and after every
reconnect:

| # | Signal | What it proves | Failure behaviour |
|---|---|---|---|
| 1 | **Host pin** — the REST base URL must equal `https://eea.okx.com` and WS URLs must be members of the compile-time EEA-demo allow-list. Not settable from YAML, env, or CLI. | We can only ever talk to the EEA entity, and only to demo WS hosts. | `MainnetRejectedError` at construction |
| 2 | **Header enforcement probe** — the client's own header builder is inspected at runtime: every authenticated request path routes through the single `_headers()` implementation that hard-codes `x-simulated-trading: 1`. | No request can be emitted without the demo switch. | verification FAIL |
| 3 | **Authenticated demo reachability** — `GET /api/v5/account/config` and `GET /api/v5/account/balance` succeed **with** the demo header, and the account config is sane (a `posMode` is returned). | The key is valid for the demo environment and the account is usable. | verification FAIL |
| 4 | **Live-environment negative control** — the same credentials are sent **once**, read-only (`GET /api/v5/account/config`), **without** the demo header. This call is *required to fail* with `50101` (environment mismatch) or an equivalent auth rejection. If it authenticates, the key is live-scoped and the system refuses to trade with it. | The key cannot act on the live environment. | verification FAIL — hard stop |

Signal 4 is the OKX translation of Bybit's mainnet negative control. It is cheaper and
tighter here: no second host is involved — the *only* difference between the control
request and a normal request is the missing header, which is precisely the failure mode
being guarded against. As before it is executed by a deliberately crippled read-only
probe object with no order methods, defaults **on**, and disabling it
(`safety.mainnet_negative_control: false`) is recorded in experiment metadata and warned
about at startup.

Failure banner (unchanged contract):

```
==================================================
SAFETY LOCK
OKX DEMO ENVIRONMENT COULD NOT BE VERIFIED
ORDER SUBMISSION DISABLED
==================================================
```

---

## 5. Instrument discovery — the "BTCUSD UM X-Perp" question

"BTCUSD UM X-Perp" is the **display name** in the OKX Europe UI (MiCA-compliant
perpetual-style product; "UM" = USD(T/C)-margined, i.e. a *linear* contract). The
underlying API `instId` for the EEA entity could **not** be confirmed from primary
documentation in this environment — and per the project rules it would not be hardcoded
even if it had been.

Discovery therefore works like this (`exchange/instruments.py`):

1. `GET /api/v5/public/instruments?instType=SWAP` against `eea.okx.com` **at runtime**.
2. Filter: `uly`/`instFamily` referencing BTC, `ctType == "linear"`, `state == "live"`,
   settle currency in the configured accept-list (`USDT`/`USDC`/`USD`).
3. If exactly one instrument matches, it is selected and its full spec is journaled. If
   several match (e.g. both a USDT- and a USDC-margined X-Perp), the configured
   preference order picks one and the choice is journaled with the alternatives listed.
   If none match, startup **fails loudly** with the full instrument list logged — the
   system never falls back to a guessed `instId`.

Fields consumed from the instrument record (semantics per the V5 schema; **values are
never assumed**, always read at runtime):

| Field | Meaning | Use |
|---|---|---|
| `instId` | instrument ID | every subsequent call |
| `ctVal` / `ctValCcy` | face value of one contract, and its currency | contract ↔ base-quantity conversion |
| `ctMult` | contract multiplier | same |
| `ctType` | `linear` / `inverse` | selection filter + PnL maths |
| `lotSz` | order size increment (in contracts) | rounding down |
| `minSz` | minimum order size (in contracts) | floor check |
| `tickSz` | price increment | price rounding |
| `lever` | maximum leverage | upper bound for the leverage engine (config caps at 10x regardless) |
| `settleCcy` | settlement currency | balance/PnL currency |
| `fundingInterval`-related fields | funding cadence | funding tracker; cadence is *discovered*, not assumed to be 8-hourly |

Contract sizing (linear): `contracts = floor(base_qty / (ctVal × ctMult) / lotSz) × lotSz`,
then `contracts ≥ minSz` or the order is rejected and journaled. Notional =
`contracts × ctVal × ctMult × price`. The inverse formulas exist and are tested, but the
selection filter prefers linear, matching the "UM" display name.

---

## 6. Margin modes, position modes, order flags

From the V5 schema (corroborated via both SDKs' typed interfaces):

* **`tdMode`** on every order: `isolated` | `cross` (| `cash` for spot — unused here).
  This project uses **`isolated` only**. There is no silent fallback to cross: if an
  isolated-mode order is rejected for mode reasons, that is a journaled failure, not a
  retry with `cross`.
* **Position mode** is an account-level setting read from `GET /api/v5/account/config`
  (`posMode`): `net_mode` or `long_short_mode`. The system **adapts to whatever the
  account is set to** rather than forcing a change:
  * `net_mode` — orders carry `side` (`buy`/`sell`) and use `reduceOnly: true` on every
    closing order so a close can never flip into an opposite position.
  * `long_short_mode` — orders carry `side` + `posSide` (`long`/`short`); opening buys
    are `side=buy, posSide=long`, closing a long is `side=sell, posSide=long`, etc.
    `reduceOnly` is not sent (open/close is unambiguous from the pair).
* **Leverage** is set per instrument+mode via `POST /api/v5/account/set-leverage`
  (`instId`, `lever`, `mgnMode`, plus `posSide` when in long/short isolated mode) and
  then **confirmed** via `GET /api/v5/account/leverage-info` before any entry order is
  sent. Set-without-confirm is not trusted.
* **Order types** used: `limit`, `market`, `post_only`, `ioc`. Every order carries a
  `clOrdId` (client order ID, ≤ 32 chars alphanumeric for OKX — note: **shorter than
  Bybit's 36**, so the ID layout was re-packed; see `utils/ids.py`).

---

## 7. Funding, settlement, fees

* A perpetual swap has **no expiry**; economic settlement happens through the **funding
  rate**, exchanged periodically between longs and shorts. Current and next funding
  come from `GET /api/v5/public/funding-rate`; realised funding paid/received appears in
  `GET /api/v5/account/bills` with bill `type = 8`.
* The funding tracker records every funding event against the open position, and funding
  costs are included in **all** PnL figures, strategy scores, and champion selection —
  a strategy that only wins by ignoring funding is not a winner.
* Trading fees come per-fill from `GET /api/v5/trade/fills` (`fee`, `feeCcy`) and the
  account's current schedule from `GET /api/v5/account/trade-fee`. Shadow accounts and
  the backtester use the discovered taker/maker rates, not hardcoded ones.
* **Liquidation:** positions expose `liqPx` (estimated liquidation price), `mgnRatio`,
  `imr`, `mmr`. The liquidation-protection layer refuses entries whose stop distance is
  not comfortably inside the projected liquidation distance at the chosen leverage, and
  the circuit breaker flattens + pauses if a live position's margin ratio degrades past
  the configured threshold.

---

## 8. What is deliberately absent

There is no code in this repository for: live/production trading, real-money mode,
withdrawals, deposits, transfers, sub-accounts, convert, lending/borrowing, or any
CLI/config switch that could select a live environment. The demo header cannot be turned
off by configuration. `scripts/audit_safety.sh` and `tests/unit/test_safety_lock.py`
fail the build if a forbidden endpoint path or a non-allow-listed host string appears in
`src/`.

## 9. Eligibility

Unchanged policy from the Bybit build: nothing here circumvents age, KYC, geographic, or
account restrictions, and nothing masks origin. The system talks to the documented EEA
demo environment with a demo key the account holder generated themselves. If OKX declines
a request, the system reports the exchange's stated reason plainly and stops.
