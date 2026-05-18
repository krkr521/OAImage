import copy
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("OAImage")


def _get_nested(config: Optional[dict], key: str, default=None):
    if not config:
        return default
    current = config
    for part in key.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def _parse_size(size: str, default_width: int = 512, default_height: int = 512) -> Tuple[int, int]:
    match = re.match(r"^\s*(\d+)\s*[xX*]\s*(\d+)\s*$", size or "")
    if not match:
        return default_width, default_height
    width = max(64, min(2048, int(match.group(1))))
    height = max(64, min(2048, int(match.group(2))))
    return width, height


def _dict_config(config: Optional[dict], key: str) -> Dict[str, Any]:
    value = _get_nested(config, key, {})
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
            if isinstance(loaded, dict):
                return loaded
        except Exception as e:
            logger.warning(f"解析透传参数 {key} 失败: {e}")
    return {}


def _merge_passthrough_params(config: dict, payload: Dict[str, Any], mode: str) -> Dict[str, Any]:
    if not _get_nested(config, "passthrough.enabled", False):
        return payload
    merged = dict(payload)
    allow_override = bool(_get_nested(config, "passthrough.allow_override_core_params", False))
    passthrough_params = _dict_config(config, "passthrough.shared_params")
    passthrough_params.update(_dict_config(config, f"passthrough.{mode}_params"))
    for key, value in passthrough_params.items():
        if not allow_override and key in payload:
            logger.debug(f"跳过透传参数 {key}: 与核心 payload 字段冲突")
            continue
        merged[key] = value
    return merged


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _merge_dict(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _get_profile_items(config: Optional[dict]) -> Dict[str, Dict[str, Any]]:
    items = _get_nested(config, "profiles.items", {})
    if isinstance(items, str) and items.strip():
        try:
            loaded = json.loads(items)
            items = loaded if isinstance(loaded, dict) else {}
        except Exception as e:
            logger.warning(f"解析 profiles.items 失败: {e}")
            items = {}
    if not isinstance(items, dict):
        return {}
    normalized: Dict[str, Dict[str, Any]] = {}
    for profile_id, profile in items.items():
        if isinstance(profile, dict):
            normalized[str(profile_id)] = profile
    return normalized


def _profile_label(profile_id: str, profile: Dict[str, Any]) -> str:
    return str(profile.get("display_name") or profile.get("name") or profile_id)


def _profile_description(profile: Dict[str, Any]) -> str:
    return str(profile.get("description") or profile.get("intro") or "")


def _as_text_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in re.split(r"[,，\n]+", value) if part.strip()]
    return []


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on", "是", "开启", "启用"}:
            return True
        if lowered in {"false", "0", "no", "n", "off", "否", "关闭", "禁用"}:
            return False
    return default


def _profile_generation_summary(profile: Dict[str, Any]) -> str:
    generation = profile.get("generation")
    if not isinstance(generation, dict):
        return ""
    parts = []
    if "width" in generation and "height" in generation:
        parts.append(f"尺寸={generation['width']}x{generation['height']}")
    elif generation.get("size"):
        parts.append(f"尺寸={generation['size']}")
    for key in ("steps", "cfg", "model_index", "model"):
        if key in generation:
            parts.append(f"{key}={generation[key]}")
    return "，".join(parts)


def _prompt_style_guidance(prompt_style: str) -> str:
    style = prompt_style.strip().lower()
    if style == "danbooru_tags":
        return (
            "使用 Danbooru 标签格式：英文小写 tag，用逗号分隔；多词概念使用下划线，"
            "例如 white_dress, blue_sky, looking_at_viewer；不要写完整自然语言句子"
        )
    if style == "natural_language":
        return "使用完整自然语言描述画面，写清主体、场景、构图、氛围、光照；不要写 Danbooru tag 列表"
    if style == "edit_instruction":
        return "使用明确图片编辑指令，说明要保留什么、修改什么、风格变化和限制"
    return ""


def _bot_appearance_enabled(config: Optional[dict]) -> bool:
    return bool(_get_nested(config, "bot_appearance.enabled", False))


