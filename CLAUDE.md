# homebridge-alarm-dot-com-v2 — working notes

Homebridge plugin exposing an Alarm.com (incl. ADT-branded / ADT Control, Brinks,
etc.) security system to HomeKit: Security Panel + contact & motion sensors.

## Architecture
- **TypeScript Homebridge plugin** (`src/` → `dist/`) — the HomeKit-facing side.
- **Python daemon** (`python/daemon.py`) — wraps `pyalarmdotcomajax` (pinned
  0.6.x). Communicates with the Node plugin over newline-delimited JSON-RPC 2.0
  on stdin/stdout. **Shipped verbatim** in the npm package (the `python/` dir is
  in package.json `files`) — it is NOT compiled.
- **Custom Homebridge UI** (`homebridge-ui/`) — plain Node + HTML, served by
  homebridge-config-ui-x. `server.js` (HomebridgePluginUiServer) handles
  `/discover` / `/request-otp` / `/submit-otp` from the frontend by spawning a
  one-shot Python helper (`python/ui_auth.py`) that drives the ADC auth flow
  and (for `submit-otp`) reads the trusted-device cookie directly from the
  bridge's persistent cookie jar — the lib's own `submit_otp()` return value
  is unreliable in 0.6.0b9 because its middleware only watches per-response
  cookies and the trust-device response doesn't re-emit it. The UI persists
  username + password + captured `mfaCookie` back to plugin config via
  `homebridge.updatePluginConfig` + `homebridge.savePluginConfig`.
- On first run the plugin bootstraps a private venv on the Homebridge host and
  pip-installs `pyalarmdotcomajax` into it. The bootstrap prefers a system
  `python3.13+` (or any earlier candidate satisfying `MIN_PYTHON_VERSION`); if
  none is available, it downloads a portable CPython 3.13 from
  astral-sh/python-build-standalone (release pinned in
  `src/settings.ts:MANAGED_PYTHON_RELEASE`), SHA-256-verifies against the
  release's `SHA256SUMS`, and uses that. The managed Python lives under
  `<state>/python/` and is reused across restarts.
  - The UI server reuses the same venv (resolved as
    `<homebridgeStoragePath>/alarm-dot-com-v2/venv/bin/python`), so the
    Sign in screen requires the plugin to have bootstrapped at least once.
    On a brand-new install, the user saves any value once to trigger the
    daemon's first launch, then returns to the Sign in screen.

## Layout
- `src/` — TypeScript source. `src/accessories/panel.ts` maps the wire panel
  state → HomeKit characteristics; `src/types.ts` holds the `PanelState` union.
- `python/daemon.py` — the daemon. Wire panel `state` values: `disarmed`,
  `armed_stay`, `armed_away`, `armed_night`, `triggered`, `unknown`.
- `dist/` — compiled TS (built by `tsc`).
- `config.schema.json` — Homebridge UI config form.

## Build & dev
- `npm run build` — `tsc` (`src/` → `dist/`).
- `npm run lint` / `npm run format`.
- `prepare` runs `npm run build` automatically — so installing from git builds
  `dist/` on the target.
- Syntax-check the daemon: `python3 -m py_compile python/daemon.py`.

## Commit & deploy procedure  ← how changes ship
1. Make the change here in this repo. `python/daemon.py` ships as-is (no build);
   `src/*.ts` changes are built by the `prepare` script on install.
2. Commit to `main`. Bump version with `npm version patch`. Push with
   `git push origin main --follow-tags`.
3. Deploy to the Homebridge host: as the `homebridge` user, run
   `npm install github:rfiorentino1/homebridge-alarm-dot-com-v2` from the
   Homebridge directory. ⚠️ Do NOT rsync/`cp` into `node_modules/` — the
   Homebridge UI prunes plugins it didn't install via npm (it tracks
   package.json). Install-from-git registers it properly and runs `prepare`.
4. Restart Homebridge: `hb-service restart`.
- Rollback: `npm install github:rfiorentino1/homebridge-alarm-dot-com-v2#vX.Y.Z`.
- Urgent daemon-only hotfix: scp the new `python/daemon.py` onto the host
  (back up the original first) + restart — but still do steps 1-4 afterward, or
  the next npm install reverts it.

## Deployment target (Rocco's parents' house)
- Runs on the parents' Pi4. Homebridge is the self-contained `/opt/homebridge`
  install (node/npm/hb-service under `/opt/homebridge/bin/`).
- Plugin on host: `/var/lib/homebridge/node_modules/homebridge-alarm-dot-com-v2/`;
  it's a `github:` dependency in `/var/lib/homebridge/package.json`.
- Daemon venv on host: `/var/lib/homebridge/alarm-dot-com-v2/venv/`.

## Notes
- An Alarm.com alarm is NOT a value of the partition `state` enum — it's a
  separate condition (`has_active_alarm`), and a `PendingAlarm` websocket event
  is delivered against the violated *sensor* id, not the partition. The daemon
  tracks alarm state in `_partition_alarm_active` (keyed by partition id) from
  alarm-class WS event subtypes and overrides the wire `state` to `triggered`.
- `pyalarmdotcomajax` marks alarm/panic event subtypes "Unsupported" — that only
  means no built-in controller acts on them; the raw event still reaches
  `on_raw_event`, which is where the daemon hooks them.
