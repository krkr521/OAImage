# ruff: noqa: E402
from __future__ import annotations

import asyncio
import random
import re
import sys
import uuid
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# MaiBot 1.0 按文件路径加载 plugin.py；本地拆分模块仍放在插件目录下。
# 热重载时清掉本插件的本地模块缓存，避免修改子模块后仍拿到旧实现。
for _module_name in (
    "oa_image_animation",
    "oa_image_clients",
    "oa_image_character_library",
    "oa_image_config",
    "oa_image_context",
    "oa_image_generation",
    "oa_image_feedback",
    "oa_image_reference",
    "oa_image_novelai",
    "oa_image_novelai_prompt",
    "oa_image_prompt",
    "oa_image_openai_prompt",
    "oa_image_prompt_types",
    "oa_image_skill",
    "oa_image_tag_search",
    "oa_image_runtime",
    "oa_image_sources",
    "usage_accounting",
):
    sys.modules.pop(_module_name, None)

from maibot_sdk import Command, EventHandler, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, EventType, HookMode, HookOrder

if __package__:
    from .usage_accounting import record_usage
else:
    from usage_accounting import record_usage

from oa_image_config import (
    _bot_appearance_text_for_action,
    _extract_image_source_option,
    _extract_profile_option,
    _get_nested,
    _profile_catalog_text,
    _resolve_profile_config,
    _to_bool,
)
from oa_image_generation import run_generation
from oa_image_animation import GIF_MAX_FRAMES, extract_animation_options, resolve_output_config
from oa_image_context import ScheduledImageContext
from oa_image_feedback import GenerationFeedback
from oa_image_reference import image_data_url, original_images, resolve_reference
from oa_image_prompt import rewrite_image_prompt
from oa_image_prompt_types import NovelAIPrompt
from oa_image_runtime import (
    build_context_prompt,
    cache_images,
    cancel_command_tasks,
    cleanup_runtime_state_from_config,
    finish_command_job,
    has_recent_command_job,
    mark_command_job,
    track_command_task,
    update_state,
)
from oa_image_sources import (
    _extract_first_url,
    _extract_local_image_source,
)


def _default_novelai_profile() -> dict[str, Any]:
    """提供可直接填写 Token 的独立套餐，不改变现有默认套餐。"""
    return {
        "display_name": "NovelAI",
        "description": "NovelAI V5 Full 普通文生图，使用 novelai-assistant 技能和 DeepSeek 整理当前会话的相关上下文。",
        "supported_modes": ["text_to_image"],
        "prompt_style": "novelai_v5",
        "use_cases": ["二次元插画", "头像", "场景配图"],
        "requires_image_source": False,
        "action_hint": "固定 V5 Full 文字生图，不使用参考图片或角色库原图。DeepSeek 会先选择关键词查询 NovelAI 官方标签库，再据真实结果整理提示词。图片编辑请选择其他套餐。",
        "provider": "novelai",
        "base_url": "https://image.novelai.net",
        "api_key": "",
        "model": "nai-diffusion-5-full",
        "timeout_seconds": 180,
        "generation": {
            "size": "832x1216", "steps": 28, "scale": 5.0,
            "sampler": "k_euler_ancestral", "noise_schedule": "karras", "seed": -1,
            "negative_prompt": "lowres, bad_anatomy, blurry, watermark", "max_concurrency": 1,
            # auto 沿用整理模型给出的 rating；填 general/sensitive/questionable/explicit 可强制整套餐尺度。
            "rating": "auto",
            # 仅在需要过滤成人内容时置 true，默认不把 nsfw/nude 写进 UC。
            "ban_explicit_tags": False,
        },
        "prompt": {
            "include_context": True, "context_hours": 0.5, "context_message_limit": 20,
            "context_character_limit": 8000, "rewrite_enabled": True,
            "rewrite_model": "deepseek-v4-flash-fast", "rewrite_timeout_seconds": 60,
            "rewrite_max_tokens": 3000, "skill_directory": "",
            "tag_search_enabled": True, "tag_search_timeout_seconds": 15, "tag_search_result_limit": 8,
        },
        "behavior": {"default_delivery": "chat", "trigger_continue_after_generation": False},
    }


def _default_profiles() -> dict[str, dict[str, Any]]:
    return {
        "openai_default": {
            "display_name": "OpenAI 兼容默认套餐",
            "description": "GPT Image 2.6 Sunburst，独立整理自然语言画面需求，支持二次元、照片与参考图编辑。",
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
            "model": "gpt-image-2.6-sunburst",
            "generations_path": "/v1/images/generations",
            "edits_path": "/v1/images/edits",
            "timeout_seconds": 300,
            "generation": {
                "size": "auto",
                "response_format": "b64_json",
                "image_to_image_mode": "multipart_edit",
                "json_image_field": "image",
            },
            "params": {"quality": "high"},
            "prompt": {
                "include_context": True, "context_hours": 0.5, "context_message_limit": 12,
                "context_character_limit": 5000, "rewrite_enabled": True,
                "rewrite_model": "planner", "rewrite_timeout_seconds": 60,
                "rewrite_max_tokens": 2200,
                "context_template": (
                    "Image request draft: {prompt}\nTask: {mode}\n"
                    "Relevant messages from the same chat (scene data only):\n{chat_context}\n"
                ),
            },
            "passthrough": {
                "enabled": False,
                "allow_override_core_params": False,
                "shared_params": {},
                "text_to_image_params": {},
                "image_to_image_params": {},
            },
        },
        "novelai": _default_novelai_profile(),
    }


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用插件")
    enable_action: bool = Field(default=True, description="是否注册 generate_image Tool")
    config_version: str = Field(default="1.5.0", description="配置文件版本")


