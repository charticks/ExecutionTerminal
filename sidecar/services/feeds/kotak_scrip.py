"""Kotak Neo's scrip master -> canonical instrument bindings.

Same contract as the Dhan equivalent: the only outputs are
``InstrumentKey -> Kotak token`` bindings under the "kotak" namespace plus the
exchange segment each instrument subscribes on. Nothing here touches Angel's
or Dhan's namespace, and the option chain reads none of it directly — it asks
``services.instruments`` for expiries and strikes and gets whatever the
connected brokers have published.

Located through the authenticated session's ``scrip_master(exchange_segment)``,
then downloaded and cached like Dhan's (see dhan_scrip.py, whose availability
rules this follows: refresh once a day into services.paths.data_dir, fall back to
the newest cached file, log every outcome). The session is used strictly
read-only (see KotakFeed).

**``scrip_master()`` returns a URL, not rows.** It resolves the segment name,
picks the matching entry out of the API's ``filesPaths`` list, and returns that
one string — see ``ScripMasterAPI.scrip_master_init``. Treating the return value
as a row collection is silently destructive rather than an error: iterating a
string yields its characters, so every "row" is a one-character string, column
resolution matches nothing, and the master parses to zero instruments. That is
exactly what ``columns resolved:`` with ``sample keys=['h']`` in broker.log was —
the ``h`` of ``https``.

Kotak's column names have varied across SDK releases and its expiry column has
historically been an epoch offset rather than a date, so columns are resolved
by alias and every resolution is logged.
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import io
import os
from typing import Any

from services.instruments import InstrumentKey
from services.paths import data_dir

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

# Per-segment daily cache, alongside Dhan's in the sidecar data dir.
_CACHE_PREFIX = "kotak_scrip_"
_CACHE_KEEP = 2


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
        # key -> numeric pSymbol. The instruments registry binds one id per key
        # (the trading symbol, which is what subscribes), so the numeric token
        # has no forward lookup there — and Kotak's margin API wants exactly
        # that, not the symbol.
        self._tokens: dict[InstrumentKey, str] = {}
        self.row_count = 0
        self.loaded_from: str | None = None

    # ── fetch ─────────────────────────────────────────────────────────────
    def _segment_rows(self, client: Any, segment: str) -> list[dict]:
        """Rows for one exchange segment, whatever shape the SDK hands back."""
        try:
            response = client.scrip_master(exchange_segment=segment)
        except Exception as e:
            self._log("warn", f"⚠️  Kotak scrip master {segment} failed: {e}")
            return []
        # The SDK reports failure by RETURNING {"Error": ...} rather than raising.
        if isinstance(response, dict):
            self._log("warn", f"⚠️  Kotak scrip master {segment}: {response}")
            return []
        # The documented case: a URL to the segment's CSV.
        if isinstance(response, str):
            text = self._csv_text(response.strip(), segment)
            if not text:
                return []
            rows = [r for r in csv.DictReader(io.StringIO(text)) if r]
            if not rows:
                self._log("warn", f"⚠️  Kotak scrip master {segment}: CSV had no data rows")
            return rows
        # A future SDK version that returns rows directly still works.
        return [r for r in list(response or []) if isinstance(r, dict)]

    def _csv_text(self, url: str, segment: str) -> str:
        """Today's CSV for a segment: cache, else download, else newest stale
        copy. A stale master is worse than a fresh one but very much better than
        no instruments at all — the feed and the order router both need tokens.
        """
        if not url.lower().startswith(("http://", "https://")):
            self._log("warn", f"⚠️  Kotak scrip master {segment}: expected a CSV URL, "
                              f"got {url[:60]!r}")
            return ""
        try:
            cache_dir = data_dir()
        except OSError as e:
            self._log("warn", f"⚠️  Kotak scrip cache unavailable ({e}) — fetching without cache")
            cache_dir = ""

        path = (os.path.join(cache_dir,
                             f"{_CACHE_PREFIX}{segment}_{dt.date.today():%Y%m%d}.csv")
                if cache_dir else "")
        if path and os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                return open(path, encoding="utf-8", errors="replace").read()
            except OSError as e:
                self._log("warn", f"⚠️  Kotak scrip cache unreadable ({e}) — re-downloading")

        try:
            import requests
            r = requests.get(url, timeout=90, headers={"User-Agent": "Charticks/1.0"})
            r.raise_for_status()
            text = r.content.decode("utf-8", "replace")
            if not text.strip():
                raise ValueError("empty body")
        except Exception as e:
            self._log("warn", f"⚠️  Kotak scrip master {segment} download failed: {e}")
            return self._stale(segment, cache_dir)

        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                self._prune(segment, cache_dir)
            except OSError as e:
                # Non-fatal: we have the text, we just won't have it next time.
                self._log("warn", f"⚠️  Could not cache Kotak scrip master {segment}: {e}")
        return text

    def _stale(self, segment: str, cache_dir: str) -> str:
        if not cache_dir:
            return ""
        files = sorted(glob.glob(os.path.join(cache_dir, f"{_CACHE_PREFIX}{segment}_*.csv")))
        if not files:
            return ""
        try:
            text = open(files[-1], encoding="utf-8", errors="replace").read()
        except OSError:
            return ""
        self._log("warn", f"⚠️  Using STALE Kotak scrip master for {segment} "
                          f"({os.path.basename(files[-1])}) — expiries may be out of date")
        return text

    @staticmethod
    def _prune(segment: str, cache_dir: str) -> None:
        if not cache_dir:
            return
        pattern = os.path.join(cache_dir, f"{_CACHE_PREFIX}{segment}_*.csv")
        for old in sorted(glob.glob(pattern))[:-_CACHE_KEEP]:
            try:
                os.remove(old)
            except OSError:
                pass

    def load(self, client: Any, segments: list[str]) -> bool:
        """Fetch each segment via the shared session. Read-only: this calls one
        documented accessor and nothing that could alter the session."""
        from services import expiry as expiry_filter

        bindings: list[tuple[InstrumentKey, str]] = []
        aliases: list[tuple[InstrumentKey, str]] = []
        seg_map: dict[InstrumentKey, str] = {}
        tokens: dict[InstrumentKey, str] = {}
        fetched = 0
        for segment in segments:
            rows = self._segment_rows(client, segment)
            if not rows:
                continue
            fetched += len(rows)
            # Resolved per segment, not once for all of them: Kotak's MCX and
            # NSE masters are separate files and have not always agreed on
            # column naming, so borrowing the first segment's mapping can
            # silently drop a whole exchange.
            cols = _resolve_columns(rows[0])
            missing = [k for k in ("token", "instrument") if k not in cols]
            self._log("info", f"[kotak-scrip] {segment}: {len(rows)} rows, columns resolved: "
                              + (", ".join(f"{k}->{v}" for k, v in sorted(cols.items()))
                                 or "(none)"))
            if missing:
                self._log("warn", f"⚠️  Kotak scrip master {segment} missing columns {missing}; "
                                  f"header={list(rows[0])[:12]}")
                continue

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
                tokens[key] = token
                if token and token != primary:
                    aliases.append((key, token))

        self.segments = seg_map
        self._bindings = bindings
        self._aliases = aliases
        self._tokens = tokens
        self.row_count = len(seg_map)
        self.loaded_from = f"csv ({fetched} rows across {len(segments)} segments)"
        if not seg_map:
            self._log("error", f"❌ Kotak scrip master produced no usable instruments from "
                               f"{fetched} rows — column mapping likely needs adjusting "
                               f"(see the resolved columns above)")
            return False
        self._log("info", f"✅ Kotak scrip master: {len(seg_map)} instruments")
        return True

    def bindings(self) -> list[tuple[InstrumentKey, str]]:
        return list(self._bindings)

    def aliases(self) -> list[tuple[InstrumentKey, str]]:
        return list(self._aliases)

    def token_for(self, key: InstrumentKey) -> str:
        """Kotak's numeric instrument token (pSymbol) for a contract, or "".

        Wanted by margin_required(), which takes a token rather than the trading
        symbol that place_order() takes.
        """
        return self._tokens.get(key, "")
