import { FlashNumber } from "@/components/FlashNumber";
import { Sparkline } from "@/components/Sparkline";
import { Icon } from "@/components/Icon";
import { Popover, usePopover } from "@/components/Popover";
import { IndexSettings } from "@/components/IndexSettings";
import { useMarketStore } from "@/stores/useMarketStore";
import { useIndicesStore } from "@/stores/useIndicesStore";
import { INDEX_BY_ID } from "@/lib/indices";
import { price, pct } from "@/lib/format";

export function IndicesPanel() {
  const visible = useIndicesStore((s) => s.visible);
  const indices = useMarketStore((s) => s.indices);
  const { open, toggle, wrapRef } = usePopover();

  return (
    <section className="panel idx-panel">
      <div className="pop-wrap idx-gear" ref={wrapRef}>
        <button className="ghost-btn" title="Customize indices" aria-label="Customize indices" onClick={toggle}>
          <Icon name="gear" size={15} />
        </button>
        <Popover open={open} className="idx-pop">
          <IndexSettings />
        </Popover>
      </div>
      <div className="tiles in-panel">
        {visible.map((id) => {
          const def = INDEX_BY_ID[id];
          if (!def) return null;
          // Live quote from the broker feed (keyed by index symbol). Absent until
          // the broker is connected and streaming this index.
          const q = indices[id];
          if (!q) {
            return (
              <div className="tile" key={id}>
                <div className="top">
                  <span className="name">{def.name}</span>
                  <span className="chgline num muted">—</span>
                </div>
                <div className="val muted">Waiting for feed…</div>
              </div>
            );
          }
          const up = q.changePct >= 0;
          const prev = q.changePct !== 0 ? q.ltp / (1 + q.changePct / 100) : q.ltp;
          const pts = q.ltp - prev;
          return (
            <div className="tile" key={id}>
              <div className="top">
                <span className="name">{def.name}</span>
                <span className={`chgline num ${up ? "up" : "down"}`}>
                  {(up ? "+" : "") + pts.toFixed(2)} ({pct(q.changePct)})
                </span>
              </div>
              <FlashNumber value={q.ltp} format={price} className="val" />
              <Sparkline data={q.history} up={up} />
            </div>
          );
        })}
        {visible.length === 0 && <div className="empty">No indices selected — add some via ⚙</div>}
      </div>
    </section>
  );
}
