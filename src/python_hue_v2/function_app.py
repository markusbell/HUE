import os
import json
import logging
from typing import Any, Dict, Optional, Tuple

import azure.functions as func
import requests
import urllib3

# Hue Bridges nutzen häufig self-signed Certs. Wir erlauben optional verify=False.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


def _env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None or v == "":
        raise ValueError(f"Missing app setting: {name}")
    return v


def _bool_env(name: str, default: str = "false") -> bool:
    return _env(name, default).strip().lower() in ("1", "true", "yes", "y", "on")


def _json(req: func.HttpRequest) -> Dict[str, Any]:
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


def _server_error(msg: str, ex: Exception) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps({"error": msg, "detail": str(ex)}),
        status_code=500,
        mimetype="application/json",
    )


def _check_api_key(req: func.HttpRequest) -> Optional[func.HttpResponse]:
    expected = _env("API_KEY")
    got = req.headers.get("x-api-key")
    if not got or got != expected:
        return _unauthorized()
    return None


class HueBridgeV2:
    """
    Minimal, Azure-safe wrapper für Philips Hue CLIP v2.
    - Base: https://<bridge>/clip/v2/resource
    - Auth Header: hue-application-key
    - Response: { "errors": [...], "data": [...] }  [1](https://www.postman.com/openhue/openhue-api/documentation/81b5lb4/hue-clip-api?entity=request-31084416-55cc62be-52a9-42fc-a08d-72829b778691)
    """

    def __init__(self, ip_address: str, app_key: str, verify_ssl: bool = False):
        self.ip_address = ip_address
        self.app_key = app_key
        self.verify_ssl = verify_ssl
        self.base_url = f"https://{self.ip_address}/clip/v2/resource"
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

        # Robust: akzeptiere 2xx, sonst Exception mit Payload/Raw
        if 200 <= resp.status_code < 300:
            return resp.json()

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

    def get_light(self, light_id: str) -> Dict[str, Any]:
        payload = self._request("GET", f"/light/{light_id}")
        data = self._to_data(payload)
        return data[0] if data else {}

    def update_light(self, light_id: str, patch: Dict[str, Any]) -> Any:
        payload = self._request("PUT", f"/light/{light_id}", patch)
        return self._to_data(payload)


def _get_bridge() -> HueBridgeV2:
    ip = _env("HUE_BRIDGE_IP")
    key = _env("HUE_APPLICATION_KEY")  # dein vorhandener Hue Application Key
    verify_ssl = _bool_env("HUE_VERIFY_SSL", "false")  # default: false (Hue self-signed)
    return HueBridgeV2(ip, key, verify_ssl=verify_ssl)


def _parse_xy(body: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    # akzeptiere entweder {"color_xy":[x,y]} oder {"color_xy":{"x":..,"y":..}} oder {"xy":[x,y]}
    for k in ("color_xy", "xy"):
        if k in body:
            v = body[k]
            if isinstance(v, (list, tuple)) and len(v) == 2:
                return float(v[0]), float(v[1])
            if isinstance(v, dict) and "x" in v and "y" in v:
                return float(v["x"]), float(v["y"])
    return None


@app.route(route="hue/lights", methods=["GET"])
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
        logging.exception("list_lights failed")
        return _server_error("Internal error", ex)


@app.route(route="hue/lights/{light_id}", methods=["GET"])
def get_light(req: func.HttpRequest) -> func.HttpResponse:
    auth = _check_api_key(req)
    if auth:
        return auth
    light_id = req.route_params.get("light_id")
    try:
        bridge = _get_bridge()
        light = bridge.get_light(light_id)
        if not light:
            return func.HttpResponse(
                body=json.dumps({"error": "Not found"}),
                status_code=404,
                mimetype="application/json",
            )
        return func.HttpResponse(
            body=json.dumps(light, ensure_ascii=False),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as ex:
        logging.exception("get_light failed")
        return _server_error("Internal error", ex)


@app.route(route="hue/lights/{light_id}/state", methods=["PUT"])
def set_light_state(req: func.HttpRequest) -> func.HttpResponse:
    auth = _check_api_key(req)
    if auth:
        return auth

    light_id = req.route_params.get("light_id")
    body = _json(req)

    # Erlaubte Inputs:
    # { "on": true/false }
    # { "brightness": 0..100 }    -> dimming.brightness
    # { "color_xy": [x,y] }       -> color.xy.{x,y}
    # { "mirek": 153..500 }       -> color_temperature.mirek
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
            # In Hue v2 wird Farbe typischerweise als color.xy.{x,y} gepatcht. [1](https://www.postman.com/openhue/openhue-api/documentation/81b5lb4/hue-clip-api?entity=request-31084416-55cc62be-52a9-42fc-a08d-72829b778691)
            patch["color"] = {"xy": {"x": x, "y": y}}

        if "mirek" in body:
            patch["color_temperature"] = {"mirek": int(body["mirek"])}

        if not patch:
            return _bad_request("No supported fields in body. Use on/brightness/color_xy/mirek.")

        bridge = _get_bridge()
        result = bridge.update_light(light_id, patch)

        return func.HttpResponse(
            body=json.dumps({"result": result}, ensure_ascii=False),
            status_code=200,
            mimetype="application/json",
        )

    except Exception as ex:
        logging.exception("set_light_state failed")
        # Hue Fehlerdetails kommen in ex (ConnectionError mit status/response)
        return func.HttpResponse(
            body=json.dumps({"error": "Hue request failed", "detail": str(ex)}),
            status_code=502,
            mimetype="application/json",
        )