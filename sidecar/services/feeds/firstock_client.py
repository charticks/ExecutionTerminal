"""Firstock REST client — authentication and the session object itself.

Firstock has no vendor SDK worth taking on: every call is a POST of flat JSON
carrying ``userId`` and ``jKey``, with no headers, no signing and no hidden
state. That is a large part of why this broker is cheap to integrate, and
wrapping it in a third-party client would import the class of problem the Kotak
and Breeze SDKs already cost us (state stashed inside the SDK, errors rendered
as unreadable lists, a bare ``import config`` resolved off sys.path).

The ``FirstockClient`` instance IS the session object BrokerManager stores per
account — the same role ``SmartConnect`` plays for Angel and ``dhanhq`` for Dhan
— so the order router, positions reader and margin checker will all receive it
unchanged in later phases.

Lives beside the feed because every Firstock wire detail belongs in one place:
the feed's index-list lookup and the login sequence talk to the same API with
the same error shape, and duplicating that in broker_manager would mean two
places to fix when Firstock renames a field. `broker_manager._connect_icici`
already imports from ``services.feeds`` for the same reason.

Secrets
-------
Nothing in this module ever logs, stringifies or re-raises a credential. The
password is hashed before it leaves the caller, the request body is never
included in an error, and `redact()` scrubs the session token out of any text
heading for a log file — the websocket URL carries ``jKey`` as a query
parameter, so a library exception mentioning that URL would otherwise write a
live session token into websocket.log.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

import diagnostics

BASE_URL = "https://api.firstock.in/V1"

LOGIN_URL = f"{BASE_URL}/login"
LOGOUT_URL = f"{BASE_URL}/logout"
USER_DETAILS_URL = f"{BASE_URL}/userDetails"
INDEX_LIST_URL = f"{BASE_URL}/indexList"
PLACE_ORDER_URL = f"{BASE_URL}/placeOrder"
MODIFY_ORDER_URL = f"{BASE_URL}/modifyOrder"
CANCEL_ORDER_URL = f"{BASE_URL}/cancelOrder"
ORDER_BOOK_URL = f"{BASE_URL}/orderBook"
TRADE_BOOK_URL = f"{BASE_URL}/tradeBook"
POSITION_BOOK_URL = f"{BASE_URL}/positionBook"
HOLDINGS_URL = f"{BASE_URL}/holdings"
LIMITS_URL = f"{BASE_URL}/limit"
ORDER_MARGIN_URL = f"{BASE_URL}/orderMargin"

# Network timeouts. Login is given longer than the rest: it is the one call a
# user is actively waiting on, and failing it early on a slow morning turns a
# working account into a support question.
LOGIN_TIMEOUT = 45
CALL_TIMEOUT = 30

# ── Charticks' internal model -> Firstock's wire vocabulary ────────────────
#
# This is the whole point of an adapter. The order engine, risk engine, live
# manager and position book all speak Charticks' model — NRML / MIS / CNC,
# MARKET / LIMIT / SL / SL-M, BUY / SELL — and NOTHING above this module is
# allowed to know that Firstock spells them M / I / C and MKT / LMT / SL-LMT /
# SL-MKT. A future broker translates the same canonical values its own way, in
# its own adapter, and the engine is untouched either way.
#
# Product codes confirmed against Firstock's documentation:
#   C = Cash & Carry (equity delivery only)   <- Charticks CNC
#   I = Intraday                              <- Charticks MIS
#   M = Regular / carry-forward margin        <- Charticks NRML
PRODUCT = {"NRML": "M", "MIS": "I", "CNC": "C"}

# Charticks' engine emits only MARKET and LIMIT today (see
# risk_engine.VALID_ORDER_TYPES); SL and SL-M are mapped so the adapter is
# complete the day that changes, and so a stop order arriving from anywhere
# cannot be silently downgraded to a market order.
ORDER_TYPE = {"MARKET": "MKT", "LIMIT": "LMT", "SL": "SL-LMT", "SL-M": "SL-MKT"}

# Order types that carry a trigger rather than (or as well as) a limit price.
TRIGGERED = ("SL", "SL-M")

# Order types Firstock requires market protection on.
NEEDS_PROTECTION = ("MARKET", "SL-M")

SIDE = {"BUY": "B", "SELL": "S"}

VALIDITY = {"DAY": "DAY", "IOC": "IOC"}

# Market protection: how far from the last trade Firstock may fill a market
# order, as a percentage. Mandatory on MKT and SL-MKT.
#
# The asymmetry matters. Too WIDE risks a poor fill on an illiquid strike — but
# Charticks already guards entries with its own away-from-LTP rule. Too TIGHT
# gets the order REJECTED, and the orders Charticks sends as MARKET are its
# automated exits: a rejected stop-loss leaves a position open past its stop,
# which is the failure that actually costs money. So this errs wide, and the
# figure is stated once here rather than scattered through the placer.
MARKET_PROTECTION_PCT = "10"

# A SHA-256 digest as Firstock wants it: 64 lowercase hex characters.
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")

# Anything that looks like a session token in free text. Both the JSON field and
# the websocket query parameter spell it `jKey`; `susertoken` is the same value
# under the name the login response uses.
_SECRET_PATTERNS = (
    re.compile(r"(jKey=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(\"?jKey\"?\s*[:=]\s*\"?)[^,&\s\"'}]+", re.IGNORECASE),
    re.compile(r"(\"?susertoken\"?\s*[:=]\s*\"?)[^,&\s\"'}]+", re.IGNORECASE),
)


def redact(text: Any) -> str:
    """`text` with any session token replaced by ``…``.

    Applied to everything that can reach a log file from a path where a URL or
    a request body might be quoted back at us. Cheap, and the alternative is a
    live token sitting in a file we ask testers to email.
    """
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(r"\1…", out)
    return out


def hash_password(raw: str) -> str:
    """Firstock's login takes a SHA-256 hex digest, not the password itself.

    A value that is ALREADY a 64-character hex digest is passed through
    unchanged. Firstock's own documentation points users at a SHA-256 converter
    tool, so a hash is exactly what many will have to hand — and hashing it a
    second time produces a login failure whose message says only that the
    password was wrong, which is unfalsifiable from the user's side.

    The residual risk is a genuine password that happens to be 64 hex
    characters. That fails closed (a rejected login the user can correct) rather
    than open, and is vanishingly unlikely next to the case it protects.
    """
    text = (raw or "").strip()
    if _SHA256_HEX.match(text):
        return text.lower()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Firstock rejects a caller whose source IP is not on the API app's allow-list
# BEFORE it looks at any credential. The wording is Firstock's own; matched on
# the message because `name` for this one is the generic UNAUTHORIZED it also
# uses for a dead session.
_IP_REJECTED = "invalid ip address"

# Where to ask what public IP this machine presents. Two providers, because the
# answer is worthless if it is wrong and a single endpoint can be blocked or
# down. Only ever called on an IP rejection, never on a healthy login.
_IP_ECHO_URLS = ("https://api.ipify.org", "https://checkip.amazonaws.com")
_IP_ECHO_TIMEOUT = 6


def public_ip() -> str:
    """The public IPv4 this machine presents, or "" if it cannot be determined.

    Firstock's allow-list is checked against this address, and it is NOT
    something the user can reliably look up: a browser may egress through a
    different route than this process, and the address shown by a "what is my
    IP" page is then not the one Firstock sees. Reporting the address THIS
    process actually presents is the whole point — it is the value that has to
    be pasted into the API portal.

    Never raises. A diagnosis is a bonus on top of an error that is already
    being reported; it must not become a second failure.
    """
    import requests

    for url in _IP_ECHO_URLS:
        try:
            got = requests.get(url, timeout=_IP_ECHO_TIMEOUT).text.strip()
            # Cheap sanity check — a captive portal will happily return HTML.
            if got and len(got) <= 45 and got.count(".") == 3:
                return got
        except Exception:
            continue
    return ""


class FirstockError(Exception):
    """An error Firstock itself reported, carrying its own code and wording.

    ``str()`` renders as ``NAME: message`` so that reliability.errors can key
    off Firstock's own error name — the same way it keys off Angel's AG8001 —
    rather than pattern-matching prose that may be reworded.
    """

    def __init__(self, name: str, message: str, code: str = "",
                 field: str = "") -> None:
        self.name = name or "FIRSTOCK_ERROR"
        self.detail = message or "Firstock reported an error with no message"
        self.code = str(code or "")
        self.field = field or ""
        super().__init__(f"{self.name}: {self.detail}")

    @property
    def is_ip_rejection(self) -> bool:
        """True when Firstock refused the SOURCE ADDRESS, not the credentials.

        Kept distinct because the remedy shares nothing with a credential
        failure: no amount of re-checking the user id, password, vendor code,
        API key or TOTP secret can fix it, and telling a user to re-check them
        sends them round a loop that cannot terminate.
        """
        return _IP_REJECTED in self.detail.lower()

    @property
    def is_session_error(self) -> bool:
        # Firstock spells a dead session three ways depending on the endpoint:
        # userDetails and logout answer INVALID_JKEY, placeOrder/modify/cancel
        # answer UNAUTHORIZED, and the book/limit/margin reads answer
        # INVALID_CREDENTIALS ("Invalid credentials or session expired").
        # An IP rejection also arrives as UNAUTHORIZED, but re-authenticating
        # cannot fix it — the next attempt comes from the same address. Treating
        # it as a dead session would put session recovery into a retry loop
        # against a wall.
        if self.is_ip_rejection:
            return False
        return self.name.upper() in ("INVALID_JKEY", "UNAUTHORIZED",
                                     "INVALID_CREDENTIALS", "SESSION_EXPIRED")


def _error_from(payload: Any) -> FirstockError:
    """Turn a ``{"status": "failed", ...}`` body into a FirstockError.

    Firstock nests the useful half under ``error`` as ``{field, message}`` and
    puts a machine-readable code in ``name``. Both are kept: the name is what
    classification matches on, the message is what the user reads.
    """
    if not isinstance(payload, dict):
        return FirstockError("FIRSTOCK_ERROR", f"unexpected response {payload!r}")
    err = payload.get("error")
    message = field = ""
    if isinstance(err, dict):
        message = str(err.get("message") or "")
        field = str(err.get("field") or "")
    elif err:
        message = str(err)
    if not message:
        message = str(payload.get("message") or "")
    return FirstockError(str(payload.get("name") or ""), message,
                         str(payload.get("code") or ""), field)


def _post(url: str, payload: dict, timeout: int) -> dict:
    """POST `payload` and return the ``data`` block, or raise.

    Deliberately never includes the request body in an exception: it carries the
    password hash on the login call and the session token on every other one.
    """
    import requests

    try:
        response = requests.post(url, json=payload, timeout=timeout,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Charticks/1.0"})
    except Exception as exc:
        # Network failures keep their own wording so reliability.errors can
        # classify them as transient rather than as a dead session.
        raise ConnectionError(f"Firstock request to {url} failed: "
                              f"{type(exc).__name__}: {redact(exc)}") from exc

    try:
        body = response.json()
    except ValueError:
        diagnostics.emit("broker", "error",
                         f"[firstock] {url.rsplit('/', 1)[-1]} → HTTP "
                         f"{response.status_code}, non-JSON body: "
                         f"{redact(response.text)[:300]}")
        raise FirstockError(
            "BAD_RESPONSE",
            f"Firstock returned HTTP {response.status_code} with a body that is "
            f"not JSON: {redact(response.text)[:200]}") from None

    if str(body.get("status", "")).lower() != "success":
        # The COMPLETE response, verbatim, at the point of failure. Firstock
        # names the rejected field and its own error code, and collapsing that
        # into a one-line summary is how "Invalid IP Address" spent an
        # afternoon being investigated as a credential problem. Safe to log in
        # full: this is a RESPONSE — it never contains a secret, and `redact`
        # covers the one case that could (a session token echoed back).
        diagnostics.emit("broker", "error",
                         f"[firstock] {url.rsplit('/', 1)[-1]} → HTTP "
                         f"{response.status_code} {redact(body)}")
        raise _error_from(body)
    # Returned raw: Firstock answers some endpoints with an object and others
    # with a list, and flattening one into the other here would cost the caller
    # the ability to tell "no rows" from "no data block".
    return body.get("data")


def _rows(data: Any) -> list[dict]:
    """The list of rows in a `data` block, whatever shape it arrived in."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _as_dict(data: Any) -> dict:
    return data if isinstance(data, dict) else {}


