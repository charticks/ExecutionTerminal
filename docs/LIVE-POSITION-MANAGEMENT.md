# Live position management

_Engine: `sidecar/services/live_manager.py`. Rebuilt 2026-08-13 into a
broker-independent, restart-safe, self-monitoring subsystem._

```
Order Request
     ↓
Broker Order Placement          order_manager
     ↓
Broker Order Synchronization    order_sync/            (polls the broker's book)
     ↓
Broker Confirms Fill ─────────┐
                              ↓
Broker Position Book ──→ position_reconciler ──→ live_book ──→ live_store (disk)
                                                     │
                                                     ↓
                              Live Position Management   live_manager
                              ├── Stop Loss
                              ├── Target
                              ├── Trail Stop Loss
                              ├── Portfolio Trail Profit
                              ├── Auto Square-Off
                              ├── Market-data subscription for every position
                              └── Monitoring alarm
```

The engine is the single source of truth for **how** every live position is
being managed, whichever broker placed it. The broker's own position book is the
single source of truth for **what** is held. Reconciliation applies the second
to the first, continuously.

---

## 1. Canonical Instrument Key

Every broker names the same contract differently. Angel calls NIFTY 28AUG2026
25000 CE token `43492`; Dhan calls it a securityId; Kotak a trading symbol;
ICICI states it as four separate fields. Position management used to be written
in Angel's vocabulary, and that single fact caused the worst class of failure
this system can have — see §7.

`InstrumentKey` (`sidecar/services/instruments.py`) is derived from the
contract's own economics:

```
underlying   NIFTY | SENSEX | CRUDEOIL | …
segment      OPT | INDEX | FUT
expiry       28AUG2026            (broker-neutral)
strike       25000                (whole rupees)
opt_type     CE | PE
```

Two brokers quoting or holding the same option necessarily produce the same key,
so one contract is one entry everywhere: one tick cache slot, one position, one
subscription, one row in the UI.

**Serialised form** — `key.position_id` → `NIFTY|28AUG2026|25000|CE`. This is
the position id used by the live book, sent to the renderer, written to disk and
accepted back by `/positions/adopt` and `/positions/close`.
`InstrumentKey.from_position_id()` rebuilds the key **with no broker connected**,
which is what makes restart recovery possible before login.

**Resolving a broker's position row to a key** (`services/broker_positions.py`),
strongest first:

1. the broker's token, via the shared instrument registry (exact — the registry
   was built from that broker's own scrip master);
2. the trading symbol, parsed (`InstrumentKey.from_symbol`);
3. nothing — the row is reported as a foreign leg the user can see but Charticks
   cannot manage.

A symbol that parses two ways (`NIFTY28AUG2625000CE` can be read as year 26 +
strike 25000 or year 2625 + strike 000) returns **None** rather than a guess.
Exits are sized and routed from this key; a wrong match would trade a contract
the user does not hold.

Adding a broker means adding a normaliser in `broker_positions.py` and a feed.
The management engine is untouched — it contains no broker name at all.

---

## 2. Market-data subscriptions

A feed's option subscription is a *replace*: hand it a set of contracts and
everything else is unsubscribed. The option chain was the only caller, so
switching the chain from CRUDEOIL to BANKNIFTY unsubscribed the contract an open
CRUDEOIL position was managed on, and its stop stopped evaluating.

`services/subscriptions.py` interposes one hub. Nothing subscribes directly any
more; each source *declares* what it needs and the hub sends the union:

```
option chain window ─┐
live positions      ─┼─→ union ─→ serving feed
paper positions     ─┘
```

Consequences: changing index, expiry, or workspace can no longer unsubscribe a
position; a contract is only dropped when **no** source wants it; and
`/market-feed` reports which source asked for what.

`reassert()` re-sends the current union even when unchanged — required after a
reconnect, where the desired set is identical but the feed has forgotten
everything.

---

## 3. Two evaluation paths

**Tick-driven.** Every option tick marks its position to market and evaluates
immediately. Reaction is as fast as the feed.

**Periodic** (`EVAL_INTERVAL_S`, 750 ms). The safety net. Ticks stop for reasons
that have nothing to do with the market — a socket dies, a subscription is
dropped, a feed fails over, a contract is illiquid. Each cycle:

1. re-declares the subscriptions every open position needs;
2. re-reads the quote cache for every position (so a missed tick cannot leave a
   stale mark);
