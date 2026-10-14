from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings
from app.services.security import validate_public_url

settings = get_settings()
logger = logging.getLogger(__name__)

# Industry-independent transaction/conversion language. Industry-specific terms
# may still appear in the page content, but are not hard-coded into the crawler.
CTA_RE = re.compile(
    r"\b("
    r"apply(?: now| online)?|buy(?: now)?|purchase|order(?: now)?|add to cart|shop now|"
    r"book(?: now)?|reserve|check availability|get (?:a )?quote|request (?:a )?demo|"
    r"subscribe|start (?:free )?trial|sign up|enrol|enroll|open account|"
    r"schedule (?:an )?appointment|contact sales|compare plans|choose plan|"
    r"get started|download brochure"
    r")\b",
    re.I,
)
ARTICLE_DATE_KEYS = {
    "article:published_time",
    "article:modified_time",
    "date",
    "datepublished",
    "date.modified",
    "date.created",
}
AUTHOR_KEYS = {"author", "article:author", "byl"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_STALE_STATUSES = {404, 410}
_ACCESS_BLOCK_STATUSES = {401, 403, 406}

# A successful HTTP status is not enough to establish that a real page was
# resolved. WAF/CDN/bot challenges commonly return 200 with access-denied copy.
_CHALLENGE_PATTERNS = [
    ("access denied", re.compile(r"\baccess denied\b", re.I)),
    ("forbidden", re.compile(r"\bforbidden\b", re.I)),
    ("captcha", re.compile(r"\bcaptcha\b", re.I)),
    ("verify human", re.compile(r"verify (?:that )?you(?:'|’)re human|verify you are human|human verification", re.I)),
    ("security check", re.compile(r"security check|checking your browser|challenge-platform", re.I)),
    ("request blocked", re.compile(r"request (?:has been )?blocked|automated requests|unusual traffic", re.I)),
    ("cloudflare challenge", re.compile(r"attention required|cloudflare ray id|cf-chl-|just a moment", re.I)),
    ("akamai denial", re.compile(r"reference #\d+\.[0-9a-f.]+|akamai.*(?:denied|blocked)", re.I)),
]


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or "", flags=re.UNICODE))


def _content_hash(text: str) -> str:
    return hashlib.sha256(_clean(text).casefold().encode("utf-8")).hexdigest()


def _challenge_reason(title: str, text: str, html: str = "") -> str | None:
    """Return a conservative challenge/error-page reason, or None.

    Human-facing pages often load CAPTCHA/security widgets whose JavaScript source
    contains words such as ``captcha`` or ``challenge``. Those raw-code tokens are
    not evidence that the page itself is blocked. We therefore:

    * trust challenge wording in the document title;
    * inspect visible/main text for challenge wording;
    * accept raw-HTML-only evidence only for strong CDN/WAF fingerprints; and
    * never fail a normal content-rich page because ``captcha`` merely appears in
      a widget/script.
    """
    title_probe = _clean(title)[:500]
    body_probe = _clean(text)[:12000]
    html_probe = (html or "")[:50000]

    title_hits = [name for name, pattern in _CHALLENGE_PATTERNS if pattern.search(title_probe)]
    if title_hits:
        return title_hits[0]

    visible_hits = [name for name, pattern in _CHALLENGE_PATTERNS if pattern.search(body_probe)]
    unique_visible_hits = list(dict.fromkeys(visible_hits))

    # Raw source is allowed to establish a challenge only for strong technical
    # fingerprints. Generic words such as ``captcha`` are deliberately excluded.
    strong_html_names = {
        "cloudflare challenge",
        "akamai denial",
        "security check",
        "request blocked",
    }
    strong_html_hits = [
        name
        for name, pattern in _CHALLENGE_PATTERNS
        if name in strong_html_names and pattern.search(html_probe)
    ]

    if len(unique_visible_hits) >= 2:
        return unique_visible_hits[0]

    if unique_visible_hits:
        hit = unique_visible_hits[0]
        words = _word_count(body_probe)

        # A lone visible CAPTCHA mention is common on legitimate forms/pages.
        # Require corroboration instead of treating it as a blocker by itself.
        if hit == "captcha":
            return strong_html_hits[0] if strong_html_hits else None

        # Sparse pages with an explicit visible denial/check message are likely
        # genuine interstitials. Content-rich pages are retained.
        if words < 220:
            return hit

    if strong_html_hits and _word_count(body_probe) < 220:
        return strong_html_hits[0]

    return None


