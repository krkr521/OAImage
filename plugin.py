# ruff: noqa: E402
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# MaiBot 1.0 按文件路径加载 plugin.py；本地拆分模块仍放在插件目录下。
# 热重载时清掉本插件的本地模块缓存，避免修改子模块后仍拿到旧实现。
for _module_name in (
    "oa_image_clients",
    "oa_image_config",
    "oa_image_generation",
    "oa_image_runtime",
    "oa_image_sources",
):
    sys.modules.pop(_module_name, None)

from maibot_sdk import Command, EventHandler, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import EventType

from oa_image_config import (
    _bot_appearance_text_for_action,
    _extract_image_source_option,
    _extract_profile_option,
    _get_nested,
    _profile_catalog_text,
    _to_bool,
)
from oa_image_generation import run_generation
from oa_image_runtime import (
    build_context_prompt,
    cache_images,
    cancel_command_tasks,
    has_recent_command_job,
    mark_command_job,
    track_command_task,
    update_state,
)
from oa_image_sources import (
    _extract_first_url,
    _extract_local_image_source,
    extract_images,
)


def _default_profiles() -> dict[str, dict[str, Any]]:
    return {
        "openai_default": {
            "display_name": "OpenAI 兼容默认套餐",
            "description": "适合通用文生图和按上游能力配置的图生图。",
            "supported_modes": ["text_to_image", "image_to_image"],
            "prompt_style": "natural_language",
            "use_cases": ["通用配图", "头像", "海报主视觉", "图片编辑"],
            "requires_image_source": False,
            "action_hint": (
                "prompt 使用自然语言描述画面，写清主体、场景、构图、光照、风格和用途；"
                "图生图时写清需要保留与修改的内容。"
            ),
            "provider": "openai",
            "base_url": "https://api.openai.com",
            "api_key": "sk-...",
            "model": "gpt-image-1",
            "generations_path": "/v1/images/generations",
            "edits_path": "/v1/images/edits",
            "timeout_seconds": 120,
            "generation": {
                "size": "1024x1024",
                "response_format": "b64_json",
                "image_to_image_mode": "multipart_edit",
                "json_image_field": "image",
            },
            "passthrough": {
                "enabled": False,
                "allow_override_core_params": False,
                "shared_params": {},
                "text_to_image_params": {},
                "image_to_image_params": {},
            },
        }
    }


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用插件")
    enable_action: bool = Field(default=True, description="是否注册 generate_image Tool")
    config_version: str = Field(default="1.0.0", description="配置文件版本")


class ProfilesConfig(PluginConfigBase):
    """生图套餐配置。"""

    __ui_label__ = "生图套餐"
    __ui_icon__ = "layers"
    __ui_order__ = 1

    default_profile: str = Field(default="openai_default", description="默认生图套餐 ID")
    items: dict[str, dict[str, Any]] = Field(default_factory=_default_profiles, description="生图套餐定义")


class BehaviorConfig(PluginConfigBase):
    """插件运行行为配置。"""

    __ui_label__ = "行为"
    __ui_icon__ = "sliders-horizontal"
    __ui_order__ = 2

    default_prompt: str = Field(default="Generate an image based on the current chat.", description="空提示词默认需求")
    reference_image_max_age_seconds: int = Field(default=900, description="最近参考图有效秒数")
    max_cached_images: int = Field(default=8, description="每个聊天缓存的最近图片数量")
    skip_when_busy: bool = Field(default=True, description="同一聊天已有生图任务时跳过新任务")
    dedupe_command_action: bool = Field(default=True, description="避免命令触发后 Tool 再重复生图")
    command_action_dedupe_seconds: int = Field(default=90, description="命令与 Tool 去重秒数")
    allow_local_image_source: bool = Field(default=False, description="是否允许读取插件目录内的本地参考图")
    local_image_base_dir: str = Field(default="local_images", description="允许读取的本地参考图目录")
    local_image_max_bytes: int = Field(default=10 * 1024 * 1024, description="本地参考图最大字节数")
    storage_message: bool = Field(default=True, description="是否把发送的图片写入消息库")
    send_error_messages: bool = Field(default=False, description="失败时是否向聊天发送错误文本")


