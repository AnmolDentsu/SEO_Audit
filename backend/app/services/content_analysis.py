from __future__ import annotations

import re
from statistics import median
from typing import Any

from app.services.embeddings import HeadingSimilarity
from app.services.text import normalize

SECTION_MATCH_STRONG = 0.78
SECTION_MATCH_WEAK = 0.58
FAQ_MATCH_STRONG = 0.80
FAQ_MATCH_WEAK = 0.64


def _token_set(text: Any) -> set[str]:
    stop = {"the","a","an","and","or","of","to","for","in","on","with","is","are","what","how","why","can","does","do"}
    return {x for x in _words(text) if len(x) > 1 and x not in stop}


def _lexical_similarity(a: Any, b: Any) -> float:
    sa, sb = _token_set(a), _token_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa | sb), 1)


def _question_intent(text: Any) -> str:
    words = _words(text)
    if not words:
        return "other"
    first = words[0]
    if first in {"what","how","why","when","where","who","which","can","does","do","is","are","should","will"}:
        return first
    return "other"


def _composite_match(semantic: float, lexical: float, semantic_weight: float = 0.75) -> float:
    return max(0.0, min(1.0, semantic_weight * float(semantic or 0) + (1-semantic_weight) * float(lexical or 0)))


def _section_priority_score(coverage: str, evidence: str, matched: list[dict], composite: float, depth_ratio: float) -> int:
    score = 0
    score += {"Missing": 48, "Weak Coverage": 28, "Covered": 0}.get(coverage, 0)
    score += 18 if evidence == "P0 + P1" else 8
    if any(x.get("keyword_type") == "PRIMARY" for x in matched):
        score += 18
    elif any(x.get("keyword_type") == "SECONDARY" for x in matched):
        score += 10
    if max([int(x.get("search_volume") or 0) for x in matched], default=0) >= 100000:
        score += 8
    if composite < SECTION_MATCH_WEAK:
        score += 6
    if coverage == "Weak Coverage" and depth_ratio < 0.5:
        score += 8
    return min(score, 100)


def _priority_from_score(score: int, coverage: str) -> str:
    if coverage == "Covered":
        return "No Change"
    if score >= 70:
        return "High"
    if score >= 45:
        return "Medium"
    return "Low"


def _public_ai_source(value: str | None) -> str:
    text = str(value or "")
    mapping = {
        "gemini": "ai-model",
        "gemini-new": "ai-model-new",
        "deterministic+gemini-reviewed": "deterministic+ai-reviewed",
    }
    return mapping.get(text, text.replace("gemini", "ai-model"))

def _notify_progress(progress, fraction: float, message: str = "", current=None, total=None):
    if progress is None:
        return
    progress(max(0.0, min(1.0, float(fraction))), message, current, total)


def _words(text: Any) -> list[str]:
    return re.findall(r"\b\w+\b", str(text or "").casefold(), flags=re.UNICODE)


def exact_phrase_present(text: Any, phrase: Any) -> bool:
    hay = _words(text)
    needle = _words(phrase)
    if not needle:
        return False
    n = len(needle)
    return any(hay[i : i + n] == needle for i in range(len(hay) - n + 1))


def _structure(page) -> dict[str, Any]:
    data = getattr(page, "structure_json", None) or {}
    if data:
        return data
    # Backward-compatible fallback for older rows.
    return {
        "root": getattr(page, "extraction_root", None) or "body",
        "title": getattr(page, "title", None) or "",
        "meta_description": getattr(page, "meta_description", None) or "",
        "canonical_url": getattr(page, "canonical_url", None) or "",
        "main_text": getattr(page, "body_text", None) or "",
        "headings": [
            *[{"level": 1, "text": x} for x in (getattr(page, "h1_json", None) or [])],
            *[{"level": 2, "text": x} for x in (getattr(page, "h2_json", None) or [])],
            *[{"level": 3, "text": x} for x in (getattr(page, "h3_json", None) or [])],
        ],
        "sections": [],
        "faqs": [],
        "paragraph_count": getattr(page, "paragraph_count", None) or 0,
    }


def _priority_pages(pages, targets_by_id):
    by_role = {}
    for page in pages:
        target = targets_by_id.get(page.target_id)
        if target and not page.error:
            by_role[target.role.upper()] = page
    return by_role


def _target_label(page, targets_by_id) -> str:
    t = targets_by_id.get(page.target_id)
    return (t.label or t.role) if t else ""


