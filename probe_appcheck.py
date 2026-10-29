#!/usr/bin/env python3
"""Monet 3.0.0 — App Check / reachability probe.

Produces evidence used in docs/PROTOCOL.md and README.md. Read-only: the only
upstream writes are the anonymous Firebase sign-up and empty-body probes; no
generation request is issued (that would bill the app owner and is blocked anyway).

    python probe_appcheck.py            # prints matrix, writes evidence/appcheck_matrix.txt
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

FIREBASE_API_KEY = os.environ.get("MONET_FIREBASE_KEY", "AIzaSyABB3fgxYajNGJv40sqkbR5ZXni72TlbOQ")
PACKAGE = "co.appnation.artgenerator"
CF = "https://us-central1-art-generator-e907c.cloudfunctions.net"
MGMT = "https://management-api-production-vfywoevzkq-uc.a.run.app"
PROXY = os.environ.get("MONET_PROXY", "")


def _opener():
    if PROXY:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return urllib.request.build_opener()


OPENER = _opener()


def post(url: str, body: dict | None, headers: dict | None = None, timeout: float = 25) -> tuple[int, str, dict]:
    data = json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:  # network / TLS / proxy
        return -1, f"{type(e).__name__}: {e}", {}


def main() -> int:
    lines: list[str] = []

    def log(msg: str) -> None:
        print(msg)
        lines.append(msg)

    log(f"# Monet 3.0.0 probe — {time.strftime('%Y-%m-%d %H:%M:%S')}")

    st, body, _ = post(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_API_KEY}",
        {"returnSecureToken": True, "clientType": "CLIENT_TYPE_ANDROID"},
    )
    if st != 200:
        log(f"[FATAL] anonymous sign-up failed: {st} {body[:200]!r}")
        return 1
    ident = json.loads(body)
    token = ident["idToken"]
    log(f"# anonymous sign-up: 200 uid={ident['localId']}")

    matrix = [
        ("v2app /status", CF + "/v2app/status", "3.0.0"),
        ("v2app /status", CF + "/v2app/status", "3.5.5"),
        ("v2app /generate_without_lora", CF + "/v2app/generate_without_lora", "3.0.0"),
        ("v2app /definitely_not_a_route", CF + "/v2app/definitely_not_a_route", "3.0.0"),
        ("v2test /status", CF + "/v2test/status", "3.0.0"),
        ("v2test /generate_without_lora", CF + "/v2test/generate_without_lora", "3.0.0"),
        ("mgmt /v1/rate", MGMT + "/v1/rate", "3.0.0"),
        ("mgmt /v1/feedback", MGMT + "/v1/feedback", "3.0.0"),
    ]
    for label, url, version in matrix:
        for appcheck in (None, "bogus-token"):
            h = {
                "Authorization": f"Bearer {token}",
                "X-App-Id": PACKAGE,
                "X-App-Platform": "android",
                "X-App-Version": version,
            }
            if appcheck:
                h["X-Firebase-AppCheck"] = appcheck
            code, resp, hdr = post(url, {}, h)
            log(
                f"{label:32s} ver={version} appcheck={str(appcheck):12s} -> {code} "
                f"{resp[:90]!r} fn-exec-id={hdr.get('Function-Execution-Id')} server={hdr.get('Server')}"
            )

    log("")
    log("# X-App-Id whitelist probe on management-api (Cloud Run)")
    for val in (None, "", PACKAGE, "artgenerator", "monet", "Monet"):
        h = {"Authorization": f"Bearer {token}", "X-App-Platform": "android", "X-App-Version": "3.0.0"}
        if val is not None:
            h["X-App-Id"] = val
        code, resp, _ = post(MGMT + "/v1/feedback", {"message": "probe"}, h)
        log(f"X-App-Id={val!r:34s} -> {code} {resp[:110]!r}")

    log("")
    log("# Cloud Functions name enumeration (404 = name free, 401 = exists + App Check gated)")
    for name in ("v1", "v2", "api", "app", "v2app", "v2test", "v2prod", "v3app", "v2api", "generate", "webhook", "admin"):
        code, resp, _ = post(f"{CF}/{name}", {}, {"Content-Type": "application/json"})
        tag = "HTML-404" if code == 404 else resp[:40]
        log(f"/{name:12s} -> {code} {tag!r}")

    os.makedirs("evidence", exist_ok=True)
    with open("evidence/appcheck_matrix.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n[ok] evidence/appcheck_matrix.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
