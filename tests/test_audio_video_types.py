import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.drama_client import DramaUpstreamError, audio_video_metadata
from media_samples import PNG
from test_media_download import download_client, response


@pytest.fixture(scope="module")
def av_samples(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("real codec tests require ffmpeg and ffprobe")
    folder = tmp_path_factory.mktemp("encoded-media")
    samples = {}
    for extension, codec in [("mp3", "libmp3lame"), ("wav", "pcm_s16le"), ("m4a", "aac"),
                             ("aac", "aac"), ("flac", "flac"), ("ogg", "libvorbis"), ("webm", "libopus")]:
        path = folder / ("audio." + extension)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
                        "-c:a", codec, "-threads", "1", "-y", str(path)], check=True, capture_output=True, timeout=30)
        samples[extension] = path.read_bytes()
    for extension, codec in [("mp4", "mpeg4"), ("mov", "mpeg4"), ("webm", "libvpx")]:
        path = folder / ("video." + extension)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=16x16:duration=0.3",
                        "-c:v", codec, "-threads", "1", "-an", "-y", str(path)], check=True, capture_output=True, timeout=30)
        samples["video_" + extension] = path.read_bytes()
    cover = folder / "cover.png"
    cover.write_bytes(PNG)
    tagged = folder / "cover.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(folder / "audio.mp3"), "-i", str(cover),
                    "-map", "0:a", "-map", "1:v", "-c", "copy", "-disposition:v", "attached_pic",
                    "-y", str(tagged)], check=True, capture_output=True, timeout=30)
    samples["mp3_cover"] = tagged.read_bytes()
    return samples


@pytest.mark.parametrize("declared", ["application/octet-stream", "", "text/plain", "video/mp4"])
@pytest.mark.parametrize("key,mime,filename", [
    ("mp3", "audio/mpeg", "audio-1.mp3"), ("wav", "audio/wav", "audio-1.wav"),
    ("m4a", "audio/mp4", "audio-1.m4a"), ("aac", "audio/aac", "audio-1.aac"),
    ("flac", "audio/flac", "audio-1.flac"), ("ogg", "audio/ogg", "audio-1.ogg"),
    ("webm", "audio/webm", "audio-1.webm"), ("mp3_cover", "audio/mpeg", "audio-1.mp3")])
def test_real_audio_with_incorrect_headers_uploads_original_bytes(settings, monkeypatch, av_samples, declared, key, mime, filename):
    item, direct = download_client(settings, monkeypatch)
    data = av_samples[key]
    downloaded = response((data,))
    downloaded.headers["Content-Type"] = declared
    direct.get.return_value = downloaded
    item._request = Mock(return_value={"upload_url": "https://upload.example/object", "public_url": "https://media.example/object"})
    upload = item.upload_media("https://media.example/download?id=private", "audio", "audio-1")
    assert (upload.content_type, upload.name, upload.width, upload.height) == (mime, filename, 0, 0)
    assert 100 < upload.duration_ms < 1500
    assert item._request.call_args.kwargs["json"] == {"filename": filename, "content_type": mime, "size_bytes": len(data)}
    assert item.session.put.call_args.kwargs["headers"] == {"Content-Type": mime}
    assert item.session.put.call_args.kwargs["data"] == data
    assert upload.source_content_type == declared
    item.session.get.assert_not_called()


@pytest.mark.parametrize("key,mime,filename", [
    ("video_mp4", "video/mp4", "clip.mp4"), ("video_mov", "video/quicktime", "clip.mov"),
    ("video_webm", "video/webm", "clip.webm")])
def test_actual_video_container_corrects_generic_mime_and_wrong_suffix(av_samples, key, mime, filename):
    actual_mime, actual_name, duration, width, height = audio_video_metadata(av_samples[key], "video", "application/octet-stream", "clip.mp3")
    assert (actual_mime, actual_name, width, height) == (mime, filename, 16, 16)
    assert 100 < duration < 1500


