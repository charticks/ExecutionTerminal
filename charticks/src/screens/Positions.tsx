import { useEffect, useRef, useState } from "react";
import { FlashNumber } from "@/components/FlashNumber";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { InfoDialog } from "@/components/InfoDialog";
import { SessionLimitsBar } from "@/components/SessionLimitsBar";
import { RollDialog, type RollDirection } from "@/components/RollDialog";
import { Icon } from "@/components/Icon";
import { Popover, usePopover } from "@/components/Popover";
import {
  usePositionsStore,
  pnlOf,
  lotSize,
  type OptionPosition,
} from "@/stores/usePositionsStore";
import { useGridPrefsStore, type StrikeBand } from "@/stores/useGridPrefsStore";
import { useOrdersStore, isWorking, type Order } from "@/stores/useOrdersStore";
import { useChainStore } from "@/stores/useChainStore";
import { useSettingsStore, FALLBACK_INSTRUMENT_DEFAULT } from "@/stores/useSettingsStore";
import { defaultsColumns } from "@/screens/Settings";
import { execDelay } from "@/lib/settingsActions";
import { marketGate, marketGateSilent } from "@/lib/marketSession";
import { useUiStore } from "@/stores/useUiStore";
import {
  useMarketStore,
  parseOptionSymbol,
  type Position as LivePosition,
  type ClosedPosition,
} from "@/stores/useMarketStore";
import type { MonitorState } from "@/bridge/events";
import { bridge } from "@/bridge/client";
import { useTradingModeStore } from "@/stores/useTradingModeStore";
import { INDEX_BY_ID } from "@/lib/indices";
import { money } from "@/lib/format";
import { formatExpiry } from "@/lib/expiry";
import { limitPriceError } from "@/lib/orderValidation";

/** Compact quantity stepper: editable amount field; +/- apply that amount of
 *  lots. Field defaults to 1 so repeated clicks make single-lot changes. */
function AdjLotsStepper({ id, underlying, disabled }:
    { id: string; underlying: string; disabled: boolean }) {
  const adjustLots = usePositionsStore((s) => s.adjustLots);
  const [amount, setAmount] = useState(1);
  const [busy, setBusy] = useState(false);

  // A refusal must be visible. The engine declines a resize for real reasons —
  // over a position limit, an exit already in flight, an unknown lot size — and
  // every one of them used to be swallowed, so the control looked broken rather
  // than refused.
  const apply = async (delta: number) => {
    if (busy || delta === 0) return;
    if (!marketGate(underlying)) return;
    setBusy(true);
    try {
      await adjustLots(id, delta);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="lots-adj">
      <button
        disabled={disabled || busy}
        onClick={() => void apply(-amount)}
        aria-label={`Reduce by ${amount} lot${amount === 1 ? "" : "s"}`}
        title={`Reduce by ${amount} lot${amount === 1 ? "" : "s"}`}
      >
        −
      </button>
      <input
        className="num"
        type="text"
        inputMode="numeric"
        value={amount}
        disabled={disabled || busy}
        aria-label="Lots to adjust"
        // Select on focus, so typing REPLACES the amount instead of appending
        // to it. Clicking into a field showing "1" and typing "2" produced 12.
        onFocus={(e) => e.currentTarget.select()}
        onChange={(e) => {
          const n = parseInt(e.target.value.replace(/\D/g, ""), 10);
          setAmount(Number.isNaN(n) ? 0 : n);
        }}
        onBlur={() => setAmount((a) => (a < 1 ? 1 : a))}
      />
      <button
        disabled={disabled || busy}
        onClick={() => void apply(amount)}
        aria-label={`Add ${amount} lot${amount === 1 ? "" : "s"}`}
        title={`Add ${amount} lot${amount === 1 ? "" : "s"}`}
      >
        +
      </button>
    </div>
  );
}

/** Shows the engine's last refusal of a position action, and clears it. */
function PositionActionError() {
  const error = usePositionsStore((s) => s.lastError);
  const clear = usePositionsStore((s) => s.clearError);
  useEffect(() => {
    if (!error) return;
    const t = setTimeout(clear, 8000);
    return () => clearTimeout(t);
  }, [error, clear]);
  if (!error) return null;
  return (
    <div className="pos-action-error" role="alert" onClick={clear}>
      {error}
    </div>
  );
}

/** Inline SL / Target value with a hover-revealed ✎. Editing commits only on ✓
 *  / Enter; blur or Esc restores the original value (never saves on focus loss). */
function EditableRisk({
  label,
  value,
  onSave,
  disabled,
  decimals = 1,
}: {
  label: string;
  value: number | undefined;
  onSave: (n: number) => void;
  disabled: boolean;
  /** Prices want a decimal; trail points and rupee amounts are whole numbers. */
  decimals?: number;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  const start = () => {
    setDraft(value != null ? String(value) : "");
    setEditing(true);
  };
  const commit = () => {
    const n = parseFloat(draft.replace(/[^\d.]/g, ""));
    if (!Number.isNaN(n)) onSave(+n.toFixed(2));
    setEditing(false);
  };

  if (editing) {
    return (
      <input
        className="risk-input num"
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => setEditing(false)}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") setEditing(false);
        }}
        aria-label={`Edit ${label}`}
      />
    );
  }

  return (
    <span className="risk-chip">
      {label} {value != null ? value.toFixed(decimals) : "—"}
      {!disabled && (
        <button className="risk-edit" title={`Edit ${label}`} aria-label={`Edit ${label}`} onClick={start}>
          ✎
        </button>
      )}
    </span>
  );
}

/** Contract expiry shown right after the instrument name, e.g. "• 31 Jul".
 *  Compact by design so it never grows the row. Renders nothing when the
 *  contract carries no expiry (non-option legs). */
function ExpiryTag({ expiry }: { expiry: string | undefined }) {
  const label = formatExpiry(expiry ?? "");
  if (!label) return null;
  return <span className="sym-exp" title={expiry}>• {label}</span>;
}