def _similarity_engine():
    # HeadingSimilarity uses embeddings when configured and lexical fallback otherwise.
    return HeadingSimilarity()


def _best_match(source_text: str, candidates: list[dict], similarity: HeadingSimilarity, key: str = "heading"):
    best = None
    score = 0.0
    for candidate in candidates:
        value = candidate.get(key) or ""
        candidate_score = similarity.similarity(source_text, value)
        if candidate_score > score:
            score = candidate_score
            best = candidate
    return best, score


def _cluster_benchmark_sections(p0_sections, p1_sections, similarity, progress=None):
    clusters = []
    used_p1 = set()
    total = max(1, len(p0_sections))
    for pos, p0 in enumerate(p0_sections, start=1):
        match, score = _best_match(p0.get("heading", ""), p1_sections, similarity)
        idx = p1_sections.index(match) if match in p1_sections else None
        if match is not None and score >= SECTION_MATCH_STRONG and idx not in used_p1:
            used_p1.add(idx)
            clusters.append({"p0": p0, "p1": match, "p0_p1_similarity": score})
        else:
            clusters.append({"p0": p0, "p1": None, "p0_p1_similarity": score})
        _notify_progress(
            progress,
            pos / total,
            f"Matching P0/P1 section topics ({pos}/{len(p0_sections)})",
            pos,
            len(p0_sections),
        )
    for idx, p1 in enumerate(p1_sections):
        if idx not in used_p1:
            clusters.append({"p0": None, "p1": p1, "p0_p1_similarity": None})
    return clusters


def _matched_keywords(cluster, keywords):
    pieces = []
    for key in ("p0", "p1"):
        section = cluster.get(key)
        if section:
            pieces.extend([section.get("heading", ""), section.get("text", "")])
    haystack = " ".join(pieces)
    out = []
    for kw in keywords:
        if exact_phrase_present(haystack, kw.keyword):
            out.append({
                "keyword": kw.keyword,
                "keyword_type": (kw.keyword_type or "OTHER").upper(),
                "search_volume": kw.search_volume,
            })
    return out


def _keyword_priority(matches) -> str:
    if any(x["keyword_type"] == "PRIMARY" for x in matches):
        return "High"
    volumes = [int(x["search_volume"] or 0) for x in matches]
    if volumes and max(volumes) >= 100_000:
        return "High"
    return "Medium"


def build_content_structure_rows(pages, targets_by_id) -> list[dict]:
    rows = []
    for page in pages:
        target = targets_by_id.get(page.target_id)
        if not target:
            continue
        structure = _structure(page)
        sections = structure.get("sections") or []
        if not sections:
            sections = [{
                "order": 0,
                "heading_level": 0,
                "heading": "Page introduction",
                "text": structure.get("main_text", ""),
                "word_count": page.word_count,
            }]
        for section in sections:
            order = int(section.get("order") or 0)
            level = int(section.get("heading_level") or section.get("headingLevel") or 0)
            rows.append({
                "Page Label": target.label or target.role,
                "Priority": target.role,
                "Role": "Client" if target.role.upper() == "CLIENT" else "Competitor",
                "Section Order": order,
                "Heading Level": f"H{level}" if level else "Intro",
                "Heading": section.get("heading") or "Page introduction",
                "Section Word Count": int(section.get("word_count") or len(_words(section.get("text", "")))),
                "Section Text Preview": (section.get("text") or "")[:1200],
                "Page Title": structure.get("title") or page.title or "",
                "Meta Description": structure.get("meta_description") or page.meta_description or "",
                "Evidence ID": f"section:{page.id}:{order}",
            })
    return rows


