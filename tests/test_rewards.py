import math
from unittest.mock import Mock

import pytest
import requests

from app.drama_client import DramaClient, DramaUpstreamError


def task(task_id="daily_generate", **overrides):
    return {"id": task_id, "name": task_id, "status": "completed", "claimed_at": None,
            "reward_eligible": True, "reward": {"type": "credits", "amount": 200}, **overrides}


@pytest.fixture
def rewards(service, monkeypatch):
    account = service.db.upsert_account({"name": "reward-test", "email": "owner@example.com",
                                         "access_token": "private-token", "enabled": False, "last_balance": 20})
    client = Mock()
    client.account_state.side_effect = [
        {"email": account["email"], "available_balance": 20, "buckets": {"credits": 20}},
        {"email": account["email"], "available_balance": 170, "buckets": {"credits": 170, "batches": []}},
    ]
    client.reward_tasks.return_value = [task()]
    client.claim_reward.side_effect = lambda key: {"task_id": key, "status": "claimed", "credits": 200}
    monkeypatch.setattr(service, "_client", lambda _: client)
    return account["id"], client


def test_only_completed_eligible_unclaimed_credit_rewards_are_claimed(service, rewards):
    account_id, client = rewards
    client.reward_tasks.return_value = [task(), task(), task("pending", status="pending"),
        task("register_reward", status=None), task("claimed", status="claimed"),
        task("dated", claimed_at="2026-09-17"), task("ineligible", reward_eligible=False),
        task("other", reward={"type": "subscription"})]
    result = service.claim_account_rewards(account_id)
    client.claim_reward.assert_called_once_with("daily_generate")
    assert result["eligible_count"] == result["claimed_count"] == 1
    assert result["claimed_credits"] == 200
    # Concurrent spending means balance is not old balance + rewards.
    assert result["account"]["last_balance"] == 170
    assert result["account"]["enabled"] is False
    assert "private-token" not in str(result)
    assert result["balance_refreshed"] is True
    assert result["status"] == "success"


def test_no_eligible_rewards_still_refreshes_balance(service, rewards):
    account_id, client = rewards
    client.reward_tasks.return_value = [task(status="claimed")]
    result = service.claim_account_rewards(account_id)
    client.claim_reward.assert_not_called()
    assert result["claimed_credits"] == 0
    assert result["account"]["last_balance"] == 170
    assert "暂无可领取奖励" in result["message"]


def test_duplicate_claim_is_not_counted_twice(service, rewards):
    account_id, client = rewards
    client.claim_reward.side_effect = None
    client.claim_reward.return_value = {"task_id": "daily_generate", "status": "already_claimed", "credits": 0}
    result = service.claim_account_rewards(account_id)
    assert result["already_claimed_count"] == 1
    assert result["claimed_count"] == result["claimed_credits"] == 0
    assert result["balance_refreshed"]


@pytest.mark.parametrize("confirmed", [True, False])
def test_lost_response_is_read_back_without_resubmitting(service, rewards, confirmed):
    account_id, client = rewards
    client.reward_tasks.side_effect = [[task(), task("second")], [task(status="claimed" if confirmed else "completed")]]
    client.claim_reward.side_effect = DramaUpstreamError("lost", code="SUBMISSION_UNCERTAIN")
    result = service.claim_account_rewards(account_id)
    client.claim_reward.assert_called_once()
    assert client.reward_tasks.call_count == 2
    assert result["claimed_credits"] == 0
    assert result["reconciled_count"] == int(confirmed)
    assert result["unknown_count"] == int(not confirmed)
    assert result["unattempted_count"] == 1
    assert result["balance_refreshed"]


def test_partial_claims_remain_visible_when_balance_refresh_fails(service, rewards):
    account_id, client = rewards
    client.reward_tasks.return_value = [task(), task("second")]
    client.claim_reward.side_effect = [
        {"task_id": "daily_generate", "status": "claimed", "credits": 200},
        DramaUpstreamError("Task is not completed", status_code=400),
    ]
    client.account_state.side_effect = [{"available_balance": 20}, DramaUpstreamError("offline")]
    result = service.claim_account_rewards(account_id)
    assert result["status"] == "partial"
    assert result["claimed_count"] == result["failed_count"] == 1
    assert result["claimed_credits"] == 200
    assert not result["balance_refreshed"]
    assert result["account"]["last_balance"] == 20
    assert result["account"]["balance_details"]["reward_claim_stats"]["claimed_credits"] == 200
    assert not service._claiming_rewards


