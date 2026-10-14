from types import SimpleNamespace

import app.services.sitemap_analysis as sitemap_analysis


def _source(source_id, role, label):
    return SimpleNamespace(
        id=source_id,
        role=role,
        label=label,
        root_url=f"https://{label}.example/sitemap.xml",
    )


def _row(row_id, source_id, category, topic, page_type="CONTENT", subtopic=""):
    return SimpleNamespace(
        id=row_id,
        source_id=source_id,
        error=None,
        broad_page_type=page_type,
        detailed_page_type=page_type,
        category=category,
        topic=topic,
        subtopic=subtopic,
        final_url=f"https://example.test/{row_id}",
        original_url=f"https://example.test/{row_id}",
        title=topic,
        h1_json=[topic],
        word_count=500,
        http_status=200,
        retry_count=0,
        classification_evidence_json={
            "terminal_state": "usable",
            "taxonomy_eligible": True,
        },
    )


def test_coverage_uses_bounded_relevance_and_emits_progress(monkeypatch):
    client = _source(1, "CLIENT", "client")
    competitor = _source(2, "COMPETITOR", "competitor")

    rows = []
    row_id = 1

    # Enough unique client taxonomy labels to expose accidental all-vs-all
    # relevance scans, without making the test itself large.
    for i in range(250):
        rows.append(
            _row(
                row_id,
                1,
                f"Client Category {i % 25}",
                f"Client Topic {i}",
            )
        )
        row_id += 1

    for i in range(750):
        rows.append(
            _row(
                row_id,
                2,
                f"Competitor Category {i % 40}",
                f"Competitor Topic {i}",
            )
        )
        row_id += 1

    discovery = [
        {
            "source_id": 1,
            "status": "success",
            "discovered_urls": 250,
            "usable_urls": 250,
        },
        {
            "source_id": 2,
            "status": "success",
            "discovered_urls": 750,
            "usable_urls": 750,
        },
    ]

    original_similarity = sitemap_analysis._text_similarity
    calls = {"count": 0}

    def counted_similarity(a, b):
        calls["count"] += 1
        return original_similarity(a, b)

    monkeypatch.setattr(
        sitemap_analysis,
        "_text_similarity",
        counted_similarity,
    )

    progress = []
    analysis, opportunities, evidence = sitemap_analysis.build_sitemap_analysis(
        rows,
        [client, competitor],
        source_discovery=discovery,
        progress_callback=lambda fraction, message, current, total: progress.append(
            (fraction, message, current, total)
        ),
    )

    assert len(analysis["topic_coverage_matrix"]) == 1000
    assert len(evidence) == 1000
    assert progress
    assert progress[-1][0] == 1.0

    # A naive 1000 x 275 client-taxonomy scan would perform roughly 275k
    # expensive similarity calls. Candidate indexing must stay far below that.
    assert calls["count"] < 60_000

    # The regression test is about bounded analysis, not a specific number of
    # SEO opportunities; just ensure the pipeline returns a real list.
    assert isinstance(opportunities, list)


def test_missing_subtopic_attribute_is_safe():
    client = _source(1, "CLIENT", "client")
    competitor = _source(2, "COMPETITOR", "competitor")

    client_row = _row(1, 1, "Loans", "Personal Loan")
    competitor_row = _row(2, 2, "Loans", "Personal Loan")

    # Historical/test rows may predate the subtopic field.
    delattr(client_row, "subtopic")
    delattr(competitor_row, "subtopic")

    analysis, _, _ = sitemap_analysis.build_sitemap_analysis(
        [client_row, competitor_row],
        [client, competitor],
        source_discovery=[
            {"source_id": 1, "status": "success", "discovered_urls": 1},
            {"source_id": 2, "status": "success", "discovered_urls": 1},
        ],
    )

    assert analysis["topic_taxonomy"]
    assert analysis["topic_taxonomy"][0]["Subtopics"] == ""


def test_relevance_candidate_ties_are_deterministic(monkeypatch):
    """Equal retrieval scores must resolve identically across iteration orders."""
    state = {"flip": False}

    def alternating_tokens(_value):
        state["flip"] = not state["flip"]
        return ["beta", "alpha"] if state["flip"] else ["alpha", "beta"]

    monkeypatch.setattr(sitemap_analysis, "_retrieval_tokens", alternating_tokens)
    monkeypatch.setattr(sitemap_analysis, "_char_ngrams", lambda _value: [])
    monkeypatch.setattr(sitemap_analysis, "_text_similarity", lambda _a, _b: 0.5)

    index = {
        "records": [("Alpha Topic", "topic"), ("Beta Topic", "topic")],
        "token_index": {"alpha": [0], "beta": [1]},
        "gram_index": {},
        "exact": {},
    }

    results = []
    for i in range(10):
        result = sitemap_analysis._client_relevance_indexed(
            f"query-{i}",
            index,
            {},
            max_candidates=8,
        )
        results.append(result[1])

    assert results == ["Alpha Topic"] * 10
