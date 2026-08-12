import { useEffect, useMemo, useState } from "react";
import { Popover, usePopover } from "@/components/Popover";
import {
  BrokerFormDialog,
  RenameDialog,
  DeleteDialog,
  MultiExecuteDialog,
  multiExecuteAcknowledged,
} from "@/components/BrokerDialogs";
import { useBrokerStore } from "@/stores/useBrokerStore";
import { displayName, healthClass, type BrokerAccount } from "@/bridge/brokers";

const EXECUTE_TOOLTIP =
  "Enable this broker for live order execution. Live orders will be sent only " +
  "to brokers with Execute enabled.";

/** Tooltip for the Execute control. Always states what Execute means, and adds
 *  why the control is unavailable when the broker is offline — a disabled
 *  checkbox with no explanation is the usual reason people think it's broken. */
function executeTooltip(a: BrokerAccount, isConnected: boolean): string {
  if (isConnected) return EXECUTE_TOOLTIP;
  return a.execute
    ? `${EXECUTE_TOOLTIP}\n\nThis broker is enabled but currently disconnected, so it will not receive orders until it reconnects.`
    : `${EXECUTE_TOOLTIP}\n\nConnect this broker first.`;
}

type Dialog =
  | { kind: "add" }
  | { kind: "edit"; account: BrokerAccount }
  | { kind: "rename"; account: BrokerAccount }
  | { kind: "delete"; accounts: BrokerAccount[] }
  | null;

