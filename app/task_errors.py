"""Shared task failure messages for the public API and admin console."""
from __future__ import annotations

import re
from typing import Any


def public_failure_message(task: dict[str, Any]) -> str:
    code = str(task.get("error_code") or "").upper()
    message = str(task.get("error_message") or "")
    text = message.lower()
    billing = (task.get("raw_status") or {}).get("billing") or {}
    refunded = bool(task.get("refund_confirmed") or billing.get("refunded_at")
                    or billing.get("status") == "refunded")

    def retry(prefix: str) -> str:
        return prefix + ("，积分已返还~" if refunded else "~")

    if (code in {"REAL_PERSON_DETECTED", "REAL_PERSON_NOT_SUPPORTED", "INPUT_IMAGE_REAL_PERSON"}
            or re.search(r"real (?:person|people|human)|真人", text)):
        return retry("参考图片中检测到可能存在真人，暂不支持，请更换图片后重试")

    if (code in {"QUEUE_INTERRUPTED", "QUEUE_SERVICE_UNAVAILABLE"}
            or re.search(r"queue.{0,40}(?:interrupt|unavailable|shutdown)|排队服务中断", text)):
        return "队列排队服务中断，请稍后再试~"
    if (code in {"RATE_LIMITED", "QUEUE_FULL", "QUEUE_LIMIT_EXCEEDED", "UPSTREAM_QUEUE_LIMIT",
                 "CONCURRENCY_LIMIT_EXCEEDED", "TOO_MANY_PENDING_TASKS"}
            or re.search(r"queue.{0,60}(?:full|limit|exceed|capacity)|concurren(?:t|cy).{0,60}(?:limit|exceed|maximum)"
                         r"|too many.{0,40}(?:pending|queued|concurrent)|maximum number of.{0,40}(?:tasks|jobs)"
                         r"|队列.{0,20}(?:限额|上限|已满)|排队限额", text)):
        return "上游队列排队限额，请稍后再试~"

    if (code in {"MEDIA_DURATION_UNSUPPORTED", "MEDIA_DURATION_EXCEEDED"}
            or re.search(r"duration\s+must\s+be\s+between|(?:reference|audio/video) duration exceeds|invalid media duration|素材时长", text)):
        return "素材时长不支持，请修改后再试~"
    if (code in {"MEDIA_LIMIT_EXCEEDED", "MEDIA_TOO_LARGE"}
            or re.search(r"media (?:exceeds|is empty or exceeds).*size limit|too many (?:images|videos|audio|references)"
                         r"|at most \d+ (?:images|videos|audio|references)|素材超限", text)):
        return "素材超限，请修改后再试~"
    if code == "MEDIA_DOWNLOAD_FAILED":
        return "素材下载失败，请检查素材链接后重试~"
    if (code == "MEDIA_EXTERNAL_URL_REQUIRED"
            or "media must be an http(s) url" in text or "素材仅支持外链" in text):
        return "素材仅支持外链，暂不支持文件流、Base64等~"

    moderation = ("MODERATION" in code or re.search(
        r"moderation|flagged|violates? safety|safety rules|nsfw|违规|敏感", text
    ))
    if moderation:
        if re.search(r"(?:generated|output) video|生成的视频", text):
            return retry("生成的视频内容违规，请修改描述后重试")
        if "TEXT" in code or re.search(r"\b(?:text|prompt)\b|文本|文字", text):
            return retry("检测到文本有敏感或违规内容，请修改后重试")
        if "IMAGE" in code or re.search(r"\b(?:image|picture)\b|图片|图像", text):
            return retry("检测到图片有敏感或违规内容，请修改后重试")
        if "VIDEO" in code or re.search(r"\bvideo\b|视频", text):
            return retry("检测到视频有敏感或违规内容，请修改后重试")
        return retry("检测到内容有敏感或违规情况，请修改后重试")

    if (code in {"PROVIDER_INVALID_REQUEST", "MEDIA_FORMAT_UNSUPPORTED"}
            or re.search(r"incompatible content type|could not be decoded|unsupported (?:media|image|video|audio) format"
                         r"|(?:height.*300.*6000|aspect ratio.*0\.4.*2\.5)|素材格式", text)):
        return "素材格式不支持，请修改后再试~"
    return "生成失败，积分已返还，请重试~" if refunded else "生成失败，请重试~"
