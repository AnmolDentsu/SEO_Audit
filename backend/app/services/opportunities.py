from collections import Counter, defaultdict
from statistics import median

from app.services.benchmarks import build_density_benchmarks
from app.services.embeddings import HeadingSimilarity
from app.services.text import normalize


_PRIORITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


def _priority_for_keyword(keyword) -> str:
    """Editorial priority. This is intentionally NOT named P0/P1/P2.

    P0/P1 are reserved exclusively for benchmark URL roles.
    """
    keyword_type = (getattr(keyword, "keyword_type", None) or "OTHER").upper()
    search_volume = int(getattr(keyword, "search_volume", None) or 0)
    if keyword_type == "PRIMARY":
        return "HIGH"
    if keyword_type == "SECONDARY":
        return "MEDIUM"
    if search_volume >= 100_000:
        return "MEDIUM"
    return "LOW"


def _downshift(priority: str) -> str:
    return {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}.get(priority, "LOW")


def build_opportunities(
    client_page,
    competitor_pages,
    keywords,
    density_by_page_keyword,
    role_by_page_id: dict[int, str] | None = None,
):
    opportunities = []
    evidence_index = {}
    role_by_page_id = role_by_page_id or {}

    def evidence(kind, ref_id, payload):
        eid = f"{kind}:{ref_id}"
        evidence_index[eid] = payload
        return eid

    if not client_page:
        return [], evidence_index

    client_eid = evidence(
        "page",
        client_page.id,
        {
            "url": client_page.url,
            "title": client_page.title,
            "h1": client_page.h1_json,
            "h2": client_page.h2_json,
            "word_count": client_page.word_count,
        },
    )

    all_pages = [client_page, *competitor_pages]
    pseudo_targets = {}
    for p in all_pages:
        role = role_by_page_id.get(p.id, "OTHER")
        pseudo_targets[p.target_id] = type(
            "BenchmarkTarget",
            (),
            {"role": role, "label": role, "url": p.url},
        )()
    all_density_rows = [r for r in density_by_page_keyword.values() if r is not None]
    benchmarks = build_density_benchmarks(all_pages, all_density_rows, pseudo_targets)

    for kw in keywords:
        c = density_by_page_keyword.get((client_page.id, kw.id))
        if not c:
            continue

        benchmark = benchmarks.get(kw.id, {})
        target_min = benchmark.get("target_min")
        target_max = benchmark.get("target_max")
        ceid = evidence(
            "density",
            c.id,
            {
                "keyword": kw.keyword,
                "keyword_type": (kw.keyword_type or "OTHER").upper(),
                "search_volume": kw.search_volume,
                "url": client_page.url,
                "count": c.count,
                "density_pct": c.density_pct,
                "target_min": target_min,
                "target_max": target_max,
                "benchmark_state": benchmark.get("benchmark_state"),
                "other_benchmark_count": benchmark.get("other_count", 0),
                "other_benchmark_median": benchmark.get("other_median"),
                "competitor_pool_median": benchmark.get("competitor_median"),
                "competitor_pool_q1": benchmark.get("competitor_q1"),
                "competitor_pool_q3": benchmark.get("competitor_q3"),
            },
        )

        comp_pairs = [
            (p, density_by_page_keyword.get((p.id, kw.id)))
            for p in competitor_pages
        ]
        comp_pairs = [(p, r) for p, r in comp_pairs if r is not None]
        comp_rows = [r for _, r in comp_pairs]
        present_ratio = (
            sum(1 for r in comp_rows if r.present) / len(comp_rows)
            if comp_rows
            else 0.0
        )
        avg_density = (
            sum(r.density_pct for r in comp_rows) / len(comp_rows)
            if comp_rows
            else 0.0
        )
        median_density = float(median([r.density_pct for r in comp_rows])) if comp_rows else 0.0

        comp_evidence = []
        for page, row in comp_pairs:
            comp_evidence.append(
                evidence(
                    "density",
                    row.id,
                    {
                        "keyword": kw.keyword,
                        "url": page.url,
                        "role": role_by_page_id.get(page.id, "OTHER"),
                        "count": row.count,
                        "density_pct": row.density_pct,
                    },
                )
            )

        if c.count == 0 and present_ratio >= 0.5:
            opportunities.append(
                {
                    "category": "keyword_gap",
                    "priority": _priority_for_keyword(kw),
                    "confidence": round(min(0.98, 0.70 + 0.28 * present_ratio), 2),
                    "title": f"Review missing phrase: {kw.keyword}",
                    "recommendation": (
                        f"The client page does not contain the exact phrase '{kw.keyword}', while "
                        f"{present_ratio:.0%} of successfully crawled benchmark pages do. "
                        "Add it only where it fits naturally and matches the page intent."
                    ),
                    "evidence_ids": [ceid, *comp_evidence[:5]],
                }
            )

        if target_min is not None and target_max is not None:
            if c.density_pct < target_min:
                opportunities.append(
                    {
                        "category": "below_anchor_range",
                        "priority": _priority_for_keyword(kw),
                        "confidence": 0.92,
                        "title": f"Client density below benchmark range: {kw.keyword}",
                        "recommendation": (
                            f"Client density is {c.density_pct:.3f}% versus dynamically assigned Target Min "
                            f"{target_min:.3f}%. Treat this as benchmark evidence, not a search-engine rule."
                        ),
                        "evidence_ids": [ceid, *comp_evidence[:5]],
                    }
                )
            elif c.density_pct > target_max:
                opportunities.append(
                    {
                        "category": "above_anchor_range",
                        "priority": _downshift(_priority_for_keyword(kw)),
                        "confidence": 0.92,
                        "title": f"Client density above benchmark range: {kw.keyword}",
                        "recommendation": (
                            f"Client density is {c.density_pct:.3f}% versus dynamically assigned Target Max "
                            f"{target_max:.3f}%. Review repetition and readability rather than optimizing "
                            "to the number blindly."
                        ),
                        "evidence_ids": [ceid, *comp_evidence[:5]],
                    }
                )

        # The broader pool is deliberately separate from the P0/P1 anchor rule.
        # Use the median once at least two benchmark pages are available so one
        # aggressive competitor cannot manufacture a gap. With one competitor,
        # median == average, so the behavior remains well-defined.
        pool_baseline = median_density if comp_rows else 0.0
        if (
            comp_rows
            and c.density_pct + 0.15 < pool_baseline
            and (kw.keyword_type or "").upper() in {"PRIMARY", "SECONDARY"}
        ):
            opportunities.append(
                {
                    "category": "competitor_density_gap",
                    "priority": _priority_for_keyword(kw),
                    "confidence": 0.78,
                    "title": f"Competitor usage gap: {kw.keyword}",
                    "recommendation": (
                        f"Client density is {c.density_pct:.3f}% versus competitor average "
                        f"{avg_density:.3f}% and median {median_density:.3f}% across successful crawls. "
                        "Review topical coverage; do not optimize to a density number blindly."
                    ),
                    "evidence_ids": [ceid, *comp_evidence[:5]],
                }
            )

    heading_counter = Counter()
    heading_sources = defaultdict(list)
    for page in competitor_pages:
        for heading in page.h2_json or []:
            key = normalize(heading)
            if len(key) < 4:
                continue
            heading_counter[key] += 1
            heading_sources[key].append((page, heading))

    similarity = HeadingSimilarity()
    client_h2 = client_page.h2_json or []
    for key, freq in heading_counter.most_common(20):
        if not competitor_pages or freq / len(competitor_pages) < 0.5:
            continue
        source_page, source_heading = heading_sources[key][0]
        best = max((similarity.similarity(source_heading, h) for h in client_h2), default=0.0)
        if best >= 0.72:
            continue
        seid = evidence(
            "page",
            source_page.id,
            {
                "url": source_page.url,
                "competitor_heading": source_heading,
                "frequency": freq,
                "competitor_count": len(competitor_pages),
                "best_client_heading_similarity": round(best, 3),
            },
        )
        opportunities.append(
            {
                "category": "heading_theme_gap",
                "priority": "MEDIUM",
                "confidence": round(
                    min(0.95, 0.65 + 0.25 * (freq / len(competitor_pages))), 2
                ),
                "title": f"Review missing competitor heading theme: {source_heading[:120]}",
                "recommendation": (
                    "This H2 theme appears across a majority of successfully crawled competitor pages "
                    "and has no close client H2 match. Validate relevance before adding a new section."
                ),
                "evidence_ids": [client_eid, seid],
            }
        )

    if client_page.word_count and competitor_pages:
        comp_words = [p.word_count for p in competitor_pages if p.word_count]
        if comp_words:
            avg = sum(comp_words) / len(comp_words)
            med = float(median(comp_words))
            if client_page.word_count < avg * 0.6 and avg - client_page.word_count > 250:
                ids = [client_eid]
                for p in competitor_pages[:5]:
                    ids.append(evidence("page", p.id, {"url": p.url, "word_count": p.word_count}))
                opportunities.append(
                    {
                        "category": "content_depth_gap",
                        "priority": "MEDIUM",
                        "confidence": 0.72,
                        "title": "Review content-depth gap",
                        "recommendation": (
                            f"Client main-content word count is {client_page.word_count}; competitor "
                            f"average is {avg:.0f} and median is {med:.0f}. Use this only as a coverage "
                            "signal and expand only missing useful sections."
                        ),
                        "evidence_ids": ids,
                    }
                )

    dedup = {}
    for item in opportunities:
        dedup[(item["category"], item["title"])] = item
    ordered = sorted(
        dedup.values(),
        key=lambda x: (_PRIORITY_RANK.get(str(x["priority"]).upper(), 9), -x["confidence"]),
    )
    return ordered[:100], evidence_index
