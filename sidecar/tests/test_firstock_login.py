"""Regression tests for Firstock authentication and session lifecycle.

    python sidecar/tests/test_firstock_login.py

Only the network boundary is stubbed (`firstock_client._post`) — the real
client, the real BrokerManager and the real SessionManager classification run
throughout, so what is verified is the wiring that actually ships.

Same style as the other sidecar suites: no pytest, so it runs on the packaged
runtime.
"""
import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:                   # pragma: no cover - non-reconfigurable
        pass

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-fslogin-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

import diagnostics                                                    # noqa: E402
from services.feeds import firstock_client                            # noqa: E402
from services.feeds.firstock_client import (                          # noqa: E402
    FirstockClient, FirstockError, hash_password, redact)
from services.broker_manager import (                                 # noqa: E402
    CONNECTED, DOWN, SESSION_EXPIRED, LABEL, SUPPORTED, manager)
from services.reliability.errors import classify_error                # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


def refused(call):
    """True when `call` raises a FirstockError rather than reaching the wire."""
    try:
        call()
    except firstock_client.FirstockError:
        return True
    return False


# ── secrets used throughout; none of them may ever reach a log ──────────────
SECRETS = {
    "password": "hunter2-super-secret",
    "apiKey": "ak_live_9f3c1d77",
    "vendorCode": "VENDOR_XYZ",
    "jkey": "b6339fa5006155c2ae3611892cd80e0b8ae6cbe0dee0",
    "totpSecret": "JBSWY3DPEHPK3PXP",
}

CREDS = {
    "userId": "AB1234",
    "password": SECRETS["password"],
    "vendorCode": SECRETS["vendorCode"],
    "apiKey": SECRETS["apiKey"],
    # Not an optional extra: Firstock rejects every login that arrives without
    # a TOTP, so a complete credential set includes the secret to generate one.
    "totpSecret": SECRETS["totpSecret"],
}


# ── network stub ────────────────────────────────────────────────────────────
class Wire:
    """Replaces firstock_client._post. Records every call, returns scripted
    responses, and never touches the network."""

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.raises = {}

    def install(self):
        firstock_client._post = self          # type: ignore[assignment]
        return self

    def __call__(self, url, payload, timeout):
        self.calls.append({"url": url, "payload": dict(payload), "timeout": timeout})
        key = url.rsplit("/", 1)[-1]
        if key in self.raises:
            raise self.raises[key]
        return self.responses.get(key, {})

    def payload_for(self, key):
        for call in self.calls:
            if call["url"].endswith(key):
                return call["payload"]
        return None

    def called(self, key):
        return any(c["url"].endswith(key) for c in self.calls)


def ok_login(token=None):
    return {"actid": "AB1234", "userName": "DEMO USER",
            "susertoken": token or SECRETS["jkey"], "email": "d@example.com"}


def ok_details():
    return {"actid": "AB1234", "userName": "DEMO USER",
            "exchange": ["NSE", "NFO", "BSE", "BFO"],
            "orarr": ["MKT", "LMT", "SL-LMT", "SL-MKT"]}


def wire_ok():
    w = Wire().install()
    w.responses = {"login": ok_login(), "userDetails": ok_details(),
                   "logout": {}, "indexList": {}}
    return w


# ── log capture, so the security assertions are real ────────────────────────
class LogSpy:
    def __init__(self):
        self.lines = []
        self._emit = diagnostics.emit
        self._event = diagnostics.event
        self._exception = diagnostics.exception

    def install(self):
        def emit(category, level, message, **kw):
            self.lines.append(f"{category}|{level}|{message}|{kw}")

        def event(category, action, status, **kw):
            self.lines.append(f"{category}|{action}|{status}|{kw}")

        def exception(category, message, **kw):
            self.lines.append(f"{category}|exc|{message}|{kw}")

        diagnostics.emit, diagnostics.event, diagnostics.exception = emit, event, exception
        return self

    def restore(self):
        diagnostics.emit = self._emit
        diagnostics.event = self._event
        diagnostics.exception = self._exception

    def text(self):
        return "\n".join(self.lines)


