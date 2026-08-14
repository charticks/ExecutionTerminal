"""Headless broker connectivity for the sidecar.

Phase-1 replacement for the fabricated ``broker_status`` events in
``simulator.py``. This is a headless port of ``app/login.py``'s ``LoginMixin``:
the Angel / Kotak / Dhan authentication *sequences* are reused byte-for-byte in
call order, and only the GUI side-effects change —

    self.log(...)             -> hub.publish(events.log_line(...))
    messagebox.showerror(...) -> structured {ok: False, error} return
    tk indicator / status_var -> hub.publish(events.broker_status(...))

Accounts are managed by the Electron main process (encrypted credential store).
The renderer forwards the decrypted credentials to ``connect()`` per account, so
this manager is credential-store agnostic: it authenticates with whatever it is
handed, keeps sessions keyed by ``account_id``, and owns the connecting ->
connected / down / session_expired health model for each account.

The Tkinter app this was ported from is retired and frozen under ``legacy/``;
the sidecar no longer imports from it or shares any state with it.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import sys
import threading
import time
from typing import Any

import diagnostics
from bridge import events
from bridge.hub import hub

from services import expiry as expiry_filter
from services.broker_limits import limit_resolver
from services.instruments import InstrumentKey, instruments
from services.feed_router import FeedRouter
from services.paths import data_dir
from services.reliability.session_manager import SessionManager
from services.reliability.subscription_registry import SubscriptionRegistry
from services.reliability.health_monitor import ConnectionHealthMonitor


SUPPORTED = ("angel", "kotak", "dhan", "icici")
LABEL = {"angel": "Angel One", "kotak": "Kotak Neo", "dhan": "Dhan HQ",
         "icici": "ICICI Direct"}

# Health values kept in sync with charticks/src/bridge/events.ts BrokerHealth.
CONNECTING = "connecting"
CONNECTED = "connected"
SESSION_EXPIRED = "session_expired"
DOWN = "down"


class BrokerManager:
    """Owns broker sessions + health, keyed by account id. Thread-safe;
    connect/disconnect run on a worker thread (the REST endpoints hand off)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._health: dict[str, str] = {}
        self._detail: dict[str, str | None] = {}
        self._broker: dict[str, str] = {}  # account_id -> broker type
        self._sessions: dict[str, Any] = {}  # account_id -> SDK session
        # Credentials cached in-memory only (never persisted here — the
        # Electron encrypted store owns that) so the reliability layer can
        # re-run the login sequence automatically after a session expires,
        # without prompting the user.
        self._creds: dict[str, dict] = {}
        # Which account owns the shared Angel market feed. The feed object
        # itself lives in the router; this only records ownership so a re-auth
        # by that account refreshes the feed's tokens.
        self._market_account: str | None = None
        self.instrument_master: list[dict] = []
        # Accounts opted in to receive LIVE orders, pushed by the renderer from
        # the persisted broker config (POST /brokers/execute). Starts EMPTY on
        # purpose: until the renderer has told us, live placement is refused
        # rather than falling back to "every connected broker", which is the
        # accidental-duplicate-order bug this set exists to prevent. The
        # renderer re-pushes on every sidecar reconnect, so a sidecar restart
        # self-heals within a moment instead of silently mis-routing.
        self._execute_accounts: set[str] = set()

        # ── Reliability layer ───────────────────────────────────────────
        self.subscriptions = SubscriptionRegistry()
        self.session_manager = SessionManager(
            set_health=self._set_health,
            reauthenticate=self.reauthenticate,
            on_recovered=self._on_session_recovered,
        )
        # Owns every broker feed. Dependencies are injected rather than
        # imported so the router stays independent of this module.
        self.router = FeedRouter(
            subscriptions=self.subscriptions,
            instrument_master=lambda: self.instrument_master,
            option_exchange=self.option_exchange,
            report_error=lambda aid, err: self.session_manager.report_error(
                aid, self._broker.get(aid, ""), err),
            log=self._log,
        )
        self.health_monitor = ConnectionHealthMonitor(
            status_map=self.status_map,
            session_manager=self.session_manager,
            # Callable, not a snapshot: feeds attach/detach with accounts.
            ws_managers=self.router.ws_managers,
        )
        self.health_monitor.start()

    # ── logging / status helpers (replace tk side-effects) ────────────────
    def _log(self, level: str, msg: str) -> None:
        # Persisted to broker.log + application.log AND streamed to the UI panel.
        diagnostics.emit("broker", level, msg, publish=True)

    def _set_health(self, account_id: str, broker: str, health: str, detail: str | None = None) -> None:
        with self._lock:
            self._health[account_id] = health
            self._detail[account_id] = detail
            self._broker[account_id] = broker
        hub.publish(events.broker_status(broker, health, detail, account=account_id))

    # ── read model for REST / stream snapshot ─────────────────────────────
    def status_map(self) -> dict[str, dict]:
        with self._lock:
            return {
                aid: {"health": self._health[aid], "detail": self._detail.get(aid), "broker": self._broker.get(aid)}
                for aid in self._health
            }

    def connected_sessions(self) -> list[tuple[str, str, Any]]:
        """(account_id, broker, session) for every currently-connected account —
        used by the positions adapter to poll each account's broker position book."""
        with self._lock:
            return [
                (aid, self._broker.get(aid, ""), self._sessions[aid])
                for aid in self._sessions
                if self._health.get(aid) == CONNECTED
            ]

    def live_session(self, account_id: str) -> tuple[str, Any] | None:
        """(broker, session) for one connected account, or None.

        Deliberately NOT filtered by the execution opt-in: this is what a modify
        or a cancel resolves against, and a user who turns Execute off still has
        to be able to pull a working order they already placed. Same principle as
        the kill switch allowing exits — a control that traps you in an order is
        not a safety feature.
        """
        with self._lock:
            if self._health.get(account_id) != CONNECTED:
                return None
            session = self._sessions.get(account_id)
            if session is None:
                return None
            return self._broker.get(account_id, ""), session

    # ── execution selection (which accounts may receive LIVE orders) ───────
    def set_execution_accounts(self, account_ids: list[str]) -> dict:
        """Replace the execution set. Authoritative for live routing; the
        Brokers page is the only thing that writes it."""
        ids = {str(a) for a in (account_ids or []) if a}
        with self._lock:
            changed = ids != self._execute_accounts
            self._execute_accounts = ids
        if changed:
            names = ", ".join(sorted(ids)) if ids else "none"
            self._log("info", f"[broker] live execution accounts → {names}")
        return {"ok": True, "accountIds": sorted(ids)}

    def execution_accounts(self) -> set[str]:
        """The opted-in set, regardless of connectivity. An empty set means the
        user has not enabled execution anywhere — distinct from 'enabled but
        nothing connected', which is a different error for the user."""
        with self._lock:
            return set(self._execute_accounts)

    def execution_sessions(self) -> list[tuple[str, str, Any]]:
        """(account_id, broker, session) for accounts that are BOTH connected
        and opted in to execution. This — not connected_sessions() — is what
        live order placement fans out over."""
        with self._lock:
            return [
                (aid, self._broker.get(aid, ""), self._sessions[aid])
                for aid in self._sessions
                if self._health.get(aid) == CONNECTED and aid in self._execute_accounts
            ]

    def snapshot_events(self) -> list[dict]:
        """Replayed to each newly-connected WS client so late joiners are correct."""
        with self._lock:
            return [
                events.broker_status(self._broker.get(aid, ""), self._health[aid],
                                     self._detail.get(aid), account=aid)
                for aid in self._health
            ]

    # ── connect / disconnect entry points ─────────────────────────────────
    def connect(self, account_id: str, broker: str, credentials: dict | None) -> dict:
        broker = (broker or "").lower()
        if broker not in SUPPORTED:
            return {"ok": False, "error": f"unknown broker '{broker}'"}
        creds = credentials or {}
        with self._lock:
            self._creds[account_id] = creds
        # A fresh session may carry different order limits — re-resolve them.
        limit_resolver.invalidate(account_id)
        self._set_health(account_id, broker, CONNECTING)
        label = LABEL.get(broker, broker)
        # Which credential fields arrived (NEVER their values) — "login failed"
        # is most often a missing field, and this distinguishes that from a
        # genuine auth rejection without ever putting a secret in a log file.
        diagnostics.event("broker", "Broker login", "started", broker=label,
                          account=account_id, credentialFields=",".join(sorted(creds)))
        try:
            # Every broker gets an EXPLICIT branch — no fallthrough. The old
            # trailing `return self._connect_dhan(...)` meant any broker added
            # to SUPPORTED without a branch here was silently handed to the
            # Dhan connector, credentials and all.
            if broker == "angel":
                result = self._connect_angel(account_id, creds)
            elif broker == "kotak":
                result = self._connect_kotak(account_id, creds)
            elif broker == "dhan":
                result = self._connect_dhan(account_id, creds)
            elif broker == "icici":
                result = self._connect_icici(account_id, creds)
            else:
                self._set_health(account_id, broker, DOWN, "no connector implemented")
                diagnostics.event("broker", "Broker login", "failed", broker=label,
                                  account=account_id, reason="no connector implemented")
                return {"ok": False, "error": f"no connector for broker '{broker}'"}
            diagnostics.event(
                "broker", "Broker login", "success" if result.get("ok") else "failed",
                broker=label, account=account_id, reason=result.get("error"))
            return result
        except Exception as exc:  # defensive
            self._set_health(account_id, broker, DOWN, str(exc))
            self._log("error", f"❌ {label} connect crashed: {exc}")
            # Trace, not just the message: a connector crash is a bug in us, and
            # the message alone has never been enough to find one.
            diagnostics.exception("broker", "Broker login crashed", exc_info=exc,
                                  broker=label, account=diagnostics.mask_account(account_id))
            return {"ok": False, "error": str(exc)}

    def disconnect(self, account_id: str) -> dict:
        limit_resolver.invalidate(account_id)
        with self._lock:
            broker = self._broker.get(account_id, "")
            self._sessions.pop(account_id, None)
        with self._lock:
            self._creds.pop(account_id, None)
        # Tear down this account's feed, if it had one (mirrors logout()). The
        # router stops the socket and promotes another feed for any capability
        # this one was primary for, so the data plane self-heals.
        self.router.detach(account_id)
        if account_id == self._market_account:
            self._market_account = None
        self._set_health(account_id, broker, DOWN)
        self._log("info", f"🔒 {LABEL.get(broker, broker)} account disconnected")
        return {"ok": True}

    # ── market-data feed lifecycle ────────────────────────────────────────
    def _start_market_data_async(self, account_id: str, broker: str, session: Any,
                                 session_tokens: dict) -> None:
        """Load the instrument master and bring the feed up, off the connect path.

        Reference data is not authentication. Holding a broker connect open
        while ~40 MB of instrument master downloads made every first login of
        the day look like a slow login, and with auto-connect enabled it was the
        single longest phase of application startup.

        The user-visible state is honest throughout: the account is genuinely
        connected (its REST session works, orders can be placed as soon as the
        master resolves), and /market-feed reports the data plane separately —
        which is exactly the distinction that layer exists to make.
        """
        def run() -> None:
            try:
                started = time.time()
                self._load_master(session)
                self._log("info", f"[broker] instrument master ready in "
                                  f"{time.time() - started:.1f}s")
            except Exception as exc:
                # A failed master is not a failed login. The account stays
                # connected; the chain simply has nothing to resolve until a
                # retry succeeds, and says so.
                self._log("error", f"❌ Instrument master could not be loaded ({exc}) "
                                   f"— the option chain will stay empty until it is. "
                                   f"Reconnect the account to retry.")
                diagnostics.exception("broker", "Instrument master load failed",
                                      exc_info=exc, account=diagnostics.mask_account(account_id))
                return
            try:
                self._start_feed(account_id, broker, session_tokens=session_tokens)
            except Exception as exc:
                diagnostics.exception("broker", "Market feed start failed",
                                      exc_info=exc, broker=broker,
                                      account=diagnostics.mask_account(account_id))

        threading.Thread(target=run, daemon=True,
                         name=f"market-data-{account_id}").start()

    def _start_feed(self, account_id: str, broker: str,
                    session_tokens: dict | None = None) -> None:
        """Bring up (or reconcile) this account's market feed after a successful
        login. This is the single place where connecting a broker turns into a
        live WebSocket, so every broker that grows a feed gets the behaviour for
        free — including auto-reconnect on re-auth, since the tokens are
        re-applied here on each login attempt.

        A broker with no feed implementation returns None from attach() and is
        simply left alone; it still trades over REST.
        """
        feed = self.router.attach(account_id, broker)
        if feed is None:
            return
        if session_tokens and hasattr(feed, "apply_session"):
            feed.apply_session(**session_tokens)
        # Always reconcile — never assume the feed is alive just because we
        # started it once. `start()` is idempotent and revives a socket that is
        # registered as running but is not actually connected.
        feed.start()

    # ── reliability layer hooks ────────────────────────────────────────────
    def reauthenticate(self, account_id: str) -> dict:
        """Re-run the login sequence for `account_id` using cached
        credentials — used by SessionManager after detecting a dead session
        (e.g. AG8001). Does not touch the renderer-facing account list."""
        with self._lock:
            broker = self._broker.get(account_id, "")
            creds = self._creds.get(account_id)
        if broker == "icici":
            # The Breeze session token comes from a daily BROWSER login; the
            # cached one is dead by definition when we land here. `permanent`
            # makes SessionManager park the account at session_expired with
            # this message instead of burning its retries down to DOWN.
            return {"ok": False, "permanent": True,
                    "error": "ICICI session key expires daily — log in again "
                             "from the Brokers screen"}
        if not broker or not creds:
            return {"ok": False, "error": "no cached credentials for reauth"}
        # Session re-established — cached order limits must be re-resolved.
        limit_resolver.invalidate(account_id)
        if broker == "angel":
            return self._connect_angel(account_id, creds)
        if broker == "kotak":
            return self._connect_kotak(account_id, creds)
        if broker == "dhan":
            return self._connect_dhan(account_id, creds)
        return {"ok": False, "error": f"unknown broker '{broker}'"}

    def _on_session_recovered(self, account_id: str) -> None:
        """Called once re-auth succeeds — reconnects the shared market feed
        (it holds the now-stale jwt/feed token) and replays every active
        subscription so option chain / index feeds resume without the user
        touching anything."""
        feed = self.router.feed_for(account_id)
        if feed is not None:
            feed.reconnect()
        self.subscriptions.replay_all()

    # ── Angel One (reuses login.py:136-164 sequence) ──────────────────────
    def _connect_angel(self, account_id: str, creds: dict) -> dict:
        import pyotp
        from SmartApi import SmartConnect

        api_key = creds.get("apiKey")
        client_id = creds.get("clientId")
        pin = creds.get("pin")
        totp_secret = creds.get("totpSecret")
        if not (api_key and client_id and pin and totp_secret):
            msg = "Missing Angel credentials (apiKey/clientId/pin/totpSecret)"
            self._set_health(account_id, "angel", DOWN, msg)
            return {"ok": False, "error": msg}

        _MAX_LOGIN_ATTEMPTS = 3
        _LOGIN_RETRY_DELAY = 5
        for attempt in range(1, _MAX_LOGIN_ATTEMPTS + 1):
            try:
                totp = pyotp.TOTP(totp_secret).now()
                smart = SmartConnect(api_key=api_key, timeout=30)
                data = smart.generateSession(client_id, pin, totp)
                if not data.get("status"):
                    raise Exception("Angel One Login Failed — bad response")
                with self._lock:
                    self._sessions[account_id] = smart
                self._set_health(account_id, "angel", CONNECTED)
                self._log("info", f"✅ Angel One Login Success (attempt {attempt})")
                # First connected Angel account drives the shared market feed;
                # a re-authenticating owner refreshes its now-stale tokens too
                # (the actual WS reconnect is triggered by the reliability
                # layer's _on_session_recovered once this returns ok).
                if self._market_account in (None, account_id):
                    self._market_account = account_id
                    # The instrument master is ~40 MB and, on the first login of
                    # the day, has to be downloaded. Doing that inline held the
                    # whole connect open for as long as the download took, so an
                    # account that had authenticated in under a second sat at
                    # "connecting" for another ten to thirty — and with
                    # auto-connect on, that was the app's entire startup.
                    #
                    # Authentication is done and the session is usable, so the
                    # connect returns now. The master and the feed come up on a
                    # worker; the option chain retries on its own cycle and
                    # populates the moment they land.
                    self._start_market_data_async(account_id, "angel", smart, {
                        "jwt_token": data["data"]["jwtToken"].replace("Bearer ", ""),
                        "feed_token": data["data"]["feedToken"],
                        "client_code": data["data"]["clientcode"],
                        "api_key": api_key,
                    })
                return {"ok": True}
            except Exception as e:
                if attempt < _MAX_LOGIN_ATTEMPTS:
                    self._log("warn", f"⚠️  Angel login {attempt}/{_MAX_LOGIN_ATTEMPTS} failed: {e} — retrying in {_LOGIN_RETRY_DELAY}s")
                    time.sleep(_LOGIN_RETRY_DELAY)
                else:
                    self._set_health(account_id, "angel", SESSION_EXPIRED, str(e))
                    self._log("error", f"❌ Angel One Login Failed after {_MAX_LOGIN_ATTEMPTS} attempts: {e}")
                    return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "angel login exhausted retries"}

    # ── Kotak Neo (reuses login.py:181-260 sequence) ──────────────────────
    def _connect_kotak(self, account_id: str, creds: dict) -> dict:
        import pyotp
        try:
            from neo_api_client import NeoAPI
        except ImportError:
            msg = ("neo_api_client not installed. Run: pip install --force-reinstall "
                   "\"git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.1#egg=neo_api_client\"")
            self._set_health(account_id, "kotak", DOWN, msg)
            self._log("error", f"❌ Kotak: {msg}")
            return {"ok": False, "error": msg}

        consumer_key = creds.get("consumerKey")
        mobile = creds.get("mobile")
        ucc = creds.get("ucc")
        mpin = creds.get("mpin")
        totp_secret = creds.get("totpSecret")
        if not (consumer_key and mobile and ucc and mpin and totp_secret):
            msg = "Missing Kotak credentials (consumerKey/mobile/ucc/mpin/totpSecret)"
            self._set_health(account_id, "kotak", DOWN, msg)
            return {"ok": False, "error": msg}
        try:
            self._log("info", "🔄 Kotak Neo: initialising NeoAPI (v2 SDK)...")
            kotak = NeoAPI(consumer_key=consumer_key, environment="prod",
                           access_token=None, neo_fin_key=None)
            kotak_totp = pyotp.TOTP(totp_secret).now()
            self._log("info", "🔄 Kotak Neo: Step 1 — totp_login()...")
            login_resp = kotak.totp_login(mobile_number=str(mobile), ucc=str(ucc), totp=kotak_totp)
            if isinstance(login_resp, dict):
                err1 = (login_resp.get("error") or login_resp.get("Error")
                        or login_resp.get("message", ""))
                stat1 = str(login_resp.get("status", "")).lower()
                if err1 and stat1 not in ("ok", "success", "200", ""):
                    raise Exception(f"Kotak totp_login() failed — {err1}")
            self._log("info", "🔄 Kotak Neo: Step 2 — totp_validate() MPIN...")
            resp = kotak.totp_validate(mpin=str(mpin))

            kotak_ok = False
            if resp is not None:
                if isinstance(resp, dict):
                    status_val = str(resp.get("status", "")).lower()
                    error_val = resp.get("error") or resp.get("Error") or resp.get("fault")
                    has_token = bool(resp.get("token") or resp.get("access_token")
                                     or resp.get("trade_token")
                                     or (isinstance(resp.get("data"), dict)
                                         and (resp["data"].get("token")
                                              or resp["data"].get("trade_token"))))
                    if not error_val:
                        if status_val in ("ok", "success", "200") or has_token:
                            kotak_ok = True
                        elif status_val == "":
                            kotak_ok = True
                elif resp is True or str(resp).lower() in ("ok", "success"):
                    kotak_ok = True

            if not kotak_ok:
                raise Exception(f"Kotak login response did not indicate success. Raw: {resp}")
            with self._lock:
                self._sessions[account_id] = kotak
            self._set_health(account_id, "kotak", CONNECTED)
            self._log("info", "✅ Kotak Neo Login Success")
            # NeoAPI owns its WebSocket internally, so the feed rides THIS
            # session rather than creating one. It treats the object as
            # read-only — WS callbacks only, never login/logout/refresh —
            # because the order router and positions poller hold the same
            # reference. Re-supplied on every login so a user-initiated
            # reconnect clears the feed's needs-reauth latch.
            self._start_feed(account_id, "kotak", session_tokens={"client": kotak})
            return {"ok": True}
        except Exception as e:
            self._set_health(account_id, "kotak", SESSION_EXPIRED, str(e))
            self._log("error", f"❌ Kotak Neo Login Failed: {e}")
            return {"ok": False, "error": str(e)}

    # ── Dhan HQ (reuses login.py:266-308 sequence) ────────────────────────
    def _connect_dhan(self, account_id: str, creds: dict) -> dict:
        try:
            from dhanhq import dhanhq as DhanHQ, DhanContext
        except ImportError:
            msg = "dhanhq package not installed. Run: pip install dhanhq"
            self._set_health(account_id, "dhan", DOWN, msg)
            self._log("error", f"❌ Dhan: {msg}")
            return {"ok": False, "error": msg}

        client_id = creds.get("clientId")
        access_token = creds.get("accessToken")
        if not (client_id and access_token):
            msg = "Missing Dhan credentials (clientId/accessToken)"
            self._set_health(account_id, "dhan", DOWN, msg)
            return {"ok": False, "error": msg}
        try:
            self._log("info", "🔄 Dhan HQ: initialising client...")
            ctx = DhanContext(client_id=client_id, access_token=access_token)
            dhan = DhanHQ(ctx)
            resp = dhan.get_fund_limits()
            if isinstance(resp, dict) and resp.get("status") == "failure":
                raise Exception(resp.get("remarks") or resp.get("message") or "Dhan auth failed")
            with self._lock:
                self._sessions[account_id] = dhan
            self._set_health(account_id, "dhan", CONNECTED)
            self._log("info", "✅ Dhan Login Success")
            # Same credentials the order path already authenticated with — the
            # feed never reads the credential store itself. Re-applied on every
            # login so a re-auth hands the socket a fresh access token.
            self._start_feed(account_id, "dhan", session_tokens={
                "client_id": client_id,
                "access_token": access_token,
            })
            return {"ok": True}
        except Exception as e:
            self._set_health(account_id, "dhan", SESSION_EXPIRED, str(e))
            self._log("error", f"❌ Dhan Login Failed: {e}")
            return {"ok": False, "error": str(e)}

    # ── ICICI Direct (Breeze) ─────────────────────────────────────────────
    def _connect_icici(self, account_id: str, creds: dict) -> dict:
        try:
            # NOT a plain `from breeze_connect import ...` — the SDK does a bare
            # `import config` that resolves against whatever is on sys.path.
            # See services.feeds.icici_feed.import_breeze.
            from services.feeds.icici_feed import import_breeze
            BreezeConnect = import_breeze()
        except ImportError as e:
            msg = f"breeze-connect not importable ({e}). Run: pip install breeze-connect"
            self._set_health(account_id, "icici", DOWN, msg)
            self._log("error", f"❌ ICICI: {msg}")
            return {"ok": False, "error": msg}

        api_key = creds.get("apiKey")
        api_secret = creds.get("apiSecret")
        session_token = creds.get("sessionToken")
        if not (api_key and api_secret and session_token):
            msg = "Missing ICICI credentials (apiKey/apiSecret/sessionToken)"
            self._set_health(account_id, "icici", DOWN, msg)
            return {"ok": False, "error": msg}
        try:
            self._log("info", "🔄 ICICI Direct: validating session key...")
            breeze = BreezeConnect(api_key=api_key)
            breeze.generate_session(api_secret=api_secret,
                                    session_token=session_token)
            # generate_session does not fail loudly on a dead token — validate
            # with a real authenticated call before declaring the account up.
            resp = breeze.get_funds()
            status = resp.get("Status") if isinstance(resp, dict) else None
            err = resp.get("Error") if isinstance(resp, dict) else None
            if status != 200 or err:
                raise Exception(err or f"ICICI funds check failed (status {status})")
            with self._lock:
                self._sessions[account_id] = breeze
            self._set_health(account_id, "icici", CONNECTED)
            self._log("info", "✅ ICICI Direct Login Success")
            # Feed builds its OWN BreezeConnect from these — teardown can then
            # never disturb this trading session. Re-applied on every connect
            # so a fresh daily login clears the feed's needs-reauth latch.
            self._start_feed(account_id, "icici", session_tokens={
                "api_key": api_key,
                "api_secret": api_secret,
                "session_token": session_token,
            })
            return {"ok": True}
        except Exception as e:
            # session_expired, not DOWN: the by-far most likely cause is the
            # daily session key having lapsed, and the fix is a new login.
            detail = (f"{e} — ICICI's session key expires daily; use "
                      f"Login with ICICI in Edit Credentials, then reconnect")
            self._set_health(account_id, "icici", SESSION_EXPIRED, detail)
            self._log("error", f"❌ ICICI Direct Login Failed: {e}")
            return {"ok": False, "error": detail}

    # ── Instrument master (headless port of login.py:392-425) ─────────────
    def _load_master(self, smart: Any) -> None:
        import requests
        cache_folder = data_dir()
        today = dt.datetime.now().strftime("%Y%m%d")
        file_name = os.path.join(cache_folder, f"instrument_master_{today}.json")
        if os.path.exists(file_name):
            # A cache written by an interrupted run is truncated mid-JSON and
            # would otherwise fail every login for the rest of the day — drop
            # it and re-download instead of propagating the parse error.
            try:
                with open(file_name, encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list) or not data:
                    raise ValueError("instrument master cache is empty/not a list")
                self.instrument_master = data
                self._log("info", f"📦 Instrument master loaded from cache ({file_name})")
                return
            except Exception as e:
                self._log("warn", f"⚠️  Instrument master cache unusable ({e}) — re-downloading")
                try:
                    os.remove(file_name)
                except OSError:
                    pass
        self._log("info", "🔄 Downloading instrument master...")
        url = ("https://margincalculator.angelbroking.com/OpenAPI_File"
               "/files/OpenAPIScripMaster.json")
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        data = json.loads(r.content.decode("utf-8"))
        if not isinstance(data, list) or not data:
            raise ValueError("instrument master download returned no rows")
        self.instrument_master = data
        # Write via a temp file + replace so a crash mid-write can never leave
        # a half-written cache behind for the next login to choke on.
        tmp_name = file_name + ".tmp"
        try:
            with open(tmp_name, "w", encoding="utf-8") as f:
                json.dump(self.instrument_master, f)
            os.replace(tmp_name, file_name)
        except OSError as e:
            # Out of disk (Errno 28) is the common one. The master is already
            # in memory, so trading still works this session — just don't
            # leave a partial temp file behind.
            try:
                os.remove(tmp_name)
            except OSError:
                pass
            self._log("warn", f"⚠️  Could not cache instrument master ({e}) — continuing without cache")
            return
        # These are ~40 MB/day and are never read after their date; a full disk
        # is what truncated the cache and broke login in the first place.
        for old in sorted(glob.glob(os.path.join(cache_folder, "instrument_master_*.json")))[:-2]:
            try:
                os.remove(old)
            except OSError:
                pass
        self._log("info", f"✅ Instrument master downloaded & cached ({file_name})")

    # Angel exchange segment per supported index family (BSE indices → BFO).
    _OPT_EXCH = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "FINNIFTY": "NFO",
                 "MIDCPNIFTY": "NFO", "SENSEX": "BFO", "BANKEX": "BFO",
                 # Crude options are OPTFUT rows on MCX. The same contracts are
                 # mirrored under the NCO segment; pinning MCX here is what keeps
                 # those duplicates out of token resolution.
                 "CRUDEOIL": "MCX"}

    def option_exchange(self, underlying: str) -> str | None:
        """The option exchange segment for a supported index, or None when the
        index is not one Charticks trades. Callers filtering the instrument
        master compare against this so unsupported names are skipped."""
        return self._OPT_EXCH.get((underlying or "").upper())

    def feed_stale(self) -> bool:
        """True when every running feed is down or silent (connected but no
        ticks). Consulted before placing an entry: a market order priced off a
        frozen feed, or an away-from-LTP check against a stale quote, is exactly
        the kind of thing that only looks wrong afterwards.

        Any one healthy feed is enough — the router will serve quotes from it.
        """
        try:
            managers = list(self.router.ws_managers())
        except Exception:
            return False
        running = [m for m in managers if getattr(m, "should_run", False)]
        if not running:
            return False
        return all((not m.connected) or m.stale for m in running)

    def option_meta(self, underlying: str, expiry: str, strike: float,
                    opt_type: str) -> dict | None:
        """Contract facts straight from the instrument master: lot size and tick
        size. Returns None when the contract is not in a loaded master.

        These were previously never read. Lot size was inferred by the ORDER
        SPLITTER as ``qty / lots`` — i.e. taken from whatever the client sent —
        so a client that miscounted sent a wrong-sized order to the broker with
        nothing to catch it. Tick size was a hard-coded 0.05 everywhere.
        """
        underlying = (underlying or "").upper()
        opt_type = (opt_type or "").upper()
        exch = self._OPT_EXCH.get(underlying, "NFO")
        target = int(strike)
        for s in self.instrument_master:
            if s.get("name", "").upper() != underlying:
                continue
            if s.get("expiry") != expiry or s.get("exch_seg", "") != exch:
                continue
            if "OPT" not in s.get("instrumenttype", ""):
                continue
            if not s.get("symbol", "").endswith(opt_type):
                continue
            try:
                if int(float(s.get("strike", 0)) / 100) != target:
                    continue
            except (TypeError, ValueError):
                continue
            def _num(key: str) -> float | None:
                try:
                    value = float(s.get(key) or 0)
                except (TypeError, ValueError):
                    return None
                return value if value > 0 else None
            lot = _num("lotsize")
            # Angel publishes tick_size in paise (5 = ₹0.05).
            tick = _num("tick_size")
            return {
                "lotSize": int(lot) if lot else None,
                "tickSize": round(tick / 100.0, 4) if tick else None,
                "tradingsymbol": s.get("symbol", ""),
            }
        return None

    def contract_specs(self) -> dict[str, dict]:
        """Lot size + tick size per underlying, read from the instrument master.

        The renderer sizes every order as `lots x lot size` and used to take that
        number from a hard-coded table it shipped with. Exchanges revise lot
        sizes; when one drifted, the quantity the renderer sent stopped matching
        the contract and `rule_lot_size` rejected every order for that index —
        correctly, but with no way to fix it short of a new build. Serving the
        master's own figure removes the possibility.

        Empty until a master is loaded, which is honest: the renderer then keeps
        using its fallback, and so does the lot-size rule (which stands down when
        it does not know the real size), so the two cannot disagree.
        """
        specs: dict[str, dict] = {}
        for s in self.instrument_master:
            if "OPT" not in (s.get("instrumenttype") or ""):
                continue
            name = (s.get("name") or "").upper()
            if not name or name in specs or self._OPT_EXCH.get(name) is None:
                continue
            if s.get("exch_seg", "") != self._OPT_EXCH[name]:
                continue
            try:
                lot = int(float(s.get("lotsize") or 0))
                tick = float(s.get("tick_size") or 0)
            except (TypeError, ValueError):
                continue
            if lot <= 0:
                continue
            specs[name] = {"lotSize": lot,
                           # Angel publishes tick size in paise (5 = ₹0.05).
                           "tickSize": round(tick / 100.0, 4) if tick > 0 else None}
        return specs

    def resolve_option(self, underlying: str, expiry: str, strike: float,
                       opt_type: str) -> tuple[str, str | None, str]:
        """Resolve (tradingsymbol, token, exch_seg) for an index option from the
        instrument master. Shared by the live order router and the paper engine
        so both resolve tokens identically. token is None when not found.

        Expired contracts never resolve, even while the broker still lists them
        — see services.expiry for the rule.
        """
        underlying = (underlying or "").upper()
        opt_type = (opt_type or "").upper()
        exch = self._OPT_EXCH.get(underlying, "NFO")
        if expiry_filter.is_expired(expiry):
            return "", None, exch
        target = int(strike)
        for s in self.instrument_master:
            if s.get("name", "").upper() != underlying:
                continue
            if s.get("expiry") != expiry:
                continue
            if s.get("exch_seg", "") != exch:
                continue
            if "OPT" not in s.get("instrumenttype", ""):
                continue
            sym = s.get("symbol", "")
            if not sym.endswith(opt_type):
                continue
            try:
                if int(float(s.get("strike", 0)) / 100) != target:
                    continue
            except (TypeError, ValueError):
                continue
            token = str(s["token"]) if s.get("token") else None
            if token:
                # Bind before returning: the caller is about to trade this
                # contract, and its ticks can only be cached once the ingest
                # path can map the token back to a key.
                instruments.bind("angel", InstrumentKey.option(
                    underlying, expiry, target, opt_type), token)
            return sym, token, exch
        # Angel's master could not resolve it — either it is not loaded (no
        # Angel account connected) or the contract is not listed there. Fall
        # back to the shared registry so a Dhan-only session can still resolve,
        # and trade, the same contract. The Angel path above is untouched: this
        # only runs when it has already failed.
        key = InstrumentKey.option(underlying, expiry, target, opt_type)
        found = instruments.any_token(key)
        if found:
            _broker, tok = found
            return f"{underlying}{expiry}{target}{opt_type}", tok, exch
        return "", None, exch

    def as_key(self, ref: "str | InstrumentKey | None") -> InstrumentKey | None:
        """Accept either a canonical key or any broker's token.

        The quote accessors are called both by key-aware code and by callers
        holding a broker token they got from resolve_option (the paper engine
        stores one on every order and position). Resolution is NOT pinned to
        Angel: in a Dhan-only session that token is a Dhan security id, and
        assuming Angel silently returned no quote at all — so every order was
        rejected for having no market price. The broker currently serving
        option data is tried first.
        """
        if ref is None or isinstance(ref, InstrumentKey):
            return ref
        feed = self.router.primary_feed("option")
        return instruments.key_for_any(ref, prefer=feed.broker if feed else None)

    def get_option_quote(self, ref: "str | InstrumentKey") -> tuple[float | None, float | None, float | None]:
        """Return (ltp, bid, ask) for a contract — used by the paper execution
        engine. bid/ask are None when no depth has been seen."""
        key = self.as_key(ref)
        if key is None:
            return None, None, None
        with self._tick_lock:
            entry = self.option_ticks.get(key)
            if not entry:
                return None, None, None
            return entry.get("ltp"), entry.get("bid"), entry.get("ask")

    def add_option_tick_listener(self, fn: Any) -> None:
        """fn(key: InstrumentKey, ltp: float, volume: int|None) — called
        synchronously on every option tick so listeners can push updates
        immediately instead of polling. `key` is canonical, so a listener sees
        the same identity no matter which broker's feed carried the tick."""
        self.router.add_option_tick_listener(fn)

    def get_option_ltp(self, ref: "str | InstrumentKey") -> float | None:
        key = self.as_key(ref)
        if key is None:
            return None
        with self._tick_lock:
            entry = self.option_ticks.get(key)
            return entry["ltp"] if entry else None

    def get_option_tick(self, ref: "str | InstrumentKey") -> dict:
        key = self.as_key(ref)
        if key is None:
            return {}
        with self._tick_lock:
            return dict(self.option_ticks.get(key, {}))

    # ── market-data delegation ────────────────────────────────────────────
    # The feed and its tick cache now live in the router. These forward to it so
    # existing callers (option chain, paper engine, order router, /market-feed)
    # keep the exact surface they had — the extraction is invisible to them.

    @property
    def market_ws_connected(self) -> bool:
        return self.market_ws_should_run and any(f.connected for f in self.router.feeds())

    @property
    def option_feed_connected(self) -> bool:
        """Whether SOME feed is currently carrying option data. The chain's
        resubscribe watchdog asks this rather than inspecting Angel's socket,
        so it behaves the same whichever broker is serving."""
        feed = self.router.primary_feed("option")
        return bool(feed is not None and feed.connected)

    @property
    def _tick_lock(self):
        return self.router.tick_lock

    @property
    def option_ticks(self) -> dict:
        return self.router.option_ticks

    @property
    def index_ltp(self) -> dict:
        return self.router.index_ltp

    @property
    def unmapped_ticks(self) -> int:
        return self.router.unmapped_ticks

    @property
    def last_unmapped_token(self) -> str | None:
        return self.router.last_unmapped_token

    @property
    def _market_feed(self):
        """The feed owning the Angel market stream, or None. Several shims below
        need it and it is only ever one object today; when a second feed lands,
        callers should ask the router for the primary of a capability instead."""
        if self._market_account is None:
            return None
        return self.router.feed_for(self._market_account)

    @property
    def market_ws(self):
        """The reconnecting-socket wrapper for the market feed.

        Callers read `.connected` and `.status()` off this. Returns a dormant
        stand-in when no feed is attached, so `manager.market_ws.connected` is
        False rather than raising on a not-yet-connected sidecar — which is what
        it did before a feed existed, too.
        """
        feed = self._market_feed
        return feed.ws if feed is not None else _NO_FEED

    @property
    def market_ws_should_run(self) -> bool:
        feed = self._market_feed
        return bool(feed is not None and getattr(feed, "should_run", False))

    @property
    def market_token_map(self) -> dict:
        feed = self._market_feed
        return getattr(feed, "market_token_map", {}) if feed else {}

    @property
    def option_sub_state(self) -> str:
        feed = self._market_feed
        return getattr(feed, "option_sub_state", "none") if feed else "none"

    @property
    def option_sub_ts(self) -> float:
        feed = self._market_feed
        return getattr(feed, "option_sub_ts", 0.0) if feed else 0.0

    @property
    def option_sub_tokens(self) -> int:
        feed = self._market_feed
        return getattr(feed, "option_sub_tokens", 0) if feed else 0

    def ensure_market_stream(self) -> None:
        """Reconcile the market feed. Kept as the name the connect paths and
        tests already use; `feed.start()` is itself idempotent."""
        if self._market_account:
            self._start_feed(self._market_account, self._broker.get(self._market_account, "angel"))

    def start_market_ltp_stream(self) -> None:
        self.ensure_market_stream()

    def subscribe_option_keys(self, keys: set) -> None:
        """Broker-neutral option subscription: hand the router the canonical
        contract set and let the serving feed translate. Feeds that subscribe
        by broker token (Angel) keep using subscribe_option_tokens."""
        self.router.subscribe_option_keys(keys)

    def subscribe_option_tokens(self, token_list: list[dict]) -> None:
        """Route an option subscription to the feed serving option data.

        A no-op when nothing is streaming yet: the option chain calls this on
        every rebuild, and before a broker connects there is simply no feed to
        carry it. The chain retries on its next cycle.
        """
        feed = self.router.primary_feed("option") or self._market_feed
        if feed is None or not hasattr(feed, "subscribe_options"):
            return
        feed.subscribe_options(token_list)


class _DormantFeed:
    """Stands in for `market_ws` before any feed is attached, so status reads
    are safe on a fresh sidecar instead of raising AttributeError."""
    name = "market-feed"
    should_run = False
    connected = False
    stale = False
    last_error = None
    last_error_ts = 0.0
    consecutive_failures = 0
    last_tick_ts = 0.0
    last_connected_ts = 0.0

    def status(self) -> dict:
        return {"name": self.name, "shouldRun": False, "connected": False,
                "stale": False, "consecutiveFailures": 0, "lastError": None,
                "lastTickTs": 0.0, "lastConnectedTs": 0.0}

    def reconnect(self) -> None:
        pass

    def live_socket(self):
        return None


_NO_FEED = _DormantFeed()


manager = BrokerManager()
