import { PlatformAccessory } from 'homebridge';

import type { AlarmDotComV2Platform } from '../platform.js';
import type { WaterSensorDevice } from '../types.js';

/**
 * Water (leak) sensor. HomeKit's LeakSensor has:
 *   LeakDetected: NOT_DETECTED=0, DETECTED=1
 */
export class WaterSensorAccessory {
  private readonly service;
  private readonly batteryService;
  private lastDevice: WaterSensorDevice;

  constructor(
    private readonly platform: AlarmDotComV2Platform,
    private readonly accessory: PlatformAccessory,
    initial: WaterSensorDevice,
  ) {
    this.lastDevice = initial;

    this.service =
      accessory.getService(platform.Service.LeakSensor) ??
      accessory.addService(platform.Service.LeakSensor, initial.name);

    this.service.setCharacteristic(platform.Characteristic.Name, initial.name);

    this.service
      .getCharacteristic(platform.Characteristic.LeakDetected)
      .onGet(() => this.toHomeKitState(this.lastDevice.leak));

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

  update(device: WaterSensorDevice): void {
    this.lastDevice = device;
    this.service.updateCharacteristic(
      this.platform.Characteristic.LeakDetected,
      this.toHomeKitState(device.leak),
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

  private toHomeKitState(leak: boolean): number {
    const L = this.platform.Characteristic.LeakDetected;
    return leak ? L.LEAK_DETECTED : L.LEAK_NOT_DETECTED;
  }

  private ensureInfoService(device: WaterSensorDevice): void {
    const info =
      this.accessory.getService(this.platform.Service.AccessoryInformation) ??
      this.accessory.addService(this.platform.Service.AccessoryInformation);
    info
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Alarm.com')
      .setCharacteristic(this.platform.Characteristic.Model, 'Leak Sensor')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, device.id);
  }
}