def build_section_comparison_rows(pages, targets_by_id, keywords, progress=None) -> list[dict]:
    by_role = _priority_pages(pages, targets_by_id)
    client = by_role.get("CLIENT")
    p0 = by_role.get("P0")
    p1 = by_role.get("P1")
    if not client or not (p0 or p1):
        return []

    cstruct = _structure(client)
    client_sections = [x for x in (cstruct.get("sections") or []) if x.get("heading")]
    p0_sections = [x for x in (_structure(p0).get("sections") or []) if x.get("heading")] if p0 else []
    p1_sections = [x for x in (_structure(p1).get("sections") or []) if x.get("heading")] if p1 else []
    similarity = _similarity_engine()

    def cluster_progress(fraction, message, current, total):
        _notify_progress(progress, 0.35 * fraction, message, current, total)

    clusters = _cluster_benchmark_sections(p0_sections, p1_sections, similarity, progress=cluster_progress)
    rows = []
    cluster_total = max(1, len(clusters))
    client_brand_text = " ".join([cstruct.get("title") or "", cstruct.get("canonical_url") or "", _target_label(client, targets_by_id)])
    brand_tokens = {x for x in _token_set(client_brand_text) if len(x) >= 4}

    for idx, cluster in enumerate(clusters, start=1):
        p0s, p1s = cluster.get("p0"), cluster.get("p1")
        topic = (p0s or p1s or {}).get("heading", "")
        client_match, semantic = _best_match(topic, client_sections, similarity)
        client_heading = (client_match or {}).get("heading", "")
        lexical = _lexical_similarity(topic, client_heading) if client_match else 0.0
        composite = _composite_match(semantic, lexical, 0.75)
        benchmark_words = [int(s.get("word_count") or len(_words(s.get("text", "")))) for s in (p0s,p1s) if s]
        benchmark_words = [x for x in benchmark_words if x > 0]
        benchmark_median = float(median(benchmark_words)) if benchmark_words else 0.0
        client_words = int((client_match or {}).get("word_count") or len(_words((client_match or {}).get("text", ""))))
        depth_ratio = (client_words / benchmark_median) if benchmark_median else 1.0

        if client_match and composite >= SECTION_MATCH_STRONG:
            coverage = "Weak Coverage" if benchmark_median and depth_ratio < 0.60 else "Covered"
        elif client_match and composite >= SECTION_MATCH_WEAK:
            coverage = "Weak Coverage"
        else:
            coverage = "Missing"
            client_match = None
            client_words = 0
            depth_ratio = 0.0

        evidence = "P0 + P1" if p0s and p1s else "P0 only" if p0s else "P1 only"
        matched = _matched_keywords(cluster, keywords)
        priority_score = _section_priority_score(coverage, evidence, matched, composite, depth_ratio)
        opportunity = _priority_from_score(priority_score, coverage)
        topic_tokens = _token_set(topic)
        evidence_strength = 0.45 + (0.20 if evidence == "P0 + P1" else 0.08) + 0.20 * composite + 0.15 * min(depth_ratio, 1.0)
        evidence_strength = max(0.0, min(0.98, evidence_strength))
        reason_bits = [f"coverage={coverage}", f"benchmark={evidence}", f"composite={composite:.2f}", f"depth={depth_ratio:.2f}x"]
        if matched:
            reason_bits.append("matched keywords=" + ", ".join(x["keyword"] for x in matched[:4]))

        client_order = int((client_match or {}).get("order") or 0)
        p0_order = int((p0s or {}).get("order") or 0)
        p1_order = int((p1s or {}).get("order") or 0)
        rows.append({
            "Topic ID": idx, "Benchmark Topic": topic, "Benchmark Evidence": evidence,
            "P0 Heading": (p0s or {}).get("heading", ""), "P0 Heading Level": f"H{int((p0s or {}).get('heading_level') or 0)}" if p0s else "", "P0 Section Words": (p0s or {}).get("word_count", ""),
            "P1 Heading": (p1s or {}).get("heading", ""), "P1 Heading Level": f"H{int((p1s or {}).get('heading_level') or 0)}" if p1s else "", "P1 Section Words": (p1s or {}).get("word_count", ""),
            "P0-P1 Topic Similarity": round(float(cluster.get("p0_p1_similarity") or 0), 3) if cluster.get("p0_p1_similarity") is not None else "",
            "Client Heading": (client_match or {}).get("heading", ""), "Client Heading Level": f"H{int((client_match or {}).get('heading_level') or 0)}" if client_match else "",
            "Client Section Words": client_words, "Semantic Similarity": round(float(semantic),3), "Lexical Similarity": round(float(lexical),3),
            "Composite Topic Match": round(float(composite),3), "Depth Ratio vs Benchmark": round(depth_ratio,3),
            "Coverage Status": coverage, "Priority Score": priority_score, "Opportunity Priority": opportunity,
            "Evidence Confidence": round(evidence_strength,3), "Priority Reason": "; ".join(reason_bits),
            "Benchmark Word Range": f"{min(benchmark_words)}-{max(benchmark_words)}" if benchmark_words else "",
            "Benchmark Median Words": round(benchmark_median,1) if benchmark_words else "",
            "Matched Input Keywords": ", ".join(x["keyword"] for x in matched[:15]),
            "Primary Keyword Involved": "Yes" if any(x["keyword_type"] == "PRIMARY" for x in matched) else "No",
            "Highest Matched Search Volume": max([int(x["search_volume"] or 0) for x in matched], default=0) or "",
            "Client Evidence ID": f"section:{client.id}:{client_order}" if client_match else "",
            "P0 Evidence ID": f"section:{p0.id}:{p0_order}" if (p0 and p0s) else "",
            "P1 Evidence ID": f"section:{p1.id}:{p1_order}" if (p1 and p1s) else "",
        })
        _notify_progress(progress, 0.35 + 0.65 * (idx / cluster_total), f"Comparing benchmark topics with Client ({idx}/{len(clusters)})", idx, len(clusters))
    return rows


