#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio

from app.services.crawler import Crawler
from app.services.text import exact_phrase_count, tokens


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crawl one page with the production Page Analysis crawler and print exact visible-text keyword counts."
    )
    parser.add_argument("url")
    parser.add_argument("phrases", nargs="+", help="Exact keyword phrases to count")
    args = parser.parse_args()

    results = await Crawler().crawl_many([(1, args.url)], concurrency=1)
    payload = results[0][1]

    print("URL              :", payload.get("url"))
    print("FINAL URL        :", payload.get("final_url"))
    print("HTTP             :", payload.get("status_code"))
    print("ERROR            :", payload.get("error"))
    print("WORDS            :", payload.get("word_count"))
    structure = payload.get("structure") or {}
    print("ROOT             :", structure.get("root"))
    print("EXTRACTION       :", structure.get("_extraction"))
    print("FETCH            :", structure.get("_fetch"))

    body = payload.get("body_text") or ""
    print("RETOKENIZED WORDS:", len(tokens(body)))

    for phrase in args.phrases:
        print(f"EXACT {phrase!r}: {exact_phrase_count(body, phrase)}")


if __name__ == "__main__":
    asyncio.run(main())
