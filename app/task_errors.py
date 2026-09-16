"""Shared task failure messages for the public API and admin console."""
from __future__ import annotations

import re
from typing import Any


def _is_media_size_failure(text: str) -> bool:
    subject = r"(?:files?|images?|videos?|audio|media|attachments?|uploads?)"
    if not re.search(rf"\b{subject}\b", text):
        return False
    if re.search(rf"\b{subject}(?:\s+files?)?\s+(?:(?:is|are|was|were)\s+)?too large\b", text):
        return True
    size = re.search(
        rf"\b{subject}\s+sizes?\b|\b\d+(?:\.\d+)?\s*(?:[kmgt]i?\s*b|(?:kilo|mega|giga|tera)?bytes?)\b",
        text,
    )
    violation = re.search(
        r"\bexceed(?:s|ed)?\b|\btoo large\b|\blarger than\b|\bmust (?:not exceed|be (?:less|smaller) than)\b"
        r"|\b(?:max(?:imum)?\s+(?:allowed\s+)?(?:file\s+)?size|size\s+limit)\b",
        text,
    )
    return bool(size and violation)


def _failure_details(task: dict[str, Any]) -> tuple[str, str]:
    raw_code = str(task.get("error_code") or "")
    code = raw_code.upper()
    message = str(task.get("error_message") or "")
    # Providers may put CamelCase error codes inside an otherwise generic message.
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw_code + " " + message)
    text = re.sub(r"[_-]+", " ", text).lower()
    billing = (task.get("raw_status") or {}).get("billing") or {}
    refunded = bool(task.get("refund_confirmed") or billing.get("refunded_at")
                    or billing.get("status") == "refunded")

    def retry(prefix: str) -> str:
        return prefix + ("，积分已返还~" if refunded else "~")

    scope = re.search(r"\b(input|output)\s+(video|audio|image|text)\s+sensitive\s+content\s+detected\b", text)
    output_moderation = bool(scope and scope.group(1) == "output")
    if not output_moderation and (code in {"REAL_PERSON_DETECTED", "REAL_PERSON_NOT_SUPPORTED", "INPUT_IMAGE_REAL_PERSON"}
                                  or re.search(r"real (?:person|people|human)|真人", text)):
        return "REAL_PERSON_DETECTED", retry("参考图片中检测到可能存在真人，暂不支持，请更换图片后重试")

    if (code in {"QUEUE_INTERRUPTED", "QUEUE_SERVICE_UNAVAILABLE", "POLLING_TIMEOUT"}
            or re.search(r"queue.{0,40}(?:interrupt|unavailable|shutdown)|\bpoll(?:ing)?\s+(?:timeout|timed?\s*out)\b|排队服务中断", text)):
        return "QUEUE_INTERRUPTED", "队列排队服务中断，请稍后再试~"
    if (code in {"RATE_LIMITED", "QUEUE_FULL", "QUEUE_LIMIT_EXCEEDED", "UPSTREAM_QUEUE_LIMIT",
                 "CONCURRENCY_LIMIT_EXCEEDED", "TOO_MANY_PENDING_TASKS"}
            or re.search(r"queue.{0,60}(?:full|limit|exceed|capacity)|concurren(?:t|cy).{0,60}(?:limit|exceed|maximum)"
                         r"|too many.{0,40}(?:pending|queued|concurrent)|maximum number of.{0,40}(?:tasks|jobs)"
                         r"|队列.{0,20}(?:限额|上限|已满)|排队限额", text)):
        return "QUEUE_FULL", "上游队列排队限额，请稍后再试~"

    if (code in {"MEDIA_DURATION_UNSUPPORTED", "MEDIA_DURATION_EXCEEDED"}
            or re.search(r"duration\s+must\s+be\s+between|(?:reference|audio/video) duration exceeds|invalid media duration|素材时长", text)):
        return "MEDIA_DURATION_UNSUPPORTED", "素材时长不支持，请修改后再试~"
    if (code in {"MEDIA_LIMIT_EXCEEDED", "MEDIA_TOO_LARGE"}
            or (not scope and "MODERATION" not in code and _is_media_size_failure(text))
            or re.search(r"media (?:exceeds|is empty or exceeds).*size limit|too many (?:images|videos|audio|references)"
                         r"|at most \d+ (?:images|videos|audio|references)|素材超限", text)):
        return "MEDIA_LIMIT_EXCEEDED", "素材超限，请修改后再试~"
    if code == "MEDIA_DOWNLOAD_FAILED":
        return "MEDIA_DOWNLOAD_FAILED", "素材下载失败，请检查素材链接后重试~"
    if (code == "MEDIA_EXTERNAL_URL_REQUIRED"
            or "media must be an http(s) url" in text or "素材仅支持外链" in text):
        return "MEDIA_EXTERNAL_URL_REQUIRED", "素材仅支持外链，暂不支持文件流、Base64等~"
    if (code in {"INVALID_ASSET_ID", "ASSET_NOT_READY"}
            or re.search(r"invalid\s*parameter[.\s]+asset\s*id|could not resolve ark asset type|asset.{0,30}not (?:active|ready)", text)):
        return "ASSET_NOT_READY", retry("参考素材无效或尚未就绪，请稍后重试")
    if code == "CREDIT_LIMIT_EXCEEDED":
        return "CREDIT_LIMIT_EXCEEDED", "上游报价超过费用上限，请调整后重试~"

    moderation = ("MODERATION" in code or re.search(
        r"moderation|flagged|violates? safety|safety rules|sensitive\s*content\s*detected"
        r"|policy[.\s]*violation|copyright\s+(?:restrictions?|violation)|nsfw|违规|敏感", text
    ))
    if moderation:
        if scope:
            direction, kind = scope.groups()
            if direction == "output" and kind in {"video", "audio"}:
                return "OUTPUT_MODERATION_FAILED", retry("生成的视频内容违规，请修改描述后重试")
            subject = {"text": "文本", "image": "图片", "video": "视频"}.get(kind)
            if subject:
                return f"{kind.upper()}_MODERATION_FAILED", retry(f"检测到{subject}有敏感或违规内容，请修改后重试")
            return "CONTENT_MODERATION_FAILED", retry("检测到内容有敏感或违规情况，请修改后重试")
        if re.search(r"(?:generated|output) (?:video|audio)|生成的视频", text):
            return "OUTPUT_MODERATION_FAILED", retry("生成的视频内容违规，请修改描述后重试")
        if "TEXT" in code or re.search(r"\b(?:text|prompt)\b|文本|文字", text):
            return "TEXT_MODERATION_FAILED", retry("检测到文本有敏感或违规内容，请修改后重试")
        if "IMAGE" in code or re.search(r"\b(?:image|picture)\b|图片|图像", text):
            return "IMAGE_MODERATION_FAILED", retry("检测到图片有敏感或违规内容，请修改后重试")
        if "VIDEO" in code or re.search(r"\bvideo\b|视频", text):
            return "VIDEO_MODERATION_FAILED", retry("检测到视频有敏感或违规内容，请修改后重试")
        return "CONTENT_MODERATION_FAILED", retry("检测到内容有敏感或违规情况，请修改后重试")

    if (code in {"PROVIDER_INVALID_REQUEST", "MEDIA_FORMAT_UNSUPPORTED"}
            or re.search(r"incompatible content type|could not be decoded|unsupported (?:media|image|video|audio) format"
                         r"|(?:height.*300.*6000|aspect ratio.*0\.4.*2\.5)|素材格式", text)):
        return "MEDIA_FORMAT_UNSUPPORTED", "素材格式不支持，请修改后再试~"
    return "GENERATION_FAILED", "生成失败，积分已返还，请重试~" if refunded else "生成失败，请重试~"