# ════════════════════════════════════════════════════════════════════════════
def scenario_password_hashing():
    section("[1] The password is hashed before it ever leaves Charticks")
    digest = hash_password("hunter2")
    check("plain password becomes a 64-char digest",
          len(digest) == 64 and digest.isalnum(), digest)
    check("hashing is stable", hash_password("hunter2") == digest)
    check("a different password differs", hash_password("hunter3") != digest)
    section("       ...and an already-hashed value passes through untouched")
    check("existing digest not double-hashed",
          hash_password(digest) == digest, hash_password(digest))
    check("uppercase digest is normalised",
          hash_password(digest.upper()) == digest)


def scenario_redaction():
    section("[2] Session tokens are scrubbed from anything log-bound")
    url = (f"wss://socket.firstock.in/V2/ws?userId=AB1234&jKey={SECRETS['jkey']}"
           f"&source=developer-api")
    out = redact(f"Handshake failed for {url}")
    check("jKey removed from a websocket URL", SECRETS["jkey"] not in out, out)
    check("the rest of the URL survives", "socket.firstock.in" in out, out)
    body = f'{{"userId": "AB1234", "jKey": "{SECRETS["jkey"]}"}}'
    check("jKey removed from a JSON body",
          SECRETS["jkey"] not in redact(body), redact(body))
    resp = f'{{"susertoken": "{SECRETS["jkey"]}"}}'
    check("susertoken removed from a login response",
          SECRETS["jkey"] not in redact(resp), redact(resp))


def scenario_login_success():
    section("[3] A successful login produces a validated session")
    w = wire_ok()
    client = firstock_client.login("AB1234", SECRETS["password"],
                                   SECRETS["vendorCode"], SECRETS["apiKey"],
                                   totp_secret=SECRETS["totpSecret"])
    check("session token captured", client.jkey == SECRETS["jkey"], client.jkey)
    check("account id captured", client.actid == "AB1234", client.actid)
    payload = w.payload_for("login")
    check("password sent hashed, never in the clear",
          payload["password"] == hash_password(SECRETS["password"])
          and SECRETS["password"] not in str(payload), payload["password"][:12])
    check("TOTP always sent — Firstock rejects a login without one",
          len(payload.get("TOTP", "")) == 6, sorted(payload))
    check("repr never leaks the token", SECRETS["jkey"] not in repr(client),
          repr(client))

    client.validate()
    check("validate() calls userDetails", w.called("userDetails"), w.calls)
    check("enabled exchanges recorded",
          client.exchanges == ["NSE", "NFO", "BSE", "BFO"], client.exchanges)
    check("order types recorded", "SL-LMT" in client.order_types, client.order_types)
    check("no missing option venues", client.missing_option_exchanges == [],
          client.missing_option_exchanges)


def scenario_totp():
    section("[4] A TOTP secret becomes a fresh 6-digit code")
    w = wire_ok()
    firstock_client.login("AB1234", "pw", "VC", "AK",
                          totp_secret=SECRETS["totpSecret"])
    payload = w.payload_for("login")
    code = payload.get("TOTP", "")
    check("TOTP included", bool(code), sorted(payload))
    check("it is a 6-digit code, not the secret",
          len(code) == 6 and code.isdigit(), code)
    check("the shared secret is never sent",
          SECRETS["totpSecret"] not in str(payload), payload)

    section("       ...and no secret at all fails before any network call")
    # Firstock validates TOTP ahead of every credential, so a login without one
    # returns "MISSING_FIELD: TOTP cannot be empty" however right the rest is.
    # Refusing locally is what turns that into a message naming the real fix.
    w2 = wire_ok()
    check("no secret is refused",
          refused(lambda: firstock_client.login("AB1234", "pw", "VC", "AK")))
    check("a blank secret is refused too, not sent as an empty field",
          refused(lambda: firstock_client.login("AB1234", "pw", "VC", "AK",
                                                totp_secret="   ")))
    check("nothing was sent to Firstock", not w2.called("login"), w2.calls)
    try:
        firstock_client.login("AB1234", "pw", "VC", "AK")
    except firstock_client.FirstockError as exc:
        check("the error names the TOTP secret, not a generic failure",
              "TOTP secret" in str(exc), exc)