class BotAppearanceConfig(PluginConfigBase):
    """Bot 外形人设配置。"""

    __ui_label__ = "Bot 外形"
    __ui_icon__ = "user-round"
    __ui_order__ = 3

    enabled: bool = Field(default=False, description="是否启用 Bot 外形人设注入")
    aliases: list[str] = Field(default_factory=lambda: ["你", "麦麦", "MaiBot", "bot"], description="Bot 名字或别名")
    trigger_phrases: list[str] = Field(
        default_factory=lambda: ["画你", "你的头像", "你的立绘", "你的自画像", "画一下麦麦"],
        description="暴露给 LLM 的典型触发说法示例",
    )
    inject_mode: str = Field(default="prepend", description="注入位置：prepend 或 append")
    tag_prompt: str = Field(default="1girl, solo, bot_persona", description="Danbooru/tag 风格外貌参数")
    natural_prompt: str = Field(default="The bot's configured character appearance.", description="自然语言外貌描述")
    edit_prompt: str = Field(
        default="Keep the generated character consistent with the bot's configured appearance.",
        description="图生图/编辑风格外貌约束",
    )


class PromptConfig(PluginConfigBase):
    """上下文提示词配置。"""

    __ui_label__ = "上下文"
    __ui_icon__ = "message-square-text"
    __ui_order__ = 4

    include_context: bool = Field(default=False, description="是否拼入聊天上下文和 bot 状态")
    context_hours: float = Field(default=2.0, description="读取最近多少小时的上下文")
    context_message_limit: int = Field(default=8, description="最多读取多少条上下文消息")
    context_template: str = Field(
        default=(
            "User image request: {prompt}\n"
            "Mode: {mode}\n"
            "Recent chat context:\n{chat_context}\n"
            "Bot state: {bot_state}\n"
            "Convert this into an image generation prompt only."
        ),
        description="上下文提示词模板",
    )


