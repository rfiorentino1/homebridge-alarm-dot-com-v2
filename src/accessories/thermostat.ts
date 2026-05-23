import { CharacteristicValue, PlatformAccessory } from 'homebridge';

import type { AlarmDotComV2Platform } from '../platform.js';
import type { ThermostatDevice, ThermostatMode } from '../types.js';

type ThermostatAction =
  | { kind: 'set_mode'; value: ThermostatMode }
  | { kind: 'set_heat_setpoint'; value: number }
  | { kind: 'set_cool_setpoint'; value: number };

/**
 * Thermostat accessory.
 *
 * HomeKit semantics:
 *   CurrentHeatingCoolingState: OFF=0, HEAT=1, COOL=2 (no AUTO)
 *   TargetHeatingCoolingState : OFF=0, HEAT=1, COOL=2, AUTO=3
 *   CurrentTemperature        : °C, read-only
 *   TargetTemperature         : °C, single setpoint (used in HEAT/COOL modes)
 *   CoolingThresholdTemperature, HeatingThresholdTemperature: used in AUTO mode
 *
 * ADC delivers everything in either °F or °C depending on Identity.use_celsius;
 * the daemon already normalizes everything to °C before sending. We trust
 * the wire format and pass values through unchanged.
 *
 * Mode mapping: HomeKit AUTO is a real mode for ADC too. CurrentState has no
 * AUTO; ADC's "inferred_state" (which side of the deadband is actually running)
 * isn't always available, so when in AUTO mode we infer current state from
 * whether current < heatSetpoint or current > coolSetpoint, else OFF.
 */
export class ThermostatAccessory {
  private readonly service;
  private lastDevice: ThermostatDevice;

  constructor(
    private readonly platform: AlarmDotComV2Platform,
    private readonly accessory: PlatformAccessory,
    initial: ThermostatDevice,
    private readonly onAction: (action: ThermostatAction) => Promise<void>,
  ) {
    this.lastDevice = initial;

    this.service =
      accessory.getService(platform.Service.Thermostat) ??
      accessory.addService(platform.Service.Thermostat, initial.name);

    this.service.setCharacteristic(platform.Characteristic.Name, initial.name);

    // Constrain TargetHeatingCoolingState to the modes this thermostat actually
    // supports. Home app gates the picker based on validValues — this is what
    // hides "Heat" on a cool-only ecobee, for example.
    this.service
      .getCharacteristic(platform.Characteristic.TargetHeatingCoolingState)
      .setProps({ validValues: this.supportedTargetStates(initial) })
      .onGet(() => this.toTargetMode(this.lastDevice))
      .onSet((value) => this.handleTargetModeSet(value));

    this.service
      .getCharacteristic(platform.Characteristic.CurrentHeatingCoolingState)
      .onGet(() => this.toCurrentMode(this.lastDevice));

    this.service
      .getCharacteristic(platform.Characteristic.CurrentTemperature)
      .onGet(() => this.lastDevice.currentTempC ?? 20);

    this.service
      .getCharacteristic(platform.Characteristic.TargetTemperature)
      .setProps({
        minValue: initial.minHeatC ?? 10,
        maxValue: initial.maxCoolC ?? 38,
        minStep: 0.5,
      })
      .onGet(() => this.targetForCurrentMode(this.lastDevice))
      .onSet((value) => this.handleTargetTempSet(value));

    if (initial.supportsAuto) {
      this.service
        .getCharacteristic(platform.Characteristic.CoolingThresholdTemperature)
        .setProps({
          minValue: initial.minCoolC ?? 10,
          maxValue: initial.maxCoolC ?? 38,
          minStep: 0.5,
        })
        .onGet(() => this.lastDevice.coolSetpointC ?? 24)
        .onSet((value) =>
          this.dispatch({ kind: 'set_cool_setpoint', value: Number(value) }, 'cooling threshold'),
        );

      this.service
        .getCharacteristic(platform.Characteristic.HeatingThresholdTemperature)
        .setProps({
          minValue: initial.minHeatC ?? 10,
          maxValue: initial.maxHeatC ?? 38,
          minStep: 0.5,
        })
        .onGet(() => this.lastDevice.heatSetpointC ?? 20)
        .onSet((value) =>
          this.dispatch({ kind: 'set_heat_setpoint', value: Number(value) }, 'heating threshold'),
        );
    }

    this.service
      .getCharacteristic(platform.Characteristic.TemperatureDisplayUnits)
      .onGet(() =>
        initial.usesCelsius
          ? platform.Characteristic.TemperatureDisplayUnits.CELSIUS
          : platform.Characteristic.TemperatureDisplayUnits.FAHRENHEIT,
      );

    if (initial.humidity !== undefined) {
      this.service
        .getCharacteristic(platform.Characteristic.CurrentRelativeHumidity)
        .onGet(() => this.lastDevice.humidity ?? 0);
    }

    this.ensureInfoService(initial);
  }