function PositionRow({ p, showRoll, onRoll }: { p: OptionPosition; showRoll: boolean; onRoll: (p: OptionPosition, dir: RollDirection) => void }) {
  const closePosition = usePositionsStore((s) => s.closePosition);
  const setRisk = usePositionsStore((s) => s.setRisk);
  const partialExits = useSettingsStore((s) => s.active().order.partialExits);
  // Profile-configured quick-exit percentages → fractions (fallback to 100%).
  const partialFractions = (partialExits.length ? partialExits : [100])
    .slice()
    .sort((a, b) => a - b)
    .map((v) => v / 100);
  const pnl = pnlOf(p);
  const closed = p.status === "CLOSED";

  return (
    <tr className={closed ? "closed" : ""}>
      <td className="c-inst">
        <div className="sym">
          {p.underlying} {p.strike} {p.optType}
          <ExpiryTag expiry={p.expiry} />
          {closed && (
            <span className={`tag closed-tag ${p.exitReason ? p.exitReason.toLowerCase() : ""}`}>
              {p.exitReason === "SL" ? "SL Hit" : p.exitReason === "TARGET" ? "Target Hit" : "Closed"}
            </span>
          )}
        </div>
        <div className="sub">
          <span className={`dir ${p.side === "BUY" ? "l" : "s"}`}>{p.side === "BUY" ? "L" : "S"}</span>
          {p.lots} {p.lots === 1 ? "Lot" : "Lots"}
          {!closed && (
            <>
              {" "}
              <EditableRisk
                label="SL"
                value={p.sl}
                disabled={closed}
                onSave={(n) => setRisk(p.id, { sl: n })}
              />
              <EditableRisk
                label="Tgt"
                value={p.target}
                disabled={closed}
                onSave={(n) => setRisk(p.id, { target: n })}
              />
              {/* Trail SL is per-position — this row shows the snapshot THIS
                  trade captured at entry, which may differ from the current
                  Instrument Defaults and from other open positions. */}
              {p.trail && (
                <>
                  <EditableRisk
                    label={p.trail.mode === "point" ? "After" : "Start"}
                    value={p.trail.after}
                    decimals={0}
                    disabled={closed}
                    onSave={(n) => setRisk(p.id, { trailAfter: n })}
                  />
                  <EditableRisk
                    label="Step"
                    value={p.trail.step}
                    decimals={0}
                    disabled={closed}
                    onSave={(n) => setRisk(p.id, { trailStep: n })}
                  />
                </>
              )}
            </>
          )}
        </div>
      </td>
      <td className="c-num num">{p.avgEntry.toFixed(2)}</td>
      <td className="c-num num">{p.exit != null ? p.exit.toFixed(2) : "—"}</td>
      <td className="c-num">
        <FlashNumber value={p.ltp} format={(n) => n.toFixed(2)} className="ltp-cell" />
      </td>
      <td className="c-pnl">
        <FlashNumber value={pnl} format={money} className={`pnl-cell ${pnl >= 0 ? "up" : "down"}`} />
      </td>
      <td className="c-adj">
        <AdjLotsStepper id={p.id} underlying={p.underlying} disabled={closed} />
      </td>
      <td className="c-close">
        <div className="close-cell">
          {partialFractions.map((f) => (
            <button
              key={f}
              disabled={closed}
              className="closeb"
              title={`Exit ${f * 100}% of position`}
              onClick={async () => {
                // Partial exit / square-off is a trading action — same gate.
                if (!marketGate(p.underlying)) return;
                await execDelay();
                closePosition(p.id, f);
              }}
            >
              {f * 100}%
            </button>
          ))}
        </div>
      </td>
      {showRoll && (
        <td className="c-roll">
          <div className="roll">
            <button disabled={closed} title="Roll up" onClick={() => onRoll(p, "up")}>⬆</button>
            <button disabled={closed} title="Roll down" onClick={() => onRoll(p, "down")}>⬇</button>
          </div>
        </td>
      )}
    </tr>
  );
}

/** Shows the active instrument's defaults with a ✎ that opens a small editor —
 *  a quick-edit surface for the values managed in full on Settings. Which fields
 *  appear follows the enabled features and the Trail SL mode exactly as the
 *  Settings table does; saving affects future trades only. */
function DefaultsChip() {
  const instrument = useChainStore((s) => s.instrument);
  const perInstrument = useSettingsStore((s) => s.active().instruments);
  const trade = useSettingsStore((s) => s.active().trade);
  const setInstrumentDefault = useSettingsStore((s) => s.setInstrumentDefault);
  const { open, toggle, wrapRef } = usePopover();

  const cur = perInstrument[instrument] ?? FALLBACK_INSTRUMENT_DEFAULT;
  const cols = defaultsColumns(trade);
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const openEditor = () => {
    setDrafts(Object.fromEntries(cols.map((c) => [c.key, String(cur[c.key])])));
    toggle();
  };
  const save = () => {
    const next = { ...cur };
    for (const c of cols) {
      next[c.key] = parseInt((drafts[c.key] ?? "").replace(/\D/g, ""), 10) || 0;
    }
    setInstrumentDefault(instrument, next);
    toggle();
  };

  const name = INDEX_BY_ID[instrument]?.name ?? instrument;
  // Summary line mirrors the visible columns, so an off feature never shows a
  // value the engine would ignore.
  const summary = cols.length === 0
    ? "None"
    : cols
        .map((c) => `${SHORT_LABEL[c.key] ?? c.key} ${cur[c.key]}`)
        .join(" · ");

  return (
    <div className="pop-wrap defaults-chip" ref={wrapRef}>
      <span className="dc-lab">{name} defaults</span>
      <span className="dc-val num">{summary}</span>
      <button className="dc-edit" title={`Edit ${name} defaults`} aria-label={`Edit ${name} defaults`} onClick={openEditor}>
        ✎
      </button>
      <Popover open={open} className="defaults-pop conn-pop">
        <div className="pop-title">{name} Defaults</div>
        {cols.length === 0 && (
          <div className="pop-empty">No trade-management features are enabled.</div>
        )}
        {cols.map((c) => (
          <label key={c.key} className="dc-field">
            {c.label}
            <input
              className="def-input num"
              inputMode="numeric"
              value={drafts[c.key] ?? ""}
              onChange={(e) => setDrafts((d) => ({ ...d, [c.key]: e.target.value }))}
            />
          </label>
        ))}
        <div className="dc-actions">
          <button className="btn-ghost" onClick={toggle}>Cancel</button>
          <button className="btn-primary" disabled={cols.length === 0} onClick={save}>Save</button>
        </div>
      </Popover>
    </div>
  );
}

