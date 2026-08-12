# Live position management

_Implemented 2026-08-12. Engine: `sidecar/services/live_manager.py`._

```
Order Request
     ↓
Broker Order Placement          order_manager
     ↓
Broker Order Synchronization    order_sync/          (polls the broker's book)
     ↓
Broker Confirms Fill
     ↓
Create / Update Position        live_book            (confirmed quantity only)
     ↓
Live Position Management        live_manager
     ├── Stop Loss
     ├── Target
     ├── Trail Stop Loss
     ├── Portfolio Trail Profit
     ├── Auto Square-Off
     └── Future automation
```

Every automation reads `live_book`, and `live_book` is written from exactly one
place: `order_sync` booking a broker-confirmed fill. There is no other path into
the live position book, which is what makes the safety rule structural rather
than a convention to remember.

## Why this could not be built earlier

Automation needs to know what is actually held. Until order synchronization
existed, a live position was created the moment a broker returned an order id —
so a stop could have fired against quantity that was never filled, sending an
exit for something the user did not own, or for the wrong size. The order id was
an *acknowledgement*, not a fill.

## Safety properties

**Confirmed quantity only.** Exits are sized from `LivePosition.qty`, which is
cumulative confirmed fills. A 150-lot entry that fills 75 is managed at 75.

**One exit in flight per position.** `begin_exit()` claims the quantity *before*
the order is routed. Without it, a stop breached at 10:00:00 would fire again on
every tick for the two seconds the broker takes to confirm — one stop-loss
becoming several.

**A failed exit re-arms.** A rejected exit releases its claim immediately; one
that never reaches a terminal state is released after 60 seconds and logged. A
lost order id must not silently leave a position unprotected for the rest of the
session.

**Exits are never blocked by entry-side rules.** The kill switch, Max Loss,
Profit Target, Max Positions, Max Trades, notional, strike sanity, feed
staleness and the duplicate guard all stand down for an exit
(`OrderContext.is_exit`), and no margin check runs. Closing a long needs no
margin and closing a short releases it — and a halt that blocked exits would
trap the user in the position they were trying to escape, while disabling every
stop loss the moment it was engaged. Structural and price checks still apply.

**Positions with no rule are never auto-exited.** A position opened without SL /
Target simply has none; the manager leaves it alone rather than inventing one.
Same contract as the paper engine: a trader who turned Stop Loss off chose to
trade without one.

**Paper is untouched.** The paper engine owns the paper book; `LiveManager`
returns immediately unless the mode is live.

## Semantics

SL / Target / trail arithmetic is **shared** with the paper engine
(`_compute_risk`, `_trail_of`, `_steps`), so paper and live cannot drift apart
in what a rule means. A rule is captured at entry and never re-read from
Settings, so changing a default affects future trades only.

Trailing runs before the stop check on each tick: a tick that both earns a trail
step and breaches the new stop must be handled in that order, or the position
exits at a stop it had already outgrown.

Averaging into a position re-derives SL and Target from the new cost basis —
otherwise a stop set for the first fill sits at the wrong distance for the
combined position.

## Verified behaviour

```
entry 100, SL 20pts, Target 40pts, trail 10/10
  → sl 80.00  target 140.00

LTP 79   → exit SELL 150 stop-loss
  two further ticks while unconfirmed → 0 additional exits
  claim released                      → fires again
LTP 141  → exit SELL 150 target
LTP 110  → SL 90      LTP 120 → SL 100      LTP 130 → SL 110
LTP 99   → exit SELL 150 stop-loss (against the trailed stop)
no rule, LTP 1                        → 0 exits
portfolio trail: peak +4500 armed, give-back 1800 > 1000 → square-off
```

Safety chain:

```
order accepted, not yet filled         → 0 positions,  0 exits
price collapses to 1 before any fill   → 0 exits
broker reports PENDING                 → 0 positions,  0 exits
broker confirms 75 of 150              → qty 75, sl 80.00
SL breached                            → exit sized 75, not 150
exit while halted + loss limit + cap   → allowed
new entry while halted                 → TRADING_HALTED
```

## Known limits

- **Fills made outside Charticks are invisible.** The book is built from orders
  this sidecar placed. A trade made in the broker's own app is not managed, and
  not counted toward limits.
- **Exits are market orders.** No limit-exit or slippage control yet.
- **Live modify / cancel is still unported.** Automated exits are fresh market
  orders so they are unaffected, but a resting live limit order cannot be
  amended from Charticks.
- **The manager is tick-driven.** With no ticks — a dead feed, outside market
  hours — nothing evaluates. This is the same exposure the paper engine has, and
  the reason the feed-staleness rule exists on the entry side.
