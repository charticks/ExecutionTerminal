"""
Kotak Neo session diagnostic — read-only, places NO order.

Purpose
-------
Reproduces the exact login flow the app uses and inspects the session fields
that order placement depends on. The live bot saw Kotak reject an order with:

    {'stCode': 100008, 'errMsg': 'unauthorized', 'stat': 'Not_Ok'}

The suspected cause is an EMPTY `hsServerId` in the totp_validate() response,
which the Neo SDK forwards as the `sId` routing query param on place_order.
With an empty sId, Kotak's trade server returns "unauthorized".

This script confirms that hypothesis WITHOUT placing a real order: it logs in,
prints the routing fields, and calls a read-only authorized endpoint
(`positions()`) that needs the same `serverId`.

Run during market hours:
    python tools/kotak_session_check.py
"""

import os
import sys

# Make project-root modules (config.py, neo_api_client) importable when run
# from anywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pyotp
import config
from neo_api_client import NeoAPI


def _line(title):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)


def main():
    _line("Step 0 — init NeoAPI (prod)")
    kotak = NeoAPI(
        consumer_key=config.KOTAK_CONSUMER_KEY,
        environment="prod",
        access_token=None,
        neo_fin_key=None,
    )
    print("NeoAPI initialised OK")

    _line("Step 1 — totp_login()")
    totp = pyotp.TOTP(config.KOTAK_TOTP_SECRET).now()
    print(f"TOTP = {totp}")
    login_resp = kotak.totp_login(
        mobile_number=str(config.KOTAK_MOBILE_NO),
        ucc=str(config.KOTAK_UCC),
        totp=totp,
    )
    print(f"raw login response: {login_resp}")

    _line("Step 2 — totp_validate(mpin)")
    val = kotak.totp_validate(mpin=str(config.KOTAK_MPIN))
    print(f"raw validate response: {val}")

    # ── The critical fields ──────────────────────────────────────────
    cfg = kotak.configuration
    data = val.get("data", {}) if isinstance(val, dict) else {}
    hs_server_id = data.get("hsServerId")

    _line("ROUTING FIELDS (what place_order uses)")
    print(f"hsServerId (raw from validate) : {hs_server_id!r}")
    print(f"configuration.serverId         : {getattr(cfg, 'serverId', None)!r}  "
          "<-- sent as place_order ?sId=")
    print(f"configuration.edit_sid set?    : {bool(getattr(cfg, 'edit_sid', None))}")
    print(f"configuration.edit_token set?  : {bool(getattr(cfg, 'edit_token', None))}")
    print(f"configuration.base_url         : {getattr(cfg, 'base_url', None)!r}")

    server_id_empty = not getattr(cfg, "serverId", None)
    if server_id_empty:
        print("\n>>> serverId is EMPTY. This is the likely cause of "
              "'unauthorized' on order placement.")
    else:
        print("\n>>> serverId is POPULATED. The empty-hsServerId theory does "
              "NOT apply to this session — orders should route fine.")

    # ── Read-only authorized call (no order placed) ──────────────────
    _line("Step 3 — positions() [read-only, needs serverId]")
    try:
        pos = kotak.positions()
        print(f"positions() response: {pos}")
        txt = str(pos).lower()
        if "unauthorized" in txt or "not_ok" in txt:
            print("\n>>> positions() ALSO returned unauthorized → confirms the "
                  "session/serverId problem (not the order code).")
        else:
            print("\n>>> positions() returned data → session is authorized; an "
                  "order rejection would point elsewhere.")
    except Exception as e:
        print(f"positions() raised: {e}")

    _line("CONCLUSION")
    if server_id_empty:
        print("Empty serverId/hsServerId → pursue Kotak-side checks:\n"
              "  1. Trade API plan active (Trade scope, not expired)\n"
              "  2. No stale/duplicate session (fully close app, re-login)\n"
              "  3. KOTAK_CONSUMER_KEY belongs to this UCC's Trade API card\n"
              "  4. If all fine, contact Kotak API support re: empty hsServerId")
    else:
        print("serverId present → the earlier rejection was likely a transient/"
              "stale session. Retry a real trade.")


if __name__ == "__main__":
    main()