def test_same_account_claim_and_daily_checkin_cannot_overlap(service, rewards):
    account_id, client = rewards

    def during_claim(key):
        with pytest.raises(DramaUpstreamError, match="账号正在") as error:
            service.claim_account_rewards(account_id)
        assert error.value.status_code == 409
        service._run_daily_checkin(account_id)
        client.daily_checkin_status.assert_not_called()
        assert not service.schedule_login(account_id)
        return {"task_id": key, "status": "claimed", "credits": 200}

    client.claim_reward.side_effect = during_claim
    service.claim_account_rewards(account_id)
    assert not service._claiming_rewards


def test_identity_failure_prevents_any_claim_and_releases_guard(service, rewards):
    account_id, client = rewards
    client.account_state.side_effect = DramaUpstreamError("identity check failed")
    with pytest.raises(DramaUpstreamError):
        service.claim_account_rewards(account_id)
    client.reward_tasks.assert_not_called()
    client.claim_reward.assert_not_called()
    assert not service._claiming_rewards


def test_reward_and_daily_stats_survive_later_balance_refresh(service, rewards):
    account_id, client = rewards
    service._store_daily_checkin_stats(account_id, {"last_sign": "2026-09-17"})
    service.claim_account_rewards(account_id)
    account = service.db.get_account(account_id)
    service._store_account_state(account_id, account, {"available_balance": 99, "buckets": {"credits": 99}})
    details = service.db.get_account(account_id)["balance_details"]
    assert details["credits"] == 99
    assert details["sign_reward_stats"]["last_sign"] == "2026-09-17"
    assert details["reward_claim_stats"]["claimed_credits"] == 200


def test_client_claim_protocol_and_duplicate_response(settings, monkeypatch):
    client = DramaClient({}, settings)
    request = Mock(return_value={"data": {"success": True, "task_id": "daily_generate",
                                          "reward_type": "credits", "reward_amount": 200}})
    monkeypatch.setattr(client, "_request", request)
    assert client.claim_reward("daily_generate")["credits"] == 200
    request.assert_called_once_with("POST", client.base + "/api/v1/task/claim", json={"task_id": "daily_generate"})
    request.side_effect = DramaUpstreamError("Reward already claimed", status_code=400)
    assert client.claim_reward("daily_generate")["status"] == "already_claimed"
    request.side_effect = DramaUpstreamError("Task is not completed", status_code=400)
    with pytest.raises(DramaUpstreamError):
        client.claim_reward("daily_generate")


@pytest.mark.parametrize("changed", [{"success": False}, {"task_id": "other"}, {"reward_type": "other"},
                                    {"reward_amount": -1}, {"reward_amount": math.nan}, {"reward_amount": True}])
def test_invalid_receipt_never_counts_as_credited(settings, monkeypatch, changed):
    client = DramaClient({}, settings)
    data = {"success": True, "task_id": "daily_generate", "reward_type": "credits", "reward_amount": 200, **changed}
    monkeypatch.setattr(client, "_request", Mock(return_value={"data": data}))
    with pytest.raises(DramaUpstreamError):
        client.claim_reward("daily_generate")


def test_claim_post_is_not_retried_on_transport_failure(settings, monkeypatch):
    client = DramaClient({}, settings)
    client.account["access_token"] = "test-token"
    monkeypatch.setattr(client, "ensure_auth", lambda: None)
    send = Mock(side_effect=requests.Timeout())
    monkeypatch.setattr(client.session, "request", send)
    with pytest.raises(DramaUpstreamError) as exc:
        client.claim_reward("daily_generate")
    assert exc.value.code == "SUBMISSION_UNCERTAIN"
    send.assert_called_once()


@pytest.mark.parametrize("body", [{}, {"data": {}}, {"data": {"tasks": {}}}, {"data": {"tasks": [None]}}])
def test_invalid_task_list_is_not_reported_as_no_rewards(settings, monkeypatch, body):
    client = DramaClient({}, settings)
    monkeypatch.setattr(client, "_request", Mock(return_value=body))
    with pytest.raises(DramaUpstreamError, match="列表格式异常"):
        client.reward_tasks()
