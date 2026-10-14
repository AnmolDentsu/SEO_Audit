from app.services.text import BatchKeywordAnalyzer, density_pct


def test_density_formula_occurrences_over_total_words():
    # 11 total words; the whole phrase occurs twice.
    # Density = 2 / 11 * 100.
    text = "sip calculator helps sip calculator with long term monthly investing plans"
    count, density, total = density_pct(text, "sip calculator")
    assert total == 11
    assert count == 2
    assert density == round(2 / 11 * 100, 4)


def test_batch_matches_single_phrase_logic():
    text = "sip calculator and mutual funds with another sip calculator"
    items = [(1, "sip calculator"), (2, "mutual funds"), (3, "absent phrase")]
    analyzer = BatchKeywordAnalyzer(items)
    result = analyzer.analyze(text)

    for key, phrase in items:
        assert result[key] == density_pct(text, phrase)


def test_batch_counts_overlapping_exact_phrases():
    analyzer = BatchKeywordAnalyzer([(1, "a a")])
    count, density, total = analyzer.analyze("a a a")[1]
    assert total == 3
    assert count == 2
    assert round(density, 4) == round(2 / 3 * 100, 4)
