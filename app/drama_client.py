"""Observed Firebase -> hosted project -> quoted video job protocol."""
from __future__ import annotations

import base64
import io
import json
import logging
import math
import mimetypes
import re
import shutil
import subprocess
import tempfile
import time
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import requests
from PIL import Image

from app.config import rewrite_loopback_proxy
from app.model_catalog import CREDIT_RATES, model_spec


LOGGER = logging.getLogger("dra2api.drama_client")


class DramaUpstreamError(RuntimeError):
    def __init__(self, message: str, *, code: str = "DRAMA_UPSTREAM_ERROR",
                 status_code: int = 502, details: Any = None):
        super().__init__(message)
        self.code, self.status_code, self.details = code, status_code, details


class DramaAuthError(DramaUpstreamError):
    def __init__(self, message: str = "Drama.Land 登录已失效"):
        super().__init__(message, code="DRAMA_AUTH_REQUIRED", status_code=401)


class DramaRiskBlocked(DramaUpstreamError):
    def __init__(self, message: str = "Drama.Land 要求完成浏览器验证"):
        super().__init__(message, code="DRAMA_RISK_BLOCKED", status_code=403)


class DramaAccountSuspended(DramaUpstreamError):
    def __init__(self, message: str = "Drama.Land 账号不可用"):
        super().__init__(message, code="DRAMA_ACCOUNT_SUSPENDED", status_code=403)


def is_rate_limit_message(value: Any) -> bool:
    return bool(re.search(r"rate.?limit|too many requests|quota exceeded", str(value), re.I))


def rate_limit_retry_after(message: str, header: Any = None) -> float | None:
    try:
        return max(float(header), 0) if header is not None else None
    except (ValueError, TypeError):
        return None


class DramaRateLimited(DramaUpstreamError):
    def __init__(self, message: str, *, retry_after: Any = None, details: Any = None):
        super().__init__(message, code="RATE_LIMITED", status_code=429, details=details)
        self.retry_after = rate_limit_retry_after(message, retry_after)


@dataclass(slots=True)
class MediaUpload:
    profile_id: str
    url: str
    kind: str
    name: str
    content_type: str
    size: int
    duration_ms: int = 0
    width: int = 0
    height: int = 0
    source_content_type: str = ""

    def audit_view(self) -> dict[str, Any]:
        return asdict(self)


def _message(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("message", "detail", "error", "msg"):
            if value.get(key):
                return _message(value[key])
    return str(value)[:500]


def firestore_value(value: dict[str, Any]) -> Any:
    if "mapValue" in value:
        return {key: firestore_value(item) for key, item in value["mapValue"].get("fields", {}).items()}
    if "arrayValue" in value:
        return [firestore_value(item) for item in value["arrayValue"].get("values", [])]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    return next(iter(value.values()), None)


def firestore_document(document: dict[str, Any]) -> dict[str, Any]:
    result = {key: firestore_value(value) for key, value in document.get("fields", {}).items()}
    result.setdefault("job_ref", document.get("name", "").rsplit("/", 1)[-1])
    return result


def result_urls(detail: dict[str, Any]) -> list[str]:
    result = detail.get("result") or {}
    values = list((detail.get("agent_body") or {}).get("assets") or [])
    if isinstance(result, dict):
        values.insert(0, result.get("url"))
    elif isinstance(result, list):
        values.extend(item.get("url") if isinstance(item, dict) else item for item in result)
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value.startswith("https://")))


def failure_reason(detail: dict[str, Any]) -> str:
    return _message(detail.get("error") or detail.get("error_message") or "Drama.Land 视频生成失败")


