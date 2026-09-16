from dataclasses import replace
from unittest.mock import MagicMock, Mock

import pytest
import requests

from app.drama_client import DramaClient, DramaUpstreamError


SOURCE = "https://media.example.com/image.png?signature=private"


def response(chunks=(b"image-data",), status=200):
    value = MagicMock(status_code=status, headers={"Content-Type": "image/png; charset=binary"})
    value.__enter__.return_value = value
    value.iter_content.return_value = iter(chunks)
    if status >= 400:
        value.raise_for_status.side_effect = requests.HTTPError("rejected", response=value)
    return value


def download_client(settings, monkeypatch, *, proxy=True):
    client = DramaClient({"proxy_url": "socks5://proxy.example:1080"} if proxy else {}, settings)
    client.session.get = Mock()
    client.session.put = Mock(return_value=Mock(status_code=200))
    direct = MagicMock(headers={})
    direct.__enter__.return_value = direct
    monkeypatch.setattr("app.drama_client.requests.Session", Mock(return_value=direct))
    return client, direct


def test_direct_download_then_upload_uses_account_proxy(settings, monkeypatch):
    client, direct = download_client(settings, monkeypatch)
    direct.get.return_value = response()
    client._request = Mock(return_value={"upload_url": "https://upload.example/signed", "public_url": "https://upload.example/image.png"})
    upload = client.upload_media(SOURCE, "image")
    assert upload.size == len(b"image-data")
    assert direct.trust_env is False
    client.session.get.assert_not_called()
    assert client.session.proxies["https"] == "socks5h://proxy.example:1080"
    assert client.session.put.call_args.kwargs["data"] == b"image-data"


@pytest.mark.parametrize("failure", [requests.ConnectTimeout(), requests.exceptions.SSLError(), requests.ConnectionError(), "http403", "partial"])
def test_failed_direct_download_falls_back_once_and_discards_partial_bytes(settings, monkeypatch, failure):
    client, direct = download_client(settings, monkeypatch)
    order = []
    def first(*args, **kwargs):
        order.append("direct")
        if failure == "http403":
            return response(status=403)
        if failure == "partial":
            def broken_stream():
                yield b"discard-me"
                raise requests.exceptions.ChunkedEncodingError()
            return response(broken_stream())
        raise failure
    def second(*args, **kwargs):
        order.append("proxy")
        return response((b"complete-image",))
    direct.get.side_effect = first
    client.session.get.side_effect = second
    data, mime = client._download_media(SOURCE)
    assert order == ["direct", "proxy"]
    assert (data, mime) == (b"complete-image", "image/png")
    assert direct.get.call_count == client.session.get.call_count == 1


@pytest.mark.parametrize("proxy", [False, True])
def test_all_routes_failed_reports_safe_diagnostics(settings, monkeypatch, proxy):
    client, direct = download_client(settings, monkeypatch, proxy=proxy)
    direct.get.side_effect = requests.Timeout(SOURCE)
    client.session.get.side_effect = requests.ConnectionError("proxy password")
    with pytest.raises(DramaUpstreamError) as result:
        client._download_media(SOURCE)
    assert result.value.code == "MEDIA_DOWNLOAD_FAILED"
    attempts = result.value.details["attempts"]
    assert [a["route"] for a in attempts] == (["direct", "account_proxy"] if proxy else ["direct"])
    assert all(a["source_host"] == "media.example.com" for a in attempts)
    assert "signature" not in str(result.value.details)
    assert "password" not in str(result.value.details)
    assert client.session.get.call_count == int(proxy)


def test_size_validation_does_not_retry_via_proxy(settings, monkeypatch):
    client, direct = download_client(replace(settings, media_max_bytes=2), monkeypatch)
    direct.get.return_value = response()
    with pytest.raises(ValueError, match="size limit"):
        client._download_media(SOURCE)
    client.session.get.assert_not_called()


def test_base64_does_not_download(settings, monkeypatch):
    client, direct = download_client(settings, monkeypatch)
    client._request = Mock(return_value={"upload_url": "https://upload.example/signed", "public_url": "https://upload.example/image.png"})
    client.upload_media("data:image/png;base64,aW1hZ2U=", "image")
    direct.get.assert_not_called()
    client.session.get.assert_not_called()
