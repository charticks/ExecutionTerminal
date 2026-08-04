import { useEffect, useMemo, useState } from "react";
import { Popover, usePopover } from "@/components/Popover";
import { BrokerFormDialog, RenameDialog, DeleteDialog } from "@/components/BrokerDialogs";
import { useBrokerStore } from "@/stores/useBrokerStore";
import { displayName, healthClass, type BrokerAccount } from "@/bridge/brokers";

type Dialog =
  | { kind: "add" }
  | { kind: "edit"; account: BrokerAccount }
  | { kind: "rename"; account: BrokerAccount }
  | { kind: "delete"; accounts: BrokerAccount[] }
  | null;

export function Brokers() {
  const { accounts, health, loadAccounts, connect, disconnect, deleteAccount } = useBrokerStore();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [dialog, setDialog] = useState<Dialog>(null);
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
            return (
              <label className={`broker-row ${selected.has(a.id) ? "sel" : ""}`} key={a.id}>
                <input
                  type="checkbox"
                  checked={selected.has(a.id)}
                  onChange={() => toggle(a.id)}
                />
                <span className={`d ${healthClass(h)}`} title={detail ?? h ?? "disconnected"} />
                <span className="broker-row-name">{displayName(a)}</span>
                {detail && h !== "connected" && (
                  <span className="broker-row-detail" title={detail}>{detail}</span>
                )}
              </label>
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
            <Popover open={manage.open} className="manage-pop">
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
    </section>
  );
}
