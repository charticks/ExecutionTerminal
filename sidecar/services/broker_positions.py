"""Reading each broker's own position book, in one canonical shape.

Every broker answers "what do I hold?" differently — different endpoint,
different field names, different sign conventions, different symbol spellings.
This module is the only place that knows any of that. It returns
``BrokerPosition`` rows carrying, where it can, the canonical
``InstrumentKey``, so everything above it — reconciliation, the live book, the
Positions grid — deals in one identity.

Resolution order for that key, strongest first:

  1. the broker's own token, looked up in the shared instrument registry (exact:
     the registry was populated from that broker's scrip master);
  2. the tradingsymbol, parsed (``InstrumentKey.from_symbol``);
  3. nothing — the row is returned with ``key=None``.

A row with no key is never guessed at. It is surfaced as a foreign position the
user can see but Charticks cannot manage, which is honest; matching it to the
wrong contract would be catastrophic, since exits are sized and routed from it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.instruments import InstrumentKey, instruments


@dataclass(frozen=True)
class BrokerPosition:
    account_id: str
    broker: str
    raw_id: str               # broker's own row id, for foreign legs
    symbol: str
    side: str                 # BUY | SELL (net direction)
    qty: int                  # absolute
    avg_entry: float
    ltp: float
    pnl: float
    key: InstrumentKey | None
    product: str = ""

    @property
    def id(self) -> str:
        """Canonical position id where we have one, else a broker-scoped id."""
        return self.key.position_id if self.key else f"{self.account_id}:{self.raw_id}"


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _first_positive(row: dict, keys: tuple[str, ...],
                    fallback: tuple[str, ...] = ()) -> Any:
    """First of `keys` whose value parses to a number greater than zero.

    Broker rows carry every average price field on every row and fill the
    irrelevant ones with "0". A plain `a or b or c` chain therefore stops at the
    first field rather than the first ANSWER, because the string "0" is truthy.
    """
    for group in (keys, fallback):
        for key in group:
            value = _f(row.get(key), 0.0)
            if value > 0:
                return value
    return 0.0


def _first_present(row: dict, keys: tuple[str, ...]) -> Any:
    """First of `keys` that parses to a number, sign preserved.

    The counterpart to `_first_positive` for quantities that may legitimately be
    NEGATIVE — P&L above all. Using the positive-only helper for those reports
    every losing position as flat.
    """
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _key_for(broker: str, token: Any, symbol: str) -> InstrumentKey | None:
    if token:
        key = instruments.key_for(broker, str(token))
        if key is not None and key.segment == "OPT":
            return key
    return InstrumentKey.from_symbol(symbol)


def _angel(account_id: str, sess: Any) -> list[BrokerPosition]:
    out: list[BrokerPosition] = []
    resp = sess.position()
    data = resp.get("data") if isinstance(resp, dict) else None
    for p in data or []:
        qty = int(_f(p.get("netqty")))
        if qty == 0:
            continue
        symbol = p.get("tradingsymbol", "")
        token = p.get("symboltoken")
        # Cost basis for the side actually HELD. This read the buy average
        # unconditionally, so every net short came back with an entry of 0 (the
        # buy fields are present but zero, and "0" is truthy in an `or` chain).
        # A zero cost basis is not cosmetic: `adopt` refuses a position without
        # one, so a short opened in Angel's own terminal could never be brought
        # under management, and its P&L read as 0.
        avg = _f(_first_positive(
            p, ("totalbuyavgprice", "buyavgprice") if qty > 0
               else ("totalsellavgprice", "sellavgprice"),
            fallback=("avgnetprice", "netprice")))
        out.append(BrokerPosition(
            account_id=account_id, broker="angel",
            raw_id=str(token or symbol), symbol=symbol,
            side="BUY" if qty > 0 else "SELL", qty=abs(qty),
            avg_entry=round(avg, 2), ltp=round(_f(p.get("ltp")), 2),
            pnl=round(_f(p.get("pnl")), 2),
            key=_key_for("angel", token, symbol),
            product=str(p.get("producttype") or "")))
    return out


def _dhan(account_id: str, sess: Any) -> list[BrokerPosition]:
    out: list[BrokerPosition] = []
    resp = sess.get_positions()
    data = resp.get("data") if isinstance(resp, dict) else resp
    for p in data or []:
        qty = int(_f(p.get("netQty")))
        if qty == 0:
            continue
        symbol = p.get("tradingSymbol", "")
        token = p.get("securityId")
        avg = _f(p.get("buyAvg") if qty > 0 else p.get("sellAvg"))
        pnl = _f(p.get("unrealizedProfit")) + _f(p.get("realizedProfit"))
        out.append(BrokerPosition(
            account_id=account_id, broker="dhan",
            raw_id=str(token or symbol), symbol=symbol,
            side="BUY" if qty > 0 else "SELL", qty=abs(qty),
            avg_entry=round(avg, 2),
            ltp=round(_f(p.get("ltp") or p.get("lastTradedPrice")), 2),
            pnl=round(pnl, 2), key=_key_for("dhan", token, symbol),
            product=str(p.get("productType") or "")))
    return out


def _kotak(account_id: str, sess: Any) -> list[BrokerPosition]:
    out: list[BrokerPosition] = []
    resp = sess.positions()
    data = resp.get("data") if isinstance(resp, dict) else None
    for p in data or []:
        qty = int(_f(p.get("flBuyQty")) - _f(p.get("flSellQty")))
        if qty == 0:
            continue
        symbol = p.get("trdSym", "")
        token = p.get("tok")
        avg = _f(p.get("buyAmt") if qty > 0 else p.get("sellAmt"))
        avg = round(avg / abs(qty), 2) if qty else 0.0
        out.append(BrokerPosition(
            account_id=account_id, broker="kotak",
            raw_id=str(token or symbol), symbol=symbol,
            side="BUY" if qty > 0 else "SELL", qty=abs(qty),
            avg_entry=avg, ltp=round(_f(p.get("ltp")), 2),
            pnl=round(_f(p.get("urPnl") or p.get("rlPnl")), 2),
            key=_key_for("kotak", token, symbol),
            product=str(p.get("prod") or "")))
    return out


def _icici(account_id: str, sess: Any) -> list[BrokerPosition]:
    """Breeze get_portfolio_positions() → {"Success": [...], "Status": 200}.

    Row field names are produced server-side (not visible in the SDK), so every
    read has documented fallbacks — a shape drift shows up as a skipped row,
    never a wrong number. Breeze states the contract in PARTS rather than as one
    tradingsymbol, so the canonical key is assembled from those directly, which
    is stronger than parsing a string.
    """
    out: list[BrokerPosition] = []
    resp = sess.get_portfolio_positions()
    rows = resp.get("Success") if isinstance(resp, dict) else None
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        qty = int(_f(p.get("quantity") or p.get("net_quantity") or p.get("qty")))
        if qty == 0:
            continue
        action = str(p.get("action") or p.get("buy_sell") or "").upper()
        side = "SELL" if (qty < 0 or action.startswith("S")) else "BUY"
        avg = _f(p.get("average_price") or p.get("avg_price") or p.get("price"))
        ltp = _f(p.get("ltp") or p.get("last_traded_price") or p.get("current_price"))
        pnl = _f(p.get("pnl") or p.get("unrealized_profit") or p.get("profit_and_loss"))
        underlying = str(p.get("stock_code") or p.get("underlying") or "")
        key = None
        extra = ""
        if p.get("strike_price"):
            right = str(p.get("right") or "").upper()
            opt = {"CALL": "CE", "PUT": "PE"}.get(right, right)
            expiry = str(p.get("expiry_date", ""))
            extra = f" {expiry} {p.get('strike_price')} {opt}".rstrip()
            key = _icici_key(underlying, expiry, p.get("strike_price"), opt)
        symbol = f"{underlying}{extra}"
        out.append(BrokerPosition(
            account_id=account_id, broker="icici",
            raw_id=symbol, symbol=symbol, side=side, qty=abs(qty),
            avg_entry=round(avg, 2), ltp=round(ltp, 2), pnl=round(pnl, 2),
            key=key, product=str(p.get("product_type") or "")))
    return out


# ICICI writes the underlying its own way and dates as ISO timestamps.
_ICICI_UNDERLYING = {"CNXBAN": "BANKNIFTY", "NIFBAN": "BANKNIFTY",
                     "NIFTY": "NIFTY", "CNXNIF": "NIFTY", "NIFFIN": "FINNIFTY",
                     "NIFMID": "MIDCPNIFTY", "SENSEX": "SENSEX",
                     "BSEBAN": "BANKEX", "CRUDE": "CRUDEOIL"}
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _icici_key(underlying: str, expiry: str, strike: Any,
               opt_type: str) -> InstrumentKey | None:
    name = _ICICI_UNDERLYING.get(underlying.upper(), underlying.upper())
    text = (expiry or "").strip().upper()
    normalised = ""
    if "-" in text:  # ISO-ish: 2026-08-28T06:00:00.000Z
        try:
            date = text.split("T")[0].split(" ")[0]
            year, month, day = date.split("-")
            normalised = f"{int(day):02d}{_MONTHS[int(month) - 1]}{year}"
        except (ValueError, IndexError):
            normalised = ""
    elif text:
        normalised = text.replace("-", "")
    if not normalised or opt_type not in ("CE", "PE"):
        return None
    try:
        return InstrumentKey.option(name, normalised, float(strike), opt_type)
    except (TypeError, ValueError):
        return None


def _firstock_key(row: dict) -> InstrumentKey | None:
    """Resolve a Firstock position row to a canonical key, by token only.

    The composite "EXCHANGE:TOKEN" is tried first because that is what the
    scrip master binds as the primary id and it cannot collide across venues;
    the bare token is registered as an alias and covers a row that omits the
    exchange.

    There is deliberately NO tradingsymbol fallback. Firstock writes an option
    three different ways (NIFTY29SEP26C29150 on NFO, BANKEX26AUG54000CE and
    SENSEX26O0170700CE on BFO) and InstrumentKey.from_symbol parses none of
    them — it expects CE/PE at the end. Calling it would return None at best and
    match the WRONG contract at worst, and an exit is sized and routed from this
    key. An unresolved row is reported as an unmanaged foreign position, which
    is honest.
    """
    token = str(row.get("token") or "").strip()
    if not token:
        return None
    exchange = str(row.get("exchange") or "").strip().upper()
    if exchange:
        key = instruments.key_for("firstock", f"{exchange}:{token}")
        if key is not None:
            return key
    return instruments.key_for("firstock", token)


def _firstock(account_id: str, sess: Any) -> list[BrokerPosition]:
    """Firstock's position book -> canonical rows.

    Cost basis has to be ASSEMBLED. `netAveragePrice` reads "0.00" even on a
    position with trades in Firstock's own documented sample, so the side
    actually held is consulted next, and the carry-forward leg after that. Using
    an `or` chain would stop at the first FIELD rather than the first ANSWER,
    because the string "0.00" is truthy — the bug that left every Angel short
    with a zero entry and made it impossible to adopt.
    """
    from services.feeds.firstock_client import PRODUCT

    reverse_product = {code: name for name, code in PRODUCT.items()}
    out: list[BrokerPosition] = []
    for p in sess.positions():
        if not isinstance(p, dict):
            continue
        qty = int(_f(p.get("netQuantity")))
        if qty == 0:
            continue
        long_side = qty > 0
        avg = _f(_first_positive(
            p,
            ("netAveragePrice",),
            fallback=("dayBuyAveragePrice",) if long_side else ("daySellAveragePrice",)))
        if avg <= 0:
            # Carry-forward leg: Firstock reports it as an amount, not a price.
            amount = _f(p.get("cfBuyAmt" if long_side else "cfSellAmt"))
            carried = _f(p.get("cfBuyQty" if long_side else "cfSellQty"))
            if amount > 0 and carried > 0:
                avg = amount / carried
        symbol = str(p.get("tradingSymbol") or "")
        out.append(BrokerPosition(
            account_id=account_id, broker="firstock",
            raw_id=str(p.get("token") or symbol), symbol=symbol,
            side="BUY" if long_side else "SELL", qty=abs(qty),
            avg_entry=round(avg, 2),
            ltp=round(_f(p.get("lastTradedPrice")), 2),
            # NOT _first_positive: that helper exists for PRICES, which are
            # always positive, and it treats anything <= 0 as "field absent".
            # P&L is routinely negative, and running it through there would
            # report every losing position as flat.
            pnl=round(_f(_first_present(p, ("totalPNL", "totalMTM", "RealizedPNL"))), 2),
            key=_firstock_key(p),
            # Translated BACK into Charticks' model: the rest of the app knows
            # NRML/MIS/CNC, and leaking "M" into a grid the user reads would be
            # the adapter failing in the other direction.
            product=reverse_product.get(str(p.get("product") or "").upper(), "")))
    return out


_READERS = {"angel": _angel, "dhan": _dhan, "kotak": _kotak,
            "icici": _icici, "firstock": _firstock}


def supported(broker: str) -> bool:
    return (broker or "").lower() in _READERS


def read(account_id: str, broker: str, session: Any) -> list[BrokerPosition]:
    """Read one account's position book. Raises whatever the SDK raises — the
    caller classifies the error, because a failed read must never be mistaken
    for "this account holds nothing"."""
    reader = _READERS.get((broker or "").lower())
    if reader is None:
        return []
    return reader(account_id, session)
