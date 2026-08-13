"""Dhan's scrip master: download, daily cache, and canonical binding.

Kept entirely separate from ``BrokerManager.instrument_master`` (Angel's JSON),
which is deliberately left untouched. The two brokers name the same contract
differently, and the whole point of ``services.instruments`` is that neither
has to know the other's vocabulary — this module's only output is a set of
``InstrumentKey -> Dhan securityId`` bindings under the "dhan" namespace, plus
the exchange segment each instrument must be subscribed on.

Availability rules, per the agreed behaviour:
  * refresh once a day into the sidecar data dir (services.paths.data_dir);
  * a failed download falls back to the newest cached file rather than leaving
    the feed with no instruments;
  * every outcome is logged, because a silently stale master shows up much
    later as an option chain quoting yesterday's strikes.

Column names differ between Dhan's compact master (``SEM_*`` prefixed) and its
detailed one (unprefixed), and both have changed shape before, so columns are
resolved by alias rather than position and the resolution is logged.
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import io
import os

from services.instruments import InstrumentKey

# Dhan publishes both; the detailed master carries the underlying symbol, which
# the compact one only encodes inside the trading symbol.
DETAILED_URL = "https://images.dhan.co/api-scrip-master-detailed.csv"
COMPACT_URL = "https://images.dhan.co/api-scrip-master.csv"

# MarketFeed exchange-segment codes (mirrored from dhanhq.marketfeed so this
# module does not need the SDK imported just to name a constant).
SEG_IDX = 0
SEG_NSE = 1
SEG_NSE_FNO = 2
SEG_BSE = 4
SEG_MCX = 5
SEG_BSE_FNO = 8

# The SAME segments as the REST API names them. Dhan's order endpoints take a
# string ("NSE_FNO"), its market feed takes an int (2) — the two are not
# interchangeable, and an order sent with a feed code is rejected. Derived from
# the master's own classification rather than a second table keyed by
# underlying, so the feed and the order router cannot drift apart.
REST_SEGMENT = {SEG_NSE_FNO: "NSE_FNO", SEG_BSE_FNO: "BSE_FNO", SEG_MCX: "MCX_COMM",
                SEG_NSE: "NSE_EQ", SEG_BSE: "BSE_EQ"}

# Dhan spells some index underlyings differently from Charticks' canonical name.
_UNDERLYING_ALIAS = {
    "NIFTY 50": "NIFTY", "NIFTY50": "NIFTY",
    "NIFTY BANK": "BANKNIFTY", "BANKNIFTY": "BANKNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY", "FINNIFTY": "FINNIFTY",
    "NIFTY MID SELECT": "MIDCPNIFTY", "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX", "BANKEX": "BANKEX", "CRUDEOIL": "CRUDEOIL",
}
SUPPORTED = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "CRUDEOIL"}

# Column aliases, most specific first.
_COLS = {
    "security_id": ("SEM_SMST_SECURITY_ID", "SECURITY_ID"),
    "exchange": ("SEM_EXM_EXCH_ID", "EXCH_ID"),
    "segment": ("SEM_SEGMENT", "SEGMENT"),
    "instrument": ("SEM_INSTRUMENT_NAME", "INSTRUMENT", "INSTRUMENT_TYPE"),
    "underlying": ("UNDERLYING_SYMBOL", "SEM_UNDERLYING_SYMBOL", "SYMBOL_NAME",
                   "SM_SYMBOL_NAME", "SEM_TRADING_SYMBOL"),
    "display": ("SEM_CUSTOM_SYMBOL", "DISPLAY_NAME", "SEM_TRADING_SYMBOL"),
    "expiry": ("SEM_EXPIRY_DATE", "SM_EXPIRY_DATE", "EXPIRY_DATE"),
    "strike": ("SEM_STRIKE_PRICE", "STRIKE_PRICE"),
    "option_type": ("SEM_OPTION_TYPE", "OPTION_TYPE"),
}


def _resolve_columns(header: list[str]) -> dict[str, str]:
    """Map our logical fields onto whichever column names this file uses."""
    present = {h.strip().upper(): h for h in header}
    out: dict[str, str] = {}
    for logical, aliases in _COLS.items():
        for alias in aliases:
            if alias in present:
                out[logical] = present[alias]
                break
    return out


def _norm_expiry(raw: str) -> str:
    """Dhan dates ('2026-09-02', or with a time component) -> Charticks'
    canonical '02SEP2026', the same form Angel uses and InstrumentKey stores."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    head = raw.split(" ")[0].split("T")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y", "%d%b%Y"):
        try:
            return dt.datetime.strptime(head, fmt).strftime("%d%b%Y").upper()
        except ValueError:
            continue
    return raw.upper()


