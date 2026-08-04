import { useEffect, useState } from "react";

// Confirmation shown only when switching Paper → Live (spec). Live → Paper
// switches immediately without a dialog, so this component is Paper→Live only.
export function SwitchToLiveDialog({
  onConfirm,
  onCancel,
}: {
  /** savePaper=true keeps the current paper orders/positions/pnl; false wipes them. */
  onConfirm: (savePaper: boolean) => void;
  onCancel: () => void;
}) {
  const [savePaper, setSavePaper] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onCancel();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div className="modal-backdrop" onMouseDown={onCancel}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Switch to Live Trading"
           onMouseDown={(e) => e.stopPropagation()}>
        <h4>Switch to Live Trading</h4>
        <p>Your current Paper Trading session will end.</p>
        <label className="fld-check">
          <input type="checkbox" checked={savePaper}
                 onChange={(e) => setSavePaper(e.target.checked)} />
          <span>Save Paper Session</span>
        </label>
        <div className="modal-actions">
          <button className="btn-ghost" onClick={onCancel}>Cancel</button>
          <button className="btn-primary" onClick={() => onConfirm(savePaper)}>Switch</button>
        </div>
      </div>
    </div>
  );
}
