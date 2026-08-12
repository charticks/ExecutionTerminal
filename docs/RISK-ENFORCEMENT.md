# Server-side trading validation

_Implemented 2026-08-11. Engine: `sidecar/services/risk_engine.py`._

No live order reaches a broker without passing every applicable rule in the
sidecar. The renderer keeps its own copies of these limits so it can grey out a
control or explain a rejection without a round trip, but it is a convenience
layer — nothing it does can let an order past the sidecar.

## How it works

A rule is a function `(OrderContext) -> Violation | None`. `RULES` is an ordered
list; the first violation wins, and rules run cheapest-first (structural →
sizing → price → session state). Adding a control is one function plus one list
entry — the order flow does not change.

`OrderContext` is built once per order in `OrderManager._risk_context`, so every
rule sees a consistent snapshot and the option quote is resolved once rather
than per rule.

Three properties worth keeping:

- **Fail closed.** A rule that raises is caught, logged with a trace to
  `exceptions.log`, and converted to `RISK_CHECK_FAILED`. An order that cannot
  be validated is not a safe order.
- **Live requires synced config.** Limits are pushed by the renderer
  (`POST /risk-config`, `stores/useRiskSync.ts`) on startup, on every change,
  and on every reconnect. Until they arrive `configured` is False and live
  orders are refused with `RISK_CONFIG_NOT_SYNCED`. Paper is unaffected.
- **Overrides are declared, never inferred.** "Always Override" and the "Ask Me"
  prompt send `overrideMaxPos: true`, which the engine allows and logs at WARN.
  Mirrors the existing `allowDuplicate` flag.

## Enforced in the sidecar

| Rule | Code | Source setting |
|---|---|---|
| Field enums (side / type / product / validity) | `INVALID_ORDER_FIELD` | — |
| Quantity > 0 | `INVALID_QUANTITY` | — |
| Max Quantity / Order | `MAX_QTY_PER_ORDER` | Execution Defaults |
| Tick grid + minimum price | `INVALID_PRICE` | exchange rule |
| Limit price far from LTP | `PRICE_TOO_FAR` | fat-finger guard |
| Max Price | `MAX_PRICE` | Execution Defaults |
| Max Loss reached | `MAX_LOSS_REACHED` | Risk Defaults / session bar |
| Profit Target reached | `PROFIT_TARGET_REACHED` | session bar |
| Max Trades | `MAX_TRADES_REACHED` | Risk Defaults / session bar |
| Max open positions | `MAX_POSITIONS_REACHED` | Risk Defaults / session bar |
| Max lots on one contract | `MAX_POSITION_LOTS` | Risk Defaults / session bar |
| Emergency halt engaged | `TRADING_HALTED` | kill switch |
| Qty not a whole multiple of the lot size | `INVALID_LOT_SIZE` | instrument master |
| Lot count disagrees with quantity | `LOT_QTY_MISMATCH` | instrument master |
| Duplicate order in flight | `DUPLICATE_PENDING` | — |
| Broker lacks the requested product | `PRODUCT_NOT_SUPPORTED` | broker capability |
| Feed down / quote too old | `FEED_STALE`, `QUOTE_TOO_OLD` | feed health |
| Over the exchange freeze qty, no splitting | `FREEZE_QTY_EXCEEDED` | `broker_limits.json` |
| Order value over the notional cap | `MAX_NOTIONAL` | production guard |
| Strike far from spot | `STRIKE_TOO_FAR` | production guard |
| Too many orders per minute | `RATE_LIMIT` | production guard |

Already enforced server-side before this work, and unchanged: market session
(`market_session.require_open`), contract expiry, duplicate working order and
trading mode.

**Tick grid, minimum price and away-from-LTP previously existed only inside the
paper engine.** A live limit order was never price-checked at all — a fat-finger
price was caught in paper and passed straight to the broker in live. That is now
a pre-route rule covering both engines.

## Deliberately NOT enforced server-side