def scenario_login_no_token():
    section("[5] A 200 with no token is a FAILURE, not a session")
    w = wire_ok()
    w.responses["login"] = {"actid": "AB1234", "userName": "DEMO"}
    try:
        firstock_client.login("AB1234", "pw", "VC", "AK", SECRETS["totpSecret"])
        check("refuses a tokenless success", False, "no exception")
    except FirstockError as e:
        check("refuses a tokenless success", e.name == "NO_SESSION_TOKEN", e.name)
        check("says the session cannot be used", "cannot be used" in str(e), str(e))


def scenario_login_rejected():
    section("[6] Firstock's own error reaches the user intact")
    w = wire_ok()
    w.raises["login"] = FirstockError("BAD_REQUEST", "Invalid Credentials",
                                      code="400", field="password")
    try:
        firstock_client.login("AB1234", "pw", "VC", "AK", SECRETS["totpSecret"])
        check("raises", False, "no exception")
    except FirstockError as e:
        check("carries Firstock's message", "Invalid Credentials" in str(e), str(e))
        check("carries the machine-readable name", e.name == "BAD_REQUEST", e.name)
        check("names the rejected field", e.field == "password", e.field)


def scenario_session_error_classification():
    section("[7] A dead session is classified, so recovery runs")
    dead = FirstockError("INVALID_JKEY", "jKey parameter is invalid", code="401")
    check("INVALID_JKEY → session_expired",
          classify_error(dead) == "session_expired", classify_error(dead))
    check("the client flags it too", dead.is_session_error)
    # orderBook/positionBook/limit/orderMargin answer a dead session with
    # INVALID_CREDENTIALS rather than userDetails' INVALID_JKEY. Both must
    # classify, or position sync polls a dead session instead of recovering it.
    stale = FirstockError("INVALID_CREDENTIALS",
                          "Invalid credentials or session expired", code="401")
    check("INVALID_CREDENTIALS → session_expired",
          classify_error(stale) == "session_expired", classify_error(stale))
    check("the client flags that one too", stale.is_session_error)
    ws_reject = {"status": "failed", "message": "unauthenticated"}
    check("websocket rejection → session_expired",
          classify_error(ws_reject) == "session_expired", classify_error(ws_reject))
    section("       ...but our own bugs and network blips are NOT")
    ours = FirstockError("BAD_REQUEST", "jKey is required")
    check("a missing-field bug does not trigger a re-login",
          classify_error(ours) != "session_expired", classify_error(ours))
    net = ConnectionError("Firstock request failed: ReadTimeout: timed out")
    check("a timeout classifies as network",
          classify_error(net) == "network", classify_error(net))


def scenario_missing_entitlement():
    section("[8] A missing option venue is reported, not guessed")
    w = wire_ok()
    w.responses["userDetails"] = {"actid": "AB1234", "exchange": ["NSE", "BSE"]}
    client = firstock_client.login("AB1234", "pw", "VC", "AK", SECRETS["totpSecret"])
    client.validate()
    check("NFO and BFO reported absent",
          client.missing_option_exchanges == ["NFO", "BFO"],
          client.missing_option_exchanges)
    section("       ...and an unvalidated client invents nothing")
    fresh = FirstockClient("AB1234", "tok")
    check("no claim before validate()", fresh.missing_option_exchanges == [],
          fresh.missing_option_exchanges)