class ProfilesConfig(PluginConfigBase):
    """生图套餐配置。"""

    __ui_label__ = "生图套餐"
    __ui_icon__ = "layers"
    __ui_order__ = 1

    default_profile: str = Field(default="openai_default", description="默认生图套餐 ID")
    items: dict[str, dict[str, Any]] = Field(default_factory=_default_profiles, description="生图套餐定义")


class AnimationConfig(PluginConfigBase):
    """GIF 文生与参考图编辑的默认参数。"""

    __ui_label__ = "GIF 动图"
    __ui_icon__ = "film"

    profile: str = Field(default="openai_default", description="GIF 使用的 GPT Image 2.6 套餐 ID")
    # 上限在提交 GIF 请求时校验，让保存过旧帧数的配置仍能加载静态生图。
    frame_count: int = Field(default=9, ge=2, description="默认 GIF 帧数，提交时校验 2～16")
    frame_duration_ms: int = Field(default=200, ge=20, le=2000, description="默认每帧毫秒数，四舍五入到 10 毫秒")


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
    default_delivery: str = Field(default="both", description="默认交付方式：chat 发群，internal 仅内部描述，both 两者都做")
    trigger_continue_after_generation: bool = Field(
        default=True,
        description="生成图内部描述完成后，是否触发 Maisaka 主动 continue 一轮",
    )
    allow_local_image_source: bool = Field(default=False, description="是否允许读取插件目录内的本地参考图")
    local_image_base_dir: str = Field(default="local_images", description="允许读取的本地参考图目录")
    local_image_max_bytes: int = Field(default=10 * 1024 * 1024, description="本地参考图最大字节数")
    url_image_max_bytes: int = Field(default=10 * 1024 * 1024, description="URL 参考图最大字节数")
    allow_non_image_url_content_type: bool = Field(default=False, description="是否允许 URL 响应缺少 image/* 类型")
    runtime_state_ttl_seconds: int = Field(default=24 * 3600, description="运行时缓存按 stream 清理的 TTL")
    max_runtime_streams: int = Field(default=1024, description="最多保留多少个 stream 的运行时状态")
    context_rating_allow: list[str] = Field(
        default_factory=lambda: ["general", "sensitive"],
        description="/生图r 中允许写回内部上下文的评分；general、sensitive 视为非成人画面",
    )
    storage_message: bool = Field(default=True, description="是否把发送的图片写入消息库")
    send_error_messages: bool = Field(default=False, description="失败时是否向聊天发送错误文本")


class CharacterLibraryConfig(PluginConfigBase):
    """角色知识私库的生图参考设置。"""

    __ui_label__ = "角色库参考图"
    __ui_icon__ = "users"
    __ui_order__ = 5

    enabled: bool = Field(default=False, description="允许生图读取角色知识私库中已登记的参考图")
    auto_match: bool = Field(default=True, description="未传 characters 时按本次画图需求匹配角色名和别名")
    max_characters: int = Field(default=8, ge=1, le=8, description="单次最多引用的角色数")
    personas: dict[str, str] = Field(default_factory=dict, description="人物昵称到角色名的绘图设定映射")