  update(device: ThermostatDevice): void {
    this.lastDevice = device;
    this.service.updateCharacteristic(
      this.platform.Characteristic.TargetHeatingCoolingState,
      this.toTargetMode(device),
    );
    this.service.updateCharacteristic(
      this.platform.Characteristic.CurrentHeatingCoolingState,
      this.toCurrentMode(device),
    );
    if (device.currentTempC !== null) {
      this.service.updateCharacteristic(
        this.platform.Characteristic.CurrentTemperature,
        device.currentTempC,
      );
    }
    const target = this.targetForCurrentMode(device);
    if (target !== null) {
      this.service.updateCharacteristic(this.platform.Characteristic.TargetTemperature, target);
    }
    if (device.supportsAuto) {
      if (device.coolSetpointC !== null) {
        this.service.updateCharacteristic(
          this.platform.Characteristic.CoolingThresholdTemperature,
          device.coolSetpointC,
        );
      }
      if (device.heatSetpointC !== null) {
        this.service.updateCharacteristic(
          this.platform.Characteristic.HeatingThresholdTemperature,
          device.heatSetpointC,
        );
      }
    }
    if (device.humidity !== undefined) {
      this.service.updateCharacteristic(
        this.platform.Characteristic.CurrentRelativeHumidity,
        device.humidity,
      );
    }
  }

  private async handleTargetModeSet(value: CharacteristicValue): Promise<void> {
    const T = this.platform.Characteristic.TargetHeatingCoolingState;
    let mode: ThermostatMode;
    switch (Number(value)) {
      case T.HEAT:
        mode = 'heat';
        break;
      case T.COOL:
        mode = 'cool';
        break;
      case T.AUTO:
        mode = 'auto';
        break;
      case T.OFF:
      default:
        mode = 'off';
        break;
    }
    await this.dispatch({ kind: 'set_mode', value: mode }, `set mode to ${mode}`);
  }

  private async handleTargetTempSet(value: CharacteristicValue): Promise<void> {
    const target = Number(value);
    // In HEAT mode this is the heat setpoint; in COOL mode the cool setpoint.
    // In AUTO mode HomeKit normally drives the dedicated threshold chars instead,
    // so we treat a TargetTemperature write in AUTO as a heat setpoint (an
    // arbitrary but consistent choice — Home app rarely writes here in AUTO).
    const mode = this.lastDevice.mode;
    if (mode === 'cool') {
      await this.dispatch({ kind: 'set_cool_setpoint', value: target }, `cool setpoint to ${target}°C`);
    } else {
      await this.dispatch({ kind: 'set_heat_setpoint', value: target }, `heat setpoint to ${target}°C`);
    }
  }

  private async dispatch(action: ThermostatAction, what: string): Promise<void> {
    this.platform.log.info(`[thermostat:${this.lastDevice.name}] ${what}`);
    try {
      await this.onAction(action);
    } catch (err) {
      this.platform.log.error(
        `[thermostat:${this.lastDevice.name}] ${what} failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      // Revert relevant characteristic so HomeKit doesn't show a stuck request.
      this.update(this.lastDevice);
    }
  }

  private toTargetMode(device: ThermostatDevice): number {
    const T = this.platform.Characteristic.TargetHeatingCoolingState;
    switch (device.mode) {
      case 'heat':
        return T.HEAT;
      case 'cool':
        return T.COOL;
      case 'auto':
        return T.AUTO;
      case 'off':
      case 'unknown':
      default:
        return T.OFF;
    }
  }

  private toCurrentMode(device: ThermostatDevice): number {
    const C = this.platform.Characteristic.CurrentHeatingCoolingState;
    if (device.mode === 'heat') return C.HEAT;
    if (device.mode === 'cool') return C.COOL;
    if (device.mode === 'auto') {
      // Infer side of deadband from current temp + setpoints. If we can't,
      // report OFF rather than guess.
      const t = device.currentTempC;
      if (t !== null && device.heatSetpointC !== null && t < device.heatSetpointC) return C.HEAT;
      if (t !== null && device.coolSetpointC !== null && t > device.coolSetpointC) return C.COOL;
      return C.OFF;
    }
    return C.OFF;
  }

  private targetForCurrentMode(device: ThermostatDevice): number | null {
    if (device.mode === 'cool') return device.coolSetpointC;
    if (device.mode === 'heat') return device.heatSetpointC;
    // In AUTO we still need to return *something* for the TargetTemperature
    // char; pick the heat setpoint as the convention (matches handleTargetTempSet).
    if (device.mode === 'auto') return device.heatSetpointC ?? device.coolSetpointC;
    // OFF / unknown: return last known heat setpoint as a stable placeholder.
    return device.heatSetpointC ?? device.coolSetpointC ?? 20;
  }

  private supportedTargetStates(device: ThermostatDevice): number[] {
    const T = this.platform.Characteristic.TargetHeatingCoolingState;
    const out: number[] = [];
    if (device.supportsOff) out.push(T.OFF);
    if (device.supportsHeat) out.push(T.HEAT);
    if (device.supportsCool) out.push(T.COOL);
    if (device.supportsAuto) out.push(T.AUTO);
    // Always include OFF as a safety net if ADC didn't report supportsOff —
    // HomeKit refuses an empty validValues array.
    if (out.length === 0) out.push(T.OFF);
    return out;
  }

  private ensureInfoService(device: ThermostatDevice): void {
    const info =
      this.accessory.getService(this.platform.Service.AccessoryInformation) ??
      this.accessory.addService(this.platform.Service.AccessoryInformation);
    info
      .setCharacteristic(this.platform.Characteristic.Manufacturer, 'Alarm.com')
      .setCharacteristic(this.platform.Characteristic.Model, 'Thermostat')
      .setCharacteristic(this.platform.Characteristic.SerialNumber, device.id);
  }
}
