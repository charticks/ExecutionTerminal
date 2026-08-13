# Order idempotency

_Implemented 2026-08-13. Package: `sidecar/services/idempotency/`._

One live order per intent, whatever the network, the process or the user's mouse
does. The guarantee covers network timeouts, application restarts, double-clicks
and transient communication failures.

```
Order Engine ──► guard.claim()  ──► BrokerIdem (per broker) ──► broker order book
   (_submit_once)      │
                       └──► ClaimStore ──► %APPDATA%/Charticks/data_cache/
                                           idempotency_<date>.jsonl
```

## Where it attaches

`OrderManager._submit_once` is the **only** place in the engine that calls a live
placement API — entries, exits and every child of a split order all funnel
through it — so it is the only place idempotency has to be enforced. Adding a
broker never touches it: a broker joins by registering a `BrokerIdem` in
`idempotency/brokers.py`, exactly as it registers a margin checker and an
order-book reader.

## Three tiers, one interface

| Broker | Client id sent as | Reconciled by | Tier |
|---|---|---|---|
| **Dhan** | `tag` → `correlationId` | `get_order_by_correlationID` | `native` |
| **Kotak** | `tag` → body `ig` | order-book echo (`GuiOrdId`) | `tag-echo` |
| **ICICI** | `user_remark` | order-book echo | `tag-echo` |
| **Angel** | *(SmartAPI has no such field)* | contract + side + quantity | `attribute` |

Every placer receives the same `client_order_id=` argument; the SDK keyword lives
in the placer with the rest of that broker's vocabulary, and Angel simply ignores
it. The tier is reported rather than hidden, so a weaker broker is never
presented as a guarantee it cannot give.

## The client order id

`CH` + 11 hex of a SHA-256 fingerprint + a 2-digit attempt — 15 characters,
alphanumeric, inside every broker's documented limit (Dhan's is 25).

The fingerprint covers account, broker, contract, side, quantity, order type,
price, product, validity and split-leg index. It deliberately **excludes time**:
two clicks a second apart must produce the *same* fingerprint or the second is
not recognisable as a duplicate. Separating a genuine second order from a
double-click is the retry window's job, not the hash's — a time-bucketed hash
would let a duplicate through whenever two clicks straddled a bucket boundary.

`POST /orders/place` also accepts `clientRequestId`. When a client reissues the
same id on retry that is the strongest possible signal — it *knows* the two
requests are one intent — so it replaces the derived fingerprint. It is optional;
without it the parameters are fingerprinted, which still catches double-clicks
and restarts.

## Holding, not deciding — and the override

A second identical order inside the retry window is **held, not silently
absorbed**. Whether a repeat is a stray click or a deliberate add is a judgement
only the user can make, so the sidecar asks:

| Code | Meaning | Overridable |
|---|---|---|
| `DUPLICATE_ORDER` | An identical order went in moments ago; Charticks knows it exists and names it. | yes |
| `IDEMPOTENCY_UNRESOLVED` | An earlier attempt was sent but never acknowledged; nobody knows if it is live. | yes |

Both come back with `overridable: true`, `duplicateOf`, `placedSecondsAgo`,
`symbol`/`side`/`qty` and a message written to be read while deciding whether to
double a live position. `DuplicateOrderDialog` renders them, with **Cancel as the
focused default** — the safe choice should be the one an absent-minded Enter press
takes. The two are worded differently on purpose: the unresolved variant asks the
user to open their broker's order book first, because that is the one check
Charticks cannot perform.

The override travels as `overrideDuplicate` on the single request, exactly like
`overrideMaxPos`. It is never inferred and never a stored preference, so a
confirmation cannot leave duplicate protection switched off — a third click is
held again immediately. An overridden order gets a **fresh client order id**
(attempt+1), so a native-tier broker does not reject it as a repeated
correlationId.

Overriding an `IDEMPOTENCY_UNRESOLVED` block also **resolves that claim**,
recorded as `resolved by explicit user override`: the user has done what the
software could not, and leaving the claim open would block every later attempt.
Every override is logged at WARN to `logs/orders.log` with the reason.

