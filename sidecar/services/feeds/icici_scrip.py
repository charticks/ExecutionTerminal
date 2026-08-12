"""ICICI Direct (Breeze) security master -> canonical instrument bindings.

Same contract as the Dhan and Kotak equivalents: the only outputs are
``InstrumentKey -> ICICI token`` bindings under the "icici" namespace, the
stream prefix each instrument subscribes with, and the ICICI ``ShortName``
per underlying (which is what Breeze's order API calls ``stock_code``).
Nothing here touches any other broker's namespace, and the option chain
reads none of it directly — it asks ``services.instruments``.

Unlike Dhan's single CSV, ICICI publishes a ZIP of per-exchange CSVs at
https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip
(regenerated daily at 08:00). The extracted CSVs are cached daily under
the sidecar data dir (services.paths.data_dir), with fallback to the newest
cached day when the download fails, and every outcome is logged — a silently
stale master surfaces much later as an option chain quoting yesterday's strikes.

Column names are resolved by alias with the resolution logged (ICICI's headers
have carried surrounding quotes and stray spaces), so a schema drift shows up
as one readable log line instead of a silent zero-instrument catalogue. This
is the file most likely to need a one-line alias addition on first contact
with the real master.
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import io
import os
import zipfile
from typing import Any

from services.instruments import InstrumentKey

MASTER_URL = "https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip"

# Breeze stream-prefix per source file STEM: "X.Y!token" where X is the
# exchange (1=BSE, 4=NSE/NFO, 8=BFO) and Y=1 subscribes exchange quotes.
# Keyed by stem, not filename: the live ZIP ships these as .txt (verified
# 2026-08-05 — BSEScripMaster.txt etc.), though they are CSV-formatted inside,
# and older mirrors used .csv. Matching the stem tolerates both.
_STEM_PREFIX = {
    "NSEScripMaster": "4.1",
    "FONSEScripMaster": "4.1",
    "BSEScripMaster": "1.1",
    "FOBSEScripMaster": "8.1",
}
# Which stems carry options vs cash/index rows.
_FO_FILES = ("FONSEScripMaster", "FOBSEScripMaster")
_CASH_FILES = ("NSEScripMaster", "BSEScripMaster")

# ICICI naming -> Charticks canonical underlying. EXACT matches only — the
# ShortName/CompanyName values below were read from the live master
# (2026-08-05). Prefix matching is deliberately absent: "NIFTY NEXT 50" and
# "NIFTY MIDCAP SELECT" both start with NIFTY, and the first cut of this
# parser lumped their 3,600+ contracts into the NIFTY chain that way.
# CNXNIF is CNX NIFTY JUNIOR (Next 50) — NOT NIFTY 50; it must never map.
_UNDERLYING_ALIAS = {
    # FONSE ShortNames (verified)      # cash-master CompanyNames (verified)
    "NIFTY": "NIFTY",                  "NIFTY 50": "NIFTY",
    "CNXBAN": "BANKNIFTY",             "NIFTY BANK": "BANKNIFTY",
    "NIFFIN": "FINNIFTY",              "NIFTY FIN SERVICE": "FINNIFTY",
    "NIFTY FINANCIAL SERVICES INDEX": "FINNIFTY",
    "NIFSEL": "MIDCPNIFTY",            "NIFTY MID SELECT": "MIDCPNIFTY",
    "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
    "BSESEN": "SENSEX",                "BSE SENSEX": "SENSEX",
    "SENSEX": "SENSEX",
    "BANKEX": "BANKEX",
}
SUPPORTED = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}

_COLS = {
    "token": ("Token", "TOKEN", "ScripCode", "SC_CODE"),
    "short_name": ("ShortName", "SHORTNAME", "Short Name"),
    "series": ("Series", "SERIES"),
    "company": ("CompanyName", "COMPANYNAME", "ScripName", "InstrumentName"),
    "instrument": ("InstrumentType", "INSTRUMENTTYPE", "Instrument", "InstrumentName", "Series"),
    "expiry": ("ExpiryDate", "EXPIRYDATE", "Expiry Date", "ExpDate"),
    "strike": ("StrikePrice", "STRIKEPRICE", "Strike Price"),
    "option_type": ("OptionType", "OPTIONTYPE", "Option Type", "CE/PE"),
    "exchange_code": ("ExchangeCode", "EXCHANGECODE", "Exchange Code"),
    "lot_size": ("LotSize", "LOTSIZE", "Lot Size", "BoardLotQty"),
}

# Last-resort index tokens if the cash masters yield no recognisable index
# rows. Community-documented Breeze index subscription identifiers — used only
# behind a warning, and never for order placement.
_INDEX_FALLBACK = {
    "NIFTY": ("4.1", "NIFTY 50"),
    "BANKNIFTY": ("4.1", "NIFTY BANK"),
    "FINNIFTY": ("4.1", "NIFTY FIN SERVICE"),
    "MIDCPNIFTY": ("4.1", "NIFTY MID SELECT"),
    "SENSEX": ("1.1", "SENSEX"),
    "BANKEX": ("1.1", "BANKEX"),
}


def _clean(cell: Any) -> str:
    """ICICI CSV cells arrive with stray quotes and padding — normalise once."""
    return str(cell or "").strip().strip('"').strip("'").strip()


def _resolve_columns(header: list[str]) -> dict[str, str]:
    present = {_clean(h).upper(): h for h in header}
    out: dict[str, str] = {}
    for logical, aliases in _COLS.items():
        for alias in aliases:
            if alias.upper() in present:
                out[logical] = present[alias.upper()]
                break
    return out


def _norm_expiry(raw: str) -> str:
    """ICICI expiry ('02-Sep-2026', ISO, or with time) -> canonical '02SEP2026'.
    Unparseable values yield "" so the row is skipped, never mis-dated."""
    raw = _clean(raw)
    if not raw:
        return ""
    head = raw.split(" ")[0].split("T")[0]
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d%b%Y", "%d-%B-%Y"):
        try:
            return dt.datetime.strptime(head, fmt).strftime("%d%b%Y").upper()
        except ValueError:
            continue
    return ""


def _canonical(*candidates: str) -> str | None:
    """Exact alias lookup only — see the note on _UNDERLYING_ALIAS."""
    for c in candidates:
        c = _clean(c).upper()
        if c and c in _UNDERLYING_ALIAS:
            return _UNDERLYING_ALIAS[c]
    return None


class ICICIScripMaster:
    """Loads ICICI's master ZIP and exposes canonical bindings + prefixes."""

    def __init__(self, cache_dir: str, log) -> None:
        self._cache_dir = cache_dir
        self._log = log
        self.prefixes: dict[InstrumentKey, str] = {}      # key -> "4.1"/"8.1"/"1.1"
        self.stock_codes: dict[str, str] = {}             # underlying -> ShortName
        self._bindings: list[tuple[InstrumentKey, str]] = []
        self._aliases: list[tuple[InstrumentKey, str]] = []
        self.index_fallback_used = False
        self.row_count = 0
        self.loaded_from: str | None = None

    # ── acquisition ───────────────────────────────────────────────────────
    def _day_dir(self, day: str) -> str:
        return os.path.join(self._cache_dir, f"icici_scrip_master_{day}")

    def _newest_cached(self) -> str | None:
        dirs = sorted(glob.glob(os.path.join(self._cache_dir, "icici_scrip_master_*")))
        return dirs[-1] if dirs else None

    def load(self) -> bool:
        """Today's master if possible, else the newest cached day. Returns
        False only when neither yields any instruments."""
        os.makedirs(self._cache_dir, exist_ok=True)
        today = dt.datetime.now().strftime("%Y%m%d")
        day_dir = self._day_dir(today)

        if os.path.isdir(day_dir):
            try:
                if self._parse_dir(day_dir):
                    self.loaded_from = f"cache:{day_dir}"
                    self._log("info", f"📦 ICICI scrip master loaded from today's cache "
                                      f"({self.row_count} instruments)")
                    return True
                raise ValueError("cached master parsed to zero instruments")
            except Exception as e:
                self._log("warn", f"⚠️  ICICI scrip cache unusable ({e}) — re-downloading")

        try:
            files = self._download()
            # Write into a temp dir + rename so a crash mid-write can never
            # leave a half-cached day for tomorrow's fallback to choke on.
            tmp = day_dir + ".tmp"
            os.makedirs(tmp, exist_ok=True)
            for stem, text in files.items():
                with open(os.path.join(tmp, stem + ".csv"), "w", encoding="utf-8", newline="") as f:
                    f.write(text)
            if os.path.isdir(day_dir):
                import shutil
                shutil.rmtree(day_dir, ignore_errors=True)
            os.replace(tmp, day_dir)
            if not self._parse_dir(day_dir):
                raise ValueError("downloaded master parsed to zero instruments")
            self.loaded_from = "download"
            self._log("info", f"✅ ICICI scrip master downloaded ({self.row_count} instruments)")
            self._prune()
            return True
        except Exception as e:
            self._log("warn", f"⚠️  ICICI scrip master download failed ({e}) — "
                              "falling back to the last cached copy")

        stale = self._newest_cached()
        if not stale:
            self._log("error", "❌ No ICICI scrip master available (no download, no cache) — "
                               "the ICICI feed cannot resolve any instrument")
            return False
        try:
            if not self._parse_dir(stale):
                raise ValueError("cached master parsed to zero instruments")
            self.loaded_from = f"stale-cache:{stale}"
            self._log("warn", f"⚠️  Using STALE ICICI scrip master {os.path.basename(stale)} "
                              f"({self.row_count} instruments) — expiries may be out of date")
            return True
        except Exception as e:
            self._log("error", f"❌ Stale ICICI scrip master unusable ({e})")
            return False

    def _download(self) -> dict[str, str]:
        import requests
        r = requests.get(MASTER_URL, timeout=120,
                         headers={"User-Agent": "Charticks/1.0"})
        r.raise_for_status()
        out: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for info in z.infolist():
                stem = os.path.splitext(os.path.basename(info.filename))[0]
                if stem in _STEM_PREFIX:
                    out[stem] = z.read(info).decode("utf-8", "replace")
            if not out:
                raise ValueError(f"ZIP contained none of {sorted(_STEM_PREFIX)}; "
                                 f"entries={[i.filename for i in z.infolist()][:8]}")
        return out

    def _prune(self) -> None:
        import shutil
        for old in sorted(glob.glob(os.path.join(self._cache_dir, "icici_scrip_master_*")))[:-2]:
            shutil.rmtree(old, ignore_errors=True)

    # ── parsing ───────────────────────────────────────────────────────────
    def _parse_dir(self, day_dir: str) -> int:
        files: dict[str, str] = {}
        for stem in _STEM_PREFIX:
            for ext in (".csv", ".txt"):
                path = os.path.join(day_dir, stem + ext)
                if os.path.exists(path):
                    files[stem] = open(path, encoding="utf-8", errors="replace").read()
                    break
        return self._parse(files)

    def _parse(self, files: dict[str, str]) -> int:
        from services import expiry as expiry_filter

        bindings: list[tuple[InstrumentKey, str]] = []
        aliases: list[tuple[InstrumentKey, str]] = []
        prefixes: dict[InstrumentKey, str] = {}
        stock_codes: dict[str, str] = {}
        logged_cols: set[str] = set()

        def note_columns(name: str, cols: dict[str, str], sample: list[str]) -> None:
            if name in logged_cols:
                return
            logged_cols.add(name)
            self._log("info", f"[icici-scrip] {name} columns resolved: "
                              + ", ".join(f"{k}->{_clean(v)}" for k, v in sorted(cols.items())))
            missing = [k for k in ("token",) if k not in cols]
            if missing:
                self._log("warn", f"⚠️  ICICI {name} missing columns {missing}; "
                                  f"header sample={[_clean(h) for h in sample[:12]]}")

        # ── option rows (FO masters) ─────────────────────────────────────
        for name in _FO_FILES:
            text = files.get(name)
            if not text:
                continue
            prefix = _STEM_PREFIX[name]
            reader = csv.DictReader(io.StringIO(text))
            header = reader.fieldnames or []
            cols = _resolve_columns(header)
            note_columns(name, cols, header)

            def get(row: dict, key: str) -> str:
                col = cols.get(key)
                return _clean(row.get(col)) if col else ""

            for row in reader:
                token = get(row, "token")
                if not token:
                    continue
                instrument = get(row, "instrument").upper()
                # Futures and stock options are skipped; only index options.
                if instrument and "OPT" not in instrument:
                    continue
                short = get(row, "short_name")
                underlying = _canonical(short, get(row, "company"))
                if underlying is None:
                    continue
                opt_type_raw = get(row, "option_type").upper()
                opt_type = {"CE": "CE", "PE": "PE", "CALL": "CE", "PUT": "PE"}.get(opt_type_raw)
                if opt_type is None:
                    continue
                expiry = _norm_expiry(get(row, "expiry"))
                if not expiry or expiry_filter.is_expired(expiry):
                    continue
                try:
                    strike = float(get(row, "strike") or 0)
                except ValueError:
                    continue
                if strike <= 0:
                    continue
                key = InstrumentKey.option(underlying, expiry, strike, opt_type)
                prefixes[key] = prefix
                bindings.append((key, token))
                # Ticks arrive keyed "4.1!<token>" — alias the full form so the
                # tick path resolves without string surgery being load-bearing.
                aliases.append((key, f"{prefix}!{token}"))
                # ShortName doubles as Breeze's order-API stock_code.
                if short and underlying not in stock_codes:
                    stock_codes[underlying] = short

        # ── index rows (cash masters) ────────────────────────────────────
        for name in _CASH_FILES:
            text = files.get(name)
            if not text:
                continue
            prefix = _STEM_PREFIX[name]
            reader = csv.DictReader(io.StringIO(text))
            header = reader.fieldnames or []
            cols = _resolve_columns(header)
            note_columns(name, cols, header)

            def get(row: dict, key: str) -> str:
                col = cols.get(key)
                return _clean(row.get(col)) if col else ""

            for row in reader:
                token = get(row, "token")
                if not token:
                    continue
                company = get(row, "company")
                short = get(row, "short_name")
                # _canonical is exact-only, so ETF rows ("SBI-ETF NIFTY 50")
                # can never claim an index key.
                cand = _canonical(company, short)
                if cand is None:
                    continue
                key = InstrumentKey.index(cand)
                if key in prefixes:
                    continue  # first exact match wins
                prefixes[key] = prefix
                bindings.append((key, token))
                aliases.append((key, f"{prefix}!{token}"))

        # ── index fallback ───────────────────────────────────────────────
        missing_idx = [u for u in SUPPORTED
                       if InstrumentKey.index(u) not in prefixes]
        self.index_fallback_used = False
        if len(missing_idx) == len(SUPPORTED) and bindings:
            # No index rows recognised at all — bind the community-known
            # subscription names so spot LTPs (and so the ATM strike) still
            # work, and say so loudly.
            self.index_fallback_used = True
            self._log("warn", "⚠️  ICICI cash masters yielded no index rows — "
                              "using the built-in index token fallback")
            for underlying, (prefix, ident) in _INDEX_FALLBACK.items():
                key = InstrumentKey.index(underlying)
                prefixes[key] = prefix
                bindings.append((key, ident))
                aliases.append((key, f"{prefix}!{ident}"))
        elif missing_idx:
            self._log("warn", f"⚠️  No ICICI spot token for {', '.join(sorted(missing_idx))} — "
                              "their option chain cannot compute an ATM strike from this feed")

        self.prefixes = prefixes
        self.stock_codes = stock_codes
        self._bindings = bindings
        self._aliases = aliases
        self.row_count = len(prefixes)
        return self.row_count

    # ── outputs ───────────────────────────────────────────────────────────
    def bindings(self) -> list[tuple[InstrumentKey, str]]:
        return list(self._bindings)

    def aliases(self) -> list[tuple[InstrumentKey, str]]:
        return list(self._aliases)

    def stock_code_for(self, underlying: str) -> str | None:
        """Breeze order-API stock_code for an underlying, from the master's
        ShortName column. None when the master has not resolved it — callers
        must treat that as 'cannot place', not guess."""
        return self.stock_codes.get((underlying or "").upper())
