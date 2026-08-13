# Handoff — Paper Trading Engine + Pending Work

_Last updated: 2026-07-28_

## ✅ Done — Paper trading realism (all 7 tested issues)

Root cause: paper trading was a **frontend mock** (timer-based fills, random LTP
jitter). Rebuilt as a **real tick-driven engine in the sidecar** sharing the live
market feed.

- **New engine:** `sidecar/services/paper_engine.py` (`PaperEngine`, singleton
  `paper_engine`) — in-memory order book on the shared option-tick feed; publishes
  a full `paper_state` snapshot.
- **#1 P&L** — positions marked-to-market on every live tick (jitter removed).
- **#2 Limit orders** — now **rest** and fill on price: Buy when `ask ≤ limit`,
  Sell when `bid ≥ limit`. No timers.
- **#3 Edit** — restored **✎ pencil (price + lots)** + Cancel on working orders in
  **both** `Orders.tsx` and `Positions.tsx`; executed orders read-only.
- **#4 Reconnect** — stale-socket watchdog in `reliability/health_monitor.py`
  (`_watchdog` / `force_reconnect_all`), backoff cap 60s→15s, renderer
  `window 'online'` → `POST /brokers/reconnect`.
- **#5 Slippage** — Market BUY→best ask, SELL→best bid (internal bid/ask captured
  in `broker_manager._best_bid_ask` / `get_option_quote`); synthetic widening
  spread when depth is missing. Option Chain UI still shows **only LTP**.
- **#6 Double-click** — in-flight lock + 400ms dedupe in `OptionChainPanel.tsx` +
  engine-side duplicate backstop.
- **#7 Validation** — `lib/orderValidation.ts` + engine `_validate` (tick 0.05,
  min 0.05) reject 0 / negative / off-tick (0.001).
- **Frontend rewiring:** `stores/paperSync.ts` drives `useOrdersStore` /
  `usePositionsStore` from `paper_state`; structural ops POST to `/orders/*`,
  `/positions/*`, `/paper/*`.

Verified: `tsc -b` clean, sidecar compiles + imports clean. **Not** run live
end-to-end (needs a connected Angel account).

---

## ⏳ PENDING — to build

### A. SL / Target / TSL automation — "linking orders with SL, target" (carryover, NOT built)

Currently paper positions **store** `sl` / `target` values (from the risk rule)
and you can edit them in the UI — but **nothing acts on them**. No tick-driven
monitoring, no auto-exit, no trailing.

Needs building (port of legacy `engines/trade_execution_engine.py`, the "C2" note
in `order_manager.py`):

- **Auto-exit on SL/Target hit** — in `paper_engine._on_tick`, for each open
  position check `ltp` vs `sl` / `target` and auto-close (book exit, emit trade +
  `paper_state`).
- **Trailing SL (TSL)** — track highest/lowest price since entry, ratchet the SL
  by the configured trail offset.
- **Linked / bracket orders (OCO)** — on entry fill, create linked SL + target
  child orders; filling one cancels the other. Decide: model as real child orders
  in the book, or as position-level triggers.
- **Live parity** — same logic must eventually drive live (broker SL/target
  orders), not just paper.
- **UI** — surface linked SL/target order state and auto-exit events in
  Orders / Positions.

### B. Multi-broker market data websocket (discussed, NOT built)

Goal: Kotak-only / Dhan-only users get market data from *their* broker (only Angel
has a feed today).

**Recommended design (agreed direction):** one provider-agnostic feed behind a
per-broker adapter, selected by connected broker — **not** three always-on sockets.

- `MarketFeedAdapter` protocol: `build_socket` / `subscribe` /
  `parse_tick → normalized {token, ltp, bid, ask, volume}` / `resolve_option`.
- `AngelFeed` / `KotakFeed` / `DhanFeed` implementations.
- `MarketFeedRouter` picks the active adapter (priority + failover), wraps in ONE
  `WebSocketManager`, adds to `health_monitor.ws_managers`, writes normalized ticks
  into existing `option_ticks` / `index_ltp`.
- Real work = per-broker **tick parsing, instrument master / token scheme,
  subscribe protocol, WS auth** (the socket plumbing is already reusable).
- **Constraint:** Angel allows only one `SmartWebSocketV2` per feed_token.
- **Two open questions to answer before building:** (1) desktop sidecar-per-user
  vs shared multi-tenant server? (2) can one user connect multiple brokers
  simultaneously?

### C. Known limitations / follow-ups from the paper-engine work

- **Position MTM freezes** if its option token leaves the subscribed option-chain
  window (switch index/expiry away → P&L stops). Fix: register position tokens in
  the `SubscriptionRegistry` so they always stream.
- **Paper book is in-memory** — sidecar restart clears open paper orders /
  positions. Add persistence if session survival matters.
- **Paper now requires a connected broker feed** (no offline mock fallback) —
  intended, but flag for UX.
- ~~Live order routing for Kotak / Dhan not ported. Live modify / cancel also
  not ported.~~ **Done 2026-08-13**: all four connected brokers (Angel, Kotak,
  ICICI, Dhan) now place, modify and cancel live. See `docs/ORDER-PIPELINE.md`.
- ~~Idempotency keys still not implemented — a placement retried after a timeout
  can double up.~~ **Done 2026-08-13**: broker-independent framework in
  `sidecar/services/idempotency/`, native `correlationId` on Dhan, tag echo on
  Kotak/ICICI, attribute matching on Angel. See `docs/IDEMPOTENCY.md`.
- **Fills made outside Charticks** are still invisible to the position book: the
  sync engine reads the broker's order book but only tracks orders it placed, so
  a trade made in the broker's own app never appears.
