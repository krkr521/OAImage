"""GIF 命令参数、套餐选择和逐请求配置；不修改共享配置。"""

from copy import deepcopy
from typing import Any

import base64
import re

from oa_image_config import _dict_config, _get_nested, _merge_passthrough_params, _resolve_profile_config


GIF_MODELS = ("gpt-image-2.6-sunburst", "gpt-image-2.6-flare")
GIF_MAX_FRAMES = 16
GIF_UNSUPPORTED_FIELDS = {"stream", "partial_images", "output_compression", "prompt_rewriter"}


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name}应为 {minimum}～{maximum} 的整数")
    return value


def gif_parameters(frame_count: Any = 9, frame_duration_ms: Any = 200) -> dict[str, int]:
    """校验帧数和帧间隔；按官方协议把间隔四舍五入到 10 毫秒。"""
    count = _integer(frame_count, "GIF 帧数", 2, GIF_MAX_FRAMES)
    duration = _integer(frame_duration_ms, "GIF 每帧毫秒数", 20, 2000)
    return {"frame_count": count, "frame_duration_ms": ((duration + 5) // 10) * 10}


def resolve_output_config(
    config: dict[str, Any], profile_name: str = "", *, output_format: str = "auto",
    frame_count: int | None = None, frame_duration_ms: int | None = None,
) -> tuple[dict[str, Any], str, str]:
    """GIF 默认选动画套餐；静态请求继续使用既有套餐及参数。"""
    if not isinstance(output_format, str) or output_format.casefold() not in {"auto", "gif"}:
        raise ValueError("output_format 只支持 auto（普通图片）或 gif（动图）")
    requested_gif = output_format.casefold() == "gif"
    if requested_gif and not profile_name:
        profile_name = str(_get_nested(config, "animation.profile", "openai_default"))
        if not profile_name or profile_name not in _get_nested(config, "profiles.items", {}):
            raise ValueError("GIF 套餐尚未配置，请检查 animation.profile")
    effective, profile_id, label = _resolve_profile_config(config, profile_name)
    if requested_gif and profile_name:
        profile = _get_nested(config, f"profiles.items.{profile_id}", {})
        aliases = {str(value).casefold() for value in (profile_id, label, profile.get("alias", "")) if value}
        if profile_name.casefold() not in aliases:
            raise ValueError("指定的 GIF 套餐不存在，请检查 profile 名称")
    params = _merge_passthrough_params(effective, {}, "text_to_image")
    is_gif = requested_gif or str(params.get("output_format", "")).casefold() == "gif"
    if not is_gif:
        # Tool 的平铺参数可能带齐动画默认值；输出格式决定分支，静态图不使用帧参数。
        # 命令里显式误用 --frames 等选项，仍由 extract_animation_options 给出用法错误。
        return effective, profile_id, label
    if _get_nested(effective, "api.provider", "openai") != "openai" or not str(
        _get_nested(effective, "api.model", "")
    ).startswith(GIF_MODELS):
        raise ValueError("GIF 动图需要 OpenAI GPT Image 2.6 Sunburst 或 Flare 套餐；NovelAI 不支持")
    count = frame_count if frame_count is not None else params.get(
        "frame_count", _get_nested(config, "animation.frame_count", 9),
    )
    duration = frame_duration_ms if frame_duration_ms is not None else params.get(
        "frame_duration_ms", _get_nested(config, "animation.frame_duration_ms", 200),
    )
    animation = gif_parameters(count, duration)
    effective = deepcopy(effective)
    passthrough = effective.setdefault("passthrough", {})
    passthrough["enabled"] = True
    for section in ("shared_params", "text_to_image_params", "image_to_image_params"):
        values = _dict_config(effective, "passthrough." + section)
        for key in GIF_UNSUPPORTED_FIELDS:
            values.pop(key, None)
        passthrough[section] = values
    passthrough["text_to_image_params"].update(output_format="gif", **animation)
    passthrough["image_to_image_params"].update(output_format="gif", **animation)
    effective.setdefault("runtime", {}).update(output_format="gif", **animation)
    return effective, profile_id, label


def prepare_gif_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """按实际输出格式保留适用参数，GIF 才校验帧数与动画能力。"""
    if str(payload.get("output_format", "")).casefold() != "gif":
        return {key: value for key, value in payload.items() if key not in {"frame_count", "frame_duration_ms"}}
    if not str(payload.get("model", "")).startswith(GIF_MODELS):
        raise ValueError("GIF 动图需要 GPT Image 2.6 Sunburst 或 Flare")
    if payload.get("mask") is not None:
        raise ValueError("GIF 输出不支持蒙版，请使用静态参考图与文字描述")
    result = {key: value for key, value in payload.items() if key not in GIF_UNSUPPORTED_FIELDS and key != "mask"}
    result.update(output_format="gif", **gif_parameters(payload.get("frame_count", 9), payload.get("frame_duration_ms", 200)))
    return result


def validate_gif_result(image_base64: str) -> None:
    """只检查文件头，不解码全部动画或改写图片字节。"""
    try:
        signature = base64.b64decode(image_base64[:24], validate=True)[:6]
    except (ValueError, TypeError) as exc:
        raise ValueError("上游未返回有效的 GIF 动图数据") from exc
    if signature not in {b"GIF87a", b"GIF89a"}:
        raise ValueError("请求了 GIF，但上游返回了其他图片格式，未按动图发送")


def extract_animation_options(text: str, *, gif_command: bool = False) -> tuple[str, dict[str, Any]]:
    """提取命令参数，阻止无效参数混进画面提示词。"""
    options: dict[str, Any] = {"output_format": "gif" if gif_command else "auto"}
    seen: set[str] = set()

    def boolean(match: re.Match[str]) -> str:
        if "gif" in seen:
            raise ValueError("--gif 不能重复指定")
        seen.add("gif")
        options["output_format"] = "gif"
        return ""

    cleaned = re.sub(r"(?<!\S)--gif(?=\s|$)", boolean, text, flags=re.IGNORECASE)
    names = {"frames": "frame_count", "frame-count": "frame_count", "帧数": "frame_count",
             "frame-duration-ms": "frame_duration_ms", "frame-ms": "frame_duration_ms", "帧间隔": "frame_duration_ms"}

    def numeric(match: re.Match[str]) -> str:
        key = names[match["name"].casefold()]
        if key in seen:
            raise ValueError("GIF 帧参数不能重复指定")
        seen.add(key)
        if not re.fullmatch(r"[0-9]+", match["value"]):
            raise ValueError("GIF 帧参数必须是整数")
        options[key] = int(match["value"])
        return ""

    numeric_names = "|".join(re.escape(name) for name in names)
    cleaned = re.sub(
        rf"(?<!\S)--(?P<name>{numeric_names})(?:=|\s+)(?P<value>\S+)", numeric, cleaned, flags=re.IGNORECASE,
    )
    if re.search(rf"(?<!\S)--(?:gif|{numeric_names})(?=$|[=\s])", cleaned, flags=re.IGNORECASE):
        raise ValueError("GIF 选项缺少参数；例如 --frames 9 --frame-ms 200")
    if options["output_format"] == "gif":
        checked = gif_parameters(options.get("frame_count", 9), options.get("frame_duration_ms", 200))
        for key in ("frame_count", "frame_duration_ms"):
            if key in options:
                options[key] = checked[key]
    elif len(options) > 1:
        raise ValueError("帧数和帧间隔只适用于 GIF，请使用 /动图 或 /生图 --gif")
    return cleaned.strip(), options