3. evaluates every armed position's SL / Target / Trail and the portfolio trail;
4. recomputes every position's monitoring state and raises or clears the alarm.

A stop that only runs when a price happens to arrive is not a stop.

The same cycle drives `paper_engine.sweep()` in paper mode, because the paper
book had the identical exposure.

---

## 4. Persistence and reconciliation

### Persistence (`services/live_store.py`)

The book is written to `<CHARTICKS_DATA_DIR>/data_cache/live_book.json` on every
change: temp file → `fsync` → atomic `os.replace`, with the previous good file
kept as `.bak`. Writes are debounced (500 ms) off the tick path; a fill, an
adopt, a manual risk edit and a removal flush **synchronously**.

Persisted per position: contract, side, quantity, lots, average entry, the risk
rule, the derived SL / Target / trail, and the trail's own baseline — so a
trailed stop comes back where it had trailed to, not where it started.

### Restore

At startup (`server.py` → `reconciler.restore_and_start()`), every position is
restored in state `restoring` with its `verified_ts` **cleared**. That timestamp
is the arming condition for automation, and a timestamp from before the restart
is evidence about a process that is no longer running. So:

> Nothing is auto-exited against a restored position until a broker confirms it
> still exists.

Exiting a position that was closed while Charticks was down would not close
anything — it would open a new position in the opposite direction.

Unreadable rows are skipped and counted, never half-restored: a position with
some of its risk state would look managed and behave as if it had no stop.
Session counters (trades today, realised P&L) are restored only for the same
trading day; positions are restored regardless of day, because a carried-forward
position is still held.

### Reconciliation (`services/position_reconciler.py`)

Every 4 seconds, and immediately after any fill:

| Broker book | Live book | Result |
|---|---|---|
| ✓ | ✓ | Management resumes/continues. The **broker's quantity wins** — exits are sized from it — and lots are re-derived. |
| ✓ | ✗ | An **unmanaged** position. Recorded, displayed, counted toward exposure, never given an invented stop. The user adopts or ignores it. |
| ✗ | ✓ | The position is gone at the broker → removed. |

The removal path is deliberately conservative, because the two error directions
are not symmetric — wrongly keeping a position costs a rejected exit; wrongly
dropping one silently disarms a live stop:

- only after a **successful** read of **every** connected account (a failed poll
  is not evidence of absence);
- never on the first clean miss (`MISSES_BEFORE_DROP = 2`);
- never within 20 s of the position being opened or last confirmed (a broker's
  position book can lag its own fill);
- never while an exit we sent is still in flight — that fill closes the position
  properly, with its realised P&L.

### Exposure vs P&L limits

