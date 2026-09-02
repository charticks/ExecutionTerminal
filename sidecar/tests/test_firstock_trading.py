"""Regression tests for Firstock trading: orders, books, positions, margin.

    python sidecar/tests/test_firstock_trading.py

The central assertion running through this file is that Charticks' internal
model (NRML / MIS / CNC, MARKET / LIMIT / SL / SL-M, BUY / SELL) is translated
to Firstock's codes in exactly ONE place — the adapter — and that the order
engine never speaks a Firstock word.

Only the network boundary is stubbed. The real client, the real order router,
the real order-sync mapper, the real position reader and the real margin
contract all run.
"""
import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:                   # pragma: no cover
        pass

SIDECAR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHARTICKS_DATA_DIR"] = tempfile.mkdtemp(prefix="charticks-fstrade-")
os.environ["CHARTICKS_LOG_DIR"] = os.environ["CHARTICKS_DATA_DIR"]
sys.path.insert(0, SIDECAR)

from services.feeds import firstock_client                            # noqa: E402
from services.feeds.firstock_client import (                          # noqa: E402
    ORDER_TYPE, PRODUCT, SIDE, FirstockClient, FirstockError)
from services.instruments import InstrumentKey, instruments           # noqa: E402
from services.order_sync.base import (                                # noqa: E402
    FILLED, PARTIAL, PENDING, REJECTED, CANCELLED)
from services.order_sync.brokers import firstock_orders               # noqa: E402
from services.idempotency.base import Tier, adapter_for               # noqa: E402
from services import broker_positions                                 # noqa: E402
from services.margin import checker_for                               # noqa: E402
from services.margin.base import MarginRequest, MarginUnavailable     # noqa: E402
from services.order_manager import order_manager                      # noqa: E402
from services.broker_manager import manager                           # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <-- ' + str(detail)}")


def section(title):
    print(f"\n{title}")


EXPIRY = "29SEP2026"
KEY = InstrumentKey.option("NIFTY", EXPIRY, 24000, "CE")
CONTRACT = {"exchange": "NFO", "tradingSymbol": "NIFTY29SEP26C24000",
            "token": "35085", "subscribeId": "NFO:35085",
            "lotSize": 65, "tickSize": 0.05}


class Wire:
    """Replaces firstock_client._post. Records calls, returns scripted data."""

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.raises = {}

    def install(self):
        firstock_client._post = self          # type: ignore[assignment]
        return self

    def __call__(self, url, payload, timeout):
        key = url.rsplit("/", 1)[-1]
        self.calls.append({"endpoint": key, "payload": dict(payload)})
        if key in self.raises:
            raise self.raises[key]
        return self.responses.get(key, {})

    def payload_for(self, key):
        for call in self.calls:
            if call["endpoint"] == key:
                return call["payload"]
        return None


def client():
    return FirstockClient(user_id="AB1234", jkey="tok")


# ════════════════════════════════════════════════════════════════════════════
def scenario_product_mapping():
    section("[1] Charticks' product model maps to Firstock's confirmed codes")
    check("NRML → M (regular / carry forward)", PRODUCT["NRML"] == "M", PRODUCT)
    check("MIS → I (intraday)", PRODUCT["MIS"] == "I", PRODUCT)
    check("CNC → C (cash & carry)", PRODUCT["CNC"] == "C", PRODUCT)
    check("exactly three products mapped", set(PRODUCT) == {"NRML", "MIS", "CNC"}, PRODUCT)

    w = Wire().install()
    w.responses["placeOrder"] = {"orderNumber": "251"}
    for internal, code in PRODUCT.items():
        w.calls.clear()
        client().place_order(exchange="NFO", trading_symbol="X", side="BUY", qty=65,
                             order_type="MARKET", product=internal)
        check(f"placing {internal} sends product={code}",
              w.payload_for("placeOrder")["product"] == code,
              w.payload_for("placeOrder")["product"])


def scenario_order_type_mapping():
    section("[2] All four order types map, including SL and SL-M")
    check("MARKET → MKT", ORDER_TYPE["MARKET"] == "MKT")
    check("LIMIT → LMT", ORDER_TYPE["LIMIT"] == "LMT")
    check("SL → SL-LMT", ORDER_TYPE["SL"] == "SL-LMT")
    check("SL-M → SL-MKT", ORDER_TYPE["SL-M"] == "SL-MKT")
    check("BUY → B, SELL → S", SIDE == {"BUY": "B", "SELL": "S"}, SIDE)

    w = Wire().install()
    w.responses["placeOrder"] = {"orderNumber": "251"}
    for internal, code in ORDER_TYPE.items():
        w.calls.clear()
        client().place_order(exchange="NFO", trading_symbol="X", side="SELL", qty=65,
                             order_type=internal, price=12.5, trigger_price=11.0)
        p = w.payload_for("placeOrder")
        check(f"{internal} sends priceType={code}", p["priceType"] == code, p["priceType"])
    check("SELL sends transactionType=S",
          w.payload_for("placeOrder")["transactionType"] == "S")


