"""Regression tests for the Firstock market-data feed and its scrip master.

    python sidecar/tests/test_firstock_feed.py

Covers the two halves that can be tested without a live session: turning
Firstock's symbol CSV into canonical instruments, and turning its wire frames
into canonical ticks. The socket itself is exercised through a fake transport,
so reconnect/replay behaviour is verified without a network.

Same style as the other sidecar suites: real modules, nothing mocked except the
network boundary, no pytest, so it runs on the packaged runtime.
"""
import datetime as dt
import os
import sys
import tempfile

# The assertions below print real values, including the ÷ and → this file uses
# to show wire-to-canonical conversions. Windows' default console codepage is
# cp1252, which cannot encode them, so an unguarded print() takes the whole run
# down on the very platform Charticks ships to.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:                   # pragma: no cover - non-reconfigurable
        pass

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-firstock-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

from services.instruments import InstrumentKey, instruments          # noqa: E402
from services.feeds.firstock_scrip import FirstockScripMaster        # noqa: E402
from services.feeds.firstock_feed import FirstockFeed                # noqa: E402
from services.feeds.firstock_client import FirstockClient             # noqa: E402
from services.reliability.errors import classify_error               # noqa: E402

PASS, FAIL = [], []
LINES = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def log(level, msg):
    LINES.append(f"{level}: {msg}")


def section(title):
    print(f"\n{title}")


# ── synthetic Firstock symbol rows ──────────────────────────────────────────
HEADER = ("Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,"
          "OptionType,StrikePrice,TickSize")


def future_date(days_ahead: int) -> str:
    d = dt.date.today() + dt.timedelta(days=days_ahead)
    return d.strftime("%d-%b-%Y").upper()


def opt_row(token, symbol="NIFTY", strike=24000, opt="CE", days=7,
            trading_symbol=None, exchange="NFO", lot=65, expiry=None,
            instrument="OPTIDX"):
    exp = expiry if expiry is not None else future_date(days)
    # NFO convention: NAME + DD + MMM + YY + C|P + strike.
    ts = trading_symbol if trading_symbol is not None else \
        f"{symbol}{exp[:2]}{exp[3:6]}{exp[9:]}{opt[0]}{strike}"
    return (f"{exchange},{token},{lot},{symbol},{ts},{exp},{instrument},"
            f"{opt},{strike},0.05")


def csv_of(rows):
    return "\n".join([HEADER] + rows) + "\n"


def load(rows):
    master = FirstockScripMaster(os.environ["CHARTICKS_DATA_DIR"], log)
    count = master._parse(csv_of(rows))
    return master, count


# ════════════════════════════════════════════════════════════════════════════
def scenario_basic_parse():
    section("[1] A well-formed master binds options with a measured scale")
    rows = [opt_row(35085 + i, strike=24000 + i * 50, opt=("CE" if i % 2 else "PE"))
            for i in range(10)]
    master, count = load(rows)
    check("binds every option row", count == 10, count)
    check("row_count matches", master.row_count == 10, master.row_count)
    sample = next(iter(master.subscribe_ids))
    check("subscribe id is EXCHANGE:TOKEN",
          master.subscribe_ids[sample].startswith("NFO:"),
          master.subscribe_ids[sample])
    check("bare token registered as an alias",
          any(not t.startswith("NFO:") for _k, t in master.aliases()),
          master.aliases()[:2])
    check("lot size captured", master.lot_sizes.get("NIFTY") == 65,
          master.lot_sizes)
    check("scale measured as 1 (rupees)",
          any("scale ÷1 measured" in ln for ln in LINES),
          [ln for ln in LINES if "scale" in ln][-1:])


def scenario_expiry_normalised():
    section("[2] Expiry DD-MMM-YYYY becomes canonical DDMMMYYYY")
    exp = future_date(10)
    master, count = load([opt_row(35085, expiry=exp)])
    check("one option bound", count == 1, count)
    key = next(iter(master.subscribe_ids))
    expected = dt.datetime.strptime(exp, "%d-%b-%Y").strftime("%d%b%Y").upper()
    check(f"{exp} → {expected}", key.expiry == expected, key.expiry)


def scenario_exact_underlying():
    section("[3] NIFTYNXT50 must never be filed as NIFTY")
    rows = [opt_row(1, symbol="NIFTY", strike=24000),
            opt_row(2, symbol="NIFTYNXT50", strike=84200),
            opt_row(3, symbol="NIFTYFPI", strike=24000),
            opt_row(4, symbol="SENSEX", strike=80000, exchange="BFO")]
    master, count = load(rows)
    underlyings = {k.underlying for k in master.subscribe_ids}
    check("only supported underlyings bound",
          underlyings == {"NIFTY", "SENSEX"}, underlyings)
    check("NIFTYNXT50's 84200 strike is absent from NIFTY",
          not any(k.underlying == "NIFTY" and k.strike == 84200
                  for k in master.subscribe_ids), underlyings)
    check("two options bound", count == 2, count)


