import copy
from unittest.mock import Mock

import pytest
import requests

from app.drama_client import (DramaAuthError, DramaClient, DramaRateLimited,
                              DramaUpstreamError, MediaUpload, firestore_document,
                              result_urls, validate_approval)
from app.model_catalog import ASPECT_RATIOS, normalize_generation_request, public_models


def payload(**values):
    return normalize_generation_request({"prompt": "A lake at sunrise", **values})


def approval():
    return {"approvalId": "quote-1", "kind": "credit_quote", "quote": {
        "totalCredits": 225, "items": [{"action": "generate_video", "credits": 225,
        "details": {"service": "seedance-2-0-mini", "resolution": "480p", "duration_seconds": 5}}],
        "raw": {"approvalPreview": {"sections": [{"items": [{"label": "Aspect ratio", "value": "16:9"}]}]}}}}


@pytest.mark.parametrize("model,seconds,resolutions", [
    ("seedance-2.0-mini", (5, 12), ("480p", "720p")),
    ("seedance-2.0-fast", (4, 15), ("480p", "720p")),
    ("seedance-2.0", (4, 15), ("480p", "720p", "1080p", "4k")),
    ("seedance-2.5", (5, 30), ("480p", "720p")),
])
def test_model_matrix(model, seconds, resolutions):
    for duration in seconds:
        for resolution in resolutions:
            for ratio in ASPECT_RATIOS:
                normalized = payload(model=model, duration=duration, resolution=resolution, aspect_ratio=ratio)
                assert (normalized["duration"], normalized["resolution"], normalized["aspect_ratio"]) == (duration, resolution, ratio)
    for duration in (seconds[0] - 1, seconds[1] + 1):
        with pytest.raises(ValueError):
            payload(model=model, duration=duration)


@pytest.mark.parametrize("values", [
    {"model": "seedance-2.0-fast", "resolution": "1080p"},
    {"model": "seedance-2.5", "resolution": "1080p"},
    {"duration": True}, {"duration": 5.5}, {"aspect_ratio": "adaptive"},
    {"max_credits": float("nan")}, {"max_credits": -1}, {"max_credits": True},
    {"generate_audio": "false"}, {"background": "false"}, {"n": 1.5},
    {"size": "1280x720"}, {"seed": 12}, {"web_search": True},
])
def test_reject_unrepresentable_parameters(values):
    with pytest.raises(ValueError):
        payload(**values)


def test_catalog_states_evidence():
    models = public_models()
    assert len(models) == 4
    assert "observed_success" in models[0]["capabilities"]["verification"]
    assert "official_page" in models[-1]["capabilities"]["verification"]


def test_references_are_preserved_and_audio_requires_visual_reference():
    with pytest.raises(ValueError, match="require an image"):
        payload(audio_url="https://example.com/a.wav")
    item = payload(prompt="Use @image1 and @audio1", image_url="https://example.com/i.png",
                   audio_urls=["https://example.com/a.wav", "https://example.com/b.wav"])
    assert item["prompt"] == "Use @image1 and @audio1"
    assert len(item["_audio"]) == 2
    with pytest.raises(ValueError, match="at most 9"):
        payload(image_urls=[f"https://example.com/{i}.png" for i in range(10)])


def test_quote_rejects_parameter_switch_and_extra_expense():
    p = payload()
    assert validate_approval(approval(), p) == 225
    for field, value in (("service", "seedance-2-5"), ("resolution", "720p"), ("duration_seconds", 6)):
        quote = approval()
        quote["quote"]["items"][0]["details"][field] = value
        with pytest.raises(DramaUpstreamError, match="不一致"):
            validate_approval(quote, p)
    quote = approval()
    quote["quote"]["items"].append(copy.deepcopy(quote["quote"]["items"][0]))
    with pytest.raises(DramaUpstreamError):
        validate_approval(quote, p)
    with pytest.raises(DramaUpstreamError) as error:
        validate_approval(approval(), payload(max_credits=200))
    assert error.value.code == "CREDIT_LIMIT_EXCEEDED"


def client(settings):
    return DramaClient({"access_token": "test-token", "refresh_token": "test-refresh", "user_id": "owner"}, settings)


def response(status=200, body=None):
    value = Mock(status_code=status, ok=status < 400, headers={})
    value.json.return_value = body if body is not None else {"ok": True}
    return value


def test_mutations_not_repeated_on_network_failure(settings, monkeypatch):
    item = client(settings)
    monkeypatch.setattr("app.drama_client.time.sleep", lambda _: None)
    item.session.request = Mock(side_effect=requests.Timeout())
    with pytest.raises(DramaUpstreamError) as error:
        item.start_generation("owned-project")
    assert error.value.code == "SUBMISSION_UNCERTAIN"
    assert item.session.request.call_count == 1
    item.session.request.reset_mock()
    with pytest.raises(DramaUpstreamError) as error:
        item.project_jobs("owned-project")
    assert error.value.code == "NETWORK_ERROR"
    assert item.session.request.call_count == 2