/** Compact column names for the header chip's one-line summary. */
const SHORT_LABEL: Record<string, string> = {
  sl: "SL",
  target: "Tgt",
  trailAfter: "After",
  trailStep: "Step",
  startTrail: "Start",
  profitStep: "Step",
};

/** Rupee amounts shortened for the header, e.g. 10000 → "₹10K". */
function shortMoney(n: number): string {
  if (n >= 100000) return `₹${(n / 100000).toFixed(n % 100000 ? 1 : 0)}L`;
  if (n >= 1000) return `₹${(n / 1000).toFixed(n % 1000 ? 1 : 0)}K`;
  return `₹${n}`;
}

/** Portfolio Trail Profit belongs to the whole book, not to any one position,
 *  so it is surfaced once in the Positions header. Trail SL is deliberately NOT
 *  shown here — each position carries its own snapshot (see PositionRow). */
function PortfolioTrailChip() {
  const pt = useSettingsStore((s) => s.active().portfolioTrail);
  const patch = useSettingsStore((s) => s.patch);
  const { open, toggle, wrapRef } = usePopover();
  const [enabled, setEnabled] = useState(pt.enabled);
  const [after, setAfter] = useState(String(pt.activateAfter));
  const [dist, setDist] = useState(String(pt.trailDistance));

  const openEditor = () => {
    setEnabled(pt.enabled);
    setAfter(String(pt.activateAfter));
    setDist(String(pt.trailDistance));
    toggle();
  };
  const save = () => {
    patch("portfolioTrail", {
      enabled,
      activateAfter: parseInt(after.replace(/\D/g, ""), 10) || 0,
      trailDistance: parseInt(dist.replace(/\D/g, ""), 10) || 0,
    });
    toggle();
  };

  return (
    <div className="pop-wrap defaults-chip" ref={wrapRef}>
      <span className="dc-lab">Portfolio TP</span>
      <span className={`dc-val num ${pt.enabled ? "" : "off"}`}>
        {pt.enabled
          ? `${shortMoney(pt.activateAfter)} → ${shortMoney(pt.trailDistance)}`
          : "OFF"}
      </span>
      <button
        className="dc-edit"
        title="Edit Portfolio Trail Profit"
        aria-label="Edit Portfolio Trail Profit"
        onClick={openEditor}
      >
        ✎
      </button>
      <Popover open={open} className="defaults-pop conn-pop">
        <div className="pop-title">Portfolio Trail Profit</div>
        <label className="col-opt">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          Trail Profit
        </label>
        <label className="dc-field">
          Activate After (₹)
          <input
            className="def-input num"
            inputMode="numeric"
            disabled={!enabled}
            value={after}
            onChange={(e) => setAfter(e.target.value)}
          />
        </label>
        <label className="dc-field">
          Trail Distance (₹)
          <input
            className="def-input num"
            inputMode="numeric"
            disabled={!enabled}
            value={dist}
            onChange={(e) => setDist(e.target.value)}
          />
        </label>
        <div className="dc-actions">
          <button className="btn-ghost" onClick={toggle}>Cancel</button>
          <button className="btn-primary" onClick={save}>Save</button>
        </div>
      </Popover>
    </div>
  );
}

const STRIKE_BANDS: StrikeBand[] = ["1-5", "5-10", "10-15"];
const BAND_LABEL: Record<StrikeBand, string> = {
  "1-5": "1–5 Strikes",
  "5-10": "5–10 Strikes",
  "10-15": "10–15 Strikes",
};

/** Column-visibility popover: the option-seller Roll column plus, when Roll is
 *  on, the live Strike Band preference that drives Roll Up/Down. */
function ColumnMenu() {
  const cols = useGridPrefsStore((s) => s.cols);
  const setCol = useGridPrefsStore((s) => s.setCol);
  const strikeBand = useGridPrefsStore((s) => s.strikeBand);
  const setStrikeBand = useGridPrefsStore((s) => s.setStrikeBand);
  const { open, toggle, wrapRef } = usePopover();

  return (
    <div className="pop-wrap col-menu" ref={wrapRef}>
      <button className="ghost-btn" title="Customize columns" aria-label="Customize columns" onClick={toggle}>
        <Icon name="grid" size={14} />
      </button>
      <Popover open={open} className="cols-pop">
        <div className="pop-title">Columns</div>
        <label className="col-opt">
          <input type="checkbox" checked={cols.roll} onChange={(e) => setCol("roll", e.target.checked)} />
          Roll
        </label>
        {cols.roll && (
          <div className="band-section">
            <div className="pop-title">Strike Band</div>
            {STRIKE_BANDS.map((b) => (
              <label key={b} className="col-opt">
                <input
                  type="radio"
                  name="strike-band"
                  checked={strikeBand === b}
                  onChange={() => setStrikeBand(b)}
                />
                {BAND_LABEL[b]}
              </label>
            ))}
          </div>
        )}
      </Popover>
    </div>
  );
}

