from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any


ASPECT_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
RESOLUTION_ALIASES = {"standard": "480p", "480": "480p", "480p": "480p", "hd": "720p", "720": "720p", "720p": "720p", "full_hd": "1080p", "fullhd": "1080p", "1080": "1080p", "1080p": "1080p", "2160": "4k", "2160p": "4k", "4k": "4k"}

@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    label: str
    upstream_model: str
    provider: str = "drama.land"
    durations: tuple[int, ...] = tuple(range(4, 16))
    resolutions: tuple[str, ...] = ("480p", "720p")
    aspect_ratios: tuple[str, ...] = ASPECT_RATIOS
    max_images: int = 9
    max_videos: int = 3
    max_audio: int = 3
    max_audio_seconds: int = 15
    max_video_seconds: float = 15.2
    max_audio_video_seconds: float | None = None
    max_references: int = 10
    generate_audio: bool = True
    all_in_one_reference: bool = True
    web_search: bool = False
    prompt_max_length: int = 20_000
    verification: str = "frontend_and_cli_documented"
    verified_combinations: tuple[tuple[int, str, str], ...] = ()

MODEL_SPECS = {
    "doubao-seedance-2-0-mini-260615": ModelSpec(
        id="doubao-seedance-2-0-mini-260615", label="Seedance 2.0 Mini",
        upstream_model="seedance-2-0-mini", durations=tuple(range(5, 13)),
        verification="observed_success_480p_16_9_5s", verified_combinations=((5, "480p", "16:9"),)),
    "doubao-seedance-2-0-fast-260128": ModelSpec(
        id="doubao-seedance-2-0-fast-260128", label="Seedance 2.0 Fast",
        upstream_model="seedance-2-0-fast", verification="observed_success_480p_9_16_4s",
        verified_combinations=((4, "480p", "9:16"),)),
    "doubao-seedance-2-0-260128": ModelSpec(
        id="doubao-seedance-2-0-260128", label="Seedance 2.0",
        upstream_model="seedance-2-0", resolutions=("480p", "720p", "1080p", "4k")),
    "doubao-seedance-2-5": ModelSpec(
        id="doubao-seedance-2-5", label="Seedance 2.5",
        upstream_model="seedance-2-5", durations=tuple(range(5, 31)),
        max_images=30, max_videos=10, max_audio=10, max_references=50,
        max_audio_seconds=30, max_video_seconds=30, max_audio_video_seconds=30,
        verification="observed_success_480p_16_9_5s_9images_3videos_3audio",
        verified_combinations=((5, "480p", "16:9"),)),
}
DEFAULT_MODEL_MAP = {}
for _id, _spec in MODEL_SPECS.items():
    DEFAULT_MODEL_MAP[_id] = _id
    DEFAULT_MODEL_MAP[_spec.upstream_model] = _id
    DEFAULT_MODEL_MAP[_spec.upstream_model.replace("seedance-2-0", "seedance-2.0").replace("seedance-2-5", "seedance-2.5")] = _id
DEFAULT_MODEL_MAP.update({"seedance-2.0-pro": "doubao-seedance-2-0-260128", "sd-2-0": "doubao-seedance-2-0-260128", "sd-2-0-fast": "doubao-seedance-2-0-fast-260128"})
MEDIA_LIMITS = {"images": 30, "videos": 10, "audio": 10}
VIDEO_DIMENSIONS = {}
# Snapshot estimates; the generation approval quote and billing receipt are authoritative.
CREDIT_RATES = {
    "seedance-2-0-mini": {"480p": 45, "720p": 90},
    "seedance-2-0-fast": {"480p": 65, "720p": 90},
    "seedance-2-0": {"480p": 60, "720p": 120, "1080p": 360, "4k": 900},
    "seedance-2-5": {"480p": 130, "720p": 200},
}

