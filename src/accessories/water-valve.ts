import { CharacteristicValue, PlatformAccessory } from 'homebridge';

import type { AlarmDotComV2Platform } from '../platform.js';
import type { WaterValveDevice } from '../types.js';

type ValveAction = 'open' | 'close';

/**
 * Water shut-off valve. HomeKit's Valve service:
 *   Active: INACTIVE=0, ACTIVE=1     ← target state, writable
 *   InUse : NOT_IN_USE=0, IN_USE=1   ← current state, read-only (we mirror Active)
 *   ValveType: GENERIC=0, IRRIGATION=1, SHOWER_HEAD=2, WATER_FAUCET=3
 *
 * "Active" semantics for HomeKit Valve = water flowing. ADC's WaterValve is a
 * shut-off (open = water can flow, closed = stopped). So open ↔ Active=1.
 *
 * We default ValveType=GENERIC. The user can re-label in Home app if desired.
 */
export class WaterValveAccessory {
  private readonly service;
  private lastDevice: WaterValveDevice;

  constructor(
    private readonly platform: AlarmDotComV2Platform,
    private readonly accessory: PlatformAccessory,
    initial: WaterValveDevice,
    private readonly onAction: (action: ValveAction) => Promise<void>,
  ) {
    this.lastDevice = initial;

    this.service =
      accessory.getService(platform.Service.Valve) ??
      accessory.addService(platform.Service.Valve, initial.name);

    this.service.setCharacteristic(platform.Characteristic.Name, initial.name);
    this.service.setCharacteristic(
      platform.Characteristic.ValveType,
      platform.Characteristic.ValveType.GENERIC_VALVE,
    );

    this.service
      .getCharacteristic(platform.Characteristic.Active)
      .onGet(() => this.toActive(this.lastDevice))
      .onSet((value) => this.handleActiveSet(value));

    this.service
      .getCharacteristic(platform.Characteristic.InUse)
      .onGet(() => this.toInUse(this.lastDevice));

    this.ensureInfoService(initial);
  }

  update(device: WaterValveDevice): void {
    this.lastDevice = device;
    this.service.updateCharacteristic(this.platform.Characteristic.Active, this.toActive(device));
    this.service.updateCharacteristic(this.platform.Characteristic.InUse, this.toInUse(device));
  }

  private async handleActiveSet(value: CharacteristicValue): Promise<void> {
    const A = this.platform.Characteristic.Active;
    const action: ValveAction = Number(value) === A.ACTIVE ? 'open' : 'close';
    this.platform.log.info(
      `[valve:${this.lastDevice.name}] active requested: ${action} (HomeKit value ${value})`,
    );
    try {
      await this.onAction(action);
    } catch (err) {
      this.platform.log.error(
        `[valve:${this.lastDevice.name}] ${action} failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      this.service.updateCharacteristic(
        this.platform.Characteristic.Active,
        this.toActive(this.lastDevice),
      );
    }
  }

  private toActive(device: WaterValveDevice): number {
    const A = this.platform.Characteristic.Active;
    return device.open ? A.ACTIVE : A.INACTIVE;
  }

  private toInUse(device: WaterValveDevice): number {
    const I = this.platform.Characteristic.InUse;
    return device.open ? I.IN_USE : I.NOT_IN_USE;
  }

  private ensureInfoService(device: WaterValveDevice): void {
    const info =
      this.accessory.getService(this.platform.Service.AccessoryInformation) ??
      this.accessory.addService(this.platform.Service.AccessoryInformation);
    info
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Alarm.com')
      .setCharacteristic(this.platform.Characteristic.Model, 'Water Valve')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, device.id);
  }
}