def _placement(structure: dict, keyword: str) -> dict[str, Any]:
    headings = structure.get("headings") or []
    faqs = structure.get("faqs") or []
    levels = {i: [] for i in range(1, 7)}
    for h in headings:
        if exact_phrase_present(h.get("text", ""), keyword):
            level = int(h.get("level") or 0)
            if level in levels:
                levels[level].append(h.get("text", ""))
    return {
        "title": exact_phrase_present(structure.get("title", ""), keyword),
        "meta": exact_phrase_present(structure.get("meta_description", ""), keyword),
        "body": exact_phrase_present(structure.get("main_text", ""), keyword),
        "levels": levels,
        "faq_q": [x for x in faqs if exact_phrase_present(x.get("question", ""), keyword)],
        "faq_a": [x for x in faqs if exact_phrase_present(x.get("answer", ""), keyword)],
    }


def build_keyword_placement_rows(pages, targets_by_id, keywords, progress=None) -> list[dict]:
    by_role = _priority_pages(pages, targets_by_id)
    client = by_role.get("CLIENT")
    p0 = by_role.get("P0")
    p1 = by_role.get("P1")
    if not client or not (p0 or p1):
        return []

    empty_structure = {
        "title": "", "meta_description": "", "main_text": "",
        "headings": [], "sections": [], "faqs": [],
    }
    structures = {
        "CLIENT": _structure(client),
        "P0": _structure(p0) if p0 else empty_structure,
        "P1": _structure(p1) if p1 else empty_structure,
    }
    rows = []

    def hx_summary(info):
        return ", ".join(f"H{level}" for level in range(1, 7) if info["levels"][level])

    keyword_total = max(1, len(keywords))
    for kw_index, kw in enumerate(keywords, start=1):
        info = {r: _placement(structures[r], kw.keyword) for r in structures}
        client_hx = hx_summary(info["CLIENT"])
        p0_hx = hx_summary(info["P0"])
        p1_hx = hx_summary(info["P1"])
        benchmark_heading_count = sum(bool(x) for x in (p0_hx, p1_hx))
        benchmark_faq_count = sum(bool(info[r]["faq_q"] or info[r]["faq_a"]) for r in ("P0", "P1"))

        if not info["CLIENT"]["body"] and (info["P0"]["body"] or info["P1"]["body"]):
            recommendation = "Missing on client; review relevance and add naturally if it supports search intent."
            opportunity = "High" if benchmark_heading_count == 2 else "Medium"
        elif info["CLIENT"]["body"] and not client_hx and benchmark_heading_count >= 1:
            recommendation = "Present in body but benchmarks use it in headings; consider a contextual H2/H3 if structurally appropriate."
            opportunity = "Medium"
        elif not (info["CLIENT"]["faq_q"] or info["CLIENT"]["faq_a"]) and benchmark_faq_count == 2:
            recommendation = "Both benchmarks cover this in FAQ content; consider an original FAQ if the user need is relevant."
            opportunity = "Medium"
        else:
            recommendation = "No structural keyword-placement change required from this comparison."
            opportunity = "No Placement Change"

        rows.append({
            "Keyword ID": kw.id,
            "Keyword": kw.keyword,
            "Keyword Type": (kw.keyword_type or "OTHER").upper(),
            "Search Volume": kw.search_volume,
            "Client Title": "Yes" if info["CLIENT"]["title"] else "No",
            "Client Meta": "Yes" if info["CLIENT"]["meta"] else "No",
            "Client Hx": client_hx,
            "Client Body": "Yes" if info["CLIENT"]["body"] else "No",
            "Client FAQ": "Yes" if (info["CLIENT"]["faq_q"] or info["CLIENT"]["faq_a"]) else "No",
            "P0 Hx": p0_hx,
            "P0 Body": "Yes" if info["P0"]["body"] else "No",
            "P0 FAQ": "Yes" if (info["P0"]["faq_q"] or info["P0"]["faq_a"]) else "No",
            "P1 Hx": p1_hx,
            "P1 Body": "Yes" if info["P1"]["body"] else "No",
            "P1 FAQ": "Yes" if (info["P1"]["faq_q"] or info["P1"]["faq_a"]) else "No",
            "Opportunity Priority": opportunity,
            "Recommendation": recommendation,
        })
        _notify_progress(
            progress,
            kw_index / keyword_total,
            f"Checking keyword placement ({kw_index}/{len(keywords)})",
            kw_index,
            len(keywords),
        )
    return rows


