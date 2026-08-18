import { useEffect, useState } from "react";
import * as diagnostics from "@/lib/diagnostics";
import { INDEX_BY_ID } from "@/lib/indices";
import type { RiskMode, TrailMode } from "@/lib/risk";
import {
  useSettingsStore,
  FALLBACK_INSTRUMENT_DEFAULT,
  WEEKDAYS,
  WEEKDAY_LABEL,
  WEEKDAY_SUBLABEL,
  STYLE_LABEL,
  SETTINGS_INSTRUMENTS,
  PARTIAL_EXIT_CHOICES,
  MAX_POS_BEHAVIORS,
  MAX_POS_BEHAVIOR_LABEL,
  type TradingStyle,
  type InstrumentDefault,
  type ProfileConfig,
} from "@/stores/useSettingsStore";
import {
  Card,
  Field,
  Toggle,
  Segmented,
  NumInput,
  Check,
  FeatureBlock,
  Collapsible,
  SectionTitle,
} from "@/components/SettingsControls";

const MODE_OPTS: { value: RiskMode; label: string }[] = [
  { value: "points", label: "Points" },
  { value: "percent", label: "Percentage" },
];

const TRAIL_MODE_OPTS: { value: TrailMode; label: string }[] = [
  { value: "point", label: "Point Based" },
  { value: "profit", label: "Profit Based" },
];

const STYLES: TradingStyle[] = ["SELLER", "BUYER", "HYBRID"];
const STYLE_DESC: Record<TradingStyle, string> = {
  SELLER: "Optimized for option selling",
  BUYER: "Optimized for option buying",
  HYBRID: "Both buyer & seller settings",
};

/** Trading Style controls only which cards are visible — hidden values are kept.
 *  Sellers rely on protective hedges; Buyers don't, so the Hedging card is
 *  hidden for Buyer. Hybrid shows everything. */
function showsHedging(style: TradingStyle): boolean {
  return style !== "BUYER";
}

/** Unit suffix for the Instrument Defaults column headers — the values are
 *  interpreted per the Trade Defaults modes above them. */
export function unit(mode: RiskMode): string {
  return mode === "percent" ? "(%)" : "(Points)";
}

// The page edits a local draft of the active profile; nothing reaches the store
// until "Save Changes". Each card gets the draft slice plus a patcher.
interface Draft {
  draft: ProfileConfig;
  patch: <K extends keyof Omit<ProfileConfig, "style">>(
    section: K,
    value: Partial<ProfileConfig[K]>,
  ) => void;
}

