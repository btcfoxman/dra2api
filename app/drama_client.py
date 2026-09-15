"""Observed Firebase -> hosted project -> quoted video job protocol."""
from __future__ import annotations

import base64
import json
import math
import mimetypes
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import requests

from app.config import rewrite_loopback_proxy
from app.model_catalog import CREDIT_RATES, model_spec


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

    def daily_checkin_status(self) -> dict[str, Any]:
        tasks = self._request("GET", self.base + "/api/v1/task/list")["data"].get("tasks", [])
        task = next((item for item in tasks if item.get("id") == "daily_login"), {})
        signed = task.get("status") == "claimed" or bool(task.get("claimed_at"))
        return {"today_signed": signed, "can_checkedin": bool(task.get("reward_eligible")) and not signed,
                "credits": (task.get("reward") or {}).get("amount", 0)}

    def daily_checkin(self) -> dict[str, Any]:
        self._request("POST", self.base + "/api/v1/task/daily-check", json={})
        result = self._request("POST", self.base + "/api/v1/task/claim", json={"task_id": "daily_login"})
        return {"credits": (result.get("data") or {}).get("reward_amount", 0), "today_signed": True}

    def upload_media(self, source: str, kind: str, name: str = "") -> MediaUpload:
        content_type = ""
        if source.startswith("data:"):
            header, encoded = source.split(",", 1)
            if ";base64" not in header:
                raise ValueError("media data URL must be base64 encoded")
            content_type = header[5:].split(";", 1)[0]
            data = base64.b64decode(encoded, validate=True)
        elif urlsplit(source).scheme in {"https", "http"}:
            try:
                with self.session.get(source, stream=True, timeout=self.settings.media_timeout_seconds) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > self.settings.media_max_bytes:
                            raise ValueError("media exceeds configured size limit")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                name = name or urlsplit(source).path.rsplit("/", 1)[-1]
            except requests.RequestException as exc:
                raise DramaUpstreamError("素材下载失败", code="MEDIA_DOWNLOAD_FAILED") from exc
        else:
            raise ValueError("media must be an HTTP(S) URL or base64 data URL")
        if not data or len(data) > self.settings.media_max_bytes:
            raise ValueError("media is empty or exceeds configured size limit")
        if not content_type or content_type == "application/octet-stream":
            content_type = mimetypes.guess_type(name)[0] or ""
        if not content_type.startswith(kind + "/"):
            raise ValueError(f"{kind} reference has incompatible content type")
        name = name or kind + (mimetypes.guess_extension(content_type) or ".bin")
        duration_ms, width, height = 0, 0, 0
        if kind in {"video", "audio"}:
            executable = shutil.which("ffprobe")
            if not executable:
                raise ValueError("ffprobe is required for audio/video reference validation")
            with tempfile.TemporaryDirectory(prefix="dra-media-") as directory:
                path = Path(directory) / ("reference" + (mimetypes.guess_extension(content_type) or ".bin"))
                path.write_bytes(data)
                try:
                    result = subprocess.run([executable, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                                            capture_output=True, timeout=30, check=True)
                    metadata = json.loads(result.stdout)
                    duration = float(metadata.get("format", {}).get("duration", 0))
                    streams = [item for item in metadata.get("streams", []) if item.get("codec_type") == kind]
                    if not streams or not math.isfinite(duration) or duration <= 0:
                        raise ValueError("invalid media duration or stream type")
                    duration_ms = round(duration * 1000)
                    width, height = int(streams[0].get("width", 0)), int(streams[0].get("height", 0))
                except (subprocess.SubprocessError, ValueError, KeyError) as exc:
                    raise ValueError("reference file could not be decoded") from exc
        signed = self._request("POST", self.base + "/api/v1/upload-url",
                               json={"filename": name, "content_type": content_type, "size_bytes": len(data)})
        try:
            response = self.session.put(signed["upload_url"], data=data, headers={"Content-Type": content_type},
                                        timeout=self.settings.media_timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise DramaUpstreamError("素材上传失败", code="MEDIA_UPLOAD_FAILED") from exc
        return MediaUpload("", signed["public_url"], kind, name, content_type, len(data), duration_ms, width, height)

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
