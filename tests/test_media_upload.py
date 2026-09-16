from dataclasses import replace
from unittest.mock import Mock, call

import pytest
import requests

from app.drama_client import DramaClient, DramaUpstreamError
from test_tasks import setup_task


def response(status):
    item = requests.Response()
    item.status_code = status
    item._content = b""
    item.close = Mock()
    return item


def upload_client(settings, monkeypatch):
    item = DramaClient({"access_token": "token"}, replace(settings, request_retries=2))
    item._request = Mock(return_value={"upload_url": "https://upload.example/object?private=signature",
                                       "public_url": "https://media.example/object"})
    monkeypatch.setattr("app.drama_client.time.sleep", Mock())
    return item


@pytest.mark.parametrize("failure", [requests.Timeout(), requests.ConnectionError(), 408, 429, 500, 503])
def test_transient_upload_reuses_same_signed_object_and_bytes(settings, monkeypatch, failure):
    item = upload_client(settings, monkeypatch)
    first = response(failure) if isinstance(failure, int) else failure
    item.session.put = Mock(side_effect=[first, response(200)])
    uploaded = item.upload_media("data:image/png;base64,aW1hZ2U=", "image")
    assert uploaded.url == "https://media.example/object"
    assert item._request.call_count == 1  # Sign once, do not re-upload earlier refs.
    assert item.session.put.call_count == 2
    assert item.session.put.call_args_list[0] == item.session.put.call_args_list[1]
    assert item.session.put.call_args.kwargs["data"] == b"image"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 415])
def test_permanent_upload_rejection_is_not_retried(settings, monkeypatch, status):
    item = upload_client(settings, monkeypatch)
    item.session.put = Mock(return_value=response(status))
    with pytest.raises(DramaUpstreamError) as error:
        item.upload_media("data:image/png;base64,aW1hZ2U=", "image")
    assert error.value.code == "MEDIA_UPLOAD_FAILED"
    assert len(error.value.details["attempts"]) == 1
    assert item.session.put.call_count == 1
    assert "signature" not in str(error.value.details)


def test_upload_exhausts_bounded_retry_budget_and_honors_backoff(settings, monkeypatch):
    item = upload_client(settings, monkeypatch)
    item.session.put = Mock(side_effect=requests.Timeout())
    with pytest.raises(DramaUpstreamError) as error:
        item.upload_media("data:image/png;base64,aW1hZ2U=", "image")
    assert len(error.value.details["attempts"]) == 3
    assert item.session.put.call_count == 3
    from app.drama_client import time
    assert time.sleep.call_args_list == [call(1), call(2)]


def test_upload_failure_audits_reference_and_never_creates_project(service, monkeypatch):
    _, task, item = setup_task(service, monkeypatch, image_url="https://media.example/image.png")
    attempts = [{"attempt": 1, "error_type": "Timeout", "status_code": None, "retryable": True}]
    item.upload_media.side_effect = DramaUpstreamError("素材上传失败", code="MEDIA_UPLOAD_FAILED", details={"attempts": attempts})
    service._run_task(task["id"])
    failed = service.db.get_task(task["id"])
    assert failed["upstream_response"]["protocol"]["media_upload_error"] == {"reference_index": 1, "kind": "image", "attempts": attempts}
    assert service.public_task(failed)["error"]["outcome"] == "rejected"
    item.create_project.assert_not_called()


def test_upload_respects_retry_after_and_closes_each_response(settings, monkeypatch):
    item = upload_client(settings, monkeypatch)
    limited, success = response(429), response(200)
    limited.headers["Retry-After"] = "4"
    item.session.put = Mock(side_effect=[limited, success])
    item._upload_bytes("https://upload.example/object", b"image", "image/png")
    from app.drama_client import time
    time.sleep.assert_called_once_with(4)
    limited.close.assert_called_once()
    success.close.assert_called_once()
