import copy
import json
from unittest.mock import call

import pytest

from app.approval_recovery import recovery_evidence
from app.drama_client import DramaUpstreamError
from test_protocol import approval, client
from test_tasks import completed, setup_task


PARSE_ERROR = "/bin/bash: -c: line 3: unexpected EOF while looking for matching `''\n\n\nCommand exited with code 2"


def history(call_id="call-1", text=PARSE_ERROR, command='dl generate-video --service=seedance-2-0-mini --prompt="I\'ve'):
    return {"entries": [
        {"message": {"role": "assistant", "content": [{"type": "toolCall", "id": call_id,
            "name": "bash", "arguments": {"command": command}}]}},
        {"message": {"role": "toolResult", "toolCallId": call_id, "toolName": "bash",
            "isError": True, "content": [{"type": "text", "text": text}]}}
    ], "billingMarkers": []}


def protocol():
    return {"approved_id": "quote-1", "approved_tool_call_id": "call-1", "prompt_started": True,
            "quoted_credits": 225, "quote": approval()["quote"]}


def revised_quote():
    return {**approval(), "approvalId": "quote-2", "toolCallId": "call-2", "dryRun": {"status": "passed"}}


def test_observed_parser_failure_and_legacy_denial_are_recoverable():
    p = protocol()
    assert recovery_evidence(history(), p, [revised_quote()])["failed_tool_call_id"] == "call-1"
    p.pop("approved_tool_call_id")  # Existing tasks predate tool-call persistence.
    p.update(denied_approval_ids=["quote-2"], extra_quotes=[revised_quote()])
    h = history()
    h["entries"] += history("call-2", json.dumps({"code": "E_USER_DENIED_TOOL_APPROVAL"}))["entries"]
    assert recovery_evidence(h, p, [])["inspected_tool_call_ids"] == ["call-1", "call-2"]


@pytest.mark.parametrize("text", ["timeout", "No charge was attempted", '{"code":"E_SCHEMA"}',
                                  '{"ok":true,"job_ref":"paid-job"}',
                                  PARSE_ERROR + '\n{"job_ref":"paid-job"}'])
def test_prose_unknown_errors_and_submissions_never_authorize_recovery(text):
    assert recovery_evidence(history(text=text), protocol(), []) is None


@pytest.mark.parametrize("change", ["charged", "job", "different_call", "missing_result", "extra_call", "limit", "compound"])
def test_ambiguous_history_or_existing_submission_vetoes_recovery(change):
    h, p = history(), protocol()
    if change == "charged":
        h["billingMarkers"] = [{"credits": 225}]
    elif change == "job":
        p["job_ref"] = "paid-job"
    elif change == "different_call":
        p["approved_tool_call_id"] = "unobserved"
    elif change == "missing_result":
        h["entries"].pop()
    elif change == "extra_call":
        h["entries"] += history("unobserved")["entries"][:1]
    elif change == "limit":
        p["approval_repairs"] = [{}, {}]
    else:
        h = history(command='dl generate-video --help; dl generate-video --prompt="bad')
    assert recovery_evidence(h, p, []) is None


def recovering_task(service, monkeypatch):
    account, task, item = setup_task(service, monkeypatch)
    service.db.update_task(task["id"], account_id=account["id"], generation_id="project-1", status="submitted",
                           upstream_response={"protocol": protocol()})
    item.project_history.return_value = history()
    item.project_jobs.return_value = []
    item.continue_generation.return_value = {"ok": True, "promptId": "resume-1"}
    return task, item


def test_corrected_quote_is_approved_after_proven_pre_submission_failure(service, monkeypatch):
    task, item = recovering_task(service, monkeypatch)
    item.generation_detail.side_effect = [
        {"status": "PROCESSING", "isStreaming": True, "pendingApprovals": [revised_quote()]}, completed()]
    service._run_task(task["id"])
    done = service.db.get_task(task["id"])
    assert done["status"] == "succeeded"
    assert done["actual_cost"] == 225
    assert done["upstream_response"]["protocol"]["approval_repairs"][0]["previous_approval_id"] == "quote-1"
    item.approve.assert_called_once_with("project-1", "quote-2")
    item.start_generation.assert_not_called()
    item.continue_generation.assert_not_called()


