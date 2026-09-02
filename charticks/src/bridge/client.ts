import type { BridgeEvent } from "./events";

interface BridgeConfig {
  restUrl: string;
  wsUrl: string;
  token: string;
}

type Listener = (e: BridgeEvent) => void;
type StatusListener = (connected: boolean) => void;

/** How long a control-plane call may hang before it is reported as failed.
 *
 *  `fetch` has NO timeout of its own: a request to a socket that was accepted
 *  and then abandoned never settles, and an `await` on it never returns. That
 *  is not a hypothetical — a stale sidecar left holding the dev port poisoned
 *  the connection pool and left the Brokers page on "Connecting…" forever,
 *  with no error anywhere, because the `catch` that renders one was never
 *  reached. A screen stuck mid-action with nothing to read is worse than a
 *  failure: there is nothing to act on and no reason to believe it is stuck.
 *
 *  Generous on purpose — this is a backstop against a dead socket, not a
 *  latency budget. Anything slower than this is broken, not busy. */
const REQUEST_TIMEOUT_MS = 30_000;

/** Broker login is the one call that is legitimately slow: an SDK handshake
 *  plus an instrument-master download, and Firstock alone allows 45s for the
 *  login and 30s more to validate the session. Cutting that short would report
 *  a working connect as a failure, so it gets its own, longer deadline. */
const CONNECT_TIMEOUT_MS = 150_000;

const SLOW_PATHS = ["/brokers/connect", "/brokers/reconnect"];

function timeoutFor(path: string): number {
  return SLOW_PATHS.some((p) => path.startsWith(p))
    ? CONNECT_TIMEOUT_MS
    : REQUEST_TIMEOUT_MS;
}

/** `fetch` with a deadline, reported as an error a user can act on.
 *
 *  AbortError is rewritten because the raw wording ("The user aborted a
 *  request") is actively misleading in a tooltip: the user aborted nothing. */
async function fetchWithTimeout(url: string, init: RequestInit,
                                timeoutMs: number, path: string): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") {
      throw new Error(
        `${path} timed out after ${Math.round(timeoutMs / 1000)}s — the sidecar ` +
        `accepted the connection but never answered. If this persists, restart ` +
        `the app; a leftover sidecar on the same port will do this.`,
      );
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

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
    const res = await fetchWithTimeout(`${this.cfg.restUrl}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this.cfg.token}`,
      },
      body: body ? JSON.stringify(body) : undefined,
    }, timeoutFor(path), path);
    if (!res.ok) throw new Error(`${path} → ${res.status}`);
    return res.json() as Promise<T>;
  }

  async get<T = unknown>(path: string): Promise<T> {
    if (!this.cfg) this.cfg = await resolveConfig();
    const res = await fetchWithTimeout(`${this.cfg.restUrl}${path}`, {
      headers: { Authorization: `Bearer ${this.cfg.token}` },
    }, timeoutFor(path), path);
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