export function Brokers() {
  const { accounts, health, loadAccounts, connect, disconnect, deleteAccount, setExecute } =
    useBrokerStore();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [dialog, setDialog] = useState<Dialog>(null);
  // Account awaiting the "you're enabling a second execution broker" confirm.
  const [confirmMultiExec, setConfirmMultiExec] = useState<BrokerAccount | null>(null);
  const manage = usePopover();

  useEffect(() => {
    loadAccounts();
  }, [loadAccounts]);

  // Drop selections for accounts that no longer exist.
  useEffect(() => {
    setSelected((prev) => {
      const ids = new Set(accounts.map((a) => a.id));
      const next = new Set([...prev].filter((id) => ids.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [accounts]);

  const selectedAccounts = useMemo(
    () => accounts.filter((a) => selected.has(a.id)),
    [accounts, selected],
  );
  const singleSelected = selectedAccounts.length === 1 ? selectedAccounts[0] : null;
  const canManage = selectedAccounts.length > 0;

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  // Turning Execute ON while exactly one broker already executes takes the user
  // from single- to multi-broker execution — the one transition where the blast
  // radius of every future order changes. Confirm that once (unless muted).
  // Going 2 → 3 is already multi-broker, so it does not re-prompt; turning
  // Execute off never prompts.
  const onExecuteChange = (account: BrokerAccount, next: boolean) => {
    const enabledCount = accounts.filter((a) => a.execute).length;
    if (next && enabledCount === 1 && !multiExecuteAcknowledged()) {
      setConfirmMultiExec(account);
      return;
    }
    setExecute(account.id, next);
  };

  const closeDialog = () => setDialog(null);
  const afterSave = () => {
    closeDialog();
    loadAccounts();
  };

  const doDelete = async () => {
    if (dialog?.kind !== "delete") return;
    for (const a of dialog.accounts) await deleteAccount(a.id);
    setSelected(new Set());
    closeDialog();
  };

  return (
    <section className="panel" style={{ gridColumn: "1 / 3" }}>
      <div className="phead">
        <h3>Brokers</h3>
        <span className="grow" />
        <span className="tag">{accounts.length} configured</span>
      </div>

      <div className="pbody">
        <div className="broker-list">
          {accounts.length === 0 && (
            <div className="empty">No brokers configured yet. Add one to get started.</div>
          )}
          {accounts.map((a) => {
            const h = health[a.id]?.health;
            const detail = health[a.id]?.detail;
            const isConnected = h === "connected";
            return (
              // A div, not a label: the row holds two independent checkboxes
              // (select + Execute) and nesting them under one label would make
              // clicking Execute also toggle the selection.
              <div className={`broker-row ${selected.has(a.id) ? "sel" : ""}`} key={a.id}>
                <label className="broker-row-select">
                  <input
                    type="checkbox"
                    checked={selected.has(a.id)}
                    onChange={() => toggle(a.id)}
                    aria-label={`Select ${displayName(a)}`}
                  />
                  <span className={`d ${healthClass(h)}`} title={detail ?? h ?? "disconnected"} />
                  <span className="broker-row-name">{displayName(a)}</span>
                </label>
                <label
                  className={`broker-row-exec ${a.execute ? "on" : ""} ${isConnected ? "" : "off"}`}
                  title={executeTooltip(a, isConnected)}
                >
                  <input
                    type="checkbox"
                    checked={a.execute}
                    disabled={!isConnected}
                    onChange={(e) => onExecuteChange(a, e.target.checked)}
                    aria-label={`Execute live orders on ${displayName(a)}`}
                  />
                  <span>Execute</span>
                </label>
                {detail && !isConnected && (
                  <span className="broker-row-detail" title={detail}>{detail}</span>
                )}
              </div>
            );
          })}
        </div>

        <div className="broker-toolbar">
          <button
            className="btn-primary"
            disabled={selectedAccounts.length === 0}
            onClick={() => connect([...selected])}
          >
            Connect
          </button>
          <button
            className="btn-ghost"
            disabled={selectedAccounts.length === 0}
            onClick={() => disconnect([...selected])}
          >
            Disconnect
          </button>

          <div className="pop-wrap" ref={manage.wrapRef}>
            <button className="btn-ghost" disabled={!canManage} onClick={manage.toggle}
                    aria-expanded={manage.open}>
              Manage ▾
            </button>
            {/* Floating: the Brokers panel clips overflow, so an anchored
                panel loses its top items when the broker list is short. */}
            <Popover open={manage.open} className="manage-pop"
                     anchorRef={manage.wrapRef} panelRef={manage.panelRef}>
              <button
                className="pop-action"
                disabled={!singleSelected}
                onClick={() => { manage.setOpen(false); if (singleSelected) setDialog({ kind: "edit", account: singleSelected }); }}
              >
                Edit Credentials
              </button>
              <button
                className="pop-action"
                disabled={!singleSelected}
                onClick={() => { manage.setOpen(false); if (singleSelected) setDialog({ kind: "rename", account: singleSelected }); }}
              >
                Rename Broker
              </button>
              <div className="pop-divider" />
              <button
                className="pop-action danger"
                onClick={() => { manage.setOpen(false); setDialog({ kind: "delete", accounts: selectedAccounts }); }}
              >
                {selectedAccounts.length > 1 ? "Delete Selected Brokers" : "Delete Broker"}
              </button>
            </Popover>
          </div>

          <span className="grow" />
          <button className="btn-ghost add-broker" onClick={() => setDialog({ kind: "add" })}>
            + Add Broker
          </button>
        </div>
      </div>

      {dialog?.kind === "add" && <BrokerFormDialog onSaved={afterSave} onCancel={closeDialog} />}
      {dialog?.kind === "edit" && (
        <BrokerFormDialog account={dialog.account} onSaved={afterSave} onCancel={closeDialog} />
      )}
      {dialog?.kind === "rename" && (
        <RenameDialog account={dialog.account} onSaved={afterSave} onCancel={closeDialog} />
      )}
      {dialog?.kind === "delete" && (
        <DeleteDialog accounts={dialog.accounts} onConfirm={doDelete} onCancel={closeDialog} />
      )}
      {confirmMultiExec && (
        <MultiExecuteDialog
          account={confirmMultiExec}
          onConfirm={() => {
            setExecute(confirmMultiExec.id, true);
            setConfirmMultiExec(null);
          }}
          onCancel={() => setConfirmMultiExec(null)}
        />
      )}
    </section>
  );
}
