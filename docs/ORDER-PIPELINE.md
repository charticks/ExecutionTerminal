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
Broker order placement      order_manager._place_with_splitting
     ↓
Order synchronization       services/order_sync/             (per broker)
     ↓
Position booked             services/live_book.py            (confirmed fills only)
```

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

## Not yet built

- **Idempotency keys (Phase 2).** Deliberately deferred: a client order id is
  only useful once there is a synchronization layer to reconcile it against.
  That layer now exists, so this is the natural next step. Note that Dhan
  already exposes `get_order_by_correlationID`, which is the hook for it.
- **Fills made outside Charticks.** The sync engine reads the broker's order
  book but only tracks orders it placed, so a trade made in the broker's own app
  is still invisible to the position book.
