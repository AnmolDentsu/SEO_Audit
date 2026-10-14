from __future__ import annotations

import asyncio

import pytest

from app.services import sitemap_page_resolver as resolver


@pytest.mark.asyncio
async def test_host_access_circuit_defers_remaining_queue(monkeypatch):
    monkeypatch.setattr(resolver.settings, "sitemap_access_block_window_size", 4)
    monkeypatch.setattr(resolver.settings, "sitemap_access_block_min_samples", 4)
    monkeypatch.setattr(resolver.settings, "sitemap_access_block_ratio", 0.75)
    monkeypatch.setattr(resolver.settings, "sitemap_http_min_per_host_concurrency", 1)

    calls = 0

    async def fetch_one(url: str):
        nonlocal calls
        calls += 1
        return {
            "requested_url": url,
            "final_url": url,
            "status_code": 403,
            "error": "HTTP 403",
            "fetch_method": "http",
            "terminal_state": "fetch_failed",
            "retryable": False,
        }

    urls = [f"https://blocked.example/page-{i}" for i in range(40)]
    rows = await resolver._host_fair_map(
        urls,
        url_getter=lambda x: x,
        fetch_one=fetch_one,
        concurrency=4,
        per_host_limit=2,
        initial_per_host_limit=2,
        ramp_successes=100,
        progress_label="test",
    )

    assert len(rows) == len(urls)
    assert calls < len(urls)
    assert any(x.get("terminal_state") == "host_access_deferred" for x in rows)
    assert all(x.get("retryable") is True for x in rows if x.get("host_access_deferred"))


@pytest.mark.asyncio
async def test_blocked_host_does_not_stop_healthy_host(monkeypatch):
    monkeypatch.setattr(resolver.settings, "sitemap_access_block_window_size", 4)
    monkeypatch.setattr(resolver.settings, "sitemap_access_block_min_samples", 4)
    monkeypatch.setattr(resolver.settings, "sitemap_access_block_ratio", 0.75)
    monkeypatch.setattr(resolver.settings, "sitemap_http_min_per_host_concurrency", 1)

    calls = {"blocked.example": 0, "healthy.example": 0}

    async def fetch_one(url: str):
        host = "blocked.example" if "blocked.example" in url else "healthy.example"
        calls[host] += 1
        if host == "blocked.example":
            return {
                "requested_url": url,
                "final_url": url,
                "status_code": 403,
                "error": "HTTP 403",
                "fetch_method": "http",
                "terminal_state": "fetch_failed",
                "retryable": False,
            }
        return {
            "requested_url": url,
            "final_url": url,
            "status_code": 200,
            "error": None,
            "fetch_method": "http",
            "terminal_state": "usable",
            "retryable": False,
        }

    urls = []
    for i in range(20):
        urls.append(f"https://blocked.example/page-{i}")
        urls.append(f"https://healthy.example/page-{i}")

    rows = await resolver._host_fair_map(
        urls,
        url_getter=lambda x: x,
        fetch_one=fetch_one,
        concurrency=8,
        per_host_limit=2,
        initial_per_host_limit=2,
        ramp_successes=100,
        progress_label="test",
    )

    healthy_rows = [x for x in rows if "healthy.example" in x.get("requested_url", "")]
    blocked_rows = [x for x in rows if "blocked.example" in x.get("requested_url", "")]

    assert len(healthy_rows) == 20
    assert all(x.get("status_code") == 200 and not x.get("error") for x in healthy_rows)
    assert calls["healthy.example"] == 20
    assert calls["blocked.example"] < 20
    assert any(x.get("terminal_state") == "host_access_deferred" for x in blocked_rows)



@pytest.mark.asyncio
async def test_host_fair_map_respects_minimum_start_interval():
    starts: list[float] = []

    async def fetch_one(url: str):
        starts.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0)
        return {
            "requested_url": url,
            "final_url": url,
            "status_code": 200,
            "error": None,
            "fetch_method": "http",
            "terminal_state": "usable",
            "retryable": False,
        }

    rows = await resolver._host_fair_map(
        [f"https://paced.example/page-{i}" for i in range(3)],
        url_getter=lambda x: x,
        fetch_one=fetch_one,
        concurrency=3,
        per_host_limit=3,
        initial_per_host_limit=3,
        ramp_successes=100,
        min_start_interval_seconds=0.03,
        progress_label="paced-test",
    )

    assert len(rows) == 3
    assert len(starts) == 3
    assert starts[1] - starts[0] >= 0.02
    assert starts[2] - starts[1] >= 0.02