Positions opened outside Charticks **count toward Max Positions** (exposure the
broker is carrying is exposure, whoever opened it — otherwise the limit is
doubled by placing half the trades in the broker's own terminal) but are
**excluded from session P&L** (Max Loss and Profit Target measure this session's
trading; halting because of a position Charticks neither opened nor manages is a
limit the user cannot act on).

---

## 5. Managed vs unmanaged, and the monitoring alarm

Each position carries one monitoring state, computed every cycle, ordered by
severity so the trader sees the most urgent condition that applies:

| State | Meaning | UI |
|---|---|---|
| `protected` | Managed, rule armed, live quote flowing | 🟢 Managed |
| `no_rule` | Managed, but the user set no SL/Target | 🟡 No SL / Target |
| `exiting` | An exit is at the broker | ⏳ Exiting |
| `restoring` | Restored/adopted, awaiting broker confirmation | ⏳ Confirming with broker |
| `feed_lost` | No market data: unsubscribed, feed down, contract unlisted, or no price for 90 s in market hours | ⚠ Feed Lost — Automation Paused |
| `paused` | Automation is not running (Charticks is in Paper mode, or the engine stopped) | ⚠ Monitoring Paused |
| `unmanaged` | A broker position Charticks is not managing | 🔴 Not Protected |

Everything except `protected` and `no_rule` raises the **monitoring alarm**: a
`monitor_alarm` event, a persistent (non-dismissible, non-toast) banner above
the Positions grid naming each position and its reason, an `Unprotected : n`
counter in the header, a tinted row, and a `warn`-level line in `orders.log` on
every transition. It clears itself when monitoring resumes.

The UI rule this enforces:

> **No live position ever renders identically to a managed one unless Charticks
> is actively monitoring it.** A row with no monitoring state at all is drawn as
> unmanaged — silence never reads as "protected".

Diagnostics: `GET /positions/monitor` answers the whole question in one place —
engine running, last cycle, per-position alarm states and reasons, the
subscription union by source, and reconciliation health.

---

## 6. Adopting a position

`POST /positions/adopt {id, rule}` (the **Manage** button on an unmanaged row).
The rule is applied against the broker's cost basis exactly as if the position
had been opened with it, so an adopted position is managed on identical terms to
a native one — there is no second, weaker kind of management. The dialog states
the entry price the stop will be derived from before anything is armed.

`POST /positions/ignore` is the inverse: management stops, the position stays
visible and plainly marked unmanaged. It is never hidden — it is still real
exposure.

Adoption is always explicit. Charticks never assumes a position it finds at a
broker is its own.

---

## 7. What this replaced

Four failures, all of which presented as a completely normal Positions tab:

**Management was tied to Angel.** `live_manager` translated every tick through
`instruments.token_for("angel", key)`. With Kotak, Dhan or ICICI connected and no
Angel session, that returned `None`, the tick handler returned early, and every
stop loss, target and trail silently stopped evaluating. The paper engine had
the identical bug in `_on_tick`, so paper stops and pending limit fills died the
same way. Both are now keyed canonically end to end.

**The book was RAM only.** A crash or restart destroyed every SL, Target and
Trail while the broker still held the position — and because the tab was driven
by the broker's book, the row came back looking exactly as before.

**Switching the option chain unsubscribed open positions.** No ticks, no stop.

**Broker-opened positions were indistinguishable from managed ones.** Worse: the
broker poller and the live book both published `position_update` for the same
contract under different ids, so a managed position could appear twice and
neither row could say whether anything was protecting it. There is now exactly
one publisher of live position rows.

---

## Safety properties

**Confirmed quantity only.** Exits are sized from `LivePosition.qty` —
cumulative confirmed fills, reconciled against the broker's book. A 150-lot
entry that fills 75 is managed at 75.

**One exit in flight per position.** `begin_exit()` claims the quantity *before*
the order is routed. Without it, a stop breached at 10:00:00 fires again on every
tick for the two seconds the broker takes to confirm.

**A failed exit re-arms.** A rejected exit releases its claim immediately; one
that never reaches a terminal state is released after 60 s and logged.

**Nothing acts on an unconfirmed position.** `verified_ts > 0` is the single
arming condition, set by a confirmed fill or by reconciliation.

**Unmanaged positions are never auto-exited.** Not by SL, Target, Trail or the
portfolio trail — which also excludes them from its P&L, so a book-level rule
cannot close a trade the user is running elsewhere. `Square Off All` *does*
include them: it is an explicit action on real exposure, and it says so in the
confirmation.

**Exits are never blocked by entry-side rules.** Kill switch, Max Loss, Profit
Target, Max Positions, Max Trades, notional, strike sanity, feed staleness and
the duplicate guard all stand down for an exit (`OrderContext.is_exit`), and no
margin check runs. A halt that blocked exits would trap the user in the position
they were trying to escape.

**Positions with no rule are never auto-exited.** A trader who turned Stop Loss
off chose to trade without one.

**Trailing runs before the stop check** on every evaluation: a tick that both
earns a trail step and breaches the new stop must be handled in that order.

**Averaging in re-derives SL and Target** from the new cost basis.

**Semantics are shared with paper** (`_compute_risk`, `_trail_of`, `_steps`), so
the two engines cannot drift apart in what a rule means. A rule is captured at
entry and never re-read from Settings.

---

## Verified workflows

`python sidecar/tests/test_live_position_management.py` — 88 checks, real
modules, stubbed brokers. **88 passed, 0 failed.**

**Canonical key** · position id round-trips · Angel-style compact tradingsymbol
parses · display-form symbol parses · four-digit-year symbol parses · an equity
symbol is not guessed at · case/format variants produce one key.

**Subscriptions (B3)** · a position's contract is subscribed · it *stays*
subscribed after the chain switches to another index · the chain's own contract
is released when only the chain wanted it.

**Broker independence (B2)** · with Angel holding no binding at all: SL and
Target derive at entry · a canonical tick fires the stop · the exit is sized
from confirmed quantity · no second exit while the first is unconfirmed · a tick
from a Kotak/ICICI/Dhan feed drives the same position.

**Periodic evaluation (B3)** · with **no tick delivered at all**, the timer marks
to market and fires the stop · trail steps 110→SL 90, 120→SL 100, 130→SL 110 ·
an exit fires against the trailed stop, not the original.

**Restart recovery (B1)** · a trailed stop is written to disk · after wiping
memory the position, its trailed SL, target and trail rule all restore · the
restored position is **not** armed · a price far through the stop produces **no**
exit while unconfirmed · the alarm names it · reconciliation confirms it · the
stop then fires · the alarm clears.

**External positions (B4)** · a broker-only position is recorded, unmanaged,
with no invented stop · it counts toward exposure · it is excluded from session
P&L · a price collapse never auto-exits it · it raises the alarm · it is still
subscribed for market data (its P&L is live) · adopting it derives the stop from
the broker's entry and arms it · the next cycle enforces it · releasing it
disarms and clears the stop.

**Reconciliation** · the broker's quantity wins and lots are re-derived · a
failed broker read never drops a position · a position is not dropped on the
first clean miss · it is dropped on the second · a foreign (equity) leg is
displayed, marked unmanaged, and never enters the live book.

**Monitoring** · a healthy position reads `protected` with no alarm · feed down →
`feed_lost` + alarm · Paper mode with a live position open → `paused` + alarm ·
an unsubscribed contract → `feed_lost`, and the next cycle re-subscribes it · a
position with no rule is never exited and never alarms.

**Fills** · managed at the filled size, not the requested one · a second partial
averages in · the exit is sized from the full confirmed quantity.

**Session counters** · same-day trade count restores · yesterday's does not ·
yesterday's position still restores.

**Square Off All** · closes managed and unmanaged positions · a short is exited
on the opposite side.

**Damaged state** · a truncated JSON book does not crash the restore and
restores nothing · good rows survive a bad neighbour, and unreadable rows are
counted rather than guessed.

**Exit claims** · a stop fires once · repeated cycles do not re-fire it · a claim
that times out re-arms it.

**Direction disagreement** · when the broker reports the opposite side, the side
follows the broker, the old side's stop is discarded rather than left pointing
the wrong way, the rule is re-derived against the broker's cost basis, and the
flip itself fires no exit.

Also verified: the sidecar boots with the new startup path, the management cycle
runs at ~750 ms, and `/positions/monitor`, `/live-book` and `/market-feed`
answer with the new fields.

---

## Remaining limitations

**Not yet exercised against a live broker in market hours.** Every test above
uses stubbed broker SDKs. The per-broker position normalisers for Kotak and
ICICI in particular need one real session each: their field names are documented
with fallbacks, so a shape drift shows up as a skipped row (reported as a foreign
leg) rather than a wrong number, but that is a safety net, not a substitute for a
live read.

**Exits are market orders.** No limit-exit or slippage control.

**Adoption uses the instrument's current defaults.** There is no per-position
rule editor at the moment of adopting; adjust SL/Target inline on the row
afterwards.

**Multi-account netting is by contract, not by account.** A contract held in two
execution accounts is netted into one canonical position, which is how the book
has always recorded fan-out orders. Exits fan out the same way. An account-level
position view is not modelled.

**Foreign legs are display-only.** Equity, futures and any symbol that does not
resolve to an option contract are shown and marked unmanaged, but cannot be
adopted or squared off from Charticks.

**No exchange holiday calendar** (unchanged): on a holiday `market_session`
resolves to open and the rejection comes from the broker.

**The paper book is still in-memory.** Paper positions do not survive a restart.
Live is the one that had to be durable; paper's own persistence is a follow-up.

**Portfolio Trail Profit peak state is not persisted.** After a restart the peak
re-arms from the current book rather than from the session's high-water mark.

**An exit in flight across a restart is not tracked.** The exit claim is
deliberately not persisted (a claim restored from disk would disarm a stop with
no order behind it). If Charticks dies between sending an exit and its fill, the
restarted process re-arms the stop and reconciliation decides what is left: if
the exit filled, the position is gone and nothing happens; if it part-filled, the
remainder is managed. Automated exits are market orders, so a resting duplicate
is not a realistic outcome — but it is not impossible, and `logs/orders.log` is
the record if it ever is.