class OAImagePluginConfig(PluginConfigBase):
    """OAImage 配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    profiles: ProfilesConfig = Field(default_factory=ProfilesConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
    bot_appearance: BotAppearanceConfig = Field(default_factory=BotAppearanceConfig)
    prompt: PromptConfig = Field(default_factory=PromptConfig)


_GENERATE_IMAGE_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "图像生成提示词；必须遵循所选套餐的提示词风格和使用要求。",
        },
        "mode": {
            "type": "string",
            "enum": ["auto", "text_to_image", "image_to_image"],
            "description": "生成模式；不确定时用 auto。",
            "default": "auto",
        },
        "use_recent_image": {
            "type": "boolean",
            "description": "图生图时是否使用当前聊天最近收到的图片作为参考。",
            "default": False,
        },
        "use_bot_appearance": {
            "type": "boolean",
            "description": "当用户明确要画 Bot 本体、头像、立绘、自画像或外形图时设为 true。",
            "default": False,
        },
        "image_source": {
            "type": "string",
            "description": "可选参考图来源；OpenAI 兼容端点可使用图片 URL 或配置允许的本地图片路径。",
        },
        "profile": {
            "type": "string",
            "description": "可选：套餐 ID 或显示名；根据套餐介绍选择最适合的上游模型和参数。",
        },
    },
    "required": ["prompt"],
}

_BASE_TOOL_DESCRIPTION = (
    "生成并发送图片，支持文字生图和使用最近图片、图片 URL 或受限本地图片作为参考的图生图。"
    "当用户要求生图、画图、配图、图生图、改图、做梗图或画设定图，且你判断应该直接发图片时使用。"
    "这个 Tool 会自己发送图片，不需要先发送正在生成之类的提示。"
)


def _message_value(message: Any, key: str, default: Any = "") -> Any:
    if isinstance(message, dict):
        return message.get(key, default)
    return getattr(message, key, default)


def _message_stream_id(message: Any, fallback: str = "") -> str:
    return str(
        fallback
        or _message_value(message, "stream_id", "")
        or _message_value(message, "session_id", "")
        or ""
    )


def _message_plain_text(message: Any) -> str:
    return str(
        _message_value(message, "plain_text", "")
        or _message_value(message, "processed_plain_text", "")
        or _message_value(message, "raw_message", "")
        or ""
    )


def _message_segments(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("message_segments") or message.get("raw_message") or []
    return (
        getattr(message, "message_segments", None)
        or getattr(message, "message_segment", None)
        or getattr(message, "raw_message", None)
        or []
    )


class OAImagePlugin(MaiBotPlugin):
    """MaiBot 1.0 原生 OAImage 插件。"""

    config_model = OAImagePluginConfig

    def _effective_config(self) -> dict[str, Any]:
        config = self.get_plugin_config_data() or type(self).build_default_config()
        config.setdefault("runtime", {})["plugin_dir"] = str(_PLUGIN_DIR)
        return config

    def _tool_description(self, config: dict[str, Any]) -> str:
        parts = [_BASE_TOOL_DESCRIPTION]
        catalog = _profile_catalog_text(config)
        if catalog:
            parts.append("可用生图套餐如下，选择 profile 时优先匹配用户需求、模型能力和介绍：\n" + catalog)
        bot_appearance_text = _bot_appearance_text_for_action(config)
        if bot_appearance_text:
            parts.append(bot_appearance_text)
        return "\n\n".join(parts)

    def get_components(self) -> list[dict[str, Any]]:
        components = super().get_components()
        config = self._effective_config()
        enable_action = _to_bool(_get_nested(config, "plugin.enable_action", True), True)
        filtered_components: list[dict[str, Any]] = []
        for component in components:
            if component.get("type") == "TOOL" and component.get("name") == "generate_image":
                if not enable_action:
                    continue
                metadata = dict(component.get("metadata") or {})
                metadata["description"] = self._tool_description(config)
                metadata["brief_description"] = _BASE_TOOL_DESCRIPTION
                component = {**component, "metadata": metadata}
            filtered_components.append(component)
        return filtered_components

    async def on_load(self) -> None:
        self.ctx.logger.info("OAImage loaded")

    async def on_unload(self) -> None:
        await cancel_command_tasks()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del scope, config_data, version

    async def _send_image(self, image_base64: str, stream_id: str, *, storage_message: bool = True) -> bool:
        return bool(await self.ctx.send.image(image_base64, stream_id, storage_message=storage_message))

    async def _send_text(self, text: str, stream_id: str, *, storage_message: bool = False) -> bool:
        return bool(await self.ctx.send.text(text, stream_id, storage_message=storage_message))

    async def _build_context_prompt(self, raw_prompt: str, stream_id: str, config: dict, mode: str) -> str:
        if not _get_nested(config, "prompt.include_context", False):
            return raw_prompt
        limit = int(_get_nested(config, "prompt.context_message_limit", 8))
        hours = float(_get_nested(config, "prompt.context_hours", 2.0))
        chat_context = ""
        try:
            messages = await self.ctx.call_capability(
                "message.get_recent",
                chat_id=stream_id,
                hours=hours,
                limit=limit,
                filter_mai=False,
            )
            chat_context = await self.ctx.call_capability(
                "message.build_readable",
                messages=messages,
                replace_bot_name=True,
                timestamp_mode="relative",
                truncate=True,
                show_actions=True,
            )
        except Exception as exc:
            self.ctx.logger.debug(f"构建生图上下文失败: {exc}")
        return build_context_prompt(raw_prompt, stream_id, config, mode, chat_context=chat_context)

    async def _run_generation(
        self,
        *,
        stream_id: str,
        raw_prompt: str,
        mode: str = "auto",
        reference_image_base64: str | None = None,
        use_recent_image: bool = False,
        image_source: str = "",
        profile_name: str = "",
        use_bot_appearance: bool | None = None,
    ) -> tuple[bool, str]:
        return await run_generation(
            stream_id=stream_id,
            raw_prompt=raw_prompt,
            config=self._effective_config(),
            mode=mode,
            reference_image_base64=reference_image_base64,
            use_recent_image=use_recent_image,
            image_source=image_source,
            profile_name=profile_name,
            use_bot_appearance=use_bot_appearance,
            send_image=self._send_image,
            send_text=self._send_text,
            context_builder=self._build_context_prompt,
        )

    @Tool(
        "generate_image",
        description=_BASE_TOOL_DESCRIPTION,
        parameters=_GENERATE_IMAGE_TOOL_PARAMETERS,
    )
    async def handle_generate_image(
        self,
        prompt: str = "",
        mode: str = "auto",
        use_recent_image: bool | str | int | None = None,
        use_bot_appearance: bool | str | int | None = None,
        image_source: str = "",
        profile: str = "",
        stream_id: str = "",
        chat_id: str = "",
        reasoning: str = "",
        action_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs

        data = action_data if isinstance(action_data, dict) else {}
        resolved_prompt = str(prompt or data.get("prompt") or data.get("description") or reasoning or "").strip()
        resolved_mode = str(mode or data.get("mode") or "auto").strip() or "auto"
        resolved_stream_id = str(stream_id or chat_id or data.get("stream_id") or data.get("chat_id") or "").strip()
        resolved_image_source = str(image_source or data.get("image_source") or "").strip()
        resolved_profile = str(
            profile
            or data.get("profile")
            or data.get("profile_name")
            or data.get("profile_id")
            or ""
        ).strip()
        recent_image = _to_bool(
            use_recent_image if use_recent_image is not None else data.get("use_recent_image"),
            resolved_mode == "image_to_image",
        )
        bot_appearance = _to_bool(
            use_bot_appearance if use_bot_appearance is not None else data.get("use_bot_appearance"),
            False,
        )

        config = self._effective_config()
        if _get_nested(config, "behavior.dedupe_command_action", True):
            dedupe_seconds = int(_get_nested(config, "behavior.command_action_dedupe_seconds", 90))
            if has_recent_command_job(resolved_stream_id, dedupe_seconds):
                return {"success": True, "content": "image generation already scheduled by command"}

        success, message = await self._run_generation(
            stream_id=resolved_stream_id,
            raw_prompt=resolved_prompt,
            mode=resolved_mode,
            use_recent_image=recent_image,
            image_source=resolved_image_source,
            profile_name=resolved_profile,
            use_bot_appearance=bot_appearance,
        )
        return {"success": success, "content": message, "message": message}

    @Command(
        "draw_image",
        description="触发生图或图生图，不拦截 MaiBot 的普通回复",
        pattern=r"^(?:/(?:draw|image|生图|画图|图生图|改图)|(?:生图|画图|图生图|改图))\s*(?P<prompt>.*)$",
    )
    async def handle_draw_image(
        self,
        text: str = "",
        stream_id: str = "",
        matched_groups: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[bool, str, bool]:
        del kwargs

        if not stream_id:
            return False, "missing stream_id", False
        groups = matched_groups if isinstance(matched_groups, dict) else {}
        prompt = str(groups.get("prompt") or "").strip()
        prompt, profile_name = _extract_profile_option(prompt)
        prompt, image_source_option = _extract_image_source_option(prompt)
        command_text = text or prompt
        mode = "image_to_image" if "图生图" in command_text or "改图" in command_text else "text_to_image"
        image_source = image_source_option or _extract_first_url(command_text) or _extract_local_image_source(command_text)
        if image_source and mode == "text_to_image":
            mode = "image_to_image"

        mark_command_job(stream_id, prompt)
        task = asyncio.create_task(
            self._run_generation(
                stream_id=stream_id,
                raw_prompt=prompt,
                mode=mode,
                use_recent_image=mode == "image_to_image",
                image_source=image_source,
                profile_name=profile_name,
            )
        )
        track_command_task(task, stream_id)
        return True, "", False

    @EventHandler(
        "image_cache_on_message",
        description="缓存最近收到的图片，供图生图 Tool 使用",
        event_type=EventType.ON_MESSAGE,
        intercept_message=False,
        weight=0,
    )
    async def handle_image_cache_on_message(self, message: Any = None, **kwargs: Any) -> dict[str, Any]:
        del kwargs

        stream_id = _message_stream_id(message)
        if stream_id:
            plain_text = _message_plain_text(message)
            update_state(stream_id, "received_message", plain_text[:80])
            images = extract_images(_message_segments(message))
            max_cache = int(_get_nested(self._effective_config(), "behavior.max_cached_images", 8))
            cache_images(stream_id, images, max_items=max_cache, source="on_message")
        return {"continue_processing": True}

    @EventHandler("image_state_on_plan", description="记录 bot 正在规划", event_type=EventType.ON_PLAN)
    async def handle_state_on_plan(self, message: Any = None, **kwargs: Any) -> dict[str, Any]:
        del kwargs

        if stream_id := _message_stream_id(message):
            update_state(stream_id, "planning", "")
        return {"continue_processing": True}

    @EventHandler("image_state_after_llm", description="记录 bot 刚完成 LLM 生成", event_type=EventType.AFTER_LLM)
    async def handle_state_after_llm(self, message: Any = None, **kwargs: Any) -> dict[str, Any]:
        del kwargs

        if stream_id := _message_stream_id(message):
            content = str(_message_value(message, "llm_response_content", "") or "")
            update_state(stream_id, "llm_done", content[:80])
        return {"continue_processing": True}

    @EventHandler("image_state_post_send", description="记录 bot 正在发送消息", event_type=EventType.POST_SEND)
    async def handle_state_post_send(self, message: Any = None, **kwargs: Any) -> dict[str, Any]:
        del kwargs

        if stream_id := _message_stream_id(message):
            update_state(stream_id, "sending_message", _message_plain_text(message)[:80])
        return {"continue_processing": True}

    @EventHandler("image_state_after_send", description="记录 bot 已发送消息", event_type=EventType.AFTER_SEND)
    async def handle_state_after_send(self, message: Any = None, **kwargs: Any) -> dict[str, Any]:
        del kwargs

        if stream_id := _message_stream_id(message):
            update_state(stream_id, "sent_message", _message_plain_text(message)[:80])
        return {"continue_processing": True}

    @EventHandler("image_cancel_command_tasks", description="停止插件命令触发的后台生图任务", event_type=EventType.ON_STOP)
    async def handle_cancel_command_tasks(self, message: Any = None, **kwargs: Any) -> dict[str, Any]:
        del message, kwargs

        await cancel_command_tasks()
        return {"continue_processing": True}


def create_plugin() -> OAImagePlugin:
    return OAImagePlugin()
