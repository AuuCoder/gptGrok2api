from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from services.openai_backend_api import InvalidAccessTokenError
from services.openai_survival_service import OpenAISurvivalService


def test_access_token_401_is_token_dead_not_deactivated(tmp_path) -> None:
    service = OpenAISurvivalService(tmp_path)
    backend = MagicMock()
    backend.__enter__.return_value.get_user_info.side_effect = InvalidAccessTokenError(
        "token invalidated (/backend-api/me)"
    )

    with patch("services.openai_backend_api.OpenAIBackendAPI", return_value=backend):
        result = service._probe_one(
            {"access_token": "expired-token", "source_type": "chatgpt_web"},
            refresh_first=True,
        )

    assert result["status"] == "token_dead"
    assert "account_deactivated" not in result["status"]


def test_token_dead_probe_preserves_previous_confirmation(tmp_path) -> None:
    service = OpenAISurvivalService(tmp_path)
    update = MagicMock()
    original = {
        "access_token": "expired-token",
        "survival_status": "free",
        "survival_alive": True,
        "survival_first_confirmed_at": "2026-07-20T00:00:00+00:00",
        "survival_last_confirmed_at": "2026-07-22T00:00:00+00:00",
        "survival_observed_seconds": 172800,
    }

    with patch("services.openai_survival_service.account_service.update_account", update):
        status = service._persist_probe(
            original,
            {"status": "token_dead", "error": "at_probe_failed: http_401", "tier": 3},
        )

    assert status == "token_dead"
    updates = update.call_args.args[1]
    assert updates["survival_status"] == "free"
    assert updates["survival_last_probe_status"] == "token_dead"
    assert "survival_alive" not in updates
    assert "survival_last_confirmed_at" not in updates
    assert "survival_observed_seconds" not in updates


def test_confirmed_probe_observes_survival_from_account_creation(tmp_path) -> None:
    service = OpenAISurvivalService(tmp_path)
    update = MagicMock()
    original = {
        "access_token": "active-token",
        "created_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
    }

    with patch("services.openai_survival_service.account_service.update_account", update):
        status = service._persist_probe(
            original,
            {"status": "free", "plan_type": "free", "tier": 1},
        )

    assert status == "free"
    updates = update.call_args.args[1]
    assert abs(updates["survival_observed_seconds"] - 3 * 24 * 60 * 60) <= 2


def test_scheduler_lease_is_exclusive_and_releasable(tmp_path) -> None:
    first = OpenAISurvivalService(tmp_path)
    second = OpenAISurvivalService(tmp_path)

    assert first._claim_run(ttl_seconds=60) is True
    assert second._claim_run(ttl_seconds=60) is False
    assert first._renew_run(ttl_seconds=60) is True
    first._release_run()
    assert second._claim_run(ttl_seconds=60) is True
    second._release_run()