def _bot_appearance_prompt(config: dict, prompt_style: str) -> str:
    style = prompt_style.strip().lower()
    if style == "danbooru_tags":
        prompt = str(_get_nested(config, "bot_appearance.tag_prompt", "") or "").strip()
    elif style == "edit_instruction":
        prompt = str(_get_nested(config, "bot_appearance.edit_prompt", "") or "").strip()
        if not prompt:
            prompt = str(_get_nested(config, "bot_appearance.natural_prompt", "") or "").strip()
    else:
        prompt = str(_get_nested(config, "bot_appearance.natural_prompt", "") or "").strip()
    if not prompt:
        prompt = str(_get_nested(config, "bot_appearance.prompt", "") or "").strip()
    return prompt


def _bot_appearance_text_for_action(config: Optional[dict]) -> str:
    if not _bot_appearance_enabled(config):
        return ""
    aliases = _as_text_list(_get_nested(config, "bot_appearance.aliases", []))
    trigger_phrases = _as_text_list(_get_nested(config, "bot_appearance.trigger_phrases", []))
    pieces = ["Bot 外形人设注入已启用"]
    if aliases:
        pieces.append(f"可指代 Bot 的名字/别名={', '.join(aliases)}")
    if trigger_phrases:
        pieces.append(f"典型触发说法={', '.join(trigger_phrases[:8])}")
    pieces.append(
        "当用户要画 Bot 本体、自画像、头像、立绘或形象图时，调用 generate_image 并将 use_bot_appearance 设为 true；"
        "prompt 可以描述动作、表情、场景和画风，插件会在请求上游前注入配置好的外貌参数"
    )
    return "；".join(pieces)


def _join_tag_prompt(appearance_prompt: str, raw_prompt: str, mode: str) -> str:
    appearance = appearance_prompt.strip().strip(",")
    prompt = raw_prompt.strip().strip(",")
    if not prompt:
        return appearance
    if not appearance:
        return prompt
    if mode == "append":
        return f"{prompt}, {appearance}"
    return f"{appearance}, {prompt}"


def _join_text_prompt(appearance_prompt: str, raw_prompt: str, mode: str) -> str:
    appearance = appearance_prompt.strip()
    prompt = raw_prompt.strip()
    if not prompt:
        return appearance
    if not appearance:
        return prompt
    if mode == "append":
        return f"{prompt}\nBot appearance/persona: {appearance}"
    return f"Bot appearance/persona: {appearance}\n{prompt}"


def _apply_bot_appearance(
    raw_prompt: str,
    config: dict,
    force: Optional[bool] = None,
) -> Tuple[str, bool]:
    if not _bot_appearance_enabled(config):
        return raw_prompt, False
    if force is not True:
        return raw_prompt, False
    prompt_style = str(_get_nested(config, "profile_meta.prompt_style", "") or "")
    appearance_prompt = _bot_appearance_prompt(config, prompt_style)
    if not appearance_prompt:
        return raw_prompt, False
    inject_mode = str(_get_nested(config, "bot_appearance.inject_mode", "prepend") or "prepend").strip().lower()
    if inject_mode not in {"prepend", "append"}:
        inject_mode = "prepend"
    if prompt_style.strip().lower() == "danbooru_tags":
        return _join_tag_prompt(appearance_prompt, raw_prompt, inject_mode), True
    return _join_text_prompt(appearance_prompt, raw_prompt, inject_mode), True


def _find_profile(config: dict, profile_hint: str = "") -> Tuple[str, Dict[str, Any]]:
    items = _get_profile_items(config)
    if not items:
        return "", {}
    selected = (profile_hint or str(_get_nested(config, "profiles.default_profile", ""))).strip()
    if selected:
        selected_lower = selected.lower()
        for profile_id, profile in items.items():
            aliases = [
                profile_id,
                _profile_label(profile_id, profile),
                str(profile.get("alias") or ""),
            ]
            if any(alias and alias.lower() == selected_lower for alias in aliases):
                return profile_id, profile
    first_id = next(iter(items))
    return first_id, items[first_id]


