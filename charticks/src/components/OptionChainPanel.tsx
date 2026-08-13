import { useEffect, useMemo, useRef, useState } from "react";
import { OPTION_INDICES, INDEX_BY_ID } from "@/lib/indices";
import { useMarketStore } from "@/stores/useMarketStore";
import { useLiveChain } from "@/stores/useLiveChain";
import { usePositionsStore, lotSize } from "@/stores/usePositionsStore";
import { useChainStore } from "@/stores/useChainStore";
import { useSettingsStore } from "@/stores/useSettingsStore";
import { useSessionLimits } from "@/stores/useSessionLimits";
import { useOrderEntryStore } from "@/stores/useOrderEntryStore";
import { useOrdersStore, type OrderInput } from "@/stores/useOrdersStore";
import { useUiStore } from "@/stores/useUiStore";
import { isValidLimitPrice } from "@/lib/orderValidation";
import { execDelay, applyEntryOffset, orderLimitError } from "@/lib/settingsActions";
import { marketGate, marketGateSilent } from "@/lib/marketSession";
import { activeExpiries, resolveActiveExpiry } from "@/lib/expiry";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { PartialFillDialog } from "@/components/PartialFillDialog";
import { MaxPositionDialog } from "@/components/MaxPositionDialog";
import { DuplicateOrderDialog } from "@/components/DuplicateOrderDialog";

type ChainFilter = "all" | "calls" | "puts";
const STRIKE_RANGES = [5, 10, 15, 20, 25, 30];
// "All" maps to the widest strike count the sidecar will subscribe.
const ALL_RANGE = 25;
// Abandoned inline Limit editors auto-close after this idle time (no submit).
const EDITOR_IDLE_MS = 10_000;

interface EditTarget {
  strike: number;
  optType: "CE" | "PE";
  side: "BUY" | "SELL";
}

/** Inline Limit-price editor that replaces a Buy/Sell button. Prefilled with the
 *  live LTP, auto-focused; ✓/Enter submits, ✕/Esc cancels. Any interaction resets
 *  the parent's 10s idle timer. */
function LimitEditor({
  ltp,
  onSubmit,
  onCancel,
  onInteract,
}: {
  ltp: number;
  onSubmit: (price: number) => void;
  onCancel: () => void;
  onInteract: () => void;
}) {
  const [val, setVal] = useState(ltp.toFixed(2));
  const [err, setErr] = useState(false);
  const ref = useRef<HTMLInputElement>(null);

  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);

  const submit = () => {
    onInteract();
    const n = parseFloat(val);
    // Reject zero/negative/NaN, sub-tick (e.g. 0.001 → 0.00) and off-tick
    // prices before submit — the sidecar re-validates as the source of truth.
    if (!isValidLimitPrice(n)) {
      setErr(true);
      return;
    }
    onSubmit(+n.toFixed(2));
  };

  return (
    <span className={`limit-edit ${err ? "err" : ""}`}>
      <input
        ref={ref}
        className="limit-input num"
        inputMode="decimal"
        value={val}
        aria-label="Limit price"
        onChange={(e) => {
          setVal(e.target.value);
          setErr(false);
          onInteract();
        }}
        onClick={onInteract}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Enter") submit();
          if (e.key === "Escape") onCancel();
        }}
      />
      <button className="le-ok" title="Submit order" onClick={(e) => { e.stopPropagation(); submit(); }}>✓</button>
      <button className="le-cancel" title="Cancel" onClick={(e) => { e.stopPropagation(); onCancel(); }}>✕</button>
    </span>
  );
}

/** Hover-only hint for a strike we already hold, e.g. "Already Long 3 Lots".
 *  Deliberately not a persistent highlight — the grid stays clean. */
function heldHint(lots: number, side: "BUY" | "SELL"): string | undefined {
  if (lots <= 0) return undefined;
  return `Already ${side === "BUY" ? "Long" : "Short"} ${lots} ${lots === 1 ? "Lot" : "Lots"}`;
}

