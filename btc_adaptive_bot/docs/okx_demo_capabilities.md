# OKX Demo Capabilities — Verified Notes

**Researched:** 2026-07-28 · **Re-verified for the Global/UAE entity:** 2026-07-29
**Target:** OKX **Demo Trading**, BTC USD-margined perpetual ("BTCUSD UM X-Perp" in the
OKX UI). The default entity is **OKX Global / UAE**; EEA and US are selectable via
`exchange.region` (§1a).

## 0. Source honesty (read this first)

The primary documentation site `https://www.okx.com/docs-v5/en/` returned **HTTP 403** from
this build environment (egress proxy), and `https://my.okx.com/docs-v5/en/` plus the legacy
`okex.com` mirror were unreachable. This was re-attempted on 2026-07-29 for the Global/UAE
migration and **still returned 403** — the official docs have not been read directly at any
point in this project. Unlike the Bybit build — where the docs' own source repository was
readable — OKX does not publish its docs in a public repo.

Every fact below was therefore corroborated from **two independent, actively maintained
SDKs plus web search**, and is marked accordingly:

| Source | What it provided |
|---|---|
| `github.com/okxapi/python-okx` (OKX's official Python SDK), `okx/utils.py` + `okx/consts.py` @ master | exact signing procedure, header names, timestamp format, all `/api/v5/*` endpoint paths; `API_URL = 'https://www.okx.com'` (Global) |
| `github.com/tiagosiebler/okx-api` (maintained TypeScript SDK), `websocket-util.ts` + `requestUtils.ts` @ master | the per-region REST base URLs and the complete live/demo WebSocket URL matrix (re-read 2026-07-29) |
| Web search (current pages, July 2026) | entity/host confirmation, demo header behaviour, error-code semantics |

**No authenticated call has ever been made from this environment.** The build sandbox
cannot reach any exchange host, and the developer's credentials were never supplied to it.
Every claim about authenticated behaviour below is derived from the sources above and is
exercised in tests against mocked transports only.

Where a value could **not** be corroborated to this standard (e.g. the exact `instId` and
contract parameters of the "BTCUSD UM X-Perp"), this document says so explicitly and
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

---

## 1a. Regions — why an API key can be "not found"

OKX operates several regional entities. **An API key is issued by exactly one of them and
does not exist on the others.** A key created in Demo Trading on the Global/UAE site,
presented to `eea.okx.com`, returns:

```
code=50119  "API key doesn't exist"
```

which reads like a typo'd key but is a *region* mismatch. This is selected by
`exchange.region` in `config/default.yaml`. Every profile in the registry is a **demo**
profile; there is no value that reaches a live endpoint.

### The demo profile registry

Defined once, in `src/btcbot/exchange/endpoints.py`. Source: tiagosiebler SDK
`requestUtils.ts` (`getRestBaseUrl`) and `websocket-util.ts`, both re-read 2026-07-29.

| `region` | Entity | REST | WS public / private / business (demo) |
|---|---|---|---|
| `global` *(default)* | OKX Global / UAE | `https://openapi.okx.com` (alt: `https://www.okx.com`) | `wss://wspap.okx.com:8443/ws/v5/{public,private,business}` |
| `eea` | OKX Europe (EEA, MiCA) | `https://eea.okx.com` | `wss://wseeapap.okx.com:8443/ws/v5/{public,private,business}` |
| `us` | OKX US | `https://us.okx.com` | `wss://wsuspap.okx.com:8443/ws/v5/{public,private,business}` |

Two REST hosts are allow-listed for Global because the SDK exposes both: `GLOBAL`/`prod`
→ `https://www.okx.com` (also `API_URL` in OKX's own Python SDK) and `OPENAPI_GLOBAL` →
`https://openapi.okx.com`. The default is `openapi.okx.com`; switching is one config line.

The demo business URL carries `?brokerId=9999` in the SDK matrix. Both spellings — with
and without the query — are allow-listed, because the SDKs disagree and neither is a
live endpoint.

### Hosts that must be rejected

REST is **not** environment-separated on OKX: every region serves demo and live from the
same host, switched by the header. So a REST deny-list would be false comfort — what
protects REST is the unconditional header plus the negative control (§4). The REST
allow-list exists to stop the client being pointed at an *unrecognised* host at all.

WebSockets *are* environment-separated, by a single `pap` infix. These nine URLs are
explicitly forbidden and `MainnetRejectedError` is raised before a socket is opened:

| Host | What it is |
|---|---|
| `wss://ws.okx.com:8443/ws/v5/{public,private,business}` | Global **live** |
| `wss://wseea.okx.com:8443/ws/v5/{public,private,business}` | EEA **live** |
| `wss://wsus.okx.com:8443/ws/v5/{public,private,business}` | US **live** |

Each is one dropped infix away from its demo counterpart (`wspap`→`ws`,
`wseeapap`→`wseea`, `wsuspap`→`wsus`), which is why the check is an **exact-match
allow-list**, never a substring test. A test asserts no forbidden URL has leaked into the
allow-list.

The demo/live WS distinction is a *hostname* distinction; on REST it is a *header*
distinction. Both mechanisms are enforced.

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
| public (demo) | `tickers`, `trades`, `books`, `open-interest`, `funding-rate`, `mark-price` |
| business (demo) | `candle1m` / `candle5m` / … — **candlestick channels live on the business endpoint**, which is why the business URL (with `brokerId=9999`) is part of the required host set |
| private (demo) | `account`, `positions`, `orders`, `balance_and_position` |

WS candle payloads carry the same `confirm` flag; only confirmed candles reach strategies.

### Order responses are batch-shaped — the envelope is not the rejection

Every OKX trade endpoint returns a batch envelope, even when you send one order:

```json
{"code": "1", "msg": "All operations failed",
 "data": [{"ordId": "", "clOrdId": "…", "sCode": "51000",
           "sMsg": "…the actual reason…", "subCode": "1000"}]}
```

| Envelope `code` | Meaning |
|---|---|
| `0` | every operation succeeded |
| `1` | every operation failed |
| `2` | partial success — some items succeeded, some did not |

With `1` and `2` the envelope `msg` is only `"All operations failed"`, which names no
cause. The real rejection is per item: `sCode`, `sMsg`, and often a `subCode`. A transport
that raises on the envelope therefore throws away the only diagnostic that exists — the
exact failure this project hit on its first live smoke test.

So the transport (`rest.py`) defers **only** codes 1 and 2, **only** on the four paths in
`ORDER_OPERATION_PATHS`, and **only** when a usable data array is present; the endpoint
parser then accepts an operation solely on `sCode == 0` and raises `OrderRejectedError`
(an `ApiError` whose `ret_code` is the real `sCode`) otherwise. Every other envelope code
— authentication, environment mismatch, rate limit, malformed body, an empty data array —
still fails immediately in the transport layer, unchanged. Requesting the relaxation on a
non-order path raises `ValueError`, so it cannot spread by accident.

Note the two independent checks this implies: a clean envelope (`code: 0`) with a rejected
item is still a rejection, and a rejected envelope with no item to inspect is still a
failure. Neither level alone is trusted.

### Read endpoints are eventually consistent, and not in the same order

After a market order is accepted, OKX's read surfaces become consistent on
different schedules. Observed order, fastest first:

| Endpoint | What it settles | Timing |
|---|---|---|
| `GET /api/v5/trade/order` | `state`, `accFillSz`, `avgPx`, `fee`, `uTime` | first — reflects the match engine |
| `GET /api/v5/account/positions` | the position | shortly after |
| `GET /api/v5/trade/fills` | the individual trades (`tradeId`, `fillPx`, `fillSz`, per-fill `fee`) | **last**, sometimes by a second or more |

Asking the fills endpoint immediately therefore produces a false negative: a
real, filled, visible position reported as "no fill matched the client order
ID". That is exactly what the first successful live smoke test hit.

So `execution/reconciliation.py` encodes: **order details are the authority on
whether the order filled; the fills endpoint is the authority on per-fill
detail.** Order details are polled on a bounded schedule — immediate, then
0.25s, 0.5s, 1s, 2s, 2s, 2s (7.75s of waiting) — stopping on `filled`,
`partially_filled` or `canceled`. Once a fill is proven, fills are polled on a
shorter schedule (0/0.25/0.5/1s); a miss there is a delay, not a failure.

Two more things this costs nothing to get right:

* **`ordId`, not `clOrdId`.** OKX does not always echo `clOrdId` on the fills
  endpoint — in the live run it came back empty. Matching on `clOrdId` alone
  finds nothing; matching on `ordId` finds it. `clOrdId` remains a fallback for
  an order whose `ordId` we never received.
* **Unconfirmed is not "did not fill".** If the budget expires without the
  order settling, the system does not know whether it holds a position. It
  enters SAFE_MODE, leaves the ledger untouched, and never sends a replacement
  order — the `UNIQUE(setup_id, intent)` reservation makes a second submission
  for that setup structurally impossible.

### Protection must be registered at the exchange, not held in Python

An entry that fills leaves a real position. Until an algo order exists at OKX,
that position has no stop — regardless of what any Python object holds. This
system opened exactly one such position before this was fixed: the bot's logs
showed `stop_loss` and `take_profit` levels, and the exchange held nothing.

`OrderRequest` has always emitted `attachAlgoOrds` when given
`sl_trigger_price`/`tp_trigger_price`; the entry path simply never set them.
The stop lived on `LedgerPosition` and was enforced by `TradeManager` issuing a
market exit when price crossed it — software-side management that dies with the
process, the WebSocket, or the machine.

Endpoints used:

| Endpoint | Purpose |
|---|---|
| `POST /api/v5/trade/order` with `attachAlgoOrds` | TP/SL created with the entry fill |
| `POST /api/v5/trade/order-algo` | standalone OCO placed after a fill |
| `GET /api/v5/trade/orders-algo-pending` | **the only evidence protection exists** |
| `POST /api/v5/trade/cancel-algos` | remove protection when the position closes |

Four things this encodes, in `execution/protection.py`:

* **OCO, not two conditionals.** A separate stop and target leave an orphan when
  one fires: the position closes on the take-profit and the stop survives, ready
  to open a *new* position in the opposite direction. OKX cancels an OCO's
  sibling atomically, which is requirement "closing one cancels the other" for
  free.
* **Placement success is not evidence.** OKX can accept an order and reject its
  attached algo. Every `protected=True` is backed by a read-back of
  `orders-algo-pending` within the same call, polled on a short bounded backoff
  (0/0.3/0.7/1.5s) because OKX registers an algo order a beat after accepting it.
* **The size is rounded *up* onto the lot grid.** Rounding down under-covers the
  position, and a fill below one lot would round to zero — no protection at all.
  Over-covering is harmless because protection is reduce-only, so the exchange
  clamps it to what is open.
* **A read failure is not "unprotected".** It raises, so a network blip cannot
  cause a healthy position to be closed. An unreadable stop *at startup* is
  treated as absent, because there the position is already unattended.

If a stop cannot be placed and verified, the position is closed reduce-only and
SAFE_MODE is entered. If the stop verifies but the take-profit does not, the
stop is kept, the position stays open, and further entries are blocked.

---

---

## 4. How this system proves it is on Demo

Same four-signal structure as the Bybit build — order submission is structurally
impossible until **all four** pass, and verification re-runs periodically and after every
reconnect:

| # | Signal | What it proves | Failure behaviour |
|---|---|---|---|
| 1 | **Host pin** — the REST base URL must be a recognised OKX demo host and every WS URL the active profile will dial must be on the exact-match demo allow-list. YAML picks *which* demo region (§1a); it cannot supply a URL. | We can only ever talk to a known OKX entity, and only to demo WS hosts. | `MainnetRejectedError` at construction |
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
underlying API `instId` could **not** be confirmed from primary
documentation in this environment — and per the project rules it would not be hardcoded
even if it had been.

Discovery therefore works like this (`exchange/instruments.py`):

1. `GET /api/v5/public/instruments?instType=SWAP` against the configured region's host **at runtime**.
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

## 8a. Account equity vs research capital

OKX's `/api/v5/account/balance` returns `totalEq` — the USD value of **every** holding in
the account — plus a per-currency `details` array. A demo account routinely holds BTC,
ETH, OKB and AED alongside USDT, so `totalEq` can be many times the USDT balance.

This system never sizes from `totalEq`. It reads the `USDT` line of `details` only, caps
it at `execution.research_equity_cap_usdt`, and does so exactly once — at experiment
start. Everything afterwards is bot-attributable accounting:

```
current research equity = starting
                        + realised PnL      (this experiment's closed positions)
                        + unrealised PnL    (this experiment's open positions)
                        - fees              (this experiment's fills)
                        + funding           (signed; positive when received)
```

Fields used, and what they are used for:

| Field | Source | Used for |
|---|---|---|
| `details[USDT].eq` | balance | research capital at start (capped) |
| `details[USDT].availEq` | balance | available margin before every order |
| `totalEq` | balance | display only — never sizing, limits or scoring |
| `positions.realized_pnl` / `.fees` | local DB | realised PnL (grossed up so fees count once) |
| `funding_events.amount` | local DB | settlement cost, signed |

See `execution/research_equity.py`.

## 9. Eligibility

Unchanged policy from the Bybit build: nothing here circumvents age, KYC, geographic, or
account restrictions, and nothing masks origin. The system talks to the documented demo
environment of the entity the account holder's own key belongs to, with a demo key they
generated themselves. If OKX declines
a request, the system reports the exchange's stated reason plainly and stops.
