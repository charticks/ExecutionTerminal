# Live trading readiness

_Assessed 2026-08-11 against commit `f738e88`._

**Verdict: not ready to enable live order execution.** Paper mode is in good
shape — the tick-driven engine, the market-session gate and the expiry gate are
all real and server-authoritative. The live path is thinner than the UI around
it implies, and several of the findings below can lose money without the user
doing anything wrong.

> **Update 2026-08-12** — **all five blockers are resolved.** Also added since
> the original review: pre-trade margin validation (`sidecar/services/margin/`),
> a real kill switch, server-side risk enforcement
> (`docs/RISK-ENFORCEMENT.md`), full disk-backed diagnostics
> (`docs/LOGGING.md`), order synchronization and live trade management
> (`docs/ORDER-PIPELINE.md`, `docs/LIVE-POSITION-MANAGEMENT.md`).
>
> **This is not the same as "ready".** Every item in "Should be fixed before a
> wider release" below still stands, and none of this has been exercised against
> a real broker in market hours — the tests are unit-level with stubbed SDKs.
> The remaining gate is a live shakedown on a small size, not more code. Start
> with one execution broker, one lot, and read `logs/orders.log` afterwards to
> confirm the lifecycle matched what the broker actually did.

The architecture itself is sound: a localhost-only bearer-guarded sidecar owning
broker sessions, an OS-keystore credential store in the main process, a
provider-agnostic feed router, and a reliability layer with backoff, staleness
watchdogs and error classification. The gaps are specific, not structural.

---

## Blockers

### 1. ~~Paper/Live mode can silently desync~~ — RESOLVED

_Fixed by the explicit per-order trading mode._

Both modes previously posted an identical body to `/orders/place`, and
`OrderManager._mode` alone decided the routing — set only by a best-effort POST
with a swallowed error. A sidecar restart (which resets it to paper) meant the
UI said Live while orders went to paper; a mode toggle while the sidecar was
down meant the UI said Paper while orders went to a real broker.

Mode routing is now **two-key**. Every order, modify and cancel request carries
the mode it was composed under. The sidecar routes on the mode **in the
request** — never on its stored copy — and only after the two agree. A
disagreement is rejected (`MODE_MISMATCH`), never resolved by guessing; a
request with no mode is rejected too (`MODE_REQUIRED`). Nothing reaches a broker
in either case.

To keep the rejection rare rather than routine, the renderer re-confirms the
mode on every sidecar reconnect, and re-confirms it immediately after a
`MODE_MISMATCH` so the user's next attempt succeeds — deliberately without
re-sending the order, which stays a human decision.

### 2. ~~A live order is reported as filled when it was only accepted~~ — RESOLVED

_Fixed by the Order Synchronization Engine (`sidecar/services/order_sync/`)._

`place_order` used to report `COMPLETE` on any non-exception return and the
renderer marked the row `EXECUTED`. An order id means the request was accepted
*for routing* — it can still be rejected at the exchange, rest unfilled, or fill
in parts, so the terminal showed positions that did not exist at prices never
traded.

Placement now registers the order with the sync engine at `SUBMITTED`. A
background poller reads each broker's own order book and walks the order through
the canonical lifecycle (`SUBMITTED → ACCEPTED → PENDING → PARTIAL → FILLED`, or
`REJECTED / CANCELLED / EXPIRED`), and **a position is booked only against
broker-confirmed filled quantity** — partials book exactly what filled. Every
transition is logged. The renderer consumes `order_update` and can no longer
mark a live order executed on its own.