# The only field logged verbatim. Not a secret — it is the client code Firstock
# prints on the user's own dashboard and the UI shows in the account form — and
# it is the field a login failure most often turns on. Everything else,
# vendorCode included, is a credential and is described rather than disclosed:
# tests/test_firstock_login.py enforces exactly that, and caught this line
# leaking the vendor code when it was first written.
_LOGGABLE = ("userId",)


def _shape(payload: dict) -> str:
    """A login payload described without disclosing it.

    Credentials become "<len=N>" rather than a prefix: a TOTP is only 6 digits,
    so a prefix of it is most of it, and this line goes to a log file we ask
    testers to send us.

    The `+ws` marker is the point of the whole line. A credential pasted from a
    portal with a trailing space or a newline is indistinguishable from a wrong
    one in every error Firstock returns, and is invisible in the UI — but it
    shows up here immediately.
    """
    parts = []
    for key, value in payload.items():
        text = str(value)
        flag = "+ws" if text != text.strip() else ""
        if key in _LOGGABLE:
            parts.append(f"{key}={text!r}{flag}")
        else:
            parts.append(f"{key}=<len={len(text)}{flag}>")
    return " ".join(parts)


def login(user_id: str, password: str, vendor_code: str, api_key: str,
          totp_secret: str | None = None) -> "FirstockClient":
    """Authenticate and return a live session.

    `password` may be the plain password or an existing SHA-256 digest — see
    `hash_password`. `totp_secret` is the shared secret, not a code: Firstock's
    API wants a current 6-digit value, and generating it here is what makes
    unattended re-authentication possible at all.

    TOTP is MANDATORY, not conditional on the account having 2FA switched on.
    Firstock validates the field before it looks at any credential — a login
    without it comes back ``MISSING_FIELD: TOTP cannot be empty`` for every
    account, valid or not — so it is demanded here, by name, rather than left
    to fail at the broker as a message the user cannot act on.
    """
    secret = (totp_secret or "").strip().replace(" ", "")
    if not secret:
        raise FirstockError(
            "MISSING_TOTP_SECRET",
            "Firstock requires a TOTP code on every login, so Charticks needs "
            "the account's TOTP secret to generate one. Add it to the account "
            "(Firstock → API app → TOTP / 2FA setup key).")

    import pyotp

    payload = {
        "userId": user_id,
        "password": hash_password(password),
        "vendorCode": vendor_code,
        "apiKey": api_key,
        "TOTP": pyotp.TOTP(secret).now(),
    }

    # What actually goes on the wire, with every secret reduced to a SHAPE.
    # Lengths and prefixes are what diagnose this class of failure — a vendor
    # code pasted with a trailing space, an API key from the wrong app, a
    # password field holding a pre-hashed value — without ever writing a
    # credential to a file we ask testers to send us.
    diagnostics.emit("broker", "info", "[firstock] login request → " + _shape(payload))

    data = _as_dict(_post(LOGIN_URL, payload, LOGIN_TIMEOUT))
    token = str(data.get("susertoken") or "")
    if not token:
        # A 200 with no token is not a success. Reporting it as one is how a
        # dead session gets marked CONNECTED and every later call fails with a
        # message about the wrong thing.
        raise FirstockError("NO_SESSION_TOKEN",
                            "Firstock accepted the login but returned no session "
                            "token, so the session cannot be used")
    return FirstockClient(user_id=user_id, jkey=token,
                          actid=str(data.get("actid") or ""),
                          user_name=str(data.get("userName") or ""))