def _profile_catalog_text(config: Optional[dict]) -> str:
    items = _get_profile_items(config)
    if not items:
        return ""
    lines = []
    for profile_id, profile in items.items():
        label = _profile_label(profile_id, profile)
        description = _profile_description(profile)
        provider = str(profile.get("provider") or _get_nested(profile, "api.provider", ""))
        supported_modes = _as_text_list(profile.get("supported_modes"))
        use_cases = _as_text_list(profile.get("use_cases"))
        prompt_style = str(profile.get("prompt_style") or "")
        action_hint = str(profile.get("action_hint") or "")
        generation_summary = _profile_generation_summary(profile)
        requires_image_source = profile.get("requires_image_source")
        pieces = [f"{profile_id} / {label}"]
        if provider:
            pieces.append(f"provider={provider}")
        if supported_modes:
            pieces.append(f"支持模式={', '.join(supported_modes)}")
        if prompt_style:
            pieces.append(f"提示词风格={prompt_style}")
            guidance = _prompt_style_guidance(prompt_style)
            if guidance:
                pieces.append(f"提示词格式要求={guidance}")
        if use_cases:
            pieces.append(f"适用场景={', '.join(use_cases)}")
        if requires_image_source is not None:
            pieces.append(f"需要image_source={str(bool(requires_image_source)).lower()}")
        if generation_summary:
            pieces.append(f"关键参数={generation_summary}")
        if description:
            pieces.append(f"介绍={description}")
        if action_hint:
            pieces.append(f"给bot的使用要求={action_hint}")
        lines.append("；".join(pieces))
    return "\n".join(lines)


def _resolve_profile_config(config: dict, profile_hint: str = "") -> Tuple[dict, str, str]:
    profile_id, profile = _find_profile(config, profile_hint)
    if not profile:
        return config, "", ""
    effective = copy.deepcopy(config)
    effective.setdefault("api", {})
    effective.setdefault("generation", {})
    effective.setdefault("passthrough", {})
    api_keys = (
        "provider",
        "base_url",
        "api_key",
        "model",
        "generations_path",
        "edits_path",
        "timeout_seconds",
    )
    for key in api_keys:
        if key in profile:
            effective["api"][key] = profile[key]
    if isinstance(profile.get("api"), dict):
        effective["api"] = _merge_dict(effective["api"], profile["api"])
    generation = profile.get("generation") or profile.get("fields")
    if isinstance(generation, dict):
        effective["generation"] = _merge_dict(effective["generation"], generation)
    passthrough = profile.get("passthrough")
    if isinstance(passthrough, dict):
        effective["passthrough"] = _merge_dict(effective["passthrough"], passthrough)
    if isinstance(profile.get("params"), dict):
        effective["passthrough"]["enabled"] = True
        shared_params = dict(effective["passthrough"].get("shared_params") or {})
        shared_params.update(profile["params"])
        effective["passthrough"]["shared_params"] = shared_params
    for mode_key in ("shared_params", "text_to_image_params", "image_to_image_params"):
        if isinstance(profile.get(mode_key), dict):
            effective["passthrough"]["enabled"] = True
            current = dict(effective["passthrough"].get(mode_key) or {})
            current.update(profile[mode_key])
            effective["passthrough"][mode_key] = current
    if isinstance(profile.get("prompt"), dict):
        effective["prompt"] = _merge_dict(dict(effective.get("prompt") or {}), profile["prompt"])
    effective["profile_meta"] = {
        "id": profile_id,
        "display_name": _profile_label(profile_id, profile),
        "description": _profile_description(profile),
        "supported_modes": _as_text_list(profile.get("supported_modes")),
        "prompt_style": str(profile.get("prompt_style") or ""),
        "use_cases": _as_text_list(profile.get("use_cases")),
        "requires_image_source": bool(profile.get("requires_image_source", False)),
        "action_hint": str(profile.get("action_hint") or ""),
    }
    return effective, profile_id, _profile_label(profile_id, profile)


def _extract_profile_option(text: str) -> Tuple[str, str]:
    pattern = re.compile(r"(?:--(?:profile|preset|套餐))\s*(?:=|:)?\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))")
    match = pattern.search(text or "")
    if not match:
        return text, ""
    profile = next((group for group in match.groups() if group), "")
    cleaned = (text[: match.start()] + text[match.end() :]).strip()
    return cleaned, profile.strip()


def _extract_image_source_option(text: str) -> Tuple[str, str]:
    pattern = re.compile(
        r"(?:--(?:image-source|image_source|source|参考图|图片))\s*(?:=|:)?\s*"
        r"(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))"
    )
    match = pattern.search(text or "")
    if not match:
        return text, ""
    image_source = next((group for group in match.groups() if group), "")
    cleaned = (text[: match.start()] + text[match.end() :]).strip()
    return cleaned, image_source.strip()
