import { PlatformAccessory } from 'homebridge';

import type { AlarmDotComV2Platform } from '../platform.js';
import type { MotionSensorDevice } from '../types.js';

/**
 * Motion sensor. HomeKit's MotionDetected characteristic is a boolean: true = motion detected.
 */
export class MotionSensorAccessory {
  private readonly service;
  private readonly batteryService;
  private lastDevice: MotionSensorDevice;

  constructor(
    private readonly platform: AlarmDotComV2Platform,
    private readonly accessory: PlatformAccessory,
    initial: MotionSensorDevice,
  ) {
    this.lastDevice = initial;

    this.service =
      accessory.getService(platform.Service.MotionSensor) ??
      accessory.addService(platform.Service.MotionSensor, initial.name);

    this.service.setCharacteristic(platform.Characteristic.Name, initial.name);

    this.service
      .getCharacteristic(platform.Characteristic.MotionDetected)
      .onGet(() => this.lastDevice.motion);

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

  update(device: MotionSensorDevice): void {
    this.lastDevice = device;
    this.service.updateCharacteristic(
      this.platform.Characteristic.MotionDetected,
      device.motion,
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

  private ensureInfoService(device: MotionSensorDevice): void {
    const info =
      this.accessory.getService(this.platform.Service.AccessoryInformation) ??
      this.accessory.addService(this.platform.Service.AccessoryInformation);
    info
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Alarm.com')
      .setCharacteristic(this.platform.Characteristic.Model, 'Motion Sensor')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, device.id);
  }
}
