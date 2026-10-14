from __future__ import annotations

import asyncio
import gzip
import logging
from collections import deque
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from lxml import etree

try:
    import tldextract
except Exception:  # pragma: no cover - safe fallback when optional dependency is absent
    tldextract = None

from app.config import get_settings
from app.services.security import validate_public_url

settings = get_settings()
logger = logging.getLogger(__name__)

_BROWSER_FALLBACK_STATUSES = {401, 403, 406, 429, 503}
_RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(slots=True)
class SitemapEntry:
    url: str
    child_sitemap: str
    lastmod: str | None = None
    hostname: str = ""
    scope_class: str = "EXTERNAL_DOMAIN"
    obvious_non_page: bool = False


def normalize_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        return (url or "").strip()
    port = parsed.port
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    tracking = {"gclid", "fbclid", "msclkid", "dclid"}
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in tracking
    ]
    return urlunparse((scheme, netloc, path, "", urlencode(query_pairs, doseq=True), ""))


def _host_key(host: str | None) -> str:
    value = (host or "").lower().rstrip(".")
    return value[4:] if value.startswith("www.") else value



_NON_PAGE_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".mp3", ".wav", ".mp4", ".mov", ".avi", ".webm",
    ".zip", ".gz", ".rar", ".7z", ".tar",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".csv", ".json", ".xml", ".txt", ".rss", ".atom",
}

_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=()) if tldextract else None


def _registrable_domain(host: str | None) -> str:
    host = _host_key(host)
    if not host:
        return ""
    if _TLD_EXTRACT is not None:
        ext = _TLD_EXTRACT(host)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}".lower()
    # Safe compatibility fallback. The production dependency should include
    # tldextract so public-suffix cases such as co.uk are handled correctly.
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def classify_url_scope(root_sitemap: str, url: str) -> str:
    """Classify a final sitemap URL without dropping it.

    ROOT_HOST: exact root hostname after www normalization.
    RELATED_SUBDOMAIN: same registrable domain but another subdomain.
    EXTERNAL_DOMAIN: different registrable domain.
    """
    root_host = _host_key(urlparse(root_sitemap).hostname)
    host = _host_key(urlparse(url).hostname)
    if not host or not root_host:
        return "EXTERNAL_DOMAIN"
    if host == root_host:
        return "ROOT_HOST"
    root_reg = _registrable_domain(root_host)
    host_reg = _registrable_domain(host)
    if root_reg and host_reg and root_reg == host_reg:
        return "RELATED_SUBDOMAIN"
    return "EXTERNAL_DOMAIN"


