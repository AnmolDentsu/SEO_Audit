#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import re

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from app.services.crawler import (
    HYDRATE_JS,
    PAGE_HYDRATION_MAX_ELAPSED_MS,
    PAGE_HYDRATION_SCROLL_WAIT_MS,
    PAGE_HYDRATION_SETTLE_MS,
    PAGE_HYDRATION_STABLE_ROUNDS,
    PAGE_HYDRATION_STEP_RATIO,
)
from app.services.text import exact_phrase_count, normalize, tokens


def overlapping_substring_count(text: str, phrase: str) -> int:
    hay = normalize(text)
    needle = normalize(phrase)
    if not hay or not needle:
        return 0
    count = 0
    start = 0
    while True:
        idx = hay.find(needle, start)
        if idx < 0:
            return count
        count += 1
        start = idx + 1


def snippets(text: str, phrase: str, limit: int = 8) -> list[str]:
    if not text or not phrase:
        return []
    pattern = re.compile(re.escape(phrase), re.I)
    out: list[str] = []
    for match in pattern.finditer(text):
        left = max(0, match.start() - 90)
        right = min(len(text), match.end() + 90)
        out.append(" ".join(text[left:right].split()))
        if len(out) >= limit:
            break
    return out


async def browser_find_count(page, phrase: str) -> int | None:
    return await page.evaluate(
        """phrase => {
            if (typeof window.find !== 'function' || !document.body) return null;

            const selection = window.getSelection();
            selection.removeAllRanges();

            const start = document.createRange();
            start.selectNodeContents(document.body);
            start.collapse(true);
            selection.addRange(start);

            let count = 0;
            while (window.find(phrase, false, false, false, false, false, false)) {
                count += 1;
                if (count > 100000) break;
            }

            selection.removeAllRanges();
            return count;
        }""",
        phrase,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit adaptive full-page hydration and keyword counts."
    )
    parser.add_argument("url")
    parser.add_argument("phrases", nargs="+")
    args = parser.parse_args()

    async with async_playwright() as pw:
        browser = None
        browser_mode = None
        errors: list[str] = []

        for kwargs, mode in [
            ({"channel": "chrome", "headless": True}, "google-chrome-new-headless"),
            ({"channel": "chromium", "headless": True}, "chromium-new-headless"),
            ({"headless": True}, "playwright-headless-shell"),
        ]:
            try:
                browser = await pw.chromium.launch(**kwargs)
                browser_mode = mode
                break
            except Exception as exc:
                errors.append(f"{mode}: {type(exc).__name__}: {exc}")

        if browser is None:
            raise RuntimeError("Could not launch Chromium: " + " | ".join(errors))

        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()

        response = await page.goto(
            args.url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            pass

        hydration = await page.evaluate(
            HYDRATE_JS,
            {
                "stable_rounds": PAGE_HYDRATION_STABLE_ROUNDS,
                "max_elapsed_ms": PAGE_HYDRATION_MAX_ELAPSED_MS,
                "settle_ms": PAGE_HYDRATION_SETTLE_MS,
                "per_scroll_wait_ms": PAGE_HYDRATION_SCROLL_WAIT_MS,
                "step_ratio": PAGE_HYDRATION_STEP_RATIO,
            },
        )

        inner_text = await page.locator("body").inner_text()
        text_content = await page.locator("body").text_content() or ""

        print("URL                 :", args.url)
        print("FINAL URL           :", page.url)
        print("HTTP                :", response.status if response else None)
        print("BROWSER MODE        :", browser_mode)
        print("UA                  :", await page.evaluate("navigator.userAgent"))
        print("WEBDRIVER           :", await page.evaluate("navigator.webdriver"))
        print("VIEWPORT            :", await page.evaluate("`${innerWidth}x${innerHeight}`"))
        print()
        print("===== ADAPTIVE HYDRATION =====")
        for key, value in hydration.items():
            print(f"{key:24}: {value}")
        print()
        print("===== CORPUS =====")
        print("innerText words     :", len(tokens(inner_text)))
        print("innerText chars     :", len(inner_text))
        print("textContent words   :", len(tokens(text_content)))
        print("textContent chars   :", len(text_content))

        for phrase in args.phrases:
            exact = exact_phrase_count(inner_text, phrase)
            substring = overlapping_substring_count(inner_text, phrase)
            find_count = await browser_find_count(page, phrase)
            dom_substring = overlapping_substring_count(text_content, phrase)

            print()
            print("PHRASE              :", phrase)
            print("EXACT TOKEN COUNT   :", exact)
            print("innerText SUBSTRING :", substring)
            print("window.find COUNT   :", find_count)
            print("textContent SUBSTR  :", dom_substring)
            print("FIRST innerText MATCHES:")
            for i, item in enumerate(snippets(inner_text, phrase), 1):
                print(f"  {i}. {item}")

        if not hydration.get("complete"):
            print()
            print("WARNING: hydration did not converge before the safety time budget.")
            print("The page may be infinite/ever-growing. Counts are a bounded snapshot,")
            print("not an exhaustive whole-feed count.")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