Two limitations remain, both documented in the module: the poller only sees
orders Charticks placed (fills made in the broker's own app are still invisible),
and an order that never reaches a terminal state is abandoned after 15 minutes
in its last known state rather than tracked forever.

### 3. ~~Every live order fans out to every connected broker~~ — RESOLVED

_Fixed by the Broker Execution Selection feature._

Live orders previously went to `manager.connected_sessions()` — every connected
account, full quantity each — with no account selector anywhere. Connecting a
second broker for market-data redundancy silently doubled every position.

Connectivity and execution are now separate concepts. Each account carries a
persisted `execute` flag, toggled on the Brokers page; live placement fans out
over `manager.execution_sessions()` (connected **and** opted in). With nothing
opted in, live orders are refused rather than routed anywhere.

The execution set is authoritative **in the sidecar**, and starts empty. The
renderer pushes it from the persisted config on load, on every change, and on
every sidecar reconnect (`useBrokerStore.pushExecutionSet`). This deliberately
avoids the failure mode described in finding #1: a dropped push or a sidecar
restart degrades to "live orders refused with a clear message", never to a
silent mis-route.

### 4. ~~All session risk limits are client-side only~~ — RESOLVED

_Fixed by server-side risk enforcement — see `docs/RISK-ENFORCEMENT.md`._

Max Positions, Max Trades, Max Loss, Profit Target, Max Quantity and Max Price
lived entirely in `localStorage` and were advisory: a renderer bug, a stale
window or anything talking to the bridge directly bypassed all of them.

They are now rules in `sidecar/services/risk_engine.py`, evaluated before the
paper/live fork. Limits are pushed by the renderer and re-pushed on reconnect;
until they arrive, live orders are refused (`RISK_CONFIG_NOT_SYNCED`). A rule
that raises fails closed.

This also closed a gap that was not in the original review: **tick grid, minimum
price and away-from-LTP were validated only inside the paper engine**, so a
fat-finger limit price was caught in paper and passed untouched to a real broker
in live. Those are now pre-route rules covering both engines.

### 5. ~~Nothing exits a live position automatically~~ — RESOLVED

_Fixed by `sidecar/services/live_manager.py`, on top of the confirmed position
book. See `docs/LIVE-POSITION-MANAGEMENT.md`._

SL / Target / Trailing SL were stored on positions and editable, but only the
**paper** engine acted on them; a live position closed only by manual action.

`LiveManager` now runs on the shared tick feed and manages **broker-confirmed
positions only**: Stop Loss, Target, Trailing SL, Portfolio Trail Profit and
Square-Off. The arithmetic is shared with the paper engine (`_compute_risk`,
`_trail_of`, `_steps`) so the two cannot disagree about what a rule means.

Still not ported: **live modify / cancel** of a working order
(`order_manager.modify_order` / `cancel_order` return "not available yet" in
live). Automated exits are placed as fresh market orders, so this does not
affect them, but a resting live limit order still cannot be amended from
Charticks.

---

## Should be fixed before a wider release

| | |
|---|---|
| **Installer ships no Python runtime** | `main.ts` spawns bare `python -m uvicorn`, and `resources/sidecar/` contains only `.py` source — no interpreter, no wheels. But `distribution/INSTALL.html` tells testers "the trading engine is built into the installer… if you were sent a setup-tester.bat file, ignore it." On a clean machine the app launches and the sidecar never starts. Either bundle an embedded Python + deps, or correct the docs and keep `setup-tester.bat` in the bundle. |
| **Sidecar logs land in the install directory** | The SDK's logzero writes to `<cwd>/logs/<date>/app.log`, and the sidecar's cwd is `resources/sidecar`. `START-HERE.txt` tells testers to send `%APPDATA%\charticks\logs`, which does not exist. Point logging at `CHARTICKS_DATA_DIR` (now passed to the sidecar) and fix the doc. |
| **No exchange holiday calendar** | `market_session.py` documents this: a trading holiday resolves to "open" and the rejection comes from the broker. Acceptable for paper, poor for live — the user gets an opaque SDK error instead of a clear "market closed". |
| **Paper book is in-memory** | A sidecar restart clears open paper orders and positions. Fine for paper; the same class of state (live order tracking, session risk counters) must be durable before live. |
| **Position MTM freezes off-window** | `docs/PENDING.md` §C: a position whose token leaves the subscribed chain window stops marking to market. Fix by registering position tokens in the `SubscriptionRegistry`. |
| **Plaintext credential fallback is silent** | `encryptSecrets` falls back to base64 plaintext with `enc: false` when `safeStorage.isEncryptionAvailable()` is false. On Windows DPAPI this should never trigger, but if it does the user is never told their credentials are unencrypted. Warn, or refuse to save. |

## What is solid

Worth stating explicitly, because these were done properly and shouldn't be
re-litigated:

- **Market-session gate** is server-authoritative and sits *ahead* of the
  paper/live fork, so one check covers both engines (`order_manager.py:79`).
  Same for the expiry gate. The renderer mirror in `lib/marketSession.ts` is
  correctly documented as a UX convenience, not the enforcement point.
- **Credential handling.** Encrypted at rest via the OS keystore, decrypted
  just-in-time for a connect, never persisted by renderer or sidecar, never
  written to logs. `git ls-files` confirms no secret has ever been committed.
- **The ICICI login popup** runs in an isolated session partition with no
  preload, so a third-party page can never reach a Charticks API. The
  redirect-capture handles both the query-param and form-POST shapes and
  cancels the dead redirect rather than flashing an error.
- **Reliability layer.** Generation-counter guards against stale callbacks,
  capped backoff, a staleness watchdog, `window 'online'` → forced reconnect,
  and error classification that routes an auth-shaped failure to session
  recovery rather than a blind retry.
- **Order splitting** stops on the first child failure and reports the
  remaining quantity instead of firing blindly — the right call for a
  part-executed order.
- **Instrument master caching** is crash-safe (temp file + atomic replace,
  truncated-cache detection, download-failure fallback to the newest cached
  day).
