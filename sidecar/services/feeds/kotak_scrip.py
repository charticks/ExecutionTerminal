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
import re
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
    "expiry": ("pExpiryDate", "lExpiryDate", "expiry", "EXPIRY"),
    # Kotak writes this header as `pStrikePrice;`. _squash() is what actually
    # matches it; the literal is listed so a reader sees the real name.
    "strike": ("pStrikePrice", "pStrikePrice;", "strike_price", "STRIKE"),
    "option_type": ("pOptionType", "option_type", "OPTION_TYPE"),
    "segment": ("pExchSeg", "exchange_segment", "EXCH_SEG"),
}

# Last-resort substring match, tried only after every alias has failed.
#
# The strike column is the reason this exists: its header could not be matched
# and the only evidence available was its ABSENCE from a log line, so the exact
# spelling Kotak ships is not something to bet the loader on. These substrings
# appear in no other column of the master, so a match is unambiguous — and a
# wrong guess here fails loudly now that strike is a required column.
_CONTAINS = {
    "strike": "strike",
    "expiry": "expiry",
    "option_type": "optiontype",
}

# Kotak's pExpiryDate has historically been "seconds since 1980-01-01" rather
# than a Unix epoch. Both are tried; whichever lands in a sane window wins.
_KOTAK_EPOCH_OFFSET = 315513000

# Per-segment daily cache, alongside Dhan's in the sidecar data dir.
_CACHE_PREFIX = "kotak_scrip_"
_CACHE_KEEP = 2

# Segments that are supposed to yield tradable OPTIONS. One of these returning
# rows but binding no option is a failure, however well the cash segments did.
_OPTION_SEGMENTS = (SEG_NSE_FO, SEG_BSE_FO, SEG_MCX_FO)

# Why a row was discarded. Counted per segment and logged, because "0 options"
# on its own does not distinguish a renamed column from an expiry format change
# from an account with no F&O entitlement — and each needs a different fix.
_DROP_REASONS = ("no_token", "unknown_underlying", "not_option_or_index",
                 "unparsable_expiry", "already_expired", "bad_strike",
                 "no_segment_mapping")


def _squash(name: str) -> str:
    """Header name reduced to its letters and digits.

    Kotak's scrip master ships punctuation inside its CSV header — the strike
    column arrives as ``pStrikePrice;``, semicolon included. Matching on the
    literal name therefore failed for that ONE column while every other column
    resolved, which is not a shape anyone reading "columns resolved: …" would
    think to check. Squashing makes the match immune to stray punctuation,
    spacing, underscores and case at once.
    """
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _resolve_columns(sample: dict) -> dict[str, str]:
    present = {str(k).strip(): k for k in sample}
    upper = {k.upper(): v for k, v in present.items()}
    # First writer wins, so an exact header is never displaced by a squashed
    # collision later in the row.
    squashed: dict[str, Any] = {}
    for key, original in present.items():
        squashed.setdefault(_squash(key), original)
    out: dict[str, str] = {}
    for logical, aliases in _COLS.items():
        for alias in aliases:
            if alias in present:
                out[logical] = present[alias]
                break
            if alias.upper() in upper:
                out[logical] = upper[alias.upper()]
                break
            if _squash(alias) in squashed:
                out[logical] = squashed[_squash(alias)]
                break
        if logical not in out and logical in _CONTAINS:
            needle = _CONTAINS[logical]
            for key, original in squashed.items():
                if needle in key:
                    out[logical] = original
                    break
    return out


# A complete number, INCLUDING an exponent. The exponent is the whole point:
# Kotak writes strikes as `1.84e+06`, and a parser that stops at the `e` reads
# ₹18,400 as 1.84 — which is exactly what happened. Every such strike then
# truncated to the same handful of integers (1, 2, 3…), collapsing 15,978 of
# 18,204 contracts onto a few keys.
_NUMBER_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _num(raw: Any) -> float | None:
    """The number in a scrip-master cell, or None.

    Handles scientific notation and tolerates the punctuation Kotak ships
    around its values (a header carrying a stray ``;`` implies the cells can
    too, and bare ``float("2450000;")`` raises).
    """
    text = str(raw or "").strip()
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