class BotAppearanceConfig(PluginConfigBase):
    """Bot 外形人设配置。"""

    __ui_label__ = "Bot 外形"
    __ui_icon__ = "user-round"
    __ui_order__ = 3

    enabled: bool = Field(default=False, description="是否启用 Bot 外形人设注入")
    reference_image: str = Field(default="", description="Bot 外形参考图文件名，位于 local_images 目录内")
    aliases: list[str] = Field(default_factory=lambda: ["你", "麦麦", "MaiBot", "bot"], description="Bot 名字或别名")
    trigger_phrases: list[str] = Field(
        default_factory=lambda: ["画你", "你的头像", "你的立绘", "你的自画像", "画一下麦麦"],
        description="暴露给 LLM 的典型触发说法示例",
    )
    inject_mode: str = Field(default="prepend", description="注入位置：prepend 或 append")
    tag_prompt: str = Field(default="1girl, solo, bot_persona", description="Danbooru/tag 风格外貌参数")
    natural_prompt: str = Field(
        default=(
            "Use the provided reference image as the source for the character identity. Preserve the recognizable "
            "face, hair color, core hairstyle silhouette, eye color, body silhouette, age impression, and overall "
            "personality. Clothing, accessories, hairstyle details, props, and palette accents may change naturally "
            "to fit the requested scene, season, activity, mood, and composition. Keep enough visual continuity that "
            "the character is still clearly the same person, but do not force the exact original outfit or ornaments "
            "unless the user asks for them."
        ),
        description="自然语言外貌描述",
    )
    edit_prompt: str = Field(
        default=(
            "Use the provided reference image as the source for the character identity. Preserve the recognizable "
            "face, hair color, core hairstyle silhouette, eye color, body silhouette, age impression, and overall "
            "personality. Clothing, accessories, hairstyle details, props, and palette accents may change naturally "
            "to fit the requested scene, season, activity, mood, and composition. Keep enough visual continuity that "
            "the character is still clearly the same person, but do not force the exact original outfit or ornaments "
            "unless the user asks for them."
        ),
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
    context_character_limit: int = Field(default=8000, ge=500, le=30000, description="发送给提示词模型的上下文字符上限")
    rewrite_enabled: bool = Field(default=False, description="使用独立模型整理图片提示词；NovelAI 套餐固定执行此步骤")
    rewrite_model: str = Field(default="planner", description="planner 跟随宿主 Planner 配置；其他值为宿主中的具体模型名称")
    rewrite_timeout_seconds: int = Field(default=60, ge=5, le=180, description="提示词整理超时秒数")
    rewrite_max_tokens: int = Field(default=3000, ge=500, le=6000, description="V5 结构化提示词的最大输出 token 数")
    skill_directory: str = Field(default="", description="安装锁定提交并含 SOURCE.json 的 novelai-assistant 目录")
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
    animation: AnimationConfig = Field(default_factory=AnimationConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
    bot_appearance: BotAppearanceConfig = Field(default_factory=BotAppearanceConfig)
    character_library: CharacterLibraryConfig = Field(default_factory=CharacterLibraryConfig)
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
        "output_format": {
            "type": "string", "enum": ["auto", "gif"], "default": "auto",
            "description": "用户明确要求 GIF、动图或循环动画时设 gif，可文生或用静态参考图编辑。普通静态图片用 auto。",
        },
        "frame_count": {
            "type": ["integer", "null"], "minimum": 2, "maximum": GIF_MAX_FRAMES, "default": None,
            "description": "仅 GIF：用户指定帧数时填写，否则留空或 null，读取配置默认 9。普通静态图必须省略或传 null。",
        },
        "frame_duration_ms": {
            "type": ["integer", "null"], "minimum": 20, "maximum": 2000, "default": None,
            "description": "仅 GIF：用户指定播放速度时填写，否则留空或 null，读取配置默认 200 毫秒。普通静态图必须省略或传 null。",
        },
        "use_recent_image": {
            "type": "boolean",
            "description": "图生图时使用当前聊天最近收到的普通图片；已知目标图的 msg_id 时优先填写 reference_message_id，避免选到其他图片。",
            "default": False,
        },
        "reference_message_id": {
            "type": "string",
            "description": "仅在本次请求指定聊天图片作为编辑、服装、画风或构图来源时填写其当前会话消息编号，支持宿主短编号。只点名库内角色时用 characters，不重复绑定旧设定图消息。不能填请求文字消息的编号，不能猜测编号。",
        },
        "reference_image_index": {
            "type": "integer", "minimum": 0, "maximum": 7, "default": 0,
            "description": "参考消息内的图片序号，从 0 开始。",
        },
        "use_bot_appearance": {
            "type": "boolean",
            "description": (
                "Set true when drawing the bot itself, avatar, portrait, standee, self-image, or character image. "
                "When true, preserve the character identity from the reference image, especially face, hair color, "
                "core hairstyle silhouette, eye color, body silhouette, age impression, and personality. Clothing, "
                "accessories, hairstyle details, props, and palette accents may change to fit the requested scene, "
                "season, activity, mood, and composition. Do not force the original outfit unless the user asks for it. "
                "For GIF, select image_to_image to use the configured still portrait as the animation reference."
            ),
            "default": False,
        },
        "image_source": {
            "type": "string",
            "description": "可选参考图来源；OpenAI 兼容端点可使用图片 URL 或配置允许的本地图片路径。",
        },
        "characters": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
            "description": (
                "画面中需要使用角色知识私库设定图的角色名或 character://角色名 句柄，支持多人，"
                "例如 [DeepSeek, GLM]。用户点名已登记人物时优先使用其名字、别名或 QQ 号查询并填写，"
                "直接引用已保存原图，无需再设置旧设定图的 reference_message_id 或 use_recent_image。"
                "Q版可用 character://DeepSeek?variant=chibi。未找到参考图时只保留角色名，不能声称看过设定图。"
                "要让 GIF 使用库内设定图时选 image_to_image，并将 use_recent_image 设为 false。"
            ),
        },
        "profile": {
            "type": "string",
            "description": "可选：套餐 ID 或显示名；根据套餐介绍选择最适合的上游模型和参数。",
        },
        "delivery": {
            "type": "string",
            "enum": ["chat", "internal", "both"],
            "description": (
                "Image delivery mode. chat sends the image to the current chat. internal does not send to chat and "
                "only builds an internal VLM description/status. both sends to chat and also builds the internal description."
            ),
            "default": "both",
        },
    },
    "required": ["prompt"],
}

