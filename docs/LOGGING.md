# Logging & diagnostics

_Implemented 2026-08-11. Module: `sidecar/diagnostics.py`._

## What it was before

Charticks wrote **no log files of its own**. The `logs/` folder that existed was
created by the Angel SDK's bundled logzero; exactly two Charticks modules
piggybacked on that logger (`angel_feed.py`, `market_data.py`) out of ~22,000
lines.

Everything else went to `hub.publish(events.log_line(...))`, which streams to
the log panel in the UI and was never persisted. Close the app — or crash it —
and the entire diagnostic record was gone. That is why the folder appeared not
to update when something failed: it genuinely wasn't.

Also missing: any `sys.excepthook`, any FastAPI exception handler (an unhandled
error became a bare 500 with the trace on a stdout nobody reads), and ~26
`except: pass` handlers that discarded the exception entirely.

## Where the logs are

`Documents\Charticks\logs\` in the packaged app — Electron resolves the location
(`charticks/electron/logs.ts`) and passes the parent as `CHARTICKS_LOG_DIR`. In
dev it falls back to `sidecar/logs/`.

```
application.log       every event, chronological — the narrative
broker.log            logins, disconnects, reconnects, session expiry
orders.log            placement, acknowledgement, fills, cancels
websocket.log         feed connect/subscribe/interruption/recovery
risk.log              validation started / passed / rejected, with the rule
exceptions.log        every unexpected exception, with a full stack trace
sidecar-process.log   sidecar stdout/stderr, written by Electron
startup.log           this launch's phase timings, written live
startup-history.log   every previous launch's completed timeline
main-process.log      anything that killed the Electron main process
```

**Why not `%APPDATA%`.** It was there first, and it is the conventional answer.
It also failed the only test that matters: a tester whose Kotak Neo live order
was failing could not produce a single log line, because the path existed only
inside a troubleshooting document and pointed into a hidden folder. `Documents`
is somewhere a person finds without being told, and survives uninstalling the
app. AppData remains the automatic fallback when `Documents` cannot be written
(redirected/offline OneDrive, locked-down machine); the probe is a real file
write, since a redirected folder can exist and still refuse one.

The instrument cache did **not** move — it is ~100 MB a day of machine noise and
stays in `%APPDATA%\Charticks\data_cache` under `CHARTICKS_DATA_DIR`.

**Reaching them from inside the app:** Settings → Diagnostics → *Open Logs
Folder*, or *Save Diagnostics ZIP*, which writes `charticks-logs-<timestamp>.zip`
to the Desktop (current `*.log` files only, not the rotated backups) and reveals
it in Explorer. That path is what testers should be pointed at, rather than any
written-down folder location.

Every line lands in its category file **and** in `application.log`, so the
per-category files stay readable while `application.log` remains the single
chronological account. Rotation is 5 MB × 5 backups per file — bounded at about
150 MB worst case.

`sidecar-process.log` is the one Electron writes. It exists because a sidecar
that dies before its own logging starts — an import error, a missing
dependency, a port already in use — otherwise leaves no trace at all in a
packaged build.

## Format

One line per event: a fixed prefix, then `key=value` fields.

```
2026-08-11 14:25:12.183 | ERROR    | orders     | Place Order status=rejected
    broker="Angel One" account=a1b2c3d4… symbol="NIFTY 26AUG2026 25000 CE"
    side=BUY qty=75 code=MAX_LOSS_REACHED reason="Maximum Loss reached (…)"
```

Single-line rather than a multi-line block on purpose: failures are usually
found by grepping for an order id, a symbol or an account across several files
at once, and multi-line records break that as well as `tail` and rotation. Every
field from the original spec is present — timestamp, level, component, broker,
masked account, action, status, detail, and a full trace for exceptions.

Account ids are truncated to an 8-character prefix (`mask_account`): enough to
correlate lines across files, not enough to need redacting before a user sends a
log file in. **Credentials are never logged.** A login failure records which
credential *fields* were supplied, never their values — that distinguishes "you
left the TOTP secret blank" from "the broker rejected your PIN" without putting
a secret on disk.

## Guarantees

- **Nothing dies quietly.** `sys.excepthook`, `threading.excepthook`, the
  asyncio exception handler and a FastAPI catch-all all route to
  `exceptions.log` with a trace. The HTTP catch-all returns a structured
  `INTERNAL_ERROR` telling the user where to look.
- **Logging never breaks trading.** If the log directory cannot be written
  (read-only install, full disk) `install()` degrades to stderr rather than
  raising. Every logging call site is either non-throwing or guarded.
- **Logging is installed at import**, not in the FastAPI startup event —
  anything that fails while the app object is being constructed happens before
  startup fires, and would otherwise fall through to Python's last-resort
  stderr writer.

## Adding to it

```python
import diagnostics

diagnostics.event("orders", "Place Order", "rejected",
                  broker="Angel One", account=account_id,
                  symbol=symbol, reason=err)          # structured lifecycle event
diagnostics.exception("broker", "Login crashed", exc_info=exc, broker=label)
diagnostics.emit("websocket", "warn", "subscribe failed", tokens=len(batch))
```

`event()` derives its level from the status, so a failure can't accidentally be
filed as INFO. Pass `publish=True` to also push the line to the UI log panel;
the existing `_log()` sinks in each service already do this, which is how every
pre-existing log call became persistent without touching its call site.