/** A submitted-but-not-fully-executed limit order shown in the Positions panel.
 *  Execution-dependent columns (Entry/Exit/LTP/P&L/Roll) stay blank until the
 *  order fills and becomes a live position. A ✎ pencil opens an inline editor to
 *  modify limit price AND quantity (lots); ✕ cancels. Executed orders are
 *  read-only (this row only renders for working orders). */
function WorkingOrderRow({ o, showRoll }: { o: Order; showRoll: boolean }) {
  const modifyOrder = useOrdersStore((s) => s.modifyOrder);
  const cancelOrder = useOrdersStore((s) => s.cancelOrder);
  const [editing, setEditing] = useState(false);
  const [priceDraft, setPriceDraft] = useState(String(o.price));
  const [lotsDraft, setLotsDraft] = useState(String(o.lots));
  const editOrderId = useUiStore((s) => s.editOrderId);
  const rowRef = useRef<HTMLTableRowElement>(null);

  const partial = o.filledLots > 0;
  const remaining = o.lots - o.filledLots;
  const badgeKind = partial ? "partial" : "pending";
  const badgeLabel = `${partial ? "🟧 Partial" : "🟦 Pending"} Limit ${
    o.side === "BUY" ? "Buy" : "Sell"
  } @${o.price.toFixed(2)}`;

  const startEdit = () => {
    setPriceDraft(o.price.toFixed(2));
    setLotsDraft(String(o.lots));
    setEditing(true);
  };
  // Handoff from the duplicate-order dialog ("Modify Order"): drop straight
  // into edit mode on the existing order and scroll it into view.
  useEffect(() => {
    if (editOrderId !== o.id) return;
    setPriceDraft(o.price.toFixed(2));
    setLotsDraft(String(o.lots));
    setEditing(true);
    rowRef.current?.scrollIntoView({ block: "center" });
    useUiStore.getState().setEditOrderId(null);
  }, [editOrderId, o.id, o.price, o.lots]);

  const commit = () => {
    // Modifying an order is a trading action — blocked outside market hours.
    if (!marketGate(o.underlying)) return;
    const price = parseFloat(priceDraft);
    const lots = parseInt(lotsDraft, 10);
    const patch: { price?: number; lots?: number } = {};
    if (limitPriceError(price) === "") patch.price = +price.toFixed(2);
    if (!Number.isNaN(lots) && lots >= 1) patch.lots = lots;
    if (patch.price != null || patch.lots != null) modifyOrder(o.id, patch);
    setEditing(false);
  };

  return (
    <tr className="working-row" ref={rowRef}>
      <td className="c-inst">
        <div className="sym">
          {o.underlying} {o.strike} {o.optType}
          <ExpiryTag expiry={o.expiry} />
        </div>
        <div className="sub">
          <span className={`dir ${o.side === "BUY" ? "l" : "s"}`}>
            {o.side === "BUY" ? "L" : "S"}
          </span>
          {partial ? `${o.lots} / ${remaining}` : o.lots}{" "}
          {o.lots === 1 ? "Lot" : "Lots"}
          <span className={`ord-badge ${badgeKind}`}>{badgeLabel}</span>
        </div>
      </td>
      <td className="c-num" />
      <td className="c-num" />
      <td className="c-num" />
      <td className="c-pnl" />
      <td className="c-adj" />
      <td className="c-close">
        {editing ? (
          <span className="wo-edit">
            <input
              className="risk-input num"
              autoFocus
              value={priceDraft}
              inputMode="decimal"
              aria-label="Modify limit price"
              onChange={(e) => setPriceDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commit();
                if (e.key === "Escape") setEditing(false);
              }}
            />
            <input
              className="risk-input num"
              value={lotsDraft}
              inputMode="numeric"
              aria-label="Modify lots"
              onChange={(e) => setLotsDraft(e.target.value.replace(/[^\d]/g, ""))}
              onKeyDown={(e) => {
                if (e.key === "Enter") commit();
                if (e.key === "Escape") setEditing(false);
              }}
            />
            <button className="wo-ok" title="Submit changes" onClick={commit}>✓</button>
            <button className="wo-cancel" title="Discard changes" onClick={() => setEditing(false)}>✕</button>
          </span>
        ) : (
          <div className="wo-actions">
            <button className="risk-edit" title="Modify order" aria-label="Modify order" onClick={startEdit}>✎</button>
            <button
              className="wo-cancel"
              title="Cancel order"
              onClick={() => { if (marketGate(o.underlying)) cancelOrder(o.id); }}
            >✕</button>
          </div>
        )}
      </td>
      {showRoll && <td className="c-roll" />}
    </tr>
  );
}

/** How each monitoring state is shown. The rule this table encodes: a position
 *  Charticks is NOT actively protecting must never render like one it is.
 *  `alarm` positions get the loud treatment and are counted in the banner. */
const MONITOR_BADGE: Record<
  MonitorState,
  { label: string; kind: "ok" | "warn" | "alarm" | "info" }
> = {
  protected: { label: "🟢 Managed", kind: "ok" },
  no_rule: { label: "🟡 No SL / Target", kind: "info" },
  exiting: { label: "⏳ Exiting", kind: "info" },
  feed_lost: { label: "⚠ Feed Lost — Automation Paused", kind: "alarm" },
  paused: { label: "⚠ Monitoring Paused", kind: "alarm" },
  restoring: { label: "⏳ Confirming with broker", kind: "warn" },
  unmanaged: { label: "🔴 Not Protected", kind: "alarm" },
};

/** Plain-English name for why a position is being exited, for the row badge. */
const EXIT_LABEL: Record<string, string> = {
  "stop-loss": "Stop Loss",
  target: "Target",
  "portfolio-trail": "Portfolio Trail",
  "square-off": "Square Off",
  "manual-exit": "Closing",
  roll: "Rolling",
};

