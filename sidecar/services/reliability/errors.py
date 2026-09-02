"""Broker error classification — one source of truth for "is this a dead
session or just a network blip?" so PositionsAdapter, SessionManager, and
WebSocketManager all agree on what triggers re-authentication.
"""
from __future__ import annotations

from typing import Any, Literal

Classification = Literal["session_expired", "network", "unknown"]

# Substrings that indicate the broker rejected our token/session outright.
# Angel: errorCode AG8001 / "Invalid Token" / "Invalid Session ID" (getPosition,
#   order APIs). Kotak: totp/mpin session dies with "session expired" /
#   "invalid session" in the error/Error field. Dhan: get_fund_limits() etc.
#   return {"status": "failure", "remarks"/"message": "..."} with wording like
#   "invalid token"/"session expired"/"unauthorized".
_SESSION_EXPIRED_MARKERS = (
    "ag8001",
    "invalid token",
    "invalid session",
    "session expired",
    "token expired",
    "unauthorized",
    "authentication failed",
    # ICICI Breeze: the SDK raises this exact wording when the daily session
    # token is dead (config.py AUTHENICATION_EXCEPTION, sic), and REST errors
    # mention the session key by name.
    "could not authenticate credentials",
    "session key",
    "api session",
    # Firstock: the websocket handshake answers a dead jKey with
    # {"status":"failed","message":"unauthenticated"}. Without this marker the
    # feed would classify it as "unknown" and retry the same dead session
    # forever instead of handing it to session recovery.
    "unauthenticated",
    # Firstock REST: every authenticated endpoint answers a dead session with
    # name="INVALID_JKEY". Matched on the machine-readable name rather than the
    # prose, exactly as Angel is matched on AG8001 — a bare "jkey" would also
    # catch "jKey is required", which is our own bug and must not trigger a
    # re-login.
    "invalid_jkey",
)

# Substrings that indicate a transient network/connectivity issue rather than
# a broker-side rejection — these should NOT trigger re-authentication.
_NETWORK_MARKERS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection closed",
    "max retry",
    "temporarily unavailable",
    "name resolution",
    "econnreset",
)


def _text_of(exc_or_response: Any) -> str:
    if isinstance(exc_or_response, dict):
        parts = [
            str(exc_or_response.get("errorCode", "")),
            str(exc_or_response.get("message", "")),
            str(exc_or_response.get("error", "")),
            str(exc_or_response.get("Error", "")),
            str(exc_or_response.get("remarks", "")),
        ]
        return " ".join(parts).lower()
    return str(exc_or_response).lower()


# Checked BEFORE the session markers, and wins over them. Firstock reports a
# rejected source address as "UNAUTHORIZED: Invalid IP Address", which contains
# a session marker but is not a session problem: re-authenticating repeats the
# request from the same address and fails identically, forever. A wall is not a
# closed door.
_NEVER_SESSION_MARKERS = (
    "invalid ip address",
)


def classify_error(exc_or_response: Any) -> Classification:
    """Classify a broker exception/response dict as session_expired, network,
    or unknown. Callers use this to decide whether to trigger the
    re-authentication + reconnect workflow (session_expired) versus a plain
    retry (network) versus just logging (unknown)."""
    text = _text_of(exc_or_response)
    if any(marker in text for marker in _NEVER_SESSION_MARKERS):
        return "unknown"
    if any(marker in text for marker in _SESSION_EXPIRED_MARKERS):
        return "session_expired"
    if any(marker in text for marker in _NETWORK_MARKERS):
        return "network"
    return "unknown"
