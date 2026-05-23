import { CharacteristicValue, PlatformAccessory } from 'homebridge';

import type { AlarmDotComV2Platform } from '../platform.js';
import type { GateDevice } from '../types.js';

type GateAction = 'open' | 'close';

/**
 * Gate accessory. HomeKit has no dedicated "gate" service — GarageDoorOpener
 * is the closest model (open/closed, with target-state writes). We reuse it.
 *
 * Many ADC gates only support remote OPEN (for safety / liability reasons).
 * When `supportsRemoteClose` is false, a Close write from HomeKit will be
 * rejected by the daemon. We surface this by refusing the write client-side
 * too, with a log line, rather than silently dropping it.
 */
export class GateAccessory {
  private readonly service;
  private lastDevice: GateDevice;

  constructor(
    private readonly platform: AlarmDotComV2Platform,
    private readonly accessory: PlatformAccessory,
    initial: GateDevice,
    private readonly onAction: (action: GateAction) => Promise<void>,
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

    this.service
      .getCharacteristic(platform.Characteristic.ObstructionDetected)
      .onGet(() => false);

    this.ensureInfoService(initial);
  }

  update(device: GateDevice): void {
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
    const action: GateAction = Number(value) === T.OPEN ? 'open' : 'close';

    if (action === 'close' && !this.lastDevice.supportsRemoteClose) {
      this.platform.log.warn(
        `[gate:${this.lastDevice.name}] remote close not supported by ADC for this device; ignoring`,
      );
      this.service.updateCharacteristic(
        this.platform.Characteristic.TargetDoorState,
        this.toTargetState(this.lastDevice),
      );
      return;
    }

    this.platform.log.info(
      `[gate:${this.lastDevice.name}] target requested: ${action} (HomeKit value ${value})`,
    );
    try {
      await this.onAction(action);
    } catch (err) {
      this.platform.log.error(
        `[gate:${this.lastDevice.name}] ${action} failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      this.service.updateCharacteristic(
        this.platform.Characteristic.TargetDoorState,
        this.toTargetState(this.lastDevice),
      );
    }
  }

  private toCurrentState(device: GateDevice): number {
    const C = this.platform.Characteristic.CurrentDoorState;
    if (device.open) return C.OPEN;
    if (device.closed) return C.CLOSED;
    return C.STOPPED;
  }

  private toTargetState(device: GateDevice): number {
    const T = this.platform.Characteristic.TargetDoorState;
    return device.closed ? T.CLOSED : T.OPEN;
  }

  private ensureInfoService(device: GateDevice): void {
    const info =
      this.accessory.getService(this.platform.Service.AccessoryInformation) ??
      this.accessory.addService(this.platform.Service.AccessoryInformation);
    info
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Alarm.com')
      .setCharacteristic(this.platform.Characteristic.Model, 'Gate')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, device.id);
  }
}
