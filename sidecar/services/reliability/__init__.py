"""Broker Connection Reliability Layer.

Detects session expiry (e.g. Angel's AG8001), WebSocket drops, and heartbeat
loss, and drives automatic re-authentication + reconnection + subscription
restoration — so a transient failure never requires the user to manually
reconnect. See sidecar/services/broker_manager.py for how this plugs in.
"""
from __future__ import annotations
