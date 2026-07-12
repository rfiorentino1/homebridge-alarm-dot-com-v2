#!/usr/bin/env python3
"""JSON-RPC daemon bridging pyalarmdotcomajax 0.6.x to the Homebridge plugin.

Wire protocol: newline-delimited JSON-RPC 2.0 over stdin/stdout.

Supported methods from Node → Python:
    login(username, password, mfaCookie?)        → {"ok": true}
    enumerate_devices(include_security_panel,
                      include_contact_sensors,
                      include_motion_sensors)    → {"devices": [...]}
                  # NB: locks, lights, thermostats, garage doors, gates,
                  # water sensors, and water valves are always discovered
                  # (no per-type toggle). Add toggles here if anyone ever
                  # asks; default-on matches Rocco's "auto-discovered" ask.
    panel_action(device_id, action, bypass_zones?) → {"ok": true}
    device_action(device_id, kind, action, value?) → {"ok": true}
                  # Generic actuator entrypoint for non-panel devices.
                  # `kind` is the wire kind ("lock", "light", "thermostat",
                  # "garage_door", "gate", "water_valve").
                  # `action` and `value` semantics are kind-specific —
                  # see _device_action below for the full grammar.
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
    SessionExpired,
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
from pyalarmdotcomajax.models.lock import (  # type: ignore[import-untyped]
    Lock,
    LockState,
)
from pyalarmdotcomajax.models.light import (  # type: ignore[import-untyped]
    Light,
    LightState,
)
from pyalarmdotcomajax.models.thermostat import (  # type: ignore[import-untyped]
    Thermostat,
    ThermostatState,
)
from pyalarmdotcomajax.models.garage_door import (  # type: ignore[import-untyped]
    GarageDoor,
    GarageDoorState,
)
from pyalarmdotcomajax.models.gate import (  # type: ignore[import-untyped]
    Gate,
    GateState,
)
from pyalarmdotcomajax.models.water_sensor import (  # type: ignore[import-untyped]
    WaterSensor,
)
from pyalarmdotcomajax.models.water_valve import (  # type: ignore[import-untyped]
    WaterValve,
    WaterValveState,
)
from pyalarmdotcomajax.websocket.client import (  # type: ignore[import-untyped]
    RawResourceEventMessage,
    WebSocketState,
)
from pyalarmdotcomajax.websocket.messages import (  # type: ignore[import-untyped]
    ResourceEventType,
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


def _derive_closed(s: Sensor) -> bool:
    """Composite source-of-truth for HomeKit closed/open.

    Priority (first match wins):
      1. `state` == OPEN/CLOSED — pyalarmdotcomajax's WS event handler
         mutates `state` instantly on Opened (15) / Closed (0) events,
         so this path delivers snappy push-driven HK transitions.
      2. `display_state_text` ("Open"/"Closed") — cloud-authoritative
         human label, the same field the alarm.com web UI and Alexa
         skill display. Updated by REST refresh (every 10s reconcile).
         This is the authoritative truth that survives WS event drops.
      3. `open_closed_status` int (1=Open, 2=Closed) — same authority
         as `display_state_text`, used as a fallback in case the cloud
         omits the human label.
      4. Default closed — sensors are normally closed in steady state,
         and false-open in HK can trigger automations spuriously.

    Why composite: WS `state` is fast but pyalarmdotcomajax leaves it
    stuck at OPENED_CLOSED (9) for collapsed cycles and never touches
    `display_state_text` / `open_closed_status`. Reading both means we
    get the snappy WS path AND the eventual-consistent REST truth —
    neither alone is sufficient.
    """
    state = s.attributes.state
    if state == SensorState.OPEN:
        return False
    if state == SensorState.CLOSED:
        return True

    attrs = s.api_resource.attributes
    text = attrs.get("display_state_text")
    if text == "Open":
        return False
    if text == "Closed":
        return True

    # Encoding verified empirically 2026-05-06 from a Great Room Slider sustained
    # open: openClosedStatus=3 means Open, =2 means Closed. The earlier inferred
    # mapping (1=Open) from JS-bundle inspection was wrong.
    ocs = attrs.get("open_closed_status")
    if ocs == 3:
        return False
    if ocs == 2:
        return True

    return True


def _sensor_to_wire_contact(s: Sensor, *, pending_close: bool = False) -> dict:
    """Map a Sensor to our HomeKit wire representation.

    `pending_close=True` forces `closed: False` regardless of any field —
    used by the OPENED_CLOSED handler to force a brief OPEN pulse so
    HomeKit automations watching for an open transition still fire on
    cycles that ADC collapsed into a single OpenedClosed event.
    """
    closed = False if pending_close else _derive_closed(s)
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


# --- Lock / Light / Thermostat / Garage / Gate / Water sensor / Water valve ---
#
# These map straight from pyalarmdotcomajax models to a minimal wire format
# consumed by the corresponding accessory class on the Node side. Each wire
# builder is intentionally narrow (only the fields HomeKit needs) so the
# accessory glue stays small and so additions don't blow up the JSON payload.
#
# Why no WS-authoritative cache for these (unlike partition / sensor):
# pyalarmdotcomajax's lock / light / thermostat / garage / gate / water-valve
# controllers each carry an `_event_state_map` (or a custom `_handle_event`)
# that mutates the resource's attribute on the *exact* WS event the user
# cares about — DoorLocked sets LockState.LOCKED, LightTurnedOn sets
# LightState.ON, ThermostatModeChanged updates ATTR_STATE, etc. So reading
# `attrs.state` immediately after a RESOURCE_UPDATED dispatch returns the
# event-driven value, and the existing on_event → _lookup_wire → diff/emit
# flow Just Works for these.
#
# The cache we maintain for partitions + sensors exists specifically to
# suppress REST-driven *reverts* (REST lags the WS by minutes; a stale REST
# refresh can clobber a fresh WS push). If we ever see that drift on lock /
# light / etc. in the field, add an authoritative cache for them too.


def _lock_to_wire(lock: Lock) -> dict:
    attrs = lock.attributes
    state = attrs.state
    out: dict[str, Any] = {
        "kind": "lock",
        "id": str(lock.id),
        "name": lock.name or f"Lock {lock.id}",
        # HomeKit's LockMechanism has UNSECURED / SECURED / JAMMED / UNKNOWN.
        # ADC has UNKNOWN / LOCKED / UNLOCKED / HIDDEN — no jam signal we
        # can rely on. Map LOCKED→secured, UNLOCKED→unsecured, else unknown.
        "locked": state == LockState.LOCKED,
        "unknown": state in (LockState.UNKNOWN, LockState.HIDDEN, None),
    }
    low = _battery_is_low(attrs.battery_level_classification)
    if low is not None:
        out["lowBattery"] = low
    return out


def _light_to_wire(light: Light) -> dict:
    attrs = light.attributes
    state = attrs.state
    out: dict[str, Any] = {
        "kind": "light",
        "id": str(light.id),
        "name": light.name or f"Light {light.id}",
        "on": state == LightState.ON,
        "dimmer": bool(attrs.is_dimmer),
    }
    if attrs.is_dimmer:
        # ADC retains the dimmer level when off so HomeKit can survive an
        # off→on round-trip at the prior brightness.
        out["brightness"] = max(0, min(100, int(attrs.light_level or 0)))
    low = _battery_is_low(attrs.battery_level_classification)
    if low is not None:
        out["lowBattery"] = low
    return out


def _f_to_c(value_f: float) -> float:
    return round((value_f - 32.0) * 5.0 / 9.0, 1)


def _thermostat_to_wire(t: Thermostat, uses_celsius: bool) -> dict:
    """Build the wire payload for a thermostat.

    HomeKit's Thermostat characteristics are ALWAYS in Celsius internally —
    `TemperatureDisplayUnits` only affects what the Home app shows, never the
    raw values exchanged with the accessory. So we normalize every temperature
    to °C here and pass `usesCelsius` separately for the display-unit hint.
    """
    attrs = t.attributes

    def to_c(v: float | None) -> float | None:
        if v is None:
            return None
        return float(v) if uses_celsius else _f_to_c(float(v))

    state = attrs.state
    if state in (ThermostatState.HEAT, ThermostatState.AUXHEAT):
        mode = "heat"
    elif state == ThermostatState.COOL:
        mode = "cool"
    elif state == ThermostatState.AUTO:
        mode = "auto"
    elif state == ThermostatState.OFF:
        mode = "off"
    else:
        mode = "unknown"

    # Prefer forwarding_ambient_temp (which includes additional sensor averaging)
    # when present; fall back to ambient_temp. ADC sometimes reports one and
    # not the other depending on hardware.
    current = attrs.forwarding_ambient_temp
    if current in (None, 0, 0.0):
        current = attrs.ambient_temp

    out: dict[str, Any] = {
        "kind": "thermostat",
        "id": str(t.id),
        "name": t.name or f"Thermostat {t.id}",
        "mode": mode,
        "supportsAuto": bool(attrs.supports_auto_mode),
        "supportsHeat": bool(attrs.supports_heat_mode),
        "supportsCool": bool(attrs.supports_cool_mode),
        "supportsOff": bool(attrs.supports_off_mode),
        "usesCelsius": bool(uses_celsius),
        "currentTempC": to_c(current),
        "heatSetpointC": to_c(attrs.heat_setpoint),
        "coolSetpointC": to_c(attrs.cool_setpoint),
        "minHeatC": to_c(attrs.min_heat_setpoint),
        "maxHeatC": to_c(attrs.max_heat_setpoint),
        "minCoolC": to_c(attrs.min_cool_setpoint),
        "maxCoolC": to_c(attrs.max_cool_setpoint),
    }
    if attrs.supports_humidity and attrs.humidity_level is not None:
        out["humidity"] = max(0, min(100, int(attrs.humidity_level)))
    return out


def _garage_door_to_wire(g: GarageDoor) -> dict:
    state = g.attributes.state
    return {
        "kind": "garage_door",
        "id": str(g.id),
        "name": g.name or f"Garage Door {g.id}",
        "open": state == GarageDoorState.OPEN,
        "closed": state == GarageDoorState.CLOSED,
    }


def _gate_to_wire(g: Gate) -> dict:
    attrs = g.attributes
    state = attrs.state
    return {
        "kind": "gate",
        "id": str(g.id),
        "name": g.name or f"Gate {g.id}",
        "open": state == GateState.OPEN,
        "closed": state == GateState.CLOSED,
        # Gates can support remote OPEN without remote CLOSE (e.g. for safety).
        # The accessory uses this to refuse a HomeKit "Close" if not supported.
        "supportsRemoteClose": bool(getattr(attrs, "supports_remote_close", False)),
    }


# Water sensor states ADC reports → HomeKit "leak detected".
# WET / OPEN / ACTIVE indicate a positive leak detection. CLOSED / DRY / IDLE
# (or UNKNOWN) mean no leak. Defaulting to "no leak" on UNKNOWN avoids false
# positive automations on first enumerate before WS pushes start flowing.
_WATER_SENSOR_LEAK_STATES = {
    SensorState.WET,
    SensorState.OPEN,
    SensorState.ACTIVE,
}


def _water_sensor_to_wire(s: WaterSensor) -> dict:
    state = s.attributes.state
    out: dict[str, Any] = {
        "kind": "water_sensor",
        "id": str(s.id),
        "name": s.name or f"Leak Sensor {s.id}",
        "leak": state in _WATER_SENSOR_LEAK_STATES,
    }
    low = _battery_is_low(s.attributes.battery_level_classification)
    if low is not None:
        out["lowBattery"] = low
    return out


def _water_valve_to_wire(v: WaterValve) -> dict:
    state = v.attributes.state
    return {
        "kind": "water_valve",
        "id": str(v.id),
        "name": v.name or f"Water Valve {v.id}",
        "open": state == WaterValveState.OPEN,
        "closed": state == WaterValveState.CLOSED,
    }


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

# When we receive an OPENED_CLOSED merged WS event, ADC's edge has collapsed
# an Open+Close pair into a single notification with NO duration field. The
# original "cycles ≥3s emit paired events" claim from the first 30-day analysis
# turned out to be wrong — a 17-day, 26000-event re-mining on 2026-05-06
# (correlated with Rocco's recollection of held-for-5/10/20-second testing)
# showed ADC's collapse window is closer to 15-30 seconds and is non-deterministic.
# Most garage/front-door entries (10-13s typical dwell) collapse silently.
#
# So a fixed-duration synthetic close is wrong: too short and HK shows ~1s for
# a real 15s open; too long and quick blips appear stuck-open. Instead we force
# OPEN immediately, then poll the per-sensor REST endpoint every
# OPENED_CLOSED_POLL_INTERVAL_S until openClosedStatus returns to 2 (Closed)
# or OPENED_CLOSED_POLL_TIMEOUT_S elapses as safety fallback. Verified
# 2026-05-06 that REST DOES update during sustained opens (3=Open, 2=Closed).
OPENED_CLOSED_POLL_INTERVAL_S = 1.5
OPENED_CLOSED_POLL_TIMEOUT_S = 30.0
# Minimum hold time after an OPENED_CLOSED before we emit close from REST
# observation. ADC sometimes sends a separate WS Closed event a few seconds
# after the OPENED_CLOSED — when that happens, the dispatch handler cancels
# this polling task and emits close at the real time. If we exit too quickly
# on REST, we miss that opportunity and HK shows the door closed before reality.
# Verified 2026-05-06: a Front Door cycle had OPENED_CLOSED at 20:13:44 with
# REST already showing Closed by 1.6s, then a separate Closed WS event arrived
# at 20:13:49 (5s later). Holding here lets the WS event win for those cases.
# Cost: brief magnet-disturbance blips that have no follow-up Closed will show
# ~5s of Open in HK instead of ~1s. Acceptable trade for transit fidelity.
OPENED_CLOSED_MIN_HOLD_S = 5.0

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

# WS-connection health (separate from the reconcile-liveness watchdog above,
# which is BLIND to a dead WebSocket while REST keeps succeeding — the
# 2026-06-02 freeze). If the pyalarmdotcomajax WebSocketClient reports itself
# not-connected for this long, force a true rebuild. Generous enough to let the
# lib's own internal reconnect (10*attempts backoff, up to 30min) reconnect a
# transient blip first, but far short of "never" — a DEAD controller (lib gave
# up after its 25 attempts) is force-rebuilt immediately, no wait.
WS_DOWN_RECONNECT_S = 120.0

# How long REST must *persistently* contradict the WS-authoritative cache before
# we distrust the cache (presume the WS is dead/zombie) and let REST drive +
# trigger a WS rebuild. Must sit safely beyond alarm.com's known 5-10min REST
# lag so a normal lagging/double-firing REST value never trips it — that lag is
# exactly what the authoritative cache exists to paper over (2026-05-09). Also
# reused as the "WS was down long enough that state may have changed unobserved"
# threshold for clearing the frozen cache on reconnect.
AUTH_STALE_AFTER_S = 600.0

# Sensor-cache version of the staleness guard. A contact/motion sensor's
# WS-authoritative cache can get stuck at OPEN when the matching close arrives
# as a merged OPENED_CLOSED event (which on_raw_event deliberately does NOT
# cache) instead of a discrete Closed — so the cache is never corrected to
# CLOSED and, because it overrides REST, HomeKit stays pinned Open forever even
# though ADC's REST says Closed (observed 2026-07-12, garage). Unlike partitions
# (5-10min arm/disarm REST lag → 600s window + WS rebuild), a sensor's true
# state settles in REST within seconds (the OPENED_CLOSED recovery poll sees
# Closed in ~6s, 30s timeout worst case), and a stale-open sensor should self-
# heal in seconds not minutes — so this window is short and does NOT trigger a
# WS rebuild (the WS is fine; it just delivered a merged event). Sits safely
# beyond the 30s OPENED_CLOSED recovery window so the poll owns the sensor first.
SENSOR_AUTH_STALE_AFTER_S = 45.0

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
        # WS-health tracking (watched by _watchdog_loop). _ws_disconnected_since
        # is set when the WS controller first reports not-connected and cleared
        # when it reconnects; _ws_rebuild_requested is a one-shot flag the
        # reconcile staleness guard raises (it can't tear down its own task, so
        # it asks the watchdog to do the rebuild). _partition_disagree_since
        # tracks how long REST has contradicted each partition's cached WS state.
        self._ws_disconnected_since: float | None = None
        self._ws_rebuild_requested: bool = False
        self._partition_disagree_since: dict[str, float] = {}
        self._heartbeat_at: float = time.monotonic()
        self._heartbeat_task: asyncio.Task | None = None
        self._stall_thread: threading.Thread | None = None
        self._known_devices: dict[str, dict] = {}
        # WS-AUTHORITATIVE STATE CACHE.
        #
        # Updated ONLY by genuine RAW_RESOURCE_EVENT pushes from the alarm.com
        # WebSocket — i.e. messages with a real `subtype` (Disarmed=8,
        # ArmedStay=9, ArmedAway=10, ArmedNight=113, Opened=15, Closed=0).
        # These are the only state-change signals alarm.com sends in real time
        # and they carry an unambiguous identity. Anything else (REST refresh,
        # internal pyalarmdotcomajax fanout, periodic reconcile) goes through
        # RESOURCE_UPDATED without a subtype attribution.
        #
        # The wire-builder methods below consult this cache before falling
        # through to `attrs.state`. If a real WS push said the panel is
        # ARMED_AWAY and a subsequent REST-driven RESOURCE_UPDATED tries to
        # revert to DISARMED (because alarm.com's REST lags 5–10 minutes
        # behind WS, OR because of a same-second double-fire we haven't
        # fully traced), the cached push wins and the revert is suppressed.
        # Real arm→disarm→arm sequences within seconds work because each
        # transition is a distinct WS push that updates the cache.
        #
        # Cache survives WS reconnects (state didn't actually change just
        # because the socket blipped). Cleared only on daemon restart.
        self._authoritative_partition_state: dict[str, PartitionState] = {}
        self._authoritative_sensor_state: dict[str, SensorState] = {}
        # Sensor analogue of _partition_disagree_since: tracks how long a sensor's
        # WS-authoritative cache (OPEN) has persistently disagreed with settled
        # REST (Closed), so the reconcile guard can drop a stuck-open cache entry
        # after SENSOR_AUTH_STALE_AFTER_S and let REST self-heal HomeKit.
        self._sensor_disagree_since: dict[str, float] = {}
        # ALARM-STATE CACHE. Keyed by partition id; True while an alarm /
        # pending-alarm / panic is standing. Alarm.com's PartitionState enum
        # has no alarm member (an alarm is a separate flag) and a PendingAlarm
        # WS event is delivered against the violated SENSOR id, not the
        # partition — so we track it here, keyed by partition id, and let
        # _build_partition_wire override the wire `state` to "triggered".
        # Set by alarm-class WS pushes; cleared on Disarmed / AlarmCancelled.
        self._partition_alarm_active: dict[str, bool] = {}
        # Tracks partitions we've already logged an auto-skip for, so the
        # "no permission to change state" notice doesn't repeat every reconcile.
        self._logged_panel_skip: dict[str, bool] = {}
        self._raw_unsubscribe: Callable[[], None] | None = None
        self._handlers: dict[str, MethodHandler] = {
            "login": self._login,
            "enumerate_devices": self._enumerate_devices,
            "panel_action": self._panel_action,
            "device_action": self._device_action,
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

        def _count(kind: str) -> int:
            return sum(1 for d in devices if d["kind"] == kind)

        _emit_log(
            "info",
            f"discovered {len(devices)} device(s): "
            f"{_count('panel')} panels, "
            f"{_count('contact_sensor')} contacts, "
            f"{_count('motion_sensor')} motions, "
            f"{_count('lock')} locks, "
            f"{_count('light')} lights, "
            f"{_count('thermostat')} thermostats, "
            f"{_count('garage_door')} garage doors, "
            f"{_count('gate')} gates, "
            f"{_count('water_sensor')} water sensors, "
            f"{_count('water_valve')} water valves",
        )
        return {"devices": devices}

    def _build_partition_wire(self, p: Partition) -> dict:
        """Wire-builder for a partition that prefers the WS-authoritative state
        cache over `attrs.state`. See the cache's docstring in __init__ for why
        this is necessary."""
        wire = _partition_to_wire(p)
        auth = self._authoritative_partition_state.get(str(p.id))
        if auth is not None:
            wire["state"] = _partition_state_to_wire(auth)
        # An active / pending alarm overrides the armed/disarmed state.
        # HomeKit's SecuritySystem accessory has a single alarm state and the
        # Node side already maps the "triggered" wire value → ALARM_TRIGGERED.
        if self._partition_alarm_active.get(str(p.id)):
            wire["state"] = "triggered"
        return wire

    def _build_sensor_wire_contact(self, s: Sensor, *, pending_close: bool = False) -> dict:
        """Wire-builder for a contact sensor that prefers the WS-authoritative
        state cache over `attrs.state` / REST. `pending_close=True` (used by the
        OPENED_CLOSED force-open path) bypasses the cache entirely so the REST
        polling loop in _opened_closed_recover_close can drive the eventual
        close transition."""
        if pending_close:
            return _sensor_to_wire_contact(s, pending_close=True)
        wire = _sensor_to_wire_contact(s)
        auth = self._authoritative_sensor_state.get(str(s.id))
        if auth == SensorState.OPEN:
            wire["closed"] = False
        elif auth == SensorState.CLOSED:
            wire["closed"] = True
        # Other auth values (OPENED_CLOSED, etc.) don't override — let
        # _derive_closed's existing fallback chain decide.
        return wire

    def _build_sensor_wire_motion(self, s: Sensor) -> dict:
        wire = _sensor_to_wire_motion(s)
        auth = self._authoritative_sensor_state.get(str(s.id))
        if auth == SensorState.ACTIVE:
            wire["motion"] = True
        elif auth == SensorState.IDLE:
            wire["motion"] = False
        return wire

    def _snapshot_devices(self) -> list[dict]:
        assert self._bridge is not None
        bridge = self._bridge
        out: list[dict] = []

        if self._expose_panel:
            for p in bridge.partitions:
                # Smart-home-only ADC accounts (locks/lights/thermostats with
                # no actual security system) get a placeholder SYSTEM partition
                # from the API that the user cannot arm/disarm. Skip it so users
                # don't see a useless dummy panel in HomeKit. Real panels have
                # has_permission_to_change_state=True.
                if not getattr(p.attributes, "has_permission_to_change_state", True):
                    if not self._logged_panel_skip.get(str(p.id)):
                        self._logged_panel_skip[str(p.id)] = True
                        logging.info(
                            "Skipping partition %s (%s): account has no permission to change state — "
                            "likely a placeholder for a smart-home-only account with no actual security system. "
                            "Set exposeSecurityPanel=false to silence this entirely, or ignore.",
                            p.name or str(p.id), str(p.id),
                        )
                    continue
                out.append(self._build_partition_wire(p))

        if self._expose_contacts or self._expose_motion:
            for s in bridge.sensors:
                subtype = getattr(s.attributes, "device_type", None)
                if subtype in CONTACT_SUBTYPES and self._expose_contacts:
                    out.append(self._build_sensor_wire_contact(s))
                elif subtype in MOTION_SUBTYPES and self._expose_motion:
                    out.append(self._build_sensor_wire_motion(s))

        # Lock / light / thermostat / garage / gate / water_sensor / water_valve
        # are always auto-discovered — there is no per-type config toggle.
        # Each is best-effort: if a controller errors on iteration (e.g. an
        # ADC account simply has none of a given kind), skip silently. We
        # don't want one bad device class to gate the rest from enumerating.
        for lock in bridge.locks:
            with suppress(Exception):
                out.append(_lock_to_wire(lock))
        for light in bridge.lights:
            with suppress(Exception):
                out.append(_light_to_wire(light))
        uses_celsius = bool(getattr(bridge.auth_controller, "use_celsius", False))
        for thermostat in bridge.thermostats:
            with suppress(Exception):
                out.append(_thermostat_to_wire(thermostat, uses_celsius))
        for garage in bridge.garage_doors:
            with suppress(Exception):
                out.append(_garage_door_to_wire(garage))
        for gate in bridge.gates:
            with suppress(Exception):
                out.append(_gate_to_wire(gate))
        for water in bridge.water_sensors:
            with suppress(Exception):
                out.append(_water_sensor_to_wire(water))
        for valve in bridge.water_valves:
            with suppress(Exception):
                out.append(_water_valve_to_wire(valve))

        return out

    def _lookup_wire(self, resource_id: str) -> dict | None:
        """Find the current wire representation for a resource id, or None if we don't expose it."""
        assert self._bridge is not None
        bridge = self._bridge

        partition = bridge.partitions.get(resource_id)
        if partition is not None and self._expose_panel:
            return self._build_partition_wire(partition)

        sensor = bridge.sensors.get(resource_id)
        if sensor is not None:
            subtype = getattr(sensor.attributes, "device_type", None)
            if subtype in CONTACT_SUBTYPES and self._expose_contacts:
                return self._build_sensor_wire_contact(sensor)
            if subtype in MOTION_SUBTYPES and self._expose_motion:
                return self._build_sensor_wire_motion(sensor)

        # New device kinds — always discovered, so no config gate. Resource
        # IDs are globally unique across kinds in ADC so it's safe to check
        # each collection in turn; the first hit wins.
        lock = bridge.locks.get(resource_id)
        if lock is not None:
            return _lock_to_wire(lock)

        light = bridge.lights.get(resource_id)
        if light is not None:
            return _light_to_wire(light)

        thermostat = bridge.thermostats.get(resource_id)
        if thermostat is not None:
            uses_celsius = bool(
                getattr(bridge.auth_controller, "use_celsius", False)
            )
            return _thermostat_to_wire(thermostat, uses_celsius)

        garage = bridge.garage_doors.get(resource_id)
        if garage is not None:
            return _garage_door_to_wire(garage)

        gate = bridge.gates.get(resource_id)
        if gate is not None:
            return _gate_to_wire(gate)

        water = bridge.water_sensors.get(resource_id)
        if water is not None:
            return _water_sensor_to_wire(water)

        valve = bridge.water_valves.get(resource_id)
        if valve is not None:
            return _water_valve_to_wire(valve)

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

    # ----- device action (lock / light / thermostat / garage / gate / valve) -----

    async def _device_action(self, params: dict) -> dict:
        """Generic actuator entrypoint for non-panel device kinds.

        Wire grammar (kind, action, value):
            lock        action ∈ {"lock", "unlock"}                              (no value)
            light       action ∈ {"on", "off"}                                   (no value)
                        action == "set_brightness", value = int 0..100
            thermostat  action == "set_mode",      value = {"off","heat","cool","auto"}
                        action == "set_heat_setpoint", value = float (°C)
                        action == "set_cool_setpoint", value = float (°C)
            garage_door action ∈ {"open", "close"}                               (no value)
            gate        action ∈ {"open", "close"}                               (no value)
            water_valve action ∈ {"open", "close"}                               (no value)

        Returns {"ok": True} on success; raises on auth / network / unsupported.
        """
        self._require_bridge()
        bridge = self._bridge
        assert bridge is not None

        device_id = params.get("device_id")
        kind = params.get("kind")
        action = params.get("action")
        value = params.get("value")

        if not device_id:
            raise ValueError("device_id is required")
        if not kind:
            raise ValueError("kind is required")
        if not action:
            raise ValueError("action is required")

        rid = str(device_id)

        if kind == "lock":
            if action == "lock":
                await bridge.locks.lock(rid)
            elif action == "unlock":
                await bridge.locks.unlock(rid)
            else:
                raise ValueError(f"unknown lock action: {action}")

        elif kind == "light":
            if action == "on":
                await bridge.lights.turn_on(rid)
            elif action == "off":
                await bridge.lights.turn_off(rid)
            elif action == "set_brightness":
                if value is None:
                    raise ValueError("set_brightness requires a value 0..100")
                brightness = max(0, min(100, int(value)))
                await bridge.lights.set_brightness(rid, brightness)
            else:
                raise ValueError(f"unknown light action: {action}")

        elif kind == "thermostat":
            uses_celsius = bool(
                getattr(bridge.auth_controller, "use_celsius", False)
            )
            if action == "set_mode":
                mode_map = {
                    "off": ThermostatState.OFF,
                    "heat": ThermostatState.HEAT,
                    "cool": ThermostatState.COOL,
                    "auto": ThermostatState.AUTO,
                }
                if not isinstance(value, str) or value not in mode_map:
                    raise ValueError(f"unknown thermostat mode: {value}")
                await bridge.thermostats.set_state(id=rid, state=mode_map[value])
            elif action == "set_heat_setpoint":
                if value is None:
                    raise ValueError("set_heat_setpoint requires a numeric value (°C)")
                temp_c = float(value)
                # HomeKit always speaks °C; ADC speaks whatever Identity.use_celsius
                # says. Convert before forwarding.
                target = temp_c if uses_celsius else round(temp_c * 9.0 / 5.0 + 32.0, 1)
                await bridge.thermostats.set_state(id=rid, heat_setpoint=target)
            elif action == "set_cool_setpoint":
                if value is None:
                    raise ValueError("set_cool_setpoint requires a numeric value (°C)")
                temp_c = float(value)
                target = temp_c if uses_celsius else round(temp_c * 9.0 / 5.0 + 32.0, 1)
                await bridge.thermostats.set_state(id=rid, cool_setpoint=target)
            else:
                raise ValueError(f"unknown thermostat action: {action}")

        elif kind == "garage_door":
            if action == "open":
                await bridge.garage_doors.open(rid)
            elif action == "close":
                await bridge.garage_doors.close(rid)
            else:
                raise ValueError(f"unknown garage_door action: {action}")

        elif kind == "gate":
            if action == "open":
                await bridge.gates.open(rid)
            elif action == "close":
                gate = bridge.gates.get(rid)
                if gate is not None and not bool(
                    getattr(gate.attributes, "supports_remote_close", False)
                ):
                    raise RuntimeError(
                        "Gate does not support remote close on this account"
                    )
                await bridge.gates.close(rid)
            else:
                raise ValueError(f"unknown gate action: {action}")

        elif kind == "water_valve":
            if action == "open":
                await bridge.water_valves.open(rid)
            elif action == "close":
                await bridge.water_valves.close(rid)
            else:
                raise ValueError(f"unknown water_valve action: {action}")

        else:
            raise ValueError(f"device_action: unsupported kind: {kind}")

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
        # Fresh socket — drop any carried-over WS-down timer so the watchdog
        # re-arms cleanly and gives this connection its full WS_DOWN_RECONNECT_S.
        self._ws_disconnected_since = None

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
            _emit_log(
                "debug",
                f"event: topic={topic_name} id={resource_id} resource_type={resource_type}",
            )

            if topic in (
                EventBrokerTopic.RESOURCE_UPDATED,
                EventBrokerTopic.RAW_RESOURCE_EVENT,
            ):
                # Both trigger the same "re-read and diff" flow. RAW_RESOURCE_EVENT is
                # what comes through for most ADT-branded sensors in practice.
                if not resource_id:
                    return

                # Special handling for OPENED_CLOSED: ADC collapses an Open+Close
                # pair into a single WS notification with no duration. We force-
                # emit OPEN to HomeKit immediately, then start a per-sensor REST
                # polling task that keeps HK at Open until ADC's REST shows the
                # sensor returned to Closed (or a safety timeout fires). See
                # OPENED_CLOSED_POLL_* constants above and `_opened_closed_recover_close`
                # for the polling logic.
                if self._bridge is not None:
                    sensor = self._bridge.sensors.get(str(resource_id))
                    if (
                        sensor is not None
                        and sensor.attributes.state == SensorState.OPENED_CLOSED
                        and sensor.attributes.device_type in CONTACT_SUBTYPES
                        and self._expose_contacts
                    ):
                        open_wire = self._build_sensor_wire_contact(sensor, pending_close=True)
                        if self._known_devices.get(open_wire["id"]) != open_wire:
                            self._known_devices[open_wire["id"]] = open_wire
                            _emit_notification("device_updated", {"device": open_wire})
                            _emit_log(
                                "info",
                                f"force-open on OPENED_CLOSED: {open_wire['name']} "
                                f"(REST-poll for close, timeout {int(OPENED_CLOSED_POLL_TIMEOUT_S)}s)",
                            )
                        # Cancel any existing pending close-poll task for this
                        # sensor (rapid re-triggers).
                        wire_id = open_wire["id"]
                        prev = self._pending_synthetic_close.pop(wire_id, None)
                        if prev is not None and not prev.done():
                            prev.cancel()
                        task = asyncio.create_task(
                            self._opened_closed_recover_close(wire_id, open_wire["name"])
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
                # a pending OPENED_CLOSED close-recovery poll, cancel the poll —
                # reality wins.
                wid = wire["id"]
                pending = self._pending_synthetic_close.pop(wid, None)
                if pending is not None and not pending.done():
                    pending.cancel()
                    _emit_log(
                        "info",
                        f"opened_closed-poll cancelled by real event: {wire.get('name')}",
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

        # SEPARATE subscription for RAW_RESOURCE_EVENT.
        #
        # Why two subscriptions: AlarmBridge.subscribe() (used above) only
        # registers callbacks for RESOURCE_ADDED/UPDATED/DELETED + CONNECTION_EVENT
        # — it deliberately does NOT include RAW_RESOURCE_EVENT. But RAW is the
        # only topic that carries the original WS message with its identifying
        # `subtype` (Disarmed=8, ArmedAway=10, Opened=15, etc.). RESOURCE_UPDATED
        # is fired both for genuine pushes AND for REST-driven catalog updates
        # — same envelope, no way to tell them apart.
        #
        # We subscribe to RAW directly so we can populate the authoritative
        # state cache (see __init__) from real pushes only. The cache is then
        # consulted by _build_partition_wire / _build_sensor_wire_* to keep
        # downstream emits truthful even when REST overwrites attrs.state.
        def on_raw_event(msg: EventBrokerMessage) -> None:
            if not isinstance(msg, RawResourceEventMessage):
                return
            ws_msg = msg.ws_message
            subtype = getattr(ws_msg, "subtype", None)
            if not isinstance(subtype, ResourceEventType):
                return  # PropertyChange/Status messages have no event subtype
            full_device_id = getattr(ws_msg, "full_device_id", None)
            if not full_device_id:
                return
            rid = str(full_device_id)

            # Alarm / pending-alarm / panic transitions.
            #
            # An alarm is NOT a value of Alarm.com's PartitionState enum — it's
            # a separate condition — and a PendingAlarm/Alarm WS event is
            # delivered against the violated SENSOR id, not the partition.
            # These event subtypes are also flagged "Unsupported" in
            # pyalarmdotcomajax, so they fire NO RESOURCE_UPDATED and on_event
            # never runs for them — we must record the state and emit here.
            # HomeKit's SecuritySystem has a single alarm state, so map any
            # alarm-class event → "triggered" for every partition; clear on
            # Disarmed / AlarmCancelled.
            ALARM_SUBTYPES = {
                ResourceEventType.Alarm,
                ResourceEventType.PendingAlarm,
                ResourceEventType.PolicePanic,
                ResourceEventType.SilentPolicePanic,
                ResourceEventType.PolicePanicSuspectedAlarm,
                ResourceEventType.SilentPolicePanicSuspectedAlarm,
                ResourceEventType.FirePanic,
                ResourceEventType.AuxiliaryPanic,
                ResourceEventType.AuxPanicPendingAlarm,
                ResourceEventType.AuxPanicSuspectedAlarm,
            }
            bridge_alarm = self._bridge
            partition_ids = (
                [str(p.id) for p in bridge_alarm.partitions]
                if bridge_alarm is not None
                else []
            )

            def _emit_partition_wires() -> None:
                if bridge_alarm is None or not self._expose_panel:
                    return
                for part in bridge_alarm.partitions:
                    w = self._build_partition_wire(part)
                    if self._known_devices.get(w["id"]) != w:
                        self._known_devices[w["id"]] = w
                        _emit_notification("device_updated", {"device": w})
                        _emit_log("info", f"device_updated: {w.get('name')} {w}")

            if subtype in ALARM_SUBTYPES:
                for pid in partition_ids:
                    self._partition_alarm_active[pid] = True
                _emit_log(
                    "info",
                    f"ws-authoritative: ALARM triggered ({subtype.name}, src={rid}) "
                    f"→ partitions {partition_ids}",
                )
                _emit_partition_wires()
                return

            if subtype == ResourceEventType.AlarmCancelled:
                for pid in partition_ids:
                    self._partition_alarm_active[pid] = False
                _emit_log(
                    "info", f"ws-authoritative: alarm cancelled ({subtype.name})"
                )
                _emit_partition_wires()
                return

            # A disarm also clears any standing alarm. Clear the flag here,
            # then fall through so the partition_map block below still records
            # the DISARMED state and the normal emit path reports "disarmed".
            if subtype == ResourceEventType.Disarmed:
                for pid in partition_ids:
                    self._partition_alarm_active[pid] = False

            # Partition arm/disarm transitions
            partition_map = {
                ResourceEventType.Disarmed: PartitionState.DISARMED,
                ResourceEventType.ArmedStay: PartitionState.ARMED_STAY,
                ResourceEventType.ArmedAway: PartitionState.ARMED_AWAY,
                ResourceEventType.ArmedNight: PartitionState.ARMED_NIGHT,
            }
            if subtype in partition_map:
                self._authoritative_partition_state[rid] = partition_map[subtype]
                _emit_log(
                    "info",
                    f"ws-authoritative: partition {rid} → {partition_map[subtype].name}",
                )
                return

            # Sensor transitions. Both motion and contact go through the same
            # cache — _build_sensor_wire_contact / _motion each pick the
            # SensorState members they care about (OPEN/CLOSED for contact,
            # ACTIVE/IDLE for motion). For contact: Opened=15→OPEN, Closed=0→CLOSED.
            # For motion: Opened=15→ACTIVE, Closed=0→IDLE. We need to know the
            # subtype to decide. Look up the sensor to see if it's motion.
            bridge_ = self._bridge
            if bridge_ is None:
                return
            sensor = bridge_.sensors.get(rid)
            if sensor is None:
                return
            device_subtype = getattr(sensor.attributes, "device_type", None)
            is_motion = device_subtype in MOTION_SUBTYPES

            if subtype == ResourceEventType.Opened:
                self._authoritative_sensor_state[rid] = (
                    SensorState.ACTIVE if is_motion else SensorState.OPEN
                )
                _emit_log(
                    "info",
                    f"ws-authoritative: sensor {rid} → {self._authoritative_sensor_state[rid].name}",
                )
            elif subtype in (
                ResourceEventType.Closed,
                ResourceEventType.DoorLeftOpenRestoral,
            ):
                self._authoritative_sensor_state[rid] = (
                    SensorState.IDLE if is_motion else SensorState.CLOSED
                )
                _emit_log(
                    "info",
                    f"ws-authoritative: sensor {rid} → {self._authoritative_sensor_state[rid].name}",
                )
            # OpenedClosed deliberately not cached — the existing
            # _opened_closed_recover_close path handles that case via REST polling.

        try:
            self._raw_unsubscribe = bridge.events.subscribe(
                EventBrokerTopic.RAW_RESOURCE_EVENT, on_raw_event
            )
        except Exception as e:
            _emit_log(
                "warn",
                f"failed to subscribe to RAW_RESOURCE_EVENT: {type(e).__name__}: {e}",
            )

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
        if self._raw_unsubscribe is not None:
            try:
                self._raw_unsubscribe()
            except Exception:
                pass
            self._raw_unsubscribe = None
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
        # Explicitly stop the underlying pyalarmdotcomajax WebSocketClient. The
        # daemon calls start_event_monitoring() WITHOUT a status callback, so it
        # returns None and self._stop_ws above is always None — meaning nothing
        # here actually tears the socket down. Worse, the controller's
        # initialize() early-returns while _initialized is True, so a rebuild via
        # _force_reconnect would just re-subscribe callbacks to the SAME (possibly
        # DEAD/zombie) socket and never reconnect. stop() cancels its tasks and
        # resets _initialized=False, so the next start truly reconnects — on
        # whatever session is current (re-derived by the WS _authenticate()).
        if self._bridge is not None:
            with suppress(Exception):
                self._bridge.ws_controller.stop()

    async def _force_reconnect(self, reason: str, *, clear_auth_cache: bool = False) -> None:
        """Tear down + rebuild the WS subscription. Lock-protected so concurrent
        triggers don't race.

        clear_auth_cache=True when the WS was down long enough (> AUTH_STALE_AFTER_S)
        that arm/disarm or sensor state may have changed unobserved while the
        WS-authoritative caches were frozen. The freshly-rebuilt WS only pushes on
        the NEXT change, so without clearing, HomeKit would keep showing the
        pre-outage state until someone re-armed. We drop the caches and let the
        reconcile loop (next tick, <=10s) repopulate current truth from REST."""
        if self._reconnect_lock.locked():
            return
        async with self._reconnect_lock:
            _emit_log("warn", f"forcing websocket resubscribe: {reason}")
            await self._stop_subscription_inner()
            try:
                await self._start_subscription_inner()
                _emit_log("info", "websocket resubscribed cleanly")
                if clear_auth_cache:
                    # Deliberately do NOT clear _partition_alarm_active — leaving a
                    # standing alarm sticky is the fail-safe direction; it clears on
                    # a real Disarmed / AlarmCancelled push.
                    self._authoritative_partition_state.clear()
                    self._authoritative_sensor_state.clear()
                    self._partition_disagree_since.clear()
                    self._sensor_disagree_since.clear()
                    _emit_log(
                        "info",
                        "cleared WS-authoritative caches after extended WS outage; "
                        "REST authoritative until fresh pushes arrive",
                    )
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
        """Liveness watchdog.

        Two independent failure modes are covered:

          1. Reconcile wedged — no successful reconcile within LIVENESS_TIMEOUT_S
             (a half-open REST/HTTP path). Uses time-of-last-successful-reconcile
             rather than time-of-last-event because CONNECTION_EVENT heartbeats
             from pyalarmdotcomajax arrive in bursts every ~5min — too sparse to
             use as a tight liveness signal.

          2. Dead WebSocket while REST is healthy — the 2026-06-02 freeze. The
             reconcile-liveness check (1) is BLIND to this: REST keeps succeeding
             so it never fires, but the WS-fed authoritative caches (arm/disarm,
             alarm) are frozen. We watch the controller's own WebSocketState and
             a persistent REST-vs-cache disagreement (flagged by reconcile via
             _ws_rebuild_requested) and force a true rebuild.
        """
        while True:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
                if self._bridge is None:
                    continue
                if self._last_successful_reconcile_at == 0.0 and not self._subscribed:
                    continue

                # (2a) Reconcile's staleness guard asked for a rebuild (it can't
                # cancel its own task, so it delegates to us).
                if self._ws_rebuild_requested:
                    self._ws_rebuild_requested = False
                    await self._force_reconnect(
                        "WS-authoritative cache stale vs REST (websocket presumed dead)"
                    )
                    continue

                # (2b) WebSocket controller health, independent of REST liveness.
                if self._subscribed:
                    ws = self._bridge.ws_controller
                    if ws.state == WebSocketState.DEAD:
                        # The lib exhausted its 25 internal reconnect attempts and
                        # gave up permanently — only a fresh initialize() revives
                        # it, and it's certainly been down for many minutes.
                        self._ws_disconnected_since = None
                        await self._force_reconnect(
                            "websocket controller DEAD (lib exhausted its reconnects)",
                            clear_auth_cache=True,
                        )
                        continue
                    if not ws.connected:
                        if self._ws_disconnected_since is None:
                            self._ws_disconnected_since = time.monotonic()
                        else:
                            down_for = time.monotonic() - self._ws_disconnected_since
                            if down_for > WS_DOWN_RECONNECT_S:
                                self._ws_disconnected_since = None
                                await self._force_reconnect(
                                    f"websocket down {down_for:.0f}s "
                                    f"(state={ws.state.name}, threshold {int(WS_DOWN_RECONNECT_S)}s)",
                                    clear_auth_cache=down_for > AUTH_STALE_AFTER_S,
                                )
                                continue
                    else:
                        self._ws_disconnected_since = None

                # (1) Reconcile-liveness.
                age = time.monotonic() - self._last_successful_reconcile_at
                if age > LIVENESS_TIMEOUT_S:
                    await self._force_reconnect(
                        f"no successful reconcile in {age:.0f}s (threshold {int(LIVENESS_TIMEOUT_S)}s)",
                        clear_auth_cache=age > AUTH_STALE_AFTER_S,
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

    async def _opened_closed_recover_close(
        self, sensor_id: str, sensor_name: str
    ) -> None:
        """Poll ADC's per-sensor REST endpoint to recover the true close time
        for an OPENED_CLOSED collapsed event.

        ADC's WS feed delivers OPENED_CLOSED with no duration, but the REST
        `openClosedStatus` field DOES update during the open window (verified
        2026-05-06: 3=Open, 2=Closed). Polling REST every
        OPENED_CLOSED_POLL_INTERVAL_S lets us emit close to HK when the door
        actually closes, instead of guessing with a fixed pulse. After
        OPENED_CLOSED_POLL_TIMEOUT_S we emit a safety close so a stuck-open
        REST never leaves HK pinned to Open.

        We hit the per-sensor URL directly via `bridge.create_request` rather
        than reading `bridge.sensors[id].api_resource.attributes`, because
        the bridge cache is updated by the device_catalogs refresh (every
        RECONCILE_INTERVAL_S) — too coarse for this loop. A direct GET is
        cheap and bypasses any internal caching.
        """
        bridge = self._bridge
        if bridge is None:
            return
        url = f"https://www.alarm.com/web/api/devices/sensors/{sensor_id}"
        started = time.monotonic()
        deadline = started + OPENED_CLOSED_POLL_TIMEOUT_S
        observed_close = False
        while time.monotonic() < deadline:
            try:
                await asyncio.sleep(OPENED_CLOSED_POLL_INTERVAL_S)
            except asyncio.CancelledError:
                raise
            try:
                async with bridge.create_request("get", url) as resp:
                    if resp.status != 200:
                        continue
                    body = await resp.text()
                payload = json.loads(body)
                attrs = (
                    payload.get("data", {}).get("attributes", {})
                    if isinstance(payload, dict)
                    else {}
                )
                ocs = attrs.get("openClosedStatus")
                text = attrs.get("displayStateText")
                if ocs == 2 or text == "Closed":
                    # ADC's REST has settled to Closed. Don't emit yet if we
                    # haven't reached MIN_HOLD — gives a separate WS Closed
                    # event a chance to arrive (which would cancel this task
                    # and let the dispatch handler emit at the real close time).
                    elapsed = time.monotonic() - started
                    if elapsed < OPENED_CLOSED_MIN_HOLD_S:
                        continue
                    _emit_log(
                        "info",
                        f"opened_closed-rest-close: {sensor_name} returned Closed "
                        f"after {elapsed:.1f}s (openClosedStatus={ocs}, display={text!r})",
                    )
                    observed_close = True
                    break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _emit_log(
                    "warn",
                    f"opened_closed-poll error for {sensor_name}: {type(e).__name__}: {e}",
                )

        if not self._subscribed:
            return
        if not observed_close:
            _emit_log(
                "info",
                f"opened_closed-rest-timeout: {sensor_name} REST never returned Closed "
                f"in {OPENED_CLOSED_POLL_TIMEOUT_S:.0f}s; emitting safety close",
            )
        sensor = bridge.sensors.get(sensor_id)
        if sensor is None:
            return
        # Correct the WS-authoritative cache to CLOSED. The open that started this
        # recovery set the cache to OPEN (via a discrete Opened), but the matching
        # close arrived as a merged OPENED_CLOSED that on_raw_event does NOT cache
        # — so without this the cache stays OPEN and the next reconcile re-opens
        # the sensor in HomeKit, flip-flopping it stuck-open (observed 2026-07-12,
        # garage). We've just confirmed REST-Closed (or hit the safety timeout),
        # so CLOSED is the truth; a genuine re-open fires a fresh Opened that
        # re-sets the cache. Motion sensors use IDLE as their "closed" analogue.
        subtype = getattr(sensor.attributes, "device_type", None)
        self._authoritative_sensor_state[sensor_id] = (
            SensorState.IDLE if subtype in MOTION_SUBTYPES else SensorState.CLOSED
        )
        self._sensor_disagree_since.pop(sensor_id, None)
        wire = self._build_sensor_wire_contact(sensor)
        # Force closed: the bridge's cached state may still be OPENED_CLOSED, in
        # which case _derive_closed falls through to the closed default — but we
        # are explicit here in case anything else changes.
        wire["closed"] = True
        if self._known_devices.get(wire["id"]) != wire:
            self._known_devices[wire["id"]] = wire
            _emit_notification("device_updated", {"device": wire})
            _emit_log("info", f"opened_closed-emit-close: {wire.get('name')}")

    async def _run_reconcile(self, reason: str) -> None:
        """Shared implementation — refresh full state and diff/emit any changes.

        Why we don't call bridge.initialize() here: in pyalarmdotcomajax 0.6,
        AlarmBridge.initialize() short-circuits on `self._initialized=True`
        and returns immediately with no I/O. After the first startup call it
        is a no-op, which makes reconcile silently succeed against stale
        in-memory state — the watchdog never fires, and a half-open TCP
        socket goes undetected indefinitely. This bit us on 2026-05-02 when
        the parents' WAN flapped: the WS went zombie, reconcile kept "passing"
        without ever touching the network, and HomeKit reported the alarm as
        disarmed for ~12hrs while it was actually ARMED_STAY.

        The fix has two halves:
          1. is_logged_in(throw=True) — POSTs to alarm.com's keep-alive URL.
             A real HTTP roundtrip that surfaces zombie TCP as TimeoutError.
             On 403 the bridge raises SessionExpired; we re-login transparently
             since alarm.com invalidates sessions after long network gaps.
          2. device_catalogs._refresh() — the upstream controller that fetches
             the full device catalog and fans out included sensors / partitions
             to their respective controllers via subscribed callbacks.
             Calling sensors._refresh() or partitions._refresh() directly
             SHORT-CIRCUITS at controllers/base.py:251 because those controllers
             have `_api_data_provider` set to device_catalogs — they're data
             RECEIVERS, not fetchers. Refreshing the catalog is the only path
             that actually updates `bridge.sensors[].api_resource.attributes`.

             It also requires repopulating `_target_device_ids` before each
             call — see the inline workaround comment below for the
             pyalarmdotcomajax 0.6.x destructive-pop bug we discovered
             2026-05-06.

        The wait_for wrap surfaces a hang as TimeoutError → reconcile raises →
        watchdog sees stale liveness → force_reconnect.
        """
        assert self._bridge is not None
        bridge = self._bridge
        try:
            await asyncio.wait_for(
                bridge.is_logged_in(throw=True), timeout=BRIDGE_CALL_TIMEOUT_S
            )
        except SessionExpired:
            _emit_log(
                "warn", f"reconcile ({reason}): session expired, re-logging in"
            )
            await asyncio.wait_for(bridge.login(), timeout=BRIDGE_CALL_TIMEOUT_S)
        # Workaround for pyalarmdotcomajax 0.6.x bug
        # (controllers/base.py:_refresh, around line 256):
        #
        #     if resource_id or len(self._target_device_ids) == 1:
        #         ids = resource_id or self._target_device_ids.pop()
        #
        # `_target_device_ids.pop()` is destructive. For device_catalogs, the set
        # always contains exactly one element (the active system id) after
        # bridge.fetch_full_state() runs at startup. The first call to _refresh
        # — done by initialize() itself — pops that id and leaves the set empty.
        # Every subsequent _refresh then short-circuits at the
        # `_requires_target_ids and not self._target_device_ids` check at line 251.
        # Result: bridge.sensors[].api_resource.attributes never updates from REST
        # after startup, no matter how many times we call _refresh.
        #
        # Verified on 2026-05-06 with a Slider sustained-open: external REST
        # showed (state=2, ocs=3, display="Open") while the daemon's bridge cache
        # was frozen at the startup-time (state=1, ocs=2, display="Closed").
        #
        # Repopulate the set before each refresh.
        dc = bridge.device_catalogs
        sysid = bridge._available_device_catalogs.active_system_id
        if sysid and sysid not in dc._target_device_ids:
            dc._target_device_ids.add(sysid)
        await asyncio.wait_for(
            dc._refresh(), timeout=BRIDGE_CALL_TIMEOUT_S
        )
        self._last_successful_reconcile_at = time.monotonic()

        # WS-authoritative staleness guard. _build_partition_wire prefers the
        # WS-pushed cache over REST so a lagging / double-firing REST value can't
        # flip arm/disarm (2026-05-09). But if the WS silently dies, that cache
        # freezes and REST — now the only live truth — is suppressed forever
        # (2026-06-02: arm/disarm stuck for ~12h while sensors kept updating).
        # Detect a cached partition value that REST has contradicted for longer
        # than the known REST lag, drop the stale entry so REST drives the wire
        # built just below, and ask the watchdog to rebuild the WS (restoring
        # real-time push + the alarm-trigger path, which is WS-only). The long
        # AUTH_STALE_AFTER_S window means a normal lagging REST value self-heals
        # before it ever trips this, so the anti-flip protection is preserved.
        now = time.monotonic()
        for p in bridge.partitions:
            rid = str(p.id)
            cached = self._authoritative_partition_state.get(rid)
            rest_state = p.attributes.state
            if cached is None or rest_state is None or rest_state == cached:
                self._partition_disagree_since.pop(rid, None)
                continue
            since = self._partition_disagree_since.get(rid)
            if since is None:
                self._partition_disagree_since[rid] = now
            elif now - since > AUTH_STALE_AFTER_S:
                _emit_log(
                    "warn",
                    f"WS-authoritative partition {rid} stale: cache={cached.name} but "
                    f"REST={rest_state.name} for {now - since:.0f}s — dropping cache "
                    "(websocket presumed dead) and forcing WS rebuild",
                )
                self._authoritative_partition_state.pop(rid, None)
                self._partition_disagree_since.pop(rid, None)
                self._ws_rebuild_requested = True

        # Sensor-cache staleness guard (contact sensors) — the sensor analogue of
        # the partition guard above. A contact sensor's WS-authoritative cache can
        # get stuck at OPEN when its close arrives as a merged OPENED_CLOSED event
        # (which on_raw_event does NOT cache) instead of a discrete Closed; the
        # stale OPEN then overrides settled REST on every reconcile and HomeKit
        # stays pinned Open forever (observed 2026-07-12, garage). If the cache
        # disagrees with settled REST for > SENSOR_AUTH_STALE_AFTER_S, drop the
        # stale entry so REST drives the wire built just below. We do NOT trigger a
        # WS rebuild here (unlike partitions) — the socket is healthy; it simply
        # delivered a merged event. Sensors with an in-flight OPENED_CLOSED
        # recovery poll are skipped: that task owns the sensor until it resolves.
        # Scoped to contact sensors (the observed failure); motion (ACTIVE/IDLE)
        # keeps its existing clearing path untouched.
        for s in bridge.sensors:
            if getattr(s.attributes, "device_type", None) not in CONTACT_SUBTYPES:
                continue
            rid = str(s.id)
            cached = self._authoritative_sensor_state.get(rid)
            pending = self._pending_synthetic_close.get(rid)
            if cached is None or (pending is not None and not pending.done()):
                self._sensor_disagree_since.pop(rid, None)
                continue
            cache_closed = cached == SensorState.CLOSED
            rest_closed = _derive_closed(s)
            if cache_closed == rest_closed:
                self._sensor_disagree_since.pop(rid, None)
                continue
            since = self._sensor_disagree_since.get(rid)
            if since is None:
                self._sensor_disagree_since[rid] = now
            elif now - since > SENSOR_AUTH_STALE_AFTER_S:
                _emit_log(
                    "warn",
                    f"WS-authoritative sensor {rid} stale: cache={cached.name} but "
                    f"REST={'Closed' if rest_closed else 'Open'} for {now - since:.0f}s "
                    "— dropping stale cache entry (likely a merged OPENED_CLOSED "
                    "close); REST now drives HomeKit",
                )
                self._authoritative_sensor_state.pop(rid, None)
                self._sensor_disagree_since.pop(rid, None)

        current = {d["id"]: d for d in self._snapshot_devices()}
        changes = 0
        for device_id, wire in current.items():
            # Skip sensors with an in-flight OPENED_CLOSED synthetic-OPEN pulse:
            # _opened_closed_recover_close owns this sensor's wire until it
            # observes REST-Closed (or its safety timeout fires) and emits the
            # eventual close itself. Without this guard, reconcile fetched fresh
            # REST data (which always says Closed for collapsed cycles, since
            # the open is too brief for REST to ever flip), saw _known_devices
            # at synthetic closed=False, called it "drift", and emitted closed
            # within 0-5s — wiping out the entire OPEN window in HK so neither
            # users nor automations could see it. Verified 2026-05-09 from the
            # log: 37 of 50 force-opens were stomped this way.
            pending = self._pending_synthetic_close.get(device_id)
            if pending is not None and not pending.done():
                continue
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


def _debug_force_stall_handler() -> None:
    """SIGUSR1 handler that blocks the asyncio main thread with a synchronous
    sleep, simulating the failure mode the OS-thread stall watchdog is designed
    to catch (a half-open WS scenario where pyalarmdotcomajax's WS recv path
    makes a sync-blocking call). After ~60s of no asyncio heartbeat updates,
    the stall thread should fire os._exit(1) and the Node side should respawn.

    Only installed when --enable-debug-rpc is passed. Trigger from the Pi:
        sudo kill -USR1 $(pgrep -f alarm-dot-com.*daemon.py)
    """
    _emit_log(
        "warn",
        "DEBUG: SIGUSR1 received — blocking asyncio main thread for 120s to force a wedge "
        "(stall thread should fire os._exit within STALL_THRESHOLD_S after heartbeat goes stale)",
    )
    sys.stderr.write("DEBUG: forcing asyncio stall via time.sleep(120) on main thread\n")
    sys.stderr.flush()
    time.sleep(120)
    # Reaching this line means the stall thread did NOT fire — that's a bug.
    _emit_log(
        "error",
        "DEBUG: time.sleep(120) returned — stall thread DID NOT fire as expected (bug in stall watchdog)",
    )


async def main_async(log_level: str, enable_debug_rpc: bool) -> None:
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

    if enable_debug_rpc:
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGUSR1, _debug_force_stall_handler)
        _emit_log(
            "info",
            "debug-rpc enabled: SIGUSR1 will trigger a forced asyncio stall (test the stall watchdog)",
        )

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
    parser.add_argument(
        "--enable-debug-rpc",
        action="store_true",
        help="Enable debug signal handlers for testing the stall watchdog "
        "(SIGUSR1 forces an asyncio loop wedge). Off by default — only "
        "enable transiently for testing, never in steady-state.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args.log_level, args.enable_debug_rpc))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
