# Live order pipeline

_Margin validation + order synchronization, 2026-08-12._

```
Order Request
     ↓
Trading-mode check          order_manager._resolve_mode      (two-key)
     ↓
Server-side risk validation services/risk_engine.py          (16 rules)
     ↓
Pre-trade margin validation services/margin/                 (per broker)
     ↓
Idempotency claim           services/idempotency/            (per broker)
     ↓
Broker order placement      order_manager._submit_once       (the only seam)
     ↓
Order synchronization       services/order_sync/             (per broker)
     ↓
Position booked             services/live_book.py            (confirmed fills only)
```

Idempotency sits last before the wire on purpose: a claim is only worth taking
for an order that has already passed every check and is genuinely about to be
sent. See `docs/IDEMPOTENCY.md`.

Both new layers follow the same shape as the rest of the sidecar: a
broker-independent core plus a per-broker adapter that registers itself. Adding a
broker is a new module and a `register()` call — the Order Engine never changes.

## Pre-trade margin validation

```
Order Engine → MarginEngine → angel.py | dhan.py | kotak.py | icici.py
```

Each adapter implements `(session, MarginRequest) -> MarginQuote`, and either
returns a quote it stands behind or raises `MarginUnavailable`. There is no
third outcome.

**Fail-safe.** A timeout, an API error, an unparseable response, or a broker with
no checker all reject the order. "We could not check" and "you cannot afford it"
have the same correct outcome, so they are treated identically.

**All-or-nothing.** Every enabled execution broker must pass. One failure rejects
the whole order — a partial fan-out would leave an unbalanced position nobody
asked for.

**Timeout.** 6 seconds, enforced with a daemon thread rather than a
`ThreadPoolExecutor`: the executor's context manager joins its workers on exit,
which made the bound meaningless against a hung SDK call (a 1 s timeout still
took 30 s to return). A daemon thread can be abandoned.

**Requirement vs. available.** Available cash always comes from the broker and
never has a fallback. The requirement prefers the broker's margin calculator; if
that is unavailable, a **long** option falls back to the premium debit
(`price × qty`), which is arithmetic rather than an estimate. A **short** option
has no fallback — SPAN + exposure cannot be derived locally — so it is rejected.
Every log line records whether the figure was quoted or derived.

| Code | Meaning |
|---|---|
| `INSUFFICIENT_MARGIN` | Required exceeds available; includes the shortfall |
| `MARGIN_CHECK_TIMEOUT` | Broker did not answer in time |
| `MARGIN_CHECK_UNAVAILABLE` | API error, bad response, or no checker for that broker |

## Order synchronization

```
Order Engine → OrderSyncEngine → angel | dhan | kotak | icici  (order-book readers)
```

Canonical lifecycle: `NEW → SUBMITTED → ACCEPTED → PENDING → PARTIAL → FILLED`,
with `REJECTED` / `CANCELLED` / `EXPIRED` as alternative terminals. Each broker
maps its own vocabulary onto it in `order_sync/brokers.py`; nothing above that
file sees a broker's status strings.

Three properties worth keeping:

- **Positions are booked only on confirmed filled quantity.** Placement no
  longer books anything. Partial fills book exactly what filled, and only the
  *increment* since the previous poll, so a repeated row cannot double-count.
- **Transitions are monotonic.** Broker order books are eventually consistent
  and can return a stale row after a fresher one; `advances()` refuses to walk a
  filled order back to pending and re-emit its fills.
- **Unknown statuses hold state.** A status word the mapper does not recognise
  leaves the order where it was rather than inventing a state, so an SDK that
  renames a value degrades to "no update" instead of to a wrong update.

Polling is an implementation detail of the *source*, not of the engine. A broker
with an order WebSocket (Kotak exposes `subscribe_to_orderfeed`) implements the
same `OrderSource` and calls `order_sync.ingest(...)` directly; the engine, the
position book and the UI are unaffected. The poller only runs while something is
open, so an idle sidecar makes no broker calls.

Every transition and every booked fill is written to `logs/orders.log` with the
broker's own raw status alongside the canonical one — when a mapping turns out to
be wrong, that raw value is the only way to see it after the fact.

## Amending a working order

_Added 2026-08-13. All four connected brokers place, modify and cancel live._

```
Order Engine → _LIVE_PLACERS    → angel | kotak | icici | dhan
             → _LIVE_MODIFIERS  → angel | kotak | icici | dhan
             → _LIVE_CANCELLERS → angel | kotak | icici | dhan
```

Three dispatch tables, one entry per broker each; adding a broker stays one
method plus one table entry, and a broker missing from a table gets a clear "not
available yet" rather than a crash.

**Modify restates the order, not the change.** Every broker's modify API wants
the whole order back — Angel and Kotak both require product, order type and
validity — so the fields the user did not touch are read from the tracked order
rather than defaulted. Defaulting them silently rewrote an MIS order as NRML.
This is why `TrackedOrder` carries `product` / `order_type` / `validity`.

**A price on a modify implies a limit.** Amending the price of a MARKET order
converts it to LIMIT rather than dropping the price, which is what the user
asked for by typing one.

**Quantity may arrive as lots**, and is multiplied by the tracked `lot_size` —
the same unit the broker's per-order limit is expressed in.

**Amends resolve to the orders that really exist.** `order_sync.resolve()` maps
the id the UI holds to live, non-terminal orders. A split order resolves to
**every child**, so a cancel cannot pull one leg of three and leave an
unbalanced position; a partial outcome is reported as `PARTIAL_AMEND` naming how
many legs succeeded, never as the first error alone.

**Not-working and not-known are different answers.** An already-filled order
returns `ORDER_NOT_WORKING`; an id Charticks never placed returns
`UNKNOWN_ORDER`. Neither reaches a broker.

**Cancel is allowed on an account that is no longer opted in to execution**
(`BrokerManager.live_session` deliberately ignores the Execute flag). Same
principle as the kill switch permitting exits: a control that traps you in a
working order is not a safety feature.

**No broker reports a rejection by raising.** SmartAPI returns
`{"status": false}`, NeoAPI returns `{"Error": …}` or `{"stat": "Not_Ok"}`,
dhanhq returns `{"status": "failure"}` (and can return `orderStatus: REJECTED`
inside a 200), Breeze returns a non-200 `Status`. Each adapter checks its own
shape, because "did not raise" is not "was accepted".

**State is never written by the amend path.** On success it calls
`order_sync.poll_soon()`; the broker's own order book remains the only authority
on what an order became.

## Not yet built

- **Idempotent modify / cancel.** Placement is idempotent (see
  `docs/IDEMPOTENCY.md`); amendments are not. They address an existing broker
  order id, so a repeat is at worst a no-op rather than a second position.
- **Fills made outside Charticks.** The sync engine reads the broker's order
  book but only tracks orders it placed, so a trade made in the broker's own app
  is still invisible to the position book.
- **Resizing a split order.** A price amend applies to every leg, but a quantity
  change is refused (`SPLIT_QTY_NOT_MODIFIABLE`) — one new quantity has no single
  correct meaning across legs, and applying it per leg would multiply the
  position. Cancel and re-place is the honest path.
