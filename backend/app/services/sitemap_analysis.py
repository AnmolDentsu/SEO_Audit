from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse
from collections import Counter, defaultdict
from statistics import median
from typing import Callable

from app import models


def _source_maps(sources):
    by_id = {x.id: x for x in sources}
    client = next((x for x in sources if x.role == "CLIENT"), None)
    competitors = [x for x in sources if x.role == "COMPETITOR"]
    return by_id, client, competitors


def _safe_label(source) -> str:
    return source.label or f"Source {source.id}"


def _norm_type(value: str | None) -> str:
    # Backward-compatible aggregation for historical cached rows.
    value = (value or "OTHER").upper()
    return "CONTENT" if value == "BLOG" else value


_CONTENT_TAXONOMY_TYPES = {
    "PRODUCT", "CONTENT", "CATEGORY", "TOOL_CALCULATOR", "FAQ_HELP", "LANDING_PAGE"
}


_STOP = {"www","com","in","co","the","and","of","for","to","a","an","ltd","limited","india","online"}

def _tokens(value: str | None) -> set[str]:
    return {x for x in re.findall(r"[a-z0-9]+", str(value or "").casefold()) if len(x) > 1 and x not in _STOP}


def _text_similarity(a: str | None, b: str | None) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / max(len(ta | tb), 1)
    seq = SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(jaccard, seq * 0.85)


def _client_relevance(label: str, client_categories: set[str], client_topics: set[str]) -> tuple[float, str, str]:
    candidates = [(x, "category") for x in client_categories] + [(x, "topic") for x in client_topics]
    if not candidates:
        return 0.0, "", "Client taxonomy unavailable"
    best_label, best_kind, best = "", "", 0.0
    for candidate, kind in candidates:
        score = _text_similarity(label, candidate)
        if score > best:
            best_label, best_kind, best = candidate, kind, score
    if label in client_categories or label in client_topics:
        best = 1.0
    reason = f"Closest client {best_kind}: {best_label or 'none'}; lexical taxonomy similarity={best:.2f}"
    return round(best, 3), best_label, reason


def _priority_score(competitor_sites: int, total_competitors: int, competitor_urls: int, client_count: int, relevance_score: float, gap_type: str) -> tuple[str, float, int, str]:
    support = competitor_sites / max(total_competitors, 1)
    volume = min(1.0, math.log1p(max(competitor_urls, 0)) / math.log(51))
    gap = 1.0 if client_count <= 0 else min(1.0, max(competitor_urls - client_count, 0) / max(client_count * 3, 1))
    weights = {"TOPIC_GAP": 1.0, "CATEGORY_HUB_GAP": 0.90, "PAGE_TYPE_GAP": 0.82, "DEPTH_GAP": 0.75}
    base = 34*support + 20*volume + 24*gap + 22*max(0.0, min(1.0, relevance_score))
    score = int(round(min(100, base * weights.get(gap_type, 0.8))))
    priority = "HIGH" if score >= 70 else "MEDIUM" if score >= 45 else "LOW"
    confidence = round(min(0.97, 0.45 + 0.0038*score + 0.12*support), 3)
    reason = f"support={support:.0%}; evidence-volume={volume:.2f}; gap={gap:.2f}; client-relevance={relevance_score:.2f}; gap-type={gap_type}"
    return priority, confidence, score, reason


def _eligible_content_row(row) -> bool:
    """Rows allowed to influence taxonomy/gap recommendations.

    Usable inventory and page-type reporting still includes homepage/utility pages,
    but those pages cannot manufacture content-gap opportunities.
    """
    if getattr(row, "error", None):
        return False
    evidence = getattr(row, "classification_evidence_json", None) or {}
    if isinstance(evidence, dict):
        terminal = str(evidence.get("terminal_state") or "usable").lower()
        if terminal != "usable":
            return False
        explicit = evidence.get("taxonomy_eligible")
        if explicit is False:
            return False
    return _norm_type(getattr(row, "detailed_page_type", None)) in _CONTENT_TAXONOMY_TYPES


def _priority(competitor_sites: int, total_competitors: int, competitor_urls: int, client_count: int, business_relevance: str) -> tuple[str, float]:
    relevance = {"high": 1.0, "medium": 0.65, "uncertain": 0.35, "low": 0.1}.get(str(business_relevance).lower(), 0.35)
    priority, confidence, _, _ = _priority_score(competitor_sites, total_competitors, competitor_urls, client_count, relevance, "TOPIC_GAP")
    return priority, confidence


def _dominant_competitor_page_type(topic: str, active, by_source, successful_source_ids: set[int]) -> str:
    counts = Counter()
    for row in active:
        if row.topic != topic or row.source_id not in successful_source_ids:
            continue
        source = by_source.get(row.source_id)
        if not source or source.role != "COMPETITOR":
            continue
        ptype = _norm_type(row.detailed_page_type)
        if ptype in {"HOMEPAGE", "OTHER", "UTILITY", "LANDING_PAGE"}:
            continue
        counts[ptype] += 1
    return counts.most_common(1)[0][0] if counts else "OTHER"


def _top_items(counter: Counter, limit: int = 8) -> str:
    return " | ".join(f"{k} ({v})" for k, v in counter.most_common(limit) if k and k != "Other / Unclassified")



def _char_ngrams(value: str | None, n: int = 3) -> set[str]:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    if not text:
        return set()
    compact = f"  {text}  "
    if len(compact) <= n:
        return {compact}
    return {compact[i:i+n] for i in range(len(compact) - n + 1)}