def parse_model_map(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        source = value
    else:
        text = str(value or "").strip()
        if not text:
            return dict(DEFAULT_MODEL_MAP)
        try:
            source = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("model_map must be a JSON object") from exc
    if not isinstance(source, dict):
        raise ValueError("model_map must be a JSON object")
    mapping = {
        str(key).strip(): str(target).strip()
        for key, target in source.items()
        if str(key).strip() and str(target).strip()
    }
    unsupported = sorted({target for target in mapping.values() if target not in MODEL_SPECS})
    if unsupported:
        raise ValueError(f"unsupported Drama model targets: {', '.join(unsupported)}")
    return {**DEFAULT_MODEL_MAP, **mapping}


def model_map_json(value: Any) -> str:
    return json.dumps(parse_model_map(value), ensure_ascii=False, separators=(",", ":"))


def mapped_model(model: Any, model_map: Any = None) -> str:
    requested = str(model or "doubao-seedance-2-0-mini-260615").strip()
    target = parse_model_map(model_map).get(requested, requested)
    if target not in MODEL_SPECS:
        raise ValueError(f"unsupported model: {requested}")
    return target


def model_spec(model: Any, model_map: Any = None) -> ModelSpec:
    return MODEL_SPECS[mapped_model(model, model_map)]


def _source_item(value: Any, kind: str, index: int) -> dict[str, str] | None:
    if isinstance(value, str):
        source = value.strip()
        name = ""
    elif isinstance(value, dict):
        raw = (
            value.get("value")
            or value.get("url")
            or value.get(f"{kind}_url")
            or value.get("data")
            or ""
        )
        if isinstance(raw, dict):
            raw = raw.get("url") or raw.get("value") or ""
        if kind == "audio" and isinstance(value.get("input_audio"), dict):
            audio = value["input_audio"]
            fmt = "mpeg" if audio.get("format") == "mp3" else str(audio.get("format") or "wav")
            raw = f"data:audio/{fmt};base64,{audio.get('data', '')}"
        source = str(raw).strip()
        name = str(value.get("name") or value.get("filename") or "").strip()
    else:
        return None
    if not source:
        return None
    return {"value": source, "name": name or f"{kind}-{index + 1}"}


def _direct_media(payload: dict[str, Any], kind: str) -> list[dict[str, str]]:
    keys = {
        "image": ("image_urls", "images", "reference_images"),
        "video": ("video_urls", "videos", "reference_videos"),
        "audio": ("audio_urls", "audios", "reference_audios"),
    }[kind]
    items: list[Any] = []
    for key in keys:
        value = payload.get(key)
        if value is not None:
            items.extend(value if isinstance(value, list) else [value])
    singular = payload.get(f"{kind}_url")
    if singular:
        items.append(singular)
    if kind == "image":
        for key in ("image", "first_frame", "last_frame", "tail_image"):
            if payload.get(key):
                items.append(payload[key])
    return [
        item
        for index, value in enumerate(items)
        if (item := _source_item(value, kind, index)) is not None
    ]


def _content_media(payload: dict[str, Any], kind: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for entry in payload.get("content") or []:
        if not isinstance(entry, dict):
            continue
        entry_type = str(entry.get("type") or "").lower()
        if kind not in entry_type and not (kind == "audio" and "voice" in entry_type):
            continue
        if item := _source_item(entry, kind, len(result)):
            result.append(item)
    return result


def _deduplicate(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for item in items:
        if item["value"] in seen:
            continue
        seen.add(item["value"])
        result.append(item)
    return result


def _normalize_resolution(value: Any, spec: ModelSpec) -> str:
    raw = str(value or spec.resolutions[0]).strip()
    key = raw.lower().replace("-", "_").replace(" ", "_")
    normalized = RESOLUTION_ALIASES.get(key, raw)
    for supported in spec.resolutions:
        if supported.lower() == normalized.lower():
            return supported
    raise ValueError(
        f"{spec.id} resolution must be one of {', '.join(spec.resolutions)}"
    )


def _apply_media_limit(
    values: list[dict[str, str]],
    *,
    limit: int,
    kind: str,
    spec: ModelSpec,
    policy: str,
) -> list[dict[str, str]]:
    if policy == "strict" and len(values) > limit:
        raise ValueError(f"{spec.id} supports at most {limit} {kind}")
    return values[:limit]


_REFERENCE_PATTERN = re.compile(r"(?i)@(?:图(?:片)?|image|视频|video|音频|声音|audio)\s*\d*")


def normalize_generation_request(
    payload: dict[str, Any],
    model_map: Any = None,
    excess_media_policy: str = "strict",
    cleanup_prompt_references: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("request body must be an object")
    policy = str(excess_media_policy or "ignore").lower()
    if policy not in {"ignore", "strict"}:
        raise ValueError("excess_media_policy must be ignore or strict")
    spec = model_spec(payload.get("model"), model_map)
    if payload.get("max_credits") is not None:
        try:
            maximum = float(payload["max_credits"])
        except (TypeError, ValueError) as exc:
            raise ValueError("max_credits must be a positive number") from exc
        if not math.isfinite(maximum) or maximum <= 0 or isinstance(payload["max_credits"], bool):
            raise ValueError("max_credits must be a positive number")
    for flag in ("generate_audio", "background"):
        if payload.get(flag) is not None and not isinstance(payload[flag], bool):
            raise ValueError(f"{flag} must be a boolean")
    try:
        raw_duration = payload.get("duration", 5)
        duration = int(raw_duration)
        if isinstance(raw_duration, bool) or float(raw_duration) != duration:
            raise ValueError("duration must be an integer number of seconds")
    except (TypeError, ValueError) as exc:
        raise ValueError("duration must be an integer number of seconds") from exc
    if duration not in spec.durations:
        raise ValueError(
            f"{spec.id} duration must be one of {', '.join(map(str, spec.durations))} seconds"
        )
    ratio = str(
        payload.get("aspect_ratio")
        or payload.get("aspectRatio")
        or payload.get("ratio")
        or "16:9"
    ).strip()
    if ratio not in spec.aspect_ratios:
        raise ValueError(
            f"{spec.id} aspect_ratio must be one of {', '.join(spec.aspect_ratios)}"
        )
    prompt = payload.get("prompt") or (payload.get("input") if isinstance(payload.get("input"), str) else "")
    if not prompt:
        prompt = "\n".join(str(item.get("text") or "") for item in payload.get("content") or []
                           if isinstance(item, dict) and item.get("type") in {"text", "input_text"})
    prompt = str(prompt).strip()
    if not prompt:
        raise ValueError("prompt is required")
    if len(prompt) > spec.prompt_max_length:
        raise ValueError(f"{spec.id} prompt exceeds {spec.prompt_max_length} characters")

    images = _apply_media_limit(
        _deduplicate(_direct_media(payload, "image") + _content_media(payload, "image")),
        limit=spec.max_images,
        kind="images",
        spec=spec,
        policy=policy,
    )
    videos = _apply_media_limit(
        _deduplicate(_direct_media(payload, "video") + _content_media(payload, "video")),
        limit=spec.max_videos,
        kind="videos",
        spec=spec,
        policy=policy,
    )
    audio = _apply_media_limit(
        _deduplicate(_direct_media(payload, "audio") + _content_media(payload, "audio")),
        limit=spec.max_audio,
        kind="audio files",
        spec=spec,
        policy=policy,
    )
    if len(images) + len(videos) + len(audio) > spec.max_references:
        raise ValueError(f"{spec.id} supports at most {spec.max_references} references in total")
    if audio and not images and not videos and spec.upstream_model != "seedance-2-5":
        raise ValueError("Seedance 2.0 audio references require an image or video reference")
    if isinstance(payload.get("n"), bool) or str(payload.get("n", 1)) != "1":
        raise ValueError("only n=1 is supported")
    for key in ("size", "width", "height", "seed", "fps"):
        if payload.get(key) is not None:
            raise ValueError(f"{key} is not supported; use resolution and aspect_ratio")
    if payload.get("web_search"):
        raise ValueError("web_search is not supported")
    if cleanup_prompt_references:
        prompt = re.sub(r"[ \t]{2,}", " ", _REFERENCE_PATTERN.sub("", prompt)).strip()

    requested_generate_audio = payload.get("generate_audio")
    generate_audio = spec.generate_audio and (
        True if requested_generate_audio is None else bool(requested_generate_audio)
    )
    requested_web_search = payload.get("web_search")
    web_search = spec.web_search and (
        True if requested_web_search is None else bool(requested_web_search)
    )
    requested_all_in_one = payload.get("all_in_one_reference")
    all_in_one_reference = spec.all_in_one_reference and (
        True if requested_all_in_one is None else bool(requested_all_in_one)
    )
    normalized = dict(payload)
    normalized.update(
        {
            "kind": "video",
            "model": spec.id,
            "upstream_model": spec.upstream_model,
            "prompt": prompt,
            "duration": duration,
            "resolution": _normalize_resolution(payload.get("resolution"), spec),
            "aspect_ratio": ratio,
            "generate_audio": generate_audio,
            "web_search": web_search,
            "all_in_one_reference": all_in_one_reference,
            "negative_prompt": str(
                payload.get("negative_prompt") or payload.get("negativePrompt") or ""
            ).strip(),
            "_images": images,
            "_videos": videos,
            "_audio": audio,
        }
    )
    return normalized


def public_models(model_map: Any = None) -> list[dict[str, Any]]:
    mapping = parse_model_map(model_map)
    aliases: dict[str, list[str]] = {key: [] for key in MODEL_SPECS}
    for alias, target in mapping.items():
        aliases[target].append(alias)
    result: list[dict[str, Any]] = []
    for spec in MODEL_SPECS.values():
        result.append(
            {
                "id": spec.id,
                "object": "model",
                "type": "video",
                "owned_by": "drama",
                "aliases": sorted(set(aliases[spec.id])),
                "meta": {
                    "label": spec.label,
                    "provider": "Drama.Land",
                    "upstream_model": spec.upstream_model,
                },
                "capabilities": {
                    "durations": list(spec.durations),
                    "aspect_ratios": list(spec.aspect_ratios),
                    "resolutions": list(spec.resolutions),
                    "generate_audio": spec.generate_audio,
                    "media_limits": {
                        "images": spec.max_images,
                        "videos": spec.max_videos,
                        "audio": spec.max_audio,
                    },
                    "max_audio_seconds": spec.max_audio_seconds,
                    "max_video_seconds": spec.max_video_seconds,
                    "max_audio_video_seconds": spec.max_audio_video_seconds,
                    "max_references": spec.max_references,
                    "verification": spec.verification,
                    "verified_combinations": [{"duration": duration, "resolution": resolution, "aspect_ratio": ratio}
                                              for duration, resolution, ratio in spec.verified_combinations],
                    "audio_toggle_guaranteed": False,
                    "estimated_credits_per_second": CREDIT_RATES.get(spec.upstream_model, {}),
                    "credit_source": "estimate; final upstream quote applies",
                },
            }
        )
    return result


def model_specs_json() -> list[dict[str, Any]]:
    return [asdict(spec) for spec in MODEL_SPECS.values()]
