import { useEffect, useMemo, useState } from "react";
import { FlashNumber } from "@/components/FlashNumber";
import { Icon } from "@/components/Icon";
import { money } from "@/lib/format";
import { useUiStore } from "@/stores/useUiStore";
import {
  useStrategyStore,
  type StrategyInstance,
  type StrategyParamField,
  type StrategySpec,
} from "@/stores/useStrategyStore";

// Finer-grained than the raw state/phase fields — the single label/badge
// every row (tree, detail header, summary bar) agrees on.
type DerivedStatus = "running" | "waiting" | "completed" | "stopped" | "error";

function derivedStatus(inst: StrategyInstance): DerivedStatus {
  if (inst.state === "error") return "error";
  if (inst.state !== "running") return "stopped"; // "new" (never started) reads the same as "stopped"
  if (inst.phase === "in_position") return "running";
  if (inst.phase === "completed") return "completed";
  return "waiting"; // phase "waiting", or null (unknown/not yet refreshed)
}

const STATUS_LABEL: Record<DerivedStatus, string> = {
  running: "Running", waiting: "Waiting", completed: "Completed",
  stopped: "Stopped", error: "Error",
};
// Reuses the Positions grid's own .mon-badge palette (ok/pending/done/info/alarm)
// instead of inventing a second badge system for the same five-way status shape.
const STATUS_BADGE_CLASS: Record<DerivedStatus, string> = {
  running: "ok", waiting: "pending", completed: "done", stopped: "info", error: "alarm",
};

function displayName(inst: StrategyInstance, spec: StrategySpec | undefined): string {
  // A discovered instance's id IS the preset's filename stem (e.g.
  // "SENSEX-S1-BearDaySetup") — already the most specific name available,
  // and the whole reason discovery keys instances by filename rather than a
  // random id. A manually-created instance's id is an opaque hex string, so
  // its spec's label ("Quant Preset") is the more useful thing to show.
  return inst.source === "discovered" ? inst.id : (spec?.label ?? inst.strategy);
}

function groupKeyOf(inst: StrategyInstance): string {
  const index = inst.params?.index;
  return typeof index === "string" && index.trim() ? index.trim().toUpperCase() : "Other";
}

function timeOf(ts: number): string {
  return new Date(ts).toLocaleTimeString();
}

type Dialog = { kind: "create" } | { kind: "edit"; id: string } | null;