class FirstockClient:
    """One authenticated Firstock session.

    Stored by BrokerManager as the account's session object and handed to the
    feed, so there is exactly one token per account and refreshing it in one
    place refreshes it everywhere.
    """

    broker = "firstock"

    def __init__(self, user_id: str, jkey: str, actid: str = "",
                 user_name: str = "") -> None:
        self.user_id = user_id
        self.jkey = jkey
        self.actid = actid or user_id
        self.user_name = user_name
        # Filled by validate(); empty until then.
        self.exchanges: list[str] = []
        self.order_types: list[str] = []

    def __repr__(self) -> str:  # never leak the token through a traceback
        return f"<FirstockClient user={self.user_id} actid={self.actid}>"

    # ── session ───────────────────────────────────────────────────────────
    def _auth(self, **extra: Any) -> dict:
        payload = {"userId": self.user_id, "jKey": self.jkey}
        payload.update(extra)
        return payload

    def validate(self) -> dict:
        """Prove the token works with a real authenticated call.

        A login response that merely looked successful is not evidence — that is
        precisely how ICICI's dead session tokens used to be reported as
        connected. `userDetails` is the cheapest call that requires a live
        session, and it returns the enabled exchange list, which is worth
        knowing before an order is ever routed.
        """
        data = _as_dict(_post(USER_DETAILS_URL, self._auth(), CALL_TIMEOUT))
        exchanges = data.get("exchange")
        if isinstance(exchanges, list):
            self.exchanges = [str(e).upper() for e in exchanges]
        order_types = data.get("orarr")
        if isinstance(order_types, list):
            self.order_types = [str(o).upper() for o in order_types]
        if data.get("actid"):
            self.actid = str(data["actid"])
        if data.get("userName"):
            self.user_name = str(data["userName"])
        return data

    def index_list(self) -> list[dict]:
        """Spot tokens for the tradable indices.

        The public symbol files hold index DERIVATIVES only, so this is the sole
        source of an index's own token — which is why it needs a session and
        cannot live in the scrip master.
        """
        return _rows(_post(INDEX_LIST_URL, self._auth(), CALL_TIMEOUT))

    def close_session(self) -> None:
        """Invalidate the token at Firstock.

        Named `close_session` rather than `logout` on purpose: BrokerManager
        calls it duck-typed on disconnect, and several broker SDKs already have
        a `logout` of their own with different semantics. A distinct name means
        adding this cannot change what disconnect does for any existing broker.

        Never raises — disconnect must succeed locally even when the broker is
        unreachable, or an account could not be removed while the network is
        down.
        """
        try:
            _post(LOGOUT_URL, self._auth(), CALL_TIMEOUT)
        except Exception:
            pass
        finally:
            self.jkey = ""

    # -- trading -----------------------------------------------------------
    # Every method below takes CHARTICKS' vocabulary and translates on the way
    # out. Callers pass "NRML"/"MIS"/"CNC", "MARKET"/"LIMIT"/"SL"/"SL-M",
    # "BUY"/"SELL" - never a Firstock code.

    @staticmethod
    def _translate(order_type: str, product: str, side: str,
                   validity: str) -> tuple[str, str, str, str]:
        """Canonical values -> Firstock codes, or raise.

        An unknown value RAISES rather than defaulting. A silent fallback here
        would place a real order under the wrong product or the wrong side, and
        `product` in particular decides whether a position carries overnight
        margin or is auto-squared-off by the exchange as an intraday one.
        """
        try:
            return (ORDER_TYPE[order_type.upper()], PRODUCT[product.upper()],
                    SIDE[side.upper()], VALIDITY[validity.upper()])
        except KeyError as exc:
            raise FirstockError(
                "UNSUPPORTED_ORDER",
                f"Charticks asked Firstock for an order attribute it does not "
                f"support ({exc.args[0]!r}). Supported: order types "
                f"{sorted(ORDER_TYPE)}, products {sorted(PRODUCT)}, sides "
                f"{sorted(SIDE)}, validities {sorted(VALIDITY)}.") from exc

    @staticmethod
    def _price_fields(order_type: str, price: float,
                      trigger_price: float) -> dict[str, str]:
        """The price/triggerPrice pair for an order type.

        Firstock wants both fields present on every order and "0" where one does
        not apply, so this is stated once rather than repeated in place and
        modify with a chance of drifting apart.
        """
        kind = order_type.upper()
        return {
            "price": f"{float(price):.2f}" if kind in ("LIMIT", "SL") else "0",
            "triggerPrice": (f"{float(trigger_price):.2f}"
                             if kind in TRIGGERED else "0"),
        }

    def place_order(self, *, exchange: str, trading_symbol: str, side: str,
                    qty: int, order_type: str, price: float = 0.0,
                    trigger_price: float = 0.0, product: str = "NRML",
                    validity: str = "DAY", remarks: str = "") -> str:
        """Place one order. Returns Firstock's order number.

        `remarks` is where the idempotency client order id rides: Firstock
        requires the field and echoes it back on the order book, which is what
        makes duplicate detection possible for this broker at all.
        """
        price_type, prd, transaction, retention = self._translate(
            order_type, product, side, validity)
        payload = self._auth(
            exchange=exchange,
            tradingSymbol=trading_symbol,
            transactionType=transaction,
            priceType=price_type,
            product=prd,
            retention=retention,
            quantity=str(int(qty)),
            remarks=remarks or "Charticks",
            **self._price_fields(order_type, price, trigger_price),
        )
        if order_type.upper() in NEEDS_PROTECTION:
            payload["mkt_protection"] = MARKET_PROTECTION_PCT
        data = _as_dict(_post(PLACE_ORDER_URL, payload, CALL_TIMEOUT))
        order_number = str(data.get("orderNumber") or "")
        if not order_number:
            raise FirstockError("NO_ORDER_NUMBER",
                                "Firstock accepted the order but returned no order "
                                "number, so it could not be tracked")
        return order_number

    def modify_order(self, *, order_number: str, exchange: str,
                     trading_symbol: str, qty: int, order_type: str,
                     price: float = 0.0, trigger_price: float = 0.0,
                     product: str = "NRML", validity: str = "DAY") -> str:
        """Modify a resting order. Firstock wants it RESTATED, not diffed."""
        price_type, prd, _side, retention = self._translate(
            order_type, product, "BUY", validity)
        payload = self._auth(
            orderNumber=str(order_number),
            exchange=exchange,
            tradingSymbol=trading_symbol,
            product=prd,
            priceType=price_type,
            retention=retention,
            quantity=str(int(qty)),
            **self._price_fields(order_type, price, trigger_price),
        )
        if order_type.upper() in NEEDS_PROTECTION:
            payload["mkt_protection"] = MARKET_PROTECTION_PCT
        data = _as_dict(_post(MODIFY_ORDER_URL, payload, CALL_TIMEOUT))
        self._raise_if_refused(data, "MODIFY_REJECTED")
        return str(data.get("orderNumber") or order_number)

    def cancel_order(self, order_number: str) -> str:
        """Cancel a resting order."""
        data = _as_dict(_post(CANCEL_ORDER_URL,
                              self._auth(orderNumber=str(order_number)),
                              CALL_TIMEOUT))
        self._raise_if_refused(data, "CANCEL_REJECTED")
        return str(data.get("orderNumber") or order_number)

    @staticmethod
    def _raise_if_refused(data: dict, name: str) -> None:
        """Firstock answers a refused modify or cancel with HTTP 200 and a
        populated `rejreason` - "SAF:order is not open to cancel" arrives as a
        success. Reading only the status would report a refusal as a success,
        which is the same trap Dhan's response shape sets and the reason
        `_dhan_response` exists.
        """
        reason = str(data.get("rejreason") or "").strip()
        if reason:
            raise FirstockError(name, reason)

    # -- books -------------------------------------------------------------
    def order_book(self) -> list[dict]:
        """Every order for the day.

        An EMPTY book is a legitimate answer and returns an empty list; a failed
        read raises. Collapsing the two is how an idempotency check concludes
        "the order is absent" and authorises a duplicate.
        """
        return _rows(_post(ORDER_BOOK_URL, self._auth(), CALL_TIMEOUT))

    def trade_book(self) -> list[dict]:
        """Executed trades, with `fillPriceRupees` added.

        Firstock's own sample shows `fillPrice: 2837` for a stock trading near
        28 rupees alongside `pricePrecision: "2"` - i.e. this ONE field arrives
        as a scaled integer while the order and position books use decimal
        strings. Rather than assume either way, each row is normalised by the
        precision the row itself carries, and the original value is left intact
        beside it so a wrong reading is visible rather than silent.
        """
        rows = _rows(_post(TRADE_BOOK_URL, self._auth(), CALL_TIMEOUT))
        for row in rows:
            raw = row.get("fillPrice")
            if raw is None:
                continue
            try:
                value = float(raw)
                precision = int(float(row.get("pricePrecision") or 0))
            except (TypeError, ValueError):
                continue
            # A value that already carries a decimal point is in rupees; only a
            # bare integer is scaled.
            if precision > 0 and "." not in str(raw):
                value = value / (10 ** precision)
            row["fillPriceRupees"] = round(value, 4)
        return rows

    def positions(self) -> list[dict]:
        return _rows(_post(POSITION_BOOK_URL, self._auth(), CALL_TIMEOUT))

    def holdings(self) -> list[dict]:
        """Delivery holdings.

        Exposed for completeness. Charticks reads holdings for NO broker - it
        trades index options rather than delivery - so wiring this into the app
        would mean inventing a seam no other broker implements.
        """
        return _rows(_post(HOLDINGS_URL, self._auth(), CALL_TIMEOUT))

    # -- funds / margin ----------------------------------------------------
    def limits(self) -> dict:
        return _as_dict(_post(LIMITS_URL, self._auth(), CALL_TIMEOUT))

    def order_margin(self, *, exchange: str, trading_symbol: str, side: str,
                     qty: int, order_type: str, price: float,
                     product: str = "NRML") -> dict:
        """What this order would need, and what the account has.

        Firstock returns BOTH in one call - `marginOnNewOrder` and
        `availableMargin` - which no incumbent broker does.
        """
        price_type, prd, transaction, _retention = self._translate(
            order_type, product, side, "DAY")
        return _as_dict(_post(ORDER_MARGIN_URL, self._auth(
            exchange=exchange,
            tradingSymbol=trading_symbol,
            transactionType=transaction,
            priceType=price_type,
            product=prd,
            quantity=str(int(qty)),
            price=f"{float(price):.2f}",
        ), CALL_TIMEOUT))

    @property
    def missing_option_exchanges(self) -> list[str]:
        """Option venues Charticks needs that this account is not enabled for.

        Empty when `validate()` has not run or the account carries them, so a
        caller can report a real entitlement gap without inventing one from an
        unanswered question.
        """
        if not self.exchanges:
            return []
        return [e for e in ("NFO", "BFO") if e not in self.exchanges]
