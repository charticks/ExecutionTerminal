"""Typed event contract — data plane events pushed to the renderer.

Mirror of charticks/src/bridge/events.ts. These are plain dicts (JSON) so the
reused engines can emit them without importing any web framework types.
"""
from __future__ import annotations

import time


def _now_ms() -> int:
    return int(time.time() * 1000)


def index_quote(symbol: str, ltp: float, change_pct: float) -> dict:
    return {"type": "index_quote", "symbol": symbol, "ltp": ltp,
            "changePct": change_pct, "ts": _now_ms()}


def tick(token: str, ltp: float, volume: int) -> dict:
    return {"type": "tick", "token": token, "ltp": ltp, "volume": volume, "ts": _now_ms()}


def position_update(pos: dict) -> dict:
    """One live or paper position. Beyond price and size it carries whether the
    position is actually being MANAGED and MONITORED right now (`managed`,
    `monitorState`, `monitorDetail`, `source`) — the renderer must never draw an
    unprotected position the same way as a protected one."""
    return {"type": "position_update", **pos}


def monitor_alarm(active: bool, positions: list) -> dict:
    """Raised whenever Charticks cannot actively protect a position it is
    displaying — feed lost, subscription missing, automation paused, a broker
    position nobody is managing. Stays active until monitoring resumes; the
    renderer surfaces it as a persistent banner rather than a toast, because the
    danger lasts as long as the condition does."""
    return {"type": "monitor_alarm", "active": active, "positions": positions,
            "ts": _now_ms()}


def hedge_orphaned(hedge_id: str, symbol: str, qty: int, lots: int,
                   parent: str, pnl: float) -> dict:
    """The last short a protective hedge was covering has closed.

    A decision only the user can make, so it is asked rather than assumed:
    closing the hedge automatically is an exit nobody requested, and keeping it
    silently leaves a long position they never intended to hold on its own. The
    hedge stays exactly as it is until they answer.
    """
    return {"type": "hedge_orphaned", "hedgeId": hedge_id, "symbol": symbol,
            "qty": qty, "lots": lots, "parent": parent, "pnl": pnl,
            "ts": _now_ms()}


def pnl_update(net_pnl: float) -> dict:
    return {"type": "pnl_update", "netPnl": net_pnl, "ts": _now_ms()}


def order_update(order_id: str, symbol: str, side: str, qty: int, price: float,
                 status: str) -> dict:
    """Real (or paper) order lifecycle update — mirror of charticks
    src/bridge/events.ts OrderUpdate."""
    return {"type": "order_update", "id": order_id, "symbol": symbol,
            "side": side, "qty": qty, "price": price, "status": status,
            "ts": _now_ms()}


def broker_status(broker: str, health: str, detail: str | None = None,
                  account: str | None = None) -> dict:
    return {"type": "broker_status", "broker": broker, "health": health,
            "detail": detail, "account": account}


def risk_event(halted: bool, reason: str | None = None) -> dict:
    return {"type": "risk_event", "halted": halted, "reason": reason, "ts": _now_ms()}


def log_line(level: str, message: str) -> dict:
    return {"type": "log_line", "level": level, "message": message, "ts": _now_ms()}


def option_chain_update(snapshot: dict) -> dict:
    """Pushed on every option-chain tick (see BrokerManager.add_option_tick_listener)
    so the renderer updates instantly instead of on a REST poll cycle."""
    return {"type": "option_chain_update", **snapshot}


def paper_state(orders: list, trades: list, positions: list, net_pnl: float) -> dict:
    """Full paper-book snapshot pushed by services.paper_engine on every change
    or relevant tick. The renderer's paper stores replace their state with this
    so Paper is driven by the sidecar engine exactly as Live is driven by the
    broker book. Mirror of charticks/src/bridge/events.ts PaperStateEvent."""
    return {"type": "paper_state", "orders": orders, "trades": trades,
            "positions": positions, "netPnl": net_pnl, "ts": _now_ms()}


def connection_health(state: str, accounts_connected: int, detail: str | None = None) -> dict:
    """Aggregate connectivity state for the header badge — published by
    services.reliability.health_monitor on state transitions only.
    state: "connected" | "reconnecting" | "auth_failed" | "down"
    """
    return {"type": "connection_health", "state": state,
            "accountsConnected": accounts_connected, "detail": detail, "ts": _now_ms()}