def _cluster_faqs(p0_faqs, p1_faqs, similarity, progress=None):
    clusters, used = [], set()
    total = max(1, len(p0_faqs))
    for pos, p0 in enumerate(p0_faqs, start=1):
        match, score = _best_match(p0.get("question", ""), p1_faqs, similarity, key="question")
        idx = p1_faqs.index(match) if match in p1_faqs else None
        if match and score >= FAQ_MATCH_STRONG and idx not in used:
            used.add(idx)
            clusters.append({"p0": p0, "p1": match, "similarity": score})
        else:
            clusters.append({"p0": p0, "p1": None, "similarity": score})
        _notify_progress(
            progress,
            pos / total,
            f"Matching P0/P1 FAQ topics ({pos}/{len(p0_faqs)})",
            pos,
            len(p0_faqs),
        )
    for idx, p1 in enumerate(p1_faqs):
        if idx not in used:
            clusters.append({"p0": None, "p1": p1, "similarity": None})
    return clusters


def build_faq_comparison_rows(pages, targets_by_id, progress=None) -> list[dict]:
    by_role = _priority_pages(pages, targets_by_id)
    client, p0, p1 = by_role.get("CLIENT"), by_role.get("P0"), by_role.get("P1")
    if not client or not (p0 or p1):
        return []
    cfaqs = _structure(client).get("faqs") or []
    p0faqs = (_structure(p0).get("faqs") or []) if p0 else []
    p1faqs = (_structure(p1).get("faqs") or []) if p1 else []
    similarity = _similarity_engine()
    rows = []
    def faq_cluster_progress(fraction, message, current, total): _notify_progress(progress, 0.40*fraction, message, current, total)
    faq_clusters = _cluster_faqs(p0faqs, p1faqs, similarity, progress=faq_cluster_progress)
    for idx, cluster in enumerate(faq_clusters, start=1):
        rep = (cluster.get("p0") or cluster.get("p1") or {}).get("question", "")
        match, semantic = _best_match(rep, cfaqs, similarity, key="question")
        cq = (match or {}).get("question", "")
        lexical = _lexical_similarity(rep, cq) if match else 0.0
        intent_same = 1.0 if _question_intent(rep) == _question_intent(cq) and _question_intent(rep) != "other" else 0.0
        composite = 0.65*float(semantic or 0) + 0.25*lexical + 0.10*intent_same
        covered = bool(match and composite >= FAQ_MATCH_STRONG and (lexical >= 0.20 or intent_same > 0))
        weak = bool(match and not covered and composite >= FAQ_MATCH_WEAK)
        evidence = "P0 + P1" if cluster.get("p0") and cluster.get("p1") else "P0 only" if cluster.get("p0") else "P1 only"
        status = "Covered" if covered else "Partial / Review" if weak else "Missing FAQ"
        priority = "No Change" if covered else "Medium" if weak else "High" if evidence == "P0 + P1" else "Medium"
        evidence_strength = min(0.98, 0.45 + (0.20 if evidence == "P0 + P1" else 0.08) + 0.27*composite)
        rows.append({
            "FAQ Topic ID": idx, "Benchmark FAQ Topic": rep, "Benchmark Evidence": evidence,
            "P0 Question": (cluster.get("p0") or {}).get("question", ""), "P1 Question": (cluster.get("p1") or {}).get("question", ""),
            "Client Matching Question": cq if match else "", "Semantic Similarity": round(float(semantic),3),
            "Lexical Similarity": round(lexical,3), "Question Intent Match": "Yes" if intent_same else "No",
            "Composite FAQ Match": round(composite,3), "Coverage Status": status, "Opportunity Priority": priority,
            "Evidence Confidence": round(evidence_strength,3),
            "Decision Logic": f"65% semantic + 25% lexical + 10% question-intent; strong threshold={FAQ_MATCH_STRONG:.2f}",
            "Recommendation": "No change required." if covered else "Review the matched FAQ and add an original FAQ only when this benchmark question represents a distinct user need not already answered by the client.",
        })
        _notify_progress(progress, 0.40 + 0.60*(idx/max(1,len(faq_clusters))), f"Comparing benchmark FAQs with Client ({idx}/{len(faq_clusters)})", idx, len(faq_clusters))
    return rows


