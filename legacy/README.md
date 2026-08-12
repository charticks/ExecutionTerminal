# Legacy Tkinter bot (frozen)

This is the original single-window Tkinter trading bot. It is **retired**: it is
not built, not shipped, not tested, and nothing in `charticks/` or `sidecar/`
imports from it. It is kept only as a reference for behaviour that has not yet
been ported to the Charticks execution terminal.

| | |
|---|---|
| Entry point | `main.py` (`python legacy/main.py`) |
| UI | Tkinter (`app/gui_builder.py`, `app/bot_app.py`) |
| Brokers | Angel One, Kotak Neo, Dhan — via `app/login.py` |
| Extra dependency | `tkcalendar` (not in the root `requirements.txt`) |

## It will not run as-is — this is deliberate

`app/login.py` reads credentials from a top-level `config.py` and an
`accounts.json`, both of which stored API keys, PINs and TOTP secrets **in
plaintext**. Those files have been deleted.

Charticks now keeps every broker credential in one place: the Electron
credential store, encrypted through the OS keystore (DPAPI on Windows) — see
`charticks/electron/main.ts`. There is no second credential store, and no
plaintext copy anywhere in this repo.

If you genuinely need to run this bot again, supply the values it expects
through environment variables rather than recreating the plaintext files.

## What is still worth reading here

These are the parts the sidecar has not fully replaced:

- `engines/trade_execution_engine.py` — tick-driven SL / Target / Trailing SL
  and auto-exit. **Not yet ported**; live positions in Charticks do not exit
  automatically. This is the reference implementation for that work.
- `app/order_manager.py` — Kotak and Dhan live order placement. The sidecar
  ports Angel and ICICI only.
- `app/backtest.py`, `app/multi_strategy_runner.py`, `app/signal_filters.py` —
  strategy layer, with no equivalent in Charticks yet.
