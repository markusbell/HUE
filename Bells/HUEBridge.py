# HUEBridge.py
import os
import json
import logging
from typing import Any, Dict, Optional, Tuple, List

import azure.functions as func
import requests
import urllib3

log = logging.getLogger(__name__)

# Hue Bridges often use self-signed certificates; requests verify may be disabled optionally.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

bp = func.Blueprint()


def _env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None or v == "":
        raise ValueError(f"Missing app setting: {name}")
    return v


def _bool_env(name: str, default: str = "false") -> bool:
    return _env(name, default).strip().lower() in ("1", "true", "yes", "y", "on")


def _json_body(req: func.HttpRequest) -> Dict[str, Any]:
    try:
        b = req.get_body()
        if not b:
            return {}
        return json.loads(b.decode("utf-8"))
    except Exception:
        return {}


def _unauthorized() -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps({"error": "Unauthorized"}),
        status_code=401,
        mimetype="application/json",
    )


def _bad_request(msg: str) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps({"error": "Bad request", "detail": msg}),
        status_code=400,
        mimetype="application/json",
    )


def _server_error(msg: str, detail: str) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps({"error": msg, "detail": detail}),
        status_code=500,
        mimetype="application/json",
    )


def _bad_gateway(detail: str) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps({"error": "Hue request failed", "detail": detail}),
        status_code=502,
        mimetype="application/json",
    )


# Lösung A: API Key ist OPTIONAL (nur wenn API_KEY gesetzt ist)
def _check_api_key(req: func.HttpRequest) -> Optional[func.HttpResponse]:
    expected = os.getenv("API_KEY")  # optional
    if not expected:
        return None
    got = req.headers.get("x-api-key")
    if not got or got != expected:
        return _unauthorized()
    return None


