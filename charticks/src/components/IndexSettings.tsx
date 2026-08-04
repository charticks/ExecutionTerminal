import { useState } from "react";
import { INDICES, MAX_VISIBLE_INDICES } from "@/lib/indices";
import { useIndicesStore } from "@/stores/useIndicesStore";

const LIMIT_MSG = `Maximum of ${MAX_VISIBLE_INDICES} indices can be displayed. Remove one to add another.`;

/** Body of the ⚙ floating popover on the Indices panel: a search box over a
 *  single scrollable checkbox list. Checking shows the index on Home, unchecking
 *  hides it — changes apply live (the store persists on each toggle). */
export function IndexSettings() {
  const visible = useIndicesStore((s) => s.visible);
  const toggle = useIndicesStore((s) => s.toggle);
  const [q, setQ] = useState("");
  const [limitHit, setLimitHit] = useState(false);

  const query = q.trim().toLowerCase();
  const shown = INDICES.filter((i) => i.name.toLowerCase().includes(query));

  const onToggle = (id: string) => {
    // toggle returns false only when an add was blocked by the 8-card limit.
    setLimitHit(!toggle(id));
  };

  return (
    <div className="idx-settings">
      <input
        className="idx-search"
        placeholder="Search indices…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        autoFocus
      />
      {limitHit && <div className="idx-limit">{LIMIT_MSG}</div>}

      <div className="idx-list">
        {shown.map((i) => {
          const checked = visible.includes(i.id);
          return (
            <label key={i.id} className={`idx-row ${checked ? "on" : ""}`}>
              <input
                type="checkbox"
                checked={checked}
                onChange={() => onToggle(i.id)}
              />
              <span className="nm">{i.name}</span>
            </label>
          );
        })}

        {shown.length === 0 && (
          <div className="pop-empty">No indices match “{q}”</div>
        )}
      </div>
    </div>
  );
}