def is_obvious_non_page_url(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    if not path or path.endswith("/"):
        return False
    last = path.rsplit("/", 1)[-1]
    if "." not in last:
        return False
    suffix = "." + last.rsplit(".", 1)[-1]
    return suffix in _NON_PAGE_EXTENSIONS


def select_resolution_entries(
    entries: list[SitemapEntry],
    *,
    scope_mode: str,
    exclude_non_pages: bool = True,
) -> tuple[list[SitemapEntry], dict]:
    scope_mode = (scope_mode or "registrable_domain").strip().lower()
    allowed = {"strict_host", "registrable_domain", "sitemap_authoritative"}
    if scope_mode not in allowed:
        raise ValueError(f"Unsupported sitemap resolution scope: {scope_mode}")

    root_host = sum(1 for x in entries if x.scope_class == "ROOT_HOST")
    related = sum(1 for x in entries if x.scope_class == "RELATED_SUBDOMAIN")
    external = sum(1 for x in entries if x.scope_class == "EXTERNAL_DOMAIN")

    def in_scope(entry: SitemapEntry) -> bool:
        if scope_mode == "sitemap_authoritative":
            return True
        if scope_mode == "strict_host":
            return entry.scope_class == "ROOT_HOST"
        return entry.scope_class in {"ROOT_HOST", "RELATED_SUBDOMAIN"}

    scoped = [x for x in entries if in_scope(x)]
    excluded_by_scope = [x for x in entries if not in_scope(x)]
    excluded_non_page = [x for x in scoped if exclude_non_pages and x.obvious_non_page]
    selected = [x for x in scoped if not (exclude_non_pages and x.obvious_non_page)]

    return selected, {
        "scope_mode": scope_mode,
        "root_host_urls": root_host,
        "related_subdomain_urls": related,
        "external_domain_urls": external,
        "in_scope_urls": len(scoped),
        "excluded_by_scope_urls": len(excluded_by_scope),
        "excluded_external_domain_urls": sum(1 for x in excluded_by_scope if x.scope_class == "EXTERNAL_DOMAIN"),
        "excluded_non_page_urls": len(excluded_non_page),
        "resolution_candidates": len(selected),
        "external_domain_samples": [x.url for x in entries if x.scope_class == "EXTERNAL_DOMAIN"][:10],
        "non_page_samples": [x.url for x in excluded_non_page][:10],
    }

def _looks_like_sitemap_url(url: str) -> bool:
    """Broad sitemap-looking URL heuristic for non-standard XML wrappers.

    This broader helper is intentionally retained for XML documents that contain
    nested ``<loc>`` values outside a standard ``sitemapindex``. A human-facing
    HTML page called ``/sitemap`` can therefore still be recognised as a possible
    nested document *only* when we are already parsing a non-standard XML wrapper.
    """
    parsed = urlparse(url)
    path = (parsed.path or "").lower()
    last = path.rsplit("/", 1)[-1]
    return (
        path.endswith(".xml")
        or path.endswith(".xml.gz")
        or "sitemap" in last
        or last in {"sitemap", "sitemap_index", "sitemap-index"}
    )


def _looks_like_explicit_xml_sitemap_url(url: str) -> bool:
    """Return True only for URLs that are explicitly XML sitemap documents.

    Standard ``urlset`` files frequently contain a normal human-facing page named
    ``/sitemap``. Treating every such page as another XML sitemap caused valid
    sources such as HDFC Life and Axis Max Life to be marked partial when that
    HTML page naturally returned ``<html>``. Inside a normal ``urlset`` we only
    recurse when the URL is explicitly XML/GZIP XML.
    """
    path = (urlparse(url).path or "").lower()
    return path.endswith(".xml") or path.endswith(".xml.gz")


async def _read_limited_response(response: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise RuntimeError(f"Response exceeds {max_bytes:,} bytes: {response.request.url}")
        chunks.append(chunk)
    return b"".join(chunks)


async def _get_with_safe_redirects(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    max_redirects: int,
    dns_cache: dict[str, bool],
) -> tuple[str, int, dict[str, str], bytes]:
    current = url
    for _ in range(max_redirects + 1):
        await validate_public_url(current, dns_cache=dns_cache)
        async with client.stream("GET", current, follow_redirects=False) as response:
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    raise RuntimeError(f"Redirect without Location from {current}")
                next_url = urljoin(current, location)
                # Validate the redirect before any request is sent to the destination.
                await validate_public_url(next_url, dns_cache=dns_cache)
                current = next_url
                continue
            body = await _read_limited_response(response, max_bytes)
            return (
                current,
                int(response.status_code),
                {str(k).lower(): str(v) for k, v in response.headers.items()},
                body,
            )
    raise RuntimeError(f"Too many redirects for {url}")


def _retry_delay(headers: dict[str, str], attempt: int) -> float:
    value = (headers.get("retry-after") or "").strip()
    if value:
        try:
            return max(0.0, min(float(value), 30.0))
        except ValueError:
            try:
                dt = parsedate_to_datetime(value)
                seconds = dt.timestamp() - __import__("time").time()
                return max(0.0, min(seconds, 30.0))
            except Exception:
                pass
    return min(15.0, settings.sitemap_fetch_backoff_base_seconds * (2 ** max(0, attempt)))


async def _browser_fetch_xml(
    url: str,
    *,
    max_bytes: int,
    dns_cache: dict[str, bool],
) -> tuple[str, int, dict[str, str], bytes]:
    """Fetch a sitemap with normal Chromium when HTTP/XML fetching is blocked.

    Navigation requests are SSRF-validated individually, including redirects.
    """
    from playwright.async_api import async_playwright

    await validate_public_url(url, dns_cache=dns_cache)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent=settings.sitemap_http_user_agent,
            viewport={"width": 1440, "height": 900},
        )

        async def route_handler(route):
            req = route.request
            if req.is_navigation_request():
                try:
                    await validate_public_url(req.url, dns_cache=dns_cache)
                except Exception:
                    await route.abort()
                    return
            elif req.resource_type in {"image", "media", "font", "stylesheet"}:
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", route_handler)
        page = await context.new_page()
        try:
            response = await page.goto(
                url,
                wait_until="commit",
                timeout=min(settings.crawl_timeout_ms, 30000),
            )
            if response is None:
                raise RuntimeError("Browser navigation returned no response")
            await validate_public_url(page.url, dns_cache=dns_cache)
            status = int(response.status)
            body = await response.body()
            if len(body) > max_bytes:
                raise RuntimeError(f"Browser sitemap response exceeds {max_bytes:,} bytes")
            headers = {str(k).lower(): str(v) for k, v in (await response.all_headers()).items()}
            return page.url, status, headers, body
        finally:
            await context.close()
            await browser.close()


async def _fetch_sitemap_document(
    client: httpx.AsyncClient,
    sitemap_url: str,
    *,
    dns_cache: dict[str, bool],
) -> tuple[str, int, dict[str, str], bytes, str, int]:
    """HTTP first -> bounded retries/backoff -> browser fallback."""
    last_error: Exception | None = None
    last_status: int | None = None
    last_headers: dict[str, str] = {}

    for attempt in range(settings.sitemap_fetch_max_retries):
        try:
            final_url, status, headers, body = await _get_with_safe_redirects(
                client,
                sitemap_url,
                max_bytes=settings.sitemap_max_xml_bytes,
                max_redirects=settings.sitemap_max_redirects,
                dns_cache=dns_cache,
            )
            last_status, last_headers = status, headers
            if status < 400:
                return final_url, status, headers, body, "http", attempt
            last_error = RuntimeError(f"HTTP {status}")
            if status in _BROWSER_FALLBACK_STATUSES and status not in _RETRYABLE_STATUSES:
                break
            if status not in _RETRYABLE_STATUSES:
                raise last_error
        except Exception as exc:
            last_error = exc

        if attempt + 1 < settings.sitemap_fetch_max_retries:
            await asyncio.sleep(_retry_delay(last_headers, attempt))

    try:
        final_url, status, headers, body = await _browser_fetch_xml(
            sitemap_url,
            max_bytes=settings.sitemap_max_xml_bytes,
            dns_cache=dns_cache,
        )
        if status >= 400:
            raise RuntimeError(f"Browser fallback HTTP {status}")
        return final_url, status, headers, body, "browser", settings.sitemap_fetch_max_retries
    except Exception as browser_exc:
        raise RuntimeError(
            f"HTTP sitemap fetch failed ({last_error or ('HTTP ' + str(last_status))}); "
            f"browser fallback failed ({browser_exc})"
        ) from browser_exc


def _xml_local_name(element) -> str:
    return etree.QName(element.tag).localname if isinstance(element.tag, str) else ""


def _first_child_text(parent, local_name: str) -> str | None:
    for child in parent:
        if _xml_local_name(child) == local_name:
            value = (child.text or "").strip()
            return value or None
    return None


def _decode_xml(payload: bytes, content_encoding: str | None, url: str) -> bytes:
    if (
        payload[:2] == b"\x1f\x8b"
        or (content_encoding or "").lower() == "gzip"
        or url.lower().endswith(".gz")
    ):
        try:
            return gzip.decompress(payload)
        except OSError:
            return payload
    return payload


def _parse_xml(payload: bytes):
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=True,
        huge_tree=False,
    )
    return etree.fromstring(payload, parser=parser)