function BuySell({
  side,
  ltp,
  editing,
  editSide,
  longLots,
  shortLots,
  onBuy,
  onSell,
  onSubmit,
  onCancel,
  onInteract,
}: {
  side: "ce" | "pe";
  ltp: number;
  editing: boolean;
  editSide: "BUY" | "SELL" | null;
  longLots: number;
  shortLots: number;
  onBuy: () => void;
  onSell: () => void;
  onSubmit: (price: number) => void;
  onCancel: () => void;
  onInteract: () => void;
}) {
  const buyHint = heldHint(longLots, "BUY");
  const sellHint = heldHint(shortLots, "SELL");
  return (
    <span className={`bs-cell ${side}`}>
      {editing && editSide === "BUY" ? (
        <LimitEditor ltp={ltp} onSubmit={onSubmit} onCancel={onCancel} onInteract={onInteract} />
      ) : (
        <button
          className="bs b"
          title={buyHint ?? "Buy"}
          data-hint={buyHint}
          onClick={(e) => { e.stopPropagation(); onBuy(); }}
        >
          B
        </button>
      )}
      <span className={`ltp ${side === "ce" ? "up" : "down"}`}>{ltp.toFixed(2)}</span>
      {editing && editSide === "SELL" ? (
        <LimitEditor ltp={ltp} onSubmit={onSubmit} onCancel={onCancel} onInteract={onInteract} />
      ) : (
        <button
          className="bs s"
          title={sellHint ?? "Sell"}
          data-hint={sellHint}
          onClick={(e) => { e.stopPropagation(); onSell(); }}
        >
          S
        </button>
      )}
    </span>
  );
}

