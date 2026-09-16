from unittest.mock import Mock, call

import pytest

from app.drama_client import DramaAccountSuspended, DramaAuthError, DramaRiskBlocked, DramaUpstreamError
from app.model_catalog import normalize_generation_request
from test_protocol import approval


def setup_task(service, monkeypatch, **request_values):
    monkeypatch.setattr(service, "_schedule", lambda _: None)
    account = service.db.upsert_account({"name": "test", "status": "active", "last_balance": 1000,
                                         "email": "test@example.com", "user_id": "owner", "access_token": "token", "refresh_token": "refresh"})
    task = service.create_task({"prompt": "A lake at sunrise", "max_credits": 225, **request_values})
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


@pytest.mark.parametrize("uncertain_denial", [False, True])
def test_duplicate_quote_is_denied_without_losing_paid_job(service, monkeypatch, uncertain_denial):
    _, task, client = setup_task(service, monkeypatch)
    extra = approval()
    extra['approvalId'] = 'quote-2'
    client.generation_detail.side_effect = [
        {'status': 'PROCESSING', 'pendingApprovals': [approval()], 'isStreaming': True},
        {'status': 'PROCESSING', 'job_ref': 'job-1', 'pendingApprovals': [extra]},
        {'status': 'PROCESSING', 'job_ref': 'job-1', 'pendingApprovals': [extra]},
        completed(),
    ]
    if uncertain_denial:
        client.approve.side_effect = [{'ok': True}, DramaUpstreamError('timeout', code='SUBMISSION_UNCERTAIN')]
    service._run_task(task['id'])
    done = service.db.get_task(task['id'])
    assert done['status'] == 'succeeded'
    assert done['actual_cost'] == 225
    assert done['upstream_response']['protocol']['denied_approval_ids'] == ['quote-2']
    assert client.approve.call_args_list == [call('project-1', 'quote-1'), call('project-1', 'quote-2', decision='denied')]
    client.create_project.assert_called_once()


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
    assert service.db.get_task(task["id"])["raw_status"]["pendingApprovals"][0]["quote"]["totalCredits"] == 300
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


def test_initial_cost_estimate_avoids_underfunded_account(service, monkeypatch):
    monkeypatch.setattr(service, "_schedule", lambda _: None)
    low = service.db.upsert_account({"name": "low", "status": "active", "last_balance": 100})
    funded = service.db.upsert_account({"name": "funded", "status": "active", "last_balance": 1000})
    task = service.create_task({"prompt": "A lake", "model": "seedance-2.0-fast"})
    assert task["estimated_cost"] == 325
    selected = service._acquire_task_account(task, float("inf"))
    assert selected["id"] == funded["id"]
    assert selected["id"] != low["id"]
    service.db.release_account(selected["id"], task_id=task["id"])


@pytest.mark.parametrize("error,status", [(DramaAccountSuspended(), "suspended"),
                                         (DramaAuthError(), "login_required"),
                                         (DramaRiskBlocked(), "challenge_required")])
def test_blocked_accounts_are_removed_from_dispatch(service, monkeypatch, error, status):
    account, task, client = setup_task(service, monkeypatch)
    client.generation_detail.side_effect = error
    service._run_task(task["id"])
    assert service.db.get_account(account["id"])["status"] == status
    assert service.db.available_account_count() == 0
    assert service.db.get_account(account["id"])["active_tasks"] == 0
    assert client.account_state.call_count == 1


def test_media_download_failure_records_reference_and_routes_before_submission(service, monkeypatch):
    _, task, client = setup_task(service, monkeypatch,
                                 image_urls=["https://example.com/one.png", "https://example.com/two.png"])
    attempts = [{"route": "direct", "source_host": "example.com", "error_type": "ReadTimeout", "status_code": None},
                {"route": "account_proxy", "source_host": "example.com", "error_type": "HTTPError", "status_code": 403}]
    client.upload_media.side_effect = [Mock(), DramaUpstreamError("failed", code="MEDIA_DOWNLOAD_FAILED", details={"attempts": attempts})]
    service._run_task(task["id"])
    failed = service.db.get_task(task["id"])
    assert failed["status"] == "failed"
    assert failed["upstream_response"]["protocol"]["media_download_error"] == {"reference_index": 2, "kind": "image", "attempts": attempts}
    client.create_project.assert_not_called()
    assert failed["reserved_cost"] == 0
