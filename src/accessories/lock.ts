import { CharacteristicValue, PlatformAccessory } from 'homebridge';

import type { AlarmDotComV2Platform } from '../platform.js';
import type { LockDevice } from '../types.js';

type LockAction = 'lock' | 'unlock';

/**
 * Lock accessory. Maps Alarm.com's LOCKED/UNLOCKED/UNKNOWN/HIDDEN to HomeKit's
 * LockCurrentState (SECURED=0, UNSECURED=1, JAMMED=2, UNKNOWN=3) and
 * LockTargetState (SECURED=0, UNSECURED=1).
 *
 * ADC has no jam signal — we never report JAMMED. UNKNOWN/HIDDEN map to
 * UNKNOWN current-state so HomeKit shows "Unavailable" rather than guessing.
 */
export class LockAccessory {
  private readonly service;
  private readonly batteryService;
  private lastDevice: LockDevice;

  constructor(
    private readonly platform: AlarmDotComV2Platform,
    private readonly accessory: PlatformAccessory,
    initial: LockDevice,
    private readonly onAction: (action: LockAction) => Promise<void>,
  ) {
    this.lastDevice = initial;

    this.service =
      accessory.getService(platform.Service.LockMechanism) ??
      accessory.addService(platform.Service.LockMechanism, initial.name);

    this.service.setCharacteristic(platform.Characteristic.Name, initial.name);

    this.service
      .getCharacteristic(platform.Characteristic.LockCurrentState)
      .onGet(() => this.toCurrentState(this.lastDevice));

    this.service
      .getCharacteristic(platform.Characteristic.LockTargetState)
      .onGet(() => this.toTargetState(this.lastDevice))
      .onSet((value) => this.handleTargetSet(value));

    if (initial.lowBattery !== undefined) {
      this.batteryService =
        accessory.getService(platform.Service.Battery) ??
        accessory.addService(platform.Service.Battery, `${initial.name} Battery`);
      this.batteryService
        .getCharacteristic(platform.Characteristic.StatusLowBattery)
        .onGet(() =>
          this.lastDevice.lowBattery
            ? platform.Characteristic.StatusLowBattery.BATTERY_LEVEL_LOW
            : platform.Characteristic.StatusLowBattery.BATTERY_LEVEL_NORMAL,
        );
    } else {
      this.batteryService = null;
    }

    this.ensureInfoService(initial);
  }

  update(device: LockDevice): void {
    this.lastDevice = device;
    this.service.updateCharacteristic(
      this.platform.Characteristic.LockCurrentState,
      this.toCurrentState(device),
    );
    this.service.updateCharacteristic(
      this.platform.Characteristic.LockTargetState,
      this.toTargetState(device),
    );
    if (this.batteryService && device.lowBattery !== undefined) {
      this.batteryService.updateCharacteristic(
        this.platform.Characteristic.StatusLowBattery,
        device.lowBattery
          ? this.platform.Characteristic.StatusLowBattery.BATTERY_LEVEL_LOW
          : this.platform.Characteristic.StatusLowBattery.BATTERY_LEVEL_NORMAL,
      );
    }
  }

  private async handleTargetSet(value: CharacteristicValue): Promise<void> {
    const T = this.platform.Characteristic.LockTargetState;
    const action: LockAction = Number(value) === T.SECURED ? 'lock' : 'unlock';
    this.platform.log.info(
      `[lock:${this.lastDevice.name}] target requested: ${action} (HomeKit value ${value})`,
    );
    try {
      await this.onAction(action);
    } catch (err) {
      this.platform.log.error(
        `[lock:${this.lastDevice.name}] ${action} failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      // Revert the target characteristic to actual state so HomeKit doesn't
      // get stuck showing a pending change that never lands.
      this.service.updateCharacteristic(
        this.platform.Characteristic.LockTargetState,
        this.toTargetState(this.lastDevice),
      );
    }
  }

  private toCurrentState(device: LockDevice): number {
    const C = this.platform.Characteristic.LockCurrentState;
    if (device.unknown) return C.UNKNOWN;
    return device.locked ? C.SECURED : C.UNSECURED;
  }

  private toTargetState(device: LockDevice): number {
    const T = this.platform.Characteristic.LockTargetState;
    // Target has no UNKNOWN; default to last-known position so HomeKit doesn't
    // immediately fire a target-change at restore time.
    return device.locked ? T.SECURED : T.UNSECURED;
  }

  private ensureInfoService(device: LockDevice): void {
    const info =
      this.accessory.getService(this.platform.Service.AccessoryInformation) ??
      this.accessory.addService(this.platform.Service.AccessoryInformation);
    info
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Alarm.com')
      .setCharacteristic(this.platform.Characteristic.Model, 'Lock')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, device.id);
  }
}
