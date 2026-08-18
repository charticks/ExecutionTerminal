"""Regression tests for the Kotak scrip-master loader.

    python sidecar/tests/test_kotak_scrip.py

The defect these pin down, from a real live session on 2026-08-17: the loader
bound the four NSE index spots and NOT ONE option, reported itself as
"✅ 4 instruments", let live trading arm, and then rejected every order with
"Kotak's instrument list (4 instruments) does not contain NIFTY 18AUG2026 24500
CE" — which reads like a wrong expiry rather than a failed instrument load.

Same style as the other sidecar tests: real modules, only the SDK client
stubbed, no pytest, so it runs on the packaged runtime.
"""
import datetime as dt
import os
import sys
import tempfile

# The assertions below name real values, and those include the → this file
# uses to show "raw → normalised". Windows' default console codepage is cp1252,
# which cannot encode it, so an unguarded print() takes the whole run down on
# the very machine this defect was found on.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                       # pragma: no cover - non-reconfigurable stream
    pass

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-kotak-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

from services.feeds.kotak_scrip import KotakScripMaster            # noqa: E402

PASS, FAIL = [], []
LINES = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def log(level, msg):
    LINES.append(f"{level}: {msg}")


# ── synthetic Kotak rows ────────────────────────────────────────────────────
# Kotak's pExpiryDate is seconds since 1980-01-01 IST, which is what the loader
# has to reconstruct. Build a real one for a near expiry so the test does not
# quietly depend on a hard-coded date going stale.
_KOTAK_EPOCH_OFFSET = 315513000


def kotak_expiry(days_ahead: int) -> str:
    d = dt.date.today() + dt.timedelta(days=days_ahead)
    midnight = dt.datetime(d.year, d.month, d.day)
    return str(int(midnight.timestamp()) - _KOTAK_EPOCH_OFFSET)


def option_row(strike, opt_type="CE", days=1, expiry=None, strike_col="pStrikePrice",
               strike_value=None):
    return {
        "pSymbol": f"{strike}{opt_type}",
        "pTrdSymbol": f"NIFTY{strike}{opt_type}",
        "pSymbolName": "NIFTY",
        "pInstType": "OPTIDX",
        "pExpiryDate": kotak_expiry(days) if expiry is None else expiry,
        strike_col: str(strike) if strike_value is None else strike_value,
        "pOptionType": opt_type,
        "pExchSeg": "nse_fo",
    }


def index_row(name="NIFTY"):
    return {
        "pSymbol": f"idx-{name}", "pTrdSymbol": name, "pSymbolName": name,
        "pInstType": "IDX", "pExpiryDate": "", "pStrikePrice": "0",
        "pOptionType": "XX", "pExchSeg": "nse_cm",
    }


class StubClient:
    """Returns rows directly — the SDK shape the loader also accepts, which
    keeps the test off the network and out of the CSV cache."""

    def __init__(self, by_segment):
        self.by_segment = by_segment

    def scrip_master(self, exchange_segment):
        return self.by_segment.get(exchange_segment, [])


SEGMENTS = ["nse_fo", "bse_fo", "mcx_fo", "nse_cm", "bse_cm"]


def scenario_healthy_load():
    print("\n[1] A master with options loads and counts them")
    scrip = KotakScripMaster(log)
    ok = scrip.load(StubClient({
        "nse_fo": [option_row(24500), option_row(24500, "PE"), option_row(24600)],
        "nse_cm": [index_row("NIFTY"), index_row("BANKNIFTY")],
    }), SEGMENTS)
    check("load() succeeds", ok, scrip.last_error)
    check("option_count counts only options", scrip.option_count == 3, scrip.option_count)
    check("row_count counts every binding", scrip.row_count == 5, scrip.row_count)


