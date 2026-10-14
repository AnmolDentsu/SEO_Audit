from __future__ import annotations

import re
from collections import Counter, defaultdict
from urllib.parse import urlparse

from app.config import get_settings

settings = get_settings()

# Statuses where a browser/TLS impersonation can genuinely change the outcome.
# HTTP 429 is intentionally NOT here: it is a request-rate problem and is handled
# by the adaptive host scheduler + Retry-After support in sitemap_page_resolver.py.
_BROWSER_HELPFUL_STATUSES = {401, 403, 406}
_IMPERSONATION_HELPFUL_STATUSES = {401, 403, 406}
_TRANSIENT_RETRY_ONLY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_STALE_STATUSES = {404, 410}

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)
_HEXISH_RE = re.compile(r"^[0-9a-f]{12,}$", re.I)
_NUMBER_RE = re.compile(r"^\d{3,}$")
_DATE_RE = re.compile(r"^(?:19|20)\d{2}(?:[-_]?\d{1,2}){0,2}$")


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def url_family_key(url: str) -> str:
    """Create a conservative URL-family key for fallback sampling only.

    This key NEVER classifies or infers page content. It is used only to avoid
    spending Chromium time on hundreds/thousands of near-identical failed URLs.
    Dynamic-looking path segments are replaced by placeholders.
    """
    parsed = urlparse(url or "")
    parts = [p for p in parsed.path.split("/") if p]
    normalized: list[str] = []

    for idx, part in enumerate(parts):
        low = part.casefold()
        stem = low.rsplit(".", 1)[0] if "." in low else low

        if _UUID_RE.match(stem):
            normalized.append("{uuid}")
            continue
        if _HEXISH_RE.match(stem):
            normalized.append("{hash}")
            continue
        if _NUMBER_RE.match(stem):
            normalized.append("{id}")
            continue
        if _DATE_RE.match(stem):
            normalized.append("{date}")
            continue

        # Deep final slugs are usually page identifiers. Replacing only the final
        # segment keeps useful parent-family structure such as /blog/{slug},
        # /jobs/job-details/{slug}, /products/{slug}, etc.
        is_last = idx == len(parts) - 1
        looks_sluggy = len(stem) >= 24 or stem.count("-") >= 3 or stem.count("_") >= 3
        if is_last and len(parts) >= 2 and looks_sluggy:
            suffix = "." + low.rsplit(".", 1)[1] if "." in low else ""
            normalized.append("{slug}" + suffix)
            continue

        normalized.append(low)

    path = "/" + "/".join(normalized)
    return f"{_host(url)}{path or '/'}"


def should_impersonation_fallback(evidence: dict) -> bool:
    if not settings.sitemap_impersonation_fallback_enabled:
        return False
    if evidence.get("non_html"):
        return False
    if not evidence.get("error"):
        return False
    status = evidence.get("status_code")
    if evidence.get("terminal_state") == "security_challenge" or evidence.get("challenge_detected"):
        return True
    if status in _IMPERSONATION_HELPFUL_STATUSES:
        return True
    # Transport/TLS errors have no status. A browser-fingerprint HTTP client can
    # sometimes recover them much more cheaply than Chromium, so allow them but
    # they will still be globally capped.
    return status is None


def _browser_priority(item: dict) -> int | None:
    if item.get("non_html"):
        return None

    status = item.get("status_code")
    terminal = str(item.get("terminal_state") or "").lower()

    if terminal == "security_challenge" or item.get("challenge_detected"):
        return 0
    if item.get("error"):
        if status in _BROWSER_HELPFUL_STATUSES:
            return 0
        if status in _STALE_STATUSES:
            return 2 if settings.sitemap_verify_stale_with_browser else None
        # 429/5xx/timeouts are better handled by bounded HTTP recovery than by
        # expensive Chromium rendering.
        if status in _TRANSIENT_RETRY_ONLY_STATUSES:
            return None
        if status is None:
            return 3
        return None

    # Successful HTTP pages are sent to Chromium only for ambiguous/sparse
    # evidence, and family sampling keeps that bounded.
    return 1


