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
 * Version spec for pyalarmdotcomajax. As of 2026-04-23, upstream's stable line is
 * 0.5.x (polling); 0.6.x is in beta (event-driven). We accept the stable line for
 * now; when 0.6 goes GA we'll bump this and switch the daemon to its event transport.
 */
export const PYALARMDOTCOMAJAX_SPEC = 'pyalarmdotcomajax>=0.5.13,<0.7.0';

/** Directory (relative to Homebridge storage) where the plugin keeps its private venv and state. */
export const PLUGIN_STATE_SUBDIR = 'alarm-dot-com-v2';
