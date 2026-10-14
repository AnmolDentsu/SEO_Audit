from app.services.sitemap_analysis import _client_relevance, _priority_score
from app.services.sitemap_export import _clean_text, EXCEL_CELL_LIMIT


def test_client_relevance_rejects_unrelated_retail_category():
    score, closest, reason = _client_relevance(
        "Mattresses",
        {"Loans", "Insurance", "Investments"},
        {"Personal Loan", "Life Insurance", "Mutual Funds"},
    )
    assert score < 0.38
    assert "Closest client" in reason


def test_priority_score_is_transparent_and_bounded():
    priority, confidence, score, reason = _priority_score(4, 5, 17, 0, 0.9, "TOPIC_GAP")
    assert priority in {"HIGH", "MEDIUM", "LOW"}
    assert 0 <= score <= 100
    assert 0 <= confidence <= 0.97
    assert "support=" in reason and "client-relevance=" in reason


def test_excel_cell_cleaning_handles_illegal_chars_and_long_values():
    value = "hello\x00world" + ("x" * (EXCEL_CELL_LIMIT + 100))
    cleaned = _clean_text(value)
    assert "\x00" not in cleaned
    assert len(cleaned) == EXCEL_CELL_LIMIT
