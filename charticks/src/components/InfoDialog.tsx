import { useEffect } from "react";

/** Single-button acknowledgement modal (e.g. "Market Closed"). Same shell as
 *  ConfirmDialog, minus the choice — the user can only acknowledge. */
export function InfoDialog({
  open,
  title,
  message,
  okLabel = "OK",
  onClose,
}: {
  open: boolean;
  title: string;
  message: string;
  okLabel?: string;
  onClose: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" || e.key === "Enter") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h4>{title}</h4>
        <p>{message}</p>
        <div className="modal-actions">
          <button className="btn-primary" onClick={onClose} autoFocus>
            {okLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