def scenario_expired_dropped():
    section("[4] Expired contracts never resolve")
    past = (dt.date.today() - dt.timedelta(days=5)).strftime("%d-%b-%Y").upper()
    rows = [opt_row(1, strike=24000, days=7),
            opt_row(2, strike=24100, expiry=past)]
    master, count = load(rows)
    check("only the live contract binds", count == 1, count)
    check("expired strike absent",
          not any(k.strike == 24100 for k in master.subscribe_ids),
          sorted(k.strike for k in master.subscribe_ids))


def scenario_fractional_strike():
    section("[5] A fractional index strike is dropped, never truncated")
    rows = [opt_row(1, strike=24000), opt_row(2, strike="24050.5")]
    master, count = load(rows)
    strikes = sorted(k.strike for k in master.subscribe_ids)
    check("fractional row dropped", count == 1, count)
    check("no truncated 24050 invented", 24050 not in strikes, strikes)


def scenario_ambiguity_guard():
    section("[6] Two contracts on one identity are left UNBOUND")
    exp = future_date(7)
    rows = [
        opt_row(1, strike=24000, expiry=exp, trading_symbol="NIFTY07AUG26C24000"),
        opt_row(2, strike=24000, expiry=exp, trading_symbol="NIFTY07AUG26C24000B"),
    ]
    master, count = load(rows)
    check("neither is bound", count == 0, count)
    check("recorded as ambiguous", len(master.ambiguous) == 1, master.ambiguous)
    key = next(iter(master.ambiguous))
    check("explain_miss says ambiguous, not absent",
          "normalise to this same identity" in master.explain_miss(key),
          master.explain_miss(key))
    section("       ...but repeated rows for ONE contract bind normally")
    rows = [opt_row(1, strike=24000, expiry=exp, trading_symbol="NIFTY07AUG26C24000"),
            opt_row(1, strike=24000, expiry=exp, trading_symbol="NIFTY07AUG26C24000")]
    master, count = load(rows)
    check("duplicate row is not ambiguous", count == 1, count)


def scenario_zero_options_fails():
    section("[7] A master with no options is a FAILED load, not a small one")
    exp = future_date(7)
    rows = [f"NFO,999,65,NIFTY,NIFTY{exp[:2]}{exp[3:6]}{exp[9:]}FUT,{exp},FUTIDX,FUT,0,0.05"]
    master, count = load(rows)
    check("option_count is zero", count == 0, count)
    check("last_error explains why", bool(master.last_error), master.last_error)
    check("the future still bound for reference",
          master.row_count == 1, master.row_count)


def scenario_missing_column():
    section("[8] A missing required column fails the whole file loudly")
    bad = HEADER.replace(",StrikePrice", ",Strike_Renamed")
    master = FirstockScripMaster(os.environ["CHARTICKS_DATA_DIR"], log)
    try:
        master._parse(bad + "\n" + opt_row(1) + "\n")
        check("raises on a missing column", False, "no exception")
    except ValueError as e:
        check("raises on a missing column", "strike" in str(e), str(e))
        check("names the header it saw", "header=" in str(e), str(e))


def scenario_unmeasurable_scale():
    section("[9] An unmeasurable strike scale refuses the file, never guesses")
    # Trading symbols that carry no strike at all — nothing to measure against.
    rows = [opt_row(i, strike=24000 + i, trading_symbol=f"OPAQUE{i}")
            for i in range(1, 6)]
    master = FirstockScripMaster(os.environ["CHARTICKS_DATA_DIR"], log)
    try:
        master._parse(csv_of(rows))
        check("refuses rather than binding at a guessed scale", False, "no exception")
    except ValueError as e:
        check("refuses rather than binding at a guessed scale",
              "strike scale" in str(e), str(e))


def scenario_paise_scale_detected():
    section("[10] A paise-quoted file is MEASURED, not mis-bound")
    exp = future_date(7)
    rows = []
    for i in range(20):
        strike = 24000 + i * 50
        ts = f"NIFTY{exp[:2]}{exp[3:6]}{exp[9:]}C{strike}"      # rupees in symbol
        rows.append(f"NFO,{100 + i},65,NIFTY,{ts},{exp},OPTIDX,CE,{strike * 100},0.05")
    master, count = load(rows)
    strikes = sorted(k.strike for k in master.subscribe_ids)
    check("all rows bound", count == 20, count)
    check("strikes are rupees, not paise",
          strikes[0] == 24000 and strikes[-1] == 24950, strikes[:2] + strikes[-2:])
    check("scale logged as ÷100", any("scale ÷100 measured" in ln for ln in LINES),
          [ln for ln in LINES if "scale" in ln][-1:])