def scenario_price_and_trigger_fields():
    section("[3] price / triggerPrice are sent per order type, never omitted")
    w = Wire().install()
    w.responses["placeOrder"] = {"orderNumber": "251"}

    def place(kind):
        w.calls.clear()
        client().place_order(exchange="NFO", trading_symbol="X", side="BUY", qty=65,
                             order_type=kind, price=12.5, trigger_price=11.0)
        return w.payload_for("placeOrder")

    p = place("MARKET")
    check("MARKET: price 0, trigger 0", (p["price"], p["triggerPrice"]) == ("0", "0"), p)
    p = place("LIMIT")
    check("LIMIT: price set, trigger 0",
          (p["price"], p["triggerPrice"]) == ("12.50", "0"), p)
    p = place("SL")
    check("SL: both set", (p["price"], p["triggerPrice"]) == ("12.50", "11.00"), p)
    p = place("SL-M")
    check("SL-M: price 0, trigger set",
          (p["price"], p["triggerPrice"]) == ("0", "11.00"), p)


def scenario_market_protection():
    section("[4] Market protection rides only on the order types that need it")
    w = Wire().install()
    w.responses["placeOrder"] = {"orderNumber": "251"}

    def place(kind):
        w.calls.clear()
        client().place_order(exchange="NFO", trading_symbol="X", side="BUY", qty=65,
                             order_type=kind, price=12.5, trigger_price=11.0)
        return w.payload_for("placeOrder")

    check("MARKET carries mkt_protection", "mkt_protection" in place("MARKET"))
    check("SL-M carries mkt_protection", "mkt_protection" in place("SL-M"))
    check("LIMIT does NOT", "mkt_protection" not in place("LIMIT"))
    check("SL does NOT", "mkt_protection" not in place("SL"))
    check("the value is a positive percentage",
          float(place("MARKET")["mkt_protection"]) > 0,
          place("MARKET")["mkt_protection"])


def scenario_unknown_values_raise():
    section("[5] An unmappable attribute RAISES — never a silent default")
    Wire().install().responses["placeOrder"] = {"orderNumber": "1"}
    for kwargs, what in (({"product": "BRACKET"}, "product"),
                         ({"order_type": "GTT"}, "order type"),
                         ({"side": "HOLD"}, "side"),
                         ({"validity": "GTC"}, "validity")):
        args = {"exchange": "NFO", "trading_symbol": "X", "side": "BUY",
                "qty": 65, "order_type": "MARKET"}
        args.update(kwargs)
        try:
            client().place_order(**args)
            check(f"an unknown {what} is refused", False, "no exception")
        except FirstockError as e:
            check(f"an unknown {what} is refused",
                  e.name == "UNSUPPORTED_ORDER", e.name)
            check(f"the {what} refusal names the offending value",
                  kwargs[list(kwargs)[0]] in str(e), str(e))
            check(f"the {what} refusal lists what IS supported",
                  "Supported:" in str(e), str(e))


def scenario_place_result():
    section("[6] Placement returns the order number, or refuses")
    w = Wire().install()
    w.responses["placeOrder"] = {"orderNumber": "25081800012345"}
    num = client().place_order(exchange="NFO", trading_symbol="X", side="BUY",
                               qty=65, order_type="MARKET", remarks="CH0123456789001")
    check("order number returned", num == "25081800012345", num)
    check("remarks carries the client order id",
          w.payload_for("placeOrder")["remarks"] == "CH0123456789001",
          w.payload_for("placeOrder")["remarks"])

    section("       ...and a 200 with no order number is a failure")
    w2 = Wire().install()
    w2.responses["placeOrder"] = {"requestTime": "10:00:00 18-08-2026"}
    try:
        client().place_order(exchange="NFO", trading_symbol="X", side="BUY",
                             qty=65, order_type="MARKET")
        check("refuses a numberless acceptance", False, "no exception")
    except FirstockError as e:
        check("refuses a numberless acceptance", e.name == "NO_ORDER_NUMBER", e.name)


