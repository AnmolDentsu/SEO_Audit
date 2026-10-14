import pytest

from app.services.crawler import (
    PAGE_STRUCTURE_VERSION,
    PAGE_HYDRATION_MAX_ELAPSED_MS,
    PAGE_HYDRATION_STABLE_ROUNDS,
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
        "sections": [{
            "order": 1,
            "heading_level": 1,
            "heading": "Example",
            "text": text,
            "word_count": len(text.split()),
        }],
        "faqs": [],
        "paragraph_count": 1,
        "h1": ["Example"],
        "h2": [], "h3": [], "h4": [], "h5": [], "h6": [],
        "browser_environment": {"user_agent": "Chrome"},
    }


class RenderedWinsPage:
    def __init__(self):
        self.content_called = False
        self.calls = 0
        self.hydration_options = None

    async def evaluate(self, _script, arg=None):
        self.calls += 1
        if self.calls == 1:
            self.hydration_options = arg
            return {
                "rounds": 3,
                "total_scroll_steps": 31,
                "initial_height": 7000,
                "final_height": 8000,
                "initial_text_length": 3000,
                "final_text_length": 5600,
                "stable_rounds": 2,
                "complete": True,
                "stop_reason": "stable",
                "observed_growth": True,
                "elapsed_ms": 3100,
            }
        return _structure(
            "Header payment gateway Footer payment gateways Button"
        )

    async def content(self):
        self.content_called = True
        raise AssertionError(
            "static DOM must not be read when rendered extraction is healthy"
        )


@pytest.mark.asyncio
async def test_adaptively_hydrated_full_rendered_body_is_authoritative():
    page = RenderedWinsPage()
    result = await _canonical_structure_from_page(page, "https://example.com/")

    assert "Header" in result["main_text"]
    assert "Footer" in result["main_text"]
    assert "Button" in result["main_text"]

    extraction = result["_extraction"]
    assert extraction["engine"] == "rendered-fullpage-innertext-v6-adaptive"
    assert extraction["keyword_corpus"] == "document.body.innerText"
    assert extraction["keyword_semantic_exclusions"] == []
    assert extraction["keyword_corpus_complete"] is True
    assert extraction["keyword_corpus_stop_reason"] == "stable"
    assert extraction["fallback_used"] is False

    assert page.hydration_options["stable_rounds"] == PAGE_HYDRATION_STABLE_ROUNDS
    assert page.hydration_options["max_elapsed_ms"] == PAGE_HYDRATION_MAX_ELAPSED_MS
    assert page.content_called is False


class InfiniteLikePage:
    def __init__(self):
        self.calls = 0

    async def evaluate(self, _script, arg=None):
        self.calls += 1
        if self.calls == 1:
            return {
                "rounds": 30,
                "total_scroll_steps": 900,
                "initial_height": 5000,
                "final_height": 90000,
                "initial_text_length": 2500,
                "final_text_length": 50000,
                "stable_rounds": 0,
                "complete": False,
                "stop_reason": "time_budget",
                "observed_growth": True,
                "elapsed_ms": PAGE_HYDRATION_MAX_ELAPSED_MS,
            }
        return _structure("All rendered page text is still returned")

    async def content(self):
        raise AssertionError("healthy rendered extraction should remain authoritative")


@pytest.mark.asyncio
async def test_ever_growing_page_is_marked_incomplete_not_silently_complete():
    result = await _canonical_structure_from_page(
        InfiniteLikePage(), "https://example.com/feed"
    )
    extraction = result["_extraction"]
    assert extraction["keyword_corpus_complete"] is False
    assert extraction["keyword_corpus_stop_reason"] == "time_budget"
    assert extraction["hydration"]["observed_growth"] is True


class StaticFallbackPage:
    def __init__(self):
        self.calls = 0

    async def evaluate(self, _script, arg=None):
        self.calls += 1
        if self.calls == 1:
            return {
                "rounds": 1,
                "total_scroll_steps": 1,
                "initial_height": 800,
                "final_height": 800,
                "complete": True,
                "stop_reason": "stable",
            }
        raise RuntimeError("rendered extractor unavailable")

    async def content(self):
        return """
        <html><body>
          <header>payment gateway header</header>
          <nav>payment links nav</nav>
          <main><h1>Example</h1><p>payment gateway body</p></main>
          <footer>payment gateway footer</footer>
          <script>const hidden = 'payment gateway script';</script>
        </body></html>
        """


@pytest.mark.asyncio
async def test_static_fallback_density_corpus_keeps_all_human_page_areas():
    result = await _canonical_structure_from_page(
        StaticFallbackPage(), "https://example.com/"
    )
    body = result["main_text"]
    assert "payment gateway header" in body
    assert "payment links nav" in body
    assert "payment gateway body" in body
    assert "payment gateway footer" in body
    assert "payment gateway script" not in body
    assert result["_extraction"]["engine"] == "canonical-html-v5-fallback"


def test_structure_version_bumped_for_adaptive_hydration_cache_invalidation():
    assert PAGE_STRUCTURE_VERSION >= 9
