import { useEffect, useState } from "react";
import {
  useSessionLimits,
  statusReason,
  isHardLocked,
  type LimitKey,
} from "@/stores/useSessionLimits";
import { usePositionsStore } from "@/stores/usePositionsStore";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { money } from "@/lib/format";
import { marketGateSilent } from "@/lib/marketSession";

/** One label + value pair. Editing commits ONLY via ✓ / Enter; blur or Esc
 *  restores the original value (never saves on focus loss or while typing). */
function LimitField({
  label,
  value,
  onSave,
  currency = false,
  disabled,
}: {
  label: string;
  value: number;
  onSave: (n: number) => void;
  currency?: boolean;
  disabled: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(String(value));

  const commit = () => {
    const n = parseInt(draft.replace(/\D/g, ""), 10);
    if (!Number.isNaN(n)) onSave(n);
    setEditing(false);
  };
  const cancel = () => setEditing(false);

  return (
    <div className="lim">
      <span className="lim-lab">{label}</span>
      {editing ? (
        <span className="lim-edit-wrap">
          <input
            className="lim-input num"
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={cancel}
            onKeyDown={(e) => {
              if (e.key === "Enter") commit();
              if (e.key === "Escape") cancel();
            }}
          />
          {/* mousedown (not click) so the input's blur-cancel doesn't fire first */}
          <button className="lim-commit" title="Save" aria-label={`Save ${label}`} onMouseDown={(e) => { e.preventDefault(); commit(); }}>
            ✓
          </button>
        </span>
      ) : (
        <>
          <span className="lim-val num">
            {currency ? "₹" + value.toLocaleString("en-IN") : value}
          </span>
          <button
            className="lim-edit"
            disabled={disabled}
            title={`Edit ${label}`}
            aria-label={`Edit ${label}`}
            onClick={() => {
              setDraft(String(value));
              setEditing(true);
            }}
          >
            ✎
          </button>
        </>
      )}
    </div>
  );
}

interface Pending {
  key: LimitKey;
  label: string;
  oldVal: number;
  newVal: number;
  currency: boolean;
}

export function SessionLimitsBar({ rollingPnl }: { rollingPnl: number }) {
  const { enabled, maxPos, maxTrades, maxLoss, profitTarget, status, setEnabled, setLimit, reset, evaluatePnl } =
    useSessionLimits();
  const squareOffAll = usePositionsStore((s) => s.squareOffAll);
  const openCount = usePositionsStore(
    (s) => s.positions.filter((p) => p.status === "OPEN").length,
  );
  const [pending, setPending] = useState<Pending | null>(null);
  // Underlyings with an open position: the auto square-off spans instruments, so
  // it must fire while ANY of their sessions is live (MCX runs past 15:30).
  const openUnderlyings = usePositionsStore(
    (s) => s.positions.filter((p) => p.status === "OPEN").map((p) => p.underlying).join(","),
  );

  // Continuously monitor Net P&L: on Max Loss / Profit Target the session locks
  // and every open position is squared off.
  // Automatic action — it must never pop the "Market Closed" dialog at the user;
  // outside market hours the session still locks and the sidecar rejects the
  // square-off anyway.
  useEffect(() => {
    const anyOpen = openUnderlyings
      ? openUnderlyings.split(",").some((u) => marketGateSilent(u))
      : marketGateSilent();
    if (evaluatePnl(rollingPnl) && anyOpen) squareOffAll();
  }, [rollingPnl, evaluatePnl, squareOffAll, openUnderlyings]);

  const fmt = (n: number, currency: boolean) => (currency ? "₹" + n.toLocaleString("en-IN") : String(n));

  const requestChange = (key: LimitKey, label: string, currency: boolean) => (newVal: number) => {
    const oldVal = { maxPos, maxTrades, maxLoss, profitTarget }[key];
    if (newVal === oldVal) return;
    // Increasing any of these raises allowable risk/exposure. Confirm only when
    // that happens during an active session (open positions on the book).
    const increasesRisk = newVal > oldVal;
    if (increasesRisk && openCount > 0) {
      setPending({ key, label, oldVal, newVal, currency });
      return;
    }
    setLimit(key, newVal);
  };

  const lockReason = isHardLocked(status) ? statusReason(status) : "";

  return (
    <div className={`slimits ${enabled ? "on" : ""} ${lockReason ? "locked" : ""}`}>
      <label className="slim-toggle">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        Session Limits
      </label>
      <LimitField label="Max Pos" value={maxPos} onSave={requestChange("maxPos", "Max Open Positions", false)} disabled={!enabled} />
      <LimitField label="Max Trades" value={maxTrades} onSave={requestChange("maxTrades", "Max Daily Trades", false)} disabled={!enabled} />
      <LimitField label="Max Loss" value={maxLoss} onSave={requestChange("maxLoss", "Max Loss", true)} currency disabled={!enabled} />
      <LimitField
        label="Profit Target"
        value={profitTarget}
        onSave={requestChange("profitTarget", "Profit Target", true)}
        currency
        disabled={!enabled}
      />
      {lockReason && (
        <span className="slim-lock">
          Locked — {lockReason}
          <button className="slim-reset" onClick={reset} title="Reset trading session">Reset</button>
        </span>
      )}
      <div className="lim rolling">
        <span className="lim-lab">Rolling P&amp;L</span>
        <span className={`lim-val num ${rollingPnl >= 0 ? "up" : "down"}`}>{money(rollingPnl)}</span>
      </div>

      <ConfirmDialog
        open={pending != null}
        title="Confirm Change"
        message={
          pending
            ? `${pending.label} will change from ${fmt(pending.oldVal, pending.currency)} to ${fmt(pending.newVal, pending.currency)}. Continue?`
            : ""
        }
        confirmLabel="Continue"
        onConfirm={() => {
          if (pending) setLimit(pending.key, pending.newVal);
          setPending(null);
        }}
        onCancel={() => setPending(null)}
      />
    </div>
  );
}
