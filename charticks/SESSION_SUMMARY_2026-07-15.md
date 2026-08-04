# Charticks Rebuild — Session Summary (2026-07-15)

Paste this file back next session to resume exactly where we left off.

---

## Goal
Re-platform the NIFTY options trading bot ("Charticks — Execution Terminal") from a
**Python 3.12 / Tkinter monolith** to an **Electron + React** desktop app, while
keeping the proven Python trading engines and running them as a **local sidecar
service**. Target diagram: Electron shell → React screens → Services (broker APIs,
WS manager, strategy/risk/order engines) → SQLite.

## Decisions locked in (do not re-litigate)
- **Backend = Python sidecar.** Reuse existing `engines/` (Angel/Kotak/Dhan WS,
  candle, filter, execution) as-is behind a FastAPI + WebSocket bridge. No Node rewrite.
- **v1 scope = core trading loop:** Dashboard, Option Chain, Positions, Brokers/login,
  run a strategy live. Backtest / Alerts / multi-strategy deferred.
- **UI = hybrid dark + light**, both first-class. **Accent = electric cyan.**
  **Default theme = dark. Default density = compact** (3 densities: comfortable/
  compact/dense, all CSS-token driven). Signature feel: numbers flash green/red on tick.
- Approved plan file: `C:\Users\apras\.claude\plans\charticks-electron-delegated-thimble.md`
- Approved UI mockups (Artifacts): cyan `https://claude.ai/code/artifact/c0b32396-4c96-4adf-a6fa-480ea865dc58`,
  violet `https://claude.ai/code/artifact/901c29e1-1218-4f8f-b794-3cabccef7e17` (we chose cyan).

## What we BUILT today — Phase 0 (skeleton + bridge), DONE & VERIFIED
New code lives in two new folders (the old Tkinter app is untouched):

```
charticks/                         Electron + React (Vite/TS) front end
  electron/main.ts                 window + Python sidecar supervisor + guard token
  electron/preload.ts              safe IPC surface (getBridgeConfig, onSidecarStatus)
  vite.config.ts                   vite + vite-plugin-electron (launches Electron in dev)
  index.html                       CSP allows localhost:8787 only
  src/bridge/events.ts             typed event contract (mirror of sidecar)
  src/bridge/client.ts             WS + REST client, auto-reconnect
  src/stores/useMarketStore.ts     live data (indices, positions, pnl, brokers, risk)
  src/stores/useUiStore.ts         theme + density + active screen (persisted)
  src/app/{App,Rail,StatusBar}.tsx + app.css   shell (icon rail, live status bar, kill-switch)
  src/screens/{Dashboard,OptionChain,Placeholder}.tsx
  src/components/{Icon,FlashNumber,Sparkline}.tsx
  src/theme/globals.css            design tokens: cyan accent, dark/light, 3 densities
  README.md                        run + architecture notes

sidecar/                           Python FastAPI + WebSocket bridge
  server.py                        /health, /option-chain (REST) + /stream (WS, token-guarded)
  bridge/events.py                 event builders (mirror of events.ts)
  bridge/hub.py                    thread-safe fan-out from engines -> WS clients (EventHub)
  services/simulator.py            Phase-0 market simulator (stands in for real engines)
  requirements.txt                 fastapi + uvicorn
```

### Architecture in one line
Control plane = REST (FastAPI). Data plane = one WebSocket streaming typed events
(`index_quote`, `position_update`, `pnl_update`, `broker_status`, `risk_event`, ...)
into Zustand stores. Guard token: `"dev"` in dev; random per-launch in packaged builds.
Keep `charticks/src/bridge/events.ts` and `sidecar/bridge/events.py` IN SYNC.

### Verified working (all actually run this session)
- Sidecar boots under uvicorn; `GET /health` OK; `GET /option-chain` returns NIFTY snapshot.
- WS `/stream` token-guarded (bad token -> 4401/403); streams all event types on connect
  (including a state snapshot for late joiners).
- `npm install` clean; `tsc -b --noEmit` passes; `vite build` bundles renderer + main + preload.
- **Full app launches in a real Electron window** via `npm run dev`; renderer connects
  (`WebSocket /stream?token=dev [accepted]`) and streams live simulated data.
- App is currently RUNNING (launched detached at end of session).

## Environment setup done on this machine
- **Node.js 24 LTS installed via winget** (was absent). It's at `%ProgramFiles%\nodejs`.
  A NEW terminal will have it on PATH.
- Sidecar Python deps installed (`fastapi`, `uvicorn`). Python 3.12.10.

## Gotchas we hit & fixed (context for future debugging)
1. **`ELECTRON_RUN_AS_NODE=1`** is injected by VS Code's terminal/extension host and
   makes the electron binary run as plain Node -> `require("electron")` crash. Fixed by
   `delete process.env.ELECTRON_RUN_AS_NODE` at top of `vite.config.ts`.
2. **ESM/CJS:** removed `"type":"module"` from package.json; Electron main/preload build
   as CommonJS; let vite-plugin-electron own the Electron launch (removed the redundant
   `dev:electron` + wait-on script that double-launched Electron).
3. **Dev token 403:** sidecar (npm script) uses default token `"dev"`, so `main.ts` uses
   `"dev"` in dev too; random token only in packaged builds.

## Known issue to resolve in Phase 1
- **websockets version conflict:** Kotak's `neo-api-client` pins `websockets==8.1` but
  `uvicorn[standard]` pulled `websockets 16.1`. Resolve before wiring the real Kotak
  engine — recommend a **dedicated venv for the sidecar** so its deps don't fight the
  engines' deps.

## How to run
```powershell
# open a NEW terminal so Node is on PATH
cd "c:\Users\apras\Desktop\Nifty\Strategy\Bot\ExecToolV2\TradeExecutionTool\charticks"
npm run dev        # starts Vite + Electron + Python sidecar together
# stop: Ctrl+C in the terminal, or close the window
```
First-time-only sidecar deps (already done here):
`cd ..\sidecar && python -m pip install -r requirements.txt`

## NEXT: Phase 1 (what to do tomorrow)
Replace `sidecar/services/simulator.py` with the REAL engines publishing to the same
`EventHub`, and add broker login. Concretely:
1. Create a dedicated sidecar venv; resolve the websockets pin conflict.
2. Port broker login (from `app/login.py`, strip Tkinter) into `sidecar/services/`.
3. Wire `engines/option_chain_engine.py` (+ kotak/dhan data engines) tick callbacks to
   `hub.publish(events.tick(...))` and index quotes to `events.index_quote(...)`.
4. Feed `candle_engine` -> `quant_filter_engine`; keep positions/pnl flowing via the
   same events the simulator already emits (contract unchanged, so the UI just works).
5. Add REST endpoints for broker login + real option-chain snapshot.
6. Build the **Brokers** screen (currently a Placeholder) for login + connection health.

Verification for Phase 1: log in to a broker (paper/real), confirm the React Option
Chain + index tiles update in real time, matching the old Tkinter app side-by-side.

## Memory
Saved to project memory: `charticks-rearchitecture` (decisions, phase status, gotchas).
Recall it next session for the same context.