export function OptionChainPanel() {
  const indexId = useChainStore((s) => s.instrument);
  const setIndexId = useChainStore((s) => s.setInstrument);
  const range = useChainStore((s) => s.range);
  const setRange = useChainStore((s) => s.setRange);
  const selectedExpiry = useChainStore((s) => s.expiry);
  const setExpiry = useChainStore((s) => s.setExpiry);
  const [filter, setFilter] = useState<ChainFilter>("all");
  const [blocked, setBlocked] = useState("");
  const [showLimitHint, setShowLimitHint] = useState(false);

  const orderType = useOrderEntryStore((s) => s.orderType);
  const setOrderType = useOrderEntryStore((s) => s.setOrderType);
  const lots = useOrderEntryStore((s) => s.lots);
  const setLots = useOrderEntryStore((s) => s.setLots);
  const [lotsDraft, setLotsDraft] = useState(String(lots));
  useEffect(() => setLotsDraft(String(lots)), [lots]);
  const lotsValid = /^\d+$/.test(lotsDraft) && parseInt(lotsDraft, 10) >= 1;

  // Selecting Limit shows a transient hint on where the price is entered; it
  // auto-dismisses after 10s (spec) so it never lingers.
  useEffect(() => {
    if (orderType !== "LIMIT") {
      setShowLimitHint(false);
      return;
    }
    setShowLimitHint(true);
    const t = setTimeout(() => setShowLimitHint(false), 10_000);
    return () => clearTimeout(t);
  }, [orderType]);

  // Inline Limit editor: at most one open at a time.
  const [editing, setEditing] = useState<EditTarget | null>(null);
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearIdle = () => {
    if (idleTimer.current) clearTimeout(idleTimer.current);
    idleTimer.current = null;
  };
  const closeEditor = () => {
    clearIdle();
    setEditing(null);
  };
  const armIdle = () => {
    clearIdle();
    idleTimer.current = setTimeout(() => setEditing(null), EDITOR_IDLE_MS);
  };
  useEffect(() => clearIdle, []);

  const ruleFor = useSettingsStore((s) => s.ruleFor);
  const registerTrade = useSessionLimits((s) => s.registerTrade);
  const placeOrder = useOrdersStore((s) => s.placeOrder);
  // A pending order already exists on this strike + side: offer to modify it
  // instead of stacking a second one.
  const [dupOrderId, setDupOrderId] = useState<string | null>(null);
  // A split order stopped part-way — offer to retry just the remainder.
  const [partial, setPartial] = useState<
    { executedQty: number; remainingQty: number; input: OrderInput } | null
  >(null);
  // "Ask Me" prompt when an add would exceed the Max Position limit.
  const [maxPosPrompt, setMaxPosPrompt] = useState<
    {
      strike: number; optType: "CE" | "PE"; side: "BUY" | "SELL";
      price: number; requested: number; remaining: number;
    } | null
  >(null);
  // The sidecar held an order because an identical one was just placed, or
  // because a previous attempt was never acknowledged. Only the user can say
  // whether to send it anyway, so the request is replayed with the override.
  const [dupPrompt, setDupPrompt] = useState<
    {
      code: "DUPLICATE_ORDER" | "IDEMPOTENCY_UNRESOLVED";
      message: string; symbol?: string; side?: string; qty?: number;
      duplicateOf?: string; placedSecondsAgo?: number | null;
      input: OrderInput;
    } | null
  >(null);
  // Open positions drive the "already held" hover hint and the add-vs-new
  // decision on every strike.
  const positions = usePositionsStore((s) => s.positions);
  const openPositions = useMemo(
    () => positions.filter((p) => p.status === "OPEN"),
    [positions],
  );
  // Double-click / duplicate-order guards (Issue #6).
  const placingRef = useRef(false);
  const lastClickRef = useRef<{ key: string; ts: number } | null>(null);

  const def = INDEX_BY_ID[indexId] ?? INDEX_BY_ID.NIFTY;
  // Live spot from the broker index feed; live chain snapshot from the sidecar.
  const spot = useMarketStore((s) => s.indices[indexId]?.ltp) ?? 0;
  const snapshot = useLiveChain((s) => s.snapshot);
  const selectChain = useLiveChain((s) => s.select);
  const feed = useLiveChain((s) => s.feed);

  const onThisIndex = snapshot.symbol === indexId;
  const rows = onThisIndex ? snapshot.rows : [];
  const atm = onThisIndex && snapshot.atm ? snapshot.atm : Math.round(spot / def.step) * def.step;
  // Charticks' own expiry layer: even if the broker is still listing yesterday's
  // contracts, only active expiries are offered, and the chain falls through to
  // the nearest active one with no user action needed.
  const expiries = useMemo(
    () => (onThisIndex ? activeExpiries(snapshot.expiries) : []),
    [onThisIndex, snapshot.expiries],
  );
  const expiry = resolveActiveExpiry(onThisIndex ? snapshot.expiry : "", expiries);

  // A pick that expires while the app is open (or a stale one from the broker)
  // is dropped so the selection follows the next active expiry automatically.
  useEffect(() => {
    if (selectedExpiry && expiries.length > 0 && !expiries.includes(selectedExpiry)) {
      setExpiry("");
    }
  }, [selectedExpiry, expiries, setExpiry]);

  // Tell the sidecar which index + strike count + expiry to stream. Sending
  // the (possibly empty) selected expiry lets the backend fall back to the
  // nearest when it's "" — index/expiry changes take effect immediately there.
  useEffect(() => {
    const count = range === "all" ? ALL_RANGE : range;
    selectChain(indexId, count, selectedExpiry);
  }, [indexId, range, selectedExpiry, selectChain]);

  const atmRef = useRef<HTMLTableRowElement>(null);
  useEffect(() => {
    atmRef.current?.scrollIntoView({ block: "center" });
  }, [indexId, range, filter]);

  /** Lots already held on a strike + side. A click on a strike we already hold
   *  is an ADD (the engine averages into the existing position) rather than a
   *  new position, so this drives both the hover hint and the limit check. */
  const heldLots = (strike: number, optType: "CE" | "PE", side: "BUY" | "SELL"): number => {
    const p = openPositions.find(
      (x) => x.underlying === indexId && x.strike === strike
        && x.optType === optType && x.side === side,
    );
    return p?.lots ?? 0;
  };

  /** Place an order at `price` for the given strike/side. The sidecar engine
   *  (paper) or broker (live) is authoritative — the resulting order/position
   *  is streamed back, so nothing is created client-side here. Gated on session
   *  limits, guarded against double-clicks, and surfaces validation errors. */
  const submitOrder = async (
    strike: number,
    optType: "CE" | "PE",
    side: "BUY" | "SELL",
    price: number,
    /** Lots to send — set by the Max Position prompt; defaults to the lot box. */
    overrideLots?: number,
    /** Skip the Max Position check (the user chose "Override Once"). */
    skipMaxPos = false,
    /** The user knowingly chose to exceed Max Position. Travels with the order
     *  so the sidecar — which enforces the limit independently — allows and
     *  logs this one rather than rejecting it. */
    overrideMaxPos = false,
  ) => {
    if (!lotsValid) return;
    // Duplicate-order prevention (Issue #6): ignore a re-entrant submit while
    // one is in flight, and swallow an identical strike/side click within a
    // short window (fast double-click).
    if (placingRef.current) return;
    const key = `${strike}|${optType}|${side}|${orderType}`;
    const now = Date.now();
    const last = lastClickRef.current;
    if (last && last.key === key && now - last.ts < 400) return;
    lastClickRef.current = { key, ts: now };

    // Market-session gate — nothing reaches the broker or the paper engine
    // outside market hours (the sidecar re-checks authoritatively).
    if (!marketGate(indexId)) return;

    // One working order per instrument + side. If one already exists, offer to
    // modify it rather than creating a duplicate — no request is sent.
    const existing = useOrdersStore.getState().findWorkingOrder({
      underlying: indexId, expiry, strike, optType, side,
    });
    if (existing) {
      setDupOrderId(existing.id);
      return;
    }

    const held = heldLots(strike, optType, side);
    // Only a genuinely NEW position is subject to the session's position-count
    // gate — adding to a strike we already hold leaves the count unchanged.
    if (held === 0) {
      const openCount = usePositionsStore
        .getState()
        .positions.filter((p) => p.status === "OPEN").length;
      const gate = useSessionLimits.getState().canOpenPosition(openCount);
      if (!gate.ok) {
        setBlocked(gate.reason);
        return;
      }
    }

    // Max Position limit on the RESULTING quantity of this position, resolved
    // through the profile's configured overflow behaviour.
    let sendLots = overrideLots ?? lots;
    let forceMaxPos = overrideMaxPos;
    if (!skipMaxPos) {
      const limit = useSessionLimits.getState().effectiveMaxPos();
      if (limit > 0 && held + sendLots > limit) {
        const remaining = Math.max(0, limit - held);
        const behavior = useSettingsStore.getState().maxPosBehavior();
        if (behavior === "block") {
          setBlocked(`Adding ${sendLots} ${sendLots === 1 ? "lot" : "lots"} exceeds your `
            + `Max Position limit (${limit}).`);
          return;
        }
        if (behavior === "ask") {
          setMaxPosPrompt({ strike, optType, side, price, requested: sendLots, remaining });
          return;
        }
        if (behavior === "auto") {
          if (remaining <= 0) {
            setBlocked(`Max Position limit (${limit}) already reached for this strike.`);
            return;
          }
          sendLots = remaining;
        }
        // "override" falls through with the full requested quantity — this
        // transaction only; the configured limit is never modified. The sidecar
        // enforces the same limit, so the breach must be declared to it.
        if (behavior === "override") forceMaxPos = true;
      }
    }

    setBlocked("");
    // Apply the active profile's Entry Price Offset, then enforce its Max
    // Qty / Max Price limits before the (optionally delayed) submit.
    const entry = applyEntryOffset(+price.toFixed(2), side);
    const qty = sendLots * lotSize(indexId);
    const limitErr = orderLimitError(qty, entry);
    if (limitErr) {
      setBlocked(limitErr);
      return;
    }
    const oc = useSettingsStore.getState().orderConfig();
    const rule = ruleFor(indexId);
    placingRef.current = true;
    try {
      // Execution Delay applies to every trading action (spec).
      await execDelay();
      const input: OrderInput = {
        underlying: indexId,
        strike,
        optType,
        side,
        orderType,
        lots: sendLots,
        qty,
        price: entry,
        rule,
        expiry,
        product: oc.product,
        validity: oc.validity,
        overrideMaxPos: forceMaxPos,
      };
      const res = await placeOrder(input);
      if (!res.ok) {
        if (res.code === "MARKET_CLOSED") {
          // Clock drift between renderer and sidecar — show the same dialog.
          useUiStore.getState().setMarketClosedNotice(true);
        } else if (res.code === "DUPLICATE_PENDING") {
          // Sidecar backstop fired (the renderer's book was stale).
          const existing = useOrdersStore.getState().findWorkingOrder({
            underlying: indexId, expiry, strike, optType, side,
          });
          if (existing) setDupOrderId(existing.id);
          else setBlocked(res.error ?? "");
        } else if (res.code === "PARTIAL_FILL") {
          // A split order stopped part-way: offer to retry the remainder only.
          setPartial({
            executedQty: res.executedQty ?? 0,
            remainingQty: res.remainingQty ?? 0,
            input,
          });
        } else if (res.code === "DUPLICATE_ORDER"
                   || res.code === "IDEMPOTENCY_UNRESOLVED") {
          // Duplicate protection held it. Ask, then replay the SAME order with
          // the override if the user insists — never auto-retry.
          setDupPrompt({
            code: res.code,
            message: res.error ?? "",
            symbol: res.symbol,
            side: res.side,
            qty: res.qty,
            duplicateOf: res.duplicateOf,
            placedSecondsAgo: res.placedSecondsAgo,
            input,
          });
        } else if (res.error && !res.error.startsWith("Duplicate")) {
          // Double-click-guard rejections are silent; real errors show.
          setBlocked(res.error);
        }
        return;
      }
      registerTrade();
      // Auto-hedge: after a filled SELL entry, place a protective BUY leg
      // further OTM (Seller/Hybrid profiles only — Buyer hides the card).
      if (side === "SELL") void placeHedge(strike, optType);
    } finally {
      placingRef.current = false;
    }
  };

  /** Place a protective BUY hedge leg `distancePts` further OTM, honouring the
   *  active profile's hedge config. Retries on failure when enabled. No-op when
   *  auto-hedge is off. Always MARKET — a protective leg must fill. */
  const placeHedge = async (mainStrike: number, optType: "CE" | "PE") => {
    const hedge = useSettingsStore.getState().hedgeConfig();
    if (!hedge.enabled) return;
    // Automatic action: fail quietly rather than popping a dialog the user
    // never asked for. submitOrder already gated the entry leg.
    if (!marketGateSilent(indexId)) return;
    const step = def.step || 50;
    const stepsAway = Math.max(1, Math.round(hedge.distancePts / step));
    const hedgeStrike = optType === "CE"
      ? mainStrike + stepsAway * step
      : mainStrike - stepsAway * step;
    // A working hedge leg on this contract already covers the position — never
    // stack a second one (this path is automatic, so no dialog).
    const existingHedge = useOrdersStore.getState().findWorkingOrder({
      underlying: indexId, expiry, strike: hedgeStrike, optType, side: "BUY",
    });
    if (existingHedge) return;
    const attempts = hedge.retryFailed ? Math.max(1, hedge.maxRetries) : 1;
    for (let i = 0; i < attempts; i++) {
      const res = await placeOrder({
        underlying: indexId,
        strike: hedgeStrike,
        optType,
        side: "BUY",
        orderType: "MARKET",
        lots,
        qty: lots * lotSize(indexId),
        price: 0,
        rule: ruleFor(indexId),
        expiry,
        product: useSettingsStore.getState().orderConfig().product,
        validity: useSettingsStore.getState().orderConfig().validity,
      });
      if (res.ok) return;
    }
  };

  /** "Modify Order" on the duplicate dialog: hand the existing order to the
   *  Home position grid, which drops that row straight into edit mode. Nothing
   *  is placed — saving there updates the existing order. */
  const modifyExisting = () => {
    const id = dupOrderId;
    setDupOrderId(null);
    if (!id) return;
    useUiStore.getState().setEditOrderId(id);
    useUiStore.getState().setScreen("home");
  };

  /** Retry only the quantity that never reached the broker after a split order
   *  failed part-way. Bypasses the duplicate check — the executed children are
   *  the same instrument and side by definition. */
  const retryRemaining = async () => {
    const p = partial;
    setPartial(null);
    if (!p || p.remainingQty <= 0) return;
    if (!marketGate(indexId)) return;
    const lotSz = lotSize(indexId) || 1;
    const res = await placeOrder({
      ...p.input,
      qty: p.remainingQty,
      lots: Math.max(1, Math.round(p.remainingQty / lotSz)),
      allowDuplicate: true,
    });
    if (!res.ok) {
      if (res.code === "PARTIAL_FILL") {
        setPartial({
          executedQty: res.executedQty ?? 0,
          remainingQty: res.remainingQty ?? 0,
          input: p.input,
        });
      } else if (res.error) {
        setBlocked(res.error);
      }
    }
  };

  /** B/S click. Market → place immediately. Limit → open the inline editor
   *  (closing any other), prefilled with the live LTP. */
  const onTrade = (
    strike: number,
    optType: "CE" | "PE",
    side: "BUY" | "SELL",
    price: number,
  ) => {
    if (!lotsValid) return;
    if (orderType === "MARKET") {
      submitOrder(strike, optType, side, price);
      return;
    }
    setEditing({ strike, optType, side });
    armIdle();
  };

  const showCE = filter !== "puts";
  const showPE = filter !== "calls";

  // An empty grid used to always read "Connect a broker…", which is wrong (and
  // was actively misleading) whenever the broker WAS connected but the market
  // feed was down. Name the actual blocker instead.
  const emptyReason = (() => {
    if (spot > 0) return "Waiting for live option-chain ticks…";
    if (!feed?.shouldRun) return "Connect a broker to stream the live option chain.";
    if (!feed.connected) {
      return `Market data feed is down — reconnecting${
        feed.lastError ? ` (${feed.lastError})` : ""
      }. The option chain needs the live index price to resolve strikes.`;
    }
    return `No spot price for ${indexId} yet — waiting for the first index tick.`;
  })();

  const bump = (delta: number) => setLots(Math.max(1, (lotsValid ? parseInt(lotsDraft, 10) : lots) + delta));
  const commitLots = () => {
    if (lotsValid) setLots(parseInt(lotsDraft, 10));
    else setLotsDraft(String(lots));
  };

  return (
    <section className="panel chain">
      <div className="phead chain-head">
        <h3>Option Chain</h3>
        <span className="tag num">Spot {spot.toFixed(2)}</span>
        <span className="grow" />
        <span className="tag">ATM {atm}</span>
      </div>

      {blocked && (
        <div className="chain-blocked" role="alert">
          New entry blocked — {blocked}
        </div>
      )}

      <div className="chain-ctrl">
        <label>
          Index
          <select value={indexId} onChange={(e) => setIndexId(e.target.value)}>
            {OPTION_INDICES.map((i) => (
              <option key={i.id} value={i.id}>{i.id}</option>
            ))}
          </select>
        </label>
        <label>
          Expiry
          <select
            value={expiry}
            disabled={expiries.length === 0}
            onChange={(e) => setExpiry(e.target.value)}
          >
            {expiries.length === 0 ? (
              <option value={expiry}>{expiry || "—"}</option>
            ) : (
              expiries.map((x) => (
                <option key={x} value={x}>{x}</option>
              ))
            )}
          </select>
        </label>
        <label>
          Order Type
          <select value={orderType} onChange={(e) => setOrderType(e.target.value as "MARKET" | "LIMIT")}>
            <option value="MARKET">Market</option>
            <option value="LIMIT">Limit</option>
          </select>
        </label>
        <label className="lots-ctrl">
          Lots
          <span className={`lots-adj ${lotsValid ? "" : "invalid"}`}>
            <button type="button" onClick={() => bump(-1)} aria-label="Fewer lots">−</button>
            <input
              className="num"
              type="text"
              inputMode="numeric"
              value={lotsDraft}
              aria-label="Lots"
              aria-invalid={!lotsValid}
              onChange={(e) => setLotsDraft(e.target.value.replace(/[^\d]/g, ""))}
              onBlur={commitLots}
            />
            <button type="button" onClick={() => bump(1)} aria-label="More lots">+</button>
          </span>
        </label>
        <label>
          Strikes
          <select
            value={range === "all" ? "all" : range}
            onChange={(e) => setRange(e.target.value === "all" ? "all" : +e.target.value)}
          >
            {STRIKE_RANGES.map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
            <option value="all">All</option>
          </select>
        </label>
        <div className="chain-radios" role="radiogroup" aria-label="Option type">
          {(["all", "calls", "puts"] as ChainFilter[]).map((f) => (
            <label key={f} className="radio">
              <input
                type="radio"
                name="oc-filter"
                checked={filter === f}
                onChange={() => setFilter(f)}
              />
              {f === "all" ? "All" : f === "calls" ? "Calls Only" : "Puts Only"}
            </label>
          ))}
        </div>
      </div>

      {showLimitHint && (
        <div className="chain-hint" role="status">
          Limit order — click <b>B</b> or <b>S</b> on a strike, then enter your
          limit price in the inline editor that appears on that button.
        </div>
      )}

      {!lotsValid && (
        <div className="chain-blocked" role="alert">
          Enter a valid lot count (whole number, 1 or more) to place orders.
        </div>
      )}

      <div className="pbody">
        <table className={`lgrid chain-grid f-${filter}`}>
          <thead>
            <tr>
              {showCE && <th className="ce-h">Call LTP</th>}
              <th className="st-h">Strike</th>
              {showPE && <th className="pe-h">Put LTP</th>}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td className="oc-waiting" colSpan={3}>
                  {emptyReason}
                </td>
              </tr>
            )}
            {rows.map((row) => {
              const k = row.strike;
              const ce = row.ce ?? 0;
              const pe = row.pe ?? 0;
              const isAtm = k === atm;
              const editCe = editing && editing.strike === k && editing.optType === "CE" ? editing.side : null;
              const editPe = editing && editing.strike === k && editing.optType === "PE" ? editing.side : null;
              return (
                <tr key={k} ref={isAtm ? atmRef : undefined} className={`hoverable ${isAtm ? "atm" : ""}`}>
                  {showCE && (
                    <td className="ce">
                      <BuySell
                        side="ce"
                        ltp={ce}
                        editing={editCe != null}
                        editSide={editCe}
                        longLots={heldLots(k, "CE", "BUY")}
                        shortLots={heldLots(k, "CE", "SELL")}
                        onBuy={() => onTrade(k, "CE", "BUY", ce)}
                        onSell={() => onTrade(k, "CE", "SELL", ce)}
                        onSubmit={(price) => { submitOrder(k, "CE", editCe!, price); closeEditor(); }}
                        onCancel={closeEditor}
                        onInteract={armIdle}
                      />
                    </td>
                  )}
                  <td className="strike">{k}</td>
                  {showPE && (
                    <td className="pe">
                      <BuySell
                        side="pe"
                        ltp={pe}
                        editing={editPe != null}
                        editSide={editPe}
                        longLots={heldLots(k, "PE", "BUY")}
                        shortLots={heldLots(k, "PE", "SELL")}
                        onBuy={() => onTrade(k, "PE", "BUY", pe)}
                        onSell={() => onTrade(k, "PE", "SELL", pe)}
                        onSubmit={(price) => { submitOrder(k, "PE", editPe!, price); closeEditor(); }}
                        onCancel={closeEditor}
                        onInteract={armIdle}
                      />
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <ConfirmDialog
        open={dupOrderId != null}
        title="Pending Order Already Exists"
        message="A pending order already exists for this strike. Would you like to modify the existing order?"
        confirmLabel="Modify Order"
        onConfirm={modifyExisting}
        onCancel={() => setDupOrderId(null)}
      />

      <DuplicateOrderDialog
        open={!!dupPrompt}
        code={dupPrompt?.code ?? "DUPLICATE_ORDER"}
        message={dupPrompt?.message ?? ""}
        symbol={dupPrompt?.symbol}
        side={dupPrompt?.side}
        qty={dupPrompt?.qty}
        duplicateOf={dupPrompt?.duplicateOf}
        placedSecondsAgo={dupPrompt?.placedSecondsAgo}
        onCancel={() => setDupPrompt(null)}
        onPlaceAnyway={() => {
          const pending = dupPrompt;
          setDupPrompt(null);
          if (!pending) return;
          void (async () => {
            const res = await placeOrder({ ...pending.input, overrideDuplicate: true });
            if (!res.ok && res.error) setBlocked(res.error);
            else registerTrade();
          })();
        }}
      />
      <MaxPositionDialog
        open={maxPosPrompt != null}
        requestedLots={maxPosPrompt?.requested ?? 0}
        remainingLots={maxPosPrompt?.remaining ?? 0}
        onAddRemaining={() => {
          const p = maxPosPrompt;
          setMaxPosPrompt(null);
          if (p) void submitOrder(p.strike, p.optType, p.side, p.price, p.remaining, true);
        }}
        onOverride={() => {
          const p = maxPosPrompt;
          setMaxPosPrompt(null);
          // Last argument: tell the sidecar this breach was chosen, not a bug.
          if (p) void submitOrder(p.strike, p.optType, p.side, p.price, p.requested, true, true);
        }}
        onCancel={() => setMaxPosPrompt(null)}
      />

      <PartialFillDialog
        open={partial != null}
        executedQty={partial?.executedQty ?? 0}
        remainingQty={partial?.remainingQty ?? 0}
        onRetry={() => void retryRemaining()}
        onDismiss={() => setPartial(null)}
      />
    </section>
  );
}
