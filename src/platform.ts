import { join as pathJoin } from 'path';

import {
  API,
  Characteristic,
  DynamicPlatformPlugin,
  Logger,
  PlatformAccessory,
  PlatformConfig,
  Service,
} from 'homebridge';

import { Bootstrap } from './bootstrap.js';
import { PythonBridge } from './python-bridge.js';
import { PLATFORM_NAME, PLUGIN_NAME } from './settings.js';
import { SecurityPanelAccessory } from './accessories/panel.js';
import { ContactSensorAccessory } from './accessories/contact-sensor.js';
import { MotionSensorAccessory } from './accessories/motion-sensor.js';
import type {
  ContactSensorDevice,
  Device,
  DevicesEnumeratedParams,
  DeviceUpdatedParams,
  LogParams,
  MotionSensorDevice,
  PanelDevice,
  PluginConfig,
  RpcNotification,
} from './types.js';

type AccessoryHandler = SecurityPanelAccessory | ContactSensorAccessory | MotionSensorAccessory;

/**
 * Homebridge DynamicPlatformPlugin entry point.
 *
 * Lifecycle:
 *   1. Constructor — receive config + cached accessories from Homebridge.
 *   2. `didFinishLaunching` — bootstrap Python venv, spawn daemon, authenticate, enumerate devices.
 *   3. Ongoing — receive device-update notifications and route them to the right accessory handler.
 *   4. Shutdown — stop the daemon cleanly when Homebridge exits.
 *
 * Cached accessories are matched to daemon-reported devices by UUID (derived from the
 * Alarm.com device ID via `api.hap.uuid.generate`). Devices that no longer exist are
 * unregistered; new devices are added and registered with HomeKit.
 */
export class AlarmDotComV2Platform implements DynamicPlatformPlugin {
  public readonly Service: typeof Service;
  public readonly Characteristic: typeof Characteristic;
  public readonly pluginConfig: PluginConfig;

  /** Accessories restored from Homebridge's cache on startup. We re-attach handlers to them. */
  private readonly cachedAccessories: PlatformAccessory[] = [];
  /** Active accessory handlers, keyed by Alarm.com device ID. */
  private readonly handlers = new Map<string, AccessoryHandler>();

  private bridge: PythonBridge | null = null;
  private shuttingDown = false;
  private venvPython: string | null = null;
  private daemonScript: string | null = null;
  /**
   * Recent respawn timestamps (epoch ms). Used to throttle auto-respawn so a
   * persistent failure (e.g. bad credentials) doesn't spin into a tight loop.
   * Bounded to RESPAWN_WINDOW_MS — older entries are discarded as we go.
   */
  private respawnTimes: number[] = [];
  private static readonly RESPAWN_WINDOW_MS = 5 * 60 * 1000; // 5 min
  private static readonly MAX_RESPAWNS_PER_WINDOW = 10;
  private static readonly RESPAWN_BACKOFF_MS = 2000;

  constructor(
    public readonly log: Logger,
    config: PlatformConfig,
    public readonly api: API,
  ) {
    this.Service = api.hap.Service;
    this.Characteristic = api.hap.Characteristic;
    this.pluginConfig = config as unknown as PluginConfig;

    this.log.debug(`${PLATFORM_NAME} platform loaded, config name=${this.pluginConfig.name}`);

    this.api.on('didFinishLaunching', () => {
      void this.start();
    });
    this.api.on('shutdown', () => {
      void this.stop();
    });
  }

  /**
   * Called by Homebridge when it restores an accessory from cache.
   * We simply stash it; handler wiring happens during `start()` after we know which devices exist.
   */
  configureAccessory(accessory: PlatformAccessory): void {
    this.log.debug(`[platform] restored cached accessory: ${accessory.displayName}`);
    this.cachedAccessories.push(accessory);
  }