def _empty_xml_kind(root) -> str | None:
    """Return a stable empty-document kind for a valid sitemap XML root."""
    kind = _xml_local_name(root)
    if kind == "urlset":
        url_nodes = [node for node in root if _xml_local_name(node) == "url"]
        if not url_nodes:
            return "empty_urlset"
    elif kind == "sitemapindex":
        sitemap_nodes = [node for node in root if _xml_local_name(node) == "sitemap"]
        if not sitemap_nodes:
            return "empty_sitemapindex"
    return None


async def _verify_empty_child_body(
    client: httpx.AsyncClient,
    sitemap_url: str,
    *,
    dns_cache: dict[str, bool],
) -> tuple[str, int, dict[str, str], bytes, str, int, int]:
    """Re-check an HTTP-200 child sitemap whose decoded response body is empty.

    Empty child bodies are surprisingly common on large CMS/CDN estates and may
    be transient.  We therefore require bounded successful re-observation before
    downgrading the child to an auditable warning.  A later non-empty response is
    used immediately.  If verification cannot establish emptiness, the caller
    receives an exception so the document remains a real discovery failure.
    """
    attempts = max(0, int(settings.sitemap_empty_child_verify_attempts))
    delay = max(0.0, float(settings.sitemap_empty_child_verify_delay_seconds))
    successful_empty_observations = 1  # the caller already observed one empty 2xx body
    last_tuple = None
    last_error: Exception | None = None

    for verify_index in range(attempts):
        if delay:
            await asyncio.sleep(delay)
        try:
            result = await _fetch_sitemap_document(
                client,
                sitemap_url,
                dns_cache=dns_cache,
            )
            final_url, status, headers, body, fetch_method, retry_count = result
            decoded = _decode_xml(body, headers.get("content-encoding"), final_url)
            last_tuple = (
                final_url,
                status,
                headers,
                decoded,
                fetch_method,
                retry_count,
                verify_index + 1,
            )
            if decoded.strip():
                return last_tuple
            successful_empty_observations += 1
            if successful_empty_observations >= 2:
                return last_tuple
        except Exception as exc:  # bounded verification; preserve real failure semantics
            last_error = exc

    if successful_empty_observations >= 2 and last_tuple is not None:
        return last_tuple
    if last_error is not None:
        raise RuntimeError(f"Empty child sitemap verification failed: {last_error}") from last_error
    raise RuntimeError("Empty child sitemap could not be confirmed within verification budget")