@pytest.mark.parametrize("change", ["cost", "references", "dry_run", "model"])
def test_recovery_never_changes_production_package(service, monkeypatch, change):
    task, item = recovering_task(service, monkeypatch)
    revised = copy.deepcopy(revised_quote())
    if change == "cost":
        revised["quote"]["totalCredits"] = revised["quote"]["items"][0]["credits"] = 226
    elif change == "references":
        revised["quote"]["raw"]["approvalPreview"]["sections"].append({"type": "kv", "items": [{"label": "Asset 1", "value": "different"}]})
    elif change == "dry_run":
        revised.pop("dryRun")
    else:
        revised["quote"]["items"][0]["details"]["service"] = "seedance-2-5"
    item.generation_detail.return_value = {"status": "PROCESSING", "pendingApprovals": [revised]}
    service._run_task(task["id"])
    assert service.db.get_task(task["id"])["status"] == "failed"
    item.approve.assert_not_called()


@pytest.mark.parametrize("visible_at", ["detail", "recheck"])
def test_paid_job_wins_over_previously_failed_command(service, monkeypatch, visible_at):
    task, item = recovering_task(service, monkeypatch)
    detail = {"status": "PROCESSING", "pendingApprovals": [revised_quote()]}
    if visible_at == "detail":
        detail["job_ref"] = "job-1"
    else:
        item.project_jobs.return_value = [{"job_ref": "job-1"}]
    item.generation_detail.side_effect = [detail, completed()]
    service._run_task(task["id"])
    item.approve.assert_called_once_with("project-1", "quote-2", decision="denied")
    item.continue_generation.assert_not_called()
    assert service.db.get_task(task["id"])["status"] == "succeeded"


@pytest.mark.parametrize("uncertain", [False, True])
def test_paused_project_continues_once_across_restart(service, monkeypatch, uncertain):
    task, item = recovering_task(service, monkeypatch)
    idle = {"status": "PROCESSING", "pendingApprovals": [], "isStreaming": False}
    def continue_once(*args):
        saved = service.db.get_task(task["id"])["upstream_response"]["protocol"]
        assert saved["continuations"][0]["failed_tool_call_id"] == "call-1"
        service._stop.set()
        if uncertain:
            raise DramaUpstreamError("timeout", code="SUBMISSION_UNCERTAIN")
        return {"ok": True}
    item.continue_generation.side_effect = continue_once
    item.generation_detail.return_value = idle
    service._run_task(task["id"])
    service._stop.clear()
    item.generation_detail.side_effect = [idle, {"status": "PROCESSING", "pendingApprovals": [revised_quote()]}, completed()]
    service._run_task(task["id"])
    item.continue_generation.assert_called_once_with("project-1", "zh-cn")
    item.approve.assert_called_once_with("project-1", "quote-2")
    item.create_project.assert_not_called()
    assert service.db.get_task(task["id"])["status"] == "succeeded"


def test_history_and_continuation_use_observed_project_scoped_endpoints(settings):
    item = client(settings)
    from unittest.mock import Mock
    item._request = Mock(return_value={"ok": True})
    item.project_history("project-1")
    item.continue_generation("project-1")
    assert item._request.call_args_list[0] == call("GET", item.site + "/api/pi/history", project_id="project-1")
    args, kwargs = item._request.call_args_list[1]
    assert args == ("POST", item.site + "/api/pi/prompt")
    assert kwargs["project_id"] == "project-1"
    assert "确认提交" in kwargs["json"]["text"]


def test_uncertain_corrected_approval_is_not_repeated_after_restart(service, monkeypatch):
    task, item = recovering_task(service, monkeypatch)
    pending = {"status": "PROCESSING", "pendingApprovals": [revised_quote()]}
    def approve_once(*args):
        saved = service.db.get_task(task["id"])["upstream_response"]["protocol"]
        assert saved["approved_id"] == "quote-2"
        assert saved["approved_tool_call_id"] == "call-2"
        service._stop.set()
        raise DramaUpstreamError("timeout", code="SUBMISSION_UNCERTAIN")
    item.approve.side_effect = approve_once
    item.generation_detail.return_value = pending
    service._run_task(task["id"])
    service._stop.clear()
    item.generation_detail.side_effect = [pending, completed()]
    service._run_task(task["id"])
    item.approve.assert_called_once_with("project-1", "quote-2")
    assert service.db.get_task(task["id"])["status"] == "succeeded"


def test_manual_paid_job_blocks_first_gateway_approval(service, monkeypatch):
    _, task, item = setup_task(service, monkeypatch)
    item.generation_detail.side_effect = [
        {"status": "PROCESSING", "job_ref": "job-1", "pendingApprovals": [approval()]}, completed()]
    service._run_task(task["id"])
    item.approve.assert_called_once_with("project-1", "quote-1", decision="denied")
    assert service.db.get_task(task["id"])["status"] == "succeeded"