@pytest.mark.parametrize("key,kind", [("mp3", "video"), ("mp3_cover", "video"), ("video_mp4", "audio")])
def test_wrong_media_kind_is_not_relabelled(av_samples, key, kind):
    with pytest.raises(DramaUpstreamError) as error:
        audio_video_metadata(av_samples[key], kind, kind + "/mp4", "reference")
    assert error.value.code == "MEDIA_FORMAT_UNSUPPORTED"


@pytest.mark.parametrize("body", [b"<html>Access denied</html>", b'{"error":"expired"}', PNG, b"ID3truncated"])
def test_error_pages_images_and_truncated_audio_never_reach_signing(settings, monkeypatch, av_samples, body):
    item, direct = download_client(settings, monkeypatch)
    direct.get.return_value = response((body,))
    item._request = Mock()
    with pytest.raises(DramaUpstreamError) as error:
        item.upload_media("https://media.example/file.mp3?secret=private", "audio", "audio-1")
    assert error.value.code == "MEDIA_FORMAT_UNSUPPORTED"
    assert "secret" not in str(error.value.details)
    item._request.assert_not_called()
    item.session.put.assert_not_called()


def probe_result(duration="5.12", streams=None):
    return {"format": {"format_name": "mp3", "duration": duration},
            "streams": [{"codec_type": "audio"}] if streams is None else streams}


@pytest.mark.parametrize("duration", ["0", "-1", "nan", "inf", "0.00001"])
def test_invalid_probed_duration_is_classified(monkeypatch, duration):
    monkeypatch.setattr("app.drama_client.shutil.which", lambda _: "ffprobe")
    monkeypatch.setattr("app.drama_client.subprocess.run", Mock(return_value=Mock(stdout=json.dumps(probe_result(duration)))))
    with pytest.raises(DramaUpstreamError) as error:
        audio_video_metadata(b"media", "audio", "application/octet-stream", "audio-1")
    assert error.value.code == "MEDIA_DURATION_UNSUPPORTED"


def test_probe_uses_bytes_not_display_name_or_remote_url(monkeypatch):
    monkeypatch.setattr("app.drama_client.shutil.which", lambda _: "ffprobe")
    def inspect(args, **kwargs):
        assert args[args.index("-protocol_whitelist") + 1] == "file,pipe"
        assert "-format_whitelist" in args
        assert Path(args[-1]).read_bytes() == b"unchanged media"
        assert Path(args[-1]).suffix == ".bin"
        return Mock(stdout=json.dumps(probe_result()))
    monkeypatch.setattr("app.drama_client.subprocess.run", inspect)
    result = audio_video_metadata(b"unchanged media", "audio", "application/octet-stream", "audio-1")
    assert result == ("audio/mpeg", "audio-1.mp3", 5120, 0, 0)


def test_missing_probe_is_distinct_from_bad_user_media(monkeypatch):
    monkeypatch.setattr("app.drama_client.shutil.which", lambda _: None)
    with pytest.raises(DramaUpstreamError) as error:
        audio_video_metadata(b"data", "audio", "audio/mpeg", "clip.mp3")
    assert error.value.code == "MEDIA_PROBE_UNAVAILABLE"


def test_video_with_audio_is_not_silently_turned_into_audio_reference(monkeypatch):
    monkeypatch.setattr("app.drama_client.shutil.which", lambda _: "ffprobe")
    metadata = probe_result(streams=[{"codec_type": "audio"}, {"codec_type": "video", "width": 1280, "height": 720}])
    metadata["format"]["format_name"] = "mov,mp4,m4a,3gp,3g2,mj2"
    monkeypatch.setattr("app.drama_client.subprocess.run", Mock(return_value=Mock(stdout=json.dumps(metadata))))
    with pytest.raises(DramaUpstreamError) as error:
        audio_video_metadata(b"media", "audio", "audio/mp4", "sound.m4a")
    assert error.value.code == "MEDIA_FORMAT_UNSUPPORTED"