def _schema_types(soup: BeautifulSoup) -> list[str]:
    found: set[str] = set()
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(node.string or node.get_text() or "null")
        except Exception:
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                t = item.get("@type")
                if isinstance(t, str):
                    found.add(t)
                elif isinstance(t, list):
                    found.update(str(x) for x in t)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
    return sorted(found)


def _breadcrumbs(soup: BeautifulSoup) -> list[str]:
    values: list[str] = []
    selectors = (
        '[aria-label*="breadcrumb" i] a, '
        '.breadcrumb a, [class*="breadcrumb" i] a, [itemtype*="BreadcrumbList"] a'
    )
    for node in soup.select(selectors):
        text = _clean(node.get_text(" ", strip=True))
        if text and text not in values:
            values.append(text)
    return values[:20]


def _extract_evidence(
    html: str,
    final_url: str,
    status_code: int,
    fetch_method: str,
    retry_count: int = 0,
) -> dict:
    soup = BeautifulSoup(html or "", "lxml")

    title = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    meta_node = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    meta = _clean(meta_node.get("content") if meta_node else "")
    canonical_node = soup.find(
        "link",
        attrs={
            "rel": lambda x: x
            and "canonical" in [str(v).lower() for v in (x if isinstance(x, list) else [x])]
        },
    )
    canonical = (
        urljoin(final_url, canonical_node.get("href"))
        if canonical_node and canonical_node.get("href")
        else ""
    )
    schema_types = _schema_types(soup)
    breadcrumb = _breadcrumbs(soup)

    # Audit-only signal: a legitimate page may embed CAPTCHA / bot-protection
    # widgets without being a challenge page. Keep this separate from
    # `challenge_detected` so a rich content page is never failed merely because
    # reCAPTCHA/hCaptcha/Turnstile code exists in the document.
    security_widget_detected = bool(
        soup.find(
            "script",
            src=re.compile(r"(?:recaptcha|hcaptcha|turnstile|challenge-platform)", re.I),
        )
        or soup.select_one(
            ".g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], "
            "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile']"
        )
    )

    author_present = bool(
        soup.select_one('[rel="author"], .author, [class*="author" i], [itemprop="author"]')
    )
    published = ""
    for node in soup.find_all("meta"):
        key = str(
            node.get("property") or node.get("name") or node.get("itemprop") or ""
        ).strip().lower()
        if key in AUTHOR_KEYS:
            author_present = author_present or bool(_clean(node.get("content")))
        if key in ARTICLE_DATE_KEYS and not published:
            published = _clean(node.get("content"))

    article_element = bool(soup.find("article"))

    # Classification evidence comes from MAIN/ARTICLE after stripping global
    # chrome. This prevents a site-wide "Buy/Apply/Book" header CTA from turning
    # every About/Privacy/Article page into a PRODUCT page.
    root = soup.find("main") or soup.find("article") or soup.body or soup
    if root:
        for selector in [
            "script",
            "style",
            "noscript",
            "svg",
            "nav",
            "footer",
            "header",
            "aside",
            "[role=navigation]",
            "[class*=cookie i]",
            "[id*=cookie i]",
            "[class*=modal i]",
        ]:
            for node in root.select(selector):
                node.decompose()

        h1 = [_clean(x.get_text(" ", strip=True)) for x in root.find_all("h1")]
        h2 = [_clean(x.get_text(" ", strip=True)) for x in root.find_all("h2")]
        h1 = [x for x in h1 if x][:10]
        h2 = [x for x in h2 if x][:40]

        forms_count = len(root.find_all("form"))
        main_buttons = root.find_all("button", limit=500)
        cta_texts: list[str] = []
        for node in root.find_all(["a", "button"], limit=800):
            label = _clean(node.get_text(" ", strip=True))
            if label and CTA_RE.search(label) and label not in cta_texts:
                cta_texts.append(label)
            if len(cta_texts) >= 30:
                break

        page_host = (urlparse(final_url).hostname or "").lower()
        internal_links: set[str] = set()
        for node in root.find_all("a", href=True, limit=1500):
            href = urljoin(final_url, str(node.get("href") or ""))
            parsed = urlparse(href)
            if parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() == page_host:
                internal_links.add(href.split("#", 1)[0])

        text = _clean(root.get_text(" ", strip=True))
        main_button_count = len(main_buttons)
        main_internal_links_count = len(internal_links)
    else:
        h1, h2 = [], []
        forms_count = 0
        cta_texts = []
        text = ""
        main_button_count = 0
        main_internal_links_count = 0

    excerpt = text[: settings.sitemap_text_excerpt_chars]
    challenge_reason = _challenge_reason(title, text, html)
    challenge_error = (
        f"Security/challenge page detected: {challenge_reason}"
        if challenge_reason
        else None
    )

    return {
        "final_url": final_url,
        "canonical_url": canonical,
        "status_code": status_code,
        "fetch_method": fetch_method,
        "retry_count": retry_count,
        "title": title,
        "meta_description": meta,
        "h1": h1,
        "h2": h2,
        "schema_types": schema_types,
        "breadcrumbs": breadcrumb,
        "author_present": author_present,
        "published_at_text": published,
        "article_element": article_element,
        "forms_count": forms_count,
        "cta_texts": cta_texts,
        "main_button_count": main_button_count,
        "main_internal_links_count": main_internal_links_count,
        "word_count": _word_count(text),
        "text_excerpt": excerpt,
        "content_hash": _content_hash(text) if text else None,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "security_widget_detected": security_widget_detected,
        "challenge_detected": bool(challenge_reason),
        "challenge_reason": challenge_reason,
        "error": challenge_error,
        "retryable": bool(challenge_reason),
        "terminal_state": "security_challenge" if challenge_reason else "usable",
    }


