"""ADC auth helper for the plugin's custom Homebridge UI.

Spawned by `homebridge-ui/server.js` once per UI request. Reads a JSON
payload from stdin, writes a JSON response to stdout. Designed so the UI
server doesn't need to know anything about pyalarmdotcomajax internals.

Input shape (one of):
  {"action": "discover", "username": "...", "password": "...", "mfa_cookie?": "..."}
  {"action": "request_otp", "username": "...", "password": "...", "method": "sms|email|app"}
  {"action": "submit_otp", "username": "...", "password": "...", "method": "sms|email|app", "code": "...", "device_name?": "..."}

Output shape (always JSON on stdout, single line):
  discover -> {"ok": true}
              | {"ok": false, "otp_required": true, "methods": ["sms","email","app"], "hints": {...}, "username": "..."}
              | {"ok": false, "error": "...", "error_kind": "auth|network|unknown"}
  request_otp -> {"ok": true, "method": "sms"}
                 | {"ok": false, "error": "...", "error_kind": "..."}
  submit_otp -> {"ok": true, "cookie": "F6912F39..."}
                | {"ok": false, "error": "...", "error_kind": "code|auth|cookie|unknown"}

Notes:
- For submit_otp, the cookie is read directly from the bridge's persistent
  cookie jar (under the name `twoFactorAuthenticationId`), NOT from the
  return value of `submit_otp()`. pyalarmdotcomajax 0.6.0b9's submit_otp
  checks `resp.cookies` per-response, but the cookie ends up in the jar
  earlier in the trust-device chain. Reading the jar directly always works.
"""
import asyncio
import json
import sys
import traceback
from typing import Any

# Cookie name ADC sets when the user trusts a device for 2FA.
TRUSTED_DEVICE_COOKIE = "twoFactorAuthenticationId"


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _err(error: str, kind: str = "unknown") -> dict[str, Any]:
    return {"ok": False, "error": error, "error_kind": kind}


async def _build_bridge(username: str, password: str, mfa_cookie: str = ""):
    from pyalarmdotcomajax import AlarmBridge
    bridge = AlarmBridge()
    bridge.auth_controller.set_credentials(
        username=username, password=password, mfa_cookie=mfa_cookie or None
    )
    return bridge


def _read_jar_cookie(bridge) -> str:
    try:
        for c in bridge._websession.cookie_jar:
            if c.key == TRUSTED_DEVICE_COOKIE and c.value:
                return c.value
    except Exception:
        pass
    return ""


async def action_discover(payload: dict[str, Any]) -> dict[str, Any]:
    from pyalarmdotcomajax.exceptions import OtpRequired, AuthenticationFailed

    bridge = await _build_bridge(
        payload["username"], payload["password"], payload.get("mfa_cookie", "")
    )
    try:
        try:
            await bridge.login()
        except OtpRequired as e:
            methods = [m.name for m in e.enabled_2fa_methods]
            hints: dict[str, str] = {}
            email = getattr(e, "email", None)
            if email:
                hints["email"] = email
            sms = getattr(e, "sms_number", None)
            if sms:
                hints["sms"] = f"***-***-{str(sms)[-4:]}"
            return {
                "ok": False,
                "otp_required": True,
                "methods": methods,
                "hints": hints,
                "username": payload["username"],
            }
        except AuthenticationFailed as e:
            return _err(f"Login failed: {e or 'invalid credentials'}", "auth")
        return {"ok": True}
    finally:
        await _close_bridge(bridge)


async def action_request_otp(payload: dict[str, Any]) -> dict[str, Any]:
    from pyalarmdotcomajax import OtpType
    from pyalarmdotcomajax.exceptions import OtpRequired, AuthenticationFailed

    method = payload["method"].lower()
    if method not in {"sms", "email", "app"}:
        return _err(f"Unknown method: {method}", "unknown")
    otp_type = {"sms": OtpType.sms, "email": OtpType.email, "app": OtpType.app}[method]

    bridge = await _build_bridge(payload["username"], payload["password"])
    try:
        try:
            await bridge.login()
        except OtpRequired:
            pass  # expected
        except AuthenticationFailed as e:
            return _err(f"Login failed: {e or 'invalid credentials'}", "auth")
        # request_otp is a no-op for TOTP (the user's authenticator already has codes).
        if otp_type in (OtpType.email, OtpType.sms):
            await bridge.auth_controller.request_otp(otp_type)
        return {"ok": True, "method": method}
    finally:
        await _close_bridge(bridge)


async def action_submit_otp(payload: dict[str, Any]) -> dict[str, Any]:
    from pyalarmdotcomajax import OtpType
    from pyalarmdotcomajax.exceptions import OtpRequired, AuthenticationFailed

    method = payload["method"].lower()
    otp_type = {"sms": OtpType.sms, "email": OtpType.email, "app": OtpType.app}.get(method)
    if otp_type is None:
        return _err(f"Unknown method: {method}", "unknown")

    code = (payload.get("code") or "").strip()
    if not code:
        return _err("Missing OTP code", "code")
    device_name = payload.get("device_name") or "Homebridge"

    bridge = await _build_bridge(payload["username"], payload["password"])
    try:
        try:
            await bridge.login()
        except OtpRequired:
            pass
        except AuthenticationFailed as e:
            return _err(f"Login failed: {e or 'invalid credentials'}", "auth")
        try:
            await bridge.auth_controller.submit_otp(code, otp_type, device_name=device_name)
        except Exception as e:
            # submit_otp may raise UnexpectedResponse even when the OTP was accepted
            # and the cookie was set in the jar; treat as soft-failure and check the jar.
            cookie_from_jar = _read_jar_cookie(bridge)
            if cookie_from_jar:
                return {"ok": True, "cookie": cookie_from_jar}
            kind = "code" if "code" in str(e).lower() or "verif" in str(e).lower() else "cookie"
            return _err(f"submit_otp failed: {e}", kind)
        cookie_from_jar = _read_jar_cookie(bridge)
        if cookie_from_jar:
            return {"ok": True, "cookie": cookie_from_jar}
        return _err(
            "OTP accepted but no trusted-device cookie was set. "
            "Your Alarm.com account may not allow trusted devices.",
            "cookie",
        )
    finally:
        await _close_bridge(bridge)


async def _close_bridge(bridge) -> None:
    try:
        await bridge.close()
    except Exception:
        pass


async def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as e:
        _emit(_err(f"Bad request JSON: {e}", "unknown"))
        return 2

    action = payload.get("action")
    handlers = {
        "discover": action_discover,
        "request_otp": action_request_otp,
        "submit_otp": action_submit_otp,
    }
    handler = handlers.get(action)
    if not handler:
        _emit(_err(f"Unknown action: {action!r}", "unknown"))
        return 2

    try:
        result = await handler(payload)
    except Exception as e:
        sys.stderr.write(traceback.format_exc())
        _emit(_err(f"{type(e).__name__}: {e}", "unknown"))
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