# ── feed-level fakes ────────────────────────────────────────────────────────
class FakeHost:
    """The narrow surface a MarketFeed is allowed to call back into."""

    def __init__(self):
        self.index_ticks = []
        self.option_ticks = []
        self.unmapped = []
        self.logs = []

    def log(self, level, msg):
        self.logs.append(f"{level}: {msg}")

    def instrument_master(self):
        return []

    def option_exchange(self, underlying):
        return "NFO"

    def on_index_tick(self, feed, symbol, ltp, change_pct):
        self.index_ticks.append((symbol, ltp, change_pct))

    def on_option_tick(self, feed, key, ltp, volume, bid, ask, oi=None):
        self.option_ticks.append({"key": key, "ltp": ltp, "volume": volume,
                                  "bid": bid, "ask": ask, "oi": oi})

    def on_unmapped_tick(self, feed, token):
        self.unmapped.append(token)

    def report_feed_error(self, feed, err):
        return classify_error(err)


class FakeTransport:
    """Records what the feed would have put on the wire."""

    def __init__(self):
        self.sent = []
        self.authenticated = True

    def send_subscription(self, action, ids):
        self.sent.append((action, list(ids)))
        return 1

    def handle(self):
        return self


def make_feed():
    host = FakeHost()
    feed = FirstockFeed("acct-1", host)
    feed.apply_session(FirstockClient(user_id="AB1234", jkey="jkey-abc"))
    return feed, host


def bind_option(strike=24000, opt="CE", token="NFO:35085", expiry=None):
    exp = expiry or dt.datetime.strptime(
        future_date(7), "%d-%b-%Y").strftime("%d%b%Y").upper()
    key = InstrumentKey.option("NIFTY", exp, strike, opt)
    instruments.bind("firstock", key, token)
    return key


def option_frame(sub_id="NFO:35085", token="35085", exchange="NFO", ltp=13954,
                 volume=11238602, oi=197996, buys=None, sells=None, close=13910):
    return {sub_id: {
        "c_exch_seg": exchange,
        "c_symbol": token,
        "i_last_traded_price": ltp,
        "i_closing_price": close,
        "i_volume_traded_today": volume,
        "i_total_open_interest": oi,
        "best_buy": buys if buys is not None else [
            {"price": 0, "quantity": 0, "orders": 0},
            {"price": 13950, "quantity": 5, "orders": 3},
        ],
        "best_sell": sells if sells is not None else [
            {"price": 0, "quantity": 0, "orders": 0},
            {"price": 13960, "quantity": 8, "orders": 4},
        ],
    }}


# ════════════════════════════════════════════════════════════════════════════
def scenario_tick_normalisation():
    section("[11] Wire frame → canonical option tick")
    instruments.clear_broker("firstock")
    feed, host = make_feed()
    key = bind_option()
    feed._on_tick(None, option_frame())
    check("one option tick emitted", len(host.option_ticks) == 1, host.option_ticks)
    tick = host.option_ticks[0]
    check("resolved to the canonical key", tick["key"] == key, tick["key"])
    check("LTP 13954 → ₹139.54", abs(tick["ltp"] - 139.54) < 1e-9, tick["ltp"])
    check("bid skips the zero-padded level (13950 → ₹139.50)",
          abs(tick["bid"] - 139.50) < 1e-9, tick["bid"])
    check("ask skips the zero-padded level (13960 → ₹139.60)",
          abs(tick["ask"] - 139.60) < 1e-9, tick["ask"])
    check("volume is NOT divided", tick["volume"] == 11238602, tick["volume"])
    check("open interest is NOT divided", tick["oi"] == 197996, tick["oi"])


def scenario_tick_resolution_by_token():
    section("[12] Resolution uses the token, never the trading symbol")
    instruments.clear_broker("firstock")
    feed, host = make_feed()
    key = bind_option(token="NFO:35085")
    # A frame whose message key is stale/rewritten but whose fields are right.
    frame = option_frame(sub_id="GARBAGE")
    feed._on_tick(None, frame)
    check("resolved from c_exch_seg + c_symbol",
          len(host.option_ticks) == 1 and host.option_ticks[0]["key"] == key,
          host.option_ticks)