  /** Kick off bootstrap + daemon connection. */
  private async start(): Promise<void> {
    try {
      if (!this.pluginConfig.username || !this.pluginConfig.password) {
        this.log.error(
          '[platform] username and password are required in the plugin config. Aborting startup.',
        );
        return;
      }

      const bootstrap = new Bootstrap(this.api.user.storagePath(), this.log);
      this.venvPython = await bootstrap.ensureReady(this.pluginConfig.pythonPath);
      this.log.info(`[platform] python venv ready at ${this.venvPython}`);
      this.daemonScript = pathJoin(__dirname, '..', 'python', 'daemon.py');

      await this.spawnAndConnect();
    } catch (err) {
      this.log.error(
        `[platform] startup failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  /**
   * Spawn the daemon, register listeners, and run the login → enumerate →
   * subscribe sequence. Used for both initial startup and auto-respawn after
   * an unexpected daemon exit (e.g. when the OS-thread stall watchdog inside
   * the daemon fires `os._exit(1)` after detecting an asyncio loop wedge).
   */
  private async spawnAndConnect(): Promise<void> {
    if (!this.venvPython || !this.daemonScript) {
      throw new Error('venvPython/daemonScript not initialized — start() must run first');
    }

    const daemonArgs = ['--log-level', this.pluginConfig.logLevel ?? 'info'];
    if (this.pluginConfig.debugRpc) {
      daemonArgs.push('--enable-debug-rpc');
      this.log.warn(
        '[platform] debugRpc enabled — daemon will install SIGUSR1 handler that forces a ' +
          'stall to test the watchdog. Disable in steady-state.',
      );
    }

    this.bridge = new PythonBridge(
      this.venvPython,
      this.daemonScript,
      daemonArgs,
      this.log,
    );

    this.bridge.on('notification', (notif: RpcNotification) => this.onNotification(notif));
    this.bridge.on('exit', ({ code, signal }: { code: number | null; signal: string | null }) => {
      if (this.shuttingDown) return;
      this.log.warn(
        `[platform] daemon exited unexpectedly (code=${code}, signal=${signal}); attempting respawn`,
      );
      void this.scheduleRespawn();
    });

    this.bridge.start();

    // Blocking login — daemon handles 2FA, session persistence, etc.
    await this.bridge.call('login', {
      username: this.pluginConfig.username,
      password: this.pluginConfig.password,
      mfaCookie: this.pluginConfig.mfaCookie,
    });
    this.log.info('[platform] Alarm.com login successful');

    const enumerated = await this.bridge.call<{ devices: Device[] }>('enumerate_devices', {
      include_security_panel: this.pluginConfig.exposeSecurityPanel ?? true,
      include_contact_sensors: this.pluginConfig.exposeContactSensors ?? true,
      include_motion_sensors: this.pluginConfig.exposeMotionSensors ?? true,
    });
    this.syncDevices(enumerated.devices);

    await this.bridge.call('subscribe_updates');
    this.log.info(
      `[platform] subscribed to events; ${this.handlers.size} accessor${this.handlers.size === 1 ? 'y' : 'ies'} active`,
    );
  }

  /**
   * Throttled respawn after unexpected daemon exit. Backs off briefly to let
   * any transient kernel-level cleanup settle, then re-runs the spawn flow.
   * Bails out if we've respawned too many times in a short window — typically
   * a sign of a permanent error (bad credentials, etc.) rather than a
   * recoverable wedge.
   */
  private async scheduleRespawn(): Promise<void> {
    if (this.shuttingDown) return;

    const now = Date.now();
    this.respawnTimes = this.respawnTimes.filter(
      (t) => now - t < AlarmDotComV2Platform.RESPAWN_WINDOW_MS,
    );
    if (this.respawnTimes.length >= AlarmDotComV2Platform.MAX_RESPAWNS_PER_WINDOW) {
      this.log.error(
        `[platform] daemon respawned ${AlarmDotComV2Platform.MAX_RESPAWNS_PER_WINDOW} times in ` +
          `${AlarmDotComV2Platform.RESPAWN_WINDOW_MS / 1000}s — likely a permanent failure ` +
          `(e.g. bad credentials). Giving up; restart Homebridge to retry.`,
      );
      return;
    }
    this.respawnTimes.push(now);

    await new Promise((resolve) =>
      setTimeout(resolve, AlarmDotComV2Platform.RESPAWN_BACKOFF_MS),
    );
    if (this.shuttingDown) return;

    try {
      await this.spawnAndConnect();
      this.log.info('[platform] daemon respawned successfully');
    } catch (err) {
      this.log.error(
        `[platform] respawn failed: ${err instanceof Error ? err.message : String(err)}; ` +
          `will retry on next exit if any`,
      );
    }
  }

  private async stop(): Promise<void> {
    this.shuttingDown = true;
    if (this.bridge) {
      await this.bridge.stop();
      this.bridge = null;
    }
  }

  /**
   * Reconcile daemon-reported devices against cached accessories: register new, update existing,
   * unregister obsolete.
   */
  private syncDevices(devices: Device[]): void {
    const seenIds = new Set<string>();

    for (const device of devices) {
      seenIds.add(device.id);

      // Respawn-idempotency: if a handler already exists for this device id
      // (because we're re-running enumerate after a daemon respawn), reuse
      // it. The accessory + HomeKit registration are still alive — we just
      // need to push the latest state through.
      const alreadyAttached = this.handlers.get(device.id);
      if (alreadyAttached) {
        this.applyUpdate(alreadyAttached, device);
        continue;
      }

      const uuid = this.api.hap.uuid.generate(`${PLUGIN_NAME}::${device.id}`);
      const existing = this.cachedAccessories.find((a) => a.UUID === uuid);

      if (existing) {
        const handler = this.attachHandler(existing, device);
        if (handler) {
          this.handlers.set(device.id, handler);
          this.applyUpdate(handler, device);
        }
      } else {
        const accessory = new this.api.platformAccessory(device.name, uuid);
        accessory.context.deviceId = device.id;
        accessory.context.kind = device.kind;
        const handler = this.attachHandler(accessory, device);
        if (handler) {
          this.handlers.set(device.id, handler);
          this.applyUpdate(handler, device);
          this.api.registerPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, [accessory]);
          this.log.info(`[platform] added new ${device.kind}: ${device.name}`);
        }
      }
    }

    // Unregister any cached accessories no longer reported by the daemon.
    const stale = this.cachedAccessories.filter((a) => {
      const id = a.context?.deviceId as string | undefined;
      return id && !seenIds.has(id);
    });
    if (stale.length > 0) {
      for (const a of stale) {
        this.log.info(`[platform] removing stale accessory: ${a.displayName}`);
      }
      this.api.unregisterPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, stale);
    }
  }

  /**
   * Narrowing helper. Each handler class's `update()` takes a specific Device
   * subtype; TypeScript can't automatically pair them by `kind`, so we do it here.
   */
  private applyUpdate(handler: AccessoryHandler, device: Device): void {
    if (handler instanceof SecurityPanelAccessory && device.kind === 'panel') {
      handler.update(device);
    } else if (handler instanceof ContactSensorAccessory && device.kind === 'contact_sensor') {
      handler.update(device);
    } else if (handler instanceof MotionSensorAccessory && device.kind === 'motion_sensor') {
      handler.update(device);
    } else {
      this.log.warn(
        `[platform] handler/device kind mismatch for ${device.id}: ` +
          `handler=${handler.constructor.name}, device.kind=${device.kind}`,
      );
    }
  }

  private attachHandler(
    accessory: PlatformAccessory,
    device: Device,
  ): AccessoryHandler | null {
    switch (device.kind) {
      case 'panel':
        return new SecurityPanelAccessory(this, accessory, device as PanelDevice, (action) =>
          this.requestPanelAction(device.id, action),
        );
      case 'contact_sensor':
        return new ContactSensorAccessory(this, accessory, device as ContactSensorDevice);
      case 'motion_sensor':
        return new MotionSensorAccessory(this, accessory, device as MotionSensorDevice);
      default:
        this.log.warn(`[platform] unknown device kind: ${(device as Device).kind}`);
        return null;
    }
  }

  private async requestPanelAction(
    deviceId: string,
    action: 'arm_stay' | 'arm_away' | 'arm_night' | 'disarm',
  ): Promise<void> {
    if (!this.bridge) throw new Error('daemon not running');
    await this.bridge.call('panel_action', {
      device_id: deviceId,
      action,
      bypass_zones: this.pluginConfig.armAwayKeypadBypass ?? false,
    });
  }

  private onNotification(notif: RpcNotification): void {
    switch (notif.method) {
      case 'device_updated': {
        const params = notif.params as unknown as DeviceUpdatedParams;
        const handler = this.handlers.get(params.device.id);
        if (handler) {
          this.applyUpdate(handler, params.device);
        }
        break;
      }
      case 'devices_enumerated': {
        // Daemon may resend the list after reconnection.
        const params = notif.params as unknown as DevicesEnumeratedParams;
        this.syncDevices(params.devices);
        break;
      }
      case 'log': {
        const { level, message } = notif.params as unknown as LogParams;
        const line = `[python-daemon] ${message}`;
        switch (level) {
          case 'error':
            this.log.error(line);
            break;
          case 'warn':
            this.log.warn(line);
            break;
          case 'info':
            this.log.info(line);
            break;
          default:
            this.log.debug(line);
        }
        break;
      }
      default:
        this.log.debug(`[platform] unhandled notification: ${notif.method}`);
    }
  }
}