def image_metadata(data: bytes, declared_type: str, name: str) -> tuple[str, str, int, int]:
    """Identify the downloaded image without converting or recompressing it."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                content_type = Image.MIME.get(image.format, "")
                width, height = image.size
                image.verify()
        if not content_type.startswith("image/"):
            raise ValueError("unrecognized image format")
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise DramaUpstreamError("图片像素尺寸超过支持范围", code="MEDIA_LIMIT_EXCEEDED",
                                 details={"declared_content_type": declared_type, "error_type": type(exc).__name__}) from exc
    except (OSError, SyntaxError, ValueError, EOFError) as exc:
        raise DramaUpstreamError("图片内容无法识别或已损坏", code="MEDIA_FORMAT_UNSUPPORTED",
                                 details={"declared_content_type": declared_type, "error_type": type(exc).__name__}) from exc
    if mimetypes.guess_type(name)[0] != content_type:
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(content_type)
        extension = extension or mimetypes.guess_extension(content_type)
        if extension:
            name = (Path(name).stem if name else "image") + extension
    return content_type, name, width, height


def audio_video_metadata(data: bytes, kind: str, declared_type: str, name: str) -> tuple[str, str, int, int, int]:
    """Probe actual streams before considering HTTP headers or display names."""
    executable = shutil.which("ffprobe")
    if not executable:
        raise DramaUpstreamError("音视频检测工具不可用", code="MEDIA_PROBE_UNAVAILABLE")
    supported_formats = "mp3,wav,flac,aac,ogg,amr,aiff,mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpeg,mpegts,flv,asf"
    diagnostic: dict[str, Any] = {"declared_content_type": declared_type, "requested_kind": kind}
    with tempfile.TemporaryDirectory(prefix="dra-media-") as directory:
        path = Path(directory) / "reference.bin"
        path.write_bytes(data)
        try:
            result = subprocess.run(
                [executable, "-v", "error", "-protocol_whitelist", "file,pipe",
                 "-format_whitelist", supported_formats, "-show_format", "-show_streams", "-of", "json", str(path)],
                capture_output=True, timeout=30, check=True)
            metadata = json.loads(result.stdout)
            container = metadata.get("format") or {}
            formats = set(str(container.get("format_name") or "").split(","))
            streams = metadata.get("streams") or []
            # MP3/M4A album art is an attached picture, not a video reference.
            videos = [item for item in streams if item.get("codec_type") == "video"
                      and not (item.get("disposition") or {}).get("attached_pic")]
            audio = [item for item in streams if item.get("codec_type") == "audio"]
            diagnostic.update(detected_format=container.get("format_name"),
                              stream_types=sorted({str(item.get("codec_type")) for item in streams}))
            selected = audio if kind == "audio" else videos
            if not selected or (kind == "audio" and videos):
                raise ValueError("requested reference kind does not match actual streams")
            duration = float(container.get("duration", 0))
            if not math.isfinite(duration) or duration <= 0 or round(duration * 1000) <= 0:
                raise DramaUpstreamError("素材时长无法识别", code="MEDIA_DURATION_UNSUPPORTED", details=diagnostic)
            width, height = (int(selected[0].get("width", 0)), int(selected[0].get("height", 0))) if kind == "video" else (0, 0)
            if kind == "video" and (width <= 0 or height <= 0):
                raise ValueError("invalid video dimensions")
            if "mov" in formats or "mp4" in formats:
                quicktime = str((container.get("tags") or {}).get("major_brand", "")).strip() == "qt"
                mime, extension = (("audio/mp4", ".m4a") if kind == "audio" else
                                   ("video/quicktime", ".mov") if quicktime else ("video/mp4", ".mp4"))
            elif "matroska" in formats or "webm" in formats:
                webm = b"\x42\x82\x84webm" in data[:4096]
                mime = kind + ("/webm" if webm else "/x-matroska")
                extension = ".webm" if webm else ".mka" if kind == "audio" else ".mkv"
            elif "ogg" in formats:
                mime, extension = ("audio/ogg", ".ogg") if kind == "audio" else ("video/ogg", ".ogv")
            elif "asf" in formats:
                mime, extension = ("audio/x-ms-wma", ".wma") if kind == "audio" else ("video/x-ms-asf", ".asf")
            else:
                types = {"mp3": ("audio/mpeg", ".mp3"), "wav": ("audio/wav", ".wav"),
                         "flac": ("audio/flac", ".flac"), "aac": ("audio/aac", ".aac"),
                         "amr": ("audio/amr", ".amr"), "aiff": ("audio/aiff", ".aiff"),
                         "avi": ("video/x-msvideo", ".avi"), "mpeg": ("video/mpeg", ".mpg"),
                         "mpegts": ("video/mp2t", ".ts"), "flv": ("video/x-flv", ".flv")}
                mime, extension = next((types[value] for value in sorted(formats) if value in types), ("", ""))
            if not mime.startswith(kind + "/"):
                raise ValueError("unsupported media container")
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError) as exc:
            diagnostic["error_type"] = type(exc).__name__
            raise DramaUpstreamError("素材内容无法识别、已损坏或类型不匹配", code="MEDIA_FORMAT_UNSUPPORTED",
                                     details=diagnostic) from exc
    if mimetypes.guess_type(name)[0] != mime:
        name = (Path(name).stem if name else kind) + extension
    return mime, name, round(duration * 1000), width, height


def validate_approval(approval: dict[str, Any], payload: dict[str, Any]) -> float:
    """Approve only one requested video expense, with matching parameters."""
    quotation = approval.get("quote") or {}
    items = quotation.get("items") or []
    if approval.get("kind", quotation.get("kind")) != "credit_quote" or len(items) != 1:
        raise DramaUpstreamError("上游请求了预期之外的计费操作", code="APPROVAL_MISMATCH")
    item = items[0]
    details = item.get("details") or {}
    if (item.get("action") != "generate_video"
            or details.get("service") != payload["upstream_model"]
            or str(details.get("resolution", "")).lower() != payload["resolution"].lower()
            or float(details.get("duration_seconds", 0)) != payload["duration"]):
        raise DramaUpstreamError("上游报价的模型、时长或分辨率与请求不一致", code="APPROVAL_MISMATCH")
    credits = float(quotation.get("totalCredits") or 0)
    if not math.isfinite(credits) or credits <= 0 or credits != float(item.get("credits") or 0):
        raise DramaUpstreamError("上游报价无效", code="INVALID_QUOTE")
    if payload.get("max_credits") is not None and credits > float(payload["max_credits"]):
        raise DramaUpstreamError("上游报价超过 max_credits", code="CREDIT_LIMIT_EXCEEDED", status_code=409)
    preview = ((quotation.get("raw") or {}).get("approvalPreview") or {})
    for section in preview.get("sections") or []:
        for pair in section.get("items") or []:
            if str(pair.get("label", "")).lower() == "aspect ratio" and pair.get("value") != payload["aspect_ratio"]:
                raise DramaUpstreamError("上游报价画面比例与请求不一致", code="APPROVAL_MISMATCH")
    return credits


class DramaClient:
    def __init__(self, account: dict[str, Any], settings: Any,
                 on_auth_update: Callable[[dict[str, Any]], Any] | None = None):
        self.account, self.settings = dict(account), settings
        self.on_auth_update = on_auth_update
        self.session = requests.Session()
        self.session.trust_env = False
        proxy = rewrite_loopback_proxy(str(account.get("proxy_url") or ""), settings.proxy_host_override)
        proxy = proxy.replace("socks5://", "socks5h://", 1)
        if proxy:
            self.session.proxies.update(http=proxy, https=proxy)
        self.session.headers["User-Agent"] = str(account.get("user_agent") or "Mozilla/5.0 Chrome/153.0.0.0 Safari/537.36")
        self.base = settings.upstream_base_url.rstrip("/")
        self.site = settings.site_base_url.rstrip("/")
        self.firebase_key = account.get("firebase_api_key") or settings.firebase_api_key

    def _request(self, method: str, url: str, *, auth: bool = True,
                 project_id: str = "", read_only: bool = False, **kwargs: Any) -> Any:
        if auth:
            self.ensure_auth()
        can_retry = method == "GET" or read_only
        attempts = 1 + (int(self.settings.request_retries) if can_retry else 0)
        for attempt in range(attempts):
            headers = {"Accept": "application/json", "Origin": self.site, "Referer": self.site + "/zh-cn/create"}
            if auth:
                headers["Authorization"] = "Bearer " + self.account["access_token"]
            if project_id:
                headers["X-Project-Id"] = project_id
            try:
                response = self.session.request(method, url, headers=headers,
                                                timeout=self.settings.request_timeout_seconds, **kwargs)
            except requests.RequestException as exc:
                if attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise DramaUpstreamError("网络请求未完成；写入操作不会自动重发",
                                         code="NETWORK_ERROR" if can_retry else "SUBMISSION_UNCERTAIN") from exc
            if response.status_code == 401 and auth:
                self.login(refresh_only=True)
                headers["Authorization"] = "Bearer " + self.account["access_token"]
                try:
                    response = self.session.request(method, url, headers=headers,
                                                    timeout=self.settings.request_timeout_seconds, **kwargs)
                except requests.RequestException as exc:
                    raise DramaUpstreamError("刷新登录后请求未完成",
                                             code="NETWORK_ERROR" if can_retry else "SUBMISSION_UNCERTAIN") from exc
            try:
                body = response.json()
            except ValueError:
                body = {"message": "上游未返回 JSON"}
            message = _message(body)
            if response.status_code == 429:
                raise DramaRateLimited(message, retry_after=response.headers.get("Retry-After"))
            if response.status_code == 401:
                raise DramaAuthError(message)
            if response.status_code == 403:
                if "USER_DISABLED" in message:
                    raise DramaAccountSuspended(message)
                raise DramaRiskBlocked(message)
            if response.status_code == 402:
                raise DramaUpstreamError(message, code="INSUFFICIENT_CREDITS", status_code=402)
            if not response.ok:
                if response.status_code >= 500 and attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise DramaUpstreamError(message, code="DRAMA_HTTP_ERROR", status_code=response.status_code)
            if isinstance(body, dict) and (body.get("success") is False or body.get("ok") is False or body.get("code", 200) not in (0, 200, "200")):
                raise DramaUpstreamError(message, code=str(body.get("code") or "DRAMA_BUSINESS_ERROR"))
            return body
        raise DramaUpstreamError("上游请求失败")

    def ensure_auth(self) -> None:
        token = self.account.get("access_token")
        expires = float(self.account.get("access_token_expires_at") or 0)
        if token and not expires:
            try:
                data = str(token).split(".")[1]
                expires = float(json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))["exp"])
            except (ValueError, KeyError, IndexError):
                pass
        if not token or (expires and expires < time.time() + 60):
            self.login()

    def login(self, *, refresh_only: bool = False) -> dict[str, Any]:
        refresh = self.account.get("refresh_token")
        if refresh:
            try:
                body = self._request("POST", "https://securetoken.googleapis.com/v1/token?key=" + quote(self.firebase_key),
                                     auth=False, data={"grant_type": "refresh_token", "refresh_token": refresh})
            except DramaUpstreamError as exc:
                if exc.status_code not in (400, 401):
                    raise
                if refresh_only or not self.account.get("password"):
                    raise DramaAuthError("Firebase refresh token 已失效，请重新登录") from exc
                refresh = None
        if not refresh:
            if not self.account.get("email") or not self.account.get("password"):
                raise DramaAuthError("请导入 Firebase 会话或填写邮箱和密码")
            try:
                body = self._request("POST", "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=" + quote(self.firebase_key),
                                     auth=False, json={"email": self.account["email"], "password": self.account["password"],
                                                       "returnSecureToken": True, "clientType": "CLIENT_TYPE_WEB"})
            except DramaUpstreamError as exc:
                if "USER_DISABLED" in str(exc):
                    raise DramaAccountSuspended() from exc
                if "CAPTCHA" in str(exc) or "MFA" in str(exc):
                    raise DramaRiskBlocked("Firebase 要求浏览器登录或额外验证") from exc
                if exc.status_code == 400:
                    raise DramaAuthError("Firebase 拒绝了邮箱或密码") from exc
                raise
        updates = {"access_token": body.get("id_token") or body.get("idToken") or "",
                   "refresh_token": body.get("refresh_token") or body.get("refreshToken") or refresh or "",
                   "access_token_expires_at": int(time.time()) + int(body.get("expires_in") or body.get("expiresIn") or 3600),
                   "user_id": body.get("user_id") or body.get("localId") or self.account.get("user_id", ""),
                   "firebase_api_key": self.firebase_key, "last_login_at": int(time.time())}
        if not updates["access_token"]:
            raise DramaAuthError("Firebase 未返回 ID token")
        self.account.update(updates)
        if self.on_auth_update:
            self.on_auth_update(updates)
        return updates

    def account_state(self) -> dict[str, Any]:
        user = self._request("GET", self.base + "/api/v1/user/get_user_info")["data"]
        if self.account.get("email") and str(user.get("email", "")).casefold() != self.account["email"].casefold():
            raise DramaAuthError("登录会话与账号邮箱不一致")
        if self.account.get("user_id") and user.get("uid") != self.account["user_id"]:
            raise DramaAuthError("登录会话与账号 UID 不一致")
        self.account["user_id"] = user.get("uid")
        summary = self._request("GET", self.base + "/api/v1/user/credits/summary")["data"]
        return {"email": user.get("email"), "user_id": user.get("uid"), "token": self.account.get("access_token"),
                "available_balance": summary.get("credits", user.get("points", 0)), "plan": user.get("tier", ""),
                "buckets": {**summary, "features": user.get("features", {}), "subscription_status": user.get("subscription_status")}}

    def reward_tasks(self) -> list[dict[str, Any]]:
        result = self._request("GET", self.base + "/api/v1/task/list")
        data = result.get("data") if isinstance(result, dict) else None
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(tasks, list) or not all(isinstance(item, dict) and item.get("id") for item in tasks):
            raise DramaUpstreamError("上游奖励任务列表格式异常", code="INVALID_REWARD_TASKS")
        return tasks

    def claim_reward(self, task_id: str) -> dict[str, Any]:
        try:
            result = self._request("POST", self.base + "/api/v1/task/claim", json={"task_id": task_id})
        except DramaUpstreamError as exc:
            if exc.status_code == 400 and str(exc).strip().casefold() == "reward already claimed":
                return {"task_id": task_id, "status": "already_claimed", "credits": 0}
            raise
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and data.get("success") is False:
            raise DramaUpstreamError(_message(data), code="REWARD_CLAIM_REJECTED", status_code=400)
        amount = data.get("reward_amount") if isinstance(data, dict) else None
        if (not isinstance(data, dict) or data.get("success") is not True
                or data.get("task_id") != task_id or data.get("reward_type") != "credits"
                or type(amount) not in (int, float) or not math.isfinite(amount) or amount <= 0):
            raise DramaUpstreamError("领取响应无法确认，请刷新奖励状态", code="REWARD_CLAIM_UNCERTAIN")
        return {"task_id": task_id, "status": "claimed", "credits": amount}

    def daily_checkin_status(self) -> dict[str, Any]:
        tasks = self.reward_tasks()
        task = next((item for item in tasks if item.get("id") == "daily_login"), {})
        signed = task.get("status") == "claimed" or bool(task.get("claimed_at"))
        return {"today_signed": signed, "can_checkedin": bool(task.get("reward_eligible")) and not signed,
                "credits": (task.get("reward") or {}).get("amount", 0)}

    def daily_checkin(self) -> dict[str, Any]:
        self._request("POST", self.base + "/api/v1/task/daily-check", json={})
        result = self._request("POST", self.base + "/api/v1/task/claim", json={"task_id": "daily_login"})
        return {"credits": (result.get("data") or {}).get("reward_amount", 0), "today_signed": True}

    def _download_media(self, source: str) -> tuple[bytes, str]:
        attempts = []
        with requests.Session() as direct:
            direct.trust_env = False
            direct.headers["User-Agent"] = self.session.headers["User-Agent"]
            routes = [("direct", direct)]
            if self.session.proxies:
                routes.append(("account_proxy", self.session))
            for route, session in routes:
                try:
                    with session.get(source, stream=True, timeout=self.settings.media_timeout_seconds) as response:
                        response.raise_for_status()
                        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                        chunks, size = [], 0
                        for chunk in response.iter_content(65536):
                            size += len(chunk)
                            if size > self.settings.media_max_bytes:
                                raise ValueError("media exceeds configured size limit")
                            chunks.append(chunk)
                        return b"".join(chunks), content_type
                except requests.RequestException as exc:
                    attempt = {"route": route, "source_host": urlsplit(source).hostname,
                               "error_type": type(exc).__name__,
                               "status_code": exc.response.status_code if exc.response is not None else None}
                    attempts.append(attempt)
                    LOGGER.warning("Media download failed: route=%s host=%s error=%s status=%s",
                                   route, attempt["source_host"], attempt["error_type"], attempt["status_code"])
                    if route == routes[-1][0]:
                        raise DramaUpstreamError("素材下载失败", code="MEDIA_DOWNLOAD_FAILED",
                                                 details={"attempts": attempts}) from exc
        raise AssertionError("media download has no route")

    def upload_media(self, source: str, kind: str, name: str = "") -> MediaUpload:
        content_type = ""
        if source.startswith("data:"):
            header, encoded = source.split(",", 1)
            if ";base64" not in header:
                raise ValueError("media data URL must be base64 encoded")
            content_type = header[5:].split(";", 1)[0]
            data = base64.b64decode(encoded, validate=True)
        elif urlsplit(source).scheme in {"https", "http"}:
            data, content_type = self._download_media(source)
            name = name or urlsplit(source).path.rsplit("/", 1)[-1]
        else:
            raise ValueError("media must be an HTTP(S) URL or base64 data URL")
        if not data or len(data) > self.settings.media_max_bytes:
            raise ValueError("media is empty or exceeds configured size limit")
        source_content_type = content_type.strip().lower()
        content_type = source_content_type
        duration_ms, width, height = 0, 0, 0
        if kind == "image":
            content_type, name, width, height = image_metadata(data, content_type, name)
        elif kind in {"audio", "video"}:
            content_type, name, duration_ms, width, height = audio_video_metadata(data, kind, content_type, name)
        elif not content_type or content_type == "application/octet-stream":
            content_type = mimetypes.guess_type(name)[0] or ""
        if not content_type.startswith(kind + "/"):
            raise ValueError(f"{kind} reference has incompatible content type")
        name = name or kind + (mimetypes.guess_extension(content_type) or ".bin")
        signed = self._request("POST", self.base + "/api/v1/upload-url",
                               json={"filename": name, "content_type": content_type, "size_bytes": len(data)})
        self._upload_bytes(signed["upload_url"], data, content_type)
        return MediaUpload("", signed["public_url"], kind, name, content_type, len(data), duration_ms, width, height,
                           source_content_type=source_content_type)

    def _upload_bytes(self, url: str, data: bytes, content_type: str) -> None:
        # A signed object PUT can safely resend identical bytes, even when its
        # response was lost. Never repeat the signing POST or video submission.
        attempts = []
        retries = max(0, min(int(self.settings.request_retries), 5))
        for index in range(retries + 1):
            response = None
            try:
                response = self.session.put(url, data=data, headers={"Content-Type": content_type},
                                            timeout=self.settings.media_timeout_seconds)
                response.raise_for_status()
                return
            except requests.RequestException as exc:
                status = response.status_code if response is not None else None
                retryable = (status in {408, 425, 429} or (status is not None and status >= 500)
                             or (status is None and isinstance(exc, (requests.Timeout, requests.ConnectionError))))
                attempts.append({"attempt": index + 1, "error_type": type(exc).__name__,
                                 "status_code": status, "retryable": retryable})
                if not retryable or index >= retries:
                    raise DramaUpstreamError("素材上传失败", code="MEDIA_UPLOAD_FAILED",
                                             details={"attempts": attempts}) from exc
                retry_after = rate_limit_retry_after("", response.headers.get("Retry-After")) if response is not None else None
                delay = min(30, max(2 ** index, retry_after or 0))
                LOGGER.warning("Media object PUT failed (attempt=%s status=%s error=%s); retrying",
                               index + 1, status, type(exc).__name__)
            finally:
                if response is not None:
                    response.close()
            time.sleep(delay)

    def build_generation_request(self, payload: dict[str, Any], uploads: list[MediaUpload]) -> dict[str, Any]:
        spec = model_spec(payload["model"])
        for kind, maximum in (("audio", spec.max_audio_seconds), ("video", spec.max_video_seconds)):
            if sum(item.duration_ms for item in uploads if item.kind == kind) > maximum * 1000:
                raise ValueError(f"total {kind} reference duration exceeds {maximum} seconds")
        if (spec.max_audio_video_seconds is not None
                and sum(item.duration_ms for item in uploads if item.kind in {"audio", "video"})
                > spec.max_audio_video_seconds * 1000):
            raise ValueError(f"combined audio/video reference duration exceeds {spec.max_audio_video_seconds} seconds")
        fast = {"kind": "video", "service": payload["upstream_model"], "aspect_ratio": payload["aspect_ratio"],
                "resolution": payload["resolution"], "duration": payload["duration"]}
        references = [{"kind": "uploaded_file", "url": item.url, "media_type": item.kind,
                       "mime_type": item.content_type, "filename": item.name} for item in uploads]
        instructions = "\n\n生成要求：只创建一个视频任务，保持上述提示词含义及所有参考素材；严格使用以下参数，不切换模型、时长、分辨率或比例。"
        instructions += json.dumps({**fast, "generate_audio": payload.get("generate_audio", True)}, ensure_ascii=False)
        if references:
            instructions += "\n多图片、视频、音频分别使用 image_urls、video_urls、reference_audio_urls 数组，CLI 分别使用 --image-urls、--video-urls、--reference-audio-urls；不要重复传递单数参数，否则只保留最后一份素材。"
        instructions += "\n仅使用本项目真实素材进行一次生成，不执行示例或测试生成，不使用 example.com 等占位素材，不使用 --echo-parsed；完成提交后只查询该任务。"
        instructions += "\n将完整提示词保存为 UTF-8 文件，使用 --prompt=@file:/workspace/prompt.txt，避免对白引号或换行破坏 shell 转义；仅在命令尚未执行的解析错误时修复命令，不重复提交已有视频任务。"
        if payload.get("negative_prompt"):
            instructions += "\n避免内容：" + payload["negative_prompt"]
        return {"name": payload["prompt"][:80], "project_type": "video", "initial_intent": payload["prompt"] + instructions,
                "initial_reference_list": references, "agent_profile": "fast", "fast_generation": fast,
                "user_language": str(payload.get("language") or "zh-cn")}

    def create_project(self, request: dict[str, Any]) -> str:
        body = self._request("POST", self.base + "/api/v1/hosted/create", json=request)
        project_id = body.get("project_id") or (body.get("data") or {}).get("project_id")
        if not project_id:
            raise DramaUpstreamError("创建项目未返回 project_id")
        return str(project_id)

    def start_generation(self, project_id: str, language: str = "zh-cn") -> dict[str, Any]:
        return self._request("POST", self.site + "/api/pi/prompt", project_id=project_id,
                             json={"text": "", "language": language})

    def project_history(self, project_id: str) -> dict[str, Any]:
        return self._request("GET", self.site + "/api/pi/history", project_id=project_id)

    def continue_generation(self, project_id: str, language: str = "zh-cn") -> dict[str, Any]:
        return self._request("POST", self.site + "/api/pi/prompt", project_id=project_id,
                             json={"text": "确认提交。保持原模型、时长、分辨率、比例、提示词及全部参考素材，仅修复命令转义；将完整提示词写入 UTF-8 文件并使用 --prompt=@file:/workspace/prompt.txt。若已有视频任务则只查询，禁止重复生成。",
                                   "language": language})

    def approve(self, project_id: str, approval_id: str, decision: str = "approved") -> dict[str, Any]:
        return self._request("POST", self.site + "/api/pi/tool-approvals/" + quote(approval_id, safe="") + "/respond",
                             project_id=project_id, json={"decision": decision})

    def project_jobs(self, project_id: str) -> list[dict[str, Any]]:
        query = {"structuredQuery": {"from": [{"collectionId": "async_tool_jobs"}],
                  "where": {"fieldFilter": {"field": {"fieldPath": "project_id"}, "op": "EQUAL",
                                            "value": {"stringValue": project_id}}}, "limit": 200}}
        url = "https://firestore.googleapis.com/v1/projects/" + quote(self.settings.firestore_project, safe="") + "/databases/(default)/documents:runQuery"
        rows = self._request("POST", url, auth=False, read_only=True, json=query)
        jobs = [firestore_document(row["document"]) for row in rows if "document" in row]
        uid = self.account.get("user_id")
        for job in jobs:
            owner = job.get("user_id") or job.get("user") or (job.get("billing_charge_claim") or {}).get("user_id")
            if job.get("project_id") != project_id or not uid or owner != uid:
                raise DramaAuthError("任务不属于当前账号或项目")
        return jobs

    def generation_detail(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        jobs = self.project_jobs(project_id)
        # Seedance 2.5 initially stores only the submit trace fields. Recognize
        # that record before the callback adds type/task_id/result metadata.
        video_jobs = [job for job in jobs if job.get("type") == "video"
                      or job.get("task_action") == "generate_video"
                      or job.get("task_subcommand") == "generate-video"
                      or str(job.get("task_id", "")).startswith("ag:video:")]
        if payload:
            for job in video_jobs:
                if (job.get("service") or job.get("task_service")) != payload["upstream_model"]:
                    raise DramaUpstreamError("任务模型与请求不一致", code="UPSTREAM_MODEL_MISMATCH")
        if len(video_jobs) > 1:
            raise DramaUpstreamError("单次请求产生了多个视频任务", code="MULTIPLE_UPSTREAM_JOBS")
        job = video_jobs[0] if video_jobs else {}
        raw_status = str(job.get("status") or "pending").lower()
        state = "COMPLETE" if raw_status in {"completed", "succeeded"} else "FAILED" if raw_status in {"failed", "error"} else "PROCESSING"
        status = self._request("GET", self.site + "/api/pi/status", project_id=project_id) if state == "PROCESSING" else {}
        return {**job, "status": state, "vendor_status": raw_status,
                "pendingApprovals": status.get("pendingApprovals") or [], "isStreaming": bool(status.get("isStreaming")),
                "project_id": project_id, "job_ref": job.get("job_ref", ""),
                "actual_cost": (job.get("billing") or {}).get("credits"),
                "progress": 0.7 if job else 0.1}

    def estimate_cost(self, payload: dict[str, Any]) -> float:
        return float(CREDIT_RATES.get(payload["upstream_model"], {}).get(payload["resolution"], 0) * payload["duration"])
