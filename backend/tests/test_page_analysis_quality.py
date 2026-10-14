from types import SimpleNamespace

import app.services.content_analysis as ca


class FixedSimilarity:
    def __init__(self, score=0.9):
        self.score = score
    def similarity(self, a, b):
        return self.score


def page(pid, target_id, sections=None, faqs=None, title="Client Brand"):
    return SimpleNamespace(
        id=pid, target_id=target_id, error=None, url=f"https://example.com/{pid}",
        final_url=f"https://example.com/{pid}", title=title, meta_description="", word_count=500,
        structure_json={
            "title": title, "canonical_url": f"https://example.com/{pid}", "sections": sections or [],
            "faqs": faqs or [], "main_text": "", "headings": [],
        },
    )


def target(role):
    return SimpleNamespace(role=role, label=role)


def test_faq_high_embedding_alone_does_not_mark_covered(monkeypatch):
    monkeypatch.setattr(ca, "_similarity_engine", lambda: FixedSimilarity(0.90))
    client = page(1, 1, faqs=[{"order": 1, "question": "What is a SIP calculator?", "answer": "..."}])
    p0 = page(2, 2, faqs=[{"order": 1, "question": "What is rupee cost averaging?", "answer": "..."}])
    rows = ca.build_faq_comparison_rows([client, p0], {1: target("CLIENT"), 2: target("P0")})
    assert rows
    assert rows[0]["Coverage Status"] != "Covered"
    assert "65% semantic" in rows[0]["Decision Logic"]
    assert "Composite FAQ Match" in rows[0]


def test_section_comparison_exposes_auditable_formula(monkeypatch):
    monkeypatch.setattr(ca, "_similarity_engine", lambda: FixedSimilarity(0.95))
    client = page(1, 1, sections=[{"order":1,"heading":"SIP Calculator","heading_level":2,"text":"calculator", "word_count":20}])
    p0 = page(2, 2, sections=[{"order":1,"heading":"Benefits of SIP Investing","heading_level":2,"text":"benefits investing", "word_count":120}])
    rows = ca.build_section_comparison_rows([client, p0], {1: target("CLIENT"), 2: target("P0")}, [])
    assert rows
    row = rows[0]
    assert "Semantic Similarity" in row
    assert "Lexical Similarity" in row
    assert "Composite Topic Match" in row
    assert "Priority Score" in row
    assert "Priority Reason" in row
    assert row["Evidence Confidence"] <= 0.98
