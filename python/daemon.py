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
import os
import signal
import sys
import threading
import time
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


async def _stdin_lines() -> tuple["asyncio.Queue[str]", asyncio.Task]:
    """Returns (queue, pump_task). Caller must keep a reference to pump_task — if
    it's garbage-collected, stdin reads stop and the daemon silently hangs."""
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

    pump_task = asyncio.create_task(pump(), name="stdin-pump")
    return queue, pump_task


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


def _sensor_to_wire_contact(s: Sensor, *, pending_close: bool = False) -> dict:
    """Map a Sensor to our HomeKit wire representation.

    `pending_close` forces `closed: False` (open) regardless of reported
    state. Used by the OPENED_CLOSED "stay open until reconcile confirms
    closed" handler so automations watching for an open transition fire
    reliably, even for brief cycles that Alarm.com collapses into a single
    OPENED_CLOSED event.
    """
    if pending_close:
        closed = False
    else:
        # CLOSED (1) = stable-closed; OPEN (2) = stable-open.
        # OPENED_CLOSED (9) normally means 'settled closed after brief cycle'
        # — but when we receive the event we force-open (via pending_close
        # flag, handled upstream); after the next reconcile, we fall through
        # here and it gets treated as closed, confirming the cycle is done.
        state = s.attributes.state
        closed = state == SensorState.CLOSED or state == SensorState.OPENED_CLOSED
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

# Background reconciliation interval. Websocket events are primary, but
# pyalarmdotcomajax 0.6.0b9 has a reliability defect where some transitions
# are silently dropped — this poll catches any drift as a safety net.
RECONCILE_INTERVAL_S = 10.0

# When a websocket event arrives, pyalarmdotcomajax tends to deliver the first
# transition in a burst but drops follow-ups (e.g. "door opened" shows up but
# the matching "door closed" 2 sec later doesn't). Schedule a quick reconcile
# shortly after every event to catch the likely-dropped follow-up.
POST_EVENT_RECONCILE_DELAY_S = 3.0

# When we receive an OPENED_CLOSED merged event (ADC batches rapid open/close
# into a single event), we force-emit OPEN immediately then schedule a
# synthetic CLOSE. The door may actually stay open longer than the typical
# rapid-cycle case, so we wait this many seconds before forcing the close
# in HomeKit. If a real Closed/device_updated event arrives for the same
# sensor during this window, we cancel the pending synthetic close so the
# real state wins.
SYNTHETIC_CLOSE_DELAY_S = 30.0

# Hard cap on any single bridge HTTP/WS-setup call. Without this, a half-open
# TCP connection (network path died with no FIN/RST) makes the awaited call
# block forever — which is what happened on 2026-04-25 when the parents'
# internet glitched: both the WS read and the periodic reconcile blocked for
# ~2 hours until manual restart. 25s is generous for healthy calls (typically
# <2s) but ensures hangs surface as TimeoutError, which the existing exception
# handlers already catch + retry.
BRIDGE_CALL_TIMEOUT_S = 25.0

# Liveness watchdog. If no successful reconcile has happened in this many
# seconds, the daemon assumes the websocket / HTTP path is wedged and force-
# resubscribes (tear down WS + reopen). Reconcile runs every
# RECONCILE_INTERVAL_S (10s) when healthy, so 60s of silence = ~6 missed
# cycles, well past natural jitter. CONNECTION_EVENT heartbeats arrive in
# bursts every ~5min so are not a tight enough liveness signal — a successful
# reconcile is.
LIVENESS_TIMEOUT_S = 60.0
WATCHDOG_INTERVAL_S = 15.0

