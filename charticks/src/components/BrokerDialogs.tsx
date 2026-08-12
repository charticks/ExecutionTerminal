import { useEffect, useState } from "react";
import { credentials as credStore, iciciLogin } from "@/bridge/credentials";
import {
  BROKER_LABEL,
  BROKER_ORDER,
  brokerLabel,
  credFieldsFor,
  displayName,
  type BrokerAccount,
  type Credentials,
} from "@/bridge/brokers";
import { useCustomBrokersStore } from "@/stores/useCustomBrokersStore";

// Sentinel dropdown value that opens the "Add New Broker" mini-dialog.
const ADD_NEW = "__add_new_broker__";

function useEscape(onClose: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
}

/** Text input for a sensitive credential with an independent show/hide (👁)
 *  toggle. Masked by default; revealing one field never affects others. */
function SecretInput({
  value,
  placeholder,
  onChange,
}: {
  value: string;
  placeholder?: string;
  onChange: (v: string) => void;
}) {
  const [revealed, setRevealed] = useState(false);
  return (
    <span className="secret-input">
      <input
        type={revealed ? "text" : "password"}
        value={value}
        placeholder={placeholder}
        autoComplete="off"
        onChange={(e) => onChange(e.target.value)}
      />
      <button
        type="button"
        className="reveal-btn"
        aria-label={revealed ? "Hide value" : "Show value"}
        aria-pressed={revealed}
        title={revealed ? "Hide" : "Show"}
        onClick={() => setRevealed((r) => !r)}
      >
        {revealed ? "🙈" : "👁"}
      </button>
    </span>
  );
}

