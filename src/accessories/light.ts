import { CharacteristicValue, PlatformAccessory } from 'homebridge';

import type { AlarmDotComV2Platform } from '../platform.js';
import type { LightDevice } from '../types.js';

type LightAction = { kind: 'on' } | { kind: 'off' } | { kind: 'set_brightness'; value: number };

/**
 * Light accessory. Exposes a Lightbulb service with On (and Brightness for dimmers).
 *
 * Brightness handling: HomeKit's Home app can deliver a Brightness write at
 * the same time as an On write. ADC's set_brightness implicitly turns the
 * light on, and turn_on/turn_off don't accept a level — so we route any
 * Brightness write through set_brightness and let the daemon dimmer logic
 * handle "on + set level" atomically.
 */
export class LightAccessory {
  private readonly service;
  private readonly batteryService;
  private lastDevice: LightDevice;

  constructor(
    private readonly platform: AlarmDotComV2Platform,
    private readonly accessory: PlatformAccessory,
    initial: LightDevice,
    private readonly onAction: (action: LightAction) => Promise<void>,
  ) {
    this.lastDevice = initial;

    this.service =
      accessory.getService(platform.Service.Lightbulb) ??
      accessory.addService(platform.Service.Lightbulb, initial.name);

    this.service.setCharacteristic(platform.Characteristic.Name, initial.name);

    this.service
      .getCharacteristic(platform.Characteristic.On)
      .onGet(() => this.lastDevice.on)
      .onSet((value) => this.handleOnSet(value));

    if (initial.dimmer) {
      this.service
        .getCharacteristic(platform.Characteristic.Brightness)
        .onGet(() => this.lastDevice.brightness ?? 0)
        .onSet((value) => this.handleBrightnessSet(value));
    }

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

  update(device: LightDevice): void {
    this.lastDevice = device;
    this.service.updateCharacteristic(this.platform.Characteristic.On, device.on);
    if (device.dimmer && device.brightness !== undefined) {
      this.service.updateCharacteristic(this.platform.Characteristic.Brightness, device.brightness);
    }
    if (this.batteryService && device.lowBattery !== undefined) {
      this.batteryService.updateCharacteristic(
        this.platform.Characteristic.StatusLowBattery,
        device.lowBattery
          ? this.platform.Characteristic.StatusLowBattery.BATTERY_LEVEL_LOW
          : this.platform.Characteristic.StatusLowBattery.BATTERY_LEVEL_NORMAL,
      );
    }
  }

  private async handleOnSet(value: CharacteristicValue): Promise<void> {
    const desired = Boolean(value);
    this.platform.log.info(`[light:${this.lastDevice.name}] On requested: ${desired}`);
    try {
      await this.onAction({ kind: desired ? 'on' : 'off' });
    } catch (err) {
      this.platform.log.error(
        `[light:${this.lastDevice.name}] on/off failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      this.service.updateCharacteristic(this.platform.Characteristic.On, this.lastDevice.on);
    }
  }

  private async handleBrightnessSet(value: CharacteristicValue): Promise<void> {
    const level = Math.max(0, Math.min(100, Math.round(Number(value))));
    this.platform.log.info(`[light:${this.lastDevice.name}] brightness requested: ${level}`);
    try {
      // Level 0 → treat as off; otherwise set_brightness implicitly turns on.
      if (level === 0) {
        await this.onAction({ kind: 'off' });
      } else {
        await this.onAction({ kind: 'set_brightness', value: level });
      }
    } catch (err) {
      this.platform.log.error(
        `[light:${this.lastDevice.name}] brightness failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      if (this.lastDevice.brightness !== undefined) {
        this.service.updateCharacteristic(
          this.platform.Characteristic.Brightness,
          this.lastDevice.brightness,
        );
      }
    }
  }

  private ensureInfoService(device: LightDevice): void {
    const info =
      this.accessory.getService(this.platform.Service.AccessoryInformation) ??
      this.accessory.addService(this.platform.Service.AccessoryInformation);
    info
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Alarm.com')
      .setCharacteristic(this.platform.Characteristic.Model, device.dimmer ? 'Dimmer' : 'Light')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, device.id);
  }
}
