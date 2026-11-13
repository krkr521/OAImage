"""通过宿主的当前会话图片与消息接口读取参考原图。"""

from typing import Any

import base64
import re
import time

from oa_image_runtime import get_latest_cached_image


class ReferenceImageError(ValueError):
    """可安全向原会话说明的参考图错误。"""


def capability_value(value: Any, key: str) -> Any:
    """解包 SDK 结果；宿主明确失败时停止，不替换为另一条消息。"""
    for _ in range(4):
        if not isinstance(value, dict):
            return value
        if value.get("success") is False:
            raise ReferenceImageError("读取参考图片失败，请重新发送原图或指定当前会话中带图的消息编号。")
        if key in value:
            return value[key]
        # SDK 的 message.get_by_id 会直接返回解包后的消息字典。
        if key == "message" and {"message_id", "session_id", "raw_message"}.issubset(value):
            return value
        if value.get("success") is True and "result" in value:
            value = value["result"]
        else:
            break
    raise ReferenceImageError("宿主参考图片响应格式异常，未启动图片生成。")


def image_data_url(value: str) -> str:
    """验证图片字节并保留真实 MIME，避免把图片描述当作 Base64。"""
    raw = value.split(",", 1)[1] if value.startswith("data:") and "," in value else value
    try:
        data = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        raise ReferenceImageError("参考图片数据不可读取，请重新发送原图。") from None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ReferenceImageError("参考内容不是可用原图，请发送 PNG、JPEG、WebP 或 GIF 图片。")
    return f"data:{mime};base64,{raw}"


def original_images(message: dict[str, Any]) -> list[dict[str, Any]]:
    """普通图片保留原始序号；表情包和回复里的文本不作为参考图片。"""
    segments = message.get("raw_message")
    if not isinstance(segments, list):
        return []
    return [part for part in segments if isinstance(part, dict) and part.get("type") == "image"]


async def stored_reference(ctx: Any, stream_id: str, message_id: str, index: int) -> str:
    """按同会话的真实消息编号读取持久原图，不依赖内存上下文。"""
    response = await ctx.call_capability(
        "message.get_by_id", message_id=message_id, chat_id=stream_id, include_binary_data=True,
    )
    message = capability_value(response, "message")
    if not isinstance(message, dict):
        raise ReferenceImageError("指定消息不在当前会话的消息记录中，请确认参考消息编号。")
    if message.get("session_id") != stream_id or message.get("message_id") != message_id:
        raise ReferenceImageError("参考图片与当前会话不匹配，已停止读取。")
    images = original_images(message)
    if index >= len(images):
        raise ReferenceImageError("指定消息内没有这个序号的图片，请检查参考图片序号。")
    value = images[index].get("binary_data_base64")
    if not isinstance(value, str) or not value:
        raise ReferenceImageError("指定图片的原始数据不可读取，请重新发送原图。")
    return image_data_url(value)


async def resolve_reference(
    ctx: Any, stream_id: str, *, message_id: str = "", index: int = 0, max_age_seconds: int = 900,
) -> str | None:
    """真实 QQ 消息从持久记录读取；短编号和工具媒体仍由当前上下文解析。"""
    if not stream_id:
        raise ReferenceImageError("缺少当前会话，未读取参考图片。")
    if type(index) is not int or not 0 <= index < 8:
        raise ReferenceImageError("参考图片序号应为 0～7 的整数。")
    if message_id:
        # 摘要可能保留真实消息编号而移除原消息。编号明确时直接读同会话记录，
        # 不改用最近图；短编号和 tool_result 媒体不能当作数据库消息编号查询。
        if re.fullmatch(r"-?[0-9]+", message_id):
            return await stored_reference(ctx, stream_id, message_id, index)
        response = await ctx.call_capability(
            "maisaka.context.resolve_image", stream_id=stream_id, source_id=message_id, index=index,
        )
        image = capability_value(response, "image")
        if not isinstance(image, dict) or not isinstance(image.get("binary_data_base64"), str):
            raise ReferenceImageError("指定消息中没有可读取的原图，请确认消息编号和图片序号。")
        return image_data_url(image["binary_data_base64"])
    if index == 0 and (cached := get_latest_cached_image(stream_id, max_age_seconds)):
        return image_data_url(cached)
    response = await ctx.call_capability(
        "message.get_recent", chat_id=stream_id, limit=200, hours=max_age_seconds / 3600,
        filter_mai=True, include_binary_data=False,
    )
    messages = capability_value(response, "messages")
    if not isinstance(messages, list):
        raise ReferenceImageError("当前会话的参考图片列表格式异常，未启动图片生成。")
    candidates: list[tuple[float, dict[str, Any]]] = []
    cutoff = time.time() - max_age_seconds
    for item in messages:
        if not isinstance(item, dict) or item.get("session_id") != stream_id or not original_images(item):
            continue
        try:
            timestamp = float(item.get("timestamp", 0))
        except (ValueError, TypeError):
            continue
        if cutoff <= timestamp <= time.time() + 5 and item.get("message_id"):
            candidates.append((timestamp, item))
    if not candidates:
        return None
    latest = max(candidates, key=lambda item: item[0])[1]
    return await stored_reference(ctx, stream_id, latest["message_id"], index)
