import type { BridgeEvent } from "./events";

interface BridgeConfig {
  restUrl: string;
  wsUrl: string;
  token: string;
}

type Listener = (e: BridgeEvent) => void;
type StatusListener = (connected: boolean) => void;

/**
 * Single connection to the Python sidecar:
 *  - REST for request/response (control plane)
 *  - one WebSocket streaming typed events (data plane)
 * Auto-reconnects with backoff. The renderer subscribes once via `on()`.
 */
class BridgeClient {
  private cfg: BridgeConfig | null = null;
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private statusListeners = new Set<StatusListener>();
  private backoff = 500;
  private closed = false;

  async connect() {
    this.cfg = await resolveConfig();
    this.closed = false;
    // The sidecar's port is chosen at launch and re-chosen if it has to be
    // restarted, so a cached config can go stale mid-session. The main process
    // says when that happens; without this the client would keep dialling a
    // port nothing is listening on and never recover.
    onConfigChanged(() => {
      void (async () => {
        this.cfg = await resolveConfig();
        this.backoff = 500;
        this.ws?.close();     // onclose schedules the reconnect on the new port
      })();
    });
    this.openSocket();
  }

  private openSocket() {
    if (!this.cfg) return;
    const url = `${this.cfg.wsUrl}?token=${encodeURIComponent(this.cfg.token)}`;
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onopen = () => {
      this.backoff = 500;
      this.emitStatus(true);
    };
    ws.onmessage = (msg) => {
      try {
        const evt = JSON.parse(msg.data) as BridgeEvent;
        this.listeners.forEach((l) => l(evt));
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = () => {
      this.emitStatus(false);
      if (!this.closed) this.scheduleReconnect();
    };
    ws.onerror = () => ws.close();
  }

  private scheduleReconnect() {
    setTimeout(() => this.openSocket(), this.backoff);
    this.backoff = Math.min(this.backoff * 2, 10_000);
  }

  private emitStatus(connected: boolean) {
    this.statusListeners.forEach((l) => l(connected));
  }

  on(l: Listener) {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }

  onStatus(l: StatusListener) {
    this.statusListeners.add(l);
    return () => this.statusListeners.delete(l);
  }

  /** Control-plane request. */
  async post<T = unknown>(path: string, body?: unknown): Promise<T> {
    if (!this.cfg) this.cfg = await resolveConfig();
    const res = await fetch(`${this.cfg.restUrl}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this.cfg.token}`,
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(`${path} → ${res.status}`);
    return res.json() as Promise<T>;
  }

  async get<T = unknown>(path: string): Promise<T> {
    if (!this.cfg) this.cfg = await resolveConfig();
    const res = await fetch(`${this.cfg.restUrl}${path}`, {
      headers: { Authorization: `Bearer ${this.cfg.token}` },
    });
    if (!res.ok) throw new Error(`${path} → ${res.status}`);
    return res.json() as Promise<T>;
  }

  disconnect() {
    this.closed = true;
    this.ws?.close();
  }
}

/**
 * In Electron the config comes from the main process (with the guard token).
 * When running the renderer in a plain browser (vite dev without Electron),
 * fall back to the default localhost sidecar so the UI still streams.
 */
interface CharticksApi {
  getBridgeConfig?(): Promise<BridgeConfig>;
  onBridgeConfigChanged?(cb: () => void): () => void;
}

function api(): CharticksApi | undefined {
  return (window as unknown as { charticks?: CharticksApi }).charticks;
}

async function resolveConfig(): Promise<BridgeConfig> {
  const bridgeApi = api();
  if (bridgeApi?.getBridgeConfig) return bridgeApi.getBridgeConfig();
  // Plain browser (vite dev without Electron): the dev sidecar runs on the
  // fixed port with the fixed token.
  return {
    restUrl: "http://127.0.0.1:8787",
    wsUrl: "ws://127.0.0.1:8787/stream",
    token: "dev",
  };
}

/** The main process re-chose the sidecar's port. */
function onConfigChanged(cb: () => void): void {
  api()?.onBridgeConfigChanged?.(cb);
}

export const bridge = new BridgeClient();