_BASE_TOOL_DESCRIPTION = (
    "Strict positive trigger only: use this tool only when the user explicitly asks to generate, draw, create, "
    "edit, modify, image-to-image, redraw, make a meme, or design a new generated work. "
    "Do not select this tool from broad retrieval/viewing/sending requests. "
    "生成并发送生图结果，支持文字生图、GIF 动图和使用最近参考、URL 或受限本地参考作为输入的图生图。"
    "只有当用户明确要求生图、生成、画图、绘制、图生图、改图、图片编辑、做梗图、画设定图或生成动图时才使用。"
    "用户要求 GIF、动态表情、循环动图或短循环动画时，设置 output_format=gif，在 prompt 写清主体、动作和循环。"
    "纯文字动图选 mode=text_to_image。指定静态参考图生成动图时选 mode=image_to_image，并填写对应的参考图参数。"
    "普通静态图用 output_format=auto，frame_count、frame_duration_ms 应省略或传 null。"
    "这个 Tool 会自己发送图片，不需要先发送正在生成之类的提示。"
)

# 明确要求延续上一张时，历史生图指令才作为画面资料保留。
_CONTINUATION_PATTERN = re.compile(
    r"接着|继续|上一张|上一幅|上一条|刚才那|刚才的|同上|再画|重画|同一张|沿用|保持原样|"
    r"same as (?:the )?(?:last|previous)|previous (?:one|image)|continue (?:from|the)|change the last",
    re.IGNORECASE,
)
# 兜底识别生图指令文本：旧数据可能没有 is_command 标记。
_IMAGE_COMMAND_PATTERN = re.compile(
    r"(?:^|\s)/(?:生图r?|画图|图生图|改图|动图|gif|draw|image)(?=\s|$)|^(?:生图|画图|图生图|改图)(?=\s|$)",
    re.IGNORECASE,
)


def _is_previous_image_command(message: Any) -> bool:
    """判断一条历史消息是否为生图指令，避免把上一张的要求带进本次画面。"""
    if not isinstance(message, dict):
        return False
    if message.get("is_command"):
        return True
    return bool(_IMAGE_COMMAND_PATTERN.search(str(message.get("processed_plain_text") or "")))


def _drop_previous_image_commands(messages: list[Any], request_text: str) -> list[Any]:
    """本次没有明确延续意图时，把历史生图指令从上下文里摘掉。"""
    if _CONTINUATION_PATTERN.search(str(request_text or "")):
        return messages
    return [message for message in messages if not _is_previous_image_command(message)]