/** Lightweight modal to name a new custom broker. */
function AddBrokerNameDialog({
  onAdd,
  onCancel,
}: {
  onAdd: (label: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const addCustom = useCustomBrokersStore((s) => s.add);
  useEscape(onCancel);

  const submit = () => {
    const reserved = BROKER_ORDER.map((b) => BROKER_LABEL[b]);
    const res = addCustom(name, reserved);
    if ("error" in res) {
      setError(res.error);
      return;
    }
    onAdd(res.key);
  };

  return (
    <div className="modal-backdrop" onMouseDown={onCancel}>
      <div className="modal add-broker-modal" role="dialog" aria-modal="true"
           aria-label="Add New Broker" onMouseDown={(e) => e.stopPropagation()}>
        <h4>Add New Broker</h4>
        <label className="fld">
          <span>Broker Name</span>
          <input
            value={name}
            autoFocus
            onChange={(e) => { setName(e.target.value); setError(null); }}
            onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
          />
        </label>
        {error && <div className="modal-error">{error}</div>}
        <div className="modal-actions">
          <button className="btn-ghost" onClick={onCancel}>Cancel</button>
          <button className="btn-primary" onClick={submit}>Add</button>
        </div>
      </div>
    </div>
  );
}

/** Add a new broker, or edit an existing one's credentials. When `account` is
 *  provided the form is in edit mode (broker locked, credentials pre-filled). */
export function BrokerFormDialog({
  account,
  onSaved,
  onCancel,
}: {
  account?: BrokerAccount;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const editing = !!account;
  const [broker, setBroker] = useState<string>(account?.broker ?? "angel");
  const [nickname, setNickname] = useState(account?.nickname ?? "");
  const [autoConnect, setAutoConnect] = useState(account?.autoConnect ?? true);
  const [creds, setCreds] = useState<Credentials>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [addingBroker, setAddingBroker] = useState(false);
  // ICICI-only: the daily session key is captured by a login popup, never typed.
  const [iciciBusy, setIciciBusy] = useState(false);
  const [iciciNote, setIciciNote] = useState<string | null>(null);
  const customBrokers = useCustomBrokersStore((s) => s.brokers);
  useEscape(onCancel);

  // In edit mode, pre-fill existing credentials.
  useEffect(() => {
    if (!account) return;
    credStore.getSecrets(account.id).then((c) => c && setCreds(c));
  }, [account]);

  const fields = credFieldsFor(broker);

  const doIciciLogin = async () => {
    setIciciBusy(true);
    setIciciNote(null);
    setError(null);
    try {
      const res = await iciciLogin(creds.apiKey ?? "");
      if (res.ok && res.token) {
        setCreds((c) => ({ ...c, sessionToken: res.token as string }));
        setIciciNote("Session key captured — click Save.");
      } else {
        setError(res.error ?? "ICICI login failed.");
      }
    } finally {
      setIciciBusy(false);
    }
  };

  const save = async () => {
    const missing = fields.filter((f) => !creds[f.key]?.trim());
    if (missing.length) {
      setError(`Fill in: ${missing.map((f) => f.label).join(", ")}`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (editing && account) {
        await credStore.update(account.id, { nickname, autoConnect, credentials: creds });
      } else {
        await credStore.add({ broker, nickname, autoConnect, credentials: creds });
      }
      onSaved();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onMouseDown={onCancel}>
      <div className="modal broker-modal" role="dialog" aria-modal="true"
           aria-label={editing ? "Edit Credentials" : "Add Broker"}
           onMouseDown={(e) => e.stopPropagation()}>
        <h4>{editing ? "Edit Credentials" : "Add Broker"}</h4>

        <label className="fld">
          <span>Broker</span>
          <select
            value={broker}
            disabled={editing}
            onChange={(e) => {
              const v = e.target.value;
              if (v === ADD_NEW) {
                setAddingBroker(true);
                return; // don't change the selection; the mini-dialog will
              }
              setBroker(v);
              setCreds({});
            }}
          >
            {BROKER_ORDER.map((b) => (
              <option key={b} value={b}>{BROKER_LABEL[b]}</option>
            ))}
            {customBrokers.map((b) => (
              <option key={b.key} value={b.key}>{b.label}</option>
            ))}
            <option disabled>────────────────────</option>
            <option value={ADD_NEW}>+ Add New Broker…</option>
          </select>
        </label>

        <label className="fld">
          <span>Nickname</span>
          <input value={nickname} placeholder="Primary"
                 onChange={(e) => setNickname(e.target.value)} />
        </label>

        {fields.map((f) => (
          <label className="fld" key={f.key}>
            <span>{f.label}</span>
            {f.secret ? (
              <SecretInput
                value={creds[f.key] ?? ""}
                placeholder={f.placeholder}
                onChange={(v) => setCreds((c) => ({ ...c, [f.key]: v }))}
              />
            ) : (
              <input
                type="text"
                value={creds[f.key] ?? ""}
                placeholder={f.placeholder}
                autoComplete="off"
                onChange={(e) => setCreds((c) => ({ ...c, [f.key]: e.target.value }))}
              />
            )}
          </label>
        ))}

        {broker === "icici" && (
          <div className="fld">
            <span />
            <div>
              <button className="btn-ghost" onClick={doIciciLogin}
                      disabled={iciciBusy || !creds.apiKey?.trim()}>
                {iciciBusy ? "Waiting for ICICI login…" : "Log in with ICICI"}
              </button>
              <div className="hint">
                {iciciNote ??
                 "ICICI's session key expires daily — log in again each morning."}
              </div>
            </div>
          </div>
        )}

        <label className="fld-check">
          <input type="checkbox" checked={autoConnect}
                 onChange={(e) => setAutoConnect(e.target.checked)} />
          <span>Auto Connect on startup</span>
        </label>

        {error && <div className="modal-error">{error}</div>}

        <div className="modal-actions">
          <button className="btn-ghost" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn-primary" onClick={save} disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </div>

      {addingBroker && (
        <AddBrokerNameDialog
          onAdd={(key) => { setAddingBroker(false); setBroker(key); setCreds({}); }}
          onCancel={() => setAddingBroker(false)}
        />
      )}
    </div>
  );
}

/** Rename an account's nickname only (credentials unchanged). */
export function RenameDialog({
  account,
  onSaved,
  onCancel,
}: {
  account: BrokerAccount;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(account.nickname);
  const [busy, setBusy] = useState(false);
  useEscape(onCancel);

  const save = async () => {
    setBusy(true);
    await credStore.rename(account.id, name);
    setBusy(false);
    onSaved();
  };

  return (
    <div className="modal-backdrop" onMouseDown={onCancel}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Rename Broker"
           onMouseDown={(e) => e.stopPropagation()}>
        <h4>Rename Broker</h4>
        <label className="fld">
          <span>Current name</span>
          <input value={account.nickname || brokerLabel(account.broker)} disabled />
        </label>
        <label className="fld">
          <span>New name</span>
          <input value={name} autoFocus onChange={(e) => setName(e.target.value)} />
        </label>
        <div className="modal-actions">
          <button className="btn-ghost" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="btn-primary" onClick={save} disabled={busy}>Save</button>
        </div>
      </div>
    </div>
  );
}

// ── Multi-broker execution confirmation ────────────────────────────────────
// Shown once, when the user goes from one execution broker to more than one.
// Muted permanently by "Don't show again" — a preference, not a secret, so
// localStorage alongside the other UI prefs is the right home for it.
const MULTI_EXEC_ACK_KEY = "ck.multiExecAck";

export function multiExecuteAcknowledged(): boolean {
  try {
    return localStorage.getItem(MULTI_EXEC_ACK_KEY) === "1";
  } catch {
    return false; // private mode / storage disabled — safer to keep asking
  }
}

function acknowledgeMultiExecute() {
  try {
    localStorage.setItem(MULTI_EXEC_ACK_KEY, "1");
  } catch {
    /* preference simply won't stick */
  }
}

/** Confirm enabling live execution on a second (or further) broker — every
 *  future live order will then be placed on all of them. */
export function MultiExecuteDialog({
  account,
  onConfirm,
  onCancel,
}: {
  account: BrokerAccount;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [dontAsk, setDontAsk] = useState(false);
  useEscape(onCancel);

  const confirm = () => {
    if (dontAsk) acknowledgeMultiExecute();
    onConfirm();
  };

  return (
    <div className="modal-backdrop" onMouseDown={onCancel}>
      <div className="modal" role="alertdialog" aria-modal="true"
           aria-label="Enable execution on multiple brokers"
           onMouseDown={(e) => e.stopPropagation()}>
        <h4>Enable Execution on Multiple Brokers?</h4>
        <p>
          You are enabling live execution on <b>{displayName(account)}</b> as well.
        </p>
        <p className="modal-note">
          Future live orders will be placed on <b>all</b> selected brokers — the
          full quantity on each, not split between them.
        </p>
        <label className="fld-check">
          <input type="checkbox" checked={dontAsk}
                 onChange={(e) => setDontAsk(e.target.checked)} />
          <span>Don't show again</span>
        </label>
        <div className="modal-actions">
          <button className="btn-ghost" onClick={onCancel}>Cancel</button>
          <button className="btn-primary" onClick={confirm} autoFocus>Continue</button>
        </div>
      </div>
    </div>
  );
}

/** Confirm deletion of one or more accounts. */
export function DeleteDialog({
  accounts,
  onConfirm,
  onCancel,
}: {
  accounts: BrokerAccount[];
  onConfirm: () => void;
  onCancel: () => void;
}) {
  useEscape(onCancel);
  const multi = accounts.length > 1;
  return (
    <div className="modal-backdrop" onMouseDown={onCancel}>
      <div className="modal" role="alertdialog" aria-modal="true" aria-label="Delete Broker"
           onMouseDown={(e) => e.stopPropagation()}>
        <h4>{multi ? `Delete ${accounts.length} Brokers` : "Delete Broker"}</h4>
        <p>
          You are about to remove:
          <br />
          <b>{accounts.map(displayName).join(", ")}</b>
        </p>
        <p className="modal-note">
          This will delete the saved credentials, remove {multi ? "them" : "it"} from Charticks,
          and disconnect {multi ? "any" : "it"} if currently connected. This action cannot be undone.
        </p>
        <div className="modal-actions">
          <button className="btn-ghost" onClick={onCancel}>Cancel</button>
          <button className="btn-danger" onClick={onConfirm} autoFocus>Delete</button>
        </div>
      </div>
    </div>
  );
}
