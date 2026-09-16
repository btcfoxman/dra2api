import importlib
import re

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api(service, monkeypatch):
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "service", service)
    monkeypatch.setattr(main, "database", service.db)
    monkeypatch.setattr(main, "settings", service.settings)
    monkeypatch.setattr(service, "_schedule", lambda _: None)
    return TestClient(main.app)


@pytest.mark.parametrize("path", ["/v1/models", "/v1/videos/unknown", "/api/accounts", "/api/tasks", "/api/settings", "/api/model-costs"])
def test_sensitive_endpoints_require_auth(api, path):
    assert api.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/v1/videos", "/v1/videos/generations", "/api/videos/generate", "/api/v3/contents/generations/tasks"])
def test_external_create_query_and_download_aliases(api, service, path):
    headers = {"Authorization": "Bearer test-api"}
    created = api.post(path, headers=headers, json={"model": "seedance-2.0-mini", "prompt": "A lake", "background": True})
    assert created.status_code == 200
    task = created.json().get("data") if path.endswith("tasks") else created.json()
    identifier = task["id"]
    assert api.get(f"/v1/videos/{identifier}/content", headers=headers).status_code == 409
    service.db.update_task(identifier, status="succeeded", result_urls=["https://example.com/result.mp4"], actual_cost=225)
    done = api.get(f"/v1/videos/{identifier}", headers=headers).json()
    assert done["status"] == "succeeded"
    assert done["data"] == [{"url": "https://example.com/result.mp4"}]
    assert "upstream_response" not in done
    download = api.get(f"/v1/videos/{identifier}/content", headers=headers, follow_redirects=False)
    assert download.status_code == 307
    assert download.headers["location"] == "https://example.com/result.mp4"


def test_responses_translates_multimodal_input(api, service):
    created = api.post("/v1/responses", headers={"X-API-Key": "test-api"}, json={
        "model": "seedance-2.0", "background": True, "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "Animate this lake"},
            {"type": "input_image", "image_url": {"url": "https://example.com/lake.png"}}]}]})
    assert created.status_code == 200
    task = service.db.get_task(created.json()["id"])
    assert task["request"]["_images"][0]["value"] == "https://example.com/lake.png"
    assert task["request"]["prompt"] == "Animate this lake"


def test_content_task_alias_accepts_text(api):
    result = api.post("/api/v3/contents/generations/tasks", headers={"X-API-Key": "test-api"},
                      json={"content": [{"type": "text", "text": "A lake at sunrise"}]})
    assert result.status_code == 200


def test_validation_is_returned_before_queueing(api, service):
    result = api.post("/v1/videos", headers={"X-API-Key": "test-api"}, json={"prompt": "A lake", "duration": 4})
    assert result.status_code == 422
    assert service.db.active_task_count() == 0


def test_admin_login_settings_and_secret_masking(api, service):
    response = api.post("/login", data={"token": "test-admin"}, follow_redirects=False)
    assert response.status_code == 303
    assert "HttpOnly" in response.headers["set-cookie"]
    assert api.get("/api/settings").status_code == 200
    changed = api.patch("/api/settings", json={"task_workers": 2})
    assert changed.status_code == 200
    service.db.upsert_account({"name": "owner", "access_token": "private-id", "refresh_token": "private-refresh", "password": "private-password"})
    response = api.get("/api/accounts")
    assert response.status_code == 200
    for secret in ("private-id", "private-refresh", "private-password"):
        assert secret not in response.text
    assert api.get("/api/integration-docs").status_code == 200
    api.post("/logout")
    assert api.get("/api/accounts").status_code == 401


def test_https_admin_cookie_is_secure(api):
    response = api.post("/login", data={"token": "test-admin"}, headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)
    assert "; Secure" in response.headers["set-cookie"]


@pytest.mark.parametrize("path", ["/login", "/"])
def test_html_uses_versioned_assets_to_avoid_stale_cdn_content(api, path):
    api.post("/login", data={"token": "test-admin"}, follow_redirects=False)
    response = api.get(path)
    assert response.headers["cache-control"] == "no-store"
    assets = re.findall(r'(?:src|href)="(/static/[^\"]+)"', response.text)
    assert assets
    assert all("?v=" in asset for asset in assets)
    for asset in assets:
        assert api.get(asset).status_code == 200


@pytest.mark.parametrize("billing,refund_suffix", [({}, "~"), ({"status": "refunded"}, "，积分已返还~")])
@pytest.mark.parametrize("raw_error,prefix", [
    ("Content moderation rejected: input image was flagged as containing a real person", "参考图片中检测到可能存在真人，暂不支持，请更换图片后重试"),
    ("OutputVideoSensitiveContentDetected.PolicyViolation: The request failed because the output video may be related to copyright restrictions.", "生成的视频内容违规，请修改描述后重试"),
    ("OutputAudioSensitiveContentDetected.PolicyViolation: Please modify the prompt.", "生成的视频内容违规，请修改描述后重试"),
])
def test_admin_and_public_error_messages_match_without_overwriting_audit(api, service, billing, refund_suffix, raw_error, prefix):
    task = service.create_task({"prompt": "A lake"})
    task_id = task["id"]
    service.db.update_task(task_id, status="failed", error_code="GENERATION_FAILED", error_message=raw_error,
                           raw_status={"billing": billing})
    expected = prefix + refund_suffix
    headers = {"Authorization": "Bearer test-api"}
    for path in [f"/v1/videos/{task_id}", f"/api/videos/{task_id}", f"/v1/responses/{task_id}"]:
        assert api.get(path, headers=headers).json()["error"]["message"] == expected
    assert api.get(f"/api/v3/contents/generations/tasks/{task_id}", headers=headers).json()["data"]["error"]["message"] == expected
    api.post("/login", data={"token": "test-admin"}, follow_redirects=False)
    detail = api.get(f"/api/tasks/{task_id}").json()
    assert detail["public_error_message"] == expected
    assert detail["error_message"] == raw_error
    summary = next(item for item in api.get("/api/tasks").json() if item["id"] == task_id)
    assert summary["public_error_message"] == expected
    assert "raw_status" not in summary


def test_upstream_image_size_limit_is_normalized_across_task_endpoints(api, service):
    raw_error = "File exceeds the 10 MB images limit"
    task = service.create_task({"prompt": "A lake"})
    task_id = task["id"]
    service.db.update_task(task_id, status="failed", error_code="DRAMA_HTTP_ERROR", error_message=raw_error)
    expected = {"code": "DRAMA_HTTP_ERROR", "message": "素材超限，请修改后再试~",
                "category": "MEDIA_LIMIT_EXCEEDED", "outcome": "rejected", "refunded": False}
    headers = {"Authorization": "Bearer test-api"}
    for path in [f"/v1/videos/{task_id}", f"/api/videos/{task_id}", f"/v1/responses/{task_id}"]:
        assert api.get(path, headers=headers).json()["error"] == expected
    assert api.get(f"/api/v3/contents/generations/tasks/{task_id}", headers=headers).json()["data"]["error"] == expected
    api.post("/login", data={"token": "test-admin"}, follow_redirects=False)
    detail = api.get(f"/api/tasks/{task_id}").json()
    assert detail["error_message"] == raw_error
    assert detail["public_error_message"] == expected["message"]
    summary = next(item for item in api.get("/api/tasks").json() if item["id"] == task_id)
    assert summary["public_error_message"] == expected["message"]