# OS-level stall watchdog. Runs in a real OS thread so it stays alive even
# when the asyncio event loop is wedged (e.g. by a synchronous-blocking call
# deep in pyalarmdotcomajax during a half-open WS scenario). The async
# watchdog above only catches reconcile drift; this catches the case where
# asyncio itself stops ticking. If the heartbeat (updated every 5s by an
# async task) is stale by more than STALL_THRESHOLD_S, the thread calls
# os._exit(1) and the Node-side python-bridge re-spawns us fresh.
HEARTBEAT_INTERVAL_S = 5.0
STALL_THRESHOLD_S = 60.0
STALL_CHECK_INTERVAL_S = 5.0


class Daemon:
    def __init__(self) -> None:
        self._bridge: AlarmBridge | None = None
        # Per-sensor pending synthetic close tasks (scheduled after OPENED_CLOSED).
        # Keyed by sensor wire id; cancelled if a real Closed event arrives first.
        self._pending_synthetic_close: dict[str, asyncio.Task] = {}
        self._expose_panel = True
        self._expose_contacts = True
        self._expose_motion = True
        self._subscribed = False
        self._unsubscribe: Callable[[], None] | None = None
        self._stop_ws: Callable[[], None] | None = None
        self._reconcile_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._reconnect_lock = asyncio.Lock()
        # Liveness markers (monotonic seconds). _last_successful_reconcile is
        # the primary signal the asyncio watchdog watches. _last_event is
        # informational / future-paranoia. _heartbeat_at is updated every
        # HEARTBEAT_INTERVAL_S by an async task and watched by the OS-thread
        # stall watchdog as proof that the asyncio loop itself is alive.
        self._last_successful_reconcile_at: float = 0.0
        self._last_event_at: float = 0.0
        self._heartbeat_at: float = time.monotonic()
        self._heartbeat_task: asyncio.Task | None = None
        self._stall_thread: threading.Thread | None = None
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
            if self._watchdog_task is not None:
                self._watchdog_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._watchdog_task
                self._watchdog_task = None
            if self._reconcile_task is not None:
                self._reconcile_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._reconcile_task
                self._reconcile_task = None
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

        # In 0.6, initialize() pulls the full device catalog. Timeout-wrapped
        # so an unstable network at startup doesn't block enumerate forever.
        await asyncio.wait_for(
            self._bridge.initialize(),  # type: ignore[union-attr]
            timeout=BRIDGE_CALL_TIMEOUT_S,
        )

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
        await self._start_subscription_inner()
        # Watchdog runs for the lifetime of the daemon; only created once.
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        return {"ok": True}

    async def _start_subscription_inner(self) -> None:
        """Open the WS subscription and start the reconcile loop. Caller should
        ensure prior subscription is torn down via _stop_subscription_inner first.

        Wraps bridge.start_event_monitoring() in a timeout so a wedged TCP
        connect during reconnect can't itself hang the watchdog.
        """
        bridge = self._bridge
        assert bridge is not None

        self._subscribed = True
        # Seed liveness so the watchdog gives the WS a fair chance to deliver
        # its first reconcile before considering the connection stale.
        self._last_successful_reconcile_at = time.monotonic()
        self._last_event_at = time.monotonic()

        # start_event_monitoring returns an optional stop handle; also opens the WS.
        self._stop_ws = await asyncio.wait_for(
            bridge.start_event_monitoring(),
            timeout=BRIDGE_CALL_TIMEOUT_S,
        )

        # EventBroker.subscribe fires our callback for every EventBrokerMessage.
        # pyalarmdotcomajax exposes 5 topic types; we handle all of them:
        #   RESOURCE_UPDATED   — resource state changed; emit device_updated if known
        #   RAW_RESOURCE_EVENT — server pushed a raw event; refresh known resource
        #   RESOURCE_ADDED/DELETED — device catalog changed; re-enumerate
        #   CONNECTION_EVENT   — websocket lifecycle; log for diagnostics
        def on_event(msg: EventBrokerMessage) -> None:
            self._last_event_at = time.monotonic()
            topic = getattr(msg, "topic", None)
            resource_id = getattr(msg, "id", None)
            resource = getattr(msg, "resource", None)

            # Trace-log everything so we can diagnose missed events in the field.
            # NOTE: Don't call repr() on the resource — some pyalarmdotcomajax
            # models (e.g. Sensor) have a buggy __repr__ that raises AttributeError
            # on missing attributes like 'model'. Use type name only.
            topic_name = topic.name if topic is not None else "?"
            resource_type = type(resource).__name__ if resource is not None else "None"
            # Temporarily emit at INFO so we can see them in the live Homebridge
            # log without needing HB-side debug mode. Will dial back to debug
            # once websocket-path is verified working in the field.
            _emit_log(
                "info",
                f"event: topic={topic_name} id={resource_id} resource_type={resource_type}",
            )
            # --- TIMING DIAGNOSTIC (temporary) ---
            # Log ADC-server-reported event time vs daemon receipt time so we
            # can measure cloud→daemon latency per event.
            try:
                _emit_log("info", f"timing-diag-fired: topic={topic_name} id={resource_id}")
                from datetime import datetime, timezone as _tz
                ws = getattr(self._bridge, "ws_controller", None) if self._bridge is not None else None
                events = list(ws.last_events) if ws is not None else []
                # Find the most recent ws event matching this resource id, if any.
                matched = None
                for m in reversed(events):
                    mid = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
                    if mid and str(mid) == str(resource_id):
                        matched = m
                        break
                if matched is None and events:
                    matched = events[-1]
                edt = getattr(matched, "event_date_utc", None)
                if edt is None and isinstance(matched, dict):
                    edt = matched.get("event_date_utc")
                now = datetime.now(_tz.utc)
                if edt is not None:
                    try:
                        delta = (now - edt).total_seconds()
                    except Exception:
                        delta = None
                    _emit_log(
                        "info",
                        f"timing: adc_event_utc={edt} daemon_recv_utc={now.isoformat()} delta_s={delta}",
                    )
                else:
                    _emit_log("info", f"timing: NO event_date_utc, events={len(events)}, last_type={type(events[-1]).__name__ if events else None}, last_raw={str(events[-1])[:500] if events else None}")
            except Exception as _te:
                _emit_log("warn", f"timing diag error: {type(_te).__name__}: {_te}")
            # --- END DIAGNOSTIC ---

            if topic in (
                EventBrokerTopic.RESOURCE_UPDATED,
                EventBrokerTopic.RAW_RESOURCE_EVENT,
            ):
                # Both trigger the same "re-read and diff" flow. RAW_RESOURCE_EVENT is
                # what comes through for most ADT-branded sensors in practice.
                if not resource_id:
                    return

                # Special handling for OPENED_CLOSED: Alarm.com emits this as a
                # single event when a sensor cycled too fast for separate
                # OPEN/CLOSED events. We force-emit OPEN immediately (so
                # automations watching for 'door opened' fire). The next
                # reconcile (<=10s) will see state still at OPENED_CLOSED and
                # treat it as closed, settling HomeKit to closed.
                if self._bridge is not None:
                    sensor = self._bridge.sensors.get(str(resource_id))
                    if (
                        sensor is not None
                        and sensor.attributes.state == SensorState.OPENED_CLOSED
                        and sensor.attributes.device_type in CONTACT_SUBTYPES
                        and self._expose_contacts
                    ):
                        open_wire = _sensor_to_wire_contact(sensor, pending_close=True)
                        closed_wire = _sensor_to_wire_contact(sensor)
                        if self._known_devices.get(open_wire["id"]) != open_wire:
                            self._known_devices[open_wire["id"]] = open_wire
                            _emit_notification("device_updated", {"device": open_wire})
                            _emit_log(
                                "info",
                                f"force-open on OPENED_CLOSED: {open_wire['name']} "
                                f"(close will follow in ~3s)",
                            )
                        # Schedule a synthetic CLOSE event SYNTHETIC_CLOSE_DELAY_S
                        # seconds after the force-open. If a real Closed state
                        # transition arrives before then (via RESOURCE_UPDATED
                        # -> device_updated path), we cancel this synthetic one.
                        # If no real close arrives, we fall through to the
                        # synthetic close so the door doesnt appear stuck-open
                        # in HomeKit forever for merged cycles.
                        wire_id = closed_wire["id"]
                        # Cancel any existing pending synthetic close for this
                        # sensor (rapid re-triggers).
                        prev = self._pending_synthetic_close.pop(wire_id, None)
                        if prev is not None and not prev.done():
                            prev.cancel()
                        task = asyncio.create_task(
                            self._emit_delayed(
                                closed_wire,
                                SYNTHETIC_CLOSE_DELAY_S,
                                label=f"post-cycle-close (synthetic, {int(SYNTHETIC_CLOSE_DELAY_S)}s)",
                            )
                        )
                        self._pending_synthetic_close[wire_id] = task
                        self._post_event_tasks.add(task)
                        def _clear(t: asyncio.Task, wid: str = wire_id) -> None:
                            self._post_event_tasks.discard(t)
                            if self._pending_synthetic_close.get(wid) is t:
                                self._pending_synthetic_close.pop(wid, None)
                        task.add_done_callback(_clear)
                        return

                wire = self._lookup_wire(str(resource_id))
                if wire is None:
                    _emit_log("info", f"event: no wire for id={resource_id} (not exposed)")
                    return
                # If a real state-transition event arrives for a sensor that has
                # a pending synthetic close, cancel the synthetic - reality wins.
                wid = wire["id"]
                pending = self._pending_synthetic_close.pop(wid, None)
                if pending is not None and not pending.done():
                    pending.cancel()
                    _emit_log(
                        "info",
                        f"synthetic-close cancelled by real event: {wire.get('name')}",
                    )
                prev = self._known_devices.get(wid)
                if prev != wire:
                    self._known_devices[wid] = wire
                    _emit_notification("device_updated", {"device": wire})
                    _emit_log(
                        "info",
                        f"device_updated: {wire.get('name')} {wire}  (was {prev})",
                    )
                else:
                    _emit_log(
                        "info",
                        f"event but no change: {wire.get('name')} remains {wire}",
                    )
                # pyalarmdotcomajax occasionally drops follow-up transitions.
                # Schedule a quick reconcile to catch anything missed.
                task = asyncio.create_task(self._post_event_reconcile())
                self._post_event_tasks.add(task)
                task.add_done_callback(self._post_event_tasks.discard)
            elif topic in (EventBrokerTopic.RESOURCE_ADDED, EventBrokerTopic.RESOURCE_DELETED):
                # Device set changed; re-enumerate and ship the new list.
                current = {d["id"]: d for d in self._snapshot_devices()}
                if set(current.keys()) != set(self._known_devices.keys()):
                    self._known_devices = current
                    _emit_notification(
                        "devices_enumerated", {"devices": list(current.values())}
                    )
            elif topic == EventBrokerTopic.CONNECTION_EVENT:
                _emit_log("debug", f"websocket state: {getattr(msg, 'resource', None)}")

        self._unsubscribe = bridge.subscribe(on_event)

        # Start a background reconciliation loop as a safety net for missed websocket
        # events. Runs forever while subscribed; on cancel it exits cleanly.
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())
        self._post_event_tasks: set[asyncio.Task] = set()

        _emit_log(
            "info",
            f"event subscription live (push via websocket; full reconcile every {int(RECONCILE_INTERVAL_S)}s)",
        )

    async def _stop_subscription_inner(self) -> None:
        """Tear down the WS subscription + reconcile task without touching the
        bridge auth state or watchdog. Counterpart to _start_subscription_inner."""
        self._subscribed = False
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._reconcile_task
            self._reconcile_task = None
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None
        if self._stop_ws is not None:
            try:
                maybe = self._stop_ws()
                if asyncio.iscoroutine(maybe):
                    # Bound the stop call too — it can hang on a wedged socket.
                    with suppress(asyncio.TimeoutError, Exception):
                        await asyncio.wait_for(maybe, timeout=BRIDGE_CALL_TIMEOUT_S)
            except Exception:
                pass
            self._stop_ws = None

    async def _force_reconnect(self, reason: str) -> None:
        """Tear down + rebuild the WS subscription. Lock-protected so concurrent
        triggers don't race."""
        if self._reconnect_lock.locked():
            return
        async with self._reconnect_lock:
            _emit_log("warn", f"forcing websocket resubscribe: {reason}")
            await self._stop_subscription_inner()
            try:
                await self._start_subscription_inner()
                _emit_log("info", "websocket resubscribed cleanly")
            except Exception as e:
                # Leave _subscribed = False so the next watchdog tick retries.
                # Bridge auth state is preserved, so we don't need a full
                # re-login — just another _start_subscription_inner attempt.
                _emit_log(
                    "warn",
                    f"resubscribe failed: {type(e).__name__}: {e} (will retry)",
                )

    async def _heartbeat_loop(self) -> None:
        """Updates _heartbeat_at every HEARTBEAT_INTERVAL_S. Watched by the
        OS-thread stall watchdog as proof that the asyncio loop is alive.

        This task is intentionally trivial — no I/O, no awaits on bridge state
        — so it can run even if other tasks are stuck in network calls. If
        THIS task stops firing, asyncio itself is wedged and the OS thread
        kills the daemon."""
        while True:
            self._heartbeat_at = time.monotonic()
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    def _stall_watchdog_thread(self) -> None:
        """OS-thread watchdog. Runs outside asyncio so it survives event-loop
        wedges. If the heartbeat is stale by STALL_THRESHOLD_S, force-exits the
        process — the Node-side python-bridge re-spawns us fresh. This is the
        backstop that fires when pyalarmdotcomajax's WS recv blocks the loop
        synchronously during a half-open TCP scenario (the original 04-25 bug).

        Writes to stderr (not stdout/_emit_log) because the asyncio loop owns
        stdout's writer and may itself be wedged.
        """
        while True:
            time.sleep(STALL_CHECK_INTERVAL_S)
            age = time.monotonic() - self._heartbeat_at
            if age > STALL_THRESHOLD_S:
                sys.stderr.write(
                    f"FATAL: asyncio heartbeat stale by {age:.0f}s "
                    f"(threshold {STALL_THRESHOLD_S:.0f}s), forcing exit for respawn\n"
                )
                sys.stderr.flush()
                os._exit(1)

    async def _watchdog_loop(self) -> None:
        """Liveness watchdog. Fires _force_reconnect when the reconcile loop
        appears wedged (no successful reconcile within LIVENESS_TIMEOUT_S).

        Uses time-of-last-successful-reconcile rather than time-of-last-event
        because CONNECTION_EVENT heartbeats from pyalarmdotcomajax arrive in
        bursts every ~5min — too sparse to use as a tight liveness signal —
        whereas a healthy reconcile loop runs every RECONCILE_INTERVAL_S.
        """
        while True:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
                if self._bridge is None:
                    continue
                if self._last_successful_reconcile_at == 0.0 and not self._subscribed:
                    continue
                age = time.monotonic() - self._last_successful_reconcile_at
                if age > LIVENESS_TIMEOUT_S:
                    await self._force_reconnect(
                        f"no successful reconcile in {age:.0f}s (threshold {int(LIVENESS_TIMEOUT_S)}s)"
                    )
                    continue
                if not self._subscribed:
                    await self._force_reconnect("subscription state lost")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _emit_log("warn", f"watchdog error: {type(e).__name__}: {e}")

    async def _reconcile_loop(self) -> None:
        """Periodically reload full device state and emit notifications for any drift."""
        assert self._bridge is not None
        while self._subscribed:
            try:
                await asyncio.sleep(RECONCILE_INTERVAL_S)
                if not self._subscribed:
                    break
                await self._run_reconcile("periodic")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _emit_log("warn", f"reconcile loop error: {type(e).__name__}: {e}")

    async def _post_event_reconcile(self) -> None:
        """Fires a reconcile a few seconds after an event arrives, to catch the
        follow-up transition that pyalarmdotcomajax commonly drops."""
        try:
            await asyncio.sleep(POST_EVENT_RECONCILE_DELAY_S)
            if self._subscribed:
                await self._run_reconcile("post-event")
        except Exception as e:
            _emit_log("warn", f"post-event reconcile error: {type(e).__name__}: {e}")

    async def _emit_delayed(self, wire: dict, delay_s: float, *, label: str = "delayed") -> None:
        """Emit a device_updated after a small delay. Used by the OPENED_CLOSED
        blip handler to follow the open event with a closed event."""
        try:
            await asyncio.sleep(delay_s)
            if not self._subscribed:
                return
            self._known_devices[wire["id"]] = wire
            _emit_notification("device_updated", {"device": wire})
            _emit_log("info", f"{label}: {wire.get('name')} {wire}")
        except Exception as e:
            _emit_log("warn", f"delayed emit error: {type(e).__name__}: {e}")

    async def _run_reconcile(self, reason: str) -> None:
        """Shared implementation — refresh full state and diff/emit any changes.

        Wraps bridge.initialize() in asyncio.wait_for so a hung TCP socket
        surfaces as TimeoutError instead of blocking the reconcile loop forever.
        On success, records the timestamp the watchdog uses for liveness.
        """
        assert self._bridge is not None
        await asyncio.wait_for(self._bridge.initialize(), timeout=BRIDGE_CALL_TIMEOUT_S)
        self._last_successful_reconcile_at = time.monotonic()
        current = {d["id"]: d for d in self._snapshot_devices()}
        changes = 0
        for device_id, wire in current.items():
            if self._known_devices.get(device_id) != wire:
                self._known_devices[device_id] = wire
                _emit_notification("device_updated", {"device": wire})
                changes += 1
        if set(current.keys()) != set(self._known_devices.keys()):
            self._known_devices = current
            _emit_notification(
                "devices_enumerated", {"devices": list(current.values())}
            )
        if changes:
            _emit_log("info", f"reconcile ({reason}): {changes} device(s) drifted, corrected")

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
    # SECURITY: pyalarmdotcomajax's debug logging dumps entire HTTP request bodies,
    # including the login POST (which contains the password in plaintext). Force its
    # logger to WARN regardless of what the user sets our plugin's log level to, so a
    # debug flag never leaks credentials to the Homebridge log file.
    logging.getLogger("pyalarmdotcomajax").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    _emit_log("info", "daemon started (pyalarmdotcomajax 0.6.x)")

    daemon = Daemon()

    # Start the OS-thread stall watchdog and the asyncio heartbeat that feeds
    # it. Both run for the lifetime of the process. The thread is daemon=True
    # so it dies with the process on clean exit.
    daemon._heartbeat_at = time.monotonic()
    daemon._heartbeat_task = asyncio.create_task(daemon._heartbeat_loop())
    daemon._stall_thread = threading.Thread(
        target=daemon._stall_watchdog_thread,
        name="stall-watchdog",
        daemon=True,
    )
    daemon._stall_thread.start()

    queue, pump_task = await _stdin_lines()
    # Hold a reference to the pump task to prevent it from being GC'd while awaiting
    # stdin — otherwise the daemon silently stops receiving JSON-RPC requests after
    # the first handler awaits something.
    _ = pump_task

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    # Keep strong references to in-flight dispatch tasks to avoid GC.
    pending: set[asyncio.Task] = set()
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
            task = asyncio.create_task(daemon.dispatch(line))
            pending.add(task)
            task.add_done_callback(pending.discard)
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
