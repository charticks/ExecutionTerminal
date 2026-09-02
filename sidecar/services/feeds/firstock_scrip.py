"""Firstock's instrument master: download, daily cache, and canonical binding.

Firstock publishes its symbol files as plain CSV over unauthenticated HTTP, one
per exchange segment, plus a pre-filtered ``Indices`` file that contains exactly
the index derivatives Charticks trades — OPTIDX and FUTIDX across NFO and BFO.
At ~1.3 MB that file is a fortieth of Angel's master and a sixth of the full
NFO one, and every row in it is a contract we might actually quote, so it is
the only file this module downloads. Freeze quantities live in the per-segment
NFO/BFO files and are not needed until live order splitting; they are
deliberately not fetched here.

Output is the same as every other scrip master: ``InstrumentKey -> token``
bindings under the "firstock" namespace, plus the exchange each contract must
be subscribed on. Nothing above this module learns a Firstock field name.

Two Firstock-specific hazards are handled here rather than being left to the
feed:

* **The subscribe token is composite.** Firstock's socket addresses an
  instrument as ``EXCHANGE:TOKEN`` ("NFO:35085") and echoes both halves back on
  every tick, so that composite — not the bare number — is what gets bound as
  the primary token. The bare token is registered as an alias too, because the
  REST position book reports it on its own (see ``alias`` in
  services.instruments, which exists for exactly this Kotak-shaped problem).

* **Three trading-symbol conventions coexist.** NFO writes
  ``NIFTY29SEP26C29150``; BFO monthly writes ``BANKEX26AUG54000CE``; BFO weekly
  writes ``SENSEX26O0170700CE``. They are never parsed to recover a contract —
  every field comes from its own column — but they ARE used, read-only, to
  measure the strike scale below.
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

# The pre-filtered index-derivatives file. See the module docstring for why this
# and not the full NFO/BFO masters.
INDICES_URL = "https://api.firstock.in/V1/symbols/Indices"

# Underlyings Charticks trades. Matched EXACTLY against the master's own
# `Symbol` column — never by prefix. The file contains NIFTYNXT50, NIFTYFPI and
# SENSEX50 alongside NIFTY and SENSEX, and "NIFTYNXT50".startswith("NIFTY") is
# the precise mistake that filed NIFTY NEXT 50 contracts as NIFTY in the Kotak
# loader, putting 84,200-strike contracts into NIFTY's strike space.
#
# CRUDEOIL is absent on purpose: Firstock publishes no MCX segment at all
# (/V1/symbols/MCX returns 404), so Crude cannot be quoted or traded here. The
# feed reports that once rather than warning on every load.
SUPPORTED = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}

# Columns this loader needs, by logical name. Resolved per file rather than by
# position: the Indices file carries 10 columns, the NFO/BFO files 12 (they add
# CompanyName and FreezeQty) and the NSE file a different 8 again, so a fixed
# index would silently read the wrong field if the source file were ever
# switched.
_COLS = {
    "exchange": ("Exchange",),
    "token": ("Token",),
    "lot_size": ("LotSize",),
    "symbol": ("Symbol",),
    "trading_symbol": ("TradingSymbol",),
    "expiry": ("Expiry",),
    "instrument": ("Instrument",),
    "option_type": ("OptionType",),
    "strike": ("StrikePrice",),
    "tick_size": ("TickSize",),
}

# Without these an option row cannot be turned into a contract, so their absence
# fails the whole load loudly. Leaving strike out of a list like this is what let
# one unresolved column in the Kotak master discard 79,088 rows one at a time
# while the load reported success.
#
# `trading_symbol` is required too, and not only because orders will need it
# later: it is the independent witness the strike scale is measured against
# below, and without it the scale could only be assumed.
_REQUIRED = ("exchange", "token", "symbol", "expiry", "instrument",
             "option_type", "strike", "trading_symbol")

_CACHE_PREFIX = "firstock_symbols_"
_CACHE_KEEP = 2

# The strike inside a Firstock trading symbol, under either convention:
#   NIFTY29SEP26C29150   -> C/P then the strike, at the end
#   BANKEX26AUG54000CE   -> the strike, then CE/PE, at the end
_SYMBOL_STRIKE_RES = (
    re.compile(r"[CP](\d+(?:\.\d+)?)$"),
    re.compile(r"(\d+(?:\.\d+)?)(?:CE|PE)$"),
)

# Scales worth recognising. Anything else is not a units difference, it is a
# misread column, and must not be silently accepted.
_PLAUSIBLE_SCALES = (1, 10, 100, 1000)

# Enough agreeing rows to be certain without walking the whole file.
_SCALE_SAMPLE = 400


def _squash(name: str) -> str:
    """Header name reduced to its letters and digits.

    Kotak shipped a header spelled ``pStrikePrice;`` — punctuation included —
    which matched no alias and cost a trading session. Firstock's headers are
    clean today; squashing makes that irrelevant rather than lucky.
    """
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _resolve_columns(header: list[str]) -> dict[str, str]:
    present = {str(h).strip(): h for h in header}
    squashed: dict[str, str] = {}
    for key, original in present.items():
        squashed.setdefault(_squash(key), original)
    out: dict[str, str] = {}
    for logical, aliases in _COLS.items():
        for alias in aliases:
            if alias in present:
                out[logical] = present[alias]
                break
            if _squash(alias) in squashed:
                out[logical] = squashed[_squash(alias)]
                break
    return out


def _num(raw: Any) -> float | None:
    """The number in a cell, or None. Tolerates stray punctuation and blanks."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _norm_expiry(raw: str) -> str:
    """Firstock's ``29-SEP-2026`` -> Charticks' canonical ``29SEP2026``.

    Returns "" rather than the raw text on an unrecognised format: an expiry we
    cannot parse must drop the row, because a contract keyed by an unparseable
    expiry can never be matched by anything else in the app.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    for fmt in ("%d-%b-%Y", "%d%b%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(text.upper(), fmt).strftime("%d%b%Y").upper()
        except ValueError:
            continue
    return ""


def _strike_divisor(rows: list[dict], strike_col: str,
                    symbol_col: str) -> tuple[float, int, str]:
    """MEASURE the scale of the strike column. Never infer it.

    Inspection on 18 Aug 2026 says Firstock quotes strikes in whole rupees, so
    this should return 1. It is measured anyway, because "it was rupees when we
    looked" is not a property that survives unattended — and the alternative,
    assuming a scale, is exactly what put every NIFTY strike at 1,840,000 in the
    Kotak loader while orders asked for 18,400.

    The trading symbol carries the strike in rupees under both of Firstock's
    conventions, so the ratio between the column and that number IS the scale,
    read off the data. A ratio counts only within 1% of a plausible power of
    ten, so a row whose symbol does not actually end in a strike cannot drag the
    measurement anywhere.

    Returns (divisor, rows agreeing, an example) so the choice can be logged.
    """
    votes: dict[int, int] = {}
    example = ""
    for row in rows:
        raw = _num(row.get(strike_col))
        if not raw or raw <= 0:
            continue
        symbol = str(row.get(symbol_col, "")).strip().upper()
        shown = None
        for pattern in _SYMBOL_STRIKE_RES:
            match = pattern.search(symbol)
            if match:
                shown = float(match.group(1))
                break
        if not shown or shown <= 0:
            continue
        ratio = raw / shown
        for scale in _PLAUSIBLE_SCALES:
            if abs(ratio - scale) <= scale * 0.01:
                votes[scale] = votes.get(scale, 0) + 1
                if not example:
                    example = f"{symbol} raw {row.get(strike_col)} -> {int(shown)}"
                break
        if sum(votes.values()) >= _SCALE_SAMPLE:
            break
    if not votes:
        return 0.0, 0, ""
    scale, agreeing = max(votes.items(), key=lambda kv: kv[1])
    return float(scale), agreeing, example


class FirstockScripMaster:
    """Loads Firstock's index-derivatives master and exposes canonical bindings.

    One instance per feed. `load()` is safe to call repeatedly; the feed calls
    it at most once a day.
    """

    def __init__(self, cache_dir: str, log) -> None:
        self._cache_dir = cache_dir
        self._log = log

        # InstrumentKey -> "EXCHANGE:TOKEN", the string the socket subscribes.
        self.subscribe_ids: dict[InstrumentKey, str] = {}
        # InstrumentKey -> the facts an ORDER needs. Firstock places orders by
        # exchange + tradingSymbol, not by token, so the symbol has to survive
        # the parse rather than being reconstructed later — its three encodings
        # (NFO, BFO monthly, BFO weekly) make reconstruction a guess.
        self.contracts: dict[InstrumentKey, dict] = {}
        # (InstrumentKey, "EXCHANGE:TOKEN") for the registry's primary binding.
        self._bindings: list[tuple[InstrumentKey, str]] = []
        # (InstrumentKey, bare token) — an additional id that resolves to the
        # same key without displacing the composite one.
        self._aliases: list[tuple[InstrumentKey, str]] = []
        # Contract facts the rest of the app may want, keyed canonically.
        self.lot_sizes: dict[str, int] = {}

        # Keys two different contracts both normalised to. Deliberately left
        # unbound; kept so a caller can say "ambiguous" rather than "absent",
        # which are opposite problems with opposite fixes.
        self.ambiguous: dict[InstrumentKey, set[str]] = {}

        self.loaded_from: str | None = None
        self.row_count = 0        # every binding, including futures
        self.option_count = 0     # options specifically — the health metric
        self.last_error: str | None = None

    # ── acquisition ───────────────────────────────────────────────────────
    def _cache_path(self, day: str) -> str:
        return os.path.join(self._cache_dir, f"{_CACHE_PREFIX}{day}.csv")

    def _newest_cached(self) -> str | None:
        files = sorted(glob.glob(os.path.join(self._cache_dir, f"{_CACHE_PREFIX}*.csv")))
        return files[-1] if files else None

    def load(self) -> bool:
        """Today's master if possible, else the newest cached one.

        Returns False only when neither yields a usable set of options. Mirrors
        DhanScripMaster.load() deliberately — a feed that behaves differently
        from its siblings on a bad download day is a feed nobody can reason
        about during an incident.
        """
        os.makedirs(self._cache_dir, exist_ok=True)
        today = dt.datetime.now().strftime("%Y%m%d")
        path = self._cache_path(today)
        self.last_error = None

        if os.path.exists(path):
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
                if self._parse(text):
                    self.loaded_from = f"cache:{os.path.basename(path)}"
                    self._log("info", f"📦 Firstock symbols loaded from today's cache "
                                      f"({self.option_count} options)")
                    return True
                raise ValueError("cached file bound no option")
            except Exception as e:
                self._log("warn", f"⚠️  Firstock symbol cache unusable ({e}) — re-downloading")

        try:
            text = self._download()
            if not self._parse(text):
                raise ValueError(self.last_error or "download bound no option")
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
                self._log("warn", f"⚠️  Could not cache Firstock symbols ({e}) — continuing")
            self.loaded_from = "download"
            self._log("info", f"✅ Firstock symbols downloaded "
                              f"({self.option_count} options, {self.row_count} instruments)")
            self._prune()
            return True
        except Exception as e:
            self.last_error = str(e)
            self._log("warn", f"⚠️  Firstock symbol download failed ({e}) — "
                              f"falling back to the last cached copy")

        stale = self._newest_cached()
        if not stale:
            self.last_error = (self.last_error
                               or "no download succeeded and no cache exists")
            self._log("error", "❌ No Firstock symbol master available (no download, no "
                               "cache) — the Firstock feed cannot resolve any instrument")
            return False
        try:
            text = open(stale, encoding="utf-8", errors="replace").read()
            if not self._parse(text):
                raise ValueError(self.last_error or "cached file bound no option")
            self.loaded_from = f"stale-cache:{os.path.basename(stale)}"
            self._log("warn", f"⚠️  Using STALE Firstock symbols {os.path.basename(stale)} "
                              f"({self.option_count} options) — expiries may be out of date")
            return True
        except Exception as e:
            self.last_error = str(e)
            self._log("error", f"❌ Stale Firstock symbol master unusable ({e})")
            return False

    def _download(self) -> str:
        import requests

        # `ref` is what Firstock's own documentation links carry; sent so the
        # request looks like the documented one rather than an unknown client.
        r = requests.get(INDICES_URL, params={"ref": "firstock.in"}, timeout=90,
                         headers={"User-Agent": "Charticks/1.0"})
        r.raise_for_status()
        text = r.content.decode("utf-8", "replace")
        if not text.strip():
            raise ValueError(f"{INDICES_URL} returned an empty body")
        return text

    def _prune(self) -> None:
        pattern = os.path.join(self._cache_dir, f"{_CACHE_PREFIX}*.csv")
        for old in sorted(glob.glob(pattern))[:-_CACHE_KEEP]:
            try:
                os.remove(old)
            except OSError:
                pass

    # ── parsing ───────────────────────────────────────────────────────────
    def _parse(self, text: str) -> int:
        """Parse a symbols CSV into bindings. Returns the OPTION count.

        Options, not rows: a load that produced only futures would be reported
        as a success by any row-based count and is useless for an options
        terminal. The Kotak master did exactly that — "✅ 4 instruments", zero
        options, live trading armed, every order rejected.
        """
        from services import expiry as expiry_filter

        reader = csv.DictReader(io.StringIO(text))
        header = [h for h in (reader.fieldnames or []) if h]
        cols = _resolve_columns(header)
        missing = [k for k in _REQUIRED if k not in cols]
        if missing:
            raise ValueError(f"Firstock symbols missing column(s) {missing}; "
                             f"header={header}")
        self._log("info", "[firstock-scrip] columns resolved: "
                          + ", ".join(f"{k}->{v}" for k, v in sorted(cols.items())))

        rows = [r for r in reader if isinstance(r, dict)]
        if not rows:
            raise ValueError("Firstock symbols file contained no data rows")

        def get(row: dict, key: str) -> str:
            col = cols.get(key)
            return (row.get(col) or "").strip() if col else ""

        # Scale is measured before anything is bound, and an unmeasurable scale
        # refuses the file. Binding strikes at an unknown scale is how an order
        # reaches the wrong contract; refusing is recoverable.
        #
        # Measured over OPTION rows only, and only consulted when there are
        # any. A file holding nothing but futures has no strikes to measure and
        # is not a scale failure — it is a file with no options in it, which the
        # zero-option check below reports precisely. Refusing it here instead
        # would answer a question nobody asked and hide the real one.
        option_rows = [r for r in rows
                       if get(r, "option_type").upper() in ("CE", "PE")]
        divisor = 1.0
        if option_rows:
            divisor, agreeing, example = _strike_divisor(
                option_rows, cols["strike"], cols["trading_symbol"])
            if not divisor:
                raise ValueError(
                    "could not measure the strike scale — none of the "
                    f"{len(option_rows)} option rows' trading symbols agreed with "
                    f"{cols['strike']!r} on a power of ten. The strike column or "
                    "the symbol format has changed.")
            self._log("info", f"[firstock-scrip] strike scale ÷{divisor:g} measured from "
                              f"{agreeing} of {len(option_rows)} option rows"
                              + (f" (e.g. {example})" if example else ""))

        # Why rows were discarded. Counted and logged because "0 options" alone
        # cannot distinguish a renamed column from a changed expiry format from
        # a file that genuinely holds nothing we trade.
        drops = {"unknown_underlying": 0, "not_option": 0, "unparsable_expiry": 0,
                 "already_expired": 0, "bad_strike": 0, "fractional_strike": 0,
                 "no_token": 0}

        # key -> [contract dict], so a key claimed by two DIFFERENT contracts
        # can be detected and left unbound rather than awarded to whichever row
        # happened to come last.
        claims: dict[InstrumentKey, list[dict]] = {}
        lot_sizes: dict[str, int] = {}
        futures: list[tuple[InstrumentKey, str, str]] = []

        for row in rows:
            token = get(row, "token")
            if not token:
                drops["no_token"] += 1
                continue
            underlying = get(row, "symbol").upper()
            if underlying not in SUPPORTED:
                drops["unknown_underlying"] += 1
                continue
            exchange = get(row, "exchange").upper()
            composite = f"{exchange}:{token}"
            instrument = get(row, "instrument").upper()
            opt_type = get(row, "option_type").upper()
            trading_symbol = get(row, "trading_symbol") or token

            if opt_type not in ("CE", "PE"):
                # FUTIDX rows land here. Kept separately: they are never part of
                # the option catalogue, but the front-month future is how an
                # index with no cash quote gets an underlying price.
                if "FUT" in instrument:
                    expiry = _norm_expiry(get(row, "expiry"))
                    if expiry and not expiry_filter.is_expired(expiry):
                        futures.append(
                            (InstrumentKey.future(underlying, expiry), composite, token))
                drops["not_option"] += 1
                continue

            expiry = _norm_expiry(get(row, "expiry"))
            if not expiry:
                drops["unparsable_expiry"] += 1
                continue
            if expiry_filter.is_expired(expiry):
                # Firstock keeps listing yesterday's contracts for a while after
                # the open; services.expiry is authoritative, not the master.
                drops["already_expired"] += 1
                continue

            raw_strike = _num(get(row, "strike"))
            if raw_strike is None or raw_strike <= 0:
                drops["bad_strike"] += 1
                continue
            scaled = raw_strike / divisor
            # Index strikes are whole rupees. A fractional one is either a stock
            # option that should not be in this file or a scale we have misread,
            # and truncating it would invent a strike that does not exist — so
            # it is dropped and counted rather than rounded away.
            if abs(scaled - round(scaled)) > 1e-6:
                drops["fractional_strike"] += 1
                continue
            strike = int(round(scaled))
            if strike <= 0:
                drops["bad_strike"] += 1
                continue

            lot = _num(get(row, "lot_size"))
            if lot and lot > 0 and underlying not in lot_sizes:
                lot_sizes[underlying] = int(lot)

            key = InstrumentKey.option(underlying, expiry, strike, opt_type)
            claims.setdefault(key, []).append({
                "exchange": exchange,
                "tradingSymbol": trading_symbol,
                "token": token,
                "subscribeId": composite,
                "lotSize": int(lot) if lot and lot > 0 else 0,
                "tickSize": _num(get(row, "tick_size")) or 0.0,
            })

        # ── ambiguity guard ───────────────────────────────────────────────
        # A key claimed by two DIFFERENT trading symbols identifies two real
        # contracts that normalised to one identity. Binding either would route
        # a subscription — and later an order — to a contract the user did not
        # choose. Such keys are left UNBOUND so the miss is reported honestly.
        # Repeated rows for ONE contract are not ambiguous and bind normally.
        subscribe_ids: dict[InstrumentKey, str] = {}
        contracts: dict[InstrumentKey, dict] = {}
        bindings: list[tuple[InstrumentKey, str]] = []
        aliases: list[tuple[InstrumentKey, str]] = []
        ambiguous: dict[InstrumentKey, set[str]] = {}

        for key, entries in claims.items():
            symbols = {e["tradingSymbol"] for e in entries}
            if len(symbols) > 1:
                ambiguous[key] = symbols
                continue
            contract = entries[0]
            subscribe_ids[key] = contract["subscribeId"]
            contracts[key] = contract
            bindings.append((key, contract["subscribeId"]))
            if contract["token"] and contract["token"] != contract["subscribeId"]:
                aliases.append((key, contract["token"]))

        option_count = len(bindings)

        for key, composite, token in futures:
            if key in subscribe_ids:
                continue
            subscribe_ids[key] = composite
            bindings.append((key, composite))
            if token and token != composite:
                aliases.append((key, token))

        self.subscribe_ids = subscribe_ids
        self.contracts = contracts
        self._bindings = bindings
        self._aliases = aliases
        self.lot_sizes = lot_sizes
        self.ambiguous = ambiguous
        self.row_count = len(bindings)
        self.option_count = option_count

        dropped = ", ".join(f"{k}={v}" for k, v in drops.items() if v)
        self._log("info", f"[firstock-scrip] {len(rows)} rows → {option_count} options, "
                          f"{len(futures)} futures"
                          + (f"; dropped {dropped}" if dropped else ""))
        if ambiguous:
            sample = list(ambiguous.items())[:3]
            self._log("error",
                      f"❌ {len(ambiguous)} Firstock contract identities were claimed by more "
                      f"than one trading symbol and are NOT bound (a quote or order for them "
                      f"is refused rather than routed to a contract you did not choose). "
                      + "; ".join(f"{k.position_id} ← {sorted(s)}" for k, s in sample))

        if not option_count:
            worst = max(drops.items(), key=lambda kv: kv[1]) if dropped else ("", 0)
            self.last_error = (
                f"parsed {len(rows)} rows but bound no option"
                + (f" (mostly {worst[0]}={worst[1]})" if worst[1] else "")
                + f"; columns resolved "
                + ", ".join(f"{k}->{v}" for k, v in sorted(cols.items())))
            self._log("error", f"❌ Firstock symbols bound no tradable option from "
                               f"{len(rows)} rows — {self.last_error}")
        return option_count

    # ── read model ────────────────────────────────────────────────────────
    def bindings(self) -> list[tuple[InstrumentKey, str]]:
        """(InstrumentKey, "EXCHANGE:TOKEN") for the instruments registry."""
        return list(self._bindings)

    def aliases(self) -> list[tuple[InstrumentKey, str]]:
        """(InstrumentKey, bare token) — additional ids resolving to the same
        key, without displacing the composite one used on the wire."""
        return list(self._aliases)

    def contract_for(self, key: InstrumentKey) -> dict | None:
        """Exchange, tradingSymbol, token, lot size and tick size for `key`.

        None is a refusal, not a default. Firstock orders are addressed by
        exchange + tradingSymbol, and the symbol has three incompatible
        encodings across NFO and BFO — so a contract that is not in the master
        must be refused, never reconstructed.
        """
        return self.contracts.get(key)

    def subscribe_id(self, key: InstrumentKey) -> str:
        """The ``EXCHANGE:TOKEN`` the socket subscribes for `key`, or "".

        Empty is a refusal, not a default: a contract we cannot address must be
        skipped, never guessed at.
        """
        return self.subscribe_ids.get(key, "")

    def explain_miss(self, key: InstrumentKey) -> str:
        """Why `key` is not in the loaded master, in terms of what IS.

        "not found" is an assertion, not a diagnosis — it cannot tell a wrong
        expiry from a wrong strike from an underlying that never parsed.
        """
        if key in self.ambiguous:
            symbols = sorted(self.ambiguous[key])
            return (f"{len(symbols)} different Firstock contracts normalise to this same "
                    f"identity ({', '.join(symbols[:6])}), so Charticks will not guess "
                    f"which one you meant")
        if key.underlying not in SUPPORTED:
            return (f"{key.underlying} is not offered by Firstock — it publishes no MCX "
                    f"segment, and its index file covers {', '.join(sorted(SUPPORTED))}")
        same = [k for k in self.subscribe_ids
                if k.underlying == key.underlying and k.segment == "OPT"]
        if not same:
            return f"no {key.underlying} option parsed at all from the Firstock master"
        # Chronological, not alphabetical: this list is read by a person trying
        # to work out which expiry they should have asked for, and a string sort
        # puts 18AUG2026 after 15SEP2026.
        from services import expiry as expiry_filter
        expiries = sorted({k.expiry for k in same}, key=expiry_filter.sort_key)
        if key.expiry not in expiries:
            return (f"{key.underlying} has {len(expiries)} expiries in the master and "
                    f"{key.expiry} is not one of them: {', '.join(expiries[:12])}")
        strikes = sorted({k.strike for k in same
                          if k.expiry == key.expiry and k.opt_type == key.opt_type})
        if key.strike not in strikes:
            near = sorted(strikes, key=lambda s: abs(s - key.strike))[:6]
            return (f"requested strike {key.strike}; {key.underlying} {key.expiry} "
                    f"{key.opt_type} lists {len(strikes)} strikes, range "
                    f"{strikes[0]}–{strikes[-1]}, nearest {sorted(near)}")
        return (f"{key.underlying} {key.expiry} {key.strike} exists but not as "
                f"{key.opt_type}")
