from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

# Public analysis contract: Brand/Non-Brand scope is no longer a feature of
# Page Analysis or Sitemap Intelligence. These names cover current and legacy
# spellings that may still exist in old run snapshots.
_BRAND_SCOPE_KEYS = {
    "brand scope",
    "brand_scope",
    "brandscope",
    "brand filter",
    "brand_filter",
    "brand scope filter",
    "brand_scope_filter",
    "brand classification",
    "brand_classification",
}


def _normalise_key(value: Any) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"[\s_-]+", " ", text)
    return text


def is_brand_scope_key(key: Any) -> bool:
    return _normalise_key(key) in {_normalise_key(x) for x in _BRAND_SCOPE_KEYS}


def strip_brand_scope(value: Any) -> Any:
    """Recursively remove legacy Brand Scope fields from analysis payloads.

    This is intentionally narrow: ordinary words such as a company name,
    product brand, or the word "brand" inside SEO content are preserved. Only
    the deprecated analysis/filter field itself is removed.
    """
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            if is_brand_scope_key(key):
                continue
            out[key] = strip_brand_scope(child)
        return out
    if isinstance(value, list):
        return [strip_brand_scope(x) for x in value]
    if isinstance(value, tuple):
        return tuple(strip_brand_scope(x) for x in value)
    return value


def sanitize_run_payload(value: Any) -> Any:
    """Return a detached sanitized copy safe for persistence/API/export."""
    return strip_brand_scope(deepcopy(value))
