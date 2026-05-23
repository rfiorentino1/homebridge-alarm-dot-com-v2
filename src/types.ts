/**
 * Shared TypeScript types. The shape of these mirrors what the Python daemon
 * serialises on the wire (see python/daemon.py).
 */

export interface PluginConfig {
  name?: string;
  username: string;
  password: string;
  mfaCookie?: string;
  exposeSecurityPanel?: boolean;
  exposeContactSensors?: boolean;
  exposeMotionSensors?: boolean;
  armAwayKeypadBypass?: boolean;
  pythonPath?: string;
  logLevel?: 'error' | 'warn' | 'info' | 'debug' | 'trace';
  /**
   * Enable debug signal handlers for testing the stall watchdog. When true,
   * the daemon installs a SIGUSR1 handler that synchronously blocks the
   * asyncio main thread for 120s — simulating an asyncio loop wedge so the
   * OS-thread stall watchdog can be verified end-to-end. Off by default;
   * enable transiently for testing, never in steady-state.
   */
  debugRpc?: boolean;
}

/** Top-level device categories the plugin exposes. Keep in sync with daemon.py. */
export type DeviceKind =
  | 'panel'
  | 'contact_sensor'
  | 'motion_sensor'
  | 'lock'
  | 'light'
  | 'thermostat'
  | 'garage_door'
  | 'gate'
  | 'water_sensor'
  | 'water_valve';

/** Thermostat operating mode, as reported by the daemon. */
export type ThermostatMode = 'off' | 'heat' | 'cool' | 'auto' | 'unknown';

/** Security-panel arming states from Alarm.com. Strings are exactly what the API returns. */
export type PanelState =
  | 'disarmed'
  | 'armed_stay'
  | 'armed_away'
  | 'armed_night'
  | 'unknown'
  | 'triggered';

export interface PanelDevice {
  kind: 'panel';
  id: string;
  name: string;
  state: PanelState;
  /** True when one or more sensors are currently in a faulted/open state. */
  hasOpenZones: boolean;
}

export interface ContactSensorDevice {
  kind: 'contact_sensor';
  id: string;
  name: string;
  /** `true` = closed/idle, `false` = open/alert. Matches Alarm.com's "closed/open" semantics. */
  closed: boolean;
  /** Battery state, if the sensor reports it. */
  lowBattery?: boolean;
}

export interface MotionSensorDevice {
  kind: 'motion_sensor';
  id: string;
  name: string;
  /** `true` = motion detected. */
  motion: boolean;
  lowBattery?: boolean;
}

export interface LockDevice {
  kind: 'lock';
  id: string;
  name: string;
  /** `true` = currently locked. */
  locked: boolean;
  /** `true` = state is UNKNOWN/HIDDEN; HomeKit should show Unknown rather than guessing. */
  unknown: boolean;
  lowBattery?: boolean;
}

export interface LightDevice {
  kind: 'light';
  id: string;
  name: string;
  /** Power state. */
  on: boolean;
  /** Whether the light is a dimmer; only then is `brightness` meaningful. */
  dimmer: boolean;
  /** 0..100. Only present when `dimmer=true`. */
  brightness?: number;
  lowBattery?: boolean;
}

export interface ThermostatDevice {
  kind: 'thermostat';
  id: string;
  name: string;
  /** Current operating mode (target heating/cooling state, in HomeKit terms). */
  mode: ThermostatMode;
  supportsAuto: boolean;
  supportsHeat: boolean;
  supportsCool: boolean;
  supportsOff: boolean;
  /** Identity.use_celsius — controls HomeKit display unit, not value semantics. */
  usesCelsius: boolean;
  /** All temperatures below are in °C — HomeKit's internal unit. */
  currentTempC: number | null;
  heatSetpointC: number | null;
  coolSetpointC: number | null;
  minHeatC: number | null;
  maxHeatC: number | null;
  minCoolC: number | null;
  maxCoolC: number | null;
  /** 0..100 if the thermostat reports humidity. */
  humidity?: number;
}

export interface GarageDoorDevice {
  kind: 'garage_door';
  id: string;
  name: string;
  open: boolean;
  closed: boolean;
}

export interface GateDevice {
  kind: 'gate';
  id: string;
  name: string;
  open: boolean;
  closed: boolean;
  /** Some gates only support remote OPEN (for safety). HomeKit Close will fail if false. */
  supportsRemoteClose: boolean;
}

export interface WaterSensorDevice {
  kind: 'water_sensor';
  id: string;
  name: string;
  /** `true` = leak detected. */
  leak: boolean;
  lowBattery?: boolean;
}

export interface WaterValveDevice {
  kind: 'water_valve';
  id: string;
  name: string;
  /** Valve open = active/in-use in HomeKit. */
  open: boolean;
  closed: boolean;
}

export type Device =
  | PanelDevice
  | ContactSensorDevice
  | MotionSensorDevice
  | LockDevice
  | LightDevice
  | ThermostatDevice
  | GarageDoorDevice
  | GateDevice
  | WaterSensorDevice
  | WaterValveDevice;

/**
 * Generic device-action grammar (matches `device_action` in daemon.py).
 * See the daemon docstring for action/value semantics per kind.
 */
export interface DeviceActionRequest {
  device_id: string;
  kind: Exclude<DeviceKind, 'panel' | 'contact_sensor' | 'motion_sensor' | 'water_sensor'>;
  action: string;
  value?: string | number | boolean | null;
}

/**
 * JSON-RPC 2.0 types for talking to the Python daemon.
 * We use strings for IDs so they stay distinct from any integer IDs the Python side might produce.
 */
export interface RpcRequest {
  jsonrpc: '2.0';
  id: string;
  method: string;
  params?: Record<string, unknown>;
}

export interface RpcResponseOk<T = unknown> {
  jsonrpc: '2.0';
  id: string;
  result: T;
}

export interface RpcResponseErr {
  jsonrpc: '2.0';
  id: string;
  error: { code: number; message: string; data?: unknown };
}

export type RpcResponse<T = unknown> = RpcResponseOk<T> | RpcResponseErr;

/** Notifications arrive unsolicited from the daemon (no `id`). */
export interface RpcNotification {
  jsonrpc: '2.0';
  method: string;
  params?: Record<string, unknown>;
}

export type RpcIncoming = RpcResponse | RpcNotification;

/**
 * Daemon notification payloads. `method` is the notification name; the interfaces below
 * describe the expected `params` shape for each.
 */
export interface DeviceUpdatedParams {
  device: Device;
}

export interface DevicesEnumeratedParams {
  devices: Device[];
}

export interface LogParams {
  level: 'error' | 'warn' | 'info' | 'debug' | 'trace';
  message: string;
}
