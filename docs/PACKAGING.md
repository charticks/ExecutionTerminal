# Packaging the Windows build

_Bundled Python runtime, 2026-08-12._

## The problem this solves

Charticks' trading engine is a Python sidecar. The installer previously shipped
only `.py` source and the app spawned a bare `python` from PATH. On a machine
without Python it installed fine, opened fine, sat on "Connecting…" and did
nothing — while the tester docs said *"the trading engine is built into the
installer… ignore setup-tester.bat"*.

Builds now carry their own interpreter, so that statement is true.

## Building

```bash
npm run build          # builds the runtime, then the app
npm run build:runtime  # just the runtime (skips if present)
npm run build:runtime -- --force
```

`scripts/build-runtime.mjs` downloads python.org's **embeddable** distribution
matching the host version, installs `scripts/runtime-requirements.txt` into
`Lib/site-packages`, and verifies the result can import every broker SDK. Output
is `charticks/runtime/python` (~150 MB, gitignored), shipped to
`resources/python` by electron-builder.

Build machine needs: Python 3.12 64-bit on PATH (its pip resolves the wheels)
and `git` (Kotak's SDK is a git dependency). **Testers need neither.**

Expect the installer to roughly double, ~166 MB → ~290 MB.

## Why embeddable Python, not PyInstaller

Freezing rewrites how imports resolve. `breeze_connect` does a bare
`import config` that already needs a `sys.modules` shim
(`services/feeds/icici_feed.py`), and `smartapi-python` writes log files
relative to its own location. Keeping real files on disk means imports behave
exactly as they do in development, and an import problem is debuggable with a
normal traceback.

## Three traps, all hit during implementation

**The embeddable distribution runs isolated.** Its `pythonNNN._pth` fully
determines `sys.path`: the working directory is *not* importable and
`PYTHONPATH` is ignored. `python -m uvicorn server:app` with `cwd` set — which
works in dev — fails with `ModuleNotFoundError: server`. The sidecar is
therefore launched with `--app-dir`, which uvicorn puts on the path itself.

**Kotak Neo is not on PyPI.** `pip install neo-api-client` fails with "No
matching distribution found"; it exists only at
`github.com/Kotak-Neo/Kotak-neo-api-v2`. Both `requirements.txt` and
`setup-tester.bat` named the PyPI package, so **neither could ever have worked**
on a clean machine. Both now use the git URL.

**Kotak's declared dependencies must not be honoured.** It pins
`websockets==8.1` while Dhan requires `>=12.0.1` and `uvicorn[standard]`
`>=10.4` — unresolvable as a set, which is what pip reports. It also declares
`asyncio==3.4.3`, a dead backport that would **shadow the standard library**,
and `certifi==2022.12.7` / `idna==2.10` / `urllib3==1.26.14`, which would drag
the TLS stack of the process that talks to brokers back several years. It is
installed with `--no-deps`; what it genuinely needs is listed explicitly in
`runtime-requirements.txt`. Verified: the SDK imports and runs on modern
`websockets`.

## Trimming the build

The sidecar imports no pandas — all ~98 MB of pandas+numpy arrives with the
Dhan, Kotak and ICICI SDKs. An **Angel-only** build is ~10 MB of packages:
delete those three SDK lines (and pandas/numpy) from
`scripts/runtime-requirements.txt` and rebuild. Only do this if the build is
genuinely Angel-only — a tester who picks another broker would otherwise get an
import error at connect time.

## Verification

The build script fails loudly if the bundled interpreter cannot import the
SDKs. Beyond that, this layout was tested by replicating `resources/` and
spawning the sidecar exactly as `main.ts` does:

```
resources/{python,sidecar}                 157 MB
GET /health                                {"ok":true,…}
GET /live-book  (correct bearer)           {"positions":[],…}
GET /live-book  (no bearer)                {"detail":"unauthorized"}
logs/ written under CHARTICKS_LOG_DIR      6 files
  (Documents\Charticks\logs; appData only as a fallback — see docs/LOGGING.md)
```

## If the runtime is missing

`pythonExecutable()` falls back to a PATH `python`, so a build made without
`build:runtime` still works for anyone who has Python. If neither exists, the
spawn fails with ENOENT — which arrives as `'error'`, not `'exit'` — and the
app shows a dialog naming the problem and writes to
`Documents\Charticks\logs\sidecar-process.log`. Before this, that failure
produced no dialog and no log entry at all.