def scenario_logout_never_raises():
    section("[9] Logout clears the token and never blocks a disconnect")
    w = wire_ok()
    client = firstock_client.login("AB1234", "pw", "VC", "AK", SECRETS["totpSecret"])
    client.close_session()
    check("logout called", w.called("logout"), w.calls)
    check("token cleared locally", client.jkey == "", client.jkey)
    section("       ...even when Firstock is unreachable")
    w2 = wire_ok()
    w2.raises["logout"] = ConnectionError("network down")
    client2 = firstock_client.login("AB1234", "pw", "VC", "AK", SECRETS["totpSecret"])
    try:
        client2.close_session()
        check("a failed logout does not raise", True)
    except Exception as e:
        check("a failed logout does not raise", False, e)
    check("token cleared regardless", client2.jkey == "", client2.jkey)


# ── BrokerManager integration ───────────────────────────────────────────────
def with_manager(fn):
    """Run `fn` with the feed start stubbed, returning the recorded starts."""
    started = []
    original = manager._start_feed
    manager._start_feed = lambda aid, broker, session_tokens=None: started.append(
        {"account": aid, "broker": broker, "tokens": session_tokens})
    try:
        fn(started)
    finally:
        manager._start_feed = original


def scenario_broker_registration():
    section("[10] Firstock is a registered broker")
    check("in SUPPORTED", "firstock" in SUPPORTED, SUPPORTED)
    check("has a display label", LABEL.get("firstock") == "Firstock", LABEL)
    check("existing brokers untouched",
          all(b in SUPPORTED for b in ("angel", "kotak", "dhan", "icici")), SUPPORTED)


def scenario_connect_success():
    section("[11] connect() authenticates, validates, then starts the feed")

    def body(started):
        wire_ok()
        res = manager.connect("fs-1", "firstock", CREDS)
        check("connect reports ok", res.get("ok") is True, res)
        check("health is connected",
              manager.status_map()["fs-1"]["health"] == CONNECTED,
              manager.status_map().get("fs-1"))
        check("session stored", manager.live_session("fs-1") is not None)
        broker, session = manager.live_session("fs-1")
        check("session is a FirstockClient", isinstance(session, FirstockClient), session)
        check("feed started once", len(started) == 1, started)
        check("feed received the shared client",
              started and started[0]["tokens"]["client"] is session, started)

    with_manager(body)


def scenario_connect_missing_credentials():
    section("[12] Missing credentials fail before any network call")

    def body(started):
        w = wire_ok()
        res = manager.connect("fs-2", "firstock", {"userId": "AB1234"})
        check("connect refuses", res.get("ok") is False, res)
        check("names every missing field",
              all(f in res["error"] for f in ("password", "vendorCode", "apiKey",
                                              "totpSecret")),
              res.get("error"))
        check("no login attempted", not w.called("login"), w.calls)
        check("health is down", manager.status_map()["fs-2"]["health"] == DOWN,
              manager.status_map().get("fs-2"))
        check("feed NOT started", not started, started)

    with_manager(body)


def scenario_connect_rejected():
    section("[13] Rejected credentials leave the broker disconnected")

    def body(started):
        w = wire_ok()
        w.raises["login"] = FirstockError("BAD_REQUEST", "Invalid Credentials",
                                          code="400")
        res = manager.connect("fs-3", "firstock", CREDS)
        check("connect fails", res.get("ok") is False, res)
        check("health is session_expired",
              manager.status_map()["fs-3"]["health"] == SESSION_EXPIRED,
              manager.status_map().get("fs-3"))
        check("the detail carries Firstock's own words",
              "Invalid Credentials" in manager.status_map()["fs-3"]["detail"],
              manager.status_map()["fs-3"]["detail"])
        check("the detail carries the credential checklist",
              "Check, in this order" in manager.status_map()["fs-3"]["detail"])
        check("feed NOT started", not started, started)
        check("no session stored", manager.live_session("fs-3") is None)

    with_manager(body)