# The strike a Kotak trading symbol ends with: NIFTY26AUG18400CE -> 18400.
# Futures (…FUT) and index rows have no CE/PE tail and are skipped.
_SYMBOL_STRIKE_RE = re.compile(r"(\d+(?:\.\d+)?)(?:CE|PE)$")

# Scales worth recognising. Anything else is not a units difference, it is a
# misread column, and must not be silently accepted.
_PLAUSIBLE_SCALES = (1, 10, 100, 1000, 10000)


def _strike_divisor(rows: list[dict], strike_col: Any,
                    symbol_col: Any) -> tuple[float, int, str]:
    """MEASURE the scale of this segment's strike column. Never infer it.

    Kotak's own trading symbol carries the strike in rupees —
    ``NIFTY26AUG18400CE`` — so the ratio between ``dStrikePrice`` and that
    number IS the scale, read off the data rather than assumed. Returns
    (divisor, rows agreeing, an example) so the choice can be logged and
    audited.

    This replaces a magnitude heuristic that took the median of the first rows
    of a segment and called anything at or above 100,000 "paise". It was a
    guess, and it was wrong exactly where it mattered: `nse_fo` begins with
    STOCK options, whose paise strikes (₹1,300 -> 130000) sit below that
    threshold, so the whole NSE F&O segment was declared "rupees" and every
    NIFTY strike stayed at 1,840,000 while orders asked for 18,400. `bse_fo`
    only escaped because it happens to start with SENSEX.

    A ratio is counted only if it lands within 1% of a plausible power of ten,
    so rows whose symbol does not actually end in a strike cannot drag the
    measurement anywhere.
    """
    votes: dict[int, int] = {}
    example = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = _num(str(row.get(strike_col, "")).strip())
        if not raw or raw <= 0:
            continue
        match = _SYMBOL_STRIKE_RE.search(str(row.get(symbol_col, "")).strip().upper())
        if not match:
            continue
        shown = float(match.group(1))
        if shown <= 0:
            continue
        ratio = raw / shown
        for scale in _PLAUSIBLE_SCALES:
            if abs(ratio - scale) <= scale * 0.01:
                votes[scale] = votes.get(scale, 0) + 1
                if not example:
                    example = (f"{str(row.get(symbol_col, '')).strip()} "
                               f"raw {str(row.get(strike_col, '')).strip()} → {int(shown)}")
                break
        if sum(votes.values()) >= 300:
            break
    if not votes:
        return 0.0, 0, ""
    scale, agreeing = max(votes.items(), key=lambda kv: kv[1])
    return float(scale), agreeing, example


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
    """The underlying Charticks trades, or None.

    The name column is DECISIVE when present, and matched exactly. Prefix
    matching it was actively dangerous: ``"NIFTYNXT50".startswith("NIFTY")`` is
    true, so every NIFTY NEXT 50 contract was filed as a NIFTY one — putting
    84,200-strike contracts into NIFTY's strike space and making it possible for
    a NIFTY order to resolve to a NIFTY NEXT 50 instrument. The same trap waits
    for SENSEX50 against SENSEX.

    Only when there is no usable name does it fall back to reading the trading
    symbol, where the underlying really is a prefix — longest first, so
    BANKNIFTY is not read as NIFTY, and requiring what follows to be the
    expiry's digits rather than more letters of a different index's name.
    """
    clean = (name or "").strip().upper()
    if clean:
        flat = clean.replace(" ", "").replace("-", "")
        for candidate in (clean, flat):
            if candidate in _ALIAS:
                return _ALIAS[candidate]
            if candidate in SUPPORTED:
                return candidate
        # A name we do not recognise settles it: this is some other instrument,
        # not one of ours wearing an unfamiliar spelling.
        return None

    symbol = (trading_symbol or "").strip().upper().replace(" ", "").replace("-", "")
    if not symbol:
        return None
    if symbol in _ALIAS:
        return _ALIAS[symbol]
    for u in sorted(SUPPORTED, key=len, reverse=True):
        if not symbol.startswith(u):
            continue
        rest = symbol[len(u):]
        if rest and rest[0].isalpha():
            continue
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
        # Key -> the scrip-master row that produced it. Kept so a failed order
        # lookup can print what IS in the master beside what was asked for,
        # instead of only asserting the contract is absent.
        self._raw_by_key: dict[InstrumentKey, dict] = {}
        # Keys two different contracts both normalised to. Deliberately left
        # unbound; kept so the refusal can say "ambiguous" rather than "absent",
        # which are opposite problems with opposite fixes.
        self._ambiguous: dict[InstrumentKey, set[str]] = {}
        self.row_count = 0
        # Options specifically. `row_count` counts every binding including index
        # spots, and a load that produces the four NSE index spots and NOT ONE
        # tradable option reported itself as "✅ 4 instruments" — a success by
        # that count, and useless for an options terminal. Every order was then
        # rejected with "the list does not contain this contract", which reads
        # like a wrong expiry rather than a broken instrument load.
        self.option_count = 0
        self.loaded_from: str | None = None
        # Why the last load produced nothing usable. Carried so the ORDER path
        # can say what is actually wrong: "scrip master not loaded — reconnect
        # the account" is not a diagnosis, and it sent users round a loop of
        # reconnecting against a cause that reconnecting does not change.
        self.last_error: str | None = None

    # ── fetch ─────────────────────────────────────────────────────────────
    def _segment_rows(self, client: Any, segment: str) -> list[dict]:
        """Rows for one exchange segment, whatever shape the SDK hands back."""
        try:
            response = client.scrip_master(exchange_segment=segment)
        except Exception as e:
            self.last_error = f"{segment}: scrip_master() raised {type(e).__name__}: {e}"
            self._log("warn", f"⚠️  Kotak scrip master {segment} failed: {e}")
            return []
        # The SDK reports failure by RETURNING {"Error": ...} rather than raising.
        if isinstance(response, dict):
            self.last_error = f"{segment}: {response}"
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
            self.last_error = f"{segment}: CSV download failed — {type(e).__name__}: {e}"
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
        options_bound = 0
        # Every segment-level failure, kept for the whole load. Previously a
        # failing nse_fo set `last_error` and a later succeeding nse_cm cleared
        # it, so the ONE fact that explained the outage was thrown away before
        # anything could report it.
        failures: list[str] = []
        # F&O segments that answered with nothing at all and no error. Harmless
        # on its own (most accounts lack MCX/BSE derivatives); the whole story
        # when NO option segment produced anything.
        empty_option_segments: list[str] = []
        # The raw row behind each key, so a key claimed twice can be shown as a
        # FIELD-BY-FIELD diff of the two contracts that produced it. 18,204
        # option rows collapsing onto ~2,224 keys is not a lookup problem: it
        # means several distinct contracts are normalising to one identity, and
        # `bind_many` keeps only the last, so the bound symbol may not even be
        # the contract that was asked for.
        raw_by_key: dict[InstrumentKey, dict] = {}
        collisions: list[tuple[InstrumentKey, dict, dict]] = []
        nifty_sample: list[dict] = []
        # key -> every (trading symbol, token, segment) that claimed it.
        option_claims: dict[InstrumentKey, list[tuple[str, str, str]]] = {}
        self.last_error = None
        for segment in segments:
            # Cleared per segment so the check below distinguishes "this segment
            # ERRORED" from "this segment is simply not on this account".
            # Treating both as failures would put a warning on every healthy
            # load (few accounts carry MCX and BSE derivatives), which is how a
            # log stops being read.
            self.last_error = None
            rows = self._segment_rows(client, segment)
            if not rows:
                if self.last_error:
                    failures.append(self.last_error)
                else:
                    if segment in _OPTION_SEGMENTS:
                        empty_option_segments.append(segment)
                    self._log("info", f"[kotak-scrip] {segment}: no rows — segment "
                                      f"not available on this account")
                continue
            fetched += len(rows)
            drops = dict.fromkeys(_DROP_REASONS, 0)
            seg_options = 0
            seg_index = 0
            # Resolved per segment, not once for all of them: Kotak's MCX and
            # NSE masters are separate files and have not always agreed on
            # column naming, so borrowing the first segment's mapping can
            # silently drop a whole exchange.
            cols = _resolve_columns(rows[0])
            # An F&O segment additionally NEEDS strike and expiry. Leaving them
            # out of this check is what let a single unresolved `strike` column
            # discard 79,088 rows one at a time without ever failing: the
            # segment looked healthy, every option silently scored strike 0,
            # and `if strike <= 0: continue` did the rest.
            required = ("token", "instrument")
            if segment in _OPTION_SEGMENTS:
                required += ("strike", "expiry")
            missing = [k for k in required if k not in cols]
            self._log("info", f"[kotak-scrip] {segment}: {len(rows)} rows, columns resolved: "
                              + (", ".join(f"{k}->{v}" for k, v in sorted(cols.items()))
                                 or "(none)"))
            if missing:
                # The WHOLE header, not the first 12 columns: the column that
                # went missing here sits late in Kotak's master, so a truncated
                # header is exactly the one that cannot be diagnosed.
                header = list(rows[0])[:40]
                failures.append(f"{segment}: missing column(s) {missing} — header={header}")
                self._log("warn", f"⚠️  Kotak scrip master {segment} missing columns {missing}; "
                                  f"header={header}")
                continue

            def get(row: dict, key: str) -> str:
                col = cols.get(key)
                return str(row.get(col, "")).strip() if col else ""

            divisor, agreeing, example = (
                _strike_divisor(rows, cols["strike"], cols.get("trading_symbol"))
                if cols.get("strike") and cols.get("trading_symbol") else (0.0, 0, ""))
            if segment in _OPTION_SEGMENTS:
                if not divisor:
                    # Unmeasurable is NOT a licence to assume. Binding strikes
                    # at an unknown scale is how an order reaches the wrong
                    # contract; refusing the segment is recoverable.
                    failures.append(
                        f"{segment}: could not measure the strike scale — no row's "
                        f"trading symbol agreed with {cols['strike']!r} on a power of "
                        f"ten. The strike column or the symbol format has changed.")
                    self._log("error",
                              f"❌ Kotak scrip master {segment}: strike scale could not "
                              f"be measured; refusing to bind this segment rather than "
                              f"guess a scale.")
                    continue
                self._log("info",
                          f"[kotak-scrip] {segment}: strike scale ÷{divisor:g} measured "
                          f"from {agreeing} trading symbols"
                          + (f" (e.g. {example})" if example else ""))
            # Cash segments carry no options, so no scale is needed — but a
            # stray CE/PE row must never divide by zero.
            divisor = divisor or 1.0

            for row in rows:
                if not isinstance(row, dict):
                    continue
                token = get(row, "token")
                if not token:
                    drops["no_token"] += 1
                    continue
                trading_symbol = get(row, "trading_symbol")
                underlying = _canonical(get(row, "name"), trading_symbol)
                if underlying is None:
                    drops["unknown_underlying"] += 1
                    continue
                instrument = get(row, "instrument").upper()
                opt_type = get(row, "option_type").upper()
                is_option = opt_type in ("CE", "PE") or "OPT" in instrument

                if is_option:
                    raw_expiry = row.get(cols.get("expiry")) if cols.get("expiry") else ""
                    raw_strike = get(row, "strike")
                    expiry = _norm_expiry(raw_expiry)
                    if not expiry:
                        drops["unparsable_expiry"] += 1
                        continue
                    if expiry_filter.is_expired(expiry):
                        drops["already_expired"] += 1
                        continue
                    parsed = _num(get(row, "strike"))
                    if parsed is None:
                        drops["bad_strike"] += 1
                        continue
                    # Scale FIRST, then round to whole rupees — and round, not
                    # truncate: 1,237,500 paise / 100 can land a hair under
                    # 12,375.0 in binary floating point, and int() would file it
                    # as 12,374, i.e. a strike that does not exist.
                    strike = round(parsed / divisor)
                    if strike <= 0 or opt_type not in ("CE", "PE"):
                        drops["bad_strike"] += 1
                        continue
                    # The key is built only once the strike is fully normalised.
                    key = InstrumentKey.option(underlying, expiry, strike, opt_type)
                    seg = OPT_SEGMENT.get(underlying)
                elif "IDX" in instrument or "INDEX" in instrument:
                    key = InstrumentKey.index(underlying)
                    seg = SPOT_SEGMENT.get(underlying)
                else:
                    drops["not_option_or_index"] += 1
                    continue
                if seg is None:
                    drops["no_segment_mapping"] += 1
                    continue
                if is_option:
                    seg_options += 1
                    # What this row actually said, next to what it became.
                    snapshot = {
                        "trdSymbol": trading_symbol, "name": get(row, "name"),
                        "rawExpiry": str(raw_expiry), "expiry": expiry,
                        "rawStrike": str(raw_strike), "strike": key.strike,
                        "optType": opt_type, "token": token, "segment": segment,
                    }
                    previous = raw_by_key.get(key)
                    if previous is None:
                        raw_by_key[key] = snapshot
                    elif (len(collisions) < 5
                          and previous["trdSymbol"] != snapshot["trdSymbol"]):
                        collisions.append((key, previous, snapshot))
                    if underlying == "NIFTY" and len(nifty_sample) < 10:
                        nifty_sample.append(snapshot)
                    # Deferred on purpose: an option is bound only once every
                    # segment has been read and the key is known to belong to
                    # exactly ONE contract. Binding as we go meant the last row
                    # to claim a key silently won it. See the guard below.
                    option_claims.setdefault(key, []).append(
                        (trading_symbol or token, token, seg))
                    continue

                seg_index += 1
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

            # The line that makes the next failure diagnosable in one read:
            # what came in, what survived, and what killed the rest.
            dropped = ", ".join(f"{k}={v}" for k, v in drops.items() if v)
            self._log("info", f"[kotak-scrip] {segment}: {len(rows)} rows → "
                              f"{seg_options} options, {seg_index} indices"
                              + (f"; dropped {dropped}" if dropped else ""))
            options_bound += seg_options
            # An F&O segment that parsed cleanly and yielded no option is the
            # exact shape of this outage. Name the dominant drop reason: it is
            # the difference between a renamed column, an expiry format Kotak
            # changed, and an account with no derivatives entitlement.
            if segment in _OPTION_SEGMENTS and seg_options == 0:
                worst = max(drops.items(), key=lambda kv: kv[1]) if dropped else ("", 0)
                failures.append(
                    f"{segment}: parsed {len(rows)} rows but bound no option"
                    + (f" (mostly {worst[0]}={worst[1]})" if worst[1] else "")
                    + f"; columns resolved "
                    + (", ".join(f"{k}->{v}" for k, v in sorted(cols.items())) or "(none)"))

        # ── ambiguity guard ───────────────────────────────────────────────
        # A key claimed by two DIFFERENT trading symbols identifies two real
        # contracts that normalised to one identity. Binding either of them
        # would route an order to a contract the user did not choose — a wrong
        # expiry is a wrong instrument, with real money on it — and the old
        # "last row wins" made that choice silently and unrepeatably.
        #
        # Such keys are therefore left UNBOUND: the order is refused, naming
        # the ambiguity, which is recoverable. Repeated rows for one contract
        # (same trading symbol) are not ambiguous and bind normally.
        ambiguous: dict[InstrumentKey, set[str]] = {}
        for key, claims in option_claims.items():
            symbols = {primary for primary, _token, _seg in claims}
            if len(symbols) > 1:
                ambiguous[key] = symbols
                continue
            primary, token, seg = claims[0]
            seg_map[key] = seg
            bindings.append((key, primary))
            tokens[key] = token
            if token and token != primary:
                aliases.append((key, token))
        self._ambiguous = ambiguous

        # ── what the parse actually produced ──────────────────────────────
        # Printed every load, not behind a flag: the numbers that mattered
        # ("18204 options" vs "2230 instruments") were both already in the log
        # and their disagreement still went unread for a session.
        distinct_options = len(option_claims)
        if nifty_sample:
            self._log("info", "[kotak-scrip] first NIFTY options parsed "
                              "(raw → canonical):")
            for s in nifty_sample:
                self._log("info",
                          f"    {s['trdSymbol']}  name={s['name']!r} "
                          f"rawExpiry={s['rawExpiry']!r}→{s['expiry']} "
                          f"rawStrike={s['rawStrike']!r}→{s['strike']} "
                          f"optType={s['optType']} token={s['token']}")
        if collisions:
            lost = options_bound - distinct_options
            self._log("error",
                      f"❌ {lost:,} of {options_bound:,} Kotak option rows collapsed onto "
                      f"an already-bound key — {distinct_options:,} distinct contracts "
                      f"remain, of which {len(ambiguous):,} are claimed by more than one "
                      f"trading symbol and are therefore NOT bound (an order for them is "
                      f"refused rather than routed to a contract you did not choose).")
            for key, first, second in collisions:
                differing = [f for f in ("rawExpiry", "rawStrike", "optType", "name")
                             if first[f] != second[f]]
                self._log("error",
                          f"    key={key.position_id} claimed by {first['trdSymbol']} "
                          f"and {second['trdSymbol']}; fields that differ in the SOURCE: "
                          + (", ".join(f"{f}={first[f]!r} vs {second[f]!r}"
                                       for f in differing) or "(none — duplicate rows)"))

        self.segments = seg_map
        self._raw_by_key = raw_by_key
        self._bindings = bindings
        self._aliases = aliases
        self._tokens = tokens
        self.row_count = len(seg_map)
        self.option_count = options_bound
        self.loaded_from = f"csv ({fetched} rows across {len(segments)} segments)"

        # Success is measured in OPTIONS, not in bindings.
        #
        # The old test was `if not seg_map`, so a load that bound the four NSE
        # index spots and zero options passed, armed live trading, and left the
        # order path to reject every contract one at a time with a message that
        # blamed the expiry. Charticks trades options; a Kotak instrument list
        # with none in it has not loaded, whatever else came back with it.
        if not options_bound:
            if failures:
                self.last_error = "; ".join(failures)
            elif empty_option_segments:
                # Every F&O segment answered with an empty list and no error.
                # Not a parsing problem — there was nothing to parse.
                self.last_error = (
                    f"the derivatives segment(s) {', '.join(empty_option_segments)} "
                    f"returned no rows and no error"
                    + (f" (the cash segments did return {fetched} rows, so the session "
                       f"itself is working)" if fetched else "")
                    + " — check that this Kotak account has F&O enabled")
            elif fetched:
                self.last_error = (f"parsed {fetched} rows but recognised no tradable "
                                   f"option — the scrip master's column names have "
                                   f"probably changed (see the resolved columns above)")
            else:
                self.last_error = "no rows were returned for any exchange segment"
            self._log("error", f"❌ Kotak scrip master bound no tradable option from "
                               f"{fetched} rows ({len(seg_map)} non-option bindings) — "
                               f"{self.last_error}")
            return False

        # A cash/other segment can still have failed while options loaded. That
        # is not fatal, but it must not be erased — it is why an index price is
        # blank, and it belongs in the log and in `last_error`.
        self.last_error = "; ".join(failures) if failures else None
        if failures:
            self._log("warn", f"⚠️  Kotak scrip master loaded with problems: {self.last_error}")
        self._log("info", f"✅ Kotak scrip master: {len(seg_map)} instruments "
                          f"({options_bound} options) across {len(segments)} segments")
        return True

    def bindings(self) -> list[tuple[InstrumentKey, str]]:
        return list(self._bindings)

    def aliases(self) -> list[tuple[InstrumentKey, str]]:
        return list(self._aliases)

    def explain_miss(self, key: InstrumentKey) -> str:
        """Why `key` is not in the loaded master, in terms of what IS.

        "the list does not contain this contract" is an assertion, not a
        diagnosis: it cannot distinguish a wrong expiry from a wrong strike
        from an underlying that never parsed. This answers the three questions
        a person would ask next — is the underlying there at all, which
        expiries exist for it, and which strikes exist for the expiry asked
        for — using the same normalised values the lookup used.
        """
        if key in self._ambiguous:
            symbols = sorted(self._ambiguous[key])
            return (f"{len(symbols)} different Kotak contracts normalise to this same "
                    f"identity ({', '.join(symbols[:6])}"
                    + (" …" if len(symbols) > 6 else "") + "), so Charticks will not "
                    f"guess which one you meant. This is a parsing fault in Charticks, "
                    f"not a problem with your order — send broker.log")

        same_underlying = [k for k in self._raw_by_key if k.underlying == key.underlying]
        if not same_underlying:
            present = sorted({k.underlying for k in self._raw_by_key})
            return (f"no {key.underlying} option parsed at all; the master produced "
                    f"options for {', '.join(present) or '(nothing)'}")

        expiries = sorted({k.expiry for k in same_underlying})
        same_expiry = [k for k in same_underlying if k.expiry == key.expiry]
        if not same_expiry:
            return (f"{key.underlying} has {len(expiries)} expiries in the master "
                    f"and {key.expiry} is not one of them: {', '.join(expiries[:12])}"
                    + (" …" if len(expiries) > 12 else ""))

        strikes = sorted({k.strike for k in same_expiry if k.opt_type == key.opt_type})
        if key.strike not in strikes:
            # The nearest strikes ALWAYS, not only those within a fixed window:
            # when the scale is wrong every strike is far away, and an empty
            # "nearest" list hides the very evidence that says so. Seeing
            # `nearest [1850000, 1855000]` next to a requested 24600 names a
            # units bug at a glance.
            near = sorted(strikes, key=lambda s: abs(s - key.strike))[:6]
            # What the master stored for one of those, raw and normalised.
            sample = next((self._raw_by_key[k] for k in same_expiry
                           if k.opt_type == key.opt_type and k.strike == near[0]), None) if near else None
            elsewhere = sorted({k.expiry for k in same_underlying
                                if k.strike == key.strike and k.opt_type == key.opt_type})
            return (f"requested strike {key.strike}; {key.underlying} {key.expiry} "
                    f"{key.opt_type} lists {len(strikes)} strikes, range "
                    f"{strikes[0]}–{strikes[-1]}, nearest {sorted(near)}"
                    + (f" (e.g. {sample['trdSymbol']}: raw {sample['rawStrike']} → "
                       f"{sample['strike']})" if sample else "")
                    + (f". Strike {key.strike} DOES exist for expiries "
                       f"{', '.join(elsewhere[:8])}" if elsewhere else ""))

        types = sorted({k.opt_type for k in same_expiry if k.strike == key.strike})
        return (f"{key.underlying} {key.expiry} {key.strike} exists but only as "
                f"{', '.join(types) or '(no option type)'}, not {key.opt_type}")

    def token_for(self, key: InstrumentKey) -> str:
        """Kotak's numeric instrument token (pSymbol) for a contract, or "".

        Wanted by margin_required(), which takes a token rather than the trading
        symbol that place_order() takes.
        """
        return self._tokens.get(key, "")
