"""Run-scoped keyword-density benchmark helpers.

Business semantics are deliberately kept separate from editorial priority:

* CLIENT = page being evaluated.
* P0/P1  = two benchmark anchors. Neither role is permanently "min" or "max".
* OTHER  = additional benchmark pool used only for broader distribution context.

For every keyword, when both P0 and P1 densities are available, the lower of
those two values becomes Target Min and the higher becomes Target Max. This
keeps the benchmark range mathematically valid without changing the configured
P0/P1 URL roles themselves.

Targets are run-scoped because page content and density can change between runs.
"""
from __future__ import annotations

from math import floor
from statistics import median


def _percentile(values: list[float], q: float) -> float | None:
    """Linear percentile that behaves sensibly for small lists and needs no NumPy."""
    if not values:
        return None
    ordered = sorted(float(x) for x in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lo = floor(pos)
    hi = min(lo + 1, len(ordered) - 1)
    weight = pos - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def _stats(values: list[float]) -> dict:
    values = [float(x) for x in values]
    if not values:
        return {
            "count": 0,
            "average": None,
            "median": None,
            "q1": None,
            "q3": None,
            "min": None,
            "max": None,
        }
    return {
        "count": len(values),
        "average": sum(values) / len(values),
        "median": float(median(values)),
        "q1": _percentile(values, 0.25),
        "q3": _percentile(values, 0.75),
        "min": min(values),
        "max": max(values),
    }


def benchmark_configuration(targets) -> dict:
    """Configuration-level readiness before crawl success is considered."""
    roles = [str(getattr(t, "role", "OTHER") or "OTHER").upper() for t in targets]
    client_count = roles.count("CLIENT")
    p0_count = roles.count("P0")
    p1_count = roles.count("P1")
    other_count = sum(1 for r in roles if r == "OTHER")

    if client_count != 1:
        state = "INVALID_CLIENT_CONFIGURATION"
    elif p0_count > 1 or p1_count > 1:
        state = "INVALID_ANCHOR_CONFIGURATION"
    elif p0_count == 1 and p1_count == 1:
        state = "ANCHORS_CONFIGURED"
    elif p0_count or p1_count:
        state = "PARTIAL_ANCHORS"
    else:
        state = "NO_ANCHORS"

    return {
        "state": state,
        "client_configured": client_count == 1,
        "p0_configured": p0_count == 1,
        "p1_configured": p1_count == 1,
        "other_count": other_count,
        "anchor_range_configured": p0_count == 1 and p1_count == 1,
        "range_assignment": "LOWER_OF_P0_P1_TO_MIN_HIGHER_TO_MAX",
    }


def build_density_benchmarks(pages, densities, target_by_id) -> dict[int, dict]:
    """Return per-keyword benchmark metadata keyed by keyword_id.

    P0 and P1 remain stable URL-role labels. Their measured density values are
    ordered per keyword:

        target_min = min(P0 density, P1 density)
        target_max = max(P0 density, P1 density)

    OTHER URLs are not allowed to redefine the P0/P1 target range. They are
    retained only as broader competitor-distribution context.
    """
    page_by_id = {p.id: p for p in pages}
    result: dict[int, dict] = {}

    for d in densities:
        page = page_by_id.get(d.page_result_id)
        if not page or page.error:
            continue
        target = target_by_id.get(page.target_id)
        if not target:
            continue

        role = str(target.role or "OTHER").upper()
        source = target.label or target.url
        density = float(d.density_pct)

        bucket = result.setdefault(
            d.keyword_id,
            {
                "p0_density": None,
                "p0_source": None,
                "p1_density": None,
                "p1_source": None,
                "target_min": None,
                "target_max": None,
                "target_min_source": None,
                "target_max_source": None,
                "target_min_role": None,
                "target_max_role": None,
                "anchor_order": None,
                "client_density": None,
                "valid_range": None,
                "p0_available": False,
                "p1_available": False,
                "client_available": False,
                "other_values": [],
                "other_sources": [],
                "competitor_values": [],
                "competitor_sources": [],
            },
        )

        if role == "P0":
            bucket["p0_density"] = density
            bucket["p0_source"] = source
            bucket["p0_available"] = True
            bucket["competitor_values"].append(density)
            bucket["competitor_sources"].append(source)
        elif role == "P1":
            bucket["p1_density"] = density
            bucket["p1_source"] = source
            bucket["p1_available"] = True
            bucket["competitor_values"].append(density)
            bucket["competitor_sources"].append(source)
        elif role == "CLIENT":
            bucket["client_density"] = density
            bucket["client_available"] = True
        else:
            bucket["other_values"].append(density)
            bucket["other_sources"].append(source)
            bucket["competitor_values"].append(density)
            bucket["competitor_sources"].append(source)

    for bucket in result.values():
        p0 = bucket.get("p0_density")
        p1 = bucket.get("p1_density")

        if p0 is not None and p1 is not None:
            if p0 <= p1:
                bucket["target_min"] = p0
                bucket["target_min_source"] = bucket.get("p0_source")
                bucket["target_min_role"] = "P0"
                bucket["target_max"] = p1
                bucket["target_max_source"] = bucket.get("p1_source")
                bucket["target_max_role"] = "P1"
                bucket["anchor_order"] = "P0<=P1"
            else:
                bucket["target_min"] = p1
                bucket["target_min_source"] = bucket.get("p1_source")
                bucket["target_min_role"] = "P1"
                bucket["target_max"] = p0
                bucket["target_max_source"] = bucket.get("p0_source")
                bucket["target_max_role"] = "P0"
                bucket["anchor_order"] = "P1<P0"

            bucket["valid_range"] = True
            bucket["benchmark_state"] = "READY"
            bucket["benchmark_ready"] = True
        elif p0 is not None or p1 is not None:
            # One anchor alone is not enough to define a lower/upper range.
            bucket["valid_range"] = None
            bucket["benchmark_state"] = "PARTIAL_ANCHORS"
            bucket["benchmark_ready"] = False
        else:
            bucket["valid_range"] = None
            bucket["benchmark_state"] = "NO_ANCHORS"
            bucket["benchmark_ready"] = False

        other = _stats(bucket.pop("other_values"))
        pool = _stats(bucket.pop("competitor_values"))

        for key, value in other.items():
            bucket[f"other_{key}"] = value
        for key, value in pool.items():
            bucket[f"competitor_{key}"] = value

        bucket["other_sources"] = bucket.get("other_sources", [])[:25]
        bucket["competitor_sources"] = bucket.get("competitor_sources", [])[:25]

    return result


def density_target_status(role: str, density: float | None, benchmark: dict | None) -> str:
    if (role or "").upper() != "CLIENT":
        return ""
    if density is None:
        return "Unavailable"

    benchmark = benchmark or {}
    lo = benchmark.get("target_min")
    hi = benchmark.get("target_max")

    if lo is None or hi is None:
        return "Target Unavailable"
    if density < lo:
        return "Below Target"
    if density > hi:
        return "Above Target"
    return "Within Target"