def _parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse Retry-After seconds or HTTP-date into a bounded delay."""
    raw = str(value or "").strip()
    if not raw:
        return None

    try:
        seconds = float(raw)
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            base = now or datetime.now(timezone.utc)
            seconds = (target.astimezone(timezone.utc) - base.astimezone(timezone.utc)).total_seconds()
        except Exception:
            return None

    if seconds < 0:
        return 0.0
    return min(float(settings.sitemap_http_max_retry_after_seconds), seconds)


def _retry_delay_seconds(status: int | None, retry: int, retry_after: str | None = None) -> float:
    if status == 429:
        parsed = _parse_retry_after(retry_after)
        if parsed is not None:
            return parsed
        return min(
            float(settings.sitemap_http_max_retry_after_seconds),
            float(settings.sitemap_http_default_rate_limit_cooldown_seconds) * (2**retry),
        )
    return min(8.0, 0.75 * (2**retry))


@dataclass
class _HostRateState:
    max_limit: int
    min_limit: int
    current_limit: int
    ramp_successes: int = 24
    in_flight: int = 0
    cooldown_until: float = 0.0
    success_streak: int = 0
    rate_limit_events: int = 0
    access_window: deque[int] = field(default_factory=deque)
    access_block_open: bool = False
    access_block_status: int | None = None
    access_block_events: int = 0
    next_launch_at: float = 0.0

    @property
    def access_block_ratio(self) -> float:
        if not self.access_window:
            return 0.0
        return sum(self.access_window) / len(self.access_window)

    def can_launch(self, now: float) -> bool:
        return (
            not self.access_block_open
            and self.in_flight < self.current_limit
            and now >= self.cooldown_until
            and now >= self.next_launch_at
        )

    def _observe_access_block(self, status: int | None) -> None:
        if status is None:
            return

        window_size = max(1, int(settings.sitemap_access_block_window_size))
        min_samples = max(1, min(window_size, int(settings.sitemap_access_block_min_samples)))
        signal = 1 if status in _ACCESS_BLOCK_STATUSES else 0
        self.access_window.append(signal)
        while len(self.access_window) > window_size:
            self.access_window.popleft()

        if status in _ACCESS_BLOCK_STATUSES:
            self.access_block_status = int(status)

        if len(self.access_window) < min_samples:
            return

        threshold = max(0.0, min(1.0, float(settings.sitemap_access_block_ratio)))
        if self.access_block_ratio >= threshold:
            if not self.access_block_open:
                self.access_block_events += 1
            self.access_block_open = True
            self.current_limit = self.min_limit
            self.success_streak = 0

    def observe(self, payload: dict, now: float) -> None:
        status = payload.get("status_code")
        self._observe_access_block(status)

        if self.access_block_open:
            return

        if status == 429:
            self.rate_limit_events += 1
            self.success_streak = 0
            shrink = max(0.1, min(1.0, float(settings.sitemap_http_rate_limit_shrink_factor)))
            next_limit = int(self.current_limit * shrink)
            self.current_limit = max(self.min_limit, min(self.max_limit, next_limit or self.min_limit))
            delay = payload.get("retry_after_seconds")
            try:
                delay = float(delay)
            except (TypeError, ValueError):
                delay = float(settings.sitemap_http_default_rate_limit_cooldown_seconds)
            delay = max(0.0, min(float(settings.sitemap_http_max_retry_after_seconds), delay))
            self.cooldown_until = max(self.cooldown_until, now + delay)
            return

        if payload.get("error"):
            self.success_streak = 0
            return

        self.success_streak += 1
        threshold = max(1, int(self.ramp_successes))
        if self.success_streak >= threshold and self.current_limit < self.max_limit:
            self.current_limit += 1
            self.success_streak = 0


async def _host_fair_map(
    items: list,
    *,
    url_getter,
    fetch_one,
    concurrency: int,
    per_host_limit: int,
    initial_per_host_limit: int | None = None,
    ramp_successes: int | None = None,
    min_start_interval_seconds: float = 0.0,
    progress=None,
    result_callback=None,
    progress_label: str,
) -> list[dict]:
    """Run a bounded host-fair scheduler while preserving output order.

    429 responses shrink only the affected host and respect Retry-After. A strong
    rolling 401/403/406 signal opens a host-level access circuit: queued URLs for
    that host are marked recoverable/deferred instead of generating tens of
    thousands of additional WAF requests. Other hosts continue normally.
    """
    if not items:
        return []

    worker_count = max(1, min(int(concurrency), len(items)))
    hard_per_host = max(1, int(per_host_limit))
    min_start_interval = max(0.0, float(min_start_interval_seconds or 0.0))
    min_per_host = max(1, min(hard_per_host, int(settings.sitemap_http_min_per_host_concurrency)))
    configured_initial = (
        settings.sitemap_http_initial_per_host_concurrency
        if initial_per_host_limit is None
        else initial_per_host_limit
    )
    initial_per_host = max(min_per_host, min(hard_per_host, int(configured_initial)))
    configured_ramp = (
        settings.sitemap_http_successes_to_ramp
        if ramp_successes is None
        else ramp_successes
    )

    queues: dict[str, deque] = {}
    host_order: list[str] = []
    for index, item in enumerate(items):
        url = str(url_getter(item) or "")
        host = (urlparse(url).hostname or "").lower() or "unknown"
        if host not in queues:
            queues[host] = deque()
            host_order.append(host)
        queues[host].append((index, item))

    states = {
        host: _HostRateState(
            max_limit=hard_per_host,
            min_limit=min_per_host,
            current_limit=initial_per_host,
            ramp_successes=max(1, int(configured_ramp)),
        )
        for host in host_order
    }
    outputs: list[dict | None] = [None] * len(items)
    active: dict[asyncio.Task, tuple[int, object, str]] = {}
    cursor = 0
    completed = 0
    loop = asyncio.get_running_loop()

    def pending_count() -> int:
        return sum(len(q) for q in queues.values())

    async def publish(index: int, payload: dict) -> None:
        nonlocal completed
        outputs[index] = payload
        completed += 1
        if result_callback:
            maybe = result_callback(index, payload)
            if asyncio.iscoroutine(maybe):
                await maybe

    while completed < len(items):
        now = loop.time()

        # Once a host-level access circuit opens, do not keep hammering every queued
        # URL. These rows remain explicitly retryable and can be recovered later.
        for host in host_order:
            state = states[host]
            queue = queues[host]
            if not state.access_block_open or not queue:
                continue

            deferred_count = len(queue)
            block_status = state.access_block_status
            while queue:
                index, item = queue.popleft()
                base = dict(item) if isinstance(item, dict) else {}
                url = str(url_getter(item) or "")
                prior_status = base.get("status_code")
                payload = {
                    **base,
                    "requested_url": base.get("requested_url") or url,
                    "final_url": base.get("final_url") or url,
                    # Do not invent a per-URL HTTP response for a URL that was not
                    # launched. Preserve a prior status only when one genuinely exists.
                    "status_code": prior_status,
                    "error": (
                        "Deferred after host access circuit opened following repeated "
                        f"HTTP {block_status or '401/403/406'} responses"
                    ),
                    "terminal_state": "host_access_deferred",
                    "retryable": True,
                    "host_access_deferred": True,
                    "host_access_block_status": block_status,
                    "host_access_block_ratio": round(state.access_block_ratio, 4),
                    "host_access_block_events": state.access_block_events,
                    "rate_limit_host": host,
                    "host_concurrency_limit": state.current_limit,
                    "host_rate_limit_events": state.rate_limit_events,
                }
                await publish(index, payload)

            logger.warning(
                "Host access circuit opened for %s after repeated HTTP %s responses; deferred %s queued URL(s)",
                host,
                block_status,
                deferred_count,
            )
            if progress:
                await progress(
                    completed,
                    len(items),
                    f"{progress_label}: deferred {deferred_count:,} queued URL(s) for blocked host {host}",
                )

        if completed >= len(items):
            break

        # Fill the global pool in fair host rounds. A single host gets one launch
        # per round, so later hosts are never trapped behind a 100k-item prefix.
        while len(active) < worker_count and pending_count() > 0:
            launched_this_round = False
            for _ in range(len(host_order)):
                host = host_order[cursor % len(host_order)]
                cursor += 1
                queue = queues[host]
                state = states[host]
                if not queue or not state.can_launch(now):
                    continue

                index, item = queue.popleft()
                state.in_flight += 1
                if min_start_interval:
                    state.next_launch_at = now + min_start_interval
                task = asyncio.create_task(fetch_one(item))
                active[task] = (index, item, host)
                launched_this_round = True
                if len(active) >= worker_count:
                    break

            if not launched_this_round:
                break
            now = loop.time()

        if not active:
            # Every remaining host is either cooling down or has just opened its
            # access circuit. Circuit-open queues will be drained on the next loop.
            now = loop.time()
            waits = [
                max(0.0, max(state.cooldown_until, state.next_launch_at) - now)
                for host, state in states.items()
                if queues[host] and not state.access_block_open
            ]
            await asyncio.sleep(max(0.01, min(waits) if waits else 0.01))
            continue

        done, _ = await asyncio.wait(active.keys(), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            index, item, host = active.pop(task)
            state = states[host]
            state.in_flight = max(0, state.in_flight - 1)
            try:
                payload = task.result()
            except Exception as exc:
                url = str(url_getter(item) or "")
                payload = {
                    "requested_url": url,
                    "final_url": url,
                    "status_code": None,
                    "error": str(exc),
                    "terminal_state": "fetch_failed",
                    "retryable": True,
                }

            state.observe(payload, loop.time())
            payload["rate_limit_host"] = host
            payload["host_concurrency_limit"] = state.current_limit
            payload["host_rate_limit_events"] = state.rate_limit_events
            payload["host_access_block_events"] = state.access_block_events
            payload["host_access_block_ratio"] = round(state.access_block_ratio, 4)
            await publish(index, payload)

            if progress:
                throttled = sum(
                    1
                    for h, st in states.items()
                    if queues[h]
                    and (
                        st.access_block_open
                        or st.cooldown_until > loop.time()
                        or st.current_limit < initial_per_host
                    )
                )
                await progress(
                    completed,
                    len(items),
                    f"{progress_label} ({completed:,}/{len(items):,}); active hosts {len(active):,}; throttled/deferred hosts {throttled:,}",
                )

    return [x or {} for x in outputs]


async def _safe_fetch_html(
    client: httpx.AsyncClient,
    url: str,
    *,
    dns_cache: dict[str, bool],
) -> dict:
    current = url
    last_error: Exception | None = None
    attempts = max(1, int(settings.sitemap_page_max_retries))

    for retry in range(attempts):
        try:
            current = url
            for _ in range(settings.sitemap_max_redirects + 1):
                await validate_public_url(current, dns_cache=dns_cache)
                async with client.stream("GET", current, follow_redirects=False) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        loc = response.headers.get("location")
                        if not loc:
                            raise RuntimeError("Redirect missing Location header")
                        next_url = urljoin(current, loc)
                        await validate_public_url(next_url, dns_cache=dns_cache)
                        current = next_url
                        continue

                    status = int(response.status_code)
                    content_type = (response.headers.get("content-type") or "").lower()
                    retry_after_raw = response.headers.get("retry-after")
                    retry_after_seconds = _parse_retry_after(retry_after_raw) if status == 429 else None

                    if status >= 400:
                        payload = {
                            "final_url": current,
                            "status_code": status,
                            "error": f"HTTP {status}",
                            "fetch_method": "http",
                            "retry_count": retry,
                            "terminal_state": (
                                "stale_sitemap_url" if status in _STALE_STATUSES else "fetch_failed"
                            ),
                            "retryable": status in _RETRYABLE_STATUSES,
                            "rate_limited": status == 429,
                            "retry_after_seconds": retry_after_seconds,
                        }
                        if status in _RETRYABLE_STATUSES and retry + 1 < attempts:
                            last_error = RuntimeError(payload["error"])
                            await asyncio.sleep(
                                _retry_delay_seconds(status, retry, retry_after_raw)
                            )
                            break
                        return payload

                    if "html" not in content_type and "xhtml" not in content_type:
                        return {
                            "final_url": current,
                            "status_code": status,
                            "error": f"Non-HTML content: {content_type or 'unknown'}",
                            "retry_count": retry,
                            "non_html": True,
                            "terminal_state": "non_html",
                            "retryable": False,
                        }

                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > settings.sitemap_max_html_bytes:
                            raise RuntimeError(
                                f"HTML exceeds {settings.sitemap_max_html_bytes:,} bytes"
                            )
                        chunks.append(chunk)
                    encoding = response.encoding or "utf-8"
                    html = b"".join(chunks).decode(encoding, errors="replace")
                    payload = _extract_evidence(html, current, status, "http", retry)
                    if payload.get("challenge_detected") and retry + 1 < attempts:
                        last_error = RuntimeError(payload.get("error") or "Security challenge")
                        await asyncio.sleep(_retry_delay_seconds(None, retry))
                        break
                    return payload
            else:
                raise RuntimeError("Too many redirects")
        except Exception as exc:
            last_error = exc
            if retry + 1 < attempts:
                await asyncio.sleep(_retry_delay_seconds(None, retry))

    return {
        "final_url": current,
        "status_code": None,
        "error": str(last_error or "Unknown resolver failure"),
        "retry_count": max(0, attempts - 1),
        "terminal_state": "fetch_failed",
        "retryable": True,
    }


async def resolve_pages_http(
    urls: list[str],
    progress=None,
    result_callback=None,
    concurrency: int | None = None,
) -> list[dict]:
    """Resolve pages with host fairness, adaptive 429 control, and a 401/403/406 circuit breaker."""
    if not urls:
        return []

    timeout = httpx.Timeout(settings.sitemap_request_timeout_seconds)
    configured_concurrency = max(1, int(concurrency or settings.sitemap_resolve_concurrency))
    worker_count = max(1, min(configured_concurrency, len(urls)))
    limits = httpx.Limits(
        max_connections=max(16, worker_count * 2),
        max_keepalive_connections=max(8, worker_count),
    )
    headers = {
        "User-Agent": settings.sitemap_http_user_agent,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
        "Accept-Language": "en-IN,en;q=0.9",
    }
    dns_cache: dict[str, bool] = {}

    async with httpx.AsyncClient(timeout=timeout, headers=headers, limits=limits) as client:
        async def fetch_one(url: str):
            payload = await _safe_fetch_html(client, url, dns_cache=dns_cache)
            payload["requested_url"] = url
            return payload

        return await _host_fair_map(
            urls,
            url_getter=lambda x: x,
            fetch_one=fetch_one,
            concurrency=worker_count,
            per_host_limit=settings.sitemap_http_per_host_concurrency,
            initial_per_host_limit=settings.sitemap_http_initial_per_host_concurrency,
            ramp_successes=settings.sitemap_http_successes_to_ramp,
            progress=progress,
            result_callback=result_callback,
            progress_label="Resolving pages",
        )


async def _safe_fetch_impersonated(session, item: dict, *, dns_cache: dict[str, bool]) -> dict:
    """Browser-fingerprint HTTP recovery with safe redirects and one 429 retry."""
    requested = item.get("requested_url") or item.get("final_url") or ""
    current = requested
    base_retry_count = int(item.get("retry_count") or 0)

    try:
        # One additional rate-limit-aware retry is enough here; the normal HTTP
        # resolver and final recovery round already provide the wider retry budget.
        for local_retry in range(2):
            current = requested
            for _ in range(settings.sitemap_max_redirects + 1):
                await validate_public_url(current, dns_cache=dns_cache)
                response = await session.get(
                    current,
                    impersonate=settings.sitemap_impersonation_browser,
                    timeout=settings.sitemap_request_timeout_seconds,
                    allow_redirects=False,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    },
                )
                status = int(response.status_code)
                if status in _REDIRECT_STATUSES:
                    loc = response.headers.get("location")
                    if not loc:
                        raise RuntimeError("Redirect missing Location header")
                    next_url = urljoin(current, loc)
                    await validate_public_url(next_url, dns_cache=dns_cache)
                    current = next_url
                    continue

                content_type = (response.headers.get("content-type") or "").lower()
                retry_after_raw = response.headers.get("retry-after")
                retry_after_seconds = _parse_retry_after(retry_after_raw) if status == 429 else None
                retry_count = base_retry_count + local_retry

                if status >= 400:
                    if status == 429 and local_retry == 0:
                        await asyncio.sleep(_retry_delay_seconds(status, local_retry, retry_after_raw))
                        break
                    return {
                        **item,
                        "requested_url": requested,
                        "final_url": current,
                        "status_code": status,
                        "error": f"HTTP {status}",
                        "fetch_method": "curl-cffi",
                        "impersonation_fallback_attempted": True,
                        "retry_count": retry_count,
                        "terminal_state": "stale_sitemap_url" if status in _STALE_STATUSES else "fetch_failed",
                        "retryable": status in _RETRYABLE_STATUSES,
                        "rate_limited": status == 429,
                        "retry_after_seconds": retry_after_seconds,
                    }

                if "html" not in content_type and "xhtml" not in content_type:
                    return {
                        **item,
                        "requested_url": requested,
                        "final_url": current,
                        "status_code": status,
                        "error": f"Non-HTML content: {content_type or 'unknown'}",
                        "fetch_method": "curl-cffi",
                        "impersonation_fallback_attempted": True,
                        "retry_count": retry_count,
                        "non_html": True,
                        "terminal_state": "non_html",
                        "retryable": False,
                    }

                raw = bytes(response.content or b"")
                if len(raw) > settings.sitemap_max_html_bytes:
                    raise RuntimeError(f"HTML exceeds {settings.sitemap_max_html_bytes:,} bytes")
                encoding = getattr(response, "encoding", None) or "utf-8"
                html = raw.decode(encoding, errors="replace")
                payload = _extract_evidence(html, current, status, "curl-cffi", retry_count)
                payload["requested_url"] = requested
                payload["impersonation_fallback_attempted"] = True
                payload["recovered_by_impersonation"] = not bool(payload.get("error"))
                return payload
            else:
                raise RuntimeError("Too many redirects")

        return {
            **item,
            "requested_url": requested,
            "final_url": current or requested,
            "status_code": 429,
            "error": "HTTP 429",
            "fetch_method": "curl-cffi",
            "impersonation_fallback_attempted": True,
            "retry_count": base_retry_count + 1,
            "terminal_state": "fetch_failed",
            "retryable": True,
            "rate_limited": True,
        }
    except Exception as exc:
        return {
            **item,
            "requested_url": requested,
            "final_url": current or requested,
            "error": str(exc),
            "fetch_method": item.get("fetch_method") or "http",
            "impersonation_fallback_attempted": True,
            "impersonation_fallback_error": str(exc),
            "terminal_state": item.get("terminal_state") or "fetch_failed",
            "retryable": bool(item.get("retryable", True)),
        }


async def resolve_pages_impersonated(
    items: list[dict],
    progress=None,
    result_callback=None,
    concurrency: int | None = None,
    per_host_limit: int | None = None,
    initial_per_host_limit: int | None = None,
    min_start_interval_seconds: float = 0.0,
) -> list[dict]:
    """Cheap TLS/browser-fingerprint recovery before Chromium.

    The same host-fair adaptive scheduler is used here. Browser-fingerprint HTTP
    starts conservatively per host and also stops a renewed 401/403/406 storm.
    """
    if not items:
        return []
    try:
        from curl_cffi.requests import AsyncSession
    except Exception as exc:
        out = []
        for item in items:
            payload = dict(item)
            payload["impersonation_fallback_attempted"] = False
            payload["impersonation_fallback_error"] = f"curl-cffi unavailable: {exc}"
            out.append(payload)
        return out

    worker_count = max(
        1,
        min(int(concurrency or settings.sitemap_impersonation_concurrency), len(items)),
    )
    dns_cache: dict[str, bool] = {}

    async with AsyncSession() as session:
        async def fetch_one(item: dict):
            return await _safe_fetch_impersonated(session, item, dns_cache=dns_cache)

        return await _host_fair_map(
            items,
            url_getter=lambda x: x.get("final_url") or x.get("requested_url") or "",
            fetch_one=fetch_one,
            concurrency=worker_count,
            per_host_limit=(
                settings.sitemap_impersonation_per_host_concurrency
                if per_host_limit is None
                else per_host_limit
            ),
            initial_per_host_limit=(
                settings.sitemap_impersonation_initial_per_host_concurrency
                if initial_per_host_limit is None
                else initial_per_host_limit
            ),
            ramp_successes=settings.sitemap_impersonation_successes_to_ramp,
            min_start_interval_seconds=min_start_interval_seconds,
            progress=progress,
            result_callback=result_callback,
            progress_label="Impersonated HTTP fallback",
        )


async def render_pages_browser(
    items: list[dict],
    progress=None,
    result_callback=None,
    concurrency: int | None = None,
) -> list[dict]:
    """Last-resort Chromium recovery with reusable worker contexts.

    Each worker keeps one context/page for its lifetime instead of creating a new
    Chromium context for every URL. Global and per-host concurrency are bounded,
    images/media/fonts are blocked, and successful results can be checkpointed
    through result_callback as soon as each page completes.
    """
    if not items:
        return []
    from playwright.async_api import async_playwright

    worker_count = max(
        1,
        min(int(concurrency or settings.sitemap_playwright_concurrency), len(items)),
    )
    per_host = max(1, int(settings.sitemap_playwright_per_host_concurrency))
    completed = 0
    outputs: list[dict | None] = [None] * len(items)
    dns_cache: dict[str, bool] = {}
    queue: asyncio.Queue = asyncio.Queue(maxsize=max(worker_count * 2, 12))
    host_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
        lambda: asyncio.Semaphore(per_host)
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        async def worker():
            nonlocal completed
            context = await browser.new_context(
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1440, "height": 900},
                user_agent=settings.sitemap_http_user_agent,
            )

            async def route_handler(route):
                req = route.request
                if req.resource_type in {"image", "media", "font"}:
                    await route.abort()
                    return
                if req.is_navigation_request():
                    try:
                        await validate_public_url(req.url, dns_cache=dns_cache)
                    except Exception:
                        await route.abort()
                        return
                await route.continue_()

            await context.route("**/*", route_handler)
            page = await context.new_page()
            try:
                while True:
                    job = await queue.get()
                    if job is None:
                        queue.task_done()
                        return

                    index, item = job
                    url = item.get("final_url") or item.get("requested_url") or ""
                    host = (urlparse(url).hostname or "").lower() or "unknown"
                    payload = dict(item)
                    try:
                        # Avoid cross-job cookie/session buildup while retaining the
                        # much cheaper reusable browser/page objects.
                        await context.clear_cookies()
                        async with host_semaphores[host]:
                            response = await page.goto(
                                url,
                                wait_until="commit",
                                timeout=min(
                                    settings.crawl_timeout_ms,
                                    settings.sitemap_browser_navigation_timeout_ms,
                                ),
                            )
                            try:
                                await page.wait_for_load_state(
                                    "domcontentloaded",
                                    timeout=min(
                                        settings.crawl_timeout_ms,
                                        settings.sitemap_browser_domcontentloaded_timeout_ms,
                                    ),
                                )
                            except Exception:
                                pass
                            if settings.sitemap_browser_networkidle_timeout_ms > 0:
                                try:
                                    await page.wait_for_load_state(
                                        "networkidle",
                                        timeout=min(
                                            settings.crawl_timeout_ms,
                                            settings.sitemap_browser_networkidle_timeout_ms,
                                        ),
                                    )
                                except Exception:
                                    pass

                            await validate_public_url(page.url, dns_cache=dns_cache)
                            browser_status = response.status if response else int(item.get("status_code") or 200)
                            if browser_status >= 400:
                                payload.update({
                                    "final_url": page.url,
                                    "status_code": browser_status,
                                    "error": f"HTTP {browser_status}",
                                    "fetch_method": "playwright",
                                    "browser_fallback_attempted": True,
                                    "browser_fallback_status": "verified_failure",
                                    "terminal_state": (
                                        "stale_sitemap_url" if browser_status in _STALE_STATUSES else "fetch_failed"
                                    ),
                                    "retryable": browser_status in _RETRYABLE_STATUSES,
                                })
                            else:
                                html = await page.content()
                                payload = _extract_evidence(
                                    html,
                                    page.url,
                                    browser_status,
                                    "playwright",
                                    item.get("retry_count", 0),
                                )
                                payload["requested_url"] = item.get("requested_url") or url
                                payload["browser_fallback_attempted"] = True
                                if payload.get("challenge_detected"):
                                    payload["browser_fallback_status"] = "challenge_detected"
                                    payload["recovered_by_browser"] = False
                                else:
                                    payload["browser_fallback_status"] = "success"
                                    payload["recovered_by_browser"] = bool(item.get("error"))
                    except Exception as exc:
                        payload = dict(item)
                        payload["browser_fallback_attempted"] = True
                        payload["browser_fallback_status"] = "failed"
                        payload["browser_fallback_error"] = str(exc)
                        if item.get("status_code") and not item.get("error"):
                            payload.pop("error", None)
                            payload["terminal_state"] = "usable"
                        else:
                            payload.setdefault("terminal_state", "fetch_failed")
                            payload.setdefault("retryable", item.get("status_code") not in _STALE_STATUSES)
                        if not payload.get("fetch_method"):
                            payload["fetch_method"] = "http"

                    outputs[index] = payload
                    completed += 1
                    if result_callback:
                        maybe = result_callback(index, payload)
                        if asyncio.iscoroutine(maybe):
                            await maybe
                    if progress:
                        await progress(
                            completed,
                            len(items),
                            f"Browser fallback ({completed:,}/{len(items):,})",
                        )
                    queue.task_done()
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    await context.close()
                except Exception:
                    pass

        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        for i, item in enumerate(items):
            await queue.put((i, item))
        for _ in workers:
            await queue.put(None)
        await queue.join()
        await asyncio.gather(*workers)
        await browser.close()

    return [x or dict(items[i]) for i, x in enumerate(outputs)]