def build_content_opportunity_rows(section_rows, keyword_rows, faq_rows, db_opportunities=None) -> list[dict]:
    rows = []
    for row in section_rows:
        priority = row.get("Opportunity Priority", "No Change")
        if priority == "No Change":
            continue
        missing = row.get("Coverage Status") == "Missing"
        action = "Add a new section only if it supports client search intent and user usefulness." if missing else "Expand or improve the existing client section rather than creating duplicate content."
        if row.get("Benchmark Word Range"):
            action += f" Benchmark coverage observed: {row['Benchmark Word Range']} words; use as evidence, not a mandatory word-count target."
        rows.append({
            "Opportunity Priority": priority,
            "Recommendation Confidence": f"{round(float(row.get("Evidence Confidence") or 0) * 100)}% evidence strength",
            "Priority Score": row.get("Priority Score"),
            "Priority Reason": row.get("Priority Reason", ""),
            
            "Opportunity Type": "Missing Section" if missing else "Weak Section",
            "Topic / Keyword": row.get("Benchmark Topic", ""),
            "Current Client State": row.get("Client Heading") or row.get("Coverage Status", ""),
            "Benchmark Evidence": row.get("Benchmark Evidence", ""),
            "Matched Keywords": row.get("Matched Input Keywords", ""),
            "Recommendation": action,
            "Source": "deterministic+embedding",
            "Evidence IDs": ", ".join(x for x in [row.get("Client Evidence ID"), row.get("P0 Evidence ID"), row.get("P1 Evidence ID")] if x),
        })
    for row in keyword_rows:
        if row.get("Opportunity Priority") in {"No Change", "No Placement Change"}:
            continue
        rows.append({
            "Opportunity Priority": row.get("Opportunity Priority"),
            "Recommendation Confidence": "High" if row.get("P0 Hx") and row.get("P1 Hx") else "Medium",
            "Opportunity Type": "Keyword Placement",
            "Topic / Keyword": row.get("Keyword", ""),
            "Current Client State": f"Body={row.get('Client Body')}; Hx={row.get('Client Hx') or 'None'}; FAQ={row.get('Client FAQ')}",
            "Benchmark Evidence": f"P0 Hx={row.get('P0 Hx') or 'None'}; P1 Hx={row.get('P1 Hx') or 'None'}",
            "Matched Keywords": row.get("Keyword", ""),
            "Recommendation": row.get("Recommendation", ""),
            "Source": "deterministic",
            "Evidence IDs": "",
        })
    for row in faq_rows:
        if row.get("Opportunity Priority") == "No Change":
            continue
        rows.append({
            "Opportunity Priority": row.get("Opportunity Priority"),
            "Recommendation Confidence": f"{round(float(row.get("Evidence Confidence") or 0) * 100)}% evidence strength",
            "Priority Score": 70 if row.get("Opportunity Priority") == "High" else 50,
            "Priority Reason": row.get("Decision Logic", ""),
            
            "Opportunity Type": "FAQ Opportunity",
            "Topic / Keyword": row.get("Benchmark FAQ Topic", ""),
            "Current Client State": row.get("Coverage Status", ""),
            "Benchmark Evidence": row.get("Benchmark Evidence", ""),
            "Matched Keywords": "",
            "Recommendation": row.get("Recommendation", ""),
            "Source": "deterministic+embedding",
            "Evidence IDs": "",
        })
    for item in db_opportunities or []:
        rows.append({
            "Opportunity Priority": {"P0":"HIGH","P1":"MEDIUM","P2":"LOW"}.get(str(item.priority), item.priority),
            "Recommendation Confidence": f"{round(float(item.confidence or 0) * 100)}% evidence strength",
            "Priority Score": round(float(item.confidence or 0) * 100),
            "Priority Reason": "AI-reviewed evidence; deterministic facts remain authoritative",
            
            "Opportunity Type": item.category,
            "Topic / Keyword": item.title,
            "Current Client State": "",
            "Benchmark Evidence": "See evidence IDs",
            "Matched Keywords": "",
            "Recommendation": item.recommendation,
            "Source": _public_ai_source(item.source),
            "Evidence IDs": ", ".join(item.evidence_json or []),
        })

    rank = {
        "HIGH": 0, "High": 0,
        "MEDIUM": 1, "Medium": 1,
        "LOW": 2, "Low": 2,
        "No Change": 9, "No Placement Change": 9,
    }
    rows.sort(key=lambda x: (rank.get(str(x.get("Opportunity Priority")), 5), str(x.get("Opportunity Type", "")), str(x.get("Topic / Keyword", ""))))
    return rows


