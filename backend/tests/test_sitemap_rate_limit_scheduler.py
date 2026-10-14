import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest

from app.services.sitemap_page_resolver import (
    _HostRateState,
    _host_fair_map,
    _parse_retry_after,
)


def test_retry_after_supports_seconds_and_http_date():
    assert _parse_retry_after("7") == 7.0

    now = datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc)
    future = now + timedelta(seconds=12)
    header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    parsed = _parse_retry_after(header, now=now)
    assert parsed is not None
    assert 11.0 <= parsed <= 12.0


def test_429_shrinks_only_affected_host_state():
    state = _HostRateState(max_limit=8, min_limit=1, current_limit=4)
    state.observe({"status_code": 429, "error": "HTTP 429", "retry_after_seconds": 6}, 100.0)
    assert state.current_limit == 2
    assert state.cooldown_until == 106.0
    assert state.rate_limit_events == 1



@pytest.mark.asyncio
async def test_host_fair_scheduler_starts_second_host_before_draining_first(monkeypatch):
    from app.services import sitemap_page_resolver as resolver

    monkeypatch.setattr(resolver.settings, "sitemap_http_initial_per_host_concurrency", 2)
    monkeypatch.setattr(resolver.settings, "sitemap_http_min_per_host_concurrency", 1)

    items = [
        *[f"https://a.example/{i}" for i in range(20)],
        "https://b.example/1",
        "https://b.example/2",
    ]
    starts = []

    async def fetch_one(url):
        starts.append((urlparse(url).hostname, url))
        await asyncio.sleep(0.01)
        return {"requested_url": url, "final_url": url, "status_code": 200, "error": None}

    out = await _host_fair_map(
        items,
        url_getter=lambda x: x,
        fetch_one=fetch_one,
        concurrency=4,
        per_host_limit=2,
        progress_label="test",
    )

    assert len(out) == len(items)
    assert any(host == "b.example" for host, _ in starts[:4])