export function Strategies() {
  const {
    specs, instances, logs, load, rescan, create, update, duplicate, start, stop, remove,
  } = useStrategyStore();
  const openStrategyInstanceId = useUiStore((s) => s.openStrategyInstanceId);
  const setOpenStrategyInstanceId = useUiStore((s) => s.setOpenStrategyInstanceId);

  const [search, setSearch] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [groupsSeeded, setGroupsSeeded] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [rescanBusy, setRescanBusy] = useState(false);
  const [rescanNote, setRescanNote] = useState<string | null>(null);

  useEffect(() => {
    load();
  }, [load]);

  const specOf = (inst: StrategyInstance) => specs.find((s) => s.name === inst.strategy);

  const rows = useMemo(
    () => Object.values(instances).sort((a, b) => a.createdTs - b.createdTs),
    [instances],
  );

  // Every group starts COLLAPSED — the whole point of grouping is to avoid
  // showing dozens of strategies at once — seeded once as soon as the
  // roster's first load resolves (can't seed before groups are known).
  useEffect(() => {
    if (groupsSeeded || rows.length === 0) return;
    setCollapsed(new Set(rows.map(groupKeyOf)));
    setGroupsSeeded(true);
  }, [rows, groupsSeeded]);

  const filtered = useMemo(() => {
    // Every word in the query must appear SOMEWHERE in the haystack — not
    // the whole query as one substring — so "gap up" still finds
    // "BN-Expiry-A-GapUp-Momentum" even though the preset names run words
    // together with no space. Deliberately NOT searching the raw params
    // blob: a 73-key legacy preset schema has fields like "legacy_gap_vars"
    // that would match "gap" on every single preset regardless of what it
    // actually does, which is noise, not a useful search result.
    const words = search.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (words.length === 0) return rows;
    return rows.filter((inst) => {
      const spec = specOf(inst);
      const haystack = [displayName(inst, spec), inst.id, inst.strategy, groupKeyOf(inst)]
        .join(" ").toLowerCase();
      return words.every((w) => haystack.includes(w));
      // eslint-disable-next-line react-hooks/exhaustive-deps
    });
  }, [rows, search, specs]);

  const groups = useMemo(() => {
    const byGroup = new Map<string, StrategyInstance[]>();
    for (const inst of filtered) {
      const key = groupKeyOf(inst);
      (byGroup.get(key) ?? byGroup.set(key, []).get(key)!).push(inst);
    }
    return [...byGroup.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [filtered]);

  const summary = useMemo(() => {
    const counts: Record<DerivedStatus, number> = {
      running: 0, waiting: 0, completed: 0, stopped: 0, error: 0,
    };
    let pnl = 0;
    for (const inst of rows) {
      counts[derivedStatus(inst)] += 1;
      pnl += inst.pnl ?? 0;
    }
    return { counts, pnl };
  }, [rows]);

  // Cross-navigation from a Positions row's "Open Strategy" action: expand
  // that instance's group, select it, then clear the handoff flag — same
  // consume-and-clear contract useUiStore's editOrderId already uses.
  useEffect(() => {
    if (!openStrategyInstanceId) return;
    const inst = instances[openStrategyInstanceId];
    if (inst) {
      setCollapsed((prev) => {
        const next = new Set(prev);
        next.delete(groupKeyOf(inst));
        return next;
      });
      setSelectedId(openStrategyInstanceId);
    }
    setOpenStrategyInstanceId(null);
  }, [openStrategyInstanceId, instances, setOpenStrategyInstanceId]);

  const toggleGroup = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

  const onRescan = async () => {
    setRescanBusy(true);
    setRescanNote(null);
    const res = await rescan();
    setRescanBusy(false);
    setRescanNote(
      res.ok
        ? res.created
          ? `Found ${res.created} new strateg${res.created === 1 ? "y" : "ies"}.`
          : "No new strategies found."
        : (res.error ?? "Rescan failed."),
    );
  };

  const onDuplicate = async (id: string) => {
    const res = await duplicate(id);
    if (res.ok && res.id) setSelectedId(res.id);
  };

  const onDelete = async (id: string) => {
    const res = await remove(id);
    if (res.ok && selectedId === id) setSelectedId(null);
  };

  const selected = selectedId ? instances[selectedId] : undefined;

  return (
    <section className="panel" style={{ gridColumn: "1 / 3" }}>
      <div className="phead">
        <h3>Strategies</h3>
        <span className="grow" />
        <span className="strategy-summary-bar">
          <span>Running <b>{summary.counts.running}</b></span>
          <span>Waiting <b>{summary.counts.waiting}</b></span>
          <span>Completed <b>{summary.counts.completed}</b></span>
          <span>Stopped <b>{summary.counts.stopped}</b></span>
          {summary.counts.error > 0 && (
            <span className="strategy-summary-error">Error <b>{summary.counts.error}</b></span>
          )}
          <span className={`num ${summary.pnl >= 0 ? "up" : "down"}`}>
            <FlashNumber value={summary.pnl} format={money} />
          </span>
        </span>
      </div>

      <div className="strategy-split">
        <div className="strategy-pane-list">
          <div className="strategy-search">
            <Icon name="search" size={14} />
            <input
              type="text"
              placeholder="Search strategy..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>

          <div className="strategy-tree">
            {rows.length === 0 && (
              <div className="empty">No strategies configured yet. Add one to get started.</div>
            )}
            {rows.length > 0 && groups.length === 0 && (
              <div className="empty small">No strategies match "{search}".</div>
            )}
            {groups.map(([key, members]) => {
              const isCollapsed = collapsed.has(key) && !search.trim();
              return (
                <div className="strategy-group" key={key}>
                  <button
                    type="button"
                    className="strategy-group-header"
                    onClick={() => toggleGroup(key)}
                    aria-expanded={!isCollapsed}
                  >
                    <span className={`tree-caret ${isCollapsed ? "" : "open"}`}>▶</span>
                    <span>{key}</span>
                    <span className="strategy-group-count">({members.length})</span>
                  </button>
                  {!isCollapsed && members.map((inst) => {
                    const status = derivedStatus(inst);
                    const spec = specOf(inst);
                    return (
                      <button
                        type="button"
                        key={inst.id}
                        className={`strategy-tree-row ${selectedId === inst.id ? "active" : ""}`}
                        onClick={() => setSelectedId(inst.id)}
                      >
                        <span className="strategy-tree-row-name">
                          {displayName(inst, spec)}
                        </span>
                        <span className={`mon-badge ${STATUS_BADGE_CLASS[status]}`}>
                          {STATUS_LABEL[status]}
                        </span>
                        {status === "running" && (
                          <span className={`num strategy-tree-row-pnl ${(inst.pnl ?? 0) >= 0 ? "up" : "down"}`}>
                            <FlashNumber value={inst.pnl ?? 0} format={money} />
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              );
            })}
          </div>

          <div className="broker-toolbar">
            {rescanNote && <span className="strategy-rescan-note">{rescanNote}</span>}
            <span className="grow" />
            <button className="btn-ghost" disabled={rescanBusy} onClick={onRescan}>
              {rescanBusy ? "Scanning…" : "Rescan"}
            </button>
            <button className="btn-ghost add-broker" onClick={() => setDialog({ kind: "create" })}>
              + New Strategy
            </button>
          </div>
        </div>

        <div className="strategy-pane-detail">
          {!selected && (
            <div className="empty strategy-detail-empty">Select a strategy to see details.</div>
          )}
          {selected && (
            <StrategyDetail
              inst={selected}
              spec={specOf(selected)}
              logs={(logs[selected.id] ?? []).slice(-20)}
              onStart={() => start(selected.id)}
              onStop={() => stop(selected.id)}
              onEdit={() => setDialog({ kind: "edit", id: selected.id })}
              onDuplicate={() => onDuplicate(selected.id)}
              onDelete={() => onDelete(selected.id)}
            />
          )}
        </div>
      </div>

      {dialog?.kind === "create" && (
        <StrategyFormDialog
          mode="create"
          specs={specs}
          onSubmit={(specName, params, autoStart) => create(specName, params, autoStart)}
          onDone={() => setDialog(null)}
          onCancel={() => setDialog(null)}
        />
      )}
      {dialog?.kind === "edit" && instances[dialog.id] && (
        <StrategyFormDialog
          mode="edit"
          specs={specs}
          initial={instances[dialog.id]}
          onSubmit={(_specName, params) => update(dialog.id, params)}
          onDone={() => setDialog(null)}
          onCancel={() => setDialog(null)}
        />
      )}
    </section>
  );
}

function StrategyDetail({
  inst, spec, logs, onStart, onStop, onEdit, onDuplicate, onDelete,
}: {
  inst: StrategyInstance;
  spec: StrategySpec | undefined;
  logs: { ts: number; level: "info" | "warn" | "error"; message: string }[];
  onStart: () => void;
  onStop: () => void;
  onEdit: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
}) {
  const status = derivedStatus(inst);
  const running = inst.state === "running";
  return (
    <>
      <div className="strategy-detail-head">
        <span className={`d ${status === "running" ? "ok" : status === "error" ? "warn" : status === "waiting" ? "pending" : "off"}`} />
        <h4>{displayName(inst, spec)}</h4>
        {inst.source === "discovered" && <span className="tag">Discovered</span>}
        <span className={`mon-badge ${STATUS_BADGE_CLASS[status]}`}>{STATUS_LABEL[status]}</span>
        <span className="grow" />
        {running ? (
          <button className="btn-ghost" onClick={onStop}>Stop</button>
        ) : (
          <button className="btn-primary" onClick={onStart}>Start</button>
        )}
      </div>

      {inst.state === "error" && inst.error && (
        <p className="strategy-row-detail" role="status">
          <span className="broker-row-detail-label">Error</span>
          {inst.error}
        </p>
      )}

      <div className="strategy-detail-stats">
        <div>
          <span className="strategy-detail-stat-label">P&L</span>
          <span className={`num ${(inst.pnl ?? 0) >= 0 ? "up" : "down"}`}>
            {inst.pnl !== undefined ? <FlashNumber value={inst.pnl} format={money} /> : "—"}
          </span>
        </div>
        <div>
          <span className="strategy-detail-stat-label">Open trades</span>
          <span className="num">{inst.positionCount}</span>
        </div>
        <div>
          <span className="strategy-detail-stat-label">Strategy</span>
          <span>{spec?.label ?? inst.strategy}</span>
        </div>
        <div>
          <span className="strategy-detail-stat-label">Instance</span>
          <span className="strategy-detail-instance">{inst.id}</span>
        </div>
      </div>

      <div className="strategy-config">
        {Object.entries(inst.params).map(([k, v]) => (
          <div className="strategy-config-row" key={k}>
            <span className="strategy-config-key">{k}</span>
            <span className="strategy-config-val">{String(v)}</span>
          </div>
        ))}
        {Object.keys(inst.params).length === 0 && (
          <div className="empty small">No params.</div>
        )}
      </div>

      <div className="strategy-logs">
        {logs.length === 0 && <div className="empty small">No log lines yet.</div>}
        {logs.map((row, i) => (
          <div className={`strategy-log-row lvl-${row.level}`} key={i}>
            <span className="strategy-log-ts">{timeOf(row.ts)}</span>
            <span className="strategy-log-msg">{row.message}</span>
          </div>
        ))}
      </div>

      <div className="strategy-detail-actions">
        <button className="btn-ghost" disabled={running}
                title={running ? "Stop the strategy before editing it" : "Edit params"}
                onClick={onEdit}>
          Edit
        </button>
        <button className="btn-ghost" onClick={onDuplicate}>Duplicate</button>
        <span className="grow" />
        <button className="btn-danger" disabled={running}
                title={running ? "Stop the strategy before removing it" : "Remove this configured instance"}
                onClick={onDelete}>
          Delete
        </button>
      </div>
    </>
  );
}

function defaultParams(params: StrategyParamField[] | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const p of params ?? []) {
    if (p.default !== null && p.default !== undefined) out[p.key] = p.default;
    else if (p.kind === "bool") out[p.key] = false;
    else if (p.kind === "choice" && p.choices.length) out[p.key] = p.choices[0];
    else out[p.key] = "";
  }
  return out;
}

function StrategyFormDialog({
  mode, specs, initial, onSubmit, onDone, onCancel,
}: {
  mode: "create" | "edit";
  specs: StrategySpec[];
  initial?: StrategyInstance;
  onSubmit: (
    specName: string,
    params: Record<string, unknown>,
    autoStart: boolean,
  ) => Promise<{ ok: boolean; id?: string; code?: string; error?: string }>;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [specName, setSpecName] = useState(initial?.strategy ?? specs[0]?.name ?? "");
  const spec = specs.find((s) => s.name === specName);
  const [params, setParams] = useState<Record<string, unknown>>(
    () => (mode === "edit" && initial ? { ...initial.params } : defaultParams(spec?.params)),
  );
  const [autoStart, setAutoStart] = useState(initial?.autoStart ?? false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Reset the form to the new spec's own defaults whenever the selected
  // strategy changes — carrying one spec's field values into another's form
  // would silently submit stale data the user never saw for THIS strategy.
  // Only applies in "create" mode — "edit" locks the spec (see the disabled
  // select below), so this never fires there.
  useEffect(() => {
    if (mode === "create") setParams(defaultParams(spec?.params));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [specName]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onCancel();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const setField = (key: string, value: unknown) =>
    setParams((prev) => ({ ...prev, [key]: value }));

  const missingRequired = (spec?.params ?? []).some(
    (p) => p.required && (params[p.key] === "" || params[p.key] === undefined || params[p.key] === null),
  );

  const submit = async () => {
    setBusy(true);
    setError(null);
    const res = await onSubmit(specName, params, autoStart);
    setBusy(false);
    if (res.ok) onDone();
    else setError(res.error ?? `Could not ${mode === "create" ? "create" : "update"} the strategy.`);
  };

  return (
    <div className="modal-backdrop" onMouseDown={onCancel}>
      <div
        className="modal broker-modal"
        role="dialog"
        aria-modal="true"
        aria-label={mode === "create" ? "New Strategy" : "Edit Strategy"}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h4>{mode === "create" ? "New Strategy" : `Edit — ${initial?.id ?? ""}`}</h4>

        <label className="fld">
          <span>Strategy</span>
          <select value={specName} disabled={mode === "edit"}
                  onChange={(e) => setSpecName(e.target.value)}>
            {specs.map((s) => (
              <option key={s.name} value={s.name}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        {spec?.description && <p className="strategy-spec-desc">{spec.description}</p>}

        {(spec?.params ?? []).map((p) => (
          <label className="fld" key={p.key}>
            <span>
              {p.label}
              {p.required ? " *" : ""}
            </span>
            {p.kind === "bool" ? (
              <input
                type="checkbox"
                checked={Boolean(params[p.key])}
                onChange={(e) => setField(p.key, e.target.checked)}
              />
            ) : p.kind === "choice" ? (
              <select
                value={String(params[p.key] ?? "")}
                onChange={(e) => setField(p.key, e.target.value)}
              >
                {p.choices.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type={p.kind === "number" ? "number" : "text"}
                value={params[p.key] === undefined ? "" : String(params[p.key])}
                onChange={(e) =>
                  setField(p.key, p.kind === "number" ? Number(e.target.value) : e.target.value)
                }
              />
            )}
          </label>
        ))}

        {mode === "create" && (
          <label className="strategy-autostart">
            <input type="checkbox" checked={autoStart} onChange={(e) => setAutoStart(e.target.checked)} />
            <span>Start automatically when Charticks restarts</span>
          </label>
        )}

        {error && <p className="broker-row-detail">{error}</p>}

        <div className="modal-actions">
          <button className="btn-ghost" onClick={onCancel}>
            Cancel
          </button>
          <button className="btn-primary" disabled={!specName || missingRequired || busy} onClick={submit}>
            {busy ? "Saving…" : mode === "create" ? "Create" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
