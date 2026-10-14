from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Hashable, Iterable

TOKEN_RE = re.compile(r"[\w]+(?:[-'][\w]+)*", re.UNICODE)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return " ".join(TOKEN_RE.findall(text))


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(unicodedata.normalize("NFKC", text or "").lower())


def exact_phrase_count(text: str, phrase: str) -> int:
    """Count exact normalized phrase occurrences, including overlapping matches."""
    hay = tokens(text)
    needle = tokens(phrase)
    if not hay or not needle or len(needle) > len(hay):
        return 0
    n = len(needle)
    return sum(1 for i in range(len(hay) - n + 1) if hay[i : i + n] == needle)


def density_pct(text: str, phrase: str) -> tuple[int, float, int]:
    """Return exact phrase count and phrase-occurrence density.

    Density (%) = exact phrase occurrences / total extracted words * 100

    A multi-word keyword is treated as one complete keyword phrase occurrence.
    The number of words inside the keyword is NOT multiplied into density.

    Example: "home loan" occurs twice in an 8-word page:
        density = 2 / 8 * 100 = 25%.
    """
    body_tokens = tokens(text)
    needle = tokens(phrase)
    if not body_tokens or not needle:
        return 0, 0.0, len(body_tokens)

    n = len(needle)
    count = sum(
        1
        for i in range(len(body_tokens) - n + 1)
        if body_tokens[i : i + n] == needle
    )
    density = (count / len(body_tokens)) * 100.0
    return count, round(density, 4), len(body_tokens)


@dataclass(frozen=True)
class PreparedKeyword:
    key: Hashable
    phrase: str
    phrase_tokens: tuple[str, ...]


class BatchKeywordAnalyzer:
    """Analyze many exact phrases against a page with one tokenization pass.

    Old complexity pattern:
        page -> for every keyword -> tokenize/scan page again

    New pattern:
        page -> tokenize once -> build only required n-gram counters -> O(1) lookup

    The page is scanned once per UNIQUE keyword length, not once per keyword.
    For typical SEO keyword lists (mostly 1-5 word phrases), this is a large
    improvement when thousands of keywords are checked against a few URLs.
    """

    def __init__(self, keyword_items: Iterable[tuple[Hashable, str]]):
        prepared: list[PreparedKeyword] = []
        lengths: set[int] = set()
        by_length: dict[int, set[tuple[str, ...]]] = defaultdict(set)

        for key, phrase in keyword_items:
            phrase_tokens = tuple(tokens(phrase))
            prepared.append(PreparedKeyword(key, phrase, phrase_tokens))
            if phrase_tokens:
                n = len(phrase_tokens)
                lengths.add(n)
                by_length[n].add(phrase_tokens)

        self.prepared = prepared
        self.lengths = tuple(sorted(lengths))
        self.by_length = by_length

    def analyze(self, text: str) -> dict[Hashable, tuple[int, float, int]]:
        page_tokens = tokens(text)
        total_words = len(page_tokens)

        if total_words == 0:
            return {item.key: (0, 0.0, 0) for item in self.prepared}

        # Build counters only for n-gram lengths that actually occur in input keywords.
        counters: dict[int, Counter] = {}
        for n in self.lengths:
            if n > total_words:
                counters[n] = Counter()
                continue

            wanted = self.by_length[n]
            counter = Counter()
            # Only retain n-grams that correspond to an input keyword. This avoids
            # storing every unique n-gram from very large pages.
            for i in range(total_words - n + 1):
                gram = tuple(page_tokens[i : i + n])
                if gram in wanted:
                    counter[gram] += 1
            counters[n] = counter

        results: dict[Hashable, tuple[int, float, int]] = {}
        for item in self.prepared:
            if not item.phrase_tokens:
                count = 0
            else:
                count = counters.get(len(item.phrase_tokens), Counter()).get(
                    item.phrase_tokens, 0
                )

            # Phrase-occurrence density:
            # exact phrase occurrences / total page words * 100.
            # A multi-word keyword is treated as one complete keyword phrase.
            density = (count / total_words) * 100.0
            results[item.key] = (count, round(density, 4), total_words)

        return results


def jaccard_similarity(a: str, b: str) -> float:
    sa, sb = set(tokens(a)), set(tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