class HueBridgeV2:
    """
    Minimal wrapper for Philips Hue CLIP v2:
      Base URL: https://<bridge-host[:port]>/clip/v2/resource
      Header: hue-application-key
      Response shape: { "errors": [...], "data": [...] }
    """

    def __init__(self, host_with_port: str, app_key: str, verify_ssl: bool = False):
        self.host_with_port = host_with_port  # Variante 1: enthält Host:Port (z.B. public-ip:8443)
        self.app_key = app_key
        self.verify_ssl = verify_ssl
        self.base_url = f"https://{self.host_with_port}/clip/v2/resource"
        self.headers = {
            "hue-application-key": self.app_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        resp = requests.request(
            method=method,
            url=url,
            headers=self.headers,
            data=json.dumps(body) if body is not None else None,
            verify=self.verify_ssl,
            timeout=10,
        )

        if 200 <= resp.status_code < 300:
            # Hue antwortet i.d.R. JSON
            return resp.json()

        # best-effort error payload
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": resp.text}

        raise ConnectionError({"status": resp.status_code, "response": payload})

    @staticmethod
    def _to_data(payload: Dict[str, Any]) -> Any:
        errs = payload.get("errors") or []
        if errs:
            raise ConnectionError(errs)
        return payload.get("data", [])

    def get_lights(self) -> Any:
        payload = self._request("GET", "/light")
        return self._to_data(payload)

    def update_light(self, light_id: str, patch: Dict[str, Any]) -> Any:
        payload = self._request("PUT", f"/light/{light_id}", patch)
        return self._to_data(payload)


def _get_bridge() -> HueBridgeV2:
    # Variante 1: HUE_BRIDGE_IP enthält Host:Port, z.B. "x.y.z.w:8443"
    host_with_port = _env("HUE_BRIDGE_IP")
    key = _env("HUE_APPLICATION_KEY")
    verify_ssl = _bool_env("HUE_VERIFY_SSL", "false")
    return HueBridgeV2(host_with_port, key, verify_ssl=verify_ssl)


def _parse_xy(body: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    # Accept: {"color_xy":[x,y]} or {"color_xy":{"x":..,"y":..}} or {"xy":[x,y]} or {"xy":{"x":..,"y":..}}
    for k in ("color_xy", "xy"):
        if k in body:
            v = body[k]
            if isinstance(v, (list, tuple)) and len(v) == 2:
                return float(v[0]), float(v[1])
            if isinstance(v, dict) and "x" in v and "y" in v:
                return float(v["x"]), float(v["y"])
    return None


# -------------------------------------------------------------------
# COPILOT-STUDIO READY ENDPOINTS
# -------------------------------------------------------------------

@bp.route(route="devices", methods=["GET"])
def list_devices(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns:
      { "devices": [ { "id": "...", "name": "...", "type": "light|plug" }, ... ] }
    Wichtig: Keine Hue-Rohobjekte zurückgeben (Schema-Stabilität in Copilot Studio).
    """
    auth = _check_api_key(req)
    if auth:
        return auth

    try:
        bridge = _get_bridge()
        lights = bridge.get_lights()

        devices: List[Dict[str, str]] = []
        for item in lights:
            meta = item.get("metadata") or {}
            rid = item.get("id")
            name = meta.get("name")

            # Hue liefert auch Steckdosen unter /resource/light; archetype "plug" ist ein gutes Signal
            archetype = (meta.get("archetype") or "").strip().lower()
            dev_type = "plug" if archetype == "plug" else "light"

            if rid and name:
                devices.append({"id": str(rid), "name": str(name), "type": dev_type})

        devices.sort(key=lambda d: d["name"].lower())

        return func.HttpResponse(
            body=json.dumps({"devices": devices}, ensure_ascii=False),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as ex:
        log.exception("list_devices failed")
        return _server_error("Internal error", str(ex))


@bp.route(route="light/set", methods=["POST"])
def set_light(req: func.HttpRequest) -> func.HttpResponse:
    """
    Input (JSON):
      {
        "id": "<v2 uuid>",
        "on": true/false,
        "brightness": 0..100 (optional),
        "color_xy": [x,y] or {"x":..,"y":..} (optional),
        "mirek": 153..500 (optional)
      }

    Output (JSON):
      { "status": "ok", "output": <hue data array> }
    """
    auth = _check_api_key(req)
    if auth:
        return auth

    body = _json_body(req)
    light_id = body.get("id")
    if not light_id:
        return _bad_request("Missing required field: id")

    try:
        patch: Dict[str, Any] = {}

        if "on" in body:
            patch["on"] = {"on": bool(body["on"])}

        if "brightness" in body:
            b = float(body["brightness"])
            if b < 0 or b > 100:
                return _bad_request("brightness must be between 0 and 100")
            patch["dimming"] = {"brightness": b}

        xy = _parse_xy(body)
        if xy is not None:
            x, y = xy
            patch["color"] = {"xy": {"x": x, "y": y}}

        if "mirek" in body:
            patch["color_temperature"] = {"mirek": int(body["mirek"])}

        if not patch:
            return _bad_request("No supported fields. Use on/brightness/color_xy/mirek.")

        bridge = _get_bridge()
        result = bridge.update_light(str(light_id), patch)

        return func.HttpResponse(
            body=json.dumps({"status": "ok", "output": result}, ensure_ascii=False),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as ex:
        log.exception("set_light failed")
        return _bad_gateway(str(ex))


# -------------------------------------------------------------------
# OPTIONAL: Legacy endpoints (falls du sie weiterhin per curl nutzen willst)
# Diese bleiben bewusst getrennt und geben weiterhin Hue-Rohobjekte zurück.
# Copilot Studio sollte sie NICHT verwenden.
# -------------------------------------------------------------------

@bp.route(route="hue/lights", methods=["GET"])
def list_lights(req: func.HttpRequest) -> func.HttpResponse:
    auth = _check_api_key(req)
    if auth:
        return auth
    try:
        bridge = _get_bridge()
        lights = bridge.get_lights()
        return func.HttpResponse(
            body=json.dumps({"lights": lights}, ensure_ascii=False),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as ex:
        log.exception("list_lights failed")
        return _server_error("Internal error", str(ex))


@bp.route(route="hue/lights/{light_id}/state", methods=["PUT"])
def set_light_state(req: func.HttpRequest) -> func.HttpResponse:
    # Legacy: PUT /api/hue/lights/{id}/state
    auth = _check_api_key(req)
    if auth:
        return auth

    light_id = req.route_params.get("light_id")
    body = _json_body(req)

    try:
        patch: Dict[str, Any] = {}

        if "on" in body:
            patch["on"] = {"on": bool(body["on"])}

        if "brightness" in body:
            b = float(body["brightness"])
            if b < 0 or b > 100:
                return _bad_request("brightness must be between 0 and 100")
            patch["dimming"] = {"brightness": b}

        xy = _parse_xy(body)
        if xy is not None:
            x, y = xy
            patch["color"] = {"xy": {"x": x, "y": y}}

        if "mirek" in body:
            patch["color_temperature"] = {"mirek": int(body["mirek"])}

        if not patch:
            return _bad_request("No supported fields. Use on/brightness/color_xy/mirek.")

        bridge = _get_bridge()
        result = bridge.update_light(str(light_id), patch)

        return func.HttpResponse(
            body=json.dumps({"result": result}, ensure_ascii=False),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as ex:
        log.exception("set_light_state failed")
        return _bad_gateway(str(ex))