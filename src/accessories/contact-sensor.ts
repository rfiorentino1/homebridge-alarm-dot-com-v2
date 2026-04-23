import { PlatformAccessory } from 'homebridge';

import type { AlarmDotComV2Platform } from '../platform.js';
import type { ContactSensorDevice } from '../types.js';

/**
 * Contact sensor (door/window). Maps Alarm.com's "closed/open" to HomeKit's
 * ContactSensorState: CONTACT_DETECTED (0) = closed, CONTACT_NOT_DETECTED (1) = open.
 */
export class ContactSensorAccessory {
  private readonly service;
  private readonly batteryService;
  private lastDevice: ContactSensorDevice;

  constructor(
    private readonly platform: AlarmDotComV2Platform,
    private readonly accessory: PlatformAccessory,
    initial: ContactSensorDevice,
  ) {
    this.lastDevice = initial;

    this.service =
      accessory.getService(platform.Service.ContactSensor) ??
      accessory.addService(platform.Service.ContactSensor, initial.name);

    this.service.setCharacteristic(platform.Characteristic.Name, initial.name);

    this.service
      .getCharacteristic(platform.Characteristic.ContactSensorState)
      .onGet(() => this.toHomeKitState(this.lastDevice.closed));

    // Optional battery service; only surfaced if the device reports battery state.
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

  update(device: ContactSensorDevice): void {
    this.lastDevice = device;
    this.service.updateCharacteristic(
      this.platform.Characteristic.ContactSensorState,
      this.toHomeKitState(device.closed),
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

  private toHomeKitState(closed: boolean): number {
    const C = this.platform.Characteristic.ContactSensorState;
    return closed ? C.CONTACT_DETECTED : C.CONTACT_NOT_DETECTED;
  }

  private ensureInfoService(device: ContactSensorDevice): void {
    const info =
      this.accessory.getService(this.platform.Service.AccessoryInformation) ??
      this.accessory.addService(this.platform.Service.AccessoryInformation);
    info
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Alarm.com')
      .setCharacteristic(this.platform.Characteristic.Model, 'Contact Sensor')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, device.id);
  }
}
