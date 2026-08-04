"""Kotak Neo's scrip master -> canonical instrument bindings.

Same contract as the Dhan equivalent: the only outputs are
``InstrumentKey -> Kotak token`` bindings under the "kotak" namespace plus the
exchange segment each instrument subscribes on. Nothing here touches Angel's
or Dhan's namespace, and the option chain reads none of it directly — it asks
``services.instruments`` for expiries and strikes and gets whatever the
connected brokers have published.

Fetched through the authenticated session's ``scrip_master(exchange_segment)``
rather than downloaded, because Kotak serves it per segment behind auth. The
session is used strictly read-only (see KotakFeed).

Kotak's column names have varied across SDK releases and its expiry column has
historically been an epoch offset rather than a date, so columns are resolved
by alias and every resolution is logged. This is the part most likely to need
a tweak on first contact with a live account.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from services.instruments import InstrumentKey

# Kotak exchange-segment strings, per underlying family.
SEG_NSE_FO = "nse_fo"
SEG_BSE_FO = "bse_fo"
SEG_MCX_FO = "mcx_fo"
SEG_NSE_CM = "nse_cm"
SEG_BSE_CM = "bse_cm"

OPT_SEGMENT = {
    "NIFTY": SEG_NSE_FO, "BANKNIFTY": SEG_NSE_FO, "FINNIFTY": SEG_NSE_FO,
    "MIDCPNIFTY": SEG_NSE_FO, "SENSEX": SEG_BSE_FO, "BANKEX": SEG_BSE_FO,
    "CRUDEOIL": SEG_MCX_FO,
}
SPOT_SEGMENT = {
    "NIFTY": SEG_NSE_CM, "BANKNIFTY": SEG_NSE_CM, "FINNIFTY": SEG_NSE_CM,
    "MIDCPNIFTY": SEG_NSE_CM, "SENSEX": SEG_BSE_CM, "BANKEX": SEG_BSE_CM,
    "CRUDEOIL": SEG_MCX_FO,
}
SUPPORTED = set(OPT_SEGMENT)

# Kotak spells index underlyings differently again.
_ALIAS = {
    "NIFTY 50": "NIFTY", "NIFTY50": "NIFTY", "NIFTY": "NIFTY",
    "NIFTY BANK": "BANKNIFTY", "BANKNIFTY": "BANKNIFTY", "NIFTY BANK INDEX": "BANKNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY", "FINNIFTY": "FINNIFTY",
    "NIFTY MID SELECT": "MIDCPNIFTY", "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX", "BANKEX": "BANKEX", "CRUDEOIL": "CRUDEOIL",
}

_COLS = {
    # The subscribe id. pSymbol is the numeric instrument token; pTrdSymbol is
    # the trading symbol. Both are bound, because which one the tick's `tk`
    # field carries has differed between segments and SDK versions.
    "token": ("pSymbol", "pSymbolName", "instrument_token", "TOKEN"),
    "trading_symbol": ("pTrdSymbol", "pTrdSymbolName", "tradingsymbol", "TRADINGSYMBOL"),
    "name": ("pSymbolName", "pDesc", "name", "SYMBOL_NAME"),
    "instrument": ("pInstType", "instrument_type", "INSTRUMENT_TYPE"),
    "expiry": ("pExpiryDate", "expiry", "EXPIRY"),
    "strike": ("pStrikePrice", "strike_price", "STRIKE"),
    "option_type": ("pOptionType", "option_type", "OPTION_TYPE"),
    "segment": ("pExchSeg", "exchange_segment", "EXCH_SEG"),
}

# Kotak's pExpiryDate has historically been "seconds since 1980-01-01" rather
# than a Unix epoch. Both are tried; whichever lands in a sane window wins.
_KOTAK_EPOCH_OFFSET = 315513000


def _resolve_columns(sample: dict) -> dict[str, str]:
    present = {str(k).strip(): k for k in sample}
    upper = {k.upper(): v for k, v in present.items()}
    out: dict[str, str] = {}
    for logical, aliases in _COLS.items():
        for alias in aliases:
            if alias in present:
                out[logical] = present[alias]
                break
            if alias.upper() in upper:
                out[logical] = upper[alias.upper()]
                break
    return out


def _norm_expiry(raw: Any) -> str:
    """Kotak expiries -> canonical '02SEP2026'.

    Accepts a date string or either epoch convention. A value that lands
    outside a plausible contract window is rejected rather than guessed at, so
    a misread column produces no bindings (and a visible zero count) instead of
    thousands of contracts on nonsense dates.
    """
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    for fmt in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(text.split(" ")[0], fmt).strftime("%d%b%Y").upper()
        except ValueError:
            continue
    try:
        secs = float(text)
    except ValueError:
        return ""
    today = dt.date.today()
    lo, hi = today - dt.timedelta(days=30), today + dt.timedelta(days=800)
    for candidate in (secs, secs + _KOTAK_EPOCH_OFFSET):
        try:
            d = dt.datetime.fromtimestamp(candidate).date()
        except (OverflowError, OSError, ValueError):
            continue
        if lo <= d <= hi:
            return d.strftime("%d%b%Y").upper()
    return ""


def _canonical(name: str, trading_symbol: str) -> str | None:
    for c in (name, trading_symbol):
        c = (c or "").strip().upper()
        if not c:
            continue
        if c in _ALIAS:
            return _ALIAS[c]
        flat = c.replace(" ", "").replace("-", "")
        for u in SUPPORTED:
            if flat.startswith(u):
                return u
    return None


class KotakScripMaster:
    """Builds canonical bindings from the authenticated session's scrip master."""

    def __init__(self, log) -> None:
        self._log = log
        self.segments: dict[InstrumentKey, str] = {}
        # Primary id (what we subscribe with) and extra ids that ticks may
        # arrive under. Kept apart because binding is one-to-one.
        self._bindings: list[tuple[InstrumentKey, str]] = []
        self._aliases: list[tuple[InstrumentKey, str]] = []
        self.row_count = 0
        self.loaded_from: str | None = None

    def load(self, client: Any, segments: list[str]) -> bool:
        """Fetch each segment via the shared session. Read-only: this calls one
        documented accessor and nothing that could alter the session."""
        from services import expiry as expiry_filter

        bindings: list[tuple[InstrumentKey, str]] = []
        aliases: list[tuple[InstrumentKey, str]] = []
        seg_map: dict[InstrumentKey, str] = {}
        cols: dict[str, str] = {}
        fetched = 0
        for segment in segments:
            try:
                rows = client.scrip_master(exchange_segment=segment)
            except Exception as e:
                self._log("warn", f"⚠️  Kotak scrip master {segment} failed: {e}")
                continue
            if isinstance(rows, dict):
                # The SDK returns {"Error": ...} rather than raising.
                self._log("warn", f"⚠️  Kotak scrip master {segment}: {rows}")
                continue
            rows = list(rows or [])
            if not rows:
                self._log("warn", f"⚠️  Kotak scrip master {segment} returned no rows")
                continue
            fetched += len(rows)
            if not cols:
                cols = _resolve_columns(rows[0])
                self._log("info", "[kotak-scrip] columns resolved: "
                                  + ", ".join(f"{k}->{v}" for k, v in sorted(cols.items())))
                missing = [k for k in ("token", "instrument") if k not in cols]
                if missing:
                    self._log("warn", f"⚠️  Kotak scrip master missing columns {missing}; "
                                      f"sample keys={list(rows[0])[:12]}")

            def get(row: dict, key: str) -> str:
                col = cols.get(key)
                return str(row.get(col, "")).strip() if col else ""

            for row in rows:
                if not isinstance(row, dict):
                    continue
                token = get(row, "token")
                if not token:
                    continue
                trading_symbol = get(row, "trading_symbol")
                underlying = _canonical(get(row, "name"), trading_symbol)
                if underlying is None:
                    continue
                instrument = get(row, "instrument").upper()
                opt_type = get(row, "option_type").upper()
                is_option = opt_type in ("CE", "PE") or "OPT" in instrument

                if is_option:
                    expiry = _norm_expiry(row.get(cols.get("expiry")) if cols.get("expiry") else "")
                    if not expiry or expiry_filter.is_expired(expiry):
                        continue
                    try:
                        strike = float(get(row, "strike") or 0)
                    except ValueError:
                        continue
                    # Some segments quote strikes in paise. Index strikes are
                    # never below 100, so a value that only makes sense after
                    # dividing by 100 is treated as paise.
                    if strike >= 100000:
                        strike = strike / 100
                    if strike <= 0 or opt_type not in ("CE", "PE"):
                        continue
                    key = InstrumentKey.option(underlying, expiry, strike, opt_type)
                    seg = OPT_SEGMENT.get(underlying)
                elif "IDX" in instrument or "INDEX" in instrument:
                    key = InstrumentKey.index(underlying)
                    seg = SPOT_SEGMENT.get(underlying)
                else:
                    continue
                if seg is None:
                    continue
                seg_map[key] = seg
                # Subscribe by TRADING SYMBOL: that is what the previously
                # working engines/kotak_data_engine.py used for both the
                # subscription and tick matching. The numeric token is
                # registered as an alias so a tick arriving under either id
                # still resolves — an unmappable tick is a blank price.
                primary = trading_symbol or token
                bindings.append((key, primary))
                if token and token != primary:
                    aliases.append((key, token))

        self.segments = seg_map
        self._bindings = bindings
        self._aliases = aliases
        self.row_count = len(seg_map)
        self.loaded_from = f"session ({fetched} rows across {len(segments)} segments)"
        if not seg_map:
            self._log("error", "❌ Kotak scrip master produced no usable instruments — "
                               "column mapping likely needs adjusting (see the resolved columns above)")
            return False
        self._log("info", f"✅ Kotak scrip master: {len(seg_map)} instruments")
        return True

    def bindings(self) -> list[tuple[InstrumentKey, str]]:
        return list(self._bindings)

    def aliases(self) -> list[tuple[InstrumentKey, str]]:
        return list(self._aliases)