def scenario_validation_failure_is_fatal():
    section("[14] A token that does not work is not a connection")

    def body(started):
        w = wire_ok()
        w.raises["userDetails"] = FirstockError("INVALID_JKEY",
                                                "jKey parameter is invalid", code="401")
        res = manager.connect("fs-4", "firstock", CREDS)
        check("connect fails despite a 200 on login", res.get("ok") is False, res)
        check("health is not connected",
              manager.status_map()["fs-4"]["health"] != CONNECTED,
              manager.status_map().get("fs-4"))
        check("feed NOT started", not started, started)

    with_manager(body)


def scenario_network_vs_auth():
    section("[15] Unreachable and rejected are different states")

    def body(started):
        w = wire_ok()
        w.raises["login"] = ConnectionError(
            "Firstock request to https://api.firstock.in/V1/login failed: "
            "ConnectTimeout: timed out")
        res = manager.connect("fs-5", "firstock", CREDS)
        check("connect fails", res.get("ok") is False, res)
        check("health is DOWN, not session_expired",
              manager.status_map()["fs-5"]["health"] == DOWN,
              manager.status_map().get("fs-5"))
        check("the message blames the network, not the credentials",
              "Could not reach Firstock" in res["error"], res.get("error"))
        check("it does NOT show the credential checklist",
              "Check, in this order" not in res["error"], res.get("error"))

    with_manager(body)


def scenario_reauthentication():
    section("[16] An expired session re-authenticates unattended")

    def body(started):
        wire_ok()
        manager.connect("fs-6", "firstock", CREDS)
        started.clear()
        w = wire_ok()
        w.responses["login"] = ok_login(token="fresh-token-999")
        res = manager.reauthenticate("fs-6")
        check("re-auth succeeds with cached credentials", res.get("ok") is True, res)
        check("no manual login required (not permanent)",
              not res.get("permanent"), res)
        _broker, session = manager.live_session("fs-6")
        check("the new token replaced the old one",
              session.jkey == "fresh-token-999", session.jkey)
        check("the feed was handed the refreshed session",
              started and started[0]["tokens"]["client"].jkey == "fresh-token-999",
              started)

    with_manager(body)


def scenario_reauth_without_credentials():
    section("[17] Re-auth without cached credentials fails cleanly")
    res = manager.reauthenticate("never-connected")
    check("refuses rather than crashing", res.get("ok") is False, res)
    check("says why", "credential" in res.get("error", "").lower(), res)


def scenario_disconnect():
    section("[18] disconnect() logs out at Firstock and clears local state")

    def body(started):
        w = wire_ok()
        manager.connect("fs-7", "firstock", CREDS)
        _broker, session = manager.live_session("fs-7")
        res = manager.disconnect("fs-7")
        check("disconnect ok", res.get("ok") is True, res)
        check("logout sent to Firstock", w.called("logout"), w.calls)
        check("token cleared", session.jkey == "", session.jkey)
        check("session removed", manager.live_session("fs-7") is None)
        check("health is down",
              manager.status_map()["fs-7"]["health"] == DOWN,
              manager.status_map().get("fs-7"))

    with_manager(body)


def scenario_disconnect_when_offline():
    section("[19] Disconnect works even when Firstock is unreachable")

    def body(started):
        w = wire_ok()
        manager.connect("fs-8", "firstock", CREDS)
        w.raises["logout"] = ConnectionError("network down")
        res = manager.disconnect("fs-8")
        check("still disconnects locally", res.get("ok") is True, res)
        check("session removed", manager.live_session("fs-8") is None)

    with_manager(body)


