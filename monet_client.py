#!/usr/bin/env python3
"""Monet 3.0.0 (co.appnation.artgenerator) upstream protocol client.

Everything here was recovered from the APK itself:

  * libapp.so (Dart AOT 3.4.3) decompiled with blutter  ->
    art_generator/data/service/art_service.dart        (routes + payload maps)
    art_generator/constant/network_constants.dart      (base urls)
    appnation_devkit/src/data/models/http/an_http_client.dart  (headers)
  * res/values/strings.xml -> google_api_key / google_app_id
  * Firebase Remote Config (public, no App Check) -> artStylesV2 style catalogue

Auth chain (identical to 3.5.5, verified live):

  POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=<API_KEY>
       {"returnSecureToken":true,"clientType":"CLIENT_TYPE_ANDROID"}   -> idToken

  every call carries
       Authorization:   Bearer <idToken>
       X-App-Id:        co.appnation.artgenerator
       X-App-Platform:  android
       X-App-Version:   3.0.0

Hard blocker: the Cloud Function (v2app / v2test) runs with **App Check
enforcement**. 3.0.0 does not even bundle firebase_app_check, so it cannot
produce `X-Firebase-AppCheck`; requests die at the Google front end with a
plain-text 401 before the Express app is reached. See docs/PROTOCOL.md §2.4.

Usage
-----
    export MONET_FIREBASE_KEY=...        # public key, also the default below
    python monet_client.py --selftest
    python monet_client.py --prompt "a cat" --style realistic
    python monet_client.py --prompt "a cat" --mock --out out.png
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

# --- constants recovered from the APK -----------------------------------------
FIREBASE_API_KEY = os.environ.get("MONET_FIREBASE_KEY", "AIzaSyABB3fgxYajNGJv40sqkbR5ZXni72TlbOQ")
PROJECT_ID = "art-generator-e907c"
FIREBASE_APP_ID = "1:238482661076:android:11aba563876e0276246541"
PACKAGE_NAME = "co.appnation.artgenerator"
DEFAULT_APP_VERSION = "3.0.0"
DEFAULT_BASE = "https://us-central1-art-generator-e907c.cloudfunctions.net/v2app"
TEST_BASE = "https://us-central1-art-generator-e907c.cloudfunctions.net/v2test"
MANAGEMENT_BASE = "https://management-api-production-vfywoevzkq-uc.a.run.app"
DEFAULT_PROXY = os.environ.get("MONET_PROXY", "")

ROUTE_WITHOUT_LORA = "/generate_without_lora"
ROUTE_WITH_LORA = "/generate_with_lora"
ROUTE_WITH_ILLUSION = "/generate_with_illusion"
ROUTE_VIDEO = "/generate_video"
ROUTE_NSFW = "/nsfw_detection"
ROUTE_UPSCALE = "/upscale"
ROUTE_SAVE_USER_ART = "/firebase/save_user_art"
ROUTE_SAVE_USER_ART_LOG = "/firebase/save_user_art_log"
ROUTE_PUBLISH = "/firebase/set_user_art_publish_request"
ROUTE_LIKE = "/firebase/like_or_unlike_user_art"   # 3.0.0-only, absent from 3.5.5

ALL_ROUTES = (
    ROUTE_WITHOUT_LORA, ROUTE_WITH_LORA, ROUTE_WITH_ILLUSION, ROUTE_VIDEO,
    ROUTE_NSFW, ROUTE_UPSCALE, ROUTE_SAVE_USER_ART, ROUTE_SAVE_USER_ART_LOG,
    ROUTE_PUBLISH, ROUTE_LIKE,
)


class MonetError(RuntimeError):
    def __init__(self, status: int, body: str, route: str):
        super().__init__(f"upstream {status} on {route}: {body[:300]}")
        self.status = status
        self.body = body
        self.route = route


def _opener() -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if DEFAULT_PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": DEFAULT_PROXY, "https": DEFAULT_PROXY}))
    return urllib.request.build_opener(*handlers)


class MonetClient:
    """Thin client for the Monet / appnation art-generation Cloud Function."""

    def __init__(
        self,
        id_token: str | None = None,
        refresh_token: str | None = None,
        appcheck_token: str | None = None,
        app_version: str = DEFAULT_APP_VERSION,
        base: str = DEFAULT_BASE,
        timeout: float = 120.0,
    ) -> None:
        self.base = base.rstrip("/")
        self.app_version = app_version
        self.id_token = id_token or os.environ.get("MONET_ID_TOKEN") or ""
        self.refresh_token = refresh_token or os.environ.get("MONET_REFRESH_TOKEN") or ""
        # Only a device that passes Play Integrity can mint this; there is no
        # offline forgery path. Kept as an env escape hatch for real-device capture.
        self.appcheck_token = appcheck_token or os.environ.get("MONET_APPCHECK_TOKEN") or ""
        self.timeout = timeout
        self._opener = _opener()
        self._expires_at = 0.0

    # ---------------------------------------------------------------- auth ----
    def _post_json(self, url: str, payload: dict, headers: dict | None = None, timeout: float | None = None) -> tuple[int, str]:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:
            return -1, f"{type(exc).__name__}: {exc}"

    def sign_in_anonymous(self) -> str:
        status, body = self._post_json(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_API_KEY}",
            {"returnSecureToken": True, "clientType": "CLIENT_TYPE_ANDROID"},
            timeout=30,
        )
        if status != 200:
            raise MonetError(status, body, "identitytoolkit:signUp")
        data = json.loads(body)
        self.id_token = data["idToken"]
        self.refresh_token = data.get("refreshToken", "")
        self._expires_at = time.time() + int(data.get("expiresIn", "3600")) - 60
        return self.id_token

    def _refresh(self) -> None:
        if not self.refresh_token:
            return self.sign_in_anonymous()
        status, body = self._post_json(
            f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}",
            {"grantType": "refresh_token", "refresh_token": self.refresh_token},
            timeout=30,
        )
        if status != 200:
            return self.sign_in_anonymous()
        data = json.loads(body)
        self.id_token = data["id_token"]
        self.refresh_token = data["refresh_token"]
        self._expires_at = time.time() + int(data["expires_in"]) - 60

    def ensure_token(self) -> str:
        if not self.id_token:
            self.sign_in_anonymous()
        elif self._expires_at and time.time() > self._expires_at:
            self._refresh()
        return self.id_token

    # ------------------------------------------------------------- request ----
    def headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.ensure_token()}",
            "X-App-Id": PACKAGE_NAME,
            "X-App-Platform": "android",
            "X-App-Version": self.app_version,
        }
        if self.appcheck_token:
            h["X-Firebase-AppCheck"] = self.appcheck_token
        return h

    def call(self, route: str, payload: dict, timeout: float | None = None) -> dict:
        status, body = self._post_json(self.base + route, payload, self.headers(), timeout)
        if status != 200:
            raise MonetError(status, body, route)
        return json.loads(body) if body.strip() else {}

    # ------------------------------------------------------------- methods ----
    def generate(
        self,
        prompt: str,
        *,
        num_images: int = 1,
        model_name: str | None = None,
        style_prompt: str | None = None,
        negative_prompt: str | None = None,
        width: int = 1024,
        height: int = 1024,
        prompt_strength: float | None = None,
        image_url: str | None = None,
        loras: list[dict] | None = None,
        seed: int | None = None,
        steps: int | None = None,
        route: str | None = None,
    ) -> dict:
        """POST /generate_without_lora | /generate_with_lora | /generate_with_illusion.

        Field set is exactly what ArtService builds in
        art_generator/data/service/art_service.dart (blutter 3.0.0).
        """
        payload: dict = {"prompt": prompt, "numImages": num_images, "width": width, "height": height}
        for key, value in (
            ("modelName", model_name), ("stylePrompt", style_prompt),
            ("negativePrompt", negative_prompt), ("promptStrength", prompt_strength),
            ("imageUrl", image_url), ("loras", loras), ("seed", seed), ("steps", steps),
        ):
            if value is not None:
                payload[key] = value
        if route is None:
            if loras:
                route = ROUTE_WITH_LORA
            elif image_url:
                route = ROUTE_WITH_ILLUSION
            else:
                route = ROUTE_WITHOUT_LORA
        return self.call(route, payload)

    def generate_video(self, prompt: str, **fields) -> dict:
        return self.call(ROUTE_VIDEO, {"prompt": prompt, **{k: v for k, v in fields.items() if v is not None}})

    def nsfw_detection(self, base64_image: str) -> dict:
        return self.call(ROUTE_NSFW, {"base64Image": base64_image})

    def upscale(self, image_url: str) -> dict:
        return self.call(ROUTE_UPSCALE, {"imageUrl": image_url})

    def save_user_art(self, user_id: str, **fields) -> dict:
        return self.call(ROUTE_SAVE_USER_ART, {"userId": user_id, **{k: v for k, v in fields.items() if v is not None}})

    def like_user_art(self, user_art_id: str, is_like: bool = True) -> dict:
        """3.0.0 only: /firebase/like_or_unlike_user_art {userArtId, isLike}."""
        return self.call(ROUTE_LIKE, {"userArtId": user_art_id, "isLike": is_like})

    # ---------------------------------------------------------------- probe ----
    def probe_routes(self) -> list[tuple[str, int, str]]:
        """Hit every route with an empty body; records status + body prefix."""
        out = []
        for route in ALL_ROUTES:
            try:
                self.call(route, {})
                out.append((route, 200, "ok"))
            except MonetError as exc:
                out.append((route, exc.status, exc.body[:80]))
        return out


class MockMonetClient:
    """Deterministic offline double: same surface, renders real PNG bytes.

    Used by demo_server.py / stress_test.py so the gateway can be exercised
    without touching (or billing) the upstream.
    """

    ROUTES = ALL_ROUTES

    def __init__(self, latency_ms: int = 0) -> None:
        self.latency_ms = latency_ms
        self.app_version = DEFAULT_APP_VERSION
        self.appcheck_token = ""

    def ensure_token(self) -> str:
        return "mock-id-token"

    def call(self, route: str, payload: dict) -> dict:
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000.0)
        if route in (ROUTE_WITHOUT_LORA, ROUTE_WITH_LORA, ROUTE_WITH_ILLUSION, ROUTE_VIDEO):
            n = max(1, int(payload.get("numImages") or 1))
            width = int(payload.get("width") or 1024)
            height = int(payload.get("height") or 1024)
            seed = json.dumps(payload, sort_keys=True)
            urls = [
                f"/mock/{_mock_name(seed, i)}.png?w={width}&h={height}&p={urllib.parse.quote(payload.get('prompt', '')[:60])}"
                for i in range(n)
            ]
            return {
                "id": _mock_name(seed, 0),
                "imageUrls": urls,
                "imageUrl": urls[0],
                "prompt": payload.get("prompt"),
                "numImages": n,
                "width": width,
                "height": height,
                "mediaType": "video" if route == ROUTE_VIDEO else "image",
                "_mock": True,
            }
        if route == ROUTE_UPSCALE:
            return {"imageUrl": payload.get("imageUrl"), "_mock": True}
        if route == ROUTE_NSFW:
            return {"isNsfw": False, "_mock": True}
        return {"ok": True, "_mock": True}

    def generate(self, prompt: str, **kwargs) -> dict:
        route = ROUTE_WITH_LORA if kwargs.get("loras") else ROUTE_WITHOUT_LORA
        return self.call(route, {"prompt": prompt, "numImages": kwargs.get("num_images", 1), **kwargs})

    def generate_video(self, prompt: str, **fields) -> dict:
        return self.call(ROUTE_VIDEO, {"prompt": prompt, **fields})

    def probe_routes(self) -> list[tuple[str, int, str]]:
        return [(r, 200, "ok") for r in ALL_ROUTES]


def _mock_name(seed: str, idx: int) -> str:
    return hashlib.sha256(f"{seed}|{idx}".encode()).hexdigest()[:16]


def render_mock_png(prompt: str, width: int = 1024, height: int = 1024) -> bytes:
    """Deterministic placeholder image; real bytes so browsers/clients accept it."""
    from PIL import Image, ImageDraw

    width = max(64, min(width, 2048))
    height = max(64, min(height, 2048))
    digest = hashlib.sha256(prompt.encode()).digest()
    top = (digest[0], digest[1], digest[2])
    bottom = (digest[3], digest[4], digest[5])

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / max(1, height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    box = max(2, width // 160)
    draw.rectangle([box, box, width - box, height - box], outline=(255, 255, 255), width=max(1, box // 2))
    text = (prompt or "monet-mock")[:120]
    draw.text((box * 4, box * 4), "MOCK / monet 3.0.0", fill=(255, 255, 255))
    draw.text((box * 4, box * 4 + 18), text, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def extract_image_urls(payload: dict) -> list[str]:
    """Pull image links out of an upstream response whose exact shape is unconfirmed."""
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, str):
            if node.startswith("http") and any(k in node for k in (".png", ".jpg", ".jpeg", ".webp", "alt=media", "/o/")):
                found.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for key, value in node.items():
                if key in ("imageUrls", "imageUrl", "images", "urls", "url", "output", "data"):
                    walk(value)
                elif isinstance(value, (dict, list)):
                    walk(value)

    walk(payload)
    return list(dict.fromkeys(found))


def _load_style(key: str) -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "styles.json")
    styles = json.load(open(path, encoding="utf-8"))
    match = next((s for s in styles if s.get("key") == key), None)
    if not match:
        raise SystemExit(f"unknown style {key!r}; available: {sorted({s['key'] for s in styles})}")
    return match


def main() -> int:
    ap = argparse.ArgumentParser(description="Monet 3.0.0 upstream client")
    ap.add_argument("--prompt")
    ap.add_argument("--style", default="")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--mock", action="store_true", help="use the offline double")
    ap.add_argument("--selftest", action="store_true", help="auth + route probe only")
    args = ap.parse_args()

    client = MockMonetClient() if args.mock else MonetClient()

    if args.selftest:
        client.ensure_token()
        print("[ok] firebase idToken acquired" if not args.mock else "[ok] mock token")
        print(f"[i] appcheck token: {'present' if client.appcheck_token else 'MISSING (upstream will answer 401)'}")
        for route, status, body in client.probe_routes():
            print(f"  {route:38s} {status} {body!r}")
        return 0

    if not args.prompt:
        ap.error("--prompt is required unless --selftest")

    style = _load_style(args.style) if args.style else {}
    result = client.generate(
        args.prompt,
        num_images=args.n,
        model_name=style.get("modelName"),
        style_prompt=style.get("stylePrompt"),
        negative_prompt=style.get("negativePrompt"),
        loras=style.get("loras"),
        width=args.width,
        height=args.height,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2)[:2000])
    urls = extract_image_urls(result)
    print(f"[i] images: {len(urls)}")
    for u in urls:
        print("   ", u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
