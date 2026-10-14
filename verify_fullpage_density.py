#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import re

from app.services.crawler import Crawler
from app.services.text import exact_phrase_count, normalize, tokens


def overlapping_substring_count(text: str, phrase: str) -> int:
    hay = normalize(text)
    needle = normalize(phrase)
    if not hay or not needle:
        return 0
    count = start = 0
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
    out = []
    for match in pattern.finditer(text):
        left = max(0, match.start() - 90)
        right = min(len(text), match.end() + 90)
        out.append(" ".join(text[left:right].split()))
        if len(out) >= limit:
            break
    return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("phrases", nargs="+")
    args = parser.parse_args()

    results = await Crawler().crawl_many([(1, args.url)], concurrency=1)
    payload = results[0][1]
    structure = payload.get("structure") or {}
    body = payload.get("body_text") or ""

    print("URL              :", payload.get("url"))
    print("FINAL URL        :", payload.get("final_url"))
    print("HTTP             :", payload.get("status_code"))
    print("ERROR            :", payload.get("error"))
    print("WORDS            :", payload.get("word_count"))
    print("ROOT             :", structure.get("root"))
    print("EXTRACTION       :", structure.get("_extraction"))
    print("BROWSER ENV      :", structure.get("browser_environment"))
    print("FETCH            :", structure.get("_fetch"))
    print("RETOKENIZED WORDS:", len(tokens(body)))
    print("BODY CHARS       :", len(body))

    for phrase in args.phrases:
        exact = exact_phrase_count(body, phrase)
        substring = overlapping_substring_count(body, phrase)
        print()
        print("PHRASE           :", phrase)
        print("EXACT TOKEN COUNT:", exact)
        print("SUBSTRING COUNT  :", substring)
        print("FIRST MATCHES    :")
        for i, snippet in enumerate(snippets(body, phrase), 1):
            print(f"  {i}. {snippet}")


if __name__ == "__main__":
    asyncio.run(main())