def _canonical_underlying(*candidates: str) -> str | None:
    for c in candidates:
        c = (c or "").strip().upper()
        if not c:
            continue
        if c in _UNDERLYING_ALIAS:
            return _UNDERLYING_ALIAS[c]
        if c in SUPPORTED:
            return c
        # Option trading symbols embed the underlying as a prefix
        # ("NIFTY 02 SEP 24000 CALL", "BANKNIFTY-Sep2026-52000-CE").
        for name in SUPPORTED:
            if c.replace(" ", "").replace("-", "").startswith(name):
                return name
    return None


def _segment_for(exchange: str, instrument: str, is_option: bool) -> int | None:
    exchange = (exchange or "").strip().upper()
    instrument = (instrument or "").strip().upper()
    if "INDEX" in instrument and not is_option:
        return SEG_IDX
    if is_option or "OPT" in instrument or "FUT" in instrument:
        if exchange in ("NSE",):
            return SEG_NSE_FNO
        if exchange in ("BSE",):
            return SEG_BSE_FNO
        if exchange in ("MCX",):
            return SEG_MCX
    if exchange == "NSE":
        return SEG_NSE
    if exchange == "BSE":
        return SEG_BSE
    if exchange == "MCX":
        return SEG_MCX
    return None


class DhanScripMaster:
    """Loads Dhan's master and exposes canonical bindings + segments."""

    def __init__(self, cache_dir: str, log) -> None:
        self._cache_dir = cache_dir
        self._log = log
        # InstrumentKey -> Dhan exchange-segment code, so the feed knows which
        # segment to subscribe each instrument on.
        self.segments: dict[InstrumentKey, int] = {}
        # (InstrumentKey, securityId) pairs for the instruments registry.
        self._bindings: list[tuple[InstrumentKey, str]] = []
        self.loaded_from: str | None = None
        self.row_count = 0

    # ── acquisition ───────────────────────────────────────────────────────
    def _cache_path(self, day: str) -> str:
        return os.path.join(self._cache_dir, f"dhan_scrip_master_{day}.csv")

    def _newest_cached(self) -> str | None:
        files = sorted(glob.glob(os.path.join(self._cache_dir, "dhan_scrip_master_*.csv")))
        return files[-1] if files else None

    def load(self) -> bool:
        """Today's master if possible, else the newest cached one. Returns
        False only when neither is available."""
        os.makedirs(self._cache_dir, exist_ok=True)
        today = dt.datetime.now().strftime("%Y%m%d")
        path = self._cache_path(today)

        if os.path.exists(path):
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
                if self._parse(text):
                    self.loaded_from = f"cache:{path}"
                    self._log("info", f"📦 Dhan scrip master loaded from today's cache "
                                      f"({self.row_count} instruments)")
                    return True
                raise ValueError("cached file parsed to zero instruments")
            except Exception as e:
                self._log("warn", f"⚠️  Dhan scrip cache unusable ({e}) — re-downloading")

        try:
            text = self._download()
            if not self._parse(text):
                raise ValueError("download parsed to zero instruments")
            # Temp-file + replace so a crash mid-write cannot leave a truncated
            # cache for tomorrow's fallback to choke on.
            tmp = path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                os.replace(tmp, path)
            except OSError as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                self._log("warn", f"⚠️  Could not cache Dhan scrip master ({e}) — continuing")
            self.loaded_from = "download"
            self._log("info", f"✅ Dhan scrip master downloaded ({self.row_count} instruments)")
            self._prune()
            return True
        except Exception as e:
            self._log("warn", f"⚠️  Dhan scrip master download failed ({e}) — "
                              "falling back to the last cached copy")

        stale = self._newest_cached()
        if not stale:
            self._log("error", "❌ No Dhan scrip master available (no download, no cache) — "
                               "the Dhan feed cannot resolve any instrument")
            return False
        try:
            text = open(stale, encoding="utf-8", errors="replace").read()
            if not self._parse(text):
                raise ValueError("cached file parsed to zero instruments")
            self.loaded_from = f"stale-cache:{stale}"
            self._log("warn", f"⚠️  Using STALE Dhan scrip master {os.path.basename(stale)} "
                              f"({self.row_count} instruments) — expiries may be out of date")
            return True
        except Exception as e:
            self._log("error", f"❌ Stale Dhan scrip master unusable ({e})")
            return False

    def _download(self) -> str:
        import requests
        last: Exception | None = None
        for url in (DETAILED_URL, COMPACT_URL):
            try:
                r = requests.get(url, timeout=90,
                                 headers={"User-Agent": "Charticks/1.0"})
                r.raise_for_status()
                text = r.content.decode("utf-8", "replace")
                if text.strip():
                    return text
                last = ValueError(f"{url} returned an empty body")
            except Exception as e:
                last = e
        raise last or RuntimeError("no scrip master URL succeeded")

    def _prune(self) -> None:
        for old in sorted(glob.glob(os.path.join(self._cache_dir, "dhan_scrip_master_*.csv")))[:-2]:
            try:
                os.remove(old)
            except OSError:
                pass

    # ── parsing ───────────────────────────────────────────────────────────
    def _parse(self, text: str) -> int:
        from services import expiry as expiry_filter

        reader = csv.DictReader(io.StringIO(text))
        header = reader.fieldnames or []
        cols = _resolve_columns(header)
        missing = [k for k in ("security_id", "instrument") if k not in cols]
        if missing:
            raise ValueError(f"scrip master missing columns {missing}; header={header[:12]}")
        self._log("info", f"[dhan-scrip] columns resolved: "
                          + ", ".join(f"{k}->{v}" for k, v in sorted(cols.items())))

        def get(row: dict, key: str) -> str:
            col = cols.get(key)
            return (row.get(col) or "").strip() if col else ""

        segments: dict[InstrumentKey, int] = {}
        # Rebuilt wholesale on every parse — a reload must not inherit rows the
        # new master dropped (expired contracts, delisted strikes).
        bindings: list[tuple[InstrumentKey, str]] = []
        for row in reader:
            sec_id = get(row, "security_id")
            if not sec_id:
                continue
            instrument = get(row, "instrument").upper()
            opt_type = get(row, "option_type").upper()
            is_option = opt_type in ("CE", "PE") or "OPT" in instrument
            underlying = _canonical_underlying(
                get(row, "underlying"), get(row, "display"))
            if underlying is None:
                continue
            exchange = get(row, "exchange")
            segment = _segment_for(exchange, instrument, is_option)
            if segment is None:
                continue

            if is_option:
                expiry = _norm_expiry(get(row, "expiry"))
                if not expiry or expiry_filter.is_expired(expiry):
                    # Same rule as the Angel path: the broker keeps listing
                    # yesterday's contracts and they must never resolve.
                    continue
                try:
                    strike = int(float(get(row, "strike") or 0))
                except ValueError:
                    continue
                if strike <= 0 or opt_type not in ("CE", "PE"):
                    continue
                key = InstrumentKey.option(underlying, expiry, strike, opt_type)
            elif "INDEX" in instrument:
                key = InstrumentKey.index(underlying)
            else:
                continue
            segments[key] = segment
            bindings.append((key, sec_id))

        self.segments = segments
        self._bindings = bindings
        self.row_count = len(bindings)
        return self.row_count

    def bindings(self) -> list[tuple[InstrumentKey, str]]:
        """(InstrumentKey, securityId) pairs for the instruments registry."""
        return list(self._bindings)

    def rest_segment_for(self, key: InstrumentKey) -> str:
        """The REST exchange segment for a contract, or "" if not in the master.

        Empty is a refusal, not a default: guessing NSE_FNO for a commodity
        would route a CRUDEOIL order to the wrong exchange.
        """
        return REST_SEGMENT.get(self.segments.get(key, -1), "")