def build_detailed_analysis(
    pages,
    targets_by_id,
    keywords,
    db_opportunities=None,
    progress=None,
) -> dict[str, list[dict]]:
    # Weighted sub-steps reflect where semantic matching usually spends time.
    _notify_progress(progress, 0.02, "Extracting structured section rows")
    content_structure = build_content_structure_rows(pages, targets_by_id)
    _notify_progress(progress, 0.06, "Structured section rows ready")

    def section_progress(fraction, message, current, total):
        _notify_progress(progress, 0.06 + 0.64 * fraction, message, current, total)

    section_comparison = build_section_comparison_rows(
        pages, targets_by_id, keywords, progress=section_progress
    )

    def keyword_progress(fraction, message, current, total):
        _notify_progress(progress, 0.70 + 0.12 * fraction, message, current, total)

    keyword_placement = build_keyword_placement_rows(
        pages, targets_by_id, keywords, progress=keyword_progress
    )

    def faq_progress(fraction, message, current, total):
        _notify_progress(progress, 0.82 + 0.15 * fraction, message, current, total)

    faq_comparison = build_faq_comparison_rows(
        pages, targets_by_id, progress=faq_progress
    )
    _notify_progress(progress, 0.98, "Consolidating content opportunities")
    content_opportunities = build_content_opportunity_rows(
        section_comparison, keyword_placement, faq_comparison, db_opportunities
    )
    _notify_progress(progress, 1.0, "Detailed analysis complete")
    return {
        "content_structure": content_structure,
        "section_comparison": section_comparison,
        "keyword_placement": keyword_placement,
        "faq_comparison": faq_comparison,
        "content_opportunities": content_opportunities,
    }


