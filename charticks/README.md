# Charticks — Execution Terminal (rebuild)

Electron + React front end over the existing Python trading engines, which run as
a local **sidecar** service. This is **Phase 0**: the app skeleton and the
Python↔React bridge, driven by a market **simulator** so the whole stack runs
without live broker credentials.

```
charticks/            Electron shell + React renderer (this folder)
../sidecar/           Python FastAPI + WebSocket bridge + simulator
../engines/           Existing broker/candle/filter/execution engines (reused in Phase 1)
```

## Architecture

- **Control plane** — REST (FastAPI) for request/response (`/health`, `/option-chain`, …).
- **Data plane** — one WebSocket (`/stream`) streaming typed events
  (`index_quote`, `position_update`, `pnl_update`, `broker_status`, …) into
  Zustand stores in the renderer.
- **Guard** — the Electron main process generates a per-launch token
  (`CHARTICKS_BRIDGE_TOKEN`) and hands it to both the sidecar and the renderer;
  the sidecar rejects any other caller. In plain-browser dev the token is `dev`.

Event contract lives in two mirrored files — keep them in sync:
`charticks/src/bridge/events.ts` ⇄ `sidecar/bridge/events.py`.

## Run (dev)

Prereqs: Node 18+, Python 3.11+.

```bash
# 1. sidecar deps (once)
cd sidecar
python -m pip install -r requirements.txt

# 2. everything together (from charticks/)
cd ../charticks
npm install
npm run dev          # starts Vite + Electron + sidecar (uvicorn --reload)
```

Or run pieces individually:

```bash
# sidecar only
cd sidecar && python -m uvicorn server:app --host 127.0.0.1 --port 8787 --reload

# renderer only (plain browser, uses token "dev")
cd charticks && npm run dev:vite
```

## Verify the bridge

```bash
curl http://127.0.0.1:8787/health
# {"ok":true,"service":"charticks-sidecar","version":"0.1.0"}
```

Open the app — index tiles, positions, net P&L, broker health dots, and the
option-chain ladder should all stream and flash live. Toggle theme (dark/light)
and density (comfortable/compact/dense) from the status bar.

## Next phases

- **Phase 1** — replace `sidecar/services/simulator.py` with the real engines
  (Angel/Kotak/Dhan) publishing to the same `EventHub`; add broker login.
- **Phase 2** — order place/modify/square-off, SQLite persistence.
- **Phase 3** — strategy presets + live runs.
