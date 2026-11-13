import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Optional, Tuple

import aiohttp
from oa_image_animation import resolve_output_config, validate_gif_result
from oa_image_clients import JsonImageClient, OpenAIImageClient
from oa_image_character_library import reference_prompt, resolve_character_images
from oa_image_config import _apply_bot_appearance, _get_nested
from oa_image_novelai import NovelAIImageClient, resolve_prompt_rating
from oa_image_prompt_types import NovelAIPrompt
from oa_image_runtime import (
    get_latest_cached_image,
    get_stream_lock,
    merge_state_detail,
    track_command_task,
    update_state,
)
from oa_image_sources import (
    _clean_base64,
    _data_url_mime,
    _download_reference_image,
    _extract_first_url,
    _extract_local_image_source,
    _load_bot_appearance_image,
    _load_local_reference_image,
    _local_image_root,
    _looks_like_any_local_image_source,
    _looks_like_local_image_source,
    _looks_like_url,
)

logger = logging.getLogger("OAImage")

SendImageCallback = Callable[..., Awaitable[Any]]
SendTextCallback = Callable[..., Awaitable[Any]]
ContextBuilderCallback = Callable[[str, str, dict, str], Awaitable[str | NovelAIPrompt]]
ProactiveTriggerCallback = Callable[..., Awaitable[Any]]


def _format_generation_error(exc: Exception, config: dict) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        timeout = int(_get_nested(config, "api.timeout_seconds", 300))
        return f"image generation timed out after {timeout}s"
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _normalize_delivery(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"chat", "internal", "both"}:
        return normalized
    return "both"


async def _describe_generated_image(
    *,
    stream_id: str,
    image_base64: str,
    prompt: str,
    resolved_mode: str,
    delivery: str,
    sent: bool = False,
    output_format: str = "auto",
    proactive_trigger: ProactiveTriggerCallback | None = None,
) -> None:
    try:
        image_bytes = base64.b64decode(_clean_base64(image_base64))
    except Exception as exc:
        merge_state_detail(stream_id, description=f"decode failed: {exc}")
        return

    try:
        from src.chat.image_system.image_manager import image_manager

        description = await image_manager.get_image_description(image_bytes=image_bytes, wait_for_build=True)
        if description and output_format == "gif":
            description = "GIF 动图可见画面（不代表已验证所有帧、动作或循环）：" + description
    except Exception as exc:
        logger.debug(f"生成图内部识图失败: {exc}")
        description = ""

    merge_state_detail(
        stream_id,
        summary="generated image completed" if description else "generated image completed; VLM description unavailable",
        prompt=prompt[:240],
        mode=resolved_mode,
        delivery=delivery,
        sent=str(sent).lower(),
        description=description[:600] if description else "",
    )
    if description:
        update_state(stream_id, "image_described", "generated image description ready")
    if proactive_trigger is not None:
        try:
            await proactive_trigger(
                stream_id=stream_id,
                prompt=prompt,
                mode=resolved_mode,
                delivery=delivery,
                description=description,
                sent=sent,
            )
        except Exception as exc:
            logger.debug(f"生成图完成后触发主动继续失败: {exc}")