def scenario_indices_only_is_a_failure():
    print("\n[2] Index spots but ZERO options is a FAILURE, not '4 instruments'")
    # Exactly the shipped outage: nse_cm answers, every F&O segment comes back
    # empty. Four bindings, none tradable.
    scrip = KotakScripMaster(log)
    ok = scrip.load(StubClient({
        "nse_cm": [index_row(n) for n in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")],
    }), SEGMENTS)
    check("load() reports failure", ok is False, "returned True with 0 options")
    check("option_count is zero", scrip.option_count == 0, scrip.option_count)
    check("row_count still shows the 4 index spots", scrip.row_count == 4, scrip.row_count)
    check("last_error names the failing segment",
          bool(scrip.last_error) and "nse_fo" in scrip.last_error, scrip.last_error)


def scenario_expiry_format_change():
    print("\n[3] An expiry format Kotak changed is NAMED, not silently dropped")
    # Every option row parses except its expiry — the failure mode that would
    # otherwise look identical to an empty download.
    scrip = KotakScripMaster(log)
    # A shape none of the accepted formats match and that is not a number
    # either. (Note "18/08/2026 14:30:00" WOULD parse — %d/%m/%Y after the
    # split on space — so a careless example here proves nothing.)
    rows = [option_row(24500 + i * 100, expiry="AUG-18-2026") for i in range(50)]
    ok = scrip.load(StubClient({"nse_fo": rows, "nse_cm": [index_row()]}), SEGMENTS)
    check("load() reports failure", ok is False, "returned True with 0 options")
    check("the dominant drop reason is reported",
          "unparsable_expiry" in (scrip.last_error or ""), scrip.last_error)
    check("per-segment counts reach the log",
          any("nse_fo: 50 rows" in ln for ln in LINES),
          [ln for ln in LINES if "nse_fo" in ln][-3:])


def scenario_failure_reason_survives_a_good_segment():
    print("\n[4] A failing segment's reason is not erased by a segment that works")
    # The bug that made the outage undiagnosable: last_error was set by the
    # failing nse_fo, then cleared because nse_cm succeeded.
    scrip = KotakScripMaster(log)
    ok = scrip.load(StubClient({
        "nse_fo": [option_row(24500)],
        "bse_fo": {"Error": "no entitlement for BSE derivatives"},
        "nse_cm": [index_row()],
    }), SEGMENTS)
    check("load() still succeeds (NSE options are there)", ok, scrip.last_error)
    check("but the bse_fo failure is retained",
          "bse_fo" in (scrip.last_error or ""), scrip.last_error)


def scenario_expired_contracts_are_distinguished():
    print("\n[5] Expired contracts are counted apart from unparsable ones")
    scrip = KotakScripMaster(log)
    ok = scrip.load(StubClient({
        "nse_fo": [option_row(24500, days=-10), option_row(24600, days=-5)],
        "nse_cm": [index_row()],
    }), SEGMENTS)
    check("load() reports failure", ok is False, "returned True with 0 options")
    check("reported as already_expired, not a parse failure",
          "already_expired" in (scrip.last_error or ""), scrip.last_error)


def scenario_semicolon_strike_header():
    print("\n[6] THE OUTAGE: Kotak's strike header is 'pStrikePrice;'")
    # Reproduced from the tester's broker.log of 2026-08-17, which resolved
    # every column except `strike` on 79,088 nse_fo rows and bound 0 options:
    #   columns resolved: expiry->pExpiryDate, instrument->pInstType,
    #   name->pSymbolName, option_type->pOptionType, segment->pExchSeg,
    #   token->pSymbol, trading_symbol->pTrdSymbol
    scrip = KotakScripMaster(log)
    rows = [option_row(24500 + i * 100, strike_col="pStrikePrice;") for i in range(20)]
    ok = scrip.load(StubClient({"nse_fo": rows, "nse_cm": [index_row()]}), SEGMENTS)
    check("the semicolon header still resolves", ok, scrip.last_error)
    check("every option binds", scrip.option_count == 20, scrip.option_count)


def scenario_semicolon_strike_value():
    print("\n[7] A strike VALUE carrying the same stray punctuation")
    scrip = KotakScripMaster(log)
    rows = [option_row(24500, strike_col="pStrikePrice;", strike_value="2450000;")]
    ok = scrip.load(StubClient({"nse_fo": rows, "nse_cm": [index_row()]}), SEGMENTS)
    check("load() succeeds", ok, scrip.last_error)
    check("paise strike is still converted", scrip.option_count == 1, scrip.option_count)
    check("bound at the real strike, not 2450000",
          any(k.strike == 24500 for k in scrip.segments),
          [getattr(k, "strike", None) for k in scrip.segments])


def scenario_unknown_strike_spelling_still_resolves():
    print("\n[9] Any header spelling containing 'strike' still resolves")
    # The exact spelling Kotak ships was inferred from its ABSENCE in a log
    # line, never seen. These are the plausible variants.
    for col in ("pStrikePrice;", "lStrikePrice", "dStrikePrice", "STRIKE_PRICE",
                "strikePrice", "pStrike Price"):
        scrip = KotakScripMaster(log)
        ok = scrip.load(StubClient({
            "nse_fo": [option_row(24500, strike_col=col)],
            "nse_cm": [index_row()],
        }), SEGMENTS)
        check(f"resolves {col!r}", ok and scrip.option_count == 1,
              scrip.last_error or scrip.option_count)


def scenario_unresolvable_strike_fails_loudly():
    print("\n[8] A strike column we genuinely cannot find fails the segment")
    scrip = KotakScripMaster(log)
    rows = [option_row(24500 + i * 100, strike_col="someFutureName") for i in range(20)]
    ok = scrip.load(StubClient({"nse_fo": rows, "nse_cm": [index_row()]}), SEGMENTS)
    check("load() reports failure", ok is False, "silently dropped every row again")
    check("the missing column is named",
          "strike" in (scrip.last_error or ""), scrip.last_error)


def scenario_key_collision_is_detected():
    print("\n[10] Distinct contracts collapsing onto one key is reported")
    # The 2026-08-17 shape: "18204 options" bound but only "2230 instruments".
    # Here eight expiries all normalise to one date, so eight real contracts
    # claim a single key and bind_many keeps only the last.
    scrip = KotakScripMaster(log)
    rows = []
    for week in range(8):
        for strike in (24400, 24500, 24600):
            r = option_row(strike, days=1 + 7 * week)
            # Same normalised expiry for every week — the collapse under test.
            r["pExpiryDate"] = kotak_expiry(1)
            r["pTrdSymbol"] = f"NIFTY-W{week}-{strike}CE"
            rows.append(r)
    ok = scrip.load(StubClient({"nse_fo": rows, "nse_cm": [index_row()]}), SEGMENTS)
    check("load() still succeeds (options did bind)", ok, scrip.last_error)
    check("option rows counted in full", scrip.option_count == 24, scrip.option_count)
    collision_lines = [ln for ln in LINES if "collapsed onto an already-bound key" in ln]
    check("the collapse is logged as an error", len(collision_lines) == 1, collision_lines)
    check("a colliding pair is shown with its differing field",
          any("claimed by" in ln for ln in LINES),
          [ln for ln in LINES if "claimed by" in ln][:2])
    # The safety property: an identity claimed by 8 different contracts is left
    # UNBOUND, so an order for it is refused rather than routed to whichever
    # row happened to be parsed last.
    from services.instruments import InstrumentKey
    near = (dt.date.today() + dt.timedelta(days=1)).strftime("%d%b%Y").upper()
    contested = InstrumentKey.option("NIFTY", near, 24500, "CE")
    check("the contested key is not bound", contested not in scrip.segments,
          "an ambiguous contract was bound anyway")
    check("row_count reflects only what is safely tradable",
          scrip.row_count == 1, scrip.row_count)
    check("the refusal calls it ambiguous, not absent",
          "normalise to this same identity" in scrip.explain_miss(contested),
          scrip.explain_miss(contested))


def scenario_explain_miss_names_the_field():
    print("\n[11] A miss is explained against what the master holds")
    scrip = KotakScripMaster(log)
    scrip.load(StubClient({
        "nse_fo": [option_row(24400), option_row(24600),
                   option_row(24500, days=8)],
        "nse_cm": [index_row()],
    }), SEGMENTS)
    from services.instruments import InstrumentKey

    near = dt.date.today() + dt.timedelta(days=1)
    wanted = InstrumentKey.option("NIFTY", near.strftime("%d%b%Y").upper(), 24500, "CE")
    msg = scrip.explain_miss(wanted)
    check("names the strike as the mismatch, not the expiry",
          "requested strike 24500" in msg and "lists 2 strikes" in msg, msg)
    check("points at the expiry that DOES list that strike",
          "DOES exist for expiries" in msg, msg)

    wrong_expiry = InstrumentKey.option("NIFTY", "01JAN2027", 24400, "CE")
    msg2 = scrip.explain_miss(wrong_expiry)
    check("a wrong expiry is named as such and the real ones listed",
          "is not one of them" in msg2, msg2)

    wrong_underlying = InstrumentKey.option("SENSEX", "01JAN2027", 80000, "CE")
    msg3 = scrip.explain_miss(wrong_underlying)
    check("an underlying that never parsed is named",
          "no SENSEX option parsed at all" in msg3, msg3)


def scenario_scientific_notation_strikes():
    print("\n[12] THE COLLAPSE: dStrikePrice in scientific notation")
    from services.feeds.kotak_scrip import _num
    # Straight from the tester's broker.log of 2026-08-17 12:47.
    for raw, paise in (("1.84e+06", 1_840_000), ("1.855e+06", 1_855_000),
                       ("2.95e+06", 2_950_000), ("3.22e+06", 3_220_000),
                       ("3e+06", 3_000_000), ("1.2375e+06", 1_237_500),
                       ("8.42e+06", 8_420_000)):
        check(f"_num({raw!r}) keeps the exponent", _num(raw) == paise, _num(raw))
    check("a trailing ';' is still tolerated", _num("2450000;") == 2450000.0,
          _num("2450000;"))


def scenario_strike_scale_and_key():
    print("\n[13] Paise are scaled to rupees before the key is built")
    scrip = KotakScripMaster(log)
    rows = [
        option_row(0, strike_col="dStrikePrice;", strike_value="1.84e+06"),
        option_row(1, strike_col="dStrikePrice;", strike_value="1.855e+06"),
        option_row(2, strike_col="dStrikePrice;", strike_value="2.95e+06"),
        option_row(3, strike_col="dStrikePrice;", strike_value="3.22e+06"),
        option_row(4, strike_col="dStrikePrice;", strike_value="1.2375e+06"),
    ]
    for r, sym in zip(rows, ("NIFTY26AUG18400CE", "NIFTY26AUG18550CE",
                             "NIFTY26AUG29500CE", "NIFTY26AUG32200CE",
                             "NIFTY26AUG12375CE")):
        r["pTrdSymbol"] = sym
    ok = scrip.load(StubClient({"nse_fo": rows, "nse_cm": [index_row()]}), SEGMENTS)
    check("load() succeeds", ok, scrip.last_error)
    strikes = sorted(k.strike for k in scrip.segments if k.segment == "OPT")
    check("every strike is the real rupee value",
          strikes == [12375, 18400, 18550, 29500, 32200], strikes)
    check("no keys collapsed", scrip.option_count == 5 and len(strikes) == 5,
          f"{scrip.option_count} rows -> {len(strikes)} keys")

    # The acceptance criterion, stated exactly.
    from services.instruments import InstrumentKey
    expiry = next(k.expiry for k in scrip.segments if k.segment == "OPT")
    wanted = InstrumentKey.option("NIFTY", expiry, 18400, "CE")
    check(f"NIFTY26AUG18400CE resolves to |{expiry}|18400|CE, not |1|",
          wanted in scrip.segments, sorted(k.position_id for k in scrip.segments)[:4])


def scenario_scale_measured_not_guessed():
    print("\n[16] Scale is MEASURED against the trading symbol")
    # The 14:06 failure: nse_fo starts with stock options whose paise strikes
    # (Rs.1,300 -> 130000) sit below the old 100,000 median threshold, so the
    # whole segment was declared "rupees" and NIFTY stayed at 1,840,000.
    scrip = KotakScripMaster(log)
    rows = []
    for i in range(400):                       # stock options first, as Kotak ships them
        r = option_row(0, strike_col="dStrikePrice;", strike_value=str(1300 * 100))
        r["pSymbolName"] = "RELIANCE"
        r["pTrdSymbol"] = f"RELIANCE26AUG1300CE"
        rows.append(r)
    for rupees in (18400, 18500, 24600, 29500):
        r = option_row(0, strike_col="dStrikePrice;", strike_value=str(rupees * 100))
        r["pTrdSymbol"] = f"NIFTY26AUG{rupees}CE"
        rows.append(r)
    ok = scrip.load(StubClient({"nse_fo": rows, "nse_cm": [index_row()]}), SEGMENTS)
    check("load() succeeds", ok, scrip.last_error)
    scale_lines = [ln for ln in LINES if "strike scale" in ln and "nse_fo" in ln]
    check("the measured scale is 100 despite stock options first",
          any("÷100 measured" in ln for ln in scale_lines), scale_lines[-1:] )
    strikes = sorted(k.strike for k in scrip.segments if k.segment == "OPT")
    check("NIFTY strikes are rupees, not paise",
          strikes == [18400, 18500, 24600, 29500], strikes)

    # The acceptance criterion, verbatim.
    from services.instruments import InstrumentKey
    expiry = next(k.expiry for k in scrip.segments if k.segment == "OPT")
    wanted = InstrumentKey.option("NIFTY", expiry, 24600, "CE")
    check("NIFTY 24600 CE resolves", wanted in scrip.segments,
          sorted(k.position_id for k in scrip.segments)[:4])
    check("the key is |24600|, not |2460000|",
          wanted.position_id.endswith("|24600|CE"), wanted.position_id)


def scenario_unmeasurable_scale_refuses():
    print("\n[17] An unmeasurable scale refuses the segment, never guesses")
    scrip = KotakScripMaster(log)
    rows = []
    for i in range(20):
        r = option_row(0, strike_col="dStrikePrice;", strike_value="1840000")
        r["pTrdSymbol"] = f"MYSTERY-{i}"        # no strike in the symbol to check against
        rows.append(r)
    ok = scrip.load(StubClient({"nse_fo": rows, "nse_cm": [index_row()]}), SEGMENTS)
    check("load() reports failure", ok is False, "bound strikes at a guessed scale")
    check("the reason names the scale measurement",
          "could not measure the strike scale" in (scrip.last_error or ""), scrip.last_error)


def scenario_miss_shows_raw_and_normalised():
    print("\n[18] A failed lookup prints requested vs stored, with the raw value")
    scrip = KotakScripMaster(log)
    rows = []
    for rupees in (18400, 18500, 18550):
        r = option_row(0, strike_col="dStrikePrice;", strike_value=str(rupees * 100))
        r["pTrdSymbol"] = f"NIFTY26AUG{rupees}CE"
        rows.append(r)
    scrip.load(StubClient({"nse_fo": rows, "nse_cm": [index_row()]}), SEGMENTS)
    from services.instruments import InstrumentKey
    expiry = next(k.expiry for k in scrip.segments if k.segment == "OPT")
    msg = scrip.explain_miss(InstrumentKey.option("NIFTY", expiry, 24600, "CE"))
    check("shows the requested strike", "requested strike 24600" in msg, msg)
    check("shows nearest available strikes", "nearest [18400, 18500, 18550]" in msg, msg)
    # The nearest to 24600 is 18550, so that is the row quoted.
    check("shows raw → normalised for a real row", "raw 1855000 → 18550" in msg, msg)
    check("shows the full available range", "range 18400–18550" in msg, msg)


def scenario_crudeoil_low_strike_not_misscaled():
    print("\n[14] A low-value segment is NOT divided by 100")
    # The threshold this replaces was scale-blind: a CRUDEOIL strike of Rs.900
    # arrives as 90,000 paise, falls under `>= 100000`, and was kept as 90000.
    scrip = KotakScripMaster(log)
    rows = []
    for rupees in (900, 950, 1000, 1050):
        r = option_row(rupees, strike_col="dStrikePrice;",
                       strike_value=str(rupees * 100))
        r["pSymbolName"] = "CRUDEOIL"
        r["pTrdSymbol"] = f"CRUDEOIL26AUG{rupees}CE"
        r["pExchSeg"] = "mcx_fo"
        rows.append(r)
    scrip.load(StubClient({"mcx_fo": rows}), SEGMENTS)
    strikes = sorted(k.strike for k in scrip.segments if k.segment == "OPT")
    check("low strikes scale correctly", strikes == [900, 950, 1000, 1050], strikes)


def scenario_niftynxt50_is_not_nifty():
    print("\n[15] NIFTYNXT50 must not be filed as NIFTY")
    from services.feeds.kotak_scrip import _canonical
    check("NIFTYNXT50 is not NIFTY", _canonical("NIFTYNXT50", "NIFTYNXT5026OCT84200CE") is None,
          _canonical("NIFTYNXT50", "NIFTYNXT5026OCT84200CE"))
    check("plain NIFTY still resolves", _canonical("NIFTY", "NIFTY26AUG18400CE") == "NIFTY")
    check("BANKNIFTY is not NIFTY", _canonical("BANKNIFTY", "BANKNIFTY26AUG55000CE") == "BANKNIFTY")
    check("index spelling still resolves", _canonical("NIFTY 50", "NIFTY") == "NIFTY")
    check("symbol-only fallback still works", _canonical("", "NIFTY26AUG18400CE") == "NIFTY")
    check("symbol-only rejects NIFTYNXT50", _canonical("", "NIFTYNXT5026OCT84200CE") is None,
          _canonical("", "NIFTYNXT5026OCT84200CE"))

    # And end to end: a NIFTYNXT50 row must not appear under NIFTY.
    scrip = KotakScripMaster(log)
    nxt = option_row(0, strike_col="dStrikePrice;", strike_value="8.42e+06")
    nxt["pSymbolName"] = "NIFTYNXT50"
    nxt["pTrdSymbol"] = "NIFTYNXT5026OCT84200CE"
    nifty = option_row(0, strike_col="dStrikePrice;", strike_value="1.84e+06")
    scrip.load(StubClient({"nse_fo": [nxt, nifty], "nse_cm": [index_row()]}), SEGMENTS)
    check("only the real NIFTY option is bound",
          sorted(k.strike for k in scrip.segments if k.segment == "OPT") == [18400],
          sorted(k.position_id for k in scrip.segments))


if __name__ == "__main__":
    print("Kotak scrip-master regression tests")
    scenario_healthy_load()
    scenario_indices_only_is_a_failure()
    scenario_expiry_format_change()
    scenario_failure_reason_survives_a_good_segment()
    scenario_expired_contracts_are_distinguished()
    scenario_semicolon_strike_header()
    scenario_semicolon_strike_value()
    scenario_unresolvable_strike_fails_loudly()
    scenario_unknown_strike_spelling_still_resolves()
    scenario_key_collision_is_detected()
    scenario_explain_miss_names_the_field()
    scenario_scientific_notation_strikes()
    scenario_strike_scale_and_key()
    scenario_crudeoil_low_strike_not_misscaled()
    scenario_niftynxt50_is_not_nifty()
    scenario_scale_measured_not_guessed()
    scenario_unmeasurable_scale_refuses()
    scenario_miss_shows_raw_and_normalised()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)
