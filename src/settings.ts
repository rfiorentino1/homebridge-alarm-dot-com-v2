/**
 * Plugin constants. Shared between platform, accessories, and the Python bridge.
 */

/** Must match `pluginAlias` in config.schema.json and what the user adds to their Homebridge config. */
export const PLATFORM_NAME = 'AlarmDotComV2';

/** Must match `name` field in package.json. */
export const PLUGIN_NAME = 'homebridge-alarm-dot-com-v2';

/** Minimum Python major.minor required by pyalarmdotcomajax as of this writing. */
export const MIN_PYTHON_VERSION = '3.13';

/**
 * Version spec for pyalarmdotcomajax. Upstream's 0.6.x line is tagged beta but is
 * the version the Home Assistant integration pins in production (and has been stable
 * for ~12 months without new beta bumps), so we use it. We require `--pre` on pip
 * install because pip treats anything with "b9" as a prerelease.
 */
export const PYALARMDOTCOMAJAX_SPEC = 'pyalarmdotcomajax>=0.6.0b9,<0.8.0';

/** Directory (relative to Homebridge storage) where the plugin keeps its private venv and state. */
export const PLUGIN_STATE_SUBDIR = 'alarm-dot-com-v2';
