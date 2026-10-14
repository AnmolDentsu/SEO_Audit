import pytest

from app.services.crawler import (
    PAGE_STRUCTURE_VERSION,
    _canonical_structure_from_page,
)


def _structure(text: str, *, root: str = "body") -> dict:
    return {
        "root": root,
        "title": "Example",
        "meta_description": "",
        "canonical_url": "https://example.com/",
        "main_text": text,
        "headings": [{"order": 1, "level": 1, "text": "Example", "id": ""}],
        "sections": [
            {
                "order": 1,
                "heading_level": 1,
                "heading": "Example",
                "text": text,
                "word_count": len(text.split()),
            }
        ],
        "faqs": [],
        "paragraph_count": 1,
        "h1": ["Example"],
        "h2": [],
        "h3": [],
        "h4": [],
        "h5": [],
        "h6": [],
    }


class RenderedWinsPage:
    def __init__(self):
        self.content_called = False

    async def evaluate(self, _script):
        return _structure("payment gateways are visible once")

    async def content(self):
        self.content_called = True
        raise AssertionError(
            "static DOM must not be read when rendered extraction is healthy"
        )


@pytest.mark.asyncio
async def test_rendered_visible_extraction_is_authoritative():
    page = RenderedWinsPage()
    result = await _canonical_structure_from_page(
        page,
        "https://example.com/",
    )

    assert result["main_text"] == "payment gateways are visible once"
    assert result["_extraction"]["engine"] == "rendered-fullpage-innertext-v6-adaptive"
    assert result["_extraction"]["fallback_used"] is False
    assert page.content_called is False


class StaticFallbackPage:
    async def evaluate(self, _script):
        raise RuntimeError("rendered extractor unavailable")

    async def content(self):
        text = " ".join(["payment", "gateway"] * 30)
        return f"<html><body><h1>Example</h1><p>{text}</p></body></html>"


@pytest.mark.asyncio
async def test_static_html_is_fallback_only():
    result = await _canonical_structure_from_page(
        StaticFallbackPage(),
        "https://example.com/",
    )

    assert result["main_text"]
    assert result["_extraction"]["engine"] == "canonical-html-v5-fallback"
    assert result["_extraction"]["fallback_used"] is True


def test_structure_version_bumped_for_cache_invalidation():
    assert PAGE_STRUCTURE_VERSION >= 6
