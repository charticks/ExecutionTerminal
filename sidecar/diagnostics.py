"""Structured, persistent diagnostics for the sidecar.

Every log line the sidecar produces goes to TWO places:

  * a rotating file on disk, split by category — the record that survives a
    crash and can be sent in with a bug report;
  * the existing ``log_line`` WebSocket event — the live panel in the UI.

Before this module, only the second existed: every ``_log()`` call published to
the hub and was gone the moment the app closed. The ``logs/`` folder that did
exist was written by the Angel SDK's bundled logzero, not by Charticks, which is
why it appeared not to update when Charticks itself failed.

Format
------
One line per event, ``key=value`` after a fixed prefix::

    2026-08-11 14:25:12.183 | ERROR | orders | action="Place Order" status=failed
        broker="Angel One" account=a1b2c3d4… order="BUY NIFTY 25000 CE"
        reason="Insufficient Margin" brokerCode=AB105

Single-line (rather than the multi-line block form) on purpose: a failure is
usually found by grepping for an order id or an account across several files at
once, and multi-line records break that as well as tail/rotation.

Categories map to files. A line always lands in its category file AND in
application.log, so application.log is the single chronological narrative while
the per-category files stay readable.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import traceback
from typing import Any

from bridge import events
from bridge.hub import hub
from services.paths import log_dir

# Category -> file. "app" has no file of its own; it lands in application.log
# only, which every category also feeds.
CATEGORIES = {
    "broker": "broker.log",
    "orders": "orders.log",
    "websocket": "websocket.log",
    "risk": "risk.log",
    "exceptions": "exceptions.log",
}

_ROOT = "charticks"
# 5 MB x 5 backups per file: bounded at ~150 MB worst case across every category,
# while still holding several full trading days of normal activity.
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 5

# Existing call sites use these words; logging wants its own constants.
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

_installed = False
_install_lock = threading.Lock()


class _Formatter(logging.Formatter):
    """Fixed prefix + the structured fields the record carries."""

    default_time_format = "%Y-%m-%d %H:%M:%S"
    default_msec_format = "%s.%03d"

    def format(self, record: logging.LogRecord) -> str:
        category = getattr(record, "category", "app")
        line = (f"{self.formatTime(record)} | {record.levelname:<8} | "
                f"{category:<10} | {record.getMessage()}")
        fields = getattr(record, "fields", None)
        if fields:
            line += " " + _render_fields(fields)
        if record.exc_info:
            # Full stack trace, indented so it stays visually attached to its
            # event when scanning a file.
            trace = "".join(traceback.format_exception(*record.exc_info)).rstrip()
            line += "\n" + "\n".join(f"    {ln}" for ln in trace.splitlines())
        return line


def _render_fields(fields: dict[str, Any]) -> str:
    parts = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        text = str(value).replace("\n", " ").replace('"', "'")
        parts.append(f'{key}="{text}"' if " " in text or "=" in text else f"{key}={text}")
    return " ".join(parts)


def mask_account(account_id: str | None) -> str:
    """Account ids identify a real brokerage account, so logs carry a stable
    prefix rather than the whole value — enough to correlate lines, not enough
    to be worth redacting before sending a log file in."""
    if not account_id:
        return ""
    text = str(account_id)
    return text if len(text) <= 8 else f"{text[:8]}…"


def install() -> str:
    """Create the handlers and take over unhandled-exception reporting.

    Idempotent, and never raises: if the log directory cannot be written the
    sidecar must still trade, so it degrades to console output.
    """
    global _installed
    with _install_lock:
        if _installed:
            return log_dir()
        _installed = True

        root = logging.getLogger(_ROOT)
        root.setLevel(logging.DEBUG)
        root.propagate = False
        formatter = _Formatter()

        try:
            directory = log_dir()
        except OSError as exc:  # read-only install dir, full disk, …
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(formatter)
            root.addHandler(handler)
            root.error("log directory unavailable — logging to stderr only",
                       extra={"category": "app", "fields": {"error": exc}})
            return ""

        def add_file(logger: logging.Logger, filename: str) -> None:
            path = os.path.join(directory, filename)
            handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        # application.log sits on the root, so every category feeds it too.
        add_file(root, "application.log")
        for category, filename in CATEGORIES.items():
            child = logging.getLogger(f"{_ROOT}.{category}")
            child.setLevel(logging.DEBUG)
            add_file(child, filename)

        _install_global_handlers()
        emit("app", "info", "Diagnostics started", logDir=directory, pid=os.getpid())
        return directory


def _install_global_handlers() -> None:
    """Nothing may die quietly. Uncaught exceptions on the main thread, on any
    worker thread, and in asyncio all land in exceptions.log with a trace."""
    previous = sys.excepthook

    def on_uncaught(exc_type, exc, tb):
        exception("app", "Unhandled exception", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    sys.excepthook = on_uncaught

    def on_thread_exception(args: threading.ExceptHookArgs) -> None:
        exception("app", "Unhandled exception in thread",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                  thread=args.thread.name if args.thread else "?")

    threading.excepthook = on_thread_exception


def _logger(category: str) -> logging.Logger:
    name = f"{_ROOT}.{category}" if category in CATEGORIES else _ROOT
    return logging.getLogger(name)


def emit(category: str, level: str, message: str, *, publish: bool = False,
         exc_info: Any = None, **fields: Any) -> None:
    """Record one event.

    `category` picks the file; `fields` become the structured key=value tail.
    `publish=True` also pushes the message to the UI log panel — used by the
    existing ``_log()`` sinks so the panel keeps behaving exactly as before
    while the same line is now also persisted.
    """
    log = _logger(category)
    log.log(_LEVELS.get(str(level).lower(), logging.INFO), message,
            exc_info=exc_info, extra={"category": category, "fields": fields})
    if publish:
        hub.publish(events.log_line(level, message))


def exception(category: str, message: str, *, exc_info: Any = True, **fields: Any) -> None:
    """An unexpected failure: logged with a full stack trace, and mirrored into
    exceptions.log so every crash in the process is in one place."""
    emit(category, "error", message, exc_info=exc_info, **fields)
    if category != "exceptions":
        emit("exceptions", "error", message, exc_info=exc_info,
             origin=category, **fields)


def event(category: str, action: str, status: str, *, level: str | None = None,
          account: str | None = None, publish: bool = False,
          exc_info: Any = None, **fields: Any) -> None:
    """The structured form used for lifecycle events (logins, orders, feeds).

    `action` is what was attempted, `status` how it ended ("started",
    "success", "failed", "rejected"). Level defaults from the status so a
    failure is never accidentally filed as INFO.
    """
    if level is None:
        level = "error" if status in ("failed", "error") else (
            "warn" if status in ("rejected", "expired", "interrupted") else "info")
    emit(category, level, action, publish=publish, exc_info=exc_info,
         status=status, account=mask_account(account), **fields)