| Setting | Why |
|---|---|
| Default Quantity / Order Type / Product / Validity | These *seed the ticket*. The chosen values travel on the request; requiring them to equal the default would make the ticket unusable. The values themselves are enum-validated. |
| Entry Price Offset (%) | A price *transformation* applied while composing an order, not a constraint. Re-applying it server-side would double it. The resulting price is still bounded by Max Price, tick grid and away-from-LTP. |
| Execution Delay (ms) | A pre-submit pause the user is meant to be able to abort during. Server-side it would only block a threadpool worker on an order already committed to. |
| Partial Exit Options | Chooses which exit buttons render. The exit *fraction* travels on the request and is range-checked; which buttons exist is not a trading rule. |
| Max Position **behaviour** (ask / block / auto / override) | A server cannot show an "Ask Me" prompt. The *limit* is enforced here; the resolution policy stays in the UI and communicates its decision via `overrideMaxPos`. |
| Hedging (enabled, distance, retry, max retries) | The hedge leg is composed client-side and placed as an ordinary order, so it already passes every rule above. Server-side Max Trades now also caps runaway hedge-retry loops, which nothing did before. |
| SL / Target / Trail values and modes | These produce the `rule` captured on the position; they do not gate placement. *Acting* on them for live positions is blocker #5 in LIVE-READINESS.md. |
| Notifications, Trading Style, profile auto-select | Presentational or config-selection. The resolved values are what get enforced. |

## The live position book

Max Positions / Max Loss / Profit Target need a live book, and the sidecar never
had one. `sidecar/services/live_book.py` builds it from the orders this sidecar
placed and accepted, marked to market off the shared option tick feed.

Known limits, both inherited from blocker #2:

- It does not see fills made in the broker's own app, on another machine, or
  before Charticks started.
- A placement accepted by the broker is recorded as filled. Acceptance is not a
  fill, so a rejected-downstream or part-filled order overstates the book.

A position whose fill price could not be determined (market order, no quote)
contributes **nothing** to P&L rather than its full mark — otherwise it would
read as pure profit and could trip Profit Target on its own.

`LiveBook.reconcile()` is where broker-confirmed positions replace this record
once order-book polling lands.

## Second pass — the full audit (2026-08-12)

The first pass moved the settings-derived limits server-side. A wider audit of
the broker integration and the non-order-entry paths found these, all now fixed:

**Instrument facts were taken from the client.** The sidecar never learned an
instrument's real lot size — `order_splitter` inferred it as `qty / lots`, so a
client that miscounted defined its own truth and a wrong-sized order went to the
broker. `BrokerManager.option_meta()` now reads `lotsize` and `tick_size` from
the instrument master, and `rule_lot_size` rejects a quantity that is not a whole
multiple, or that disagrees with the lot count sent alongside it. Tick size is
now per-instrument rather than a hard-coded 0.05.

**Live had no duplicate guard.** The one-working-order rule and the double-click
backstop lived inside `paper_engine.place()`. Live — the mode where a double-fire
costs money — had neither. `LiveBook.note_submitted()` opens a short window
*before* routing, so a second request racing the first is rejected.

**Three endpoints moved quantity while bypassing validation entirely:**

- `/positions/close` took `fraction` straight off the request body with no range
  check, and a negative value inverted the arithmetic — `close(fraction=-1)`
  **doubled** the position instead of closing it.
- `/positions/adjust` took an unbounded `delta` and grew a position without going
  near `place_order`, skipping every limit. `+9999` was accepted.
- `/positions/roll` accepted any strike and entry price, silently defining the
  new leg's cost basis and (through `_compute_risk`) its SL and target.

**The kill switch was decorative.** The button had no click handler, nothing ever
emitted `risk_event`, and no code path consulted it. It is now
`services/kill_switch.py`: server-owned, sticky, replayed to reconnecting
clients, and enforced as the first rule. It blocks new entries only — exits stay
available, because a halt that traps you in a position is not a safety feature.
It does not square off; that belongs behind its own confirmation.

**ICICI silently substituted the product.** Breeze has no intraday options
product, and `_place_icici` logged a warning and placed MIS orders as
carry-forward — turning an intraday trade into a positional one. Now rejected by
`rule_broker_capability`.

**Freeze quantity was applied but not validated.** `cap_qty()` returns None when
a broker cannot split, which is right for the splitter but hid the limit from
validation: an oversized order at a non-splitting broker was sent whole for the
exchange to reject. `hard_cap_qty()` exposes the cap itself.

Added as production guards, configurable and off unless set (except the rate
limit, which defaults on): per-order notional cap, orders-per-minute throttle,
strike-vs-spot sanity, and a maximum quote age.
