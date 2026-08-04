import ssl
import socket
import base64
import os
import struct
import threading
import time
import json
import datetime as dt


class DhanDataEngine:
    """
    Custom Dhan v2 WebSocket client — pure Python, no external dependencies.

    Uses built-in ssl + socket for the WebSocket connection, avoiding the
    asyncio.Lock(loop=...) incompatibility in websockets 8.x on Python 3.13.

    Same external interface as OptionChainEngine / KotakDataEngine:
      on_tick_cb(angel_token, ltp, cum_vol, now)
      tick_store  {token: {"ltp": float, "volume": int, "timestamp": datetime}}
    """

    IDX      = 0
    NSE      = 1
    NSE_FNO  = 2
    NSE_CURR = 3
    BSE      = 4
    MCX      = 5
    BSE_CURR = 7
    BSE_FNO  = 8

    Ticker = 15

    _EXCH_MAP = {
        0: "IDX_I",
        1: "NSE_EQ",
        2: "NSE_FNO",
        3: "NSE_CURRENCY",
        4: "BSE_EQ",
        5: "MCX_COMM",
        7: "BSE_CURRENCY",
        8: "BSE_FNO",
    }
    _WS_HOST = "api-feed.dhan.co"
    _WS_PORT = 443

    def __init__(self, dhan_context, dhan_to_angel: dict):
        self.client_id      = dhan_context.get_client_id()
        self.access_token   = dhan_context.get_access_token()
        self.dhan_to_angel  = dhan_to_angel
        self.on_tick_cb     = None
        self._ltp_cache     = {}
        self._cum_vol_cache = {}          # {angel_token: accumulated_ltq} — builds intraday cum vol
        self.tick_store     = {}          # {token: {"ltp", "volume", "timestamp"}}
        self.running        = False
        self._instruments   = []
        self.last_tick_time = time.time()
        self._app_ref       = None        # set by caller for UI log access

    # ----------------------------------------------------------
    def subscribe(self, dhan_tokens: list, exch_map: dict = None):
        self._instruments = [
            (int((exch_map or {}).get(tok, self.NSE_FNO)), str(tok))
            for tok in dhan_tokens
        ]
        if not self._instruments:
            print("DhanDataEngine: no instruments to subscribe")
            return
        self.running = True
        threading.Thread(target=self._run, daemon=True,
                         name="dhan-ws").start()
        print(f"DhanDataEngine: subscribed {len(self._instruments)} instruments")

    # ----------------------------------------------------------
    def _run(self):
        """Entry point for the daemon thread — synchronous reconnect loop."""
        _delay = 5
        _first = True
        while self.running:
            try:
                self._ws_connect_and_run()
                _delay = 5   # reset backoff on clean disconnect
                _first = True
            except Exception as e:
                if self.running:
                    msg = str(e)
                    print(f"DhanDataEngine: WebSocket error: {msg[:120]}")
                    if "429" in msg:
                        if _first:
                            _delay = 30
                            _first = False
                            print("DhanDataEngine: rate-limited on first connect — "
                                  "previous session may still be open; retry in 30s")
                        else:
                            _delay = min(_delay * 2, 120)
                            print(f"DhanDataEngine: rate-limited — retry in {_delay}s")
                    elif "auth_failure" in msg:
                        if _first:
                            _delay = 60
                            _first = False
                            print("DhanDataEngine: auth failure — check token/plan; retry in 60s")
                        else:
                            _delay = min(_delay * 2, 300)
                            print(f"DhanDataEngine: auth failure — retry in {_delay}s")
                    else:
                        _delay = 5
            if self.running:
                time.sleep(_delay)

    def _ws_connect_and_run(self):
        """Open SSL socket, perform WebSocket handshake, subscribe, receive."""
        path = (
            f"/?version=2&token={self.access_token}"
            f"&clientId={self.client_id}&authType=2"
        )
        ctx  = ssl.create_default_context()
        raw  = socket.create_connection((self._WS_HOST, self._WS_PORT), timeout=30)
        sock = ctx.wrap_socket(raw, server_hostname=self._WS_HOST)
        sock.settimeout(60)

        # Shared receive buffer — persists across the HTTP/WS boundary.
        buf = bytearray()

        try:
            # HTTP WebSocket upgrade handshake
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {self._WS_HOST}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"\r\n"
            ).encode())

            # Read HTTP headers — any WS bytes that trail the headers stay in buf.
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Connection closed during handshake")
                buf.extend(chunk)

            sep = bytes(buf).index(b"\r\n\r\n") + 4
            http_hdr = bytes(buf[:sep])
            del buf[:sep]

            if b"101" not in http_hdr:
                raise ConnectionError(f"WebSocket upgrade failed: {http_hdr[:200]}")

            print("DhanDataEngine: WebSocket connected ✅")

            # ── Drain any frame the server sends before subscription ────
            # Official dhanhq library does NOT wait here — just connects and
            # subscribes immediately.  We do a short drain in case there is a
            # stray frame, but we must NOT block on a missing ack.
            sock.settimeout(1)
            try:
                init_msg = self._ws_recv(sock, buf)
                if init_msg is None:
                    # Genuine close frame from server before subscription
                    # (often a stale frame from the previous session — continue anyway)
                    print("DhanDataEngine: pre-sub close frame (stale?) — continuing")
                elif isinstance(init_msg, bytes) and init_msg:
                    print(f"DhanDataEngine: server pre-sub frame {len(init_msg)}B "
                          f"first={init_msg[:4].hex()}")
                    self._handle_binary(init_msg)
                    if not self.running:
                        return   # disconnect packet received — stop
            except TimeoutError:
                pass   # no pre-sub frame within 1 s — normal
            except Exception:
                pass
            sock.settimeout(60)

            # ── Send subscription ────────────────────────────────────────
            sub_payload = {
                "RequestCode": self.Ticker,
                "InstrumentCount": len(self._instruments),
                "InstrumentList": [
                    {
                        "ExchangeSegment": self._EXCH_MAP.get(seg, str(seg)),
                        "SecurityId": sid,
                    }
                    for seg, sid in self._instruments
                ],
            }
            print(f"DhanDataEngine: subscription (first 3): {sub_payload['InstrumentList'][:3]}")
            self._ws_send_text(sock, json.dumps(sub_payload))

            # Receive loop — buf is shared so partial frames are preserved
            _frames = 0
            while self.running:
                msg = self._ws_recv(sock, buf)
                if msg is None:
                    print(f"DhanDataEngine: recv ended after {_frames} frames "
                          f"(last_err={getattr(self, '_last_recv_err', 'none')})")
                    break
                _frames += 1
                if _frames <= 5:
                    if isinstance(msg, bytes):
                        print(f"DhanDataEngine: frame#{_frames} binary {len(msg)}B first={msg[:8].hex() if msg else 'empty'}")
                    else:
                        print(f"DhanDataEngine: frame#{_frames} text: {str(msg)[:200]}")
                if isinstance(msg, bytes) and msg:
                    self._handle_binary(msg)
                    if not self.running:
                        print("DhanDataEngine: stopping after server disconnect packet")
                        break
        finally:
            try:
                sock.close()
            except Exception:
                pass

    # ----------------------------------------------------------
    def _ws_send_text(self, sock, text: str):
        """Send a client-masked WebSocket text frame."""
        payload = text.encode("utf-8")
        mask    = os.urandom(4)
        masked  = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        if n <= 125:
            header = bytes([0x81, 0x80 | n]) + mask
        elif n <= 65535:
            header = bytes([0x81, 0xFE]) + struct.pack(">H", n) + mask
        else:
            header = bytes([0x81, 0xFF]) + struct.pack(">Q", n) + mask
        sock.sendall(header + masked)

    def _ws_recv(self, sock, buf: bytearray):
        """
        Read one WebSocket frame from buf+socket.
        Returns bytes (binary), str (text), b'' (ping/pong), or None (close/error).
        """
        def _read_exact(n):
            while len(buf) < n:
                try:
                    chunk = sock.recv(max(n - len(buf), 4096))
                except Exception as _exc:
                    # Re-raise timeouts so callers can distinguish them from
                    # a genuine close/connection-drop (which returns None).
                    if isinstance(_exc, TimeoutError):
                        raise
                    self._last_recv_err = f"{type(_exc).__name__}: {_exc}"
                    return None
                if not chunk:
                    return None
                buf.extend(chunk)   # extend avoids rebind — no nonlocal needed
            data = bytes(buf[:n])
            del buf[:n]
            return data

        hdr = _read_exact(2)
        if not hdr:
            return None
        opcode    = hdr[0] & 0x0F
        is_masked = (hdr[1] & 0x80) != 0
        length    = hdr[1] & 0x7F
        if length == 126:
            ext = _read_exact(2)
            if ext is None:
                return None
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = _read_exact(8)
            if ext is None:
                return None
            length = struct.unpack(">Q", ext)[0]
        mask_key = _read_exact(4) if is_masked else None
        if is_masked and mask_key is None:
            return None
        payload  = _read_exact(length) if length else b""
        if payload is None:
            return None
        if is_masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:   # Close frame
            if len(payload) >= 2:
                code = struct.unpack(">H", payload[:2])[0]
                reason = payload[2:].decode("utf-8", errors="replace")
                print(f"DhanDataEngine: Close frame code={code} reason='{reason}'")
            else:
                print(f"DhanDataEngine: Close frame (no payload)")
            return None
        if opcode == 0x2:   # Binary frame
            return payload
        if opcode == 0x1:   # Text frame (e.g. server ack/error JSON)
            txt = payload.decode("utf-8", errors="replace")
            print(f"DhanDataEngine: server msg: {txt[:300]}")
            return b""      # log and continue — don't treat as close
        if opcode == 0x9:   # Ping — send Pong
            self._ws_send_pong(sock, payload)
            return b""
        return b""          # continuation — ignored

    def _ws_send_pong(self, sock, payload: bytes):
        """Respond to a server Ping with a masked Pong frame (RFC 6455 §5.3)."""
        mask   = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n      = len(payload)
        # 0x80 in the length byte sets the MASK bit — required for all client→server frames
        if n <= 125:
            header = bytes([0x8A, 0x80 | n]) + mask
        else:
            header = bytes([0x8A, 0xFE]) + struct.pack(">H", n) + mask
        try:
            sock.sendall(header + masked)
        except Exception:
            pass

    # ----------------------------------------------------------
    _DISCONNECT_CODES = {
        805: "Too many active WebSocket connections",
        806: "Not subscribed to Data APIs — enable Market Feed in Dhan account",
        807: "Access token expired — please re-login",
        808: "Invalid Client ID",
        809: "Authentication failed — check token",
    }

    def _handle_binary(self, data: bytes):
        """
        Parse Dhan binary frames.
        first_byte=2  → Ticker packet   (LTP)
        first_byte=50 → Server disconnection with error code
        All others are silently consumed (prev-close, OI, status, etc.)
        """
        if not data:
            return
        first = data[0]

        # Server-sent disconnection packet (first byte = 50 / 0x32)
        if first == 50:
            try:
                if len(data) >= 10:
                    _, _, _, _, code = struct.unpack('<BHBIH', data[:10])
                    msg = self._DISCONNECT_CODES.get(code, f"unknown code {code}")
                    print(f"DhanDataEngine: SERVER DISCONNECT code={code}: {msg}")
                    if getattr(self, "_app_ref", None):
                        self._app_ref.log(f"❌ Dhan WS disconnect code={code}: {msg}")
                    self.running = False   # stop reconnecting — user must fix account
                else:
                    print(f"DhanDataEngine: SERVER DISCONNECT (short packet {len(data)}B)")
            except struct.error:
                print(f"DhanDataEngine: SERVER DISCONNECT (parse error)")
            return

        # Ticker packet
        if first == 2 and len(data) >= 16:
            try:
                _, _, _, security_id, ltp_f, ltq = struct.unpack('<BHBIfI', data[:16])
            except struct.error:
                return
            angel_tok = self.dhan_to_angel.get(str(security_id))
            if not angel_tok or ltp_f <= 0:
                return
            now = dt.datetime.now()
            # Accumulate last-traded-quantity into a running intraday cumulative volume.
            # Dhan sends LTQ per tick (not the NSE cumulative), so we sum them ourselves.
            cum_vol = self._cum_vol_cache.get(angel_tok, 0) + max(int(ltq), 0)
            self._cum_vol_cache[angel_tok] = cum_vol
            self._ltp_cache[angel_tok] = ltp_f
            self.tick_store[angel_tok] = {"ltp": ltp_f, "volume": cum_vol, "timestamp": now}
            self.last_tick_time = time.time()
            if self.on_tick_cb:
                self.on_tick_cb(angel_tok, ltp_f, cum_vol, now)

    # ----------------------------------------------------------
    def get_ltp(self, angel_token) -> float | None:
        return self._ltp_cache.get(angel_token)

    # ----------------------------------------------------------
    def _connect(self):
        """Called by watchdog on tick freeze — force-restart the WebSocket thread."""
        print("DhanDataEngine: watchdog restart — forcing reconnect")
        if getattr(self, "_app_ref", None):
            self._app_ref.log("⚠️  Dhan WS: tick freeze — forcing reconnect")
        self.running = False          # break any stuck sock.recv() via timeout
        time.sleep(1)
        if self._instruments:
            self.running = True
            threading.Thread(target=self._run, daemon=True,
                             name="dhan-ws-restart").start()

    def stop(self):
        """Signal the receive loop to exit; socket timeout will unblock recv."""
        self.running = False
