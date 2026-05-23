import { CharacteristicValue, PlatformAccessory } from 'homebridge';

import type { AlarmDotComV2Platform } from '../platform.js';
import type { GarageDoorDevice } from '../types.js';

type GarageDoorAction = 'open' | 'close';

/**
 * Garage door accessory. HomeKit's GarageDoorOpener has:
 *   CurrentDoorState: OPEN=0, CLOSED=1, OPENING=2, CLOSING=3, STOPPED=4
 *   TargetDoorState : OPEN=0, CLOSED=1
 *
 * ADC reports OPEN / CLOSED / UNKNOWN — we don't get OPENING/CLOSING in-flight
 * signals, so we surface OPEN or CLOSED only. Once we see a fresh state we
 * propagate it; if state is UNKNOWN we don't update CurrentDoorState (HomeKit
 * will keep the last known value).
 *
 * Note: HomeKit requires that TargetDoorState changes are mirrored back into
 * CurrentDoorState. ADC updates flow back through the daemon, so the eventual-
 * consistency loop closes naturally.
 */
export class GarageDoorAccessory {
  private readonly service;
  private lastDevice: GarageDoorDevice;

  constructor(
    private readonly platform: AlarmDotComV2Platform,
    private readonly accessory: PlatformAccessory,
    initial: GarageDoorDevice,
    private readonly onAction: (action: GarageDoorAction) => Promise<void>,
  ) {
    this.lastDevice = initial;

    this.service =
      accessory.getService(platform.Service.GarageDoorOpener) ??
      accessory.addService(platform.Service.GarageDoorOpener, initial.name);

    this.service.setCharacteristic(platform.Characteristic.Name, initial.name);

    this.service
      .getCharacteristic(platform.Characteristic.CurrentDoorState)
      .onGet(() => this.toCurrentState(this.lastDevice));

    this.service
      .getCharacteristic(platform.Characteristic.TargetDoorState)
      .onGet(() => this.toTargetState(this.lastDevice))
      .onSet((value) => this.handleTargetSet(value));

    // ObstructionDetected is required by GarageDoorOpener. ADC doesn't expose
    // an obstruction signal, so it's always false.
    this.service
      .getCharacteristic(platform.Characteristic.ObstructionDetected)
      .onGet(() => false);

    this.ensureInfoService(initial);
  }

  update(device: GarageDoorDevice): void {
    this.lastDevice = device;
    if (device.open || device.closed) {
      this.service.updateCharacteristic(
        this.platform.Characteristic.CurrentDoorState,
        this.toCurrentState(device),
      );
      this.service.updateCharacteristic(
        this.platform.Characteristic.TargetDoorState,
        this.toTargetState(device),
      );
    }
  }

  private async handleTargetSet(value: CharacteristicValue): Promise<void> {
    const T = this.platform.Characteristic.TargetDoorState;
    const action: GarageDoorAction = Number(value) === T.OPEN ? 'open' : 'close';
    this.platform.log.info(
      `[garage:${this.lastDevice.name}] target requested: ${action} (HomeKit value ${value})`,
    );
    try {
      await this.onAction(action);
    } catch (err) {
      this.platform.log.error(
        `[garage:${this.lastDevice.name}] ${action} failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      this.service.updateCharacteristic(
        this.platform.Characteristic.TargetDoorState,
        this.toTargetState(this.lastDevice),
      );
    }
  }

  private toCurrentState(device: GarageDoorDevice): number {
    const C = this.platform.Characteristic.CurrentDoorState;
    if (device.open) return C.OPEN;
    if (device.closed) return C.CLOSED;
    return C.STOPPED;
  }

  private toTargetState(device: GarageDoorDevice): number {
    const T = this.platform.Characteristic.TargetDoorState;
    return device.closed ? T.CLOSED : T.OPEN;
  }

  private ensureInfoService(device: GarageDoorDevice): void {
    const info =
      this.accessory.getService(this.platform.Service.AccessoryInformation) ??
      this.accessory.addService(this.platform.Service.AccessoryInformation);
    info
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Alarm.com')
      .setCharacteristic(this.platform.Characteristic.Model, 'Garage Door')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, device.id);
  }
}
