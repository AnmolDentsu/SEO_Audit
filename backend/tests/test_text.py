from app.services.text import exact_phrase_count, density_pct


def test_exact_phrase_count_is_token_based():
    assert exact_phrase_count("Home loan, home-loan. HOME loan!", "home loan") == 2


def test_density_uses_phrase_occurrences_over_total_words():
    count, density, words = density_pct(
        "home loan is a useful home loan product",
        "home loan",
    )
    assert count == 2
    assert words == 8
    assert density == 25.0
