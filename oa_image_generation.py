import traceback
from typing import Optional, Tuple

import aiohttp
from oa_image_clients import JsonImageClient, OpenAIImageClient
from oa_image_config import _apply_bot_appearance, _get_nested, _resolve_profile_config
from oa_image_runtime import build_context_prompt, get_latest_cached_image, get_stream_lock, update_state
from oa_image_sources import (
    _clean_base64,
    _data_url_mime,
    _download_reference_image,
    _extract_first_url,
    _extract_local_image_source,
    _load_local_reference_image,
    _local_image_root,
    _looks_like_any_local_image_source,
    _looks_like_local_image_source,
    _looks_like_url,
)

from src.common.logger import get_logger
from src.plugin_system.apis import send_api

logger = get_logger("OAImage")


async def run_generation(
    *,
    stream_id: str,
    raw_prompt: str,
    config: dict,
    mode: str = "auto",
    reference_image_base64: Optional[str] = None,
    use_recent_image: bool = False,
    image_source: str = "",
    profile_name: str = "",
    use_bot_appearance: Optional[bool] = None,
) -> Tuple[bool, str]:
    if not stream_id:
        return False, "missing stream_id"
    if not _get_nested(config, "plugin.enabled", False):
        return False, "plugin disabled"
    config, profile_id, profile_label = _resolve_profile_config(config, profile_name)
    if not raw_prompt.strip():
        raw_prompt = str(_get_nested(config, "behavior.default_prompt", "根据当前聊天内容生成一张图片"))
    provider = str(_get_nested(config, "api.provider", "openai")).lower()
    is_json_provider = provider == "json_api"
    max_age = int(_get_nested(config, "behavior.reference_image_max_age_seconds", 900))
    if use_recent_image and not reference_image_base64:
        reference_image_base64 = get_latest_cached_image(stream_id, max_age)
    reference_image_mime = _data_url_mime(reference_image_base64 or "") or "image/png"
    reference_image_filename = "reference.png"
    if not image_source:
        image_source = _extract_first_url(raw_prompt)
    if not image_source and not is_json_provider:
        image_source = _extract_local_image_source(raw_prompt)
    if not image_source and reference_image_base64 and _looks_like_url(reference_image_base64):
        image_source = reference_image_base64
    resolved_mode = mode
    if resolved_mode == "auto":
        resolved_mode = "image_to_image" if reference_image_base64 or image_source else "text_to_image"
    supported_modes = _get_nested(config, "profile_meta.supported_modes", [])
    if supported_modes and resolved_mode not in supported_modes:
        return False, f"profile {profile_id or profile_label} does not support {resolved_mode}"
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
            async with aiohttp.ClientSession() as session:
                (
                    reference_image_base64,
                    reference_image_mime,
                    reference_image_filename,
                ) = await _download_reference_image(session, image_source, timeout)
        elif _looks_like_local_image_source(image_source, config):
            if not _get_nested(config, "behavior.allow_local_image_source", False):
                return False, "local image_source is disabled"
            reference_image_base64, reference_image_mime, reference_image_filename = _load_local_reference_image(
                config, image_source
            )
        elif _looks_like_any_local_image_source(image_source):
            return False, f"local image_source must be inside {_local_image_root(config)}"
        else:
            reference_image_base64 = _clean_base64(image_source)
            reference_image_mime = _data_url_mime(image_source) or "image/png"
    prompt_source, appearance_injected = _apply_bot_appearance(raw_prompt.strip(), config, use_bot_appearance)
    prompt = build_context_prompt(prompt_source, stream_id, config, resolved_mode)
    client = JsonImageClient(config) if is_json_provider else OpenAIImageClient(config)
    if not getattr(client, "base_url", ""):
        return False, "api.base_url is empty"
    lock = get_stream_lock(stream_id)
    if lock.locked() and _get_nested(config, "behavior.skip_when_busy", True):
        return False, "stream is busy"
    try:
        async with lock:
            profile_note = f"[{profile_label}] " if profile_label else ""
            appearance_note = "[bot appearance] " if appearance_injected else ""
            update_state(stream_id, "generating_image", f"{profile_note}{appearance_note}{raw_prompt[:80]}")
            if resolved_mode == "image_to_image":
                if is_json_provider:
                    image_base64 = await client.generate_image_to_image(prompt, image_source)  # type: ignore[arg-type]
                elif not reference_image_base64:
                    update_state(stream_id, "image_failed", "missing reference image")
                    return False, "missing reference image"
                else:
                    image_base64 = await client.generate_image_to_image(
                        prompt,
                        reference_image_base64,
                        reference_image_mime,
                        reference_image_filename,
                    )
            else:
                image_base64 = await client.generate_text_to_image(prompt)
            if not image_base64:
                return False, "endpoint returned no image"
            sent = await send_api.image_to_stream(
                image_base64=image_base64,
                stream_id=stream_id,
                storage_message=bool(_get_nested(config, "behavior.storage_message", True)),
            )
            result_text = f"image sent via {profile_id}" if profile_id else "image sent"
            update_state(
                stream_id,
                "image_sent" if sent else "image_send_failed",
                f"{profile_note}{appearance_note}{raw_prompt[:80]}",
            )
            return sent, result_text if sent else "send failed"
    except Exception as e:
        update_state(stream_id, "image_failed", str(e)[:120])
        logger.error(f"生图失败: {e}")
        logger.error(traceback.format_exc())
        if _get_nested(config, "behavior.send_error_messages", False):
            await send_api.text_to_stream(
                text=f"生图失败：{e}",
                stream_id=stream_id,
                storage_message=False,
            )
        return False, str(e)
