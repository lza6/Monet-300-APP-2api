#!/usr/bin/env python3
"""Monet 3.0.0 — local OpenAI-compatible gateway + demo page.

    python demo_server.py --mode mock            # offline, always works
    python demo_server.py --mode real            # real upstream (blocked by App Check, see below)
    python demo_server.py --mode auto            # real first, transparent mock fallback

Endpoints
    GET  /                        demo UI (demo.html)
    GET  /health                  liveness + mode + upstream state
    GET  /stats                   counters + latency percentiles
    GET  /v1/models               OpenAI model list (monet-3.0.0 + per-style ids)
    POST /v1/images/generations   OpenAI images API  {prompt,n,size,model,style}
    GET  /mock/<id>.png           deterministic placeholder bytes (mock mode)

Why `real` cannot succeed from a PC: the Cloud Function enforces App Check.
Requests without a Play-Integrity-backed `X-Firebase-AppCheck` token get a
plain-text 401 from the Google front end before the Express app runs, and the
3.0.0 build does not even ship firebase_app_check. `/v1/images/generations`
therefore returns 503 `upstream_app_check_required` in real mode. This server
is the *client-side* half of the chain; the mock mode is what the local stress
test and demo page exercise.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from monet_client import (
    DEFAULT_APP_VERSION,
    DEFAULT_BASE,
    MonetClient,
    MonetError,
    MockMonetClient,
    extract_image_urls,
    render_mock_png,
)

MAX_N = 4                     # billing safety: bound the image multiplier
MIN_DIM, MAX_DIM = 256, 2048
HERE = os.path.dirname(os.path.abspath(__file__))
STYLES = json.load(open(os.path.join(HERE, "styles.json"), encoding="utf-8"))
STYLE_BY_KEY = {}
for _s in STYLES:
    STYLE_BY_KEY.setdefault(_s["key"], _s)

STATE = {
    "mode": "mock",
    "started": time.time(),
    "total": 0,
    "ok": 0,
    "failed": 0,
    "mock_served": 0,
    "real_ok": 0,
    "latencies": [],
    "last_error": "",
}
STATE_LOCK = threading.Lock()
CONCURRENCY = threading.BoundedSemaphore(int(os.environ.get("MONET_MAX_CONCURRENCY", "8")))


def _record(ok: bool, latency_ms: float, served: str, error: str = "") -> None:
    with STATE_LOCK:
        STATE["total"] += 1
        STATE["ok" if ok else "failed"] += 1
        STATE["latencies"].append(latency_ms)
        if len(STATE["latencies"]) > 5000:
            del STATE["latencies"][:2000]
        if served == "mock":
            STATE["mock_served"] += 1
        elif served == "real":
            STATE["real_ok"] += 1
        if error:
            STATE["last_error"] = error


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return round(ordered[k], 1)


def _parse_size(size: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{2,4})x(\d{2,4})", (size or "").strip())
    if not m:
        return 1024, 1024
    w, h = int(m.group(1)), int(m.group(2))
    clamp = lambda v: max(MIN_DIM, min(MAX_DIM, v))  # noqa: E731
    return clamp(w), clamp(h)


class Handler(BaseHTTPRequestHandler):
    server_version = "monet-3.0.0-demo"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- helpers ----
    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict, extra: dict | None = None) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def _error(self, code: int, message: str, etype: str = "upstream_error", ecode: str = "") -> None:
        self._json(code, {"error": {"message": message, "type": etype, "code": ecode or etype, "param": None}})

    def log_message(self, fmt: str, *args) -> None:  # keep the console readable
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------------------------------------------------------------- verbs ----
    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            page = os.path.join(HERE, "demo.html")
            if not os.path.exists(page):
                return self._error(404, "demo.html missing", "not_found")
            return self._send(200, open(page, "rb").read(), "text/html; charset=utf-8")

        if path == "/health":
            upstream = "appcheck_blocked" if STATE["mode"] in ("real", "auto") else "mock"
            return self._json(200, {
                "status": "ok", "mode": STATE["mode"], "upstream": upstream,
                "app_version": DEFAULT_APP_VERSION, "base": DEFAULT_BASE,
                "uptime_s": round(time.time() - STATE["started"], 1),
            })

        if path == "/stats":
            with STATE_LOCK:
                lat = list(STATE["latencies"])
                return self._json(200, {
                    "mode": STATE["mode"], "total": STATE["total"], "ok": STATE["ok"],
                    "failed": STATE["failed"], "real_ok": STATE["real_ok"],
                    "mock_served": STATE["mock_served"],
                    "p50_ms": _percentile(lat, 50), "p95_ms": _percentile(lat, 95),
                    "p99_ms": _percentile(lat, 99),
                    "avg_ms": round(sum(lat) / len(lat), 1) if lat else 0.0,
                    "last_error": STATE["last_error"],
                })

        if path == "/v1/models":
            data = [{"id": f"monet-3.0.0", "object": "model", "created": int(STATE["started"]), "owned_by": "appnation"}]
            data += [
                {"id": f"monet-3.0.0/{k}", "object": "model", "created": int(STATE["started"]), "owned_by": "appnation"}
                for k in sorted(STYLE_BY_KEY)
            ]
            return self._json(200, {"object": "list", "data": data})

        if path.startswith("/mock/"):
            qs = parse_qs(urlparse(self.path).query)
            prompt = (qs.get("p") or ["monet-mock"])[0]
            w = int((qs.get("w") or ["1024"])[0])
            h = int((qs.get("h") or ["1024"])[0])
            png = render_mock_png(prompt, w, h)
            return self._send(200, png, "image/png", {"Cache-Control": "public, max-age=600"})

        return self._error(404, f"unknown path {path}", "not_found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/v1/images/generations":
            return self._error(404, f"unknown path {path}", "not_found")

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, f"invalid JSON body: {exc}", "invalid_request_error")

        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self._error(400, "prompt is required", "invalid_request_error")
        try:
            n = int(body.get("n") or 1)
        except (TypeError, ValueError):
            return self._error(400, "n must be an integer", "invalid_request_error")
        if not 1 <= n <= MAX_N:
            return self._error(400, f"n must be between 1 and {MAX_N}", "invalid_request_error")

        width, height = _parse_size(body.get("size", "1024x1024"))
        model = body.get("model") or "monet-3.0.0"
        style_key = model.split("/", 1)[1] if "/" in model else (body.get("style") or "")
        style = STYLE_BY_KEY.get(style_key, {})

        with CONCURRENCY:
            started = time.time()
            try:
                result, served = _run_generation(prompt, n, width, height, style)
            except MonetError as exc:
                _record(False, (time.time() - started) * 1000, "", str(exc))
                if exc.status in (401, 403):
                    return self._error(
                        503,
                        "upstream rejects the request: App Check enforcement on the Cloud Function "
                        "(missing/invalid X-Firebase-AppCheck). Monet 3.0.0 does not bundle "
                        "firebase_app_check, so real generation is unreachable from a PC. "
                        "Use --mode mock.",
                        "upstream_app_check_required", "appcheck_required",
                    )
                return self._error(502, f"upstream {exc.status}: {exc.body[:200]}", "upstream_error", str(exc.status))
            except Exception as exc:  # noqa: BLE001 - surfaced to the client, never swallowed
                _record(False, (time.time() - started) * 1000, "", repr(exc))
                return self._error(502, f"{type(exc).__name__}: {exc}", "upstream_error")

            latency_ms = (time.time() - started) * 1000
            _record(True, latency_ms, served)
            urls = extract_image_urls(result) or result.get("imageUrls") or []
            host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"
            data = [
                {"url": (u if u.startswith("http") else f"http://{host}{u}"), "revised_prompt": prompt}
                for u in urls
            ]
            return self._json(200, {
                "created": int(time.time()), "data": data,
                "model": model, "mode": served,
                "latency_ms": round(latency_ms, 1),
            }, extra={"X-Monet-Mode": served})


def _run_generation(prompt: str, n: int, width: int, height: int, style: dict) -> tuple[dict, str]:
    """real first when the mode allows it, mock as the documented fallback."""
    mode = STATE["mode"]
    if mode in ("real", "auto"):
        client = MonetClient()
        try:
            return client.generate(
                prompt,
                num_images=n,
                model_name=style.get("modelName"),
                style_prompt=style.get("stylePrompt"),
                negative_prompt=style.get("negativePrompt"),
                loras=style.get("loras"),
                width=width,
                height=height,
            ), "real"
        except MonetError as exc:
            if mode == "real" or exc.status not in (401, 403, 503, -1):
                raise
            with STATE_LOCK:
                STATE["last_error"] = f"real failed ({exc.status}), served mock: {exc.body[:120]}"
    result = MockMonetClient().generate(
        prompt,
        num_images=n,
        model_name=style.get("modelName"),
        style_prompt=style.get("stylePrompt"),
        loras=style.get("loras"),
        width=width,
        height=height,
    )
    return result, "mock"


def main() -> int:
    ap = argparse.ArgumentParser(description="Monet 3.0.0 local gateway")
    ap.add_argument("--mode", choices=("auto", "real", "mock"), default=os.environ.get("MONET_MODE", "mock"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MONET_PORT", "8931")))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    STATE["mode"] = args.mode
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[monet300] mode={args.mode} on http://{args.host}:{args.port}  (demo page: /)")
    if args.mode != "mock":
        print("[monet300] note: real/auto upstream is App Check gated; expect "
              "503 upstream_app_check_required unless MONET_APPCHECK_TOKEN is a real device token.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[monet300] bye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
