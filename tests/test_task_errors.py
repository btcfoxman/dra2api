import pytest

from app.task_errors import public_failure_message


@pytest.mark.parametrize("code,message,expected", [
    ("GENERATION_FAILED", "All vendors failed: byteplus: Content moderation rejected by ByteDance ARK: the input image was flagged as containing a real person. Please use AI-generated reference images instead of real photos, then retry.", "参考图片中检测到可能存在真人，暂不支持，请更换图片后重试~"),
    ("CONTENT_MODERATION_FAILED", "content violates safety rules", "检测到内容有敏感或违规情况，请修改后重试~"),
    ("GENERATION_FAILED", "Input image flagged by moderation", "检测到图片有敏感或违规内容，请修改后重试~"),
    ("GENERATION_FAILED", "Text prompt failed moderation", "检测到文本有敏感或违规内容，请修改后重试~"),
    ("GENERATION_FAILED", "Input video violates safety rules", "检测到视频有敏感或违规内容，请修改后重试~"),
    ("GENERATION_FAILED", "Generated video violates safety rules", "生成的视频内容违规，请修改描述后重试~"),
    ("QUEUE_FULL", "", "上游队列排队限额，请稍后再试~"),
    ("GENERATION_FAILED", "Maximum number of concurrent tasks exceeded", "上游队列排队限额，请稍后再试~"),
    ("GENERATION_FAILED", "Upstream queue limit exceeded", "上游队列排队限额，请稍后再试~"),
    ("RATE_LIMITED", "Too many requests", "上游队列排队限额，请稍后再试~"),
    ("GENERATION_FAILED", "Queue service interrupted", "队列排队服务中断，请稍后再试~"),
    ("TASK_FAILED", "Video duration must be between 2s and 15s", "素材时长不支持，请修改后再试~"),
    ("TASK_FAILED", "total video reference duration exceeds 15 seconds", "素材时长不支持，请修改后再试~"),
    ("TASK_FAILED", "combined audio/video reference duration exceeds 30 seconds", "素材时长不支持，请修改后再试~"),
    ("TASK_FAILED", "media exceeds configured size limit", "素材超限，请修改后再试~"),
    ("MEDIA_DOWNLOAD_FAILED", "", "素材下载失败，请检查素材链接后重试~"),
    ("TASK_FAILED", "image reference has incompatible content type", "素材格式不支持，请修改后再试~"),
    ("PROVIDER_INVALID_REQUEST", "unsupported media format", "素材格式不支持，请修改后再试~"),
    ("MEDIA_EXTERNAL_URL_REQUIRED", "", "素材仅支持外链，暂不支持文件流、Base64等~"),
    ("GENERATION_FAILED", "Unknown provider error with private details", "生成失败，请重试~"),
])
def test_error_categories(code, message, expected):
    assert public_failure_message({"error_code": code, "error_message": message}) == expected


@pytest.mark.parametrize("billing,confirmed", [
    ({}, False),
    ({"credits": 975, "refund_requested_at": "2026-09-16T01:59:32Z", "status": "refund_pending"}, False),
    ({"credits": 975, "status": "refunded"}, True),
    ({"credits": 975, "refunded_at": "2026-09-16T01:59:33Z"}, True),
])
def test_refund_requires_receipt(billing, confirmed):
    task = {"error_code": "GENERATION_FAILED", "error_message": "input image containing a real person",
            "raw_status": {"billing": billing}}
    message = public_failure_message(task)
    assert ("积分已返还" in message) is confirmed
    summary = {"error_code": task["error_code"], "error_message": task["error_message"], "refund_confirmed": confirmed}
    assert public_failure_message(summary) == message


def test_unknown_refunded_failure():
    assert public_failure_message({"refund_confirmed": True}) == "生成失败，积分已返还，请重试~"
