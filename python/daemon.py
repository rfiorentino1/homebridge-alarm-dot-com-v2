#!/usr/bin/env python3
"""JSON-RPC daemon bridging pyalarmdotcomajax 0.6.x to the Homebridge plugin.

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

from pyalarmdotcomajax import (  # type: ignore[import-untyped]
    AlarmBridge,
    AuthenticationFailed,
    EventBrokerMessage,
    EventBrokerTopic,
    OtpRequired,
)
from pyalarmdotcomajax.models.base import BatteryLevel  # type: ignore[import-untyped]
from pyalarmdotcomajax.models.partition import (  # type: ignore[import-untyped]
    Partition,
    PartitionState,
)
from pyalarmdotcomajax.models.sensor import (  # type: ignore[import-untyped]
    Sensor,
    SensorState,
    SensorSubtype,
)


# ---------------------------------------------------------------------------
# stdio JSON-RPC plumbing
# ---------------------------------------------------------------------------


async def _stdin_lines() -> "asyncio.Queue[str]":
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
# pyalarmdotcomajax 0.6 → wire translation
# ---------------------------------------------------------------------------


CONTACT_SUBTYPES = {
    SensorSubtype.CONTACT_SENSOR,
    SensorSubtype.CONTACT_SHOCK_SENSOR,
}
MOTION_SUBTYPES = {
    SensorSubtype.MOTION_SENSOR,
    SensorSubtype.PANEL_MOTION_SENSOR,
}


def _partition_state_to_wire(state: PartitionState | None) -> str:
    if state == PartitionState.DISARMED:
        return "disarmed"
    if state == PartitionState.ARMED_STAY:
        return "armed_stay"
    if state == PartitionState.ARMED_AWAY:
        return "armed_away"
    if state == PartitionState.ARMED_NIGHT:
        return "armed_night"
    return "unknown"


def _battery_is_low(b: BatteryLevel | None) -> bool | None:
    if b is None or b == BatteryLevel.NONE:
        return None
    return b in (BatteryLevel.CRITICAL, BatteryLevel.LOW)


def _partition_to_wire(p: Partition) -> dict:
    attrs = p.attributes
    return {
        "kind": "panel",
        "id": str(p.id),
        "name": p.name or f"Panel {p.id}",
        "state": _partition_state_to_wire(attrs.state),
        "hasOpenZones": bool(getattr(attrs, "has_open_bypassable_sensors", False)),
    }


def _sensor_to_wire_contact(s: Sensor) -> dict:
    closed = s.attributes.state == SensorState.CLOSED
    out: dict[str, Any] = {
        "kind": "contact_sensor",
        "id": str(s.id),
        "name": s.name or f"Contact Sensor {s.id}",
        "closed": closed,
    }
    low = _battery_is_low(s.attributes.battery_level_classification)
    if low is not None:
        out["lowBattery"] = low
    return out


def _sensor_to_wire_motion(s: Sensor) -> dict:
    motion = s.attributes.state == SensorState.ACTIVE
    out: dict[str, Any] = {
        "kind": "motion_sensor",
        "id": str(s.id),
        "name": s.name or f"Motion Sensor {s.id}",
        "motion": motion,
    }
    low = _battery_is_low(s.attributes.battery_level_classification)
    if low is not None:
        out["lowBattery"] = low
    return out


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


MethodHandler = Callable[[dict], Awaitable[dict]]


class Daemon:
    def __init__(self) -> None:
        self._bridge: AlarmBridge | None = None
        self._expose_panel = True
        self._expose_contacts = True
        self._expose_motion = True
        self._subscribed = False
        self._unsubscribe: Callable[[], None] | None = None
        self._stop_ws: Callable[[], None] | None = None
        self._known_devices: dict[str, dict] = {}
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

        self._bridge = AlarmBridge(
            username=username, password=password, mfa_token=mfa_cookie
        )

        try:
            await self._bridge.login()
        except OtpRequired as e:
            await self._cleanup()
            raise RuntimeError(
                "2FA is required on this account. Run `python -m pyalarmdotcomajax "
                "--username ... --password ...` once in the plugin's venv, submit the "
                "OTP when prompted, and paste the returned cookie into the "
                "`mfaCookie` field of the plugin config."
            ) from e
        except AuthenticationFailed as e:
            await self._cleanup()
            raise RuntimeError(f"Alarm.com authentication failed: {e}") from e

        _emit_log("info", f"logged in as {username}")
        return {"ok": True}

    async def _cleanup(self) -> None:
        try:
            if self._unsubscribe is not None:
                self._unsubscribe()
                self._unsubscribe = None
            if self._stop_ws is not None:
                maybe = self._stop_ws()
                if asyncio.iscoroutine(maybe):
                    await maybe
                self._stop_ws = None
        finally:
            if self._bridge is not None:
                try:
                    maybe_close = self._bridge.close()
                    if asyncio.iscoroutine(maybe_close):
                        await maybe_close
                except Exception:
                    pass
            self._bridge = None

    # ----- enumerate -----

    async def _enumerate_devices(self, params: dict) -> dict:
        self._require_bridge()
        self._expose_panel = bool(params.get("include_security_panel", True))
        self._expose_contacts = bool(params.get("include_contact_sensors", True))
        self._expose_motion = bool(params.get("include_motion_sensors", True))

        # In 0.6, initialize() pulls the full device catalog.
        await self._bridge.initialize()  # type: ignore[union-attr]

        devices = self._snapshot_devices()
        self._known_devices = {d["id"]: d for d in devices}
        _emit_log(
            "info",
            f"discovered {len(devices)} device(s): "
            f"{sum(1 for d in devices if d['kind'] == 'panel')} panels, "
            f"{sum(1 for d in devices if d['kind'] == 'contact_sensor')} contacts, "
            f"{sum(1 for d in devices if d['kind'] == 'motion_sensor')} motions",
        )
        return {"devices": devices}

    def _snapshot_devices(self) -> list[dict]:
        assert self._bridge is not None
        out: list[dict] = []

        if self._expose_panel:
            for p in self._bridge.partitions:
                out.append(_partition_to_wire(p))

        if self._expose_contacts or self._expose_motion:
            for s in self._bridge.sensors:
                subtype = getattr(s.attributes, "device_type", None)
                if subtype in CONTACT_SUBTYPES and self._expose_contacts:
                    out.append(_sensor_to_wire_contact(s))
                elif subtype in MOTION_SUBTYPES and self._expose_motion:
                    out.append(_sensor_to_wire_motion(s))

        return out

    def _lookup_wire(self, resource_id: str) -> dict | None:
        """Find the current wire representation for a resource id, or None if we don't expose it."""
        assert self._bridge is not None
        partition = self._bridge.partitions.get(resource_id)
        if partition is not None and self._expose_panel:
            return _partition_to_wire(partition)

        sensor = self._bridge.sensors.get(resource_id)
        if sensor is not None:
            subtype = getattr(sensor.attributes, "device_type", None)
            if subtype in CONTACT_SUBTYPES and self._expose_contacts:
                return _sensor_to_wire_contact(sensor)
            if subtype in MOTION_SUBTYPES and self._expose_motion:
                return _sensor_to_wire_motion(sensor)
        return None

    # ----- panel action -----

    async def _panel_action(self, params: dict) -> dict:
        self._require_bridge()
        device_id = params.get("device_id")
        action = params.get("action")
        bypass = bool(params.get("bypass_zones", False))

        if action not in {"arm_stay", "arm_away", "arm_night", "disarm"}:
            raise ValueError(f"unknown action: {action}")
        if not device_id:
            raise ValueError("device_id is required")

        partitions = self._bridge.partitions  # type: ignore[union-attr]
        partition = partitions.get(str(device_id))
        if partition is None:
            raise RuntimeError(f"partition {device_id} not found")

        if action == "disarm":
            await partitions.disarm(str(device_id))
        elif action == "arm_stay":
            await partitions.arm_stay(str(device_id), force_bypass=bypass)
        elif action == "arm_away":
            await partitions.arm_away(str(device_id), force_bypass=bypass)
        elif action == "arm_night":
            await partitions.arm_night(str(device_id), force_bypass=bypass)

        return {"ok": True}

    # ----- subscribe (0.6 event-driven) -----

    async def _subscribe_updates(self, _params: dict) -> dict:
        self._require_bridge()
        if self._subscribed:
            return {"ok": True}
        self._subscribed = True

        bridge = self._bridge
        assert bridge is not None

        # start_event_monitoring returns an optional stop handle; also opens the WS.
        self._stop_ws = await bridge.start_event_monitoring()

        # EventBroker.subscribe fires our callback for every ResourceEventMessage.
        # We filter to resource-update events and re-emit as device_updated if known.
        def on_event(msg: EventBrokerMessage) -> None:
            topic = getattr(msg, "topic", None)
            if topic == EventBrokerTopic.RESOURCE_UPDATED:
                resource_id = getattr(msg, "id", None)
                if not resource_id:
                    return
                wire = self._lookup_wire(str(resource_id))
                if wire is not None and self._known_devices.get(wire["id"]) != wire:
                    self._known_devices[wire["id"]] = wire
                    _emit_notification("device_updated", {"device": wire})
            elif topic in (EventBrokerTopic.RESOURCE_ADDED, EventBrokerTopic.RESOURCE_DELETED):
                # Device set changed; re-enumerate and ship the new list.
                current = {d["id"]: d for d in self._snapshot_devices()}
                if set(current.keys()) != set(self._known_devices.keys()):
                    self._known_devices = current
                    _emit_notification(
                        "devices_enumerated", {"devices": list(current.values())}
                    )

        self._unsubscribe = bridge.subscribe(on_event)
        _emit_log("info", "event subscription live (push events via websocket)")
        return {"ok": True}

    def _require_bridge(self) -> None:
        if self._bridge is None:
            raise RuntimeError("not logged in — call login first")

    async def shutdown(self) -> None:
        self._subscribed = False
        await self._cleanup()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def main_async(log_level: str) -> None:
    logging.basicConfig(
        level=log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _emit_log("info", "daemon started (pyalarmdotcomajax 0.6.x)")

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
