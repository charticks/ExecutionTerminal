// Shared broker-account model + per-broker credential field specs.
// Used by the Brokers UI, the credential store bridge, and the connect flow.
import type { BrokerId, BrokerHealth } from "./events";
import { useCustomBrokersStore } from "@/stores/useCustomBrokersStore";

export type { BrokerId, BrokerHealth };

export const BROKER_LABEL: Record<BrokerId, string> = {
  angel: "Angel One",
  kotak: "Kotak Neo",
  dhan: "Dhan HQ",
  icici: "ICICI Direct",
  firstock: "Firstock",
};

export const BROKER_ORDER: BrokerId[] = ["angel", "kotak", "dhan", "icici", "firstock"];

/** A credential field to render in the Add/Edit dialog. `secret` fields are
 *  masked in the UI; all credential values are encrypted at rest. */
export interface CredField {
  key: string;
  label: string;
  secret?: boolean;
  placeholder?: string;
}

export const CRED_FIELDS: Record<BrokerId, CredField[]> = {
  angel: [
    { key: "apiKey", label: "API Key", secret: true },
    { key: "clientId", label: "Client ID" },
    { key: "pin", label: "PIN", secret: true },
    { key: "totpSecret", label: "TOTP Secret", secret: true },
  ],
  kotak: [
    { key: "consumerKey", label: "Consumer Key", secret: true },
    { key: "mobile", label: "Mobile No.", placeholder: "+91XXXXXXXXXX" },
    { key: "ucc", label: "UCC" },
    { key: "mpin", label: "MPIN", secret: true },
    { key: "totpSecret", label: "TOTP Secret", secret: true },
  ],
  dhan: [
    { key: "clientId", label: "Client ID" },
    { key: "accessToken", label: "Access Token", secret: true },
  ],
  // ICICI has no TOTP and no long-lived token: the session key is issued by a
  // browser login and expires daily, so it is filled by the "Log in with
  // ICICI" button in the dialog rather than typed.
  icici: [
    { key: "apiKey", label: "API Key", secret: true },
    { key: "apiSecret", label: "API Secret", secret: true },
    { key: "sessionToken", label: "Session Key", secret: true,
      placeholder: "Use the Log in with ICICI button below" },
  ],
  // Firstock hashes the password itself (SHA-256) before it leaves the sidecar,
  // so the user types it exactly as they would on Firstock's own site. Vendor
  // Code and API Key are a matched pair from the Firstock API app. The TOTP
  // secret is the 2FA SETUP KEY, not a code: Firstock rejects every login that
  // arrives without a TOTP, so the sidecar generates a fresh one per attempt
  // (which is also what makes unattended re-authentication possible).
  firstock: [
    { key: "userId", label: "User ID" },
    { key: "password", label: "Password", secret: true },
    { key: "vendorCode", label: "Vendor Code" },
    { key: "apiKey", label: "API Key", secret: true },
    { key: "totpSecret", label: "TOTP Secret", secret: true,
      placeholder: "2FA setup key — not a 6-digit code" },
  ],
};

/** Generic credential fields for a user-added custom broker (which has no
 *  predefined SDK field spec). Sensitive fields use the eye-toggle. */
export const GENERIC_CRED_FIELDS: CredField[] = [
  { key: "apiKey", label: "API Key", secret: true },
  { key: "apiSecret", label: "API Secret", secret: true },
  { key: "clientId", label: "Client ID" },
  { key: "pin", label: "PIN", secret: true },
  { key: "totpSecret", label: "TOTP Secret", secret: true },
];

/** Credential field spec for any broker key — predefined brokers use their
 *  specific spec; custom brokers fall back to the generic set. */
export function credFieldsFor(broker: string): CredField[] {
  return CRED_FIELDS[broker as BrokerId] ?? GENERIC_CRED_FIELDS;
}

/** True for the built-in SDK-backed brokers (angel/kotak/dhan). */
export function isPredefinedBroker(broker: string): broker is BrokerId {
  return (BROKER_ORDER as string[]).includes(broker);
}

/** Human label for any broker key — predefined label, else the custom
 *  broker's user-typed label, else the raw key as a last resort. */
export function brokerLabel(broker: string): string {
  return (
    BROKER_LABEL[broker as BrokerId] ??
    useCustomBrokersStore.getState().labelFor(broker) ??
    broker
  );
}

export type Credentials = Record<string, string>;

/** Account metadata — never carries secret values (those live only in the
 *  encrypted store and are fetched just-in-time for a connect). `broker` is a
 *  BrokerId for predefined brokers or a custom broker key (see
 *  useCustomBrokersStore) for user-added ones. */
export interface BrokerAccount {
  id: string;
  broker: string;
  nickname: string;
  autoConnect: boolean;
  /** Opted in to receive LIVE orders. Independent of connectivity: a connected
   *  account without this still streams market data and reports positions, but
   *  never receives an order. Persisted with the account. */
  execute: boolean;
}

/** Live connection state for an account, driven by sidecar broker_status. */
export interface AccountHealth {
  health: BrokerHealth;
  detail?: string | null;
}

export function displayName(a: BrokerAccount): string {
  const label = brokerLabel(a.broker);
  return a.nickname ? `${a.nickname} (${label})` : label;
}

export function healthClass(h: BrokerHealth | undefined): string {
  switch (h) {
    case "connected":
      return "ok";
    case "connecting":
    case "reconnecting":
      return "pending";
    case "session_expired":
      return "warn";
    default:
      return "off";
  }
}