def scenario_modify_and_cancel():
    section("[7] A refused modify/cancel arrives as HTTP 200 and must not pass")
    w = Wire().install()
    w.responses["modifyOrder"] = {"orderNumber": "251", "rejreason": ""}
    out = client().modify_order(order_number="251", exchange="NFO",
                                trading_symbol="X", qty=130, order_type="LIMIT",
                                price=13.0)
    check("a clean modify returns the order number", out == "251", out)
    p = w.payload_for("modifyOrder")
    check("the order is RESTATED, not diffed",
          all(k in p for k in ("orderNumber", "exchange", "tradingSymbol", "product",
                               "priceType", "quantity", "price", "triggerPrice",
                               "retention")), sorted(p))

    w = Wire().install()
    w.responses["modifyOrder"] = {"orderNumber": "251",
                                  "rejreason": "SAF:order is not open to modify"}
    try:
        client().modify_order(order_number="251", exchange="NFO", trading_symbol="X",
                              qty=130, order_type="LIMIT", price=13.0)
        check("a rejreason on modify is a failure", False, "returned ok")
    except FirstockError as e:
        check("a rejreason on modify is a failure", "not open to modify" in str(e), str(e))

    w = Wire().install()
    w.responses["cancelOrder"] = {"orderNumber": "251",
                                  "rejreason": "SAF:order is not open to cancel"}
    try:
        client().cancel_order("251")
        check("a rejreason on cancel is a failure", False, "returned ok")
    except FirstockError as e:
        check("a rejreason on cancel is a failure", "not open to cancel" in str(e), str(e))

    w = Wire().install()
    w.responses["cancelOrder"] = {"orderNumber": "251"}
    check("a clean cancel returns the order number",
          client().cancel_order("251") == "251")


# ── order synchronization ───────────────────────────────────────────────────
def order_row(**kw):
    row = {"orderNumber": "251", "status": "OPEN", "quantity": "130",
           "fillShares": "", "averagePrice": "0.00", "rejectReason": "",
           "tradingSymbol": "NIFTY29SEP26C24000", "transactionType": "B",
           "remarks": ""}
    row.update(kw)
    return row


def scenario_order_sync():
    section("[8] Firstock's status vocabulary maps to the canonical lifecycle")
    w = Wire().install()
    w.responses["orderBook"] = [
        order_row(orderNumber="1", status="OPEN"),
        order_row(orderNumber="2", status="COMPLETE", fillShares="130",
                  averagePrice="12.75"),
        order_row(orderNumber="3", status="REJECTED",
                  rejectReason="RED:RULE:Margin shortfall"),
        order_row(orderNumber="4", status="CANCELED"),
        order_row(orderNumber="5", status="TRIGGER_PENDING"),
        order_row(orderNumber="6", status="INVALID"),
    ]
    got = {o.order_id: o for o in firstock_orders(client())}
    check("OPEN → PENDING", got["1"].status == PENDING, got["1"].status)
    check("COMPLETE → FILLED", got["2"].status == FILLED, got["2"].status)
    check("filled quantity read", got["2"].filled_qty == 130, got["2"].filled_qty)
    check("average price read", got["2"].avg_price == 12.75, got["2"].avg_price)
    check("REJECTED → REJECTED", got["3"].status == REJECTED, got["3"].status)
    check("rejection reason carried",
          "Margin shortfall" in got["3"].reason, got["3"].reason)
    check("CANCELED (one L) → CANCELLED", got["4"].status == CANCELLED, got["4"].status)
    check("TRIGGER_PENDING → PENDING", got["5"].status == PENDING, got["5"].status)
    check("INVALID → REJECTED", got["6"].status == REJECTED, got["6"].status)
    check("the broker's own word is kept for the log",
          got["4"].raw_status == "CANCELED", got["4"].raw_status)

    section("       ...and a part-filled OPEN order is PARTIAL, from the quantities")
    w.responses["orderBook"] = [order_row(orderNumber="7", status="OPEN",
                                          quantity="130", fillShares="65",
                                          averagePrice="12.50")]
    partial = firstock_orders(client())[0]
    check("OPEN with fills → PARTIAL", partial.status == PARTIAL, partial.status)
    check("only the filled quantity is booked", partial.filled_qty == 65,
          partial.filled_qty)


