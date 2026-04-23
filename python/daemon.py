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
    device_updated({"device": {...}})
    devices_enumerated({"devices": [...]})
    log({"level": "...", "message": "..."})

Device shapes mirror the TypeScript types in src/types.ts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from contextlib import suppress
from typing import Any, Awaitable, Callable

import aiohttp  # type: ignore[import-untyped]

from pyalarmdotcomajax import (  # type: ignore[import-untyped]
    AlarmController,
    AuthenticationFailed,
    NotAuthorized,
    OtpRequired,
    TryAgain,
)
from pyalarmdotcomajax.devices.partition import Partition  # type: ignore[import-untyped]
from pyalarmdotcomajax.devices.sensor import Sensor  # type: ignore[import-untyped]


# ---------------------------------------------------------------------------
# I/O primitives (stdin lines in, JSON-RPC messages out)
# ---------------------------------------------------------------------------


async def _stdin_lines() -> "asyncio.Queue[str]":
    """Pump stdin lines through a queue, one line per item. EOF pushes ''."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    async def pump() -> None:
        while True:
            line = await reader.readline()
            if not line:
                await queue.put("")
                return
            await queue.put(line.decode("utf-8", errors="replace").rstrip("\n"))

    asyncio.create_task(pump())
    return queue


def _write_message(msg: dict) -> None:
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
# Mapping pyalarmdotcomajax device state ↔ our wire format
# ---------------------------------------------------------------------------


# Subtypes we expose (ContactSensor / MotionSensor). Other subtypes (smoke, glass
# break, freeze, etc.) are left for a future release.
CONTACT_SUBTYPES = {
    Sensor.Subtype.CONTACT_SENSOR,
    Sensor.Subtype.CONTACT_SHOCK_SENSOR,
}
MOTION_SUBTYPES = {
    Sensor.Subtype.MOTION_SENSOR,
    Sensor.Subtype.PANEL_MOTION_SENSOR,
}


def _partition_state_to_wire(state: Partition.DeviceState | None) -> str:
    if state == Partition.DeviceState.DISARMED:
        return "disarmed"
    if state == Partition.DeviceState.ARMED_STAY:
        return "armed_stay"
    if state == Partition.DeviceState.ARMED_AWAY:
        return "armed_away"
    if state == Partition.DeviceState.ARMED_NIGHT:
        return "armed_night"
    return "unknown"


def _partition_to_wire(p: Partition) -> dict:
    has_open_zones = bool(getattr(p, "uncleared_issues", None))
    return {
        "kind": "panel",
        "id": str(p.id),  # type: ignore[attr-defined]
        "name": p.name or f"Panel {p.id}",  # type: ignore[attr-defined]
        "state": _partition_state_to_wire(p.state),
        "hasOpenZones": has_open_zones,
    }


def _contact_sensor_to_wire(s: Sensor) -> dict:
    closed = s.state == Sensor.DeviceState.CLOSED
    out = {
        "kind": "contact_sensor",
        "id": str(s.id),  # type: ignore[attr-defined]
        "name": s.name or f"Contact Sensor {s.id}",  # type: ignore[attr-defined]
        "closed": closed,
    }
    if s.battery_low is not None:
        out["lowBattery"] = bool(s.battery_low)
    return out


def _motion_sensor_to_wire(s: Sensor) -> dict:
    motion = s.state == Sensor.DeviceState.ACTIVE
    out = {
        "kind": "motion_sensor",
        "id": str(s.id),  # type: ignore[attr-defined]
        "name": s.name or f"Motion Sensor {s.id}",  # type: ignore[attr-defined]
        "motion": motion,
    }
    if s.battery_low is not None:
        out["lowBattery"] = bool(s.battery_low)
    return out


# ---------------------------------------------------------------------------
# Daemon state + handlers
# ---------------------------------------------------------------------------


MethodHandler = Callable[[dict], Awaitable[dict]]


class Daemon:
    """Holds the AlarmController + aiohttp session for the daemon's lifetime."""

    POLL_INTERVAL_S = 30.0  # async_update() cadence while subscribed (0.5.x has no push)

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._controller: AlarmController | None = None
        self._expose_panel = True
        self._expose_contacts = True
        self._expose_motion = True
        self._subscribed = False
        self._poll_task: asyncio.Task | None = None
        self._known_devices: dict[str, dict] = {}  # device_id → last wire representation
        self._handlers: dict[str, MethodHandler] = {
            "login": self._login,
            "enumerate_devices": self._enumerate_devices,
            "panel_action": self._panel_action,
            "subscribe_updates": self._subscribe_updates,
        }

    # ----- JSON-RPC dispatch -----

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
        except Exception as e:
            logging.exception("handler %s raised", method)
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32000, "message": f"{type(e).__name__}: {e}"},
                }
            )

    # ----- login -----

    async def _login(self, params: dict) -> dict:
        username = params.get("username")
        password = params.get("password")
        mfa_cookie = params.get("mfaCookie") or None
        if not username or not password:
            raise ValueError("username and password are required")

        # Fresh aiohttp session; controller holds a reference.
        self._session = aiohttp.ClientSession()
        self._controller = AlarmController(
            username=username,
            password=password,
            websession=self._session,
            twofactorcookie=mfa_cookie,
        )

        try:
            await self._controller.async_login()
        except OtpRequired as e:
            await self._cleanup_session()
            raise RuntimeError(
                "2FA is required on this account. Run `python -m pyalarmdotcomajax "
                "--username ... --password ...` once in the plugin's venv, submit the "
                "OTP when prompted, and paste the returned cookie into the "
                "`mfaCookie` field of the plugin config."
            ) from e
        except AuthenticationFailed as e:
            await self._cleanup_session()
            raise RuntimeError(f"Alarm.com authentication failed: {e}") from e
        except (TryAgain, NotAuthorized) as e:
            await self._cleanup_session()
            raise RuntimeError(f"Alarm.com login error (retryable): {e}") from e

        _emit_log("info", f"logged in as {username}")
        return {"ok": True}

    async def _cleanup_session(self) -> None:
        if self._controller is not None:
            # close_websession may or may not be a coroutine depending on upstream version.
            try:
                maybe_coro = self._controller.close_websession()
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            except Exception:
                pass
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._controller = None
        self._session = None

    # ----- enumerate -----

    async def _enumerate_devices(self, params: dict) -> dict:
        self._require_controller()
        self._expose_panel = bool(params.get("include_security_panel", True))
        self._expose_contacts = bool(params.get("include_contact_sensors", True))
        self._expose_motion = bool(params.get("include_motion_sensors", True))

        await self._controller.async_update()  # type: ignore[union-attr]

        devices = self._snapshot_devices()
        # Cache initial snapshot so we can detect changes during polling.
        self._known_devices = {d["id"]: d for d in devices}
        return {"devices": devices}

    def _snapshot_devices(self) -> list[dict]:
        assert self._controller is not None
        out: list[dict] = []

        if self._expose_panel:
            for p in self._controller.devices.partitions:
                out.append(_partition_to_wire(p))

        if self._expose_contacts or self._expose_motion:
            for s in self._controller.devices.sensors:
                subtype = getattr(s, "device_subtype", None)
                if subtype in CONTACT_SUBTYPES and self._expose_contacts:
                    out.append(_contact_sensor_to_wire(s))
                elif subtype in MOTION_SUBTYPES and self._expose_motion:
                    out.append(_motion_sensor_to_wire(s))

        return out

    # ----- panel action -----

    async def _panel_action(self, params: dict) -> dict:
        self._require_controller()
        device_id = params.get("device_id")
        action = params.get("action")
        bypass = bool(params.get("bypass_zones", False))

        if action not in {"arm_stay", "arm_away", "arm_night", "disarm"}:
            raise ValueError(f"unknown action: {action}")
        if not device_id:
            raise ValueError("device_id is required")

        partition = next(
            (
                p
                for p in self._controller.devices.partitions  # type: ignore[union-attr]
                if str(p.id) == str(device_id)  # type: ignore[attr-defined]
            ),
            None,
        )
        if partition is None:
            raise RuntimeError(f"partition {device_id} not found")

        if action == "disarm":
            await partition.async_disarm()
        elif action == "arm_stay":
            await partition.async_arm_stay(force_bypass=bypass or None)
        elif action == "arm_away":
            await partition.async_arm_away(force_bypass=bypass or None)
        elif action == "arm_night":
            if not getattr(partition, "supports_night_arming", False):
                raise RuntimeError("this partition does not support Night arming")
            await partition.async_arm_night(force_bypass=bypass or None)

        # Push an immediate state refresh so Home.app reflects the change quickly.
        asyncio.create_task(self._refresh_and_notify())
        return {"ok": True}

    # ----- subscribe -----

    async def _subscribe_updates(self, _params: dict) -> dict:
        self._require_controller()
        if self._subscribed:
            return {"ok": True}
        self._subscribed = True

        # Register per-device callbacks: every external state change emits a notification.
        assert self._controller is not None
        for p in self._controller.devices.partitions:
            p.register_external_update_callback(
                lambda *_, _p=p: self._notify_device_change(_partition_to_wire(_p))
            )
        for s in self._controller.devices.sensors:
            subtype = getattr(s, "device_subtype", None)
            if subtype in CONTACT_SUBTYPES and self._expose_contacts:
                s.register_external_update_callback(
                    lambda *_, _s=s: self._notify_device_change(_contact_sensor_to_wire(_s))
                )
            elif subtype in MOTION_SUBTYPES and self._expose_motion:
                s.register_external_update_callback(
                    lambda *_, _s=s: self._notify_device_change(_motion_sensor_to_wire(_s))
                )

        # Also start a polling loop as a safety net — pyalarmdotcomajax 0.5.x doesn't
        # stream push events out of the box, so we drive async_update() periodically.
        # When 0.6 ships stable we'll swap this for start_websocket().
        self._poll_task = asyncio.create_task(self._poll_loop())

        _emit_log("info", "subscribed to events (polling every 30s)")
        return {"ok": True}

    async def _poll_loop(self) -> None:
        assert self._controller is not None
        while self._subscribed:
            await asyncio.sleep(self.POLL_INTERVAL_S)
            try:
                await self._refresh_and_notify()
            except Exception as e:
                _emit_log("warn", f"poll error: {type(e).__name__}: {e}")

    async def _refresh_and_notify(self) -> None:
        """Call async_update() and diff against known state; notify on changes."""
        assert self._controller is not None
        await self._controller.async_update()
        current = {d["id"]: d for d in self._snapshot_devices()}

        # Emit notifications only for devices whose wire state actually changed.
        for device_id, wire in current.items():
            prev = self._known_devices.get(device_id)
            if prev != wire:
                self._notify_device_change(wire)

        # If the device set changed (added/removed), re-send the full enumeration.
        if set(current.keys()) != set(self._known_devices.keys()):
            _emit_notification("devices_enumerated", {"devices": list(current.values())})

        self._known_devices = current

    def _notify_device_change(self, wire: dict) -> None:
        self._known_devices[wire["id"]] = wire
        _emit_notification("device_updated", {"device": wire})

    def _require_controller(self) -> None:
        if self._controller is None:
            raise RuntimeError("not logged in — call login first")

    async def shutdown(self) -> None:
        self._subscribed = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
        await self._cleanup_session()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def main_async(log_level: str) -> None:
    logging.basicConfig(
        level=log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _emit_log("info", "daemon started")

    daemon = Daemon()
    queue = await _stdin_lines()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        while not stop.is_set():
            try:
                line = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if line == "":
                break
            if not line.strip():
                continue
            asyncio.create_task(daemon.dispatch(line))
    finally:
        await daemon.shutdown()
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
