import { useEffect } from "react";

/** Shown when a large order was split across several broker orders and one of
 *  the children failed: the earlier children are already live, the rest were
 *  never sent. The user sees one trade — executed vs remaining quantity — and
 *  can retry just the remainder. */
export function PartialFillDialog({
  open,
  executedQty,
  remainingQty,
  onRetry,
  onDismiss,
}: {
  open: boolean;
  executedQty: number;
  remainingQty: number;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onDismiss();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onDismiss]);

  if (!open) return null;
  const total = executedQty + remainingQty;
  return (
    <div className="modal-backdrop" onMouseDown={onDismiss}>
      <div
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-label="Order Partially Executed"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h4>Order Partially Executed</h4>
        <p>
          Only part of the requested quantity was executed. Executed{" "}
          <b>{executedQty}</b> of <b>{total}</b>; <b>{remainingQty}</b> remaining.
        </p>
        <div className="modal-actions">
          <button className="btn-ghost" onClick={onDismiss}>Dismiss</button>
          <button className="btn-primary" onClick={onRetry} autoFocus>
            Retry Remaining
          </button>
        </div>
      </div>
    </div>
  );
}
