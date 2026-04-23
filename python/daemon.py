#!/usr/bin/env python3
"""JSON-RPC daemon bridging pyalarmdotcomajax to the Homebridge plugin.

Wire protocol: newline-delimited JSON-RPC 2.0 over stdin/stdout.

Supported methods from Node → Python:
    login(username, password, mfaCookie?)        → {"ok": true}
    enumerate_devices(include_security_panel,
                      include_contact_sensors,
                      include_motion_sensors)    → {"devices": [...]}
    panel_action(device_id, action, bypass_zones?) → {"ok": true}
    subscribe_updates()                          → {"ok": true}

Notifications from Python → Node:
    device_updated(device)
    devices_enumerated(devices)
    log(level, message)

Device shapes match the TypeScript types in src/types.ts.

This file is intentionally self-contained so it can run under a minimal venv.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


# ---------------------------------------------------------------------------
# I/O primitives
# ---------------------------------------------------------------------------


async def _stdin_lines() -> "asyncio.Queue[str]":
    """Pump stdin lines through a queue, one line per item. EOF pushes None."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    async def pump() -> None:
        while True:
            line = await reader.readline()
            if not line:
                await queue.put("")  # sentinel for EOF
                return
            await queue.put(line.decode("utf-8", errors="replace").rstrip("\n"))

    asyncio.create_task(pump())
    return queue


def _write_message(msg: dict) -> None:
    """Write one JSON object to stdout, newline-delimited, flushed immediately."""
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _emit_notification(method: str, params: dict | None = None) -> None:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    _write_message(msg)


def _emit_log(level: str, message: str) -> None:
    _emit_notification("log", {"level": level, "message": message})


# ---------------------------------------------------------------------------
# Daemon state
# ---------------------------------------------------------------------------


@dataclass
class DaemonState:
    """Live state. In the MVP we hold pyalarmdotcomajax's client here."""

    alarm_client: Any | None = None  # pyalarmdotcomajax AlarmController
    subscribed: bool = False
    expose_panel: bool = True
    expose_contacts: bool = True
    expose_motion: bool = True


MethodHandler = Callable[[dict], Awaitable[dict]]


class Daemon:
    def __init__(self) -> None:
        self.state = DaemonState()
        self._handlers: dict[str, MethodHandler] = {
            "login": self._login,
            "enumerate_devices": self._enumerate_devices,
            "panel_action": self._panel_action,
            "subscribe_updates": self._subscribe_updates,
        }

    # ----- dispatch -----

    async def dispatch(self, line: str) -> None:
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _emit_log("error", f"malformed JSON from host: {e}")
            return

        rpc_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if method not in self._handlers:
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )
            return

        try:
            result = await self._handlers[method](params)
            _write_message({"jsonrpc": "2.0", "id": rpc_id, "result": result})
        except Exception as e:  # pragma: no cover
            logging.exception("handler %s raised", method)
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32000, "message": f"{type(e).__name__}: {e}"},
                }
            )

    # ----- method implementations (MVP stubs; real Alarm.com integration in v0.1.x) -----

    async def _login(self, params: dict) -> dict:
        """Log in to Alarm.com via pyalarmdotcomajax.

        NOTE: in the v0.1.0 scaffold this is a stub. Wiring to the real library
        comes in the next commit, after the plugin's end-to-end plumbing is verified.
        """
        username = params.get("username")
        password = params.get("password")
        if not username or not password:
            raise ValueError("username and password are required")
        _emit_log("info", f"(stub) would log in as {username}")
        return {"ok": True}

    async def _enumerate_devices(self, params: dict) -> dict:
        """Return the devices the plugin should expose to HomeKit.

        STUB: returns an empty list until the pyalarmdotcomajax integration lands.
        """
        self.state.expose_panel = bool(params.get("include_security_panel", True))
        self.state.expose_contacts = bool(params.get("include_contact_sensors", True))
        self.state.expose_motion = bool(params.get("include_motion_sensors", True))
        return {"devices": []}

    async def _panel_action(self, params: dict) -> dict:
        """Arm/disarm the panel. STUB until pyalarmdotcomajax is wired in."""
        action = params.get("action")
        device_id = params.get("device_id")
        if action not in {"arm_stay", "arm_away", "arm_night", "disarm"}:
            raise ValueError(f"unknown action: {action}")
        _emit_log("info", f"(stub) would perform {action} on {device_id}")
        return {"ok": True}

    async def _subscribe_updates(self, _params: dict) -> dict:
        """Start pushing `device_updated` notifications as events arrive.

        STUB: marks subscribed=True; real event pump lands with the
        pyalarmdotcomajax integration.
        """
        self.state.subscribed = True
        _emit_log("info", "(stub) event subscription registered (no events will fire yet)")
        return {"ok": True}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def main_async(log_level: str) -> None:
    logging.basicConfig(level=log_level.upper(), stream=sys.stderr, format="%(message)s")
    _emit_log("info", "daemon started (scaffold; pyalarmdotcomajax integration pending)")

    daemon = Daemon()
    queue = await _stdin_lines()

    # Clean shutdown on SIGTERM/SIGINT.
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    while not stop.is_set():
        try:
            line = await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        if line == "":
            break  # stdin closed; host is gone
        if not line.strip():
            continue
        asyncio.create_task(daemon.dispatch(line))

    _emit_log("info", "daemon shutting down")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args.log_level))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