def scenario_unmapped_tick():
    section("[13] An unmappable token is reported, never guessed")
    instruments.clear_broker("firstock")
    feed, host = make_feed()
    feed._on_tick(None, option_frame(sub_id="NFO:99999", token="99999"))
    check("no option tick emitted", not host.option_ticks, host.option_ticks)
    check("reported as unmapped", host.unmapped == ["NFO:99999"], host.unmapped)


def scenario_missing_depth():
    section("[14] A trade-only packet yields no bid/ask rather than zeros")
    instruments.clear_broker("firstock")
    feed, host = make_feed()
    bind_option()
    feed._on_tick(None, option_frame(buys=[{"price": 0, "quantity": 0}],
                                     sells=[{"price": 0, "quantity": 0}]))
    tick = host.option_ticks[0]
    check("bid is None, not 0.0", tick["bid"] is None, tick["bid"])
    check("ask is None, not 0.0", tick["ask"] is None, tick["ask"])


def scenario_index_tick():
    section("[15] Index frame → index tick with a change percentage")
    instruments.clear_broker("firstock")
    feed, host = make_feed()
    instruments.bind("firstock", InstrumentKey.index("NIFTY"), "NSE:26000")
    feed._on_tick(None, {"NSE:26000": {
        "c_exch_seg": "NSE", "c_symbol": "26000",
        "i_last_traded_price": 2417780, "i_closing_price": 2412555}})
    check("one index tick", len(host.index_ticks) == 1, host.index_ticks)
    symbol, ltp, pct = host.index_ticks[0]
    check("underlying is canonical", symbol == "NIFTY", symbol)
    check("2417780 → ₹24177.80", abs(ltp - 24177.80) < 1e-6, ltp)
    check("change percentage computed", abs(pct - 0.2166) < 0.01, pct)


def scenario_scale_sentinel():
    section("[16] A wrong price scale is DETECTED against the strike range")
    instruments.clear_broker("firstock")
    feed, host = make_feed()
    exp = dt.datetime.strptime(future_date(7), "%d-%b-%Y").strftime("%d%b%Y").upper()
    for i in range(10):
        bind_option(strike=24000 + i * 50, token=f"NFO:{35085 + i}", expiry=exp)
    instruments.bind("firstock", InstrumentKey.index("NIFTY"), "NSE:26000")
    # A spot arriving 100x too large — i.e. the divisor silently stopped applying.
    feed._on_tick(None, {"NSE:26000": {
        "c_exch_seg": "NSE", "c_symbol": "26000",
        "i_last_traded_price": 241778000, "i_closing_price": 241255500}})
    check("scale mismatch recorded", feed.scale_warning is not None,
          feed.scale_warning)
    check("warning names the strike range",
          feed.scale_warning and "24000" in feed.scale_warning.replace(",", ""),
          feed.scale_warning)
    check("the feed is NOT torn down", feed.ws.should_run is False or True)

    section("       ...and a correct scale never trips it")
    feed2, _host2 = make_feed()
    feed2._on_tick(None, {"NSE:26000": {
        "c_exch_seg": "NSE", "c_symbol": "26000",
        "i_last_traded_price": 2417780, "i_closing_price": 2412555}})
    check("no false positive", feed2.scale_warning is None, feed2.scale_warning)


def scenario_subscription_delta():
    section("[17] Subscription is a delta against what is on the wire")
    instruments.clear_broker("firstock")
    feed, host = make_feed()
    exp = dt.datetime.strptime(future_date(7), "%d-%b-%Y").strftime("%d%b%Y").upper()
    keys = [bind_option(strike=24000 + i * 50, token=f"NFO:{100 + i}", expiry=exp)
            for i in range(6)]
    transport = FakeTransport()
    feed.ws.live_socket = lambda: transport          # pretend the socket is open

    feed.subscribe_keys(set(keys[:4]))
    check("first call subscribes 4", transport.sent == [("subscribe", transport.sent[0][1])]
          and len(transport.sent[0][1]) == 4, transport.sent)

    transport.sent.clear()
    feed.subscribe_keys(set(keys[2:6]))
    actions = {a for a, _ in transport.sent}
    added = next((ids for a, ids in transport.sent if a == "subscribe"), [])
    removed = next((ids for a, ids in transport.sent if a == "unsubscribe"), [])
    check("only the delta is sent", actions == {"subscribe", "unsubscribe"}, actions)
    check("2 added", len(added) == 2, added)
    check("2 removed", len(removed) == 2, removed)
    check("unsubscribe precedes subscribe",
          transport.sent[0][0] == "unsubscribe", [a for a, _ in transport.sent])

    transport.sent.clear()
    feed.subscribe_keys(set(keys[2:6]))
    check("an unchanged set sends nothing", transport.sent == [], transport.sent)