// ── Header: profile selector, style, auto-select, banner ─────────────────────
function ProfileHeader({ style, onStyle }: { style: TradingStyle; onStyle: (s: TradingStyle) => void }) {
  const activeProfile = useSettingsStore((s) => s.activeProfile);
  const setActiveProfile = useSettingsStore((s) => s.setActiveProfile);
  const autoSelect = useSettingsStore((s) => s.autoSelect);
  const setAutoSelect = useSettingsStore((s) => s.setAutoSelect);
  const sessionOverride = useSettingsStore((s) => s.sessionOverride);

  return (
    <div className="sf-header">
      <div className="sf-header-top">
        <div className="sf-profiles" role="radiogroup" aria-label="Trading Profile">
          {WEEKDAYS.map((d) => (
            <button
              key={d}
              role="radio"
              aria-checked={activeProfile === d}
              className={`sf-profile ${activeProfile === d ? "on" : ""}`}
              onClick={() => setActiveProfile(d)}
            >
              <span className="pf-day">{WEEKDAY_LABEL[d]}</span>
              <span className="pf-sub">{WEEKDAY_SUBLABEL[d]}</span>
            </button>
          ))}
        </div>
        <div className="sf-styles">
          {STYLES.map((st) => (
            <button
              key={st}
              className={`sf-style ${style === st ? "on" : ""}`}
              onClick={() => onStyle(st)}
              title={STYLE_DESC[st]}
            >
              <span className="st-name">{STYLE_LABEL[st]}</span>
              <span className="st-desc">{STYLE_DESC[st]}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="sf-banner">
        <span className="sf-banner-msg">
          ✓ You are editing <b>{WEEKDAY_LABEL[activeProfile]} ({STYLE_LABEL[style]})</b> profile.
          Changes save only to this profile.
        </span>
        <label className="sf-auto">
          <input
            type="checkbox"
            checked={autoSelect}
            onChange={(e) => setAutoSelect(e.target.checked)}
          />
          Auto-select profile by weekday
        </label>
      </div>

      {sessionOverride && (
        <div className="sf-override" role="status">
          {WEEKDAY_LABEL[activeProfile]} profile is active for this session.
          Automatic selection resumes the next time Charticks starts.
        </div>
      )}
    </div>
  );
}

// ── Trade Defaults ───────────────────────────────────────────────────────────
/** Each risk feature is independently switchable. Unchecking greys its mode
 *  control out (rather than hiding it) and drops the feature from every FUTURE
 *  trade — positions already open keep the rule they captured at entry. */
function TradeDefaultsCard({ draft, patch }: Draft) {
  const trade = draft.trade;
  const [trailWarning, setTrailWarning] = useState(false);

  // Trail SL only ever MOVES a stop, so it cannot work without one. Turning
  // Stop Loss off therefore also turns Trail SL off, and the Trail SL checkbox
  // stays locked (with an explanation) until Stop Loss comes back.
  const setSlEnabled = (slEnabled: boolean) => {
    patch("trade", slEnabled ? { slEnabled } : { slEnabled, trailEnabled: false });
    if (slEnabled) setTrailWarning(false);
  };
  const setTrailEnabled = (trailEnabled: boolean) => {
    if (trailEnabled && !trade.slEnabled) {
      setTrailWarning(true);
      return;
    }
    setTrailWarning(false);
    patch("trade", { trailEnabled });
  };

  return (
    <Card title="Trade Defaults" hint="Applied to future trades only" span={6}>
      <div className="sf-features">
        <FeatureBlock
          label="Stop Loss"
          checked={trade.slEnabled}
          onChange={setSlEnabled}
        >
          <Segmented
            compact
            disabled={!trade.slEnabled}
            value={trade.slMode}
            options={MODE_OPTS}
            onChange={(slMode) => patch("trade", { slMode })}
            aria-label="Stop Loss mode"
          />
        </FeatureBlock>

        <FeatureBlock
          label="Target"
          checked={trade.targetEnabled}
          onChange={(targetEnabled) => patch("trade", { targetEnabled })}
        >
          <Segmented
            compact
            disabled={!trade.targetEnabled}
            value={trade.targetMode}
            options={MODE_OPTS}
            onChange={(targetMode) => patch("trade", { targetMode })}
            aria-label="Target mode"
          />
        </FeatureBlock>

        <FeatureBlock
          label="Trail SL"
          checked={trade.trailEnabled}
          onChange={setTrailEnabled}
          lockReason={trade.slEnabled ? undefined : "Requires Stop Loss"}
        >
          <Segmented
            compact
            disabled={!trade.trailEnabled}
            value={trade.trailMode}
            options={TRAIL_MODE_OPTS}
            onChange={(trailMode) => patch("trade", { trailMode })}
            aria-label="Trail SL mode"
          />
        </FeatureBlock>

        {trailWarning && !trade.slEnabled && (
          <div className="sf-warning" role="alert">
            Trail SL requires Stop Loss to be enabled. Please enable Stop Loss first.
          </div>
        )}
      </div>
    </Card>
  );
}

// ── Instrument Defaults ──────────────────────────────────────────────────────
export interface DefaultsColumn {
  key: keyof InstrumentDefault;
  label: string;
}

/** The columns the table shows, derived from which features are enabled and
 *  which Trail SL mode is selected — an off feature contributes no column, so
 *  the table never carries dead space. */
export function defaultsColumns(trade: ProfileConfig["trade"]): DefaultsColumn[] {
  const cols: DefaultsColumn[] = [];
  if (trade.slEnabled) cols.push({ key: "sl", label: `SL ${unit(trade.slMode)}` });
  if (trade.targetEnabled) cols.push({ key: "target", label: `Target ${unit(trade.targetMode)}` });
  // Trailing depends on Stop Loss, so its columns disappear with the SL column.
  if (trade.trailEnabled && trade.slEnabled) {
    if (trade.trailMode === "point") {
      cols.push({ key: "trailAfter", label: "Trail After (Points)" });
      cols.push({ key: "trailStep", label: "Trail Step (Points)" });
    } else {
      cols.push({ key: "startTrail", label: "Start Trail (₹)" });
      cols.push({ key: "profitStep", label: "Trail Step (₹)" });
    }
  }
  return cols;
}

function InstrumentDefaultsCard({ draft, patch }: Draft) {
  const instruments = draft.instruments;
  const cols = defaultsColumns(draft.trade);

  const edit = (id: string, key: keyof InstrumentDefault, raw: string) => {
    const n = parseInt(raw.replace(/\D/g, ""), 10);
    const current = instruments[id] ?? FALLBACK_INSTRUMENT_DEFAULT;
    patch("instruments", { [id]: { ...current, [key]: Number.isNaN(n) ? 0 : n } });
  };

  return (
    <Card title="Instrument Defaults" hint="Values new trades inherit" span={6}>
      {cols.length === 0 ? (
        <div className="sf-empty">
          Every trade-management feature is off — new trades will carry no SL,
          Target or Trail SL.
        </div>
      ) : (
        <table className="sf-defaults">
          <thead>
            <tr>
              <th>Instrument</th>
              {cols.map((c) => (
                <th key={c.key} className="c-num">{c.label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {SETTINGS_INSTRUMENTS.map((id) => (
              <tr key={id}>
                <td>{INDEX_BY_ID[id]?.name ?? id}</td>
                {cols.map((c) => (
                  <td key={c.key} className="c-num">
                    <input
                      className="sf-cell num"
                      inputMode="numeric"
                      value={instruments[id]?.[c.key] ?? 0}
                      onChange={(e) => edit(id, c.key, e.target.value)}
                      aria-label={`${id} ${c.label}`}
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

// ── Execution Defaults ───────────────────────────────────────────────────────
/** The everyday order settings. The four rarely-touched parameters live behind
 *  the collapsed "Advanced Execution" disclosure at the bottom of this card. */
function ExecutionDefaultsCard({ draft, patch }: Draft) {
  const order = draft.order;

  const togglePartial = (v: number) => {
    const has = order.partialExits.includes(v);
    const next = has
      ? order.partialExits.filter((x) => x !== v)
      : [...order.partialExits, v].sort((a, b) => a - b);
    patch("order", { partialExits: next });
  };

  return (
    <Card title="Execution Defaults" hint="Default values for placing / modifying orders" span={6}>
      <div className="sf-grid three">
        <Field label="Default Quantity">
          <NumInput value={order.defaultQty} min={1} onChange={(defaultQty) => patch("order", { defaultQty })} aria-label="Default quantity" />
        </Field>
        <Field label="Default Order Type">
          <Segmented
            value={order.orderType}
            options={[{ value: "MARKET", label: "Market" }, { value: "LIMIT", label: "Limit" }]}
            onChange={(orderType) => patch("order", { orderType })}
            aria-label="Default order type"
          />
        </Field>
        <Field label="Default Product">
          <Segmented
            value={order.product}
            options={[{ value: "NRML", label: "NRML" }, { value: "MIS", label: "MIS" }]}
            onChange={(product) => patch("order", { product })}
            aria-label="Default product"
          />
        </Field>
        <Field label="Order Validity">
          <Segmented
            value={order.validity}
            options={[{ value: "DAY", label: "DAY" }, { value: "IOC", label: "IOC" }]}
            onChange={(validity) => patch("order", { validity })}
            aria-label="Order validity"
          />
        </Field>
        <Field label="Partial Exit Options">
          <div className="sf-chips">
            {PARTIAL_EXIT_CHOICES.map((v) => (
              <button
                key={v}
                type="button"
                className={`sf-chip ${order.partialExits.includes(v) ? "on" : ""}`}
                aria-pressed={order.partialExits.includes(v)}
                onClick={() => togglePartial(v)}
              >
                {v}%
              </button>
            ))}
          </div>
        </Field>
      </div>

      <Collapsible title="Advanced Execution">
        <div className="sf-grid three">
          <Field label="Max Quantity / Order" hint="0 = no limit">
            <NumInput value={order.maxQtyPerOrder} onChange={(maxQtyPerOrder) => patch("order", { maxQtyPerOrder })} aria-label="Max quantity per order" />
          </Field>
          <Field label="Max Price" hint="0 = no limit">
            <NumInput value={order.maxPrice} allowDecimal onChange={(maxPrice) => patch("order", { maxPrice })} aria-label="Max price" />
          </Field>
          <Field label="Execution Delay (ms)" hint="Buy / Sell / Square Off / Exit / Roll">
            <NumInput value={order.execDelayMs} onChange={(execDelayMs) => patch("order", { execDelayMs })} aria-label="Execution delay" />
          </Field>
          <Field label="Entry Price Offset (%)">
            <NumInput value={order.entryOffsetPct} allowDecimal onChange={(entryOffsetPct) => patch("order", { entryOffsetPct })} aria-label="Entry price offset" />
          </Field>
        </div>
      </Collapsible>
    </Card>
  );
}

// ── Portfolio Trail Profit ───────────────────────────────────────────────────
/** One global portfolio-level feature — never per-instrument, never per-position.
 *  Once combined open P&L reaches "Activate After", a give-back of "Trail
 *  Distance" from the running peak squares off every open position. */
function PortfolioTrailCard({ draft, patch }: Draft) {
  const pt = draft.portfolioTrail;
  return (
    <Card title="Portfolio Trail Profit" hint="Combined P&L across all instruments" span={6}>
      <div className={`sf-feature sf-stack ${pt.enabled ? "" : "off"}`}>
        <Check
          checked={pt.enabled}
          onChange={(enabled) => patch("portfolioTrail", { enabled })}
          label="Trail Profit"
          className="sf-feature-head"
        />
        <div className="sf-feature-body">
          <div className="sf-grid two">
            <Field label="Activate After Profit (₹)">
              <NumInput
                value={pt.activateAfter}
                disabled={!pt.enabled}
                onChange={(activateAfter) => patch("portfolioTrail", { activateAfter })}
                aria-label="Activate after profit"
              />
            </Field>
            <Field label="Trail Distance (₹)">
              <NumInput
                value={pt.trailDistance}
                disabled={!pt.enabled}
                onChange={(trailDistance) => patch("portfolioTrail", { trailDistance })}
                aria-label="Trail distance"
              />
            </Field>
          </div>
          <p className="sf-note">
            Squares off <b>every</b> open position once profit falls
            ₹{pt.trailDistance.toLocaleString("en-IN")} from its peak, after the peak
            first reaches ₹{pt.activateAfter.toLocaleString("en-IN")}.
          </p>
        </div>
      </div>
    </Card>
  );
}

/** Advanced Max Position overflow control. Collapsed to a summary line once a
 *  behaviour is chosen; "Change" reopens the options. Hidden entirely when Max
 *  Positions is disabled (0) — the setting cannot apply then. */
function MaxPosBehaviorControl({ draft, patch }: Draft) {
  const risk = draft.risk;
  const [expanded, setExpanded] = useState(false);

  if (risk.maxPositions <= 0) return null;

  const toggle = (customizeMaxPos: boolean) => {
    patch("risk", { customizeMaxPos });
    setExpanded(customizeMaxPos);
  };

  return (
    <div className="sf-advanced">
      <label className="sf-check">
        <input
          type="checkbox"
          checked={risk.customizeMaxPos}
          onChange={(e) => toggle(e.target.checked)}
        />
        Customize Max Position Limit Behavior
      </label>

      {risk.customizeMaxPos && (
        expanded ? (
          <Field label="Behavior">
            <div className="sf-radios" role="radiogroup" aria-label="Max Position limit behavior">
              {MAX_POS_BEHAVIORS.map((b) => (
                <label key={b.value} className="radio">
                  <input
                    type="radio"
                    name="sf-maxpos"
                    checked={risk.maxPosBehavior === b.value}
                    onChange={() => {
                      patch("risk", { maxPosBehavior: b.value });
                      setExpanded(false);
                    }}
                  />
                  {b.label}
                </label>
              ))}
            </div>
          </Field>
        ) : (
          <div className="sf-summary">
            <span className="sf-summary-label">Behavior:</span>
            <span className="sf-summary-value">{MAX_POS_BEHAVIOR_LABEL[risk.maxPosBehavior]}</span>
            <button type="button" className="sf-link" onClick={() => setExpanded(true)}>
              ✏️ Change
            </button>
          </div>
        )
      )}
    </div>
  );
}

// ── Risk Defaults ────────────────────────────────────────────────────────────
function RiskDefaultsCard({ draft, patch }: Draft) {
  const risk = draft.risk;
  return (
    <Card title="Risk Defaults" hint="Defaults for new sessions — not the live Home limits" span={5}>
      <div className="sf-grid three">
        <Field label="Default Max Loss (₹)" hint="0 = disabled">
          <NumInput value={risk.maxLoss} onChange={(maxLoss) => patch("risk", { maxLoss })} aria-label="Default max loss" />
        </Field>
        <Field label="Default Max Orders" hint="0 = disabled">
          <NumInput value={risk.maxOrders} onChange={(maxOrders) => patch("risk", { maxOrders })} aria-label="Default max orders" />
        </Field>
        <Field label="Default Max Positions" hint="0 = disabled">
          <NumInput value={risk.maxPositions} onChange={(maxPositions) => patch("risk", { maxPositions })} aria-label="Default max positions" />
        </Field>
      </div>
      <MaxPosBehaviorControl draft={draft} patch={patch} />
    </Card>
  );
}

// ── Hedging ──────────────────────────────────────────────────────────────────
function HedgingCard({ draft, patch }: Draft) {
  const hedge = draft.hedge;
  return (
    <Card title="Hedging" hint="Automatic hedge behaviour" span={3}>
      <div className="sf-toggles">
        <Toggle checked={hedge.enabled} onChange={(enabled) => patch("hedge", { enabled })} label="Enable Auto Hedge" />
        <Toggle checked={hedge.retryFailed} onChange={(retryFailed) => patch("hedge", { retryFailed })} label="Retry Failed Hedge" />
      </div>
      <div className="sf-grid two">
        <Field label="Hedge Distance (Points)">
          <NumInput value={hedge.distancePts} onChange={(distancePts) => patch("hedge", { distancePts })} aria-label="Hedge distance" />
        </Field>
        <Field label="Max Retry Attempts">
          <NumInput value={hedge.maxRetries} onChange={(maxRetries) => patch("hedge", { maxRetries })} aria-label="Max retry attempts" />
        </Field>
      </div>
    </Card>
  );
}

// ── Notifications ────────────────────────────────────────────────────────────
const NOTIFY_ROWS: { key: keyof ProfileConfig["notify"]; label: string }[] = [
  { key: "executed", label: "Order Executed" },
  { key: "modified", label: "Order Modification" },
  { key: "tradeAlert", label: "Trade Alert (SL/Target Hit)" },
  { key: "system", label: "System / Connection Alerts" },
];

/** Notification preferences.
 *
 *  Deliberately labelled as not yet active. Nothing reads `notify` — there is no
 *  notification system behind it — and a settings card that looks enforced and
 *  is not is worse than one that is absent, because the user stops watching for
 *  the event themselves. Every alert these describe is written to the Activity
 *  log and to logs/orders.log today. */
function NotificationsCard({ draft, patch }: Draft) {
  const notify = draft.notify;
  return (
    <Card title="Notifications" hint="Not yet active — see note below" span={4}>
      <div className="sf-toggles">
        {NOTIFY_ROWS.map((r) => (
          <Toggle
            key={r.key}
            checked={notify[r.key]}
            onChange={(v) => patch("notify", { [r.key]: v })}
            label={r.label}
          />
        ))}
      </div>
      <p className="sf-note">
        These preferences are saved but <b>not yet acted on</b> — Charticks does
        not raise desktop or sound notifications in this version. Order fills,
        modifications, SL / Target hits and connection changes all appear in the
        Activity log and in <code>logs/orders.log</code>.
      </p>
    </Card>
  );
}

// ── Diagnostics: the log files, one click away ───────────────────────────────
// Not part of the trading profile, so it takes no draft slice and saves
// nothing. It lives on this page because it is the page people already open
// when they are trying to work out what the app just did.
function DiagnosticsCard() {
  const [dir, setDir] = useState("");
  const [status, setStatus] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    diagnostics.logFolder().then(setDir);
  }, []);

  const open = async () => {
    const res = await diagnostics.openLogFolder();
    setStatus(res.ok ? null : { kind: "err", text: res.error ?? "Could not open the folder." });
  };

  const bundle = async () => {
    setStatus({ kind: "ok", text: "Creating…" });
    const res = await diagnostics.saveDiagnosticsBundle();
    setStatus(
      res.ok
        ? { kind: "ok", text: `Saved to your Desktop: ${res.path?.split(/[\\/]/).pop()}` }
        : { kind: "err", text: res.error ?? "Could not create the bundle." },
    );
  };

  return (
    <Card title="Logs & Diagnostics" hint="What to send when something goes wrong">
      <p className="sf-note" style={{ marginTop: 0 }}>
        Charticks writes down everything it does — broker logins, every order it
        sent and what came back, feed interruptions and any unexpected error.
        When a connection or a live order fails, this folder says why.
      </p>
      {dir && <div className="sf-path" title={dir}>{dir}</div>}
      <div className="sf-diag-actions">
        <button className="btn-ghost" onClick={open} disabled={!diagnostics.available()}>
          Open Logs Folder
        </button>
        <button className="btn-ghost" onClick={bundle} disabled={!diagnostics.available()}>
          Save Diagnostics ZIP
        </button>
      </div>
      {status && (
        <p className={`sf-note ${status.kind === "err" ? "sf-note-err" : ""}`}>{status.text}</p>
      )}
      {!diagnostics.available() && (
        <p className="sf-note">Available in the desktop app.</p>
      )}
    </Card>
  );
}

export function Settings() {
  const activeProfile = useSettingsStore((s) => s.activeProfile);
  const profiles = useSettingsStore((s) => s.profiles);
  const replaceActive = useSettingsStore((s) => s.replaceActive);
  const saved = profiles[activeProfile];

  // Everything on this page is edited as a draft and committed together by
  // "Save Changes"; "Cancel" discards it. Switching profile re-seeds the draft
  // (an unsaved edit belongs to the profile it was made on).
  const [draft, setDraft] = useState<ProfileConfig>(saved);
  const [lastProfile, setLastProfile] = useState(activeProfile);
  const [justSaved, setJustSaved] = useState(false);
  if (lastProfile !== activeProfile) {
    setLastProfile(activeProfile);
    setDraft(saved);
    setJustSaved(false);
  }

  const patch = <K extends keyof Omit<ProfileConfig, "style">>(
    section: K,
    value: Partial<ProfileConfig[K]>,
  ) => {
    setDraft((d) => ({ ...d, [section]: { ...d[section], ...value } }));
    setJustSaved(false);
  };
  const setStyle = (style: TradingStyle) => {
    setDraft((d) => ({ ...d, style }));
    setJustSaved(false);
  };

  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const slice = { draft, patch };

  return (
    <section className="panel" style={{ gridColumn: "1 / 3" }}>
      <div className="phead">
        <h3>Settings</h3>
        <span className="tag">Trading Profile</span>
      </div>
      <div className="pbody sf-body">
        <ProfileHeader style={draft.style} onStyle={setStyle} />
        <div className="sf-cards">
          <SectionTitle>Trade Management</SectionTitle>
          <TradeDefaultsCard {...slice} />
          <InstrumentDefaultsCard {...slice} />

          <SectionTitle>Execution</SectionTitle>
          <ExecutionDefaultsCard {...slice} />
          <PortfolioTrailCard {...slice} />

          <SectionTitle>Risk Management</SectionTitle>
          <RiskDefaultsCard {...slice} />
          {showsHedging(draft.style) && <HedgingCard {...slice} />}
          <NotificationsCard {...slice} />

          <SectionTitle>Diagnostics</SectionTitle>
          <DiagnosticsCard />
        </div>
        <div className="sf-footer">
          {justSaved && !dirty && <span className="sf-saved">Saved ✓</span>}
          <button className="btn-ghost" disabled={!dirty} onClick={() => { setDraft(saved); setJustSaved(false); }}>
            Cancel
          </button>
          <button
            className="btn-primary"
            disabled={!dirty}
            onClick={() => { replaceActive(draft); setJustSaved(true); }}
          >
            ✓ Save Changes
          </button>
        </div>
      </div>
    </section>
  );
}