def scenario_idempotency():
    section("[9] Idempotency rides on `remarks`, echoed by the order book")
    adapter = adapter_for("firstock")
    check("registered", adapter is not None)
    check("tag-echo tier", adapter.tier == Tier.TAG_ECHO, adapter.tier)
    check("accepts a client id", adapter.accepts_client_id is True)
    check("reads it back from `remarks`", adapter.tag_keys == ("remarks",),
          adapter.tag_keys)
    w = Wire().install()
    w.responses["orderBook"] = [order_row(remarks="CH0123456789001")]
    rows = adapter.rows(client())
    check("rows() returns RAW dicts, not normalised orders",
          isinstance(rows[0], dict), type(rows[0]))
    check("an unrecognised status still counts as an order",
          len(adapter.rows(client())) == 1)


def scenario_trade_book():
    section("[10] Trade book fill prices are normalised by the row's own precision")
    w = Wire().install()
    w.responses["tradeBook"] = [
        {"orderNumber": "251", "fillPrice": 2837, "pricePrecision": "2",
         "fillQuantity": "1", "tradingSymbol": "PSB-EQ"},
        {"orderNumber": "252", "fillPrice": "12.75", "pricePrecision": "2",
         "fillQuantity": "65", "tradingSymbol": "NIFTY29SEP26C24000"},
    ]
    rows = client().trade_book()
    check("a scaled integer becomes rupees", rows[0]["fillPriceRupees"] == 28.37,
          rows[0]["fillPriceRupees"])
    check("a decimal string is left alone", rows[1]["fillPriceRupees"] == 12.75,
          rows[1]["fillPriceRupees"])
    check("the original value survives beside it", rows[0]["fillPrice"] == 2837,
          rows[0]["fillPrice"])


def scenario_holdings():
    section("[11] Holdings are readable (and deliberately not wired into the app)")
    w = Wire().install()
    w.responses["holdings"] = [{"exchange": "NSE", "tradingSymbol": "VIKASECO-EQ"}]
    rows = client().holdings()
    check("returns rows", len(rows) == 1, rows)
    check("no holdings reader is registered for ANY broker",
          not hasattr(broker_positions, "_holdings_readers"))


# ── positions ───────────────────────────────────────────────────────────────
def position_row(**kw):
    row = {"netQuantity": "130", "tradingSymbol": "NIFTY29SEP26C24000",
           "token": "35085", "exchange": "NFO", "product": "M",
           "lastTradedPrice": "13.20", "netAveragePrice": "12.50",
           "totalPNL": "45.50", "dayBuyAveragePrice": "12.50",
           "daySellAveragePrice": "0.00", "cfBuyAmt": "", "cfBuyQty": "",
           "cfSellAmt": "", "cfSellQty": ""}
    row.update(kw)
    return row


def scenario_positions():
    section("[12] The position book resolves by token and reports Charticks' model")
    instruments.clear_broker("firstock")
    instruments.bind("firstock", KEY, "NFO:35085")
    instruments.alias("firstock", KEY, "35085")
    w = Wire().install()
    w.responses["positionBook"] = [position_row()]
    rows = broker_positions.read("acct", "firstock", client())
    check("one position", len(rows) == 1, rows)
    pos = rows[0]
    check("resolved to the canonical key", pos.key == KEY, pos.key)
    check("long side", pos.side == "BUY" and pos.qty == 130, (pos.side, pos.qty))
    check("entry price", pos.avg_entry == 12.50, pos.avg_entry)
    check("ltp", pos.ltp == 13.20, pos.ltp)
    check("pnl", pos.pnl == 45.50, pos.pnl)
    check("product translated BACK to Charticks' model",
          pos.product == "NRML", pos.product)

    section("       ...a short is signed, and its cost basis is the SELL side")
    w.responses["positionBook"] = [position_row(
        netQuantity="-130", netAveragePrice="0.00", product="I",
        dayBuyAveragePrice="0.00", daySellAveragePrice="18.75")]
    pos = broker_positions.read("acct", "firstock", client())[0]
    check("short detected", pos.side == "SELL" and pos.qty == 130, (pos.side, pos.qty))
    check("cost basis is NON-ZERO (the Angel short bug)",
          pos.avg_entry == 18.75, pos.avg_entry)
    check("MIS translated back", pos.product == "MIS", pos.product)

    section("       ...a LOSING position reports a negative P&L, not zero")
    w.responses["positionBook"] = [position_row(totalPNL="-1234.50")]
    pos = broker_positions.read("acct", "firstock", client())[0]
    check("negative pnl survives", pos.pnl == -1234.50, pos.pnl)

    section("       ...and a carry-forward leg is amount ÷ quantity")
    w.responses["positionBook"] = [position_row(
        netAveragePrice="0.00", dayBuyAveragePrice="0.00",
        cfBuyAmt="1625.00", cfBuyQty="130")]
    pos = broker_positions.read("acct", "firstock", client())[0]
    check("cf average derived", pos.avg_entry == 12.50, pos.avg_entry)

    section("       ...a flat row is skipped, an unknown token is reported foreign")
    w.responses["positionBook"] = [position_row(netQuantity="0"),
                                   position_row(token="99999", exchange="NFO")]
    rows = broker_positions.read("acct", "firstock", client())
    check("flat position dropped", len(rows) == 1, rows)
    check("unknown token yields NO key rather than a wrong one",
          rows[0].key is None, rows[0].key)
    check("it is still visible as a foreign row",
          rows[0].symbol == "NIFTY29SEP26C24000", rows[0].symbol)