def select_browser_fallback_candidates(
    candidates: list[dict],
    *,
    max_pages: int | None = None,
    per_host_cap: int | None = None,
    per_family_cap: int | None = None,
    stale_cap: int | None = None,
) -> tuple[list[dict], dict]:
    max_pages = max(0, int(max_pages if max_pages is not None else settings.sitemap_browser_fallback_max_pages))
    per_host_cap = max(1, int(per_host_cap if per_host_cap is not None else settings.sitemap_browser_fallback_per_host_cap))
    per_family_cap = max(1, int(per_family_cap if per_family_cap is not None else settings.sitemap_browser_family_sample_size))
    stale_cap = max(0, int(stale_cap if stale_cap is not None else settings.sitemap_browser_stale_sample_max))

    eligible: list[tuple[int, int, dict]] = []
    stats = {
        "considered": len(candidates),
        "selected": 0,
        "skipped_transient_http": 0,
        "skipped_non_browser_helpful": 0,
        "skipped_host_cap": 0,
        "skipped_family_cap": 0,
        "skipped_global_cap": 0,
        "skipped_stale_cap": 0,
        "status_counts": {},
        "selected_status_counts": {},
        "selected_hosts": {},
    }

    status_counter = Counter(str(x.get("status_code")) for x in candidates)
    stats["status_counts"] = dict(status_counter)

    for order, item in enumerate(candidates):
        priority = _browser_priority(item)
        if priority is None:
            status = item.get("status_code")
            if status in _TRANSIENT_RETRY_ONLY_STATUSES:
                stats["skipped_transient_http"] += 1
            else:
                stats["skipped_non_browser_helpful"] += 1
            continue
        eligible.append((priority, order, item))

    eligible.sort(key=lambda x: (x[0], x[1]))
    host_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    selected_status_counts: Counter[str] = Counter()
    stale_selected = 0
    selected: list[dict] = []

    for _, _, item in eligible:
        if len(selected) >= max_pages:
            stats["skipped_global_cap"] += 1
            continue

        status = item.get("status_code")
        if status in _STALE_STATUSES and stale_selected >= stale_cap:
            stats["skipped_stale_cap"] += 1
            continue

        url = item.get("final_url") or item.get("requested_url") or ""
        host = _host(url) or "unknown"
        family = url_family_key(url)

        if host_counts[host] >= per_host_cap:
            stats["skipped_host_cap"] += 1
            continue
        if family_counts[family] >= per_family_cap:
            stats["skipped_family_cap"] += 1
            continue

        selected.append(item)
        host_counts[host] += 1
        family_counts[family] += 1
        selected_status_counts[str(status)] += 1
        if status in _STALE_STATUSES:
            stale_selected += 1

    stats["selected"] = len(selected)
    stats["selected_status_counts"] = dict(selected_status_counts)
    stats["selected_hosts"] = dict(host_counts.most_common(25))
    return selected, stats


def select_final_http_recovery_indices(evidence_by_index: dict[int, dict]) -> list[int]:
    """Bound the final HTTP recovery round to truly retryable failures only."""
    priority_order = {429: 0, 503: 1, 502: 2, 504: 3, 500: 4, 408: 5, 425: 6, None: 7}
    rows: list[tuple[int, int]] = []
    for i, evidence in evidence_by_index.items():
        if not evidence.get("error") or evidence.get("non_html"):
            continue
        if evidence.get("status_code") in _STALE_STATUSES:
            continue
        if evidence.get("terminal_state") == "security_challenge":
            continue
        if not bool(evidence.get("retryable")):
            continue
        status = evidence.get("status_code")
        if status not in priority_order:
            continue
        rows.append((priority_order[status], i))

    rows.sort(key=lambda x: x[0])
    cap = max(0, int(settings.sitemap_final_recovery_max_pages))
    if cap:
        rows = rows[:cap]
    else:
        rows = []
    return [i for _, i in rows]