def test_auth_refresh_persists_rotated_credentials(settings):
    updates = []
    item = DramaClient({"refresh_token": "old"}, settings, on_auth_update=updates.append)
    item.session.request = Mock(return_value=response(body={"id_token": "fresh", "refresh_token": "rotated", "user_id": "owner", "expires_in": "3600"}))
    item.login()
    assert updates[0]["access_token"] == "fresh"
    assert updates[0]["refresh_token"] == "rotated"
    item.session.request = Mock(return_value=response(429, {"message": "slow down"}))
    with pytest.raises(DramaRateLimited):
        item.login()


def test_401_refresh_retries_with_new_bearer(settings):
    item = client(settings)
    item.session.request = Mock(side_effect=[response(401), response(body={"id_token": "fresh", "refresh_token": "new"}), response()])
    item.start_generation("owned-project")
    assert item.session.request.call_count == 3
    assert item.session.request.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh"


def firestore_row(owner="owner", project="owned-project"):
    return {"document": {"name": "documents/async_tool_jobs/job-1", "fields": {
        "user_id": {"stringValue": owner}, "project_id": {"stringValue": project},
        "status": {"stringValue": "completed"}, "type": {"stringValue": "video"},
        "service": {"stringValue": "seedance-2-0-mini"}, "vendor_status": {"stringValue": "pending"},
        "billing": {"mapValue": {"fields": {"credits": {"integerValue": "225"}}}},
        "result": {"mapValue": {"fields": {"url": {"stringValue": "https://example.com/result.mp4"}}}}}}}


def test_completed_job_does_not_require_site_agent(settings):
    item = client(settings)
    item._request = Mock(return_value=[firestore_row()])
    detail = item.generation_detail("owned-project", payload())
    assert detail["status"] == "COMPLETE"
    assert detail["actual_cost"] == 225
    assert result_urls(detail) == ["https://example.com/result.mp4"]
    assert item._request.call_count == 1
    assert firestore_document(firestore_row()["document"])["job_ref"] == "job-1"


def trace_job(**changes):
    return {"job_ref": "trace-job", "task_action": "generate_video",
            "task_subcommand": "generate-video", "task_service": "seedance-2-5",
            "status": "pending", "billing": {"credits": 650}, **changes}


def test_submit_trace_is_visible_while_agent_is_idle(settings):
    item = client(settings)
    item.project_jobs = Mock(return_value=[trace_job()])
    item._request = Mock(return_value={"isStreaming": False})
    detail = item.generation_detail("owned-project", payload(model="seedance-2.5"))
    assert detail["job_ref"] == "trace-job"
    assert detail["status"] == "PROCESSING"
    assert detail["actual_cost"] == 650


def test_completed_submit_trace_returns_result_without_site_agent(settings):
    item = client(settings)
    item.project_jobs = Mock(return_value=[trace_job(status="completed", result={"url": "https://example.com/video.mp4"})])
    item._request = Mock(side_effect=AssertionError("completed jobs must not query the site"))
    detail = item.generation_detail("owned-project", payload(model="seedance-2.5"))
    assert detail["status"] == "COMPLETE"
    assert result_urls(detail) == ["https://example.com/video.mp4"]


@pytest.mark.parametrize("jobs,code", [
    ([trace_job(task_service="seedance-2-0")], "UPSTREAM_MODEL_MISMATCH"),
    ([trace_job(), trace_job(job_ref="another-job")], "MULTIPLE_UPSTREAM_JOBS"),
])
def test_submit_trace_enforces_model_and_single_generation(settings, jobs, code):
    item = client(settings)
    item.project_jobs = Mock(return_value=jobs)
    with pytest.raises(DramaUpstreamError) as error:
        item.generation_detail("owned-project", payload(model="seedance-2.5"))
    assert error.value.code == code


@pytest.mark.parametrize("owner,project", [("other", "owned-project"), ("owner", "other-project")])
def test_jobs_are_scoped_to_current_owner_and_project(settings, owner, project):
    item = client(settings)
    item._request = Mock(return_value=[firestore_row(owner, project)])
    with pytest.raises(DramaAuthError):
        item.project_jobs("owned-project")


def test_all_uploaded_audio_is_forwarded_and_duration_checked(settings):
    item = client(settings)
    uploads = [MediaUpload("", "https://example.com/i.png", "image", "i.png", "image/png", 50)]
    uploads += [MediaUpload("", f"https://example.com/{i}.wav", "audio", f"{i}.wav", "audio/wav", 50, 6000) for i in range(2)]
    body = item.build_generation_request(payload(), uploads)
    assert len(body["initial_reference_list"]) == 3
    assert all(ref["url"] for ref in body["initial_reference_list"])
    uploads.append(MediaUpload("", "https://example.com/c.wav", "audio", "c.wav", "audio/wav", 50, 4000))
    with pytest.raises(ValueError, match="duration"):
        item.build_generation_request(payload(), uploads)