def scenario_no_symbol_parsing():
    section("[13] Firstock symbols are never parsed back into a contract")
    for symbol in ("NIFTY29SEP26C24000", "BANKEX26AUG54000CE", "SENSEX26O0170700CE"):
        check(f"from_symbol refuses {symbol}",
              InstrumentKey.from_symbol(symbol) != KEY,
              InstrumentKey.from_symbol(symbol))


# ── margin ──────────────────────────────────────────────────────────────────
class FakeScrip:
    def __init__(self, contract=CONTRACT):
        self.contract = contract
        self.option_count = 12988

    def contract_for(self, key):
        return self.contract

    def explain_miss(self, key):
        return "requested strike 99999; NIFTY lists 231 strikes, range 18500-30000"


def with_scrip(scrip, fn):
    original = manager.router.scrip_of
    manager.router.scrip_of = lambda broker, session=None: scrip
    try:
        fn()
    finally:
        manager.router.scrip_of = original


def margin_request(**kw):
    args = dict(underlying="NIFTY", expiry=EXPIRY, strike=24000, opt_type="CE",
                side="BUY", qty=130, lots=2, order_type="MARKET", price=0.0,
                product="NRML", exchange="NFO", ltp=12.5)
    args.update(kw)
    return MarginRequest(**args)


def scenario_margin():
    section("[14] Margin: requirement and balance from one call")

    def body():
        w = Wire().install()
        w.responses["orderMargin"] = {"marginOnNewOrder": "78046.80",
                                      "availableMargin": "125000.00",
                                      "cash": "125000.00"}
        quote = checker_for("firstock")(client(), margin_request())
        check("requirement read", quote.required == 78046.80, quote.required)
        check("available read", quote.available == 125000.00, quote.available)
        check("sufficient", quote.sufficient is True)
        check("not an estimate", quote.estimated_requirement is False)
        check("the source is named", "orderMargin" in quote.source, quote.source)
        p = w.payload_for("orderMargin")
        check("product translated for the margin call too",
              p["product"] == "M", p["product"])
        check("order type translated", p["priceType"] == "MKT", p["priceType"])

        section("       ...a shortfall is reported with the gap")
        w.responses["orderMargin"] = {"marginOnNewOrder": "78046.80",
                                      "availableMargin": "1000.00"}
        quote = checker_for("firstock")(client(), margin_request())
        check("insufficient", quote.sufficient is False)
        check("shortfall computed", quote.shortfall == 77046.80, quote.shortfall)

        section("       ...a failed call falls back to premium for a LONG only")
        w2 = Wire().install()
        w2.raises["orderMargin"] = ConnectionError("timed out")
        w2.responses["limit"] = {"availableMargin": "125000.00"}
        quote = checker_for("firstock")(client(), margin_request(side="BUY"))
        check("long option falls back to premium debit",
              quote.required == 12.5 * 130 and quote.estimated_requirement,
              (quote.required, quote.estimated_requirement))
        try:
            checker_for("firstock")(client(), margin_request(side="SELL"))
            check("a SHORT is refused rather than estimated", False, "returned a quote")
        except MarginUnavailable as e:
            check("a SHORT is refused rather than estimated", True)

    with_scrip(FakeScrip(), body)

    section("       ...and an unlisted contract is refused, not guessed")

    def body_missing():
        Wire().install()
        try:
            checker_for("firstock")(client(), margin_request(strike=99999))
            check("unlisted contract refused", False, "returned a quote")
        except MarginUnavailable as e:
            check("unlisted contract refused", "does not list" in str(e), str(e))

    with_scrip(FakeScrip(contract=None), body_missing)


