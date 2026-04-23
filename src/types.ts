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
}

/** Top-level device categories the plugin exposes. Keep in sync with daemon.py. */
export type DeviceKind = 'panel' | 'contact_sensor' | 'motion_sensor';

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

export type Device = PanelDevice | ContactSensorDevice | MotionSensorDevice;

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
