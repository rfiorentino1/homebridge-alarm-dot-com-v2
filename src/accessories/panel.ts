import { CharacteristicValue, PlatformAccessory } from 'homebridge';

import type { AlarmDotComV2Platform } from '../platform.js';
import type { PanelDevice, PanelState } from '../types.js';

type PanelAction = 'arm_stay' | 'arm_away' | 'arm_night' | 'disarm';

/**
 * Maps HomeKit's `SecuritySystemCurrentState` / `TargetState` characteristic
 * values to/from Alarm.com's panel states.
 *
 * HomeKit values:
 *   STAY_ARM = 0, AWAY_ARM = 1, NIGHT_ARM = 2, DISARMED = 3, ALARM_TRIGGERED = 4
 */
export class SecurityPanelAccessory {
  private readonly service;
  private lastDevice: PanelDevice;

  constructor(
    private readonly platform: AlarmDotComV2Platform,
    private readonly accessory: PlatformAccessory,
    initial: PanelDevice,
    private readonly onAction: (action: PanelAction) => Promise<void>,
  ) {
    this.lastDevice = initial;

    this.service =
      accessory.getService(platform.Service.SecuritySystem) ??
      accessory.addService(platform.Service.SecuritySystem, initial.name);

    this.service.setCharacteristic(platform.Characteristic.Name, initial.name);

    this.service
      .getCharacteristic(platform.Characteristic.SecuritySystemCurrentState)
      .onGet(() => this.toHomeKitCurrent(this.lastDevice.state));

    this.service
      .getCharacteristic(platform.Characteristic.SecuritySystemTargetState)
      .onGet(() => this.toHomeKitTarget(this.lastDevice.state))
      .onSet((value) => this.handleTargetSet(value));

    this.ensureInfoService(initial);
  }

  update(device: PanelDevice): void {
    this.lastDevice = device;
    this.service.updateCharacteristic(
      this.platform.Characteristic.SecuritySystemCurrentState,
      this.toHomeKitCurrent(device.state),
    );
    this.service.updateCharacteristic(
      this.platform.Characteristic.SecuritySystemTargetState,
      this.toHomeKitTarget(device.state),
    );
  }

  private async handleTargetSet(value: CharacteristicValue): Promise<void> {
    const action = this.homeKitTargetToAction(Number(value));
    this.platform.log.info(
      `[panel] target-state change requested: ${action} (HomeKit value ${value})`,
    );
    try {
      await this.onAction(action);
    } catch (err) {
      this.platform.log.error(
        `[panel] ${action} failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      // Revert the HomeKit target characteristic to the actual state so the user sees it didn't stick.
      this.service.updateCharacteristic(
        this.platform.Characteristic.SecuritySystemTargetState,
        this.toHomeKitTarget(this.lastDevice.state),
      );
    }
  }

  private toHomeKitCurrent(state: PanelState): number {
    const C = this.platform.Characteristic.SecuritySystemCurrentState;
    switch (state) {
      case 'armed_stay':
        return C.STAY_ARM;
      case 'armed_away':
        return C.AWAY_ARM;
      case 'armed_night':
        return C.NIGHT_ARM;
      case 'triggered':
        return C.ALARM_TRIGGERED;
      case 'disarmed':
      case 'unknown':
      default:
        return C.DISARMED;
    }
  }

  private toHomeKitTarget(state: PanelState): number {
    const T = this.platform.Characteristic.SecuritySystemTargetState;
    switch (state) {
      case 'armed_stay':
        return T.STAY_ARM;
      case 'armed_away':
        return T.AWAY_ARM;
      case 'armed_night':
        return T.NIGHT_ARM;
      case 'disarmed':
      case 'triggered':
      case 'unknown':
      default:
        return T.DISARM;
    }
  }

  private homeKitTargetToAction(value: number): PanelAction {
    const T = this.platform.Characteristic.SecuritySystemTargetState;
    switch (value) {
      case T.STAY_ARM:
        return 'arm_stay';
      case T.AWAY_ARM:
        return 'arm_away';
      case T.NIGHT_ARM:
        return 'arm_night';
      case T.DISARM:
      default:
        return 'disarm';
    }
  }

  private ensureInfoService(device: PanelDevice): void {
    const info =
      this.accessory.getService(this.platform.Service.AccessoryInformation) ??
      this.accessory.addService(this.platform.Service.AccessoryInformation);
    info
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Alarm.com')
      .setCharacteristic(this.platform.Characteristic.Model, 'Security Panel')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, device.id);
  }
}