# ── order router integration ────────────────────────────────────────────────
class FakeClient:
    def __init__(self):
        self.placed = []
        self.modified = []
        self.cancelled = []

    def place_order(self, **kw):
        self.placed.append(kw)
        return "25081800012345"

    def modify_order(self, **kw):
        self.modified.append(kw)
        return kw["order_number"]

    def cancel_order(self, order_number):
        self.cancelled.append(order_number)
        return order_number


class FakeOrder:
    order_id = "25081800012345"
    underlying, expiry, strike, opt_type = "NIFTY", EXPIRY, 24000, "CE"
    side, qty, price = "BUY", 130, 12.5
    product, order_type, validity = "NRML", "LIMIT", "DAY"
    symbol = "NIFTY 29SEP2026 24000 CE"
    filled_qty = 0


def scenario_router():
    section("[15] The order router passes CANONICAL values, never Firstock codes")

    def body():
        fake = FakeClient()
        res = order_manager._place_firstock(
            "acct", fake, "NIFTY", EXPIRY, 24000, "CE", "BUY", 130,
            "MARKET", 0.0, product="NRML", validity="DAY",
            client_order_id="CH0123456789001")
        check("placement ok", res.get("ok") is True, res)
        check("order id returned", res["orderId"] == "25081800012345", res)
        sent = fake.placed[0]
        check("the router sent Charticks' product, not a code",
              sent["product"] == "NRML", sent["product"])
        check("the router sent Charticks' order type",
              sent["order_type"] == "MARKET", sent["order_type"])
        check("the router sent Charticks' side", sent["side"] == "BUY", sent["side"])
        check("exchange and symbol came from the master",
              (sent["exchange"], sent["trading_symbol"])
              == ("NFO", "NIFTY29SEP26C24000"), sent)
        check("the client order id was forwarded",
              sent["remarks"] == "CH0123456789001", sent["remarks"])

        res = order_manager._modify_firstock("acct", fake, FakeOrder(), 13.5, 195)
        check("modify ok", res.get("ok") is True, res)
        check("modify restates the contract",
              fake.modified[0]["trading_symbol"] == "NIFTY29SEP26C24000")
        check("a price on a modify implies LIMIT",
              fake.modified[0]["order_type"] == "LIMIT")

        res = order_manager._cancel_firstock("acct", fake, FakeOrder())
        check("cancel ok", res.get("ok") is True, res)
        check("cancel used the order id",
              fake.cancelled == ["25081800012345"], fake.cancelled)

    with_scrip(FakeScrip(), body)

    section("       ...and an unlisted contract is refused with a diagnosis")

    def body_missing():
        fake = FakeClient()
        res = order_manager._place_firstock(
            "acct", fake, "NIFTY", EXPIRY, 99999, "CE", "BUY", 130,
            "MARKET", 0.0)
        check("placement refused", res.get("ok") is False, res)
        check("nothing was sent to the broker", not fake.placed, fake.placed)
        check("the refusal explains what IS listed",
              "231 strikes" in res.get("error", ""), res.get("error"))
        check("it does not blame the expiry generically",
              "does not contain" in res.get("error", ""), res.get("error"))

    with_scrip(FakeScrip(contract=None), body_missing)


def scenario_dispatch_registered():
    section("[16] Every trading seam is registered for Firstock")
    for table, name in ((order_manager._LIVE_PLACERS, "placer"),
                        (order_manager._LIVE_MODIFIERS, "modifier"),
                        (order_manager._LIVE_CANCELLERS, "canceller")):
        check(f"{name} registered", "firstock" in table, sorted(table))
    check("placer resolves to a bound method",
          callable(order_manager._live_placer("firstock")))
    check("modifier resolves", callable(order_manager._live_modifier("firstock")))
    check("canceller resolves", callable(order_manager._live_canceller("firstock")))
    check("no existing broker was displaced",
          all(b in order_manager._LIVE_PLACERS
              for b in ("angel", "kotak", "dhan", "icici")),
          sorted(order_manager._LIVE_PLACERS))


if __name__ == "__main__":
    for fn in (scenario_product_mapping, scenario_order_type_mapping,
               scenario_price_and_trigger_fields, scenario_market_protection,
               scenario_unknown_values_raise, scenario_place_result,
               scenario_modify_and_cancel, scenario_order_sync,
               scenario_idempotency, scenario_trade_book, scenario_holdings,
               scenario_positions, scenario_no_symbol_parsing, scenario_margin,
               scenario_router, scenario_dispatch_registered):
        fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
