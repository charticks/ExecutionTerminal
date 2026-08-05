import { useEffect, useRef, useState } from "react";
import { FlashNumber } from "@/components/FlashNumber";
import { ConfirmDialog } from "@/components/ConfirmDialog";
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
} from "@/stores/useMarketStore";
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

  return (
    <div className="lots-adj">
      <button
        disabled={disabled}
        onClick={() => { if (marketGate(underlying)) adjustLots(id, -amount); }}
        aria-label="Reduce lots"
      >
        −
      </button>
      <input
        className="num"
        type="text"
        inputMode="numeric"
        value={amount}
        disabled={disabled}
        aria-label="Lots to adjust"
        onChange={(e) => {
          const n = parseInt(e.target.value.replace(/\D/g, ""), 10);
          setAmount(Number.isNaN(n) ? 0 : n);
        }}
        onBlur={() => setAmount((a) => (a < 1 ? 1 : a))}
      />
      <button
        disabled={disabled}
        onClick={() => { if (marketGate(underlying)) adjustLots(id, +amount); }}
        aria-label="Add lots"
      >
        +
      </button>
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

/** Live broker-position row (read-only). The broker position book gives
 *  symbol/side/qty/entry/ltp/pnl; interactive order actions (adjust/close/roll)
 *  require the order engine and are disabled here until that is wired. */
function LivePositionRow({
  p,
  showRoll,
  onRoll,
}: {
  p: LivePosition;
  showRoll: boolean;
  onRoll: (p: LivePosition, dir: RollDirection) => void;
}) {
  // Roll applies only to option legs (a parseable strike/CE-PE symbol).
  const parsed = parseOptionSymbol(p.symbol);
  const rollable = parsed != null;
  return (
    <tr>
      <td className="c-inst">
        <div className="sym">
          {p.symbol}
          <ExpiryTag expiry={parsed?.expiry} />
        </div>
        <div className="sub">
          <span className={`dir ${p.side === "BUY" ? "l" : "s"}`}>{p.side === "BUY" ? "L" : "S"}</span>
          {p.qty} Qty
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
      <td className="c-adj" />
      <td className="c-close" />
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

/** Positions panel backed by the live broker position book (per-account, with
 *  aggregate P&L) whenever a broker is connected. */
function LivePositionGridPanel() {
  const positionsMap = useMarketStore((s) => s.positions);
  const netPnl = useMarketStore((s) => s.netPnl);
  const rollLivePosition = useMarketStore((s) => s.rollPosition);
  const showRoll = useGridPrefsStore((s) => s.cols.roll);
  const positions = Object.values(positionsMap).filter((p) => p.qty > 0);
  const colCount = 7 + (showRoll ? 1 : 0);
  const [rolling, setRolling] = useState<{ position: LivePosition; dir: RollDirection } | null>(null);

  // Adapt a live position into the Roll Decider's target shape (same logic the
  // paper panel uses); non-option legs are filtered out before this runs. The
  // parsed expiry keeps the roll — and its quotes — on the same contract series.
  const rollTarget = (() => {
    if (!rolling) return null;
    const parsed = parseOptionSymbol(rolling.position.symbol);
    if (!parsed) return null;
    const size = lotSize(parsed.underlying);
    return {
      ...parsed,
      lots: Math.max(1, Math.round(rolling.position.qty / size)),
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
        <ColumnMenu />
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
            {positions.map((p) => (
              <LivePositionRow
                key={p.id}
                p={p}
                showRoll={showRoll}
                onRoll={(position, dir) => setRolling({ position, dir })}
              />
            ))}
            {positions.length === 0 && (
              <tr>
                <td colSpan={colCount} className="empty">No open positions</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {rolling && rollTarget && (
        <RollDialog
          position={rollTarget}
          direction={rolling.dir}
          onRoll={(newStrike, premium) => {
            if (!marketGate(rollTarget.underlying)) return;
            rollLivePosition(rolling.position.id, newStrike, premium);
          }}
          onClose={() => setRolling(null)}
        />
      )}
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
