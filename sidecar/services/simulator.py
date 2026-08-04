"""Phase-0 market simulator.

Stands in for the real broker engines so the whole stack (Electron → React →
bridge → data plane) can be verified end-to-end without live credentials. In
Phase 1 this module is replaced by the reused engines in ../engines wired to the
same EventHub — the renderer contract does not change.
"""
from __future__ import annotations

import random
import threading
import time

from bridge import events
from bridge.hub import hub

INDICES = [
    {"symbol": "NIFTY", "ltp": 24903.5, "chg": 0.62},
    {"symbol": "BANKNIFTY", "ltp": 55218.9, "chg": -0.28},
    {"symbol": "SENSEX", "ltp": 81544.2, "chg": 0.41},
    {"symbol": "FINNIFTY", "ltp": 26120.7, "chg": 0.18},
]

POSITIONS = [
    {"id": "p1", "symbol": "NIFTY 24900 CE", "side": "BUY", "qty": 150, "entry": 142.5, "ltp": 171.2},
    {"id": "p2", "symbol": "BANKNIFTY 55000 PE", "side": "BUY", "qty": 60, "entry": 288.0, "ltp": 262.4},
    {"id": "p3", "symbol": "NIFTY 25000 CE", "side": "SELL", "qty": 75, "entry": 96.4, "ltp": 88.1},
]

_ATM = 24900
_STEP = 50
_chain_rows = [
    {
        "strike": _ATM + k * _STEP,
        "ce": max(2.0, 190 - k * 32 + random.random() * 6),
        "pe": max(2.0, 190 + k * 30 + random.random() * 6),
        "ceoi": 40 + random.random() * 60 - abs(k) * 4,
        "peoi": 40 + random.random() * 60 - abs(k) * 4,
    }
    for k in range(-6, 7)
]

_net_pnl = 18420.0
_lock = threading.Lock()

# NOTE: broker health is now owned by services.broker_manager (real
# connectivity). The simulator only supplies market data (indices, positions,
# pnl) in this phase.


def snapshot_events() -> list[dict]:
    """State events to replay to a client the moment it connects."""
    return [events.pnl_update(round(_net_pnl, 0))]


def option_chain_snapshot() -> dict:
    with _lock:
        return {
            "symbol": "NIFTY",
            "expiry": "17 Jul",
            "atm": _ATM,
            "rows": [dict(r) for r in _chain_rows],
        }


def _pnl_for(p: dict) -> float:
    direction = 1 if p["side"] == "BUY" else -1
    return (p["ltp"] - p["entry"]) * p["qty"] * direction


def _index_feed_active() -> bool:
    """True once a real broker market WebSocket is streaming index quotes."""
    try:
        from services.broker_manager import manager
        return bool(manager.market_ws_connected)
    except Exception:
        return False


def _broker_connected() -> bool:
    """True once any account is connected — the positions adapter then owns the
    positions/pnl stream, so the simulator stands down."""
    try:
        from services.broker_manager import manager
        return bool(manager.connected_sessions())
    except Exception:
        return False


def _run() -> None:
    global _net_pnl
    while True:
        # indices — suppressed once a real broker feed is live so the UI shows
        # genuine market prices instead of interleaved simulated ones.
        if not _index_feed_active():
            for t in INDICES:
                d = (random.random() - 0.48) * t["ltp"] * 0.0004
                t["ltp"] += d
                t["chg"] += d / t["ltp"] * 100 * 0.3
                hub.publish(events.index_quote(t["symbol"], round(t["ltp"], 2), round(t["chg"], 2)))

        # positions + pnl — suppressed once a broker is connected (the positions
        # adapter publishes the real per-account book + aggregate P&L instead).
        if not _broker_connected():
            for p in POSITIONS:
                p["ltp"] = max(0.5, p["ltp"] + (random.random() - 0.49) * p["ltp"] * 0.006)
                pos = {
                    "id": p["id"], "symbol": p["symbol"], "side": p["side"], "qty": p["qty"],
                    "entry": round(p["entry"], 1), "ltp": round(p["ltp"], 1),
                    "pnl": round(_pnl_for(p), 0), "sl": round(p["entry"] * 0.9, 1),
                    "tsl": round(p["ltp"] * 0.94, 1),
                }
                hub.publish(events.position_update(pos))

            _net_pnl += (1 if random.random() > 0.45 else -1) * round(random.random() * 600)
            hub.publish(events.pnl_update(round(_net_pnl, 0)))

        time.sleep(1.1)


def start() -> None:
    threading.Thread(target=_run, name="charticks-simulator", daemon=True).start()