def _generic_nested_xml_locations(root, final_url: str) -> list[str]:
    """Recover nested XML references from non-standard XML sitemap wrappers."""
    found: list[str] = []
    for node in root.iter():
        if _xml_local_name(node) != "loc":
            continue
        value = (node.text or "").strip()
        if not value:
            continue
        candidate = urljoin(final_url, value)
        if _looks_like_sitemap_url(candidate):
            found.append(candidate)
    return found


async def discover_sitemap_inventory(
    root_sitemap: str,
    *,
    max_urls: int | None = None,
    max_sitemaps: int | None = None,
) -> tuple[list[SitemapEntry], list[dict], dict]:
    """Discover the authoritative recursive sitemap inventory without hidden host filtering.

    Every final ``<url><loc>`` is retained and scope-classified. Resolution scope
    is applied later by the job runner, so raw sitemap truth and crawl workload
    remain separate and auditable.
    """
    max_urls = max_urls or settings.sitemap_max_urls_per_source
    max_sitemaps = max_sitemaps or settings.sitemap_max_child_sitemaps

    timeout = httpx.Timeout(settings.sitemap_request_timeout_seconds)
    headers = {
        "User-Agent": settings.sitemap_http_user_agent,
        "Accept": "application/xml,text/xml,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "en-IN,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    limits = httpx.Limits(
        max_connections=max(10, settings.sitemap_resolve_concurrency * 2),
        max_keepalive_connections=10,
    )

    queue: deque[tuple[str, int, str | None]] = deque([(root_sitemap, 0, None)])
    queued: set[str] = {normalize_url(root_sitemap)}
    visited: set[str] = set()
    seen_urls: dict[str, SitemapEntry] = {}
    duplicate_evidence: list[dict] = []
    entries: list[SitemapEntry] = []
    issues: list[dict] = []
    dns_cache: dict[str, bool] = {}
    raw_url_records = 0
    max_depth_seen = 0
    failed_xmls = 0
    empty_child_sitemaps = 0
    root_document_parsed = False
    external_sitemap_documents = 0

    def enqueue(candidate: str, depth: int, parent: str | None):
        nonlocal external_sitemap_documents
        if depth > settings.sitemap_max_recursion_depth:
            issues.append({
                "sitemap": candidate,
                "parent_sitemap": parent,
                "severity": "warning",
                "code": "SITEMAP_DEPTH_LIMIT_REACHED",
                "error": f"Sitemap recursion depth limit {settings.sitemap_max_recursion_depth} reached",
            })
            return
        if classify_url_scope(root_sitemap, candidate) == "EXTERNAL_DOMAIN":
            external_sitemap_documents += 1
        key = normalize_url(candidate)
        if key in queued or key in visited:
            return
        queued.add(key)
        queue.append((candidate, depth, parent))

    async with httpx.AsyncClient(timeout=timeout, headers=headers, limits=limits) as client:
        while queue and len(visited) < max_sitemaps and len(entries) < max_urls:
            sitemap_url, depth, parent = queue.popleft()
            max_depth_seen = max(max_depth_seen, depth)
            normalized_sitemap = normalize_url(sitemap_url)
            if normalized_sitemap in visited:
                continue
            visited.add(normalized_sitemap)

            try:
                final_url, status, response_headers, body, fetch_method, retry_count = await _fetch_sitemap_document(
                    client,
                    sitemap_url,
                    dns_cache=dns_cache,
                )
                body = _decode_xml(body, response_headers.get("content-encoding"), final_url)
                verification_attempts = 0

                if len(body) > settings.sitemap_max_xml_bytes:
                    raise RuntimeError(
                        f"Decompressed XML exceeds {settings.sitemap_max_xml_bytes:,} bytes"
                    )

                if not body.strip():
                    if parent is None:
                        raise RuntimeError("Root sitemap returned an empty response body")
                    (
                        final_url,
                        status,
                        response_headers,
                        body,
                        fetch_method,
                        retry_count,
                        verification_attempts,
                    ) = await _verify_empty_child_body(
                        client,
                        sitemap_url,
                        dns_cache=dns_cache,
                    )
                    if not body.strip():
                        empty_child_sitemaps += 1
                        issues.append({
                            "sitemap": sitemap_url,
                            "final_sitemap": final_url,
                            "parent_sitemap": parent,
                            "severity": "warning",
                            "code": "EMPTY_CHILD_SITEMAP",
                            "http_status": status,
                            "fetch_method": fetch_method,
                            "retry_count": retry_count,
                            "verification_attempts": verification_attempts,
                            "empty_kind": "empty_body",
                            "error": "Child sitemap returned an empty body and remained empty during bounded verification",
                        })
                        continue

                root = _parse_xml(body)
                kind = _xml_local_name(root)
                if parent is None:
                    root_document_parsed = True

                empty_kind = _empty_xml_kind(root)
                if empty_kind:
                    if parent is None:
                        raise RuntimeError(f"Root sitemap is a valid but empty <{kind}> document")
                    empty_child_sitemaps += 1
                    issues.append({
                        "sitemap": sitemap_url,
                        "final_sitemap": final_url,
                        "parent_sitemap": parent,
                        "severity": "warning",
                        "code": "EMPTY_CHILD_SITEMAP",
                        "http_status": status,
                        "fetch_method": fetch_method,
                        "retry_count": retry_count,
                        "verification_attempts": verification_attempts,
                        "empty_kind": empty_kind,
                        "error": f"Child sitemap is a valid empty <{kind}> document",
                    })
                    continue

                if kind == "sitemapindex":
                    for node in root:
                        if _xml_local_name(node) != "sitemap":
                            continue
                        loc = _first_child_text(node, "loc")
                        if loc:
                            enqueue(urljoin(final_url, loc), depth + 1, final_url)

                elif kind == "urlset":
                    for node in root:
                        if _xml_local_name(node) != "url":
                            continue
                        loc = _first_child_text(node, "loc")
                        if not loc:
                            continue
                        resolved = urljoin(final_url, loc)

                        if settings.sitemap_follow_xml_links_in_urlset and _looks_like_explicit_xml_sitemap_url(resolved):
                            enqueue(resolved, depth + 1, final_url)
                            continue

                        raw_url_records += 1
                        normalized = normalize_url(resolved)
                        lastmod = _first_child_text(node, "lastmod")
                        host = _host_key(urlparse(resolved).hostname)
                        current_entry = SitemapEntry(
                            url=resolved,
                            child_sitemap=final_url,
                            lastmod=lastmod,
                            hostname=host,
                            scope_class=classify_url_scope(root_sitemap, resolved),
                            obvious_non_page=is_obvious_non_page_url(resolved),
                        )

                        first_entry = seen_urls.get(normalized)
                        if first_entry is not None:
                            duplicate_evidence.append({
                                "duplicate_kind": (
                                    "SITEMAP_EXACT_DUPLICATE"
                                    if resolved == first_entry.url
                                    else "SITEMAP_NORMALIZED_DUPLICATE"
                                ),
                                "match_url": normalized,
                                "primary_url": first_entry.url,
                                "duplicate_url": resolved,
                                "primary_child_sitemap": first_entry.child_sitemap,
                                "duplicate_child_sitemap": final_url,
                                "primary_lastmod": first_entry.lastmod,
                                "duplicate_lastmod": lastmod,
                            })
                            continue

                        seen_urls[normalized] = current_entry
                        entries.append(current_entry)
                        if len(entries) >= max_urls:
                            break

                else:
                    nested = _generic_nested_xml_locations(root, final_url)
                    if nested:
                        for child in nested:
                            enqueue(child, depth + 1, final_url)
                        issues.append({
                            "sitemap": sitemap_url,
                            "parent_sitemap": parent,
                            "severity": "warning",
                            "code": "NON_STANDARD_XML_WRAPPER",
                            "error": f"Unsupported XML root <{kind or 'unknown'}>; recovered {len(nested)} nested XML reference(s)",
                        })
                    else:
                        raise RuntimeError(f"Unsupported XML root <{kind or 'unknown'}>")

                if fetch_method == "browser":
                    issues.append({
                        "sitemap": sitemap_url,
                        "parent_sitemap": parent,
                        "severity": "warning",
                        "code": "BROWSER_FALLBACK_USED",
                        "retry_count": retry_count,
                        "http_status": status,
                        "error": "Normal HTTP sitemap fetch was blocked/failed; browser fallback succeeded",
                    })

            except Exception as exc:
                failed_xmls += 1
                logger.warning("Sitemap fetch failed %s: %s", sitemap_url, exc)
                issues.append({
                    "sitemap": sitemap_url,
                    "parent_sitemap": parent,
                    "severity": "error",
                    "code": "SITEMAP_FETCH_FAILED",
                    "error": str(exc),
                })

    if queue and len(visited) >= max_sitemaps:
        issues.append({
            "sitemap": root_sitemap,
            "severity": "warning",
            "code": "CHILD_SITEMAP_LIMIT_REACHED",
            "error": f"Sitemap document safety limit {max_sitemaps} reached; {len(queue)} document(s) remain unexplored",
        })
    if len(entries) >= max_urls:
        issues.append({
            "sitemap": root_sitemap,
            "severity": "warning",
            "code": "URL_LIMIT_REACHED",
            "error": f"Unique sitemap URL safety limit {max_urls} reached; results truncated",
        })

    scope_counts = {
        "root_host_urls": sum(1 for x in entries if x.scope_class == "ROOT_HOST"),
        "related_subdomain_urls": sum(1 for x in entries if x.scope_class == "RELATED_SUBDOMAIN"),
        "external_domain_urls": sum(1 for x in entries if x.scope_class == "EXTERNAL_DOMAIN"),
        "obvious_non_page_urls": sum(1 for x in entries if x.obvious_non_page),
    }
    stats = {
        "raw_url_records": raw_url_records,
        "unique_sitemap_urls": len(entries),
        "duplicate_url_records": len(duplicate_evidence),
        # Kept out of run config/analysis JSON. The job runner persists these
        # rows into sitemap_duplicates and removes this list from stats first.
        "duplicate_evidence": duplicate_evidence,
        "xmls_traversed": len(visited),
        "max_depth": max_depth_seen,
        "failed_xmls": failed_xmls,
        "empty_child_sitemaps": empty_child_sitemaps,
        "root_document_parsed": root_document_parsed,
        "external_sitemap_documents_declared": external_sitemap_documents,
        **scope_counts,
    }
    return entries, issues, stats


async def discover_sitemap_urls(
    root_sitemap: str,
    *,
    max_urls: int | None = None,
    max_sitemaps: int | None = None,
) -> tuple[list[SitemapEntry], list[dict]]:
    """Backward-compatible wrapper returning the full authoritative unique inventory."""
    entries, issues, _stats = await discover_sitemap_inventory(
        root_sitemap,
        max_urls=max_urls,
        max_sitemaps=max_sitemaps,
    )
    return entries, issues