def public_failure_message(task: dict[str, Any]) -> str:
    return _failure_details(task)[1]


def public_failure(task: dict[str, Any]) -> dict[str, Any]:
    """Keep diagnostic codes separate from classification and submission certainty."""
    category, message = _failure_details(task)
    protocol = (task.get("upstream_response") or {}).get("protocol") or {}
    outcome = protocol.get("failure_outcome")
    if outcome not in {"rejected", "failed", "unknown"}:
        outcome = "unknown"
        if (task.get("raw_status") or {}).get("status") == "FAILED":
            outcome = "failed"
        elif (
            category in {"MEDIA_LIMIT_EXCEEDED", "MEDIA_DURATION_UNSUPPORTED", "MEDIA_FORMAT_UNSUPPORTED",
                         "MEDIA_DOWNLOAD_FAILED", "MEDIA_EXTERNAL_URL_REQUIRED"}
            and not task.get("generation_id")
            and not any(protocol.get(key) for key in ("project_create_started", "project_id", "prompt_started", "approved_id", "job_ref"))
            and not (task.get("upstream_request") or {}).get("submit")
        ):
            # Older upload failures have no project or persisted create request.
            # Never infer safety from a friendly message after project creation.
            outcome = "rejected"
    billing = (task.get("raw_status") or {}).get("billing") or {}
    return {
        "code": task.get("error_code") or "generation_failed",
        "message": message,
        "category": category,
        "outcome": outcome,
        "refunded": bool(task.get("refund_confirmed") or billing.get("refunded_at") or billing.get("status") == "refunded"),
    }