def scenario_other_brokers_unaffected():
    section("[20] No existing broker's disconnect behaviour changed")

    class LegacySession:
        """Stands in for an SDK session with a logout() of its own — it must
        NOT be called, because only `close_session` opts in."""
        def __init__(self):
            self.logout_called = False

        def logout(self):
            self.logout_called = True

    def body(started):
        wire_ok()
        session = LegacySession()
        with manager._lock:
            manager._sessions["legacy-1"] = session
            manager._broker["legacy-1"] = "kotak"
            manager._health["legacy-1"] = CONNECTED
        manager.disconnect("legacy-1")
        check("a session without close_session is untouched",
              session.logout_called is False, session.logout_called)
        check("it is still removed", manager.live_session("legacy-1") is None)

    with_manager(body)


def scenario_no_secrets_in_logs():
    section("[21] No credential or token ever reaches a log")

    def body(started):
        spy = LogSpy().install()
        try:
            wire_ok()
            manager.connect("fs-9", "firstock", CREDS)
            manager.disconnect("fs-9")
            w = wire_ok()
            w.raises["login"] = FirstockError("BAD_REQUEST", "Invalid Credentials")
            manager.connect("fs-10", "firstock", CREDS)
        finally:
            spy.restore()
        text = spy.text()
        for name, value in SECRETS.items():
            check(f"{name} absent from every log line", value not in text,
                  [ln for ln in spy.lines if value in ln][:1])
        check("something WAS logged (the test is meaningful)",
              len(spy.lines) > 3, len(spy.lines))
        check("field NAMES are logged, so a missing field is diagnosable",
              "credentialFields" in text)

    with_manager(body)


def scenario_health_states():
    section("[22] Every UI state the Brokers page renders is reachable")

    def body(started):
        wire_ok()
        manager.connect("fs-11", "firstock", CREDS)
        check("connected", manager.status_map()["fs-11"]["health"] == CONNECTED)
        manager.disconnect("fs-11")
        check("disconnected", manager.status_map()["fs-11"]["health"] == DOWN)
        w = wire_ok()
        w.raises["login"] = FirstockError("BAD_REQUEST", "Invalid Credentials")
        manager.connect("fs-12", "firstock", CREDS)
        check("authentication failed → session_expired",
              manager.status_map()["fs-12"]["health"] == SESSION_EXPIRED)
        events = manager.snapshot_events()
        fs = [e for e in events if e.get("account") == "fs-12"]
        check("status is replayed to a late-joining UI", len(fs) == 1, fs)
        check("the event names the broker", fs and fs[0]["broker"] == "firstock", fs)

    with_manager(body)


def scenario_execute_selection():
    section("[23] Firstock can be opted in to live execution")

    def body(started):
        wire_ok()
        manager.connect("fs-13", "firstock", CREDS)
        manager.set_execution_accounts(["fs-13"])
        check("in the execution set", "fs-13" in manager.execution_accounts())
        sessions = manager.execution_sessions()
        check("connected + opted in → routable",
              any(aid == "fs-13" and b == "firstock" for aid, b, _s in sessions),
              sessions)
        manager.set_execution_accounts([])
        check("opting out removes it",
              not any(aid == "fs-13" for aid, _b, _s in manager.execution_sessions()))

    with_manager(body)


if __name__ == "__main__":
    for fn in (scenario_password_hashing, scenario_redaction,
               scenario_login_success, scenario_totp, scenario_login_no_token,
               scenario_login_rejected, scenario_session_error_classification,
               scenario_missing_entitlement, scenario_logout_never_raises,
               scenario_broker_registration, scenario_connect_success,
               scenario_connect_missing_credentials, scenario_connect_rejected,
               scenario_validation_failure_is_fatal, scenario_network_vs_auth,
               scenario_reauthentication, scenario_reauth_without_credentials,
               scenario_disconnect, scenario_disconnect_when_offline,
               scenario_other_brokers_unaffected, scenario_no_secrets_in_logs,
               scenario_health_states, scenario_execute_selection):
        fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
