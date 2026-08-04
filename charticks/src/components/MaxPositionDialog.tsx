import { useEffect } from "react";

/** "Ask Me" prompt for the Max Position overflow behaviour: the requested lots
 *  would push this position past the configured Max Position limit, so the user
 *  chooses between topping up to the limit, overriding just this once, or
 *  cancelling. Overriding never changes the configured setting. */
export function MaxPositionDialog({
  open,
  requestedLots,
  remainingLots,
  onAddRemaining,
  onOverride,
  onCancel,
}: {
  open: boolean;
  requestedLots: number;
  remainingLots: number;
  onAddRemaining: () => void;
  onOverride: () => void;
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
  const lot = (n: number) => `${n} ${n === 1 ? "Lot" : "Lots"}`;

  return (
    <div className="modal-backdrop" onMouseDown={onCancel}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Max Position Limit Exceeded"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h4>Max Position Limit Exceeded</h4>
        <p>Adding {lot(requestedLots)} exceeds your Max Position limit.</p>
        <div className="modal-actions stacked">
          {remainingLots > 0 && (
            <button className="btn-primary" onClick={onAddRemaining}>
              Add Remaining {lot(remainingLots)}
            </button>
          )}
          <button className="btn-ghost" onClick={onOverride}>
            Add All {lot(requestedLots)} (Override Once)
          </button>
          <button className="btn-ghost" onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