def _retrieval_tokens(value: str | None) -> set[str]:
    """Cheap retrieval-only token variants; scoring still uses _text_similarity."""
    output: set[str] = set()
    for token in _tokens(value):
        output.add(token)
        if len(token) > 4 and token.endswith("ies"):
            output.add(token[:-3] + "y")
        elif len(token) > 4 and token.endswith("es"):
            output.add(token[:-2])
        elif len(token) > 3 and token.endswith("s"):
            output.add(token[:-1])
    return output


def _build_relevance_index(client_categories: set[str], client_topics: set[str]) -> dict:
    """Build a bounded candidate index for client-taxonomy relevance checks.

    The old implementation compared every discovered topic against every client
    category/topic with SequenceMatcher. On large sitemaps that becomes
    O(discovered_topics * client_taxonomy) and can stall coverage for hours.

    This index retrieves a small candidate pool by shared tokens / character
    trigrams, then applies the *same* _text_similarity() scoring to that pool.
    """
    records: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for label in sorted(client_categories):
        key = (label, "category")
        if key not in seen:
            seen.add(key)
            records.append(key)

    for label in sorted(client_topics):
        key = (label, "topic")
        if key not in seen:
            seen.add(key)
            records.append(key)

    token_index: dict[str, list[int]] = defaultdict(list)
    gram_index: dict[str, list[int]] = defaultdict(list)
    exact: dict[str, tuple[str, str]] = {}

    for idx, (label, kind) in enumerate(records):
        exact.setdefault(label, (label, kind))
        exact.setdefault(label.casefold(), (label, kind))
        for token in sorted(_retrieval_tokens(label)):
            token_index[token].append(idx)
        for gram in sorted(_char_ngrams(label)):
            gram_index[gram].append(idx)

    return {
        "records": records,
        "token_index": token_index,
        "gram_index": gram_index,
        "exact": exact,
    }


def _client_relevance_indexed(
    label: str,
    index: dict,
    cache: dict[str, tuple[float, str, str]],
    *,
    max_candidates: int = 48,
) -> tuple[float, str, str]:
    cached = cache.get(label)
    if cached is not None:
        return cached

    records: list[tuple[str, str]] = index.get("records") or []
    if not records:
        result = (0.0, "", "Client taxonomy unavailable")
        cache[label] = result
        return result

    exact = index.get("exact") or {}
    exact_hit = exact.get(label) or exact.get(str(label).casefold())
    if exact_hit:
        best_label, best_kind = exact_hit
        result = (
            1.0,
            best_label,
            f"Closest client {best_kind}: {best_label}; lexical taxonomy similarity=1.00",
        )
        cache[label] = result
        return result

    candidate_hits: Counter[int] = Counter()
    token_index = index.get("token_index") or {}
    gram_index = index.get("gram_index") or {}
    record_count = max(len(records), 1)

    # Only use the rarest useful token postings. Very common taxonomy words
    # ("loan", "insurance", "plan", "topic") are poor candidate selectors and
    # were the main source of quadratic behaviour in large inventories.
    token_postings = []
    token_cap = max(120, int(record_count * 0.35))
    for token in sorted(_retrieval_tokens(label)):
        posting = token_index.get(token) or ()
        if posting and len(posting) <= token_cap:
            token_postings.append((len(posting), posting))
    token_postings.sort(key=lambda x: (x[0], tuple(x[1])))

    for _size, posting in token_postings[:3]:
        for idx in posting:
            candidate_hits[idx] += 8

    # Character trigrams recover near-spelling / inflection matches, but only
    # from low-frequency grams and only a small number of postings.
    gram_postings = []
    gram_cap = max(80, int(record_count * 0.12))
    for gram in sorted(_char_ngrams(label)):
        posting = gram_index.get(gram) or ()
        if posting and len(posting) <= gram_cap:
            gram_postings.append((len(posting), posting))
    gram_postings.sort(key=lambda x: (x[0], tuple(x[1])))

    for _size, posting in gram_postings[:8]:
        for idx in posting:
            candidate_hits[idx] += 1

    if not candidate_hits:
        result = (
            0.0,
            "",
            "Closest client taxonomy: none; lexical taxonomy similarity=0.00",
        )
        cache[label] = result
        return result

    # Counter.most_common() preserves first-seen order for ties. First-seen
    # order can vary across Python processes when inputs originate from sets,
    # which made the supposedly deterministic opportunity count drift between
    # identical recovery runs. Sort ties by stable record id instead.
    candidate_ids = [
        idx
        for idx, _score in sorted(
            candidate_hits.items(),
            key=lambda item: (-item[1], item[0]),
        )[: max(8, int(max_candidates))]
    ]

    best_label, best_kind, best = "", "", 0.0
    for idx in candidate_ids:
        candidate, kind = records[idx]
        score = _text_similarity(label, candidate)
        if score > best:
            best_label, best_kind, best = candidate, kind, score

    result = (
        round(best, 3),
        best_label,
        f"Closest client {best_kind or 'taxonomy'}: {best_label or 'none'}; "
        f"lexical taxonomy similarity={best:.2f}",
    )
    cache[label] = result
    return result

def _emit_analysis_progress(
    callback: Callable[[float, str, int | None, int | None], None] | None,
    fraction: float,
    message: str,
    current: int | None = None,
    total: int | None = None,
) -> None:
    if callback is None:
        return
    callback(
        max(0.0, min(1.0, float(fraction))),
        message,
        current,
        total,
    )