def build_ai_granular_context(
    pages,
    targets_by_id,
    keywords,
    detailed_analysis: dict,
    evidence_index: dict,
) -> tuple[dict, dict]:
    """Build the complete, auditable context passed to AI-model deep analysis.

    This deliberately includes the granular analysis tables PLUS section/FAQ text.
    Deterministic facts remain authoritative; The AI model receives them as evidence.

    To keep token use reasonable for large keyword sets, Keyword Placement is kept
    complete but represented in a compact field subset. Section text is preserved
    per CLIENT/P0/P1 section because depth/aspect reasoning needs the actual copy.
    """
    merged_evidence = dict(evidence_index or {})
    by_role = _priority_pages(pages, targets_by_id)

    page_payload = {}
    for role in ("CLIENT", "P0", "P1"):
        page = by_role.get(role)
        if not page:
            continue
        target = targets_by_id.get(page.target_id)
        structure = _structure(page)

        page_eid = f"page:{page.id}"
        merged_evidence.setdefault(
            page_eid,
            {
                "type": "page",
                "role": role,
                "label": (target.label or target.role) if target else role,
                "url": page.url,
                "final_url": page.final_url,
                "title": page.title,
                "meta_description": page.meta_description,
                "word_count": page.word_count,
            },
        )

        sections = []
        for section in structure.get("sections") or []:
            order = int(section.get("order") or 0)
            eid = f"section:{page.id}:{order}"
            record = {
                "type": "section",
                "role": role,
                "label": (target.label or target.role) if target else role,
                "page_url": page.url,
                "section_order": order,
                "heading_level": int(
                    section.get("heading_level") or section.get("headingLevel") or 0
                ),
                "heading": section.get("heading") or "Page introduction",
                "word_count": int(
                    section.get("word_count") or len(_words(section.get("text", "")))
                ),
                # Full extracted section copy is intentionally supplied for depth/aspect analysis.
                "text": section.get("text") or "",
            }
            merged_evidence[eid] = record
            sections.append({"evidence_id": eid, **record})

        faqs = []
        for faq in structure.get("faqs") or []:
            order = int(faq.get("order") or len(faqs) + 1)
            eid = f"faq:{page.id}:{order}"
            record = {
                "type": "faq",
                "role": role,
                "label": (target.label or target.role) if target else role,
                "page_url": page.url,
                "faq_order": order,
                "question": faq.get("question") or "",
                "answer": faq.get("answer") or "",
                "source": faq.get("source") or "",
            }
            merged_evidence[eid] = record
            faqs.append({"evidence_id": eid, **record})

        page_payload[role] = {
            "page_evidence_id": page_eid,
            "label": (target.label or target.role) if target else role,
            "url": page.url,
            "title": structure.get("title") or page.title or "",
            "meta_description": structure.get("meta_description") or page.meta_description or "",
            "word_count": page.word_count,
            "sections": sections,
            "faqs": faqs,
        }

    compact_keyword_placement = []
    for row in detailed_analysis.get("keyword_placement", []) or []:
        compact_keyword_placement.append(
            {
                "keyword_id": row.get("Keyword ID"),
                "keyword": row.get("Keyword"),
                "keyword_type": row.get("Keyword Type"),
                "search_volume": row.get("Search Volume"),
                "client_title": row.get("Client Title"),
                "client_meta": row.get("Client Meta"),
                "client_hx": row.get("Client Hx"),
                "client_body": row.get("Client Body"),
                "client_faq": row.get("Client FAQ"),
                "p0_hx": row.get("P0 Hx"),
                "p0_body": row.get("P0 Body"),
                "p0_faq": row.get("P0 FAQ"),
                "p1_hx": row.get("P1 Hx"),
                "p1_body": row.get("P1 Body"),
                "p1_faq": row.get("P1 FAQ"),
                "opportunity_priority": row.get("Opportunity Priority"),
                "deterministic_recommendation": row.get("Recommendation"),
            }
        )

    granular = {
        "pages": page_payload,
        "section_comparison": detailed_analysis.get("section_comparison", []) or [],
        "keyword_placement": compact_keyword_placement,
        "faq_comparison": detailed_analysis.get("faq_comparison", []) or [],
        "content_opportunities": detailed_analysis.get("content_opportunities", []) or [],
        "keyword_catalog": [
            {
                "id": kw.id,
                "keyword": kw.keyword,
                "keyword_type": (kw.keyword_type or "OTHER").upper(),
                "search_volume": kw.search_volume,
            }
            for kw in keywords
        ],
    }
    return granular, merged_evidence


def apply_section_ai_reviews(snapshot: dict, section_reviews: list[dict]) -> dict:
    """Annotate Section Comparison rows with AI-model depth/aspect reasoning."""
    out = dict(snapshot or {})
    review_by_topic = {
        int(item["topic_id"]): item
        for item in (section_reviews or [])
        if item.get("topic_id") is not None
    }
    rows = []
    for row in out.get("section_comparison", []) or []:
        updated = dict(row)
        topic_id = updated.get("Topic ID")
        review = review_by_topic.get(int(topic_id)) if topic_id is not None else None
        if review:
            updated.update(
                {
                    "AI Same Topic": "Yes" if review.get("same_topic") else "No",
                    "AI Coverage Assessment": str(review.get("coverage_assessment", "")).title(),
                    "AI Intent Relevance": str(review.get("intent_relevance", "")).title(),
                    "AI Missing Aspects": "; ".join(review.get("missing_aspects") or []),
                    "AI Recommended Action": review.get("recommended_action", ""),
                    "AI Recommendation": review.get("recommendation", ""),
                    "AI Confidence": round(float(review.get("confidence", 0)) * 100, 1),
                    "AI Evidence IDs": ", ".join(review.get("evidence_ids") or []),
                }
            )
        rows.append(updated)
    out["section_comparison"] = rows
    out["ai_deep_reviews"] = section_reviews or []
    return out