async def _run_generation_impl(
    *,
    stream_id: str,
    raw_prompt: str,
    config: dict,
    mode: str = "auto",
    output_format: str = "auto",
    frame_count: int | None = None,
    frame_duration_ms: int | None = None,
    reference_image_base64: Optional[str] = None,
    use_recent_image: bool = False,
    image_source: str = "",
    characters: list[str] | None = None,
    profile_name: str = "",
    use_bot_appearance: Optional[bool] = None,
    delivery: str = "both",
    send_image: SendImageCallback | None = None,
    send_text: SendTextCallback | None = None,
    context_builder: ContextBuilderCallback | None = None,
    proactive_trigger: ProactiveTriggerCallback | None = None,
    usage_callback: Callable[..., Awaitable[Any]] | None = None,
    reference_resolver: Callable[[], Awaitable[str | None]] | None = None,
    meta: dict[str, Any] | None = None,
) -> Tuple[bool, str]:
    if not stream_id:
        return False, "missing stream_id"
    if not _get_nested(config, "plugin.enabled", False):
        return False, "plugin disabled"
    config, profile_id, profile_label = resolve_output_config(
        config, profile_name, output_format=output_format, frame_count=frame_count, frame_duration_ms=frame_duration_ms,
    )
    is_gif = _get_nested(config, "runtime.output_format", "") == "gif"
    if not raw_prompt.strip():
        raw_prompt = str(_get_nested(config, "behavior.default_prompt", "根据当前聊天内容生成一张图片"))
    provider = str(_get_nested(config, "api.provider", "openai")).lower()
    is_json_provider = provider == "json_api"
    is_novelai = provider == "novelai"
    if is_novelai:
        NovelAIImageClient(config).validate_configuration()
        if context_builder is None:
            return False, "NovelAI 需要配置独立的提示词整理步骤"
        if reference_image_base64 or image_source or use_recent_image or use_bot_appearance or reference_resolver is not None or mode == "image_to_image":
            return False, "NovelAI 当前套餐支持文生图；参考图和图片编辑请使用 openai_default 套餐"
    if not image_source:
        image_source = _extract_first_url(raw_prompt)
    if not image_source and not is_json_provider:
        image_source = _extract_local_image_source(raw_prompt)
    requested_reference = bool(reference_image_base64 or image_source or use_recent_image or reference_resolver)
    if is_gif and requested_reference:
        # 指定参考图时走编辑接口。原图读取失败也保持编辑模式并报告缺图。
        mode = "image_to_image"
    if is_novelai or (is_gif and mode != "image_to_image"):
        # V5 和纯文字动图只使用角色名称。参考图动图可以使用原有的角色库参考。
        character_images, missing_characters = [], []
        if characters:
            if not isinstance(characters, list) or any(not isinstance(name, str) for name in characters):
                return False, "characters 必须是角色名字符串数组"
            raw_prompt += "\nRequested characters: " + ", ".join(characters)
    else:
        try:
            character_images, missing_characters = await asyncio.to_thread(
                resolve_character_images, config, raw_prompt, characters,
            )
        except (ValueError, OSError) as exc:
            update_state(stream_id, "image_failed", str(exc)[:120])
            return False, str(exc)
    if character_images and is_json_provider:
        return False, "character library references require an OpenAI-compatible image edit profile"
    resolved_delivery = _normalize_delivery(delivery or str(_get_nested(config, "behavior.default_delivery", "both")))
    max_age = int(_get_nested(config, "behavior.reference_image_max_age_seconds", 900))
    if reference_resolver is not None and not reference_image_base64 and not image_source:
        reference_image_base64 = await reference_resolver()
    if use_recent_image and not reference_image_base64 and not image_source:
        reference_image_base64 = get_latest_cached_image(stream_id, max_age)
    if is_gif and requested_reference and not (reference_image_base64 or image_source):
        return False, "缺少可用参考原图，请在当前会话发送图片，或指定带图消息的 reference_message_id。"
    reference_image_mime = _data_url_mime(reference_image_base64 or "") or "image/png"
    reference_image_filename = "reference.png"
    if not image_source and reference_image_base64 and _looks_like_url(reference_image_base64):
        image_source = reference_image_base64
    resolved_mode = mode
    if resolved_mode == "auto":
        resolved_mode = "image_to_image" if reference_image_base64 or image_source else "text_to_image"
    if (
        resolved_mode == "image_to_image"
        and is_json_provider
        and _get_nested(config, "profile_meta.requires_image_source", False)
        and not image_source
    ):
        return False, f"profile {profile_id or profile_label} requires image_source"
    if (
        resolved_mode == "image_to_image"
        and is_json_provider
        and image_source
        and not _looks_like_url(image_source)
    ):
        return False, "json_api image_source must be a public URL"
    if resolved_mode == "image_to_image" and not is_json_provider and not reference_image_base64 and image_source:
        if _looks_like_url(image_source):
            timeout = int(_get_nested(config, "api.timeout_seconds", 120))
            max_bytes = int(
                _get_nested(
                    config,
                    "behavior.url_image_max_bytes",
                    _get_nested(config, "behavior.local_image_max_bytes", 10 * 1024 * 1024),
                )
            )
            allow_non_image = bool(_get_nested(config, "behavior.allow_non_image_url_content_type", False))
            async with aiohttp.ClientSession() as session:
                (
                    reference_image_base64,
                    reference_image_mime,
                    reference_image_filename,
                ) = await _download_reference_image(session, image_source, timeout, max_bytes, allow_non_image)
        elif _looks_like_local_image_source(image_source, config):
            if not _get_nested(config, "behavior.allow_local_image_source", False):
                return False, "local image_source is disabled"
            reference_image_base64, reference_image_mime, reference_image_filename = await asyncio.to_thread(
                _load_local_reference_image, config, image_source
            )
        elif _looks_like_any_local_image_source(image_source):
            return False, f"local image_source must be inside {_local_image_root(config)}"
        else:
            reference_image_base64 = _clean_base64(image_source)
            reference_image_mime = _data_url_mime(image_source) or "image/png"
    references: list[tuple[str, str, str]] = []
    reference_labels: list[str] = []
    reference_manifest: list[dict[str, Any]] = []
    if reference_image_base64 and resolved_mode == "image_to_image":
        references.append((reference_image_base64, reference_image_mime, reference_image_filename))
        reference_labels.append("user-provided editing source")
        reference_manifest.append({"kind": "user_reference", "label": "user-provided reference"})
    if use_bot_appearance and (not is_gif or mode == "image_to_image"):
        bot_app_b64, bot_app_mime, bot_app_name = await asyncio.to_thread(_load_bot_appearance_image, config)
        if bot_app_b64:
            references.append((bot_app_b64, bot_app_mime or "image/png", bot_app_name or "bot_appearance.png"))
            reference_labels.append("Bot's own appearance")
            reference_manifest.append({"kind": "bot_identity", "label": "Bot's own appearance"})
            if resolved_mode == "text_to_image":
                resolved_mode = "image_to_image"
            logger.debug("已注入 Bot 外形参考图，模式切换为 image_to_image")
    for image in character_images:
        references.append((image.image_base64, image.mime, image.filename))
        reference_labels.append(image.name)
        reference_manifest.append({"kind": "character_identity", "label": image.name})
    if character_images:
        resolved_mode = "image_to_image"
    if ((resolved_mode == "image_to_image" or use_recent_image) and not is_json_provider
            and not references):
        return False, "缺少可用参考原图，请在当前会话发送图片，或指定带图消息的 reference_message_id。无需一定引用图片。"
    supported_modes = _get_nested(config, "profile_meta.supported_modes", [])
    if supported_modes and resolved_mode not in supported_modes:
        return False, f"profile {profile_id or profile_label} does not support {resolved_mode}"
    if send_image is None and resolved_delivery in {"chat", "both"}:
        return False, "send_image callback is required"
    client = NovelAIImageClient(config) if is_novelai else (JsonImageClient(config) if is_json_provider else OpenAIImageClient(config))
    if not is_json_provider:
        client.usage_callback = usage_callback
    if not getattr(client, "base_url", ""):
        return False, "api.base_url is empty"
    lock = get_stream_lock(stream_id)
    if lock.locked() and _get_nested(config, "behavior.skip_when_busy", True):
        return False, "stream is busy"
    try:
        async with lock:
            prompt_source, appearance_injected = _apply_bot_appearance(raw_prompt.strip(), config, use_bot_appearance)
            update_state(stream_id, "preparing_prompt", "preparing image prompt")
            # 每次请求独立构造清单，顺序与真实 API 附图一致；不修改共享套餐配置。
            prompt_config = {**config, "runtime": {
                **_get_nested(config, "runtime", {}),
                "reference_images": reference_manifest, "generation_mode": resolved_mode,
            }}
            prompt = (
                await context_builder(prompt_source, stream_id, prompt_config, resolved_mode)
                if context_builder is not None
                else prompt_source
            )
            if not is_novelai and (references or missing_characters):
                prompt = reference_prompt(
                    prompt, reference_labels, missing_characters,
                    [reference["kind"] for reference in reference_manifest],
                )
            profile_note = f"[{profile_label}] " if profile_label else ""
            appearance_note = "[bot appearance] " if appearance_injected else ""
            # NovelAI 的评分标签决定结果能否进入内部上下文；与客户端 _apply_rating 同一套判定。
            if is_novelai and meta is not None:
                rating_text = prompt.prompt if isinstance(prompt, NovelAIPrompt) else str(prompt)
                meta["rating"] = resolve_prompt_rating(rating_text, config)
            update_state(stream_id, "generating_image", f"{profile_note}{appearance_note}{raw_prompt[:80]}")
            if resolved_mode == "image_to_image":
                if is_json_provider:
                    image_base64 = await client.generate_image_to_image(prompt, image_source)  # type: ignore[arg-type]
                elif not references:
                    update_state(stream_id, "image_failed", "missing reference image")
                    return False, "missing reference image"
                else:
                    image_base64 = await client.generate_image_to_image(
                        prompt,
                        *references[0],
                        additional_images=references[1:],
                    )
            else:
                image_base64 = await client.generate_text_to_image(prompt)
            if not image_base64:
                return False, "endpoint returned no image"
            if is_gif:
                validate_gif_result(image_base64)
            sent = False
            if resolved_delivery in {"chat", "both"}:
                sent = bool(
                    await send_image(
                        image_base64,
                        stream_id,
                        storage_message=bool(_get_nested(config, "behavior.storage_message", True)),
                    )
                )
            if resolved_delivery in {"internal", "both"}:
                merge_state_detail(
                    stream_id,
                    summary="generated image completed; VLM description pending",
                    prompt=raw_prompt[:240],
                    mode=resolved_mode,
                    delivery=resolved_delivery,
                    sent=str(sent).lower(),
                    description="pending",
                )
                description_task = asyncio.create_task(
                    _describe_generated_image(
                        stream_id=stream_id,
                        image_base64=image_base64,
                        prompt=raw_prompt,
                        resolved_mode=resolved_mode,
                        delivery=resolved_delivery,
                        sent=sent,
                        output_format="gif" if is_gif else "auto",
                        proactive_trigger=proactive_trigger,
                    )
                )
                track_command_task(description_task, stream_id)
            result_text = f"image sent via {profile_id}" if profile_id else "image sent"
            if is_gif:
                result_text = f"GIF animation sent via {profile_id}"
            if resolved_delivery == "internal":
                result_text = f"image generated internally via {profile_id}" if profile_id else "image generated internally"
            update_state(
                stream_id,
                "image_sent" if sent or resolved_delivery == "internal" else "image_send_failed",
                {
                    "summary": result_text,
                    "prompt": f"{profile_note}{appearance_note}{raw_prompt[:160]}",
                    "mode": resolved_mode,
                    "delivery": resolved_delivery,
                    "sent": str(sent).lower(),
                    "rating": str((meta or {}).get("rating", "")),
                    "description": "pending" if resolved_delivery in {"internal", "both"} else "",
                    "output_format": "gif" if is_gif else "auto",
                    "reference_characters": ", ".join(image.name for image in character_images),
                    "characters_without_reference": ", ".join(missing_characters),
                    "prompt_skill_modules": ", ".join(prompt.skill_modules) if isinstance(prompt, NovelAIPrompt) else "",
                    "prompt_skill_version": prompt.skill_version if isinstance(prompt, NovelAIPrompt) else "",
                },
            )
            if resolved_delivery == "internal":
                return True, result_text
            return sent, result_text if sent else "send failed"
    except Exception as e:
        error_text = _format_generation_error(e, config)
        update_state(stream_id, "image_failed", error_text[:120])
        return False, error_text