def build_sitemap_analysis(
    url_rows,
    sources,
    source_discovery: list[dict] | None = None,
    progress_callback: Callable[[float, str, int | None, int | None], None] | None = None,
) -> tuple[dict, list[dict], dict[str, dict]]:
    """Build site profiles and client-vs-competitor coverage from persisted rows.

    Performance properties:
    - one primary pass over URL rows;
    - one primary pass over eligible content rows;
    - O(1) indexed lookups for topic/category/page-type evidence;
    - bounded client-taxonomy relevance candidate retrieval;
    - no topic/category loop rescans the full URL inventory.

    The optional progress callback keeps long coverage jobs DB-visible and allows
    watchdogs to distinguish active CPU work from a genuinely frozen process.
    """
    by_source, client_source, competitors = _source_maps(sources)
    if not client_source:
        raise ValueError("Sitemap analysis requires one CLIENT source")

    _emit_analysis_progress(progress_callback, 0.01, "Indexing persisted sitemap rows")

    discovery_by_id = {
        int(x.get("source_id")): x
        for x in (source_discovery or [])
        if x.get("source_id") is not None
    }

    rows_by_source: dict[int, list] = defaultdict(list)
    active_by_source: dict[int, list] = defaultdict(list)
    content_by_source: dict[int, list] = defaultdict(list)
    active: list = []
    content_active: list = []
    failures: list = []

    for row in url_rows:
        source_id = getattr(row, "source_id", None)
        if source_id is not None:
            rows_by_source[source_id].append(row)

        if getattr(row, "error", None):
            failures.append(row)
            continue

        active.append(row)
        if source_id is not None:
            active_by_source[source_id].append(row)

        if _eligible_content_row(row):
            content_active.append(row)
            if source_id is not None:
                content_by_source[source_id].append(row)

    active_counts = Counter({sid: len(rows) for sid, rows in active_by_source.items()})
    content_counts = Counter({sid: len(rows) for sid, rows in content_by_source.items()})

    successful_source_ids: set[int] = set()
    for source in sources:
        d = discovery_by_id.get(source.id, {})
        if d:
            if (
                d.get("status") in {"success", "warning", "partial"}
                and int(d.get("discovered_urls") or 0) > 0
            ):
                successful_source_ids.add(source.id)
        elif active_counts.get(source.id, 0) > 0:
            successful_source_ids.add(source.id)

    client_ready = (
        client_source.id in successful_source_ids
        and content_counts.get(client_source.id, 0) > 0
    )
    comparison_competitors = [
        source
        for source in competitors
        if (
            source.id in successful_source_ids
            and content_counts.get(source.id, 0) > 0
        )
    ]
    comparison_ready = bool(client_ready and comparison_competitors)

    _emit_analysis_progress(
        progress_callback,
        0.06,
        "Building per-site coverage profiles",
        0,
        len(sources),
    )

    overview: list[dict] = []
    url_type_summary: list[dict] = []
    site_profiles: list[dict] = []

    ordered_sources = [client_source] + competitors
    for source_index, source in enumerate(ordered_sources, start=1):
        rows = rows_by_source.get(source.id, [])
        good = active_by_source.get(source.id, [])
        eligible = content_by_source.get(source.id, [])
        d = discovery_by_id.get(source.id, {})

        broad = Counter(
            _norm_type(getattr(x, "broad_page_type", None))
            for x in good
        )
        detailed = Counter(
            _norm_type(getattr(x, "detailed_page_type", None))
            for x in good
        )
        cat_counter = Counter(
            getattr(x, "category", None) or "Other / Unclassified"
            for x in eligible
        )
        topic_counter = Counter(
            getattr(x, "topic", None) or "Other / Unclassified"
            for x in eligible
        )

        resolution_candidates = int(
            d.get("resolution_candidates")
            if d.get("resolution_candidates") is not None
            else (d.get("discovered_urls") if d else len(rows))
        )
        unique_sitemap_urls = int(
            d.get("unique_sitemap_urls")
            if d.get("unique_sitemap_urls") is not None
            else resolution_candidates
        )
        raw_url_records = int(d.get("raw_url_records") or unique_sitemap_urls)
        usable_count = int(
            d.get("usable_urls")
            if d.get("usable_urls") is not None
            else len(good)
        )
        stale_count = int(d.get("stale_urls") or 0)
        non_html_count = int(d.get("non_html_urls") or 0)
        challenge_count = int(d.get("security_challenge_urls") or 0)
        canonical_dupes = int(d.get("canonical_duplicates") or 0)
        other_failed = int(
            d.get("failed_urls")
            or max(
                0,
                len(rows)
                - len(good)
                - stale_count
                - non_html_count
                - challenge_count,
            )
        )

        overview.append({
            "Site": _safe_label(source),
            "Role": source.role,
            "Root Sitemap": source.root_url,
            "Discovery Status": d.get("status") or ("success" if rows else "unknown"),
            "Scope Mode": d.get("scope_mode") or "legacy",
            "Raw Sitemap URL Records": raw_url_records,
            "Unique Sitemap URLs": unique_sitemap_urls,
            "XMLs Traversed": int(d.get("xmls_traversed") or 0),
            "Max XML Depth": int(d.get("max_depth") or 0),
            "Root Host URLs": int(d.get("root_host_urls") or 0),
            "Related Subdomain URLs": int(d.get("related_subdomain_urls") or 0),
            "External Domain URLs": int(d.get("external_domain_urls") or 0),
            "In-Scope URLs": int(d.get("in_scope_urls") or resolution_candidates),
            "Excluded by Scope": int(d.get("excluded_by_scope_urls") or 0),
            "Excluded External-Domain URLs": int(
                d.get("excluded_external_domain_urls") or 0
            ),
            "Excluded Non-Page URLs": int(d.get("excluded_non_page_urls") or 0),
            "Resolution Candidates": resolution_candidates,
            "Discovered URLs": resolution_candidates,
            "Resolved / Usable URLs": usable_count,
            "Stale 404/410": stale_count,
            "Non-HTML After Fetch": non_html_count,
            "Security / WAF Challenges": challenge_count,
            "Canonical Duplicates": canonical_dupes,
            "Other Failed": other_failed,
            "Homepage": broad.get("HOMEPAGE", 0),
            "Product": broad.get("PRODUCT", 0),
            "Content / Blog": broad.get("CONTENT", 0),
            "Other": broad.get("OTHER", 0),
            "Discovery Note": d.get("message") or "",
        })

        total = max(len(good), 1)
        all_types = set(detailed) | {
            "HOMEPAGE",
            "PRODUCT",
            "CONTENT",
            "CATEGORY",
            "TOOL_CALCULATOR",
            "FAQ_HELP",
            "LANDING_PAGE",
            "UTILITY",
            "OTHER",
        }
        for page_type in sorted(all_types):
            count = detailed.get(page_type, 0)
            url_type_summary.append({
                "Site": _safe_label(source),
                "Role": source.role,
                "Page Type": page_type,
                "Count": count,
                "Percent of Usable URLs": round(count / total * 100, 2),
            })

        site_profiles.append({
            "Site": _safe_label(source),
            "Role": source.role,
            "Usable URLs": len(good),
            "Eligible Content URLs": len(eligible),
            "Homepage": broad.get("HOMEPAGE", 0),
            "Product": broad.get("PRODUCT", 0),
            "Content / Blog": broad.get("CONTENT", 0),
            "Other": broad.get("OTHER", 0),
            "Distinct Categories": len(
                [k for k in cat_counter if k != "Other / Unclassified"]
            ),
            "Distinct Topics": len(
                [k for k in topic_counter if k != "Other / Unclassified"]
            ),
            "Top Categories": _top_items(cat_counter),
            "Top Topics": _top_items(topic_counter),
        })

        _emit_analysis_progress(
            progress_callback,
            0.06 + 0.09 * source_index / max(len(ordered_sources), 1),
            f"Built site profile {source_index}/{len(ordered_sources)}",
            source_index,
            len(ordered_sources),
        )

    # ------------------------------------------------------------------
    # ONE aggregation pass. Every later phase consumes these indexes.
    # ------------------------------------------------------------------
    topic_counts = defaultdict(Counter)
    topic_type_counts = defaultdict(Counter)
    category_counts = defaultdict(Counter)
    category_type_counts = defaultdict(Counter)

    evidence_by_topic = defaultdict(list)
    evidence_index: dict[str, dict] = {}

    topic_category: dict[str, str] = {}
    category_topic_pairs: set[tuple[str, str]] = set()
    subtopics_by_pair: dict[tuple[str, str], set[str]] = defaultdict(set)

    dominant_competitor_types: dict[str, Counter] = defaultdict(Counter)
    topic_type_competitor_evidence: dict[tuple[str, str], list[str]] = defaultdict(list)
    category_hub_evidence: dict[str, list[str]] = defaultdict(list)

    client_categories: set[str] = set()
    client_topics: set[str] = set()

    eligible_total = len(content_active)
    emit_every = max(100, min(1000, eligible_total // 50 if eligible_total else 100))

    for index, row in enumerate(content_active, start=1):
        source = by_source.get(getattr(row, "source_id", None))
        if not source:
            continue

        label = _safe_label(source)
        category = getattr(row, "category", None) or "Other / Unclassified"
        topic = getattr(row, "topic", None) or "Other / Unclassified"
        ptype = _norm_type(getattr(row, "detailed_page_type", None))

        topic_counts[topic][label] += 1
        topic_type_counts[(topic, ptype)][label] += 1
        category_counts[category][label] += 1
        category_type_counts[(category, ptype)][label] += 1

        topic_category.setdefault(topic, category)

        if (
            category != "Other / Unclassified"
            and topic != "Other / Unclassified"
        ):
            category_topic_pairs.add((category, topic))
            subtopic = str(getattr(row, "subtopic", "") or "").strip()
            if subtopic:
                subtopics_by_pair[(category, topic)].add(subtopic)

        eid = f"sitemap-url:{getattr(row, 'id', '')}"
        if len(evidence_by_topic[(topic, label)]) < 5:
            evidence_by_topic[(topic, label)].append(eid)

        evidence_index[eid] = {
            "site": label,
            "role": source.role,
            "url": getattr(row, "final_url", None)
            or getattr(row, "original_url", None),
            "page_type": ptype,
            "category": category,
            "topic": topic,
            "title": getattr(row, "title", None),
            "h1": getattr(row, "h1_json", None),
            "word_count": getattr(row, "word_count", None),
        }

        if source.id == client_source.id:
            if category != "Other / Unclassified":
                client_categories.add(str(category))
            if topic != "Other / Unclassified":
                client_topics.add(str(topic))

        if (
            source.role == "COMPETITOR"
            and source.id in successful_source_ids
        ):
            if ptype not in {"HOMEPAGE", "OTHER", "UTILITY", "LANDING_PAGE"}:
                dominant_competitor_types[topic][ptype] += 1

            evidence_bucket = topic_type_competitor_evidence[(topic, ptype)]
            if len(evidence_bucket) < 8:
                evidence_bucket.append(eid)

            if ptype == "CATEGORY":
                hub_bucket = category_hub_evidence[category]
                if len(hub_bucket) < 8:
                    hub_bucket.append(eid)

        if (
            progress_callback is not None
            and (index == eligible_total or index % emit_every == 0)
        ):
            _emit_analysis_progress(
                progress_callback,
                0.15 + 0.20 * index / max(eligible_total, 1),
                f"Indexed eligible content rows ({index:,}/{eligible_total:,})",
                index,
                eligible_total,
            )

    client_label = _safe_label(client_source)
    competitor_labels = [_safe_label(x) for x in comparison_competitors]
    total_competitors = len(comparison_competitors)

    relevance_index = _build_relevance_index(client_categories, client_topics)
    relevance_cache: dict[str, tuple[float, str, str]] = {}

    # ------------------------------------------------------------------
    # Topic coverage matrix.
    # ------------------------------------------------------------------
    topic_coverage = []
    topic_keys = [
        topic
        for topic in sorted(topic_counts)
        if topic != "Other / Unclassified"
    ]
    topic_total = len(topic_keys)
    for index, topic in enumerate(topic_keys, start=1):
        counts = topic_counts[topic]
        comp_values = [counts.get(label, 0) for label in competitor_labels]
        category = topic_category.get(topic, "Other / Unclassified")
        rel_score, closest_client, rel_reason = _client_relevance_indexed(
            topic,
            relevance_index,
            relevance_cache,
        )

        row = {
            "Category": category,
            "Topic": topic,
            "Client Relevance Score": rel_score,
            "Closest Client Taxonomy": closest_client,
            "Relevance Evidence": rel_reason,
            "Client Count": counts.get(client_label, 0),
        }
        for label in competitor_labels:
            row[label] = counts.get(label, 0)

        sites_covering = sum(1 for value in comp_values if value > 0)
        comp_median = float(median(comp_values)) if comp_values else 0
        row.update({
            "Competitor Total": sum(comp_values),
            "Competitor Average": round(
                sum(comp_values) / max(len(comp_values), 1), 2
            ),
            "Competitor Median": comp_median,
            "Competitor Max": max(comp_values) if comp_values else 0,
            "Competitor Sites Covering": sites_covering,
            "Competitor Support %": round(
                sites_covering / max(len(comp_values), 1) * 100, 1
            ) if comp_values else 0,
            "Client Gap vs Competitor Median": round(
                counts.get(client_label, 0) - comp_median, 2
            ),
        })
        topic_coverage.append(row)

        if progress_callback is not None and (
            index == topic_total or index % 250 == 0
        ):
            _emit_analysis_progress(
                progress_callback,
                0.35 + 0.13 * index / max(topic_total, 1),
                f"Built topic coverage ({index:,}/{topic_total:,})",
                index,
                topic_total,
            )

    # ------------------------------------------------------------------
    # Topic x page-type head-to-head matrix.
    # ------------------------------------------------------------------
    head_to_head = []
    topic_type_items = [
        item
        for item in sorted(topic_type_counts.items())
        if item[0][0] != "Other / Unclassified"
    ]
    topic_type_total = len(topic_type_items)

    for index, ((topic, ptype), counts) in enumerate(topic_type_items, start=1):
        comp_values = [counts.get(label, 0) for label in competitor_labels]
        category = topic_category.get(topic, "Other / Unclassified")
        rel_score, closest_client, rel_reason = _client_relevance_indexed(
            topic,
            relevance_index,
            relevance_cache,
        )

        row = {
            "Category": category,
            "Topic": topic,
            "Page Type": ptype,
            "Client Relevance Score": rel_score,
            "Closest Client Taxonomy": closest_client,
            "Relevance Evidence": rel_reason,
            "Client Count": counts.get(client_label, 0),
        }
        for label in competitor_labels:
            row[label] = counts.get(label, 0)

        sites_covering = sum(1 for value in comp_values if value > 0)
        row.update({
            "Competitor Total": sum(comp_values),
            "Competitor Median": float(median(comp_values)) if comp_values else 0,
            "Competitor Sites Covering": sites_covering,
            "Competitor Support %": round(
                sites_covering / max(len(comp_values), 1) * 100, 1
            ) if comp_values else 0,
        })
        head_to_head.append(row)

        if progress_callback is not None and (
            index == topic_type_total or index % 250 == 0
        ):
            _emit_analysis_progress(
                progress_callback,
                0.48 + 0.10 * index / max(topic_type_total, 1),
                f"Built head-to-head coverage ({index:,}/{topic_type_total:,})",
                index,
                topic_type_total,
            )

    # ------------------------------------------------------------------
    # Category -> Topic -> Subtopic taxonomy. No full-row rescans.
    # ------------------------------------------------------------------
    topic_taxonomy = []
    pair_list = sorted(category_topic_pairs)
    pair_total = len(pair_list)

    for index, (category, topic) in enumerate(pair_list, start=1):
        counts = topic_counts[topic]
        subtopics = sorted(subtopics_by_pair.get((category, topic), set()))
        topic_taxonomy.append({
            "Category": category,
            "Topic": topic,
            "Subtopics": " | ".join(subtopics[:25]),
            "Client URLs": counts.get(client_label, 0),
            "Competitor URLs": sum(
                counts.get(label, 0)
                for label in competitor_labels
            ),
        })

        if progress_callback is not None and (
            index == pair_total or index % 500 == 0
        ):
            _emit_analysis_progress(
                progress_callback,
                0.58 + 0.08 * index / max(pair_total, 1),
                f"Built taxonomy coverage ({index:,}/{pair_total:,})",
                index,
                pair_total,
            )

    missing_categories: list[dict] = []
    opportunities: list[dict] = []

    if comparison_ready:
        # --------------------------------------------------------------
        # Missing category/hub structure.
        # --------------------------------------------------------------
        category_items = [
            item
            for item in sorted(category_counts.items())
            if item[0] != "Other / Unclassified"
        ]
        category_total = len(category_items)

        for index, (category, counts) in enumerate(category_items, start=1):
            client_total = counts.get(client_label, 0)
            client_hubs = category_type_counts[(category, "CATEGORY")].get(
                client_label, 0
            )
            comp_hub_sites = sum(
                1
                for label in competitor_labels
                if category_type_counts[(category, "CATEGORY")].get(label, 0) > 0
            )
            comp_sites = sum(
                1
                for label in competitor_labels
                if counts.get(label, 0) > 0
            )
            comp_hub_urls = sum(
                category_type_counts[(category, "CATEGORY")].get(label, 0)
                for label in competitor_labels
            )

            if client_hubs == 0 and comp_hub_sites > 0:
                rel_score, closest_client, rel_reason = _client_relevance_indexed(
                    category,
                    relevance_index,
                    relevance_cache,
                )
                support_pct = round(
                    comp_hub_sites / max(total_competitors, 1) * 100,
                    1,
                )
                decision = (
                    "RELEVANT"
                    if client_total > 0 or rel_score >= 0.58
                    else "REVIEW"
                    if rel_score >= 0.38 and support_pct >= 40
                    else "EXCLUDE"
                )

                evidence_ids = list(
                    category_hub_evidence.get(category, [])
                )[:8]

                missing_categories.append({
                    "Category": category,
                    "Client Related URLs": client_total,
                    "Client Hub Pages": client_hubs,
                    "Competitor Sites Covering": comp_sites,
                    "Competitor Sites With Hub": comp_hub_sites,
                    "Competitor Hub URLs": comp_hub_urls,
                    "Competitor Support %": support_pct,
                    "Client Similarity": rel_score,
                    "Closest Client Taxonomy": closest_client,
                    "Relevance Decision": decision,
                    "Relevance Reason": rel_reason,
                    "Observed Competitor Page Type": "CATEGORY",
                    "Suggested Action": (
                        "Evaluate a category/hub"
                        if decision == "RELEVANT"
                        else "Human review before any SEO action"
                        if decision == "REVIEW"
                        else "Do not create an SEO opportunity"
                    ),
                    "Status": (
                        "Missing category/hub structure"
                        if decision != "EXCLUDE"
                        else "Excluded: low client relevance"
                    ),
                    "Evidence IDs": evidence_ids,
                })

            if progress_callback is not None and (
                index == category_total or index % 250 == 0
            ):
                _emit_analysis_progress(
                    progress_callback,
                    0.66 + 0.07 * index / max(category_total, 1),
                    f"Evaluated category gaps ({index:,}/{category_total:,})",
                    index,
                    category_total,
                )

        # --------------------------------------------------------------
        # Opportunities from already-built indexes.
        # --------------------------------------------------------------
        seq = 1

        for gap in missing_categories:
            if gap.get("Relevance Decision") == "EXCLUDE":
                continue

            category = gap["Category"]
            comp_sites = int(gap["Competitor Sites With Hub"])
            comp_urls = int(gap["Competitor Hub URLs"])
            rel_score = float(gap.get("Client Similarity") or 0)
            priority, confidence, priority_score, priority_reason = _priority_score(
                comp_sites,
                total_competitors,
                comp_urls,
                int(gap["Client Hub Pages"]),
                rel_score,
                "CATEGORY_HUB_GAP",
            )

            opportunities.append({
                "opportunity_key": f"SOPP-{seq:04d}",
                "gap_type": "CATEGORY_HUB_GAP",
                "priority": priority,
                "confidence": confidence,
                "category": category,
                "topic": None,
                "recommended_page_type": "CATEGORY",
                "client_count": int(gap["Client Hub Pages"]),
                "competitor_count": comp_urls,
                "competitor_sites": comp_sites,
                "business_relevance": gap.get(
                    "Relevance Decision", "REVIEW"
                ).lower(),
                "priority_score": priority_score,
                "priority_reason": priority_reason,
                "title": f"Evaluate a dedicated {category} category/hub",
                "recommendation": (
                    "The client has no classified category/hub for this area "
                    "while at least one successfully analysed competitor does. "
                    "Review whether a central hub serves a distinct navigation "
                    "or user-intent need; competitor structure is evidence, not "
                    "a mandatory template."
                ),
                "evidence_ids": list(gap.get("Evidence IDs") or [])[:8],
                "source": "deterministic",
            })
            seq += 1

        topic_coverage_total = len(topic_coverage)
        for index, row in enumerate(topic_coverage, start=1):
            topic = row["Topic"]
            client_count = int(row["Client Count"])
            comp_sites = int(row["Competitor Sites Covering"])
            comp_total = int(row["Competitor Total"])
            comp_median = float(row["Competitor Median"])
            category = topic_category.get(topic, "Other / Unclassified")
            category_client_total = category_counts[category].get(
                client_label, 0
            )

            rel_score, closest_client, _rel_reason = _client_relevance_indexed(
                topic,
                relevance_index,
                relevance_cache,
            )
            relevance = (
                "high"
                if category_client_total > 0 or rel_score >= 0.65
                else "medium"
                if rel_score >= 0.45
                else "low"
            )

            evidence_ids: list[str] = []
            for label in competitor_labels:
                evidence_ids.extend(
                    evidence_by_topic.get((topic, label), [])[:2]
                )
            evidence_ids.extend(
                evidence_by_topic.get((topic, client_label), [])[:2]
            )
            evidence_ids = list(dict.fromkeys(evidence_ids))[:10]

            type_counter = dominant_competitor_types.get(topic)
            dominant_page_type = (
                type_counter.most_common(1)[0][0]
                if type_counter
                else "OTHER"
            )

            if client_count == 0 and comp_sites > 0:
                if relevance != "low":
                    (
                        priority,
                        confidence,
                        priority_score,
                        priority_reason,
                    ) = _priority_score(
                        comp_sites,
                        total_competitors,
                        comp_total,
                        0,
                        rel_score,
                        "TOPIC_GAP",
                    )
                    opportunities.append({
                        "opportunity_key": f"SOPP-{seq:04d}",
                        "gap_type": "TOPIC_GAP",
                        "priority": priority,
                        "confidence": confidence,
                        "category": category,
                        "topic": topic,
                        "recommended_page_type": dominant_page_type,
                        "client_count": 0,
                        "competitor_count": comp_total,
                        "competitor_sites": comp_sites,
                        "business_relevance": relevance,
                        "priority_score": priority_score,
                        "priority_reason": priority_reason,
                        "client_relevance_score": rel_score,
                        "closest_client_taxonomy": closest_client,
                        "title": f"Review missing topic coverage: {topic}",
                        "recommendation": (
                            "The topic is absent from the client's usable "
                            "classified sitemap inventory and appears in "
                            f"competitor output ({comp_total} URLs across "
                            f"{comp_sites}/{total_competitors} successfully "
                            "analysed competitors). Validate business relevance "
                            "and user intent before creating content."
                        ),
                        "evidence_ids": evidence_ids,
                        "source": "deterministic",
                    })
                    seq += 1

            elif client_count > 0 and comp_sites > 0:
                depth_ratio = comp_median / max(client_count, 1)
                if (
                    comp_median > client_count
                    and (
                        depth_ratio >= 1.5
                        or comp_median - client_count >= 3
                    )
                ):
                    (
                        priority,
                        confidence,
                        priority_score,
                        priority_reason,
                    ) = _priority_score(
                        comp_sites,
                        total_competitors,
                        comp_total,
                        client_count,
                        rel_score,
                        "DEPTH_GAP",
                    )
                    opportunities.append({
                        "opportunity_key": f"SOPP-{seq:04d}",
                        "gap_type": "DEPTH_GAP",
                        "priority": priority,
                        "confidence": min(confidence, 0.92),
                        "category": category,
                        "topic": topic,
                        "recommended_page_type": dominant_page_type,
                        "client_count": client_count,
                        "competitor_count": comp_total,
                        "competitor_sites": comp_sites,
                        "business_relevance": relevance,
                        "priority_score": priority_score,
                        "priority_reason": priority_reason,
                        "client_relevance_score": rel_score,
                        "closest_client_taxonomy": closest_client,
                        "title": f"Review content depth for {topic}",
                        "recommendation": (
                            f"The client covers this topic with {client_count} "
                            "usable classified URLs, while the competitor median "
                            f"is {comp_median:g}. Review uncovered subtopics and "
                            "user needs; competitor URL counts are a benchmark, "
                            "not a page-count quota."
                        ),
                        "evidence_ids": evidence_ids,
                        "source": "deterministic",
                    })
                    seq += 1

            if progress_callback is not None and (
                index == topic_coverage_total or index % 250 == 0
            ):
                _emit_analysis_progress(
                    progress_callback,
                    0.73 + 0.16 * index / max(topic_coverage_total, 1),
                    f"Built topic opportunities ({index:,}/{topic_coverage_total:,})",
                    index,
                    topic_coverage_total,
                )

        head_total = len(head_to_head)
        for index, row in enumerate(head_to_head, start=1):
            topic, ptype = row["Topic"], row["Page Type"]
            if ptype in {"OTHER", "HOMEPAGE", "UTILITY", "LANDING_PAGE"}:
                continue

            client_type_count = int(row["Client Count"])
            client_topic_count = topic_counts[topic].get(client_label, 0)
            comp_sites = int(row["Competitor Sites Covering"])
            comp_total = int(row["Competitor Total"])

            if (
                client_topic_count > 0
                and client_type_count == 0
                and comp_sites > 0
            ):
                category = topic_category.get(
                    topic, "Other / Unclassified"
                )
                evidence_ids = list(
                    topic_type_competitor_evidence.get((topic, ptype), [])
                )[:8]

                (
                    rel_score,
                    closest_client,
                    _rel_reason,
                ) = _client_relevance_indexed(
                    topic,
                    relevance_index,
                    relevance_cache,
                )
                (
                    priority,
                    confidence,
                    priority_score,
                    priority_reason,
                ) = _priority_score(
                    comp_sites,
                    total_competitors,
                    comp_total,
                    0,
                    max(rel_score, 0.7),
                    "PAGE_TYPE_GAP",
                )

                opportunities.append({
                    "opportunity_key": f"SOPP-{seq:04d}",
                    "gap_type": "PAGE_TYPE_GAP",
                    "priority": priority,
                    "confidence": confidence,
                    "category": category,
                    "topic": topic,
                    "recommended_page_type": ptype,
                    "client_count": 0,
                    "competitor_count": comp_total,
                    "competitor_sites": comp_sites,
                    "business_relevance": "high",
                    "priority_score": priority_score,
                    "priority_reason": priority_reason,
                    "client_relevance_score": rel_score,
                    "closest_client_taxonomy": closest_client,
                    "title": (
                        f"Evaluate {ptype.lower().replace('_', ' ')} "
                        f"coverage for {topic}"
                    ),
                    "recommendation": (
                        f"The client covers {topic}, but not with a classified "
                        f"{ptype} page while competitor output does. Review "
                        "whether this page type serves a distinct user intent "
                        "before adding it."
                    ),
                    "evidence_ids": evidence_ids,
                    "source": "deterministic",
                })
                seq += 1

            if progress_callback is not None and (
                index == head_total or index % 500 == 0
            ):
                _emit_analysis_progress(
                    progress_callback,
                    0.89 + 0.06 * index / max(head_total, 1),
                    f"Evaluated page-type gaps ({index:,}/{head_total:,})",
                    index,
                    head_total,
                )

        priority_rank = {"HIGH": 1, "MEDIUM": 2, "LOW": 3}
        opportunities.sort(
            key=lambda x: (
                priority_rank.get(x["priority"], 9),
                -x["confidence"],
                x["title"],
            )
        )

    _emit_analysis_progress(
        progress_callback,
        0.96,
        "Building comparison and failure summaries",
    )

    failed_sources = [
        x for x in (source_discovery or [])
        if x.get("status") == "failed"
    ]
    comparison_status = {
        "ready": comparison_ready,
        "client_ready": client_ready,
        "configured_competitors": len(competitors),
        "successful_competitors": len(comparison_competitors),
        "client_eligible_content_urls": content_counts.get(client_source.id, 0),
        "competitor_eligible_content_urls": sum(
            content_counts.get(x.id, 0)
            for x in comparison_competitors
        ),
        "failed_sources": len(failed_sources),
        "message": (
            f"Client and {len(comparison_competitors)} competitor(s) were "
            "successfully analysed; opportunity logic supports any 1..N "
            "competitor count."
            if comparison_ready
            else "Head-to-head gaps/opportunities are unavailable because the "
            "client or every competitor lacks eligible content pages after "
            "failure/utility filtering."
        ),
    }

    failure_summary = []
    for x in failures:
        source_id = getattr(x, "source_id", None)
        if source_id not in by_source:
            continue

        http_status = getattr(x, "http_status", None)
        is_stale = http_status in {404, 410}
        evidence = getattr(x, "classification_evidence_json", None) or {}
        terminal = (
            str(evidence.get("terminal_state") or "")
            if isinstance(evidence, dict)
            else ""
        )
        error_text = str(getattr(x, "error", "") or "")
        is_challenge = (
            terminal == "security_challenge"
            or "challenge" in error_text.lower()
        )

        if is_stale:
            code, severity = "STALE_SITEMAP_URL", "warning"
        elif is_challenge:
            code, severity = "SECURITY_CHALLENGE", "error"
        else:
            code, severity = "PAGE_RESOLUTION_FAILED", "error"

        failure_summary.append({
            "Site": _safe_label(by_source[source_id]),
            "Stage": "page_resolution",
            "URL": getattr(x, "original_url", None),
            "Error": getattr(x, "error", None),
            "HTTP Status": http_status,
            "Severity": severity,
            "Code": code,
            "Retry Count": getattr(x, "retry_count", 0),
        })

    for x in active:
        evidence = getattr(x, "classification_evidence_json", None) or {}
        warning = (
            evidence.get("browser_fallback_warning")
            if isinstance(evidence, dict)
            else None
        )
        source_id = getattr(x, "source_id", None)

        if warning and source_id in by_source:
            failure_summary.append({
                "Site": _safe_label(by_source[source_id]),
                "Stage": "page_browser_enrichment",
                "URL": getattr(x, "original_url", None),
                "Error": warning,
                "HTTP Status": getattr(x, "http_status", None),
                "Severity": "warning",
                "Code": "BROWSER_ENRICHMENT_FAILED_HTTP_EVIDENCE_RETAINED",
                "Retry Count": getattr(x, "retry_count", 0),
            })

    analysis = {
        "overview": overview,
        "site_profiles": site_profiles,
        "url_type_summary": url_type_summary,
        "topic_taxonomy": topic_taxonomy,
        "topic_coverage_matrix": topic_coverage,
        "head_to_head": head_to_head,
        "missing_categories": missing_categories,
        "content_opportunities": opportunities,
        "comparison_status": comparison_status,
        "source_discovery": source_discovery or [],
        "discovery_scope": source_discovery or [],
        "failure_summary": failure_summary,
    }

    _emit_analysis_progress(
        progress_callback,
        1.0,
        (
            f"Coverage built: {len(topic_coverage):,} topics; "
            f"{len(opportunities):,} deterministic opportunities"
        ),
        len(topic_coverage),
        len(topic_coverage),
    )
    return analysis, opportunities, evidence_index