function MonitorBadge({ p }: { p: LivePosition }) {
  // An exit in flight outranks every other state. It is the most specific thing
  // true about the position, it is what the user just did, and it is the reason
  // the row's controls are locked — so it must be what the row says.
  if ((p.exitPendingQty ?? 0) > 0) {
    const why = EXIT_LABEL[p.exitReason ?? ""] ?? "Closing";
    const partial = (p.exitPendingQty ?? 0) < p.qty;
    return (
      <span
        className="mon-badge pending"
        title={`${why} — ${p.exitPendingQty} of ${p.qty} qty is at the broker. `
          + `This row will close, or return to normal if the exit is refused.`}
      >
        ⏳ Exit Pending{partial ? ` · ${p.exitPendingQty} qty` : ""} · {why}
      </span>
    );
  }
  // No state yet (an event from an older sidecar) is treated as unmanaged
  // rather than assumed safe — the whole point is that silence never reads as
  // "protected".
  const state: MonitorState = p.monitorState ?? (p.managed ? "restoring" : "unmanaged");
  const badge = MONITOR_BADGE[state] ?? MONITOR_BADGE.unmanaged;
  return (
    <span className={`mon-badge ${badge.kind}`} title={p.monitorDetail || badge.label}>
      {badge.label}
    </span>
  );
}

/** Which side of a hedge relationship this row is on.
 *
 *  A protective leg is not an independent trade and must not read like one: the
 *  grid says what it protects, so a long sitting next to a short is obviously
 *  one strategy rather than two unrelated positions. The short says it is
 *  covered for the same reason. */
function HedgeTag({ p }: { p: LivePosition }) {
  const positions = useMarketStore((s) => s.positions);
  const name = (id: string) => positions[id]?.symbol ?? id.split("|").slice(2).join(" ");
  if (p.hedgeFor && p.hedgeFor.length > 0) {
    const covers = p.hedgeFor.map(name).join(", ");
    return (
      <span className="hedge-tag child" title={`Protective hedge for ${covers}`}>
        🛡 Hedge · {covers}
      </span>
    );
  }
  if (p.hedgedBy) {
    return (
      <span className="hedge-tag parent" title={`Hedged by ${name(p.hedgedBy)}`}>
        🛡 Hedged
      </span>
    );
  }
  return null;
}

/** Live position row. Charticks-managed positions carry their SL / Target and
 *  the same partial-exit controls as paper; positions opened elsewhere are
 *  clearly marked and offer Manage / Ignore instead. */
function LivePositionRow({
  p,
  showRoll,
  onRoll,
  onAdopt,
}: {
  p: LivePosition;
  showRoll: boolean;
  onRoll: (p: LivePosition, dir: RollDirection) => void;
  onAdopt: (p: LivePosition) => void;
}) {
  const setRisk = usePositionsStore((s) => s.setRisk);
  const closePosition = usePositionsStore((s) => s.closePosition);
  const partialExits = useSettingsStore((s) => s.active().order.partialExits);
  const partialFractions = (partialExits.length ? partialExits : [100])
    .slice()
    .sort((a, b) => a - b)
    .map((v) => v / 100);
  // Prefer the structured contract the sidecar sends; fall back to parsing the
  // symbol for foreign legs, which carry no canonical identity.
  const parsed = parseOptionSymbol(p.symbol);
  const underlying = p.underlying ?? parsed?.underlying ?? "";
  const managed = p.managed === true;
  // An exit is at the broker. Everything that would change this position is
  // locked until it resolves: a roll, an SL edit or a second partial exit sent
  // now would be racing an order whose outcome nobody knows yet, and would be
  // sized against a quantity that is about to change.
  const exiting = (p.exitPendingQty ?? 0) > 0;
  const rollable = managed && !exiting && (p.optType != null || parsed != null);

  return (
    <tr className={`${managed ? "" : "unmanaged-row"} ${exiting ? "exiting-row" : ""}`}>
      <td className="c-inst">
        <div className="sym">
          {p.symbol}
          <ExpiryTag expiry={p.expiry ?? parsed?.expiry} />
          <MonitorBadge p={p} />
          <HedgeTag p={p} />
        </div>
        <div className="sub">
          <span className={`dir ${p.side === "BUY" ? "l" : "s"}`}>{p.side === "BUY" ? "L" : "S"}</span>
          {p.qty} Qty
          {managed && (
            <>
              {" "}
              <EditableRisk
                label="SL"
                value={p.sl ?? undefined}
                disabled={exiting}
                onSave={(n) => setRisk(p.id, { sl: n })}
              />
              <EditableRisk
                label="Tgt"
                value={p.target ?? undefined}
                disabled={exiting}
                onSave={(n) => setRisk(p.id, { target: n })}
              />
            </>
          )}
        </div>
      </td>
      <td className="c-num num">{p.entry.toFixed(2)}</td>
      <td className="c-num num">—</td>
      <td className="c-num">
        <FlashNumber value={p.ltp} format={(n) => n.toFixed(2)} className="ltp-cell" />
      </td>
      <td className="c-pnl">
        <FlashNumber value={p.pnl} format={money} className={`pnl-cell ${p.pnl >= 0 ? "up" : "down"}`} />
      </td>
      <td className="c-adj">
        {/* Live positions resize with REAL orders now: adding places an entry
            for the extra lots, reducing is a partial exit of exactly that many.
            The cell was empty because the endpoint routed to the paper engine
            whatever the mode, so on a live position it did nothing at all. */}
        {managed && (
          <AdjLotsStepper id={p.id} underlying={underlying} disabled={exiting} />
        )}
      </td>
      <td className="c-close">
        {exiting ? (
          // No buttons and no second row: the exit already sent IS this
          // position's current state, and it is shown in the badge above.
          <span className="close-pending" title={p.exitReason ?? "exit in progress"}>
            Exit sent
          </span>
        ) : managed ? (
          <div className="close-cell">
            {partialFractions.map((f) => (
              <button
                key={f}
                className="closeb"
                title={`Exit ${f * 100}% of position at market`}
                onClick={async () => {
                  if (!marketGate(underlying)) return;
                  await execDelay();
                  closePosition(p.id, f);
                }}
              >
                {f * 100}%
              </button>
            ))}
          </div>
        ) : (
          <button
            className="adoptb"
            title="Apply this instrument's Stop Loss / Target / Trail to this position and manage it from now on"
            onClick={() => onAdopt(p)}
          >
            Manage
          </button>
        )}
      </td>
      {showRoll && (
        <td className="c-roll">
          {rollable && (
            <div className="roll">
              <button title="Roll up" onClick={() => onRoll(p, "up")}>⬆</button>
              <button title="Roll down" onClick={() => onRoll(p, "down")}>⬇</button>
            </div>
          )}
        </td>
      )}
    </tr>
  );
}

