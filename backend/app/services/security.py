from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlparse

import httpx

from app.config import get_settings

settings = get_settings()

# Module-local short-lived cache. It stores only the result of independent public
# DNS verification; it is never populated from user input alone.
_PUBLIC_DNS_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}

_RESERVED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
    ".invalid",
    ".test",
)


def _normalized_host(host: str | None) -> str:
    return (host or "").strip().lower().rstrip(".")


def _is_global_ip(value: str) -> bool:
    try:
        obj = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(obj.is_global and not any([
        obj.is_private,
        obj.is_loopback,
        obj.is_link_local,
        obj.is_multicast,
        obj.is_reserved,
        obj.is_unspecified,
    ]))


def _is_blocked_ip(value: str) -> bool:
    return not _is_global_ip(value)


def _validate_url_shape(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL hostname is required")
    if parsed.username or parsed.password:
        raise ValueError("Credentials embedded in URLs are not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid URL port") from exc
    if port not in {None, 80, 443}:
        raise ValueError("Only standard HTTP/HTTPS ports are allowed")
    return parsed


async def _resolve_system_ips(host: str, port: int) -> set[str]:
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed for {host}") from exc
    return {str(item[4][0]).split("%")[0] for item in infos if item and item[4]}


def _extract_dns_json_ips(payload: dict) -> set[str]:
    values: set[str] = set()
    for row in payload.get("Answer") or []:
        data = str(row.get("data") or "").strip().strip('"')
        if _is_global_ip(data):
            values.add(data)
    return values


async def _query_doh_provider(base_url: str, host: str) -> set[str]:
    timeout = httpx.Timeout(settings.sitemap_public_dns_timeout_seconds)
    headers = {
        "Accept": "application/dns-json, application/json",
        "User-Agent": settings.sitemap_http_user_agent,
    }
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=False) as client:
        async def one(qtype: str) -> set[str]:
            response = await client.get(base_url, params={"name": host, "type": qtype})
            response.raise_for_status()
            return _extract_dns_json_ips(response.json())

        results = await asyncio.gather(one("A"), one("AAAA"), return_exceptions=True)
        ips: set[str] = set()
        for result in results:
            if isinstance(result, set):
                ips.update(result)
        return ips


async def _resolve_public_ips(host: str) -> set[str]:
    now = time.monotonic()
    cached = _PUBLIC_DNS_CACHE.get(host)
    if cached and cached[0] > now:
        return set(cached[1])

    # Two independent public JSON DNS providers are attempted. If neither can be
    # reached, validation stays strict; we never silently trust the user hostname.
    providers = (
        "https://dns.google/resolve",
        "https://cloudflare-dns.com/dns-query",
    )
    ips: set[str] = set()
    for provider in providers:
        try:
            ips = await _query_doh_provider(provider, host)
            if ips:
                break
        except Exception:
            continue

    if ips:
        _PUBLIC_DNS_CACHE[host] = (
            now + max(10, settings.sitemap_public_dns_cache_ttl_seconds),
            tuple(sorted(ips)),
        )
    return ips


async def validate_public_url(url: str, dns_cache: dict[str, bool] | None = None) -> None:
    """Validate a user-controlled HTTP(S) URL against SSRF risks.

    Enterprise/corporate DNS often maps legitimate public internet hostnames to
    private proxy/security-gateway IPs. In that case the function independently
    verifies that the hostname has a globally routable public-DNS answer before
    permitting the corporate route. This is domain-agnostic: no customer/domain
    allowlist is required.

    Literal private IPs, localhost/reserved names, non-standard ports and unsafe
    redirect destinations remain blocked.
    """
    parsed = _validate_url_shape(url)
    host = _normalized_host(parsed.hostname)

    if not settings.block_private_networks:
        return

    if host in {"localhost", "localhost.localdomain"} or host.endswith(_RESERVED_HOST_SUFFIXES):
        raise ValueError("Local/private targets are blocked")

    # IP-literal URLs are decided entirely from the literal. A hostname-based
    # corporate-DNS exception can never turn a private literal into an allowed URL.
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_global_ip(str(literal)):
            raise ValueError("Local/private targets are blocked")
        return

    if dns_cache is not None and host in dns_cache:
        if dns_cache[host]:
            return
        raise ValueError("URL resolves to a local/private/reserved network")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    system_ips = await _resolve_system_ips(host, port)
    if not system_ips:
        if dns_cache is not None:
            dns_cache[host] = False
        raise ValueError(f"DNS resolution returned no addresses for {host}")

    # Normal internet environment: every candidate destination is public.
    if all(_is_global_ip(ip) for ip in system_ips):
        if dns_cache is not None:
            dns_cache[host] = True
        return

    # Mixed/private system DNS is rejected unless an independent public-DNS
    # lookup proves the hostname is globally routed and corporate-proxy mode is
    # enabled. This handles enterprise DNS/VPN gateways without fixed allowlists.
    if settings.sitemap_public_dns_fallback_enabled and settings.sitemap_allow_corporate_dns_proxy:
        public_ips = await _resolve_public_ips(host)
        if public_ips and all(_is_global_ip(ip) for ip in public_ips):
            if dns_cache is not None:
                dns_cache[host] = True
            return

    if dns_cache is not None:
        dns_cache[host] = False
    raise ValueError("URL resolves to a local/private/reserved network")
