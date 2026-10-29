#!/usr/bin/env python3
"""Local load test for the Monet 3.0.0 demo gateway.

Two kinds of runs, both local-only (no upstream traffic, no billing):

  1. 生成压测   POST /v1/images/generations  with a warm keep-alive connection
  2. 健康探针压测 GET /health                (pure Python-side throughput ceiling)

    python stress_test.py -c 8 -n 200
    python stress_test.py -c 32 -n 1000 --verify-image
    python stress_test.py --health-bench -c 32 -n 3000

Exit code is non-zero when failures appear or when the observed mode is not the
one the gateway advertises, so it is usable as a smoke gate.
"""
from __future__ import annotations

import argparse
import http.client
import json
import statistics
import sys
import threading
import time
from urllib.parse import urlparse

PROMPTS = [
    "a lighthouse on a cliff at dusk, dramatic sky",
    "neon cyberpunk alley in the rain",
    "portrait of an astronaut, studio light",
    "isometric low poly island at sunrise",
    "food photography of a ramen bowl, top view",
]
STYLES = ["monet-3.0.0", "monet-3.0.0/realistic", "monet-3.0.0/cinematic", "monet-3.0.0/anime"]


class Recorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latencies: list[float] = []
        self.ok = 0
        self.failed = 0
        self.statuses: dict[int, int] = {}
        self.errors: list[str] = []

    def add(self, status: int, ms: float, err: str = "") -> None:
        with self.lock:
            self.latencies.append(ms)
            self.statuses[status] = self.statuses.get(status, 0) + 1
            if 200 <= status < 300:
                self.ok += 1
            else:
                self.failed += 1
                if err and len(self.errors) < 8:
                    self.errors.append(f"{status}: {err[:160]}")


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1)))))
    return round(ordered[k], 1)


def worker_generate(url: str, count: int, rec: Recorder, verify_image: bool, offset: int) -> None:
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=60)
    for i in range(count):
        idx = offset + i
        payload = json.dumps({
            "prompt": PROMPTS[idx % len(PROMPTS)],
            "n": 1 + (idx % 2),
            "size": "1024x1024" if idx % 3 else "512x512",
            "model": STYLES[idx % len(STYLES)],
        }).encode()
        t0 = time.perf_counter()
        try:
            conn.request("POST", "/v1/images/generations", body=payload,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
            ms = (time.perf_counter() - t0) * 1000
            if resp.status != 200:
                rec.add(resp.status, ms, body.decode("utf-8", "replace"))
                continue
            data = json.loads(body)
            if not data.get("data"):
                rec.add(0, ms, "empty data[]")
                continue
            if verify_image:
                img_path = urlparse(data["data"][0]["url"]).path
                conn.request("GET", img_path)
                img_resp = conn.getresponse()
                blob = img_resp.read()
                if img_resp.status != 200 or not blob.startswith(b"\x89PNG\r\n\x1a\n"):
                    rec.add(img_resp.status, ms, f"bad image ({len(blob)}B)")
                    continue
            rec.add(200, ms)
        except Exception as exc:  # noqa: BLE001
            rec.add(-1, (time.perf_counter() - t0) * 1000, f"{type(exc).__name__}: {exc}")
            conn.close()
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=60)
    conn.close()


def worker_health(url: str, count: int, rec: Recorder) -> None:
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=30)
    for _ in range(count):
        t0 = time.perf_counter()
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            resp.read()
            rec.add(resp.status, (time.perf_counter() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001
            rec.add(-1, (time.perf_counter() - t0) * 1000, f"{type(exc).__name__}: {exc}")
            conn.close()
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=30)
    conn.close()


def run(url: str, concurrency: int, total: int, verify_image: bool, health: bool) -> int:
    rec = Recorder()
    per_worker = total // concurrency
    threads = []
    started = time.perf_counter()
    for w in range(concurrency):
        count = per_worker + (1 if w < total % concurrency else 0)
        target = worker_health if health else worker_generate
        args = (url, count, rec) if health else (url, count, rec, verify_image, w * count)
        threads.append(threading.Thread(target=target, args=args, daemon=True))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started

    label = "health" if health else "generations"
    print(f"\n=== {label} : {rec.ok + rec.failed} req / c={concurrency} / {elapsed:.2f}s ===")
    print(f"  ok={rec.ok}  failed={rec.failed}  rps={len(rec.latencies) / elapsed:.1f}")
    print(f"  p50={pct(rec.latencies, 50)}ms  p95={pct(rec.latencies, 95)}ms  "
          f"p99={pct(rec.latencies, 99)}ms  max={round(max(rec.latencies), 1) if rec.latencies else 0}ms  "
          f"avg={round(statistics.fmean(rec.latencies), 1) if rec.latencies else 0}ms")
    print(f"  status histogram: {dict(sorted(rec.statuses.items()))}")
    for err in rec.errors:
        print(f"  ! {err}")
    return 0 if rec.failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Monet 3.0.0 demo gateway load test")
    ap.add_argument("--url", default="http://127.0.0.1:8931")
    ap.add_argument("-c", "--concurrency", type=int, default=8)
    ap.add_argument("-n", "--requests", type=int, default=100)
    ap.add_argument("--verify-image", action="store_true", help="also GET one image per response and check PNG magic")
    ap.add_argument("--health-bench", action="store_true", help="bench /health instead of generation")
    ap.add_argument("--ramp", default="", help="comma separated concurrency ladder, e.g. 1,4,8,16,32")
    args = ap.parse_args()

    if args.ramp:
        rc = 0
        for level in [int(x) for x in args.ramp.split(",") if x.strip()]:
            rc |= run(args.url, level, args.requests, args.verify_image, args.health_bench)
        return rc
    return run(args.url, args.concurrency, args.requests, args.verify_image, args.health_bench)


if __name__ == "__main__":
    sys.exit(main())
