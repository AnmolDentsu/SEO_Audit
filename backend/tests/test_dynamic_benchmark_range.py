from types import SimpleNamespace

from app.services.benchmarks import build_density_benchmarks, density_target_status


def _page(page_id, target_id, *, error=None):
    return SimpleNamespace(id=page_id, target_id=target_id, error=error)


def _density(page_result_id, keyword_id, value):
    return SimpleNamespace(
        page_result_id=page_result_id,
        keyword_id=keyword_id,
        density_pct=value,
    )


def _target(role, label):
    return SimpleNamespace(role=role, label=label, url=f"https://example.com/{label}")


def test_p0_lower_becomes_target_min():
    pages = [_page(1, 10), _page(2, 11), _page(3, 12)]
    targets = {
        10: _target("CLIENT", "client"),
        11: _target("P0", "comp-p0"),
        12: _target("P1", "comp-p1"),
    }
    densities = [
        _density(1, 99, 1.20),
        _density(2, 99, 0.80),
        _density(3, 99, 1.60),
    ]

    b = build_density_benchmarks(pages, densities, targets)[99]

    assert b["p0_density"] == 0.80
    assert b["p1_density"] == 1.60
    assert b["target_min"] == 0.80
    assert b["target_min_role"] == "P0"
    assert b["target_max"] == 1.60
    assert b["target_max_role"] == "P1"
    assert b["benchmark_state"] == "READY"
    assert b["benchmark_ready"] is True
    assert density_target_status("CLIENT", 1.20, b) == "Within Target"


def test_p1_lower_becomes_target_min_without_invalid_range():
    pages = [_page(1, 10), _page(2, 11), _page(3, 12)]
    targets = {
        10: _target("CLIENT", "client"),
        11: _target("P0", "comp-p0"),
        12: _target("P1", "comp-p1"),
    }
    densities = [
        _density(1, 99, 2.50),
        _density(2, 99, 2.10),
        _density(3, 99, 1.90),
    ]

    b = build_density_benchmarks(pages, densities, targets)[99]

    assert b["target_min"] == 1.90
    assert b["target_min_role"] == "P1"
    assert b["target_max"] == 2.10
    assert b["target_max_role"] == "P0"
    assert b["valid_range"] is True
    assert b["benchmark_state"] == "READY"
    assert density_target_status("CLIENT", 2.50, b) == "Above Target"


def test_single_anchor_does_not_fake_a_range():
    pages = [_page(1, 10), _page(2, 11)]
    targets = {
        10: _target("CLIENT", "client"),
        11: _target("P0", "comp-p0"),
    }
    densities = [_density(1, 99, 1.0), _density(2, 99, 1.5)]

    b = build_density_benchmarks(pages, densities, targets)[99]

    assert b["p0_density"] == 1.5
    assert b["p1_density"] is None
    assert b["target_min"] is None
    assert b["target_max"] is None
    assert b["benchmark_state"] == "PARTIAL_ANCHORS"
    assert b["benchmark_ready"] is False
    assert density_target_status("CLIENT", 1.0, b) == "Target Unavailable"