/** A hedge whose last protected short has closed.
 *
 *  Asked rather than decided: closing it is an exit the user never requested,
 *  keeping it silently leaves a long position they never chose to hold on its
 *  own. Both are legitimate — which one is right depends on what they were
 *  trading, which Charticks cannot know. The hedge stays exactly as it is,
 *  fully managed, until they answer.
 */
function OrphanedHedgeDialog() {
  const queue = useMarketStore((s) => s.orphanedHedges);
  const dismiss = useMarketStore((s) => s.dismissOrphanedHedge);
  const [busy, setBusy] = useState(false);
  const hedge = queue[0];
  if (!hedge) return null;

  const decide = async (action: "close" | "keep") => {
    setBusy(true);
    try {
      await bridge.post("/positions/hedge-decision", {
        hedgeId: hedge.hedgeId, action,
      }).catch(() => {});
      dismiss(hedge.hedgeId);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-label="Hedge still open">
        <h4>The hedge for this trade is still open</h4>
        <p>
          <b>{hedge.symbol}</b> was opened automatically to protect a short
          position that has now closed. Nothing else is relying on it, so it is
          currently a long position on its own.
        </p>
        <p className="dim">
          {hedge.qty} qty · running P&amp;L{" "}
          <b className={hedge.pnl >= 0 ? "up" : "down"}>{money(hedge.pnl)}</b>
        </p>
        <p className="dim">
          Closing it exits at market now. Keeping it leaves it as an ordinary
          position of your own, managed like any other — Charticks will stop
          treating it as somebody else's protection.
        </p>
        <div className="modal-actions">
          <button className="btn-ghost" disabled={busy} onClick={() => void decide("keep")}>
            Keep Hedge Open
          </button>
          <button className="btn-primary" disabled={busy} onClick={() => void decide("close")}>
            Close Hedge
          </button>
        </div>
      </div>
    </div>
  );
}

/** One completed live trade.
 *
 *  Kept because a trade that has been opened, managed and closed is the thing a
 *  trader most wants to look back at, and the application used to delete it the
 *  instant the position went flat — the Positions tab could not tell you what
 *  you had just done. */
function ClosedPositionRow({ p, showRoll }: { p: ClosedPosition; showRoll: boolean }) {
  const pnl = p.realised ?? 0;
  const held = p.openedTs && p.closedTs
    ? `${new Date(p.openedTs).toLocaleTimeString("en-GB")} → `
      + `${new Date(p.closedTs).toLocaleTimeString("en-GB")}`
    : undefined;
  return (
    <tr className="closed-row">
      <td className="c-inst">
        <div className="sym">
          {p.symbol}
          <ExpiryTag expiry={p.expiry} />
          <span className="mon-badge done" title={held}>✓ Closed</span>
        </div>
        <div className="sub">
          <span className={`dir ${p.side === "BUY" ? "l" : "s"}`}>
            {p.side === "BUY" ? "L" : "S"}
          </span>
          {p.qty} Qty
          {held && <span className="closed-when"> · {held}</span>}
        </div>
      </td>
      <td className="c-num num">{p.entry.toFixed(2)}</td>
      <td className="c-num num">{p.exit != null ? p.exit.toFixed(2) : "—"}</td>
      <td className="c-num num">—</td>
      <td className="c-pnl">
        <span className={`pnl-cell ${pnl >= 0 ? "up" : "down"}`}>{money(pnl)}</span>
      </td>
      <td className="c-adj" />
      <td className="c-close" />
      {showRoll && <td className="c-roll" />}
    </tr>
  );
}

/** Persistent banner listing every position Charticks currently cannot protect.
 *  Deliberately not dismissible and not a toast: the danger lasts exactly as
 *  long as the condition, so the warning does too. */
function MonitorAlarmBanner() {
  const alarm = useMarketStore((s) => s.monitorAlarm);
  const positions = useMarketStore((s) => s.positions);
  if (alarm.length === 0) return null;
  const label = (id: string) => positions[id]?.symbol ?? id;
  return (
    <div className="mon-alarm" role="alert">
      <div className="mon-alarm-head">
        ⚠ {alarm.length} position{alarm.length === 1 ? "" : "s"} not protected
      </div>
      <ul>
        {alarm.map((a) => (
          <li key={a.id}>
            <b>{label(a.id)}</b> — {a.detail || MONITOR_BADGE[a.state]?.label || a.state}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Positions panel backed by the live broker position book (per-account, with
 *  aggregate P&L) whenever a broker is connected. */
function LivePositionGridPanel() {
  const positionsMap = useMarketStore((s) => s.positions);
  const netPnl = useMarketStore((s) => s.netPnl);
  const closed = useMarketStore((s) => s.closedPositions);
  const clearClosed = useMarketStore((s) => s.clearClosedPositions);
  const closedPnl = closed.reduce((a, p) => a + (p.realised ?? 0), 0);
  const showRoll = useGridPrefsStore((s) => s.cols.roll);
  const ruleFor = useSettingsStore((s) => s.ruleFor);
  const positions = Object.values(positionsMap).filter((p) => p.qty > 0);
  const colCount = 7 + (showRoll ? 1 : 0);
  const [rolling, setRolling] = useState<{ position: LivePosition; dir: RollDirection } | null>(null);
  // Positions opened outside Charticks are never adopted silently — the user
  // is shown exactly which stop and target would be applied, and confirms it.
  const [adopting, setAdopting] = useState<LivePosition | null>(null);
  const [squaringOff, setSquaringOff] = useState(false);
  const [rollError, setRollError] = useState("");
  const unprotected = positions.filter((p) => p.managed !== true).length;

  // Adapt a live position into the Roll Decider's target shape (same logic the
  // paper panel uses); non-option legs are filtered out before this runs. The
  // parsed expiry keeps the roll — and its quotes — on the same contract series.
  const rollTarget = (() => {
    if (!rolling) return null;
    const p = rolling.position;
    // The structured contract the sidecar sends is authoritative; parsing the
    // display symbol is only the fallback for a leg that carries none.
    const parsed =
      p.underlying && p.optType && p.strike != null
        ? { underlying: p.underlying, expiry: p.expiry ?? "", strike: p.strike, optType: p.optType }
        : parseOptionSymbol(p.symbol);
    if (!parsed) return null;
    const size = lotSize(parsed.underlying);
    return {
      ...parsed,
      lots: p.lots && p.lots > 0 ? p.lots : Math.max(1, Math.round(p.qty / size)),
    };
  })();

  return (
    <section className="panel pos-grid-panel">
      <div className="phead pos-head">
        <h3>Positions</h3>
        <span className="tag">Live</span>
        <DefaultsChip />
        <PortfolioTrailChip />
        <span className="grow" />
        <span className="head-pnl">
          <span className="lab">Net P&amp;L</span>
          <FlashNumber value={netPnl} format={money} className={netPnl >= 0 ? "up" : "down"} />
        </span>
        <span className="live-count">
          Live : <b className="num">{positions.length}</b>
        </span>
        {unprotected > 0 && (
          <span className="unprotected-count" title="Positions Charticks is not managing">
            Unprotected : <b className="num">{unprotected}</b>
          </span>
        )}
        <ColumnMenu />
        <button
          className="sq-off"
          disabled={positions.length === 0}
          onClick={() => setSquaringOff(true)}
          title="Square off every open position at market"
        >
          Square Off All
        </button>
      </div>

      <SessionLimitsBar rollingPnl={netPnl} />
      <MonitorAlarmBanner />
      <PositionActionError />
      <OrphanedHedgeDialog />

      <div className="pbody">
        <table className="pos-grid">
          <colgroup>
            <col className="w-inst" />
            <col className="w-num" />
            <col className="w-num" />
            <col className="w-num" />
            <col className="w-pnl" />
            <col className="w-adj" />
            <col className="w-close" />
            {showRoll && <col className="w-roll" />}
          </colgroup>
          <thead>
            <tr>
              <th>Instrument</th>
              <th className="c-num">Entry</th>
              <th className="c-num">Exit</th>
              <th className="c-num">LTP</th>
              <th className="c-pnl">P&amp;L</th>
              <th className="c-adj">AdjLots</th>
              <th className="c-close">Close</th>
              {showRoll && <th className="c-roll">Roll</th>}
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <LivePositionRow
                key={p.id}
                p={p}
                showRoll={showRoll}
                onRoll={(position, dir) => setRolling({ position, dir })}
                onAdopt={setAdopting}
              />
            ))}
            {positions.length === 0 && closed.length === 0 && (
              <tr>
                <td colSpan={colCount} className="empty">No open positions</td>
              </tr>
            )}
            {closed.length > 0 && (
              <tr className="section-row">
                <td colSpan={colCount}>
                  <span>Closed today · {closed.length}</span>
                  <span className="grow" />
                  <span className={`num ${closedPnl >= 0 ? "up" : "down"}`}>
                    {money(closedPnl)}
                  </span>
                  <button className="sf-link" onClick={clearClosed}>Clear</button>
                </td>
              </tr>
            )}
            {/* Newest first: the trade just finished is the one being looked at. */}
            {[...closed].reverse().map((p) => (
              <ClosedPositionRow key={p.id} p={p} showRoll={showRoll} />
            ))}
          </tbody>
        </table>
      </div>

      {rolling && rollTarget && (
        <RollDialog
          position={rollTarget}
          direction={rolling.dir}
          // A live roll is two real broker orders, sequenced by the sidecar:
          // the current leg is closed and the new one is opened only once the
          // broker confirms that close. Nothing is written to the local grid —
          // the rolled position appears when the broker reports it, exactly
          // like every other live position.
          onRoll={async (newStrike) => {
            if (!marketGate(rollTarget.underlying)) return;
            const res = await bridge
              .post<{ ok: boolean; error?: string }>("/positions/roll", {
                id: rolling.position.id,
                newStrike,
              })
              .catch(() => ({ ok: false, error: "The roll could not be sent." }));
            if (!res.ok) setRollError(res.error ?? "The roll was not accepted.");
          }}
          onClose={() => setRolling(null)}
        />
      )}

      <InfoDialog
        open={rollError !== ""}
        title="Roll not sent"
        message={rollError}
        onClose={() => setRollError("")}
      />

      <ConfirmDialog
        open={adopting != null}
        title="Manage this position with Charticks?"
        message={
          adopting
            ? `${adopting.symbol} was not opened by Charticks, so nothing is ` +
              `protecting it. Managing it applies your ` +
              `${adopting.underlying ?? "instrument"} defaults — Stop Loss, ` +
              `Target and Trail SL — against the broker's entry price of ` +
              `₹${adopting.entry.toFixed(2)}, and Charticks will exit it ` +
              `automatically when they are hit.`
            : ""
        }
        confirmLabel="Manage"
        onConfirm={async () => {
          const target = adopting;
          setAdopting(null);
          if (!target) return;
          // The instrument's own defaults, exactly as a new trade on it would
          // capture them. Falls back to NIFTY's when the leg has no canonical
          // underlying (a foreign row the sidecar could not parse).
          await bridge
            .post("/positions/adopt", {
              id: target.id,
              rule: ruleFor(target.underlying ?? "NIFTY"),
            })
            .catch(() => {});
        }}
        onCancel={() => setAdopting(null)}
      />

      <ConfirmDialog
        open={squaringOff}
        danger
        title="Square off all positions?"
        message={
          `This closes all ${positions.length} open position` +
          `${positions.length === 1 ? "" : "s"} at market — including any ` +
          `Charticks is not managing. This cannot be undone.`
        }
        confirmLabel="Square Off All"
        onConfirm={async () => {
          setSquaringOff(false);
          if (!positions.some((p) => marketGateSilent(p.underlying ?? ""))) {
            marketGate();
            return;
          }
          await execDelay();
          await bridge.post("/positions/square-off").catch(() => {});
        }}
        onCancel={() => setSquaringOff(false)}
      />
    </section>
  );
}

export function PositionGridPanel() {
  // The trading mode — not the broker connection — decides which book shows:
  // Live routes to the broker position book; Paper shows the simulated
  // (client-side) session so paper trades are visible even with a broker
  // connected for its market feed.
  const mode = useTradingModeStore((s) => s.mode);
  if (mode === "live") return <LivePositionGridPanel />;
  return <MockPositionGridPanel />;
}

function MockPositionGridPanel() {
  const positions = usePositionsStore((s) => s.positions);
  const squareOffAll = usePositionsStore((s) => s.squareOffAll);
  const orders = useOrdersStore((s) => s.orders);
  const showRoll = useGridPrefsStore((s) => s.cols.roll);
  const [confirming, setConfirming] = useState(false);
  const [rolling, setRolling] = useState<{ position: OptionPosition; dir: RollDirection } | null>(null);
  const onRoll = (position: OptionPosition, dir: RollDirection) => setRolling({ position, dir });

  const netPnl = positions.reduce((a, p) => a + pnlOf(p), 0);
  const live = positions.filter((p) => p.status === "OPEN").length;
  // Working orders (pending / partially filled limit orders) surface above the
  // live positions so a submitted order is immediately visible.
  const workingOrders = orders.filter(isWorking);
  const working = workingOrders.length;
  // Open (live) positions first, closed sink to the bottom; stable within group.
  const ordered = [...positions].sort(
    (a, b) => (a.status === "OPEN" ? 0 : 1) - (b.status === "OPEN" ? 0 : 1),
  );
  const colCount = 7 + (showRoll ? 1 : 0);

  return (
    <section className="panel pos-grid-panel">
      <div className="phead pos-head">
        <h3>Positions</h3>
        <span className="tag">Paper</span>
        <DefaultsChip />
        <PortfolioTrailChip />
        <span className="grow" />
        <span className="head-pnl">
          <span className="lab">Net P&amp;L</span>
          <FlashNumber value={netPnl} format={money} className={netPnl >= 0 ? "up" : "down"} />
        </span>
        <span className="live-count">
          Open : <b className="num">{live}</b>
        </span>
        {working > 0 && (
          <span className="working-count">
            Working : <b className="num">{working}</b>
          </span>
        )}
        <ColumnMenu />
        <button
          className="sq-off"
          disabled={live === 0}
          onClick={() => setConfirming(true)}
          title="Square off all open positions at market"
        >
          Square Off All
        </button>
      </div>

      <SessionLimitsBar rollingPnl={netPnl} />

      <div className="pbody">
        <table className="pos-grid">
          <colgroup>
            <col className="w-inst" />
            <col className="w-num" />
            <col className="w-num" />
            <col className="w-num" />
            <col className="w-pnl" />
            <col className="w-adj" />
            <col className="w-close" />
            {showRoll && <col className="w-roll" />}
          </colgroup>
          <thead>
            <tr>
              <th>Instrument</th>
              <th className="c-num">Entry</th>
              <th className="c-num">Exit</th>
              <th className="c-num">LTP</th>
              <th className="c-pnl">P&amp;L</th>
              <th className="c-adj">AdjLots</th>
              <th className="c-close">Close</th>
              {showRoll && <th className="c-roll">Roll</th>}
            </tr>
          </thead>
          <tbody>
            {workingOrders.map((o) => (
              <WorkingOrderRow key={o.id} o={o} showRoll={showRoll} />
            ))}
            {ordered.map((p) => (
              <PositionRow key={p.id} p={p} showRoll={showRoll} onRoll={onRoll} />
            ))}
            {ordered.length === 0 && workingOrders.length === 0 && (
              <tr>
                <td colSpan={colCount} className="empty">No positions</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <ConfirmDialog
        open={confirming}
        danger
        title="Square off all positions?"
        message={`This will close all ${live} open position${live === 1 ? "" : "s"} at market price. This cannot be undone.`}
        confirmLabel="Square Off All"
        onConfirm={async () => {
          setConfirming(false);
          // Spans instruments — allowed while ANY open position is in session.
          if (!positions.some((x) => x.status === "OPEN" && marketGateSilent(x.underlying))) {
            marketGate();
            return;
          }
          await execDelay();
          squareOffAll();
        }}
        onCancel={() => setConfirming(false)}
      />

      {rolling && (
        <RollDialog
          position={rolling.position}
          direction={rolling.dir}
          onRoll={async (newStrike, premium) => {
            // Rolling closes one leg and opens another — a trading action.
            if (!marketGate(rolling.position.underlying)) return;
            await execDelay();
            usePositionsStore.getState().rollPosition(rolling.position.id, newStrike, premium);
          }}
          onClose={() => setRolling(null)}
        />
      )}
    </section>
  );
}
