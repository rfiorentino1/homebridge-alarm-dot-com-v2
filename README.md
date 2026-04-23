# homebridge-alarm-dot-com-v2

Event-driven Homebridge plugin for Alarm.com-backed security systems — including **ADT Control**, **ADT Command**, **ADT+**, **Brinks**, and other Alarm.com-powered platforms.

This is a modern successor to the older `homebridge-node-alarm-dot-com`. It piggybacks on the actively-maintained [`pyalarmdotcomajax`](https://github.com/pyalarmdotcom/pyalarmdotcomajax) Python library (the same library that powers Home Assistant's Alarm.com integration) for Alarm.com protocol work, and exposes the panel and sensors as first-class HomeKit accessories.

> ⚠️ Alarm.com does not provide a public API. This plugin relies on reverse-engineered endpoints that can change without notice. Issues are tracked upstream in pyalarmdotcomajax.

## Features

- 🔐 **Security Panel** — arm Stay / Away / Night + Disarm from Home.app
- 🚪 **Contact Sensors** — door/window state in real time
- 👋 **Motion Sensors** — motion alerts via HomeKit
- 🔋 **Battery status** on sensors that report it
- 📡 Polling today; ready to switch to pyalarmdotcomajax's push-update transport when it hits stable (0.6.x)

Planned for later releases: locks, thermostats, garage doors, cameras, glass-break, smoke, water.

## Requirements

- Homebridge 1.8+ (v2 beta supported)
- Node.js 18.20+, 20.15+, or 22+
- **Python 3.13+** installed on the Homebridge host (the plugin creates its own venv and installs `pyalarmdotcomajax` into it — it does not touch your system Python)
- An Alarm.com account (or any account on an Alarm.com-powered service: ADT Control, ADT+, Brinks, etc.)

## Installation

1. In the Homebridge UI, search for and install `homebridge-alarm-dot-com-v2`.
2. On first start, the plugin will create a private venv at `~/.homebridge/alarm-dot-com-v2/venv` and install `pyalarmdotcomajax` there.
3. Open the plugin's config, enter your Alarm.com username and password, and save.
4. Restart Homebridge.

If your account uses 2FA, see the **2FA** section below.

## Configuration

| Field | Description |
|---|---|
| `name` | Display name for the platform. Default: `Alarm.com`. |
| `username` | Your Alarm.com login email. |
| `password` | Your Alarm.com password. |
| `mfaCookie` | (Optional) 2FA remember-me cookie — see below. |
| `exposeSecurityPanel` | Publish the alarm panel as a HomeKit Security System. Default `true`. |
| `exposeContactSensors` | Publish door/window sensors. Default `true`. |
| `exposeMotionSensors` | Publish motion sensors. Default `true`. |
| `armAwayKeypadBypass` | When Home.app requests Arm Away, bypass faulted zones. Default `false`. |
| `pythonPath` | (Advanced) Override the detected Python 3.13+ binary. |
| `logLevel` | `error`, `warn`, `info`, `debug`, `trace`. Default `info`. |

## 2FA

If your Alarm.com account has 2FA enabled:

1. On the Homebridge host, activate the plugin's venv and run the pyalarmdotcomajax CLI to complete a one-time 2FA sign-in:
   ```bash
   source ~/.homebridge/alarm-dot-com-v2/venv/bin/activate
   python -m pyalarmdotcomajax --username YOUR_EMAIL --password YOUR_PASSWORD
   ```
2. When prompted, enter the 2FA code from your authenticator app or SMS.
3. The CLI will print a remember-me cookie string. Paste that into the `mfaCookie` field in the plugin config and save.
4. Restart Homebridge.

The cookie is valid for an extended period; if it expires, repeat the steps above.

## Architecture

The plugin is written in TypeScript and runs under Node. Alarm.com protocol work is delegated to a short-lived Python subprocess (`python/daemon.py`) that imports `pyalarmdotcomajax`. Node and Python exchange newline-delimited JSON-RPC 2.0 messages over stdio. This lets us ride on the upstream library's active maintenance without reimplementing its reverse-engineering work in JavaScript.

```
+------------------+                 +-----------------------+
|  Homebridge      |    JSON-RPC     |  python/daemon.py     |
|  TypeScript      |  <----stdio---> |  (pyalarmdotcomajax)  |
|  (HomeKit side)  |                 |  (Alarm.com side)     |
+------------------+                 +-----------------------+
```

## Credits

- [`pyalarmdotcomajax`](https://github.com/pyalarmdotcom/pyalarmdotcomajax) by uvjustin and contributors — the heavy lifter
- [Home Assistant's alarmdotcom component](https://github.com/pyalarmdotcom/alarmdotcom) for reference
- Original [`homebridge-node-alarm-dot-com`](https://github.com/node-alarm-dot-com/homebridge-node-alarm-dot-com) for inspiration on Homebridge-side patterns

## License

MIT — see `LICENSE`.
