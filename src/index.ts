import type { API } from 'homebridge';

import { AlarmDotComV2Platform } from './platform.js';
import { PLATFORM_NAME } from './settings.js';

/**
 * Homebridge plugin entry point.
 * Registers the platform class under the alias declared in config.schema.json.
 */
export default (api: API): void => {
  api.registerPlatform(PLATFORM_NAME, AlarmDotComV2Platform);
};
