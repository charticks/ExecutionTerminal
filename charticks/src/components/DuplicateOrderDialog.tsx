import { useEffect } from "react";

/** Duplicate-order protection prompt.
 *
 *  The sidecar keeps a client order id for every live order it sends and holds a
 *  second identical one rather than guessing (see docs/IDEMPOTENCY.md). Whether a
 *  repeat is a stray click or a deliberate add is something only the user knows,
 *  so it is asked rather than decided — and the override travels with that one
 *  request, never as a setting.
 *
 *  Two cases, deliberately worded differently, because the risk is not the same:
 *
 *  - `DUPLICATE_ORDER` — Charticks KNOWS an identical order went in moments ago.
 *    Confirming means holding two.
 *  - `IDEMPOTENCY_UNRESOLVED` — an earlier attempt was sent but never
 *    acknowledged, so nobody knows whether it reached the broker. Only the
 *    broker's own order book can settle it, which is why that variant asks the
 *    user to look before confirming and does not offer a primary-styled action.
 */
export function DuplicateOrderDialog({
  open,
  code,
  message,
  symbol,
  side,
  qty,
  duplicateOf,
  placedSecondsAgo,
  onPlaceAnyway,
  onCancel,
}: {
  open: boolean;
  code: "DUPLICATE_ORDER" | "IDEMPOTENCY_UNRESOLVED";
  message: string;
  symbol?: string;
  side?: string;
  qty?: number;
  duplicateOf?: string;
  placedSecondsAgo?: number | null;
  onPlaceAnyway: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;
  const unresolved = code === "IDEMPOTENCY_UNRESOLVED";

  return (
    <div className="modal-backdrop" onMouseDown={onCancel}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={unresolved ? "Previous Attempt Unconfirmed" : "Identical Order Detected"}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h4>{unresolved ? "Previous Attempt Unconfirmed" : "Identical Order Detected"}</h4>
        <p>{message}</p>
        {symbol && (
          <p className="muted">
            {side} {qty} {symbol}
            {duplicateOf ? ` · existing order ${duplicateOf}` : ""}
            {typeof placedSecondsAgo === "number" ? ` · ${placedSecondsAgo}s ago` : ""}
          </p>
        )}
        <div className="modal-actions stacked">
          {/* Cancel is the default and is focused: the safe choice should be the
              one an absent-minded Enter press takes. */}
          <button className="btn-primary" onClick={onCancel} autoFocus>
            Cancel — don't send
          </button>
          <button className="btn-ghost" onClick={onPlaceAnyway}>
            {unresolved
              ? "I checked my order book — send it"
              : "Place a second identical order"}
          </button>
        </div>
      </div>
    </div>
  );
}