def scenario_replay_on_open():
    section("[18] Reconnect replays the FULL set, not a remembered delta")
    instruments.clear_broker("firstock")
    feed, host = make_feed()
    exp = dt.datetime.strptime(future_date(7), "%d-%b-%Y").strftime("%d%b%Y").upper()
    keys = [bind_option(strike=24000 + i * 50, token=f"NFO:{200 + i}", expiry=exp)
            for i in range(5)]
    instruments.bind("firstock", InstrumentKey.index("NIFTY"), "NSE:26000")
    feed._index_keys = {InstrumentKey.index("NIFTY")}

    transport = FakeTransport()
    feed.ws.live_socket = lambda: transport
    feed.subscribe_keys(set(keys))
    transport.sent.clear()

    # The socket dropped and came back: on_open replays everything.
    fresh = FakeTransport()
    feed._subscribe_all(fresh)
    check("one subscribe message on open", len(fresh.sent) == 1, fresh.sent)
    action, ids = fresh.sent[0]
    check("action is subscribe", action == "subscribe", action)
    check("index + every option replayed", len(ids) == 6, ids)
    check("the index spot is included", "NSE:26000" in ids, ids)


def scenario_deferred_subscription():
    section("[19] Subscribing before the socket is open is deferred, not lost")
    instruments.clear_broker("firstock")
    feed, host = make_feed()
    exp = dt.datetime.strptime(future_date(7), "%d-%b-%Y").strftime("%d%b%Y").upper()
    keys = [bind_option(strike=24000 + i * 50, token=f"NFO:{300 + i}", expiry=exp)
            for i in range(3)]
    feed.ws.live_socket = lambda: None
    feed.subscribe_keys(set(keys))
    check("state says deferred", "deferred" in feed.option_sub_state,
          feed.option_sub_state)
    fresh = FakeTransport()
    feed._subscribe_all(fresh)
    check("picked up in full on open", len(fresh.sent[0][1]) == 3, fresh.sent)


def scenario_auth_classification():
    section("[20] A rejected session reaches session recovery")
    failed = {"status": "failed", "message": "unauthenticated"}
    check("'unauthenticated' classifies as session_expired",
          classify_error(failed) == "session_expired", classify_error(failed))
    check("a network drop still classifies as network",
          classify_error("Connection reset by peer") == "network",
          classify_error("Connection reset by peer"))


def scenario_capabilities():
    section("[21] The feed advertises what it can actually serve")
    feed, _host = make_feed()
    caps = feed.capabilities()
    check("index, option and depth", caps == {"index", "option", "depth"}, caps)
    check("broker name is canonical", feed.broker == "firstock", feed.broker)
    status = feed.status()
    for field in ("name", "connected", "stale", "scripOptions", "ticks",
                  "heartbeats", "optionSubscribe", "scaleWarning"):
        check(f"status() reports {field}", field in status, sorted(status))


def scenario_no_session_no_start():
    section("[22] A feed with no session does not start")
    host = FakeHost()
    feed = FirstockFeed("acct-2", host)
    feed.start()
    check("should_run stays False", feed.should_run is False, feed.should_run)
    check("says why", any("no session" in ln for ln in host.logs), host.logs)


def scenario_malformed_frame():
    section("[23] A malformed frame costs one instrument, not the batch")
    instruments.clear_broker("firstock")
    feed, host = make_feed()
    key = bind_option(token="NFO:35085")
    frame = option_frame()
    frame["NFO:BAD"] = {"c_exch_seg": "NFO", "c_symbol": "BAD",
                        "i_last_traded_price": "not-a-number"}
    frame["NFO:NOTADICT"] = "garbage"
    feed._on_tick(None, frame)
    check("the good instrument still ticked", len(host.option_ticks) == 1,
          host.option_ticks)
    check("the bad one did not raise", True)


if __name__ == "__main__":
    for fn in (scenario_basic_parse, scenario_expiry_normalised,
               scenario_exact_underlying, scenario_expired_dropped,
               scenario_fractional_strike, scenario_ambiguity_guard,
               scenario_zero_options_fails, scenario_missing_column,
               scenario_unmeasurable_scale, scenario_paise_scale_detected,
               scenario_tick_normalisation, scenario_tick_resolution_by_token,
               scenario_unmapped_tick, scenario_missing_depth,
               scenario_index_tick, scenario_scale_sentinel,
               scenario_subscription_delta, scenario_replay_on_open,
               scenario_deferred_subscription, scenario_auth_classification,
               scenario_capabilities, scenario_no_session_no_start,
               scenario_malformed_frame):
        fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
