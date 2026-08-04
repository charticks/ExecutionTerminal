import { useEffect, useMemo } from "react";
import { INDEX_BY_ID } from "@/lib/indices";
import { useGridPrefsStore, BAND_RANGES } from "@/stores/useGridPrefsStore";
import { useLiveChain } from "@/stores/useLiveChain";

export type RollDirection = "up" | "down";

/** The minimal shape the Roll Decider needs — satisfied by both a paper
 *  OptionPosition and a parsed live broker position. */
export interface RollTarget {
  underlying: string; // index id, e.g. "NIFTY"
  expiry: string; // the contract's expiry — rolls stay on the same expiry
  strike: number;
  optType: "CE" | "PE";
  lots: number;
}

/** Compact Roll popup: lists only the strikes in the chosen direction within the
 *  active Strike Band, with Distance (+n/−n) and a live LTP. Selecting one rolls
 *  the position (close current, open new strike, same qty + risk rule).
 *
 *  Each row's LTP is the real premium of THAT contract — the position's own
 *  underlying, expiry and option type at the candidate strike. The dialog
 *  registers those exact contracts with the sidecar on open (POST
 *  /option-chain/watch) and reads the quotes back off the pushed option-chain
 *  snapshot, so prices tick in real time and are never derived from another
 *  contract or a model. A strike with no quote yet shows "—" rather than a
 *  fabricated number, and cannot be rolled into. */
export function RollDialog({
  position,
  direction,
  onRoll,
  onClose,
}: {
  position: RollTarget;
  direction: RollDirection;
  /** Executes the roll against the caller's data source. */
  onRoll: (newStrike: number, premium: number) => void;
  onClose: () => void;
}) {
  const strikeBand = useGridPrefsStore((s) => s.strikeBand);
  const watch = useLiveChain((s) => s.watch);
  const quotes = useLiveChain((s) => s.snapshot.watch);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const def = INDEX_BY_ID[position.underlying];
  const step = def?.step ?? 50;
  const [lo, hi] = BAND_RANGES[strikeBand];
  const sign = direction === "up" ? 1 : -1;

  // Candidate strikes, in the selected direction only, lo..hi steps away.
  const candidates = useMemo(() => {
    const out: { n: number; strike: number }[] = [];
    for (let n = lo; n <= hi; n++) {
      const strike = position.strike + sign * n * step;
      if (strike <= 0) continue;
      out.push({ n, strike });
    }
    return out;
  }, [lo, hi, sign, step, position.strike]);

  // Subscribe to exactly these contracts while the dialog is open; release on
  // close so the sidecar stops streaming tokens nothing is showing.
  const strikeKey = candidates.map((c) => c.strike).join(",");
  useEffect(() => {
    const strikes = strikeKey ? strikeKey.split(",").map(Number) : [];
    watch(position.underlying, position.expiry, position.optType, strikes);
    return () => watch(position.underlying, position.expiry, position.optType, []);
  }, [watch, position.underlying, position.expiry, position.optType, strikeKey]);

  const ltpFor = (strike: number): number | null =>
    quotes.find((q) => q.strike === strike)?.ltp ?? null;

  const roll = (newStrike: number, premium: number | null) => {
    if (premium == null || premium <= 0) return; // never roll on an unknown price
    onRoll(newStrike, premium);
    onClose();
  };

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div
        className="modal roll-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Roll position"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h4>Roll {direction === "up" ? "Up" : "Down"}</h4>
        <p className="roll-current">
          {position.underlying} {position.strike} {position.optType} · {position.lots}{" "}
          {position.lots === 1 ? "Lot" : "Lots"}
        </p>
        <table className="roll-grid">
          <thead>
            <tr>
              <th>Strike</th>
              <th className="c-num">Distance</th>
              <th className="c-num">LTP</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map(({ n, strike }) => {
              const ltp = ltpFor(strike);
              return (
                <tr
                  key={strike}
                  className={`roll-grow ${ltp == null ? "no-quote" : ""}`}
                  onClick={() => roll(strike, ltp)}
                >
                  <td className="rs-strike num">{strike}</td>
                  <td className="c-num num">{sign > 0 ? `+${n}` : `-${n}`}</td>
                  <td className="c-num num">{ltp != null ? ltp.toFixed(2) : "—"}</td>
                </tr>
              );
            })}
            {candidates.length === 0 && (
              <tr>
                <td colSpan={3} className="empty">No strikes in range</td>
              </tr>
            )}
          </tbody>
        </table>
        <div className="modal-actions">
          <button className="btn-ghost" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