# /生图r 收到指令后只回一句确认，结果不再进入模型上下文。
_SUBMIT_ACK_TEXTS = (
    "收到，画着呢，好了一张一张发你。",
    "好，这就画，稍等。",
    "收到，排队画了，出图直接发。",
    "行，马上给你画。",
    "了解，画好直接发过来。",
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


def _capability_value(value: Any, key: str) -> Any:
    """兼容 SDK 已拆包结果与 RPC 信封；能力调用失败时停止准备流程。"""
    for _ in range(3):
        if not isinstance(value, dict):
            return value
        if value.get("success") is not True:
            raise ValueError("读取聊天上下文的能力调用失败")
        if key in value:
            return value[key]
        value = value.get("result")
    raise ValueError("聊天上下文响应结构无效")


class OAImagePlugin(MaiBotPlugin):
    """MaiBot 1.0 原生 OAImage 插件。"""

    config_model = OAImagePluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._command_context = ScheduledImageContext()
        self._generation_feedback = GenerationFeedback()

    @HookHandler(
        "chat.command.after_execute",
        name="image_remember_queued_command",
        description="仅记录已成功提交且继续主链的 NovelAI 指令，供模型上下文注明异步处理",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def remember_queued_command(
        self, message: Any = None, plugin_id: str = "", command_name: str = "",
        matched_groups: dict[str, Any] | None = None, success: bool = False,
        response: str | None = None, continue_process: bool = False,
        intercept_message_level: int = 0, **kwargs: Any,
    ) -> None:
        del kwargs
        if (
            plugin_id != "a-dawn.oaimage" or command_name != "draw_image"
            or not success or not continue_process or intercept_message_level or response
        ):
            return
        groups = matched_groups or {}
        if not groups.get("novelai"):
            _, profile_name = _extract_profile_option(str(groups.get("prompt") or ""))
            if not profile_name:
                return
            profile_config, _, _ = _resolve_profile_config(self._effective_config(), profile_name)
            if _get_nested(profile_config, "api.provider", "") != "novelai":
                return
        self._command_context.remember(message)

    def _annotate_queued_commands(self, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        items = kwargs.get("items")
        session_id = str(kwargs.get("session_id") or kwargs.get("stream_id") or "")
        annotated = self._command_context.annotate(items, session_id)
        annotated = self._generation_feedback.annotate(annotated, session_id)
        if annotated is items:
            return None
        kwargs["items"] = annotated
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.planner.before_request", name="image_planner_background_note",
        description="向规划器同步异步生图的提交说明与真实任务状态",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, error_policy=ErrorPolicy.SKIP,
    )
    async def annotate_planner_commands(self, **kwargs: Any) -> dict[str, Any] | None:
        return self._annotate_queued_commands(kwargs)

    @HookHandler(
        "maisaka.replyer.before_model_request", name="image_replyer_background_note",
        description="向回复模型同步真实生图状态，防止失败后仍声称正在生成",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, error_policy=ErrorPolicy.SKIP,
    )
    async def annotate_replyer_commands(self, **kwargs: Any) -> dict[str, Any] | None:
        return self._annotate_queued_commands(kwargs)

    def _context_note_allowed(self, rating: Any) -> bool:
        """按 behavior.context_rating_allow 判断本次评级能否写回内部上下文。"""
        configured = _get_nested(self._effective_config(), "behavior.context_rating_allow", ["general", "sensitive"])
        if not isinstance(configured, list):
            return False
        allowed = {str(item).strip().casefold() for item in configured if str(item).strip()}
        return str(rating or "").strip().casefold() in allowed

    async def _append_context_note(self, stream_id: str, *, prompt: str, rating: str) -> None:
        """把非成人结果压缩成一行写进内部历史，供后续对话自然提起。"""
        text = f"【已生成图片】rating:{rating or 'general'}；画面：{prompt[:160] or '未记录'}"
        try:
            result = await self.ctx.call_capability(
                "maisaka.context.append", stream_id=stream_id,
                segments=[{"type": "text", "data": text}], visible_text=text,
                source_kind="oaimage.generation.safe_result", replyable=False,
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                raise RuntimeError("context append rejected")
        except Exception as exc:
            self.ctx.logger.warning("OAImage 安全图写入内部历史失败: %s", type(exc).__name__)

    def _effective_config(self) -> dict[str, Any]:
        config = self.get_plugin_config_data() or type(self).build_default_config()
        config.setdefault("runtime", {})["plugin_dir"] = str(_PLUGIN_DIR)
        cleanup_runtime_state_from_config(config)
        return config

    def _tool_description(self, config: dict[str, Any]) -> str:
        parts = [_BASE_TOOL_DESCRIPTION]
        catalog = _profile_catalog_text(config)
        if catalog:
            parts.append("可用生图套餐如下，选择 profile 时优先匹配用户需求、模型能力和介绍：\n" + catalog)
        bot_appearance_text = _bot_appearance_text_for_action(config)
        if bot_appearance_text:
            parts.append(bot_appearance_text)
        if _get_nested(config, "character_library.enabled", False):
            parts.append(
                "可通过 characters 数组使用角色知识私库原图，支持多个角色同时作为生图参考。"
                "角色不确定时先查 get_character_knowledge；画别人时无需开启 use_bot_appearance。"
                "用户只点名已登记角色、没有要求修改某张聊天图片时，characters 填对应名字、别名或 QQ 号，"
                "直接使用库内设定图；不要因为聊天摘要提到旧设定图，就设置 reference_message_id、use_recent_image 或 image_source。"
                "只有本次请求明确指定聊天图片作为编辑、服装、画风或构图来源时，才额外绑定该图片。"
                "不要把其他人物的设定替换为 Bot 本人的外形。"
            )
            personas = _get_nested(config, "character_library.personas", {})
            if personas:
                parts.append("已确认的人物绘图设定：" + "；".join(f"{name} → {role}" for name, role in personas.items()))
        parts.append(
            "GIF 动图统一使用 output_format=gif。未指定 profile 时使用 animation.profile 套餐。"
            "把聊天图片变成动图时，优先填写该图片消息的 reference_message_id，只有用户指定最近一张且编号未知时使用 use_recent_image。"
            "只用 Bot 外形或角色库原图做动图时，选 image_to_image、设置 use_bot_appearance 或 characters，并将 use_recent_image 设为 false。"
        )
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

    async def _publish_generation_result(
        self, *, job_id: str, stream_id: str, prompt: str, mode: str,
        delivery: str, success: bool, message: str, wake_on_success: bool,
        cancelled: bool = False, rating: str = "", error_notified: bool = False,
    ) -> None:
        state = "cancelled" if cancelled else ("completed" if success else "failed")
        text = self._generation_feedback.update(
            stream_id, job_id, state=state, prompt=prompt, detail=message, error_notified=error_notified,
        )
        finish_command_job(stream_id, job_id, state)
        # 先写入内部历史；即使当前 Planner 已发出请求，后续 Replyer Hook 也读取同一份状态。
        try:
            result = await self.ctx.call_capability(
                "maisaka.context.append", stream_id=stream_id,
                segments=[{"type": "text", "data": text}], visible_text=text,
                source_kind="oaimage.generation.result", replyable=False,
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                raise RuntimeError("context append rejected")
        except Exception as exc:
            self.ctx.logger.warning("OAImage 结果写入内部历史失败: %s", type(exc).__name__)
        if cancelled or (success and not wake_on_success):
            return
        try:
            result = await self.ctx.call_capability(
                "maisaka.proactive.trigger", stream_id=stream_id, intent=text,
                reason="OAImage generation " + state, priority="normal",
                metadata={"source": "oaimage", "task_id": job_id, "state": state,
                          "mode": mode, "delivery": delivery, "success": success,
                          "error_notified": error_notified, "background_task_result": True,
                          "user_notified": error_notified or (success and delivery != "internal")},
            )
            if not isinstance(result, dict) or result.get("success") is not True:
                raise RuntimeError("proactive trigger rejected")
        except Exception as exc:
            self.ctx.logger.warning("OAImage 结果唤醒 Planner 失败: %s", type(exc).__name__)

    async def _trigger_continue_after_generation(
        self, *, stream_id: str, prompt: str, mode: str, delivery: str,
        description: str, sent: bool | str = False,
    ) -> None:
        # 成功/失败已在主任务结束时同步；迟到的识图描述只补充材料，不重复唤醒或发送通知。
        if not description:
            return
        text = "【已生成图片的补充描述】" + description[:1200]
        await self.ctx.call_capability(
            "maisaka.context.append", stream_id=stream_id,
            segments=[{"type": "text", "data": text}], visible_text=text,
            source_kind="oaimage.generated_image.description", replyable=False,
        )

    async def _build_context_prompt(self, raw_prompt: str, stream_id: str, config: dict, mode: str) -> str | NovelAIPrompt:
        include_context = bool(_get_nested(config, "prompt.include_context", False))
        rewrite = _get_nested(config, "api.provider", "") == "novelai" or bool(_get_nested(config, "prompt.rewrite_enabled", False))
        if not include_context and not rewrite:
            return raw_prompt
        limit = max(1, min(50, int(_get_nested(config, "prompt.context_message_limit", 8))))
        hours = float(_get_nested(config, "prompt.context_hours", 2.0))
        chat_context = ""
        if not include_context:
            return await rewrite_image_prompt(raw_prompt, config, self.ctx.llm.generate, request_text=raw_prompt)
        try:
            messages = await self.ctx.call_capability(
                "message.get_recent",
                chat_id=stream_id,
                hours=hours,
                limit=limit,
                filter_mai=False,
            )
            messages = _capability_value(messages, "messages")
            if not isinstance(messages, list):
                raise ValueError("聊天消息列表结构无效")
            # 上一张的指令只在用户明确要求延续时才作为画面资料。
            messages = _drop_previous_image_commands(messages, raw_prompt)
            chat_context = await self.ctx.call_capability(
                "message.build_readable",
                messages=messages,
                replace_bot_name=True,
                timestamp_mode="relative",
                truncate=True,
                show_actions=True,
            )
            chat_context = _capability_value(chat_context, "text")
            if not isinstance(chat_context, str):
                raise ValueError("聊天上下文文本结构无效")
        except Exception as exc:
            if rewrite:
                raise ValueError("读取生图上下文失败，未启动图片生成") from exc
            self.ctx.logger.debug(f"构建生图上下文失败: {exc}")
        # 只读原聊天流；先截断再交给整理模型选择相关内容。
        char_limit = int(_get_nested(config, "prompt.context_character_limit", 8000))
        scene = build_context_prompt(raw_prompt, stream_id, config, mode, chat_context=str(chat_context)[-char_limit:])
        if rewrite:
            return await rewrite_image_prompt(scene, config, self.ctx.llm.generate, request_text=raw_prompt)
        return scene

    async def _run_generation(
        self,
        *,
        stream_id: str,
        raw_prompt: str,
        mode: str = "auto",
        output_format: str = "auto",
        frame_count: int | None = None,
        frame_duration_ms: int | None = None,
        reference_image_base64: str | None = None,
        use_recent_image: bool = False,
        image_source: str = "",
        characters: list[str] | None = None,
        profile_name: str = "",
        use_bot_appearance: bool | None = None,
        delivery: str = "both",
        reference_message_id: str = "",
        reference_image_index: int = 0,
        job_id: str = "",
        silent_context: bool = False,
    ) -> tuple[bool, str]:
        config = self._effective_config()
        if reference_message_id and mode == "text_to_image":
            mode = "image_to_image"
        job_id = job_id or uuid.uuid4().hex[:16]
        if not silent_context:
            self._generation_feedback.update(stream_id, job_id, state="running", prompt=raw_prompt)

        async def reference_resolver() -> str | None:
            return await resolve_reference(
                self.ctx, stream_id, message_id=reference_message_id, index=reference_image_index,
                max_age_seconds=int(_get_nested(config, "behavior.reference_image_max_age_seconds", 900)),
            )

        async def publish_result(**details: Any) -> None:
            if silent_context:
                finish_command_job(
                    stream_id,
                    job_id,
                    "cancelled" if details.get("cancelled") else ("completed" if details.get("success") else "failed"),
                )
                # /生图r：非成人画面留一条简短内部记录（不唤醒 Planner），成人结果和失败保持静默。
                if details.get("success") and self._context_note_allowed(details.get("rating")):
                    await self._append_context_note(
                        stream_id, prompt=str(details.get("prompt") or ""), rating=str(details.get("rating") or ""),
                    )
                return
            await self._publish_generation_result(
                job_id=job_id, wake_on_success=bool(details.get("success")) and bool(_get_nested(
                    _resolve_profile_config(config, profile_name)[0], "behavior.trigger_continue_after_generation", True,
                )), **details,
            )

        async def report_usage(**details: Any) -> dict:
            return await record_usage(self.ctx, session_id=stream_id, **details)

        return await run_generation(
            stream_id=stream_id,
            raw_prompt=raw_prompt,
            config=config,
            mode=mode,
            output_format=output_format,
            frame_count=frame_count,
            frame_duration_ms=frame_duration_ms,
            reference_image_base64=reference_image_base64,
            use_recent_image=use_recent_image,
            image_source=image_source,
            characters=characters,
            profile_name=profile_name,
            use_bot_appearance=use_bot_appearance,
            # 静默链路强制只发图：不生成内部识图描述，也不留任何主动唤回入口。
            delivery="chat" if silent_context else delivery,
            send_image=self._send_image,
            send_text=self._send_text,
            context_builder=self._build_context_prompt,
            usage_callback=report_usage,
            reference_resolver=(reference_resolver if reference_message_id or use_recent_image else None),
            result_callback=publish_result,
            proactive_trigger=None if silent_context else self._trigger_continue_after_generation,
        )

    @Tool(
        "generate_image",
        description=_BASE_TOOL_DESCRIPTION,
        parameters=_GENERATE_IMAGE_TOOL_PARAMETERS,
    )
    async def handle_generate_image(
        self,
        prompt: str = "",
        mode: str = "",
        output_format: str = "",
        frame_count: int | None = None,
        frame_duration_ms: int | None = None,
        use_recent_image: bool | str | int | None = None,
        use_bot_appearance: bool | str | int | None = None,
        image_source: str = "",
        reference_message_id: str = "",
        reference_image_index: int = 0,
        characters: list[str] | None = None,
        profile: str = "",
        delivery: str = "",
        stream_id: str = "",
        chat_id: str = "",
        reasoning: str = "",
        action_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs

        data = action_data if isinstance(action_data, dict) else {}
        config = self._effective_config()
        resolved_prompt = str(prompt or data.get("prompt") or data.get("description") or reasoning or "").strip()
        resolved_mode = str(mode or data.get("mode") or "auto").strip() or "auto"
        resolved_stream_id = str(stream_id or chat_id or data.get("stream_id") or data.get("chat_id") or "").strip()
        resolved_image_source = str(image_source or data.get("image_source") or "").strip()
        reference_message_id = str(reference_message_id or data.get("reference_message_id") or "").strip()
        reference_image_index = data.get("reference_image_index", reference_image_index)
        if type(reference_image_index) is not int or not 0 <= reference_image_index < 8:
            return {"success": False, "content": "参考图片序号应为 0～7 的整数"}
        if reference_message_id and resolved_image_source:
            return {"success": False, "content": "请在 reference_message_id 和 image_source 中只选择一种参考图来源"}
        resolved_profile = str(
            profile
            or data.get("profile")
            or data.get("profile_name")
            or data.get("profile_id")
            or ""
        ).strip()
        resolved_format = output_format or data.get("output_format") or "auto"
        resolved_count = frame_count if frame_count is not None else data.get("frame_count")
        resolved_duration = frame_duration_ms if frame_duration_ms is not None else data.get("frame_duration_ms")
        try:
            profile_config, profile_id, _ = resolve_output_config(
                config, resolved_profile, output_format=resolved_format,
                frame_count=resolved_count, frame_duration_ms=resolved_duration,
            )
        except ValueError as exc:
            return {"success": False, "content": str(exc)}
        if _get_nested(profile_config, "runtime.output_format", "") == "gif":
            resolved_format = "gif"
            resolved_profile = profile_id
        else:
            # 已记录的真实调用会同时填入 9 / 200；静态任务排队前去掉不适用的 GIF 参数。
            resolved_count = None
            resolved_duration = None
        resolved_delivery = str(delivery or data.get("delivery") or _get_nested(profile_config, "behavior.default_delivery", "both")).strip()
        recent_image = _to_bool(
            use_recent_image if use_recent_image is not None else data.get("use_recent_image"),
            resolved_mode == "image_to_image",
        )
        bot_appearance = _to_bool(
            use_bot_appearance if use_bot_appearance is not None else data.get("use_bot_appearance"),
            False,
        )
        if _get_nested(config, "behavior.dedupe_command_action", True):
            dedupe_seconds = int(_get_nested(config, "behavior.command_action_dedupe_seconds", 90))
            if has_recent_command_job(resolved_stream_id, dedupe_seconds):
                return {"success": True, "content": "image generation already scheduled by command"}

        if not resolved_stream_id:
            return {"success": False, "content": "missing stream_id", "message": "missing stream_id"}

        job_id = uuid.uuid4().hex[:16]
        mark_command_job(resolved_stream_id, resolved_prompt, job_id=job_id)
        self._generation_feedback.update(resolved_stream_id, job_id, state="queued", prompt=resolved_prompt)
        task = asyncio.create_task(
            self._run_generation(
                stream_id=resolved_stream_id,
                raw_prompt=resolved_prompt,
                mode=resolved_mode,
                output_format=resolved_format,
                frame_count=resolved_count,
                frame_duration_ms=resolved_duration,
                use_recent_image=recent_image,
                image_source=resolved_image_source,
                characters=characters if characters is not None else data.get("characters"),
                profile_name=resolved_profile,
                use_bot_appearance=bot_appearance,
                delivery=resolved_delivery,
                reference_message_id=reference_message_id,
                reference_image_index=reference_image_index,
                job_id=job_id,
            )
        )
        track_command_task(task, resolved_stream_id)
        return {
            "success": True,
            "content": f"生图任务已受理并在后台处理；task_id={job_id}。"
                       "请先通过 Replyer 向用户简短确认已开始处理，再继续聊天，不能直接沉默或 wait。"
                       "目前仅提交，尚未完成；最终成功或失败会另行回传，失败时及时说明原因。",
            "message": "image generation scheduled", "task_id": job_id,
            "status": "queued", "background_task": True,
        }

    @Command(
        "draw_image",
        description="/生图 画面描述。/动图 或 /gif 动作描述生成 GIF。/图生图 --gif 动作描述将当前会话的最近图片转成 GIF。帧数可用 --frames 指定。/生图r 使用 NovelAI。群聊先 @机器人，私聊直接发送。",
        pattern=r"^(?:(?P<novelai>/生图r)(?=\s|$)|(?P<gif>/(?:动图|(?i:gif)))(?=\s|$)|/(?:draw|image|生图|画图|图生图|改图)(?=\s|$)|(?:生图|画图|图生图|改图))\s*(?P<prompt>[\s\S]*)$",
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
        is_novelai = bool(groups.get("novelai"))
        prompt = str(groups.get("prompt") or "").strip()
        prompt, profile_name = _extract_profile_option(prompt)
        if is_novelai:
            profile_name = "novelai"
        prompt, image_source_option = _extract_image_source_option(prompt)
        try:
            prompt, animation_options = extract_animation_options(prompt, gif_command=bool(groups.get("gif")))
        except ValueError as exc:
            await self._send_text(str(exc), stream_id)
            return True, str(exc), True
        if not prompt.strip():
            await self._send_text(
                "用法：@机器人 /生图 画面描述\n"
                "NovelAI：@机器人 /生图r 画面描述\n"
                "动图：@机器人 /动图 小猫挥爪，首尾平滑循环\n"
                "参考图转动图：先发静态图，再发 @机器人 /图生图 --gif 让角色眨眼，循环播放\n"
                "参数：/gif --frames 9 --frame-ms 200 动作描述；也可 /生图 --gif 动作描述。\n"
                "例如：@机器人 /生图 一只坐在窗边的猫\n"
                "私聊直接发送 /生图 画面描述；修改图片可用 /图生图 修改描述。",
                stream_id,
            )
            return True, "已发送生图用法", True
        command_text = text or prompt
        command_head = command_text.split(maxsplit=1)[0]
        mode = "image_to_image" if not is_novelai and ("图生图" in command_head or "改图" in command_head) else "text_to_image"
        image_source = image_source_option or _extract_first_url(command_text) or _extract_local_image_source(command_text)
        if image_source and mode == "text_to_image":
            mode = "image_to_image"

        try:
            profile_config, profile_id, _ = resolve_output_config(self._effective_config(), profile_name, **animation_options)
        except ValueError as exc:
            await self._send_text(str(exc), stream_id)
            return True, str(exc), True
        if _get_nested(profile_config, "runtime.output_format", "") == "gif":
            animation_options["output_format"] = "gif"
            profile_name = profile_id
        if is_novelai and (profile_id != "novelai" or _get_nested(profile_config, "api.provider", "") != "novelai"):
            await self._send_text("尚未配置 NovelAI 套餐，请先完成生图插件的 novelai 套餐设置。", stream_id)
            return True, "NovelAI 套餐未配置", True
        job_id = uuid.uuid4().hex[:16]
        # /生图r 走静默链路：结果不入模型上下文，指令本身也不交给 Planner。
        silent_context = is_novelai
        mark_command_job(stream_id, prompt, job_id=job_id)
        if not silent_context:
            self._generation_feedback.update(stream_id, job_id, state="queued", prompt=prompt)
        task = asyncio.create_task(
            self._run_generation(
                stream_id=stream_id,
                raw_prompt=prompt,
                mode=mode,
                **animation_options,
                use_recent_image=mode == "image_to_image" and not image_source,
                image_source=image_source,
                profile_name=profile_name,
                delivery=str(_get_nested(profile_config, "behavior.default_delivery", "both")),
                job_id=job_id,
                silent_context=silent_context,
            )
        )
        track_command_task(task, stream_id)
        if not silent_context:
            return True, "", False
        ack = random.choice(_SUBMIT_ACK_TEXTS)
        await self._send_text(ack, stream_id)
        return True, ack, True

    @HookHandler(
        "chat.receive.after_process", name="image_cache_on_message",
        description="从当前入站 Hook 缓存普通参考原图，不把表情包或引用文字作为参考图",
        mode=HookMode.BLOCKING, order=HookOrder.LATE, error_policy=ErrorPolicy.SKIP,
    )
    async def handle_image_cache_on_message(self, message: Any = None, **kwargs: Any) -> None:
        del kwargs
        if not isinstance(message, dict) or not (stream_id := _message_stream_id(message)):
            return
        images = original_images(message)
        if not images:
            return
        value = images[0].get("binary_data_base64")
        if not isinstance(value, str) or not value:
            return
        # 未就绪的第一张不替换成同条消息里的另一张；可由按编号读取接口加载原图。
        reference = image_data_url(value)
        config = self._effective_config()
        cache_images(stream_id, [reference], max_items=int(_get_nested(config, "behavior.max_cached_images", 8)),
                     source="chat.receive.after_process")

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