async def run_generation(**kwargs: Any) -> Tuple[bool, str]:
    """统一回报参考图准备、上游与发送结果；后台失败始终进入内部上下文。"""
    config = kwargs.get("config") or {}
    result_callback = kwargs.pop("result_callback", None)
    stream_id = kwargs.get("stream_id", "")
    cancelled = False
    meta: dict[str, Any] = {}
    kwargs["meta"] = meta
    try:
        success, message = await _run_generation_impl(**kwargs)
    except asyncio.CancelledError:
        success, message, cancelled = False, "图片生成任务已取消，不能继续报告正在生成。", True
    except Exception as exc:
        success, message = False, _format_generation_error(exc, config)
    # 内部结果和群消息使用相同的脱敏文本，包含所有套餐密钥与异常包装之前的错误。
    for profile in _get_nested(config, "profiles.items", {}).values():
        for api in (profile, profile.get("api", {})):
            key = str(api.get("api_key") or "")
            if key:
                message = message.replace(key, "[redacted]")
    message = message[:500]
    if not success:
        update_state(stream_id, "image_cancelled" if cancelled else "image_failed", message[:120])
        logger.error("生图失败: %s", message)
    error_notified = False
    if not success and not cancelled and _get_nested(config, "behavior.send_error_messages", False) and kwargs.get("send_text") is not None:
        try:
            error_notified = bool(await kwargs["send_text"](f"生图失败：{message}", stream_id, storage_message=False))
        except Exception as exc:
            # 直发失败也必须继续回传 Planner，由 Replyer 告知用户。
            logger.warning("生图错误消息发送失败: %s", type(exc).__name__)
    if result_callback is not None:
        try:
            await result_callback(
                stream_id=stream_id, prompt=kwargs.get("raw_prompt", ""), mode=kwargs.get("mode", "auto"),
                delivery=kwargs.get("delivery", "both"), rating=str(meta.get("rating", "")),
                success=success, message=message, cancelled=cancelled,
                error_notified=error_notified,
            )
        except Exception as exc:
            logger.warning("生图结果反馈失败: %s", type(exc).__name__)
    if cancelled:
        raise asyncio.CancelledError
    return success, message
