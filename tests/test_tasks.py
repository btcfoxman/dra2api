from unittest.mock import Mock

from app.drama_client import DramaUpstreamError
from app.model_catalog import normalize_generation_request
from test_protocol import approval


def setup_task(service, monkeypatch):
    monkeypatch.setattr(service, "_schedule", lambda _: None)
    account = service.db.upsert_account({"name": "test", "status": "active", "last_balance": 1000,
                                         "email": "test@example.com", "user_id": "owner", "access_token": "token", "refresh_token": "refresh"})
    task = service.create_task({"prompt": "A lake at sunrise", "max_credits": 225})
    client = Mock(base="https://example.com")
    client.account_state.return_value = {"available_balance": 1000, "user_id": "owner", "email": "test@example.com"}
    client.build_generation_request.return_value = {"project_type": "video"}
    client.estimate_cost.return_value = 225
    client.create_project.return_value = "project-1"
    client.start_generation.return_value = {"ok": True}
    client.approve.return_value = {"ok": True}
    monkeypatch.setattr(service, "_client", lambda _: client)
    return account, task, client


def completed():
    return {"status": "COMPLETE", "job_ref": "job-1", "actual_cost": 225,
            "result": {"url": "https://example.com/video.mp4"}}


def test_submit_quote_complete_balance_and_audit(service, monkeypatch):
    account, task, client = setup_task(service, monkeypatch)
    client.generation_detail.side_effect = [
        {"status": "PROCESSING", "pendingApprovals": [approval()], "isStreaming": True},
        completed()]
    service._run_task(task["id"])
    done = service.db.get_task(task["id"])
    assert done["status"] == "succeeded"
    assert done["actual_cost"] == 225
    assert done["result_urls"] == ["https://example.com/video.mp4"]
    assert done["upstream_response"]["protocol"]["approved_id"] == "quote-1"
    assert service.db.get_account(account["id"])["active_tasks"] == 0
    assert done["reserved_cost"] == 0
    assert client.start_generation.call_count == client.approve.call_count == 1


def test_recovery_queries_existing_project_without_resubmission(service, monkeypatch):
    account, task, client = setup_task(service, monkeypatch)
    service.db.update_task(task["id"], account_id=account["id"], generation_id="existing", status="submitted",
                           upstream_response={"protocol": {"prompt_started": True, "approved_id": "quote-1"}})
    client.generation_detail.side_effect = [
        {"status": "PROCESSING", "pendingApprovals": [approval()]}, completed()]
    service._run_task(task["id"])
    assert service.db.get_task(task["id"])["status"] == "succeeded"
    client.create_project.assert_not_called()
    client.start_generation.assert_not_called()
    client.approve.assert_not_called()


def test_new_project_retry_clears_old_approval(service, monkeypatch):
    _, task, client = setup_task(service, monkeypatch)
    service.db.update_task(task["id"], status="failed", error_code="GENERATION_FAILED",
                           upstream_response={"protocol": {"prompt_started": True, "approved_id": "old-quote"}})
    service.retry_task(task["id"])
    client.generation_detail.side_effect = [{"status": "PROCESSING", "pendingApprovals": [approval()]}, completed()]
    service._run_task(task["id"])
    assert service.db.get_task(task["id"])["status"] == "succeeded"
    client.approve.assert_called_once_with("project-1", "quote-1")


def test_ambiguous_prompt_write_is_not_retried(service, monkeypatch):
    _, task, client = setup_task(service, monkeypatch)
    client.start_generation.side_effect = DramaUpstreamError("timeout", code="SUBMISSION_UNCERTAIN")
    client.generation_detail.return_value = completed()
    service._run_task(task["id"])
    assert service.db.get_task(task["id"])["status"] == "succeeded"
    assert client.start_generation.call_count == 1


def test_reject_over_budget_before_approval(service, monkeypatch):
    _, task, client = setup_task(service, monkeypatch)
    quote = approval()
    quote["quote"]["totalCredits"] = quote["quote"]["items"][0]["credits"] = 300
    client.generation_detail.return_value = {"status": "PROCESSING", "pendingApprovals": [quote]}
    service._run_task(task["id"])
    assert service.db.get_task(task["id"])["error_code"] == "CREDIT_LIMIT_EXCEEDED"
    client.approve.assert_not_called()


def test_refresh_token_is_durable_and_masked(service):
    account = service.db.upsert_account({"name": "session", "refresh_token": "private-refresh"})
    service.db.update_account(account["id"], {"refresh_token": "rotated-refresh"})
    assert service.db.get_account(account["id"], include_secrets=True)["refresh_token"] == "rotated-refresh"
    assert "rotated-refresh" not in str(service.db.get_account(account["id"], include_secrets=False))


def test_sync_wait_returns_pending_identifier(service, monkeypatch):
    task = service.db.create_task("pending", normalize_generation_request({"prompt": "A lake"}))
    monkeypatch.setattr("app.service.time.monotonic", Mock(side_effect=[0, 2]))
    result = service.wait_task(task["id"], timeout=1)
    assert result["id"] == "pending"
