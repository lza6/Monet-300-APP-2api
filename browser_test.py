#!/usr/bin/env python3
"""Real-browser E2E for the Monet 3.0.0 demo page (Playwright / Chromium).

    python demo_server.py --mode mock --port 8951 &
    python browser_test.py --url http://127.0.0.1:8951

Asserts the actual user path: page loads -> model list populated -> click 生成
-> an <img> appears with non-zero naturalWidth -> stats counters move.
Writes browser_demo.png as evidence. Exits non-zero on any failed assertion.
"""
from __future__ import annotations

import argparse
import sys

from playwright.sync_api import expect, sync_playwright


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8951")
    ap.add_argument("--shot", default="browser_demo.png")
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args()

    failures: list[str] = []

    def check(name: str, fn) -> None:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {exc}")
            print(f"  FAIL  {name} -> {exc}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headful)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.goto(args.url, wait_until="domcontentloaded")

        check("page title", lambda: expect(page).to_have_title("Monet 3.0.0 · 本地网关 Demo"))
        check("mode pill = mock", lambda: expect(page.locator("#pill-mode")).to_contain_text("mock"))
        check("prompt prefilled", lambda: expect(page.locator("#prompt")).not_to_be_empty())
        # 23 entries in artStylesV2, but `foodDesign` is duplicated upstream, so the
        # gateway exposes 22 unique style keys + 1 "no style" option = 23 options.
        check("style list populated (23 options)",
              lambda: expect(page.locator("#style option")).to_have_count(23, timeout=5000))
        check("health panel has status",
              lambda: expect(page.locator("#health")).to_contain_text("ok", timeout=5000))

        before = page.locator("#grid figure").count()
        page.fill("#prompt", "a neon cyberpunk alley in the rain, e2e")
        page.select_option("#style", "monet-3.0.0/realistic")
        page.fill("#n", "2")
        page.select_option("#size", "512x512")
        page.click("#go")

        check("two results rendered",
              lambda: expect(page.locator("#grid figure")).to_have_count(before + 2, timeout=20000))
        check("first image has real pixels", lambda: expect(page.locator("#grid figure img").first).to_have_js_property(
            "naturalWidth", 512))
        check("log reports 200", lambda: expect(page.locator("#log")).to_contain_text("200 OK"))
        check("stats total >= 1", lambda: expect(page.locator("#stats")).not_to_contain_text("total0"))

        page.screenshot(path=args.shot, full_page=True)
        print(f"  [i] screenshot -> {args.shot}")
        browser.close()

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("\nE2E PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
