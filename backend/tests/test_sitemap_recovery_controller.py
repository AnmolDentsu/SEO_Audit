from __future__ import annotations

import pytest

from app.services import sitemap_recovery as recovery


def _ok() -> dict:
    return {
        "status_code": 200,
        "error": None,
        "terminal_state": "usable",
    }


def _blocked() -> dict:
    return {
        "status_code": 403,
        "error": "HTTP 403",
        "terminal_state": "fetch_failed",
    }


def test_recovery_health_rejects_five_of_eight_probe():
    health = recovery._recovery_batch_health(
        [_ok() for _ in range(5)] + [_blocked() for _ in range(3)],
        min_success_ratio=0.80,
        block_ratio_limit=0.50,
    )

    assert health["processed"] == 8
    assert health["successes"] == 5
    assert health["success_ratio"] == pytest.approx(0.625)
    assert health["block_ratio"] == pytest.approx(0.375)
    assert health["healthy"] is False
    assert "below 80.0%" in health["reason"]


def test_recovery_health_accepts_clean_slow_probe():
    health = recovery._recovery_batch_health(
        [_ok() for _ in range(6)],
        min_success_ratio=0.80,
        block_ratio_limit=0.50,
    )

    assert health["processed"] == 6
    assert health["success_ratio"] == pytest.approx(1.0)
    assert health["block_ratio"] == pytest.approx(0.0)
    assert health["healthy"] is True
    assert health["reason"] is None


def test_deferred_rows_are_not_counted_as_http_attempts():
    deferred = {
        "status_code": 403,
        "error": "Deferred after host access circuit opened",
        "terminal_state": "host_access_deferred",
    }
    health = recovery._recovery_batch_health(
        [_ok(), _ok(), deferred, deferred],
        min_success_ratio=0.80,
        block_ratio_limit=0.50,
    )

    assert health["processed"] == 2
    assert health["deferred"] == 2
    assert health["healthy"] is False
    assert "circuit reopened" in health["reason"]


def test_recovery_cooldown_uses_bounded_exponential_backoff(monkeypatch):
    monkeypatch.setattr(recovery.settings, "sitemap_recovery_cooldown_initial_seconds", 60.0)
    monkeypatch.setattr(recovery.settings, "sitemap_recovery_cooldown_max_seconds", 600.0)
    monkeypatch.setattr(recovery.settings, "sitemap_recovery_cooldown_multiplier", 2.0)

    assert recovery._recovery_cooldown_seconds(1) == 60.0
    assert recovery._recovery_cooldown_seconds(2) == 120.0
    assert recovery._recovery_cooldown_seconds(4) == 480.0
    assert recovery._recovery_cooldown_seconds(5) == 600.0
    assert recovery._recovery_cooldown_seconds(20) == 600.0


def test_bulk_interval_ramps_down_when_healthy_and_backs_off_when_blocked(monkeypatch):
    monkeypatch.setattr(recovery.settings, "sitemap_recovery_bulk_min_interval_seconds", 0.5)
    monkeypatch.setattr(recovery.settings, "sitemap_recovery_bulk_initial_interval_seconds", 1.0)
    monkeypatch.setattr(recovery.settings, "sitemap_recovery_probe_interval_seconds", 5.0)
    monkeypatch.setattr(recovery.settings, "sitemap_recovery_rate_ramp_factor", 0.8)
    monkeypatch.setattr(recovery.settings, "sitemap_recovery_rate_backoff_factor", 2.0)

    assert recovery._next_bulk_interval_seconds(1.0, healthy=True) == pytest.approx(0.8)
    assert recovery._next_bulk_interval_seconds(0.5, healthy=True) == pytest.approx(0.5)
    assert recovery._next_bulk_interval_seconds(1.0, healthy=False) == pytest.approx(2.0)
    assert recovery._next_bulk_interval_seconds(4.0, healthy=False) == pytest.approx(5.0)


def test_terminal_recovery_clears_recovery_requested_latch():
    cfg = {
        "_control": {
            "cancel_requested": False,
            "recovery_requested": True,
        }
    }

    updated = recovery._clear_recovery_request(cfg, terminal_status="completed_with_warnings")

    assert updated["_control"]["recovery_requested"] is False
    assert updated["_control"]["recovery_completed_at"]
    assert updated["_control"]["recovery_terminal_status"] == "completed_with_warnings"


def test_pending_checkpoint_row_is_not_recoverable():
    from types import SimpleNamespace

    row = SimpleNamespace(
        error="Pending page resolution",
        http_status=None,
        classification_evidence_json={"terminal_state": "pending_resolution"},
    )
    assert recovery._is_recoverable(row) is False
