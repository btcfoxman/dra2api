import pytest

from app.task_errors import public_failure, public_failure_message


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


@pytest.mark.parametrize("code,message,prefix", [
    ("GENERATION_FAILED", "OutputVideoSensitiveContentDetected.PolicyViolation: The request failed because the output video may be related to copyright restrictions.", "生成的视频内容违规，请修改描述后重试"),
    ("GENERATION_FAILED", "OutputAudioSensitiveContentDetected.PolicyViolation: The request failed because the output audio may be related to copyright restrictions.", "生成的视频内容违规，请修改描述后重试"),
    ("OutputVideoSensitiveContentDetected.PolicyViolation", "", "生成的视频内容违规，请修改描述后重试"),
    ("OUTPUT_VIDEO_SENSITIVE_CONTENT_DETECTED", "Please change the input image and text prompt.", "生成的视频内容违规，请修改描述后重试"),
    ("OutputVideoSensitiveContentDetected.PolicyViolation", "Please change the prompt or images of real people.", "生成的视频内容违规，请修改描述后重试"),
    ("GENERATION_FAILED", "InputImageSensitiveContentDetected: Please change the text prompt.", "检测到图片有敏感或违规内容，请修改后重试"),
    ("GENERATION_FAILED", "InputTextSensitiveContentDetected: Please use a different video.", "检测到文本有敏感或违规内容，请修改后重试"),
    ("InputVideoSensitiveContentDetected", "Please change the input image.", "检测到视频有敏感或违规内容，请修改后重试"),
    ("InputAudioSensitiveContentDetected", "Request rejected", "检测到内容有敏感或违规情况，请修改后重试"),
    ("GENERATION_FAILED", "All vendors failed: byteplus: Seedance 2.0 could not resolve ARK asset type for asset-example: InvalidParameter.AssetID: Id is Invalid. Retry after the asset is Active or check it with dl asset get.", "参考素材无效或尚未就绪，请稍后重试"),
    ("InvalidParameter.AssetID", "Id is Invalid", "参考素材无效或尚未就绪，请稍后重试"),
])
@pytest.mark.parametrize("refunded", [False, True])
def test_structured_provider_failures_are_classified_with_confirmed_refund(code, message, prefix, refunded):
    task = {"error_code": code, "error_message": message, "raw_status": {"billing": {"status": "refunded" if refunded else "refund_pending"}}}
    expected = prefix + ("，积分已返还~" if refunded else "~")
    assert public_failure_message(task) == expected
    assert public_failure_message({"error_code": code, "error_message": message, "refund_confirmed": refunded}) == expected


@pytest.mark.parametrize("code,message,expected", [
    ("GENERATION_FAILED", "Polling timeout", "队列排队服务中断，请稍后再试~"),
    ("POLLING_TIMEOUT", "", "队列排队服务中断，请稍后再试~"),
    ("GENERATION_FAILED", "PollingTimeout", "队列排队服务中断，请稍后再试~"),
    ("CREDIT_LIMIT_EXCEEDED", "上游报价超过 max_credits", "上游报价超过费用上限，请调整后重试~"),
    ("TASK_TIMEOUT", "Drama.Land generation timed out", "生成失败，请重试~"),
])
def test_specific_operational_failures_do_not_become_queue_limits(code, message, expected):
    assert public_failure_message({"error_code": code, "error_message": message}) == expected


@pytest.mark.parametrize("message", [
    "File exceeds the 10 MB images limit",
    "File exceeds the 100 MB videos limit",
    "File exceeds the 20MB audio limit",
    "The image file size of 12.5 MB exceeds the maximum size of 10 MB",
    "Image size must be less than 10 MiB",
    "Video file size must not exceed 1.5 GB",
    "Maximum allowed file size is 10485760 bytes",
    "Image is too large",
    "FileTooLarge",
    "ImageFileTooLarge",
    "FileSizeExceeded",
    "Image files are too large",
    "Upload exceeds 10 megabytes",
    "All vendors failed: File exceeds the 10 MB images limit",
])
@pytest.mark.parametrize("code", ["DRAMA_HTTP_ERROR", "PROVIDER_INVALID_REQUEST"])
def test_provider_file_size_limits_have_specific_message(code, message):
    task = {"error_code": code, "error_message": message}
    assert public_failure_message(task) == "素材超限，请修改后再试~"
    assert public_failure_message({**task, "refund_confirmed": True}) == "素材超限，请修改后再试~"


@pytest.mark.parametrize("message,expected", [
    ("File upload queue limit exceeded", "上游队列排队限额，请稍后再试~"),
    ("Video duration must be between 2s and 15s", "素材时长不支持，请修改后再试~"),
    ("Prompt token limit exceeded", "生成失败，请重试~"),
    ("Account storage quota exceeded (10 GB)", "生成失败，请重试~"),
    ("Image reference has incompatible content type", "素材格式不支持，请修改后再试~"),
    ("Unable to download image; the documented file limit is 10 MB", "生成失败，请重试~"),
])
def test_size_detection_does_not_swallow_other_failure_types(message, expected):
    assert public_failure_message({"error_code": "GENERATION_FAILED", "error_message": message}) == expected


@pytest.mark.parametrize("code,message,expected", [
    ("IMAGE_MODERATION_FAILED", "Please replace the image. Maximum allowed file size is 10 MB.", "检测到图片有敏感或违规内容，请修改后重试~"),
    ("GENERATION_FAILED", "OutputVideoSensitiveContentDetected.PolicyViolation: Please replace the input image (maximum file size 10 MB).", "生成的视频内容违规，请修改描述后重试~"),
])
def test_explicit_moderation_is_not_overridden_by_file_size_advice(code, message, expected):
    assert public_failure_message({"error_code": code, "error_message": message}) == expected


@pytest.mark.parametrize("extra,outcome", [
    ({}, "rejected"),
    ({"generation_id": "project-1"}, "unknown"),
    ({"upstream_request": {"submit": {"method": "POST"}}}, "unknown"),
    ({"upstream_response": {"protocol": {"project_create_started": True}}}, "unknown"),
    ({"upstream_response": {"protocol": {"failure_outcome": "unknown"}}}, "unknown"),
    ({"generation_id": "project-1", "raw_status": {"status": "FAILED"}}, "failed"),
])
def test_size_classification_does_not_imply_safe_resubmission(extra, outcome):
    error = public_failure({"error_code": "DRAMA_HTTP_ERROR", "error_message": "File exceeds the 10 MB images limit", **extra})
    assert error["category"] == "MEDIA_LIMIT_EXCEEDED"
    assert error["outcome"] == outcome
    assert error["code"] == "DRAMA_HTTP_ERROR"


def test_unknown_http_error_is_not_inferred_to_be_a_rejection():
    error = public_failure({"error_code": "DRAMA_HTTP_ERROR", "error_message": "Internal server error"})
    assert error["outcome"] == "unknown"
    assert error["refunded"] is False
