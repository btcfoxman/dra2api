from unittest.mock import Mock

import pytest
from PIL import Image

from app.drama_client import DramaUpstreamError
from media_samples import PNG, image_bytes
from test_media_download import SOURCE, download_client, response
from test_tasks import setup_task


@pytest.mark.parametrize("declared", ["application/octet-stream", "", "text/plain", "application/download", "IMAGE/PNG; charset=binary", "image/jpeg"])
@pytest.mark.parametrize("format,mime,extension", [("PNG", "image/png", ".png"), ("JPEG", "image/jpeg", ".jpg"), ("WEBP", "image/webp", ".webp")])
def test_actual_image_content_controls_signed_and_uploaded_type(settings, monkeypatch, declared, format, mime, extension):
    item, direct = download_client(settings, monkeypatch)
    data = image_bytes(format)
    downloaded = response((data,))
    downloaded.headers["Content-Type"] = declared
    direct.get.return_value = downloaded
    item._request = Mock(return_value={"upload_url": "https://upload.example/object", "public_url": "https://media.example/object"})
    uploaded = item.upload_media(SOURCE, "image", "image-1")
    assert (uploaded.content_type, uploaded.name, uploaded.width, uploaded.height) == (mime, "image-1" + extension, 4, 3)
    assert item._request.call_args.kwargs["json"]["content_type"] == mime
    assert item._request.call_args.kwargs["json"]["filename"] == "image-1" + extension
    assert item.session.put.call_args.kwargs["headers"] == {"Content-Type": mime}
    assert item.session.put.call_args.kwargs["data"] == data
    assert uploaded.audit_view()["source_content_type"] == declared.split(";", 1)[0].strip().lower()
    item.session.get.assert_not_called()


@pytest.mark.parametrize("name,expected", [("", "image.png"), ("asset.jpg", "asset.png"), ("asset.png", "asset.png"), ("image-1", "image-1.png")])
def test_filename_matches_actual_format_even_when_url_is_misleading(settings, monkeypatch, name, expected):
    item, direct = download_client(settings, monkeypatch)
    direct.get.return_value = response()
    item._request = Mock(return_value={"upload_url": "https://upload.example/object", "public_url": "https://media.example/object"})
    # An extension-free endpoint and an explicit display label must both work.
    uploaded = item.upload_media("https://media.example/image", "image", name)
    assert uploaded.name == expected


@pytest.mark.parametrize("body", [b"<html>Access denied</html>", b'{"error":"expired"}', b"not an image", PNG[:40]])
def test_non_image_and_corrupt_content_is_rejected_before_signing(settings, monkeypatch, body):
    item, direct = download_client(settings, monkeypatch)
    direct.get.return_value = response((body,))  # Even an image/png header proves nothing.
    item._request = Mock()
    with pytest.raises(DramaUpstreamError) as error:
        item.upload_media(SOURCE, "image", "image-1.png")
    assert error.value.code == "MEDIA_FORMAT_UNSUPPORTED"
    assert error.value.details["declared_content_type"] == "image/png"
    assert "signature" not in str(error.value.details)
    item._request.assert_not_called()
    item.session.put.assert_not_called()


def test_oversized_pixel_count_stops_before_upload(settings, monkeypatch):
    item, direct = download_client(settings, monkeypatch)
    direct.get.return_value = response()
    item._request = Mock()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(DramaUpstreamError) as error:
        item.upload_media(SOURCE, "image", "image-1")
    assert error.value.code == "MEDIA_LIMIT_EXCEEDED"
    item._request.assert_not_called()


def test_invalid_image_has_normalized_error_and_reference_audit(service, monkeypatch):
    _, task, item = setup_task(service, monkeypatch, image_url=SOURCE)
    details = {"declared_content_type": "image/png", "error_type": "UnidentifiedImageError"}
    item.upload_media.side_effect = DramaUpstreamError("图片内容无法识别或已损坏", code="MEDIA_FORMAT_UNSUPPORTED", details=details)
    service._run_task(task["id"])
    failed = service.db.get_task(task["id"])
    assert service.public_task(failed)["error"] == {
        "code": "MEDIA_FORMAT_UNSUPPORTED", "category": "MEDIA_FORMAT_UNSUPPORTED",
        "message": "素材格式不支持，请修改后再试~", "outcome": "rejected", "refunded": False}
    assert failed["upstream_response"]["protocol"]["media_validation_error"] == {"reference_index": 1, "kind": "image", **details}
    item.create_project.assert_not_called()