A reconciliation that *finds* the lost order is different again — no dialog. The
user asked once, the order exists, and it is adopted and reported as the success
it is.

## Answering "no" is not the same as "don't know"

`Resolution` has three values and **UNKNOWN is not ABSENT**:

| | meaning | action |
|---|---|---|
| `FOUND` | the order is at the broker | adopt its id, send nothing |
| `ABSENT` | positively not there | safe to send, reusing the same client id |
| `UNKNOWN` | cannot tell | **refuse to send** |

An unreachable broker, an unparseable response, or an order book that came back
empty (indistinguishable from a failed read) are all UNKNOWN. Collapsing them
into ABSENT is how an idempotency layer causes the duplicate it exists to
prevent. This is the same fail-safe policy as pre-trade margin: "we could not
check" and "you cannot" have the same correct outcome.

On the attribute tier, anything that *looks* like the order is treated as the
order. A false FOUND costs a resend the user can make deliberately; a false
ABSENT costs them a duplicate live position.

## Why the journal is on disk

"A restart must not duplicate an order" cannot be satisfied in memory. The
dangerous sequence is: send, lose the answer, restart, user presses Buy again.
Only a record that outlived the process can tell the second attempt that the
first may be live.

One JSON object per line, appended and **fsync'd** before the SDK call returns —
append-only because a torn tail line is recoverable (skip it) whereas a
partially rewritten record is not, and fsync'd because the failure this exists
for is exactly the one that loses buffered writes. Files are per-day, pruned to
seven. If no writable location exists, the guard degrades to in-memory (which
still stops double-clicks) and logs that restart protection is OFF — trading does
not stop because a disk is full.

## State machine and the two windows

```
CLAIMED ──► PLACED     broker returned an id
    │  └──► FAILED     broker explicitly rejected it   → retry is legitimate
    └─────► CLAIMED    outcome never learned           → UNRESOLVED
```

The asymmetry that makes this safe:

- A **PLACED** claim holds an identical order only inside `RETRY_WINDOW_S`
  (120s — the shipped default; a future release may make it configurable),
  measured from *placement*, not from last write — keying it to the
  update time silently extended the window every time the claim was touched.
  Past the window an identical order is a new order the user can see in their
  book and legitimately wants; it gets attempt+1 and a fresh client id.
- An **UNRESOLVED** claim blocks regardless of age until it is reconciled,
  because "probably didn't happen" is not a basis for sending a live order.

## Tied to the Order Synchronization Engine

The reconciliation readers hit the **same order-book endpoints the sync engine
polls**, so a broker that can be synchronized can be reconciled — the two cannot
drift apart. They read raw rows rather than the sync engine's normalized
`BrokerOrder`, deliberately: that mapper *drops* a row whose status word it does
not recognise, and a dropped row would read as "order absent" and authorise a
duplicate. Here an unrecognised order must still count as an order.

`TrackedOrder` carries its `client_order_id`, and when the sync engine sees an
order reach a terminal state it calls `guard.note_terminal()`. That closes the
loop: an order whose acknowledgement was lost but which the poller later sees
fill resolves its claim, instead of blocking identical orders forever.

## Inspecting it

`GET /orders/idempotency` returns the claim summary and, importantly, every
unresolved claim with its age. An unresolved claim is the one state that will
refuse a later order, so it must be visible without reading a log file. Every
suppression, block and unknown outcome is also written to `logs/orders.log`.

## Not covered

- **Modify and cancel are not idempotent.** They are addressed to a broker order
  id that already exists, so a repeat is at worst a no-op at the broker rather
  than a second position — a materially smaller hazard than duplicate placement.
- **Fills made outside Charticks** remain invisible to the position book, so an
  order placed in the broker's own app is not a duplicate this can see.
- The attribute tier (Angel) cannot distinguish two *intentionally* identical
  orders placed within its match window from one order and a retry. It resolves
  that ambiguity toward "already placed".
