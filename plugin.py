# ruff: noqa: E402
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple, Type

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# MaiBot 0.12.2 按文件路径加载 plugin.py，本地拆分模块需要显式走插件目录导入。
# 热重载时清掉本插件的本地模块缓存，避免修改子模块后仍拿到旧实现。
for _module_name in (
    "oa_image_clients",
    "oa_image_components",
    "oa_image_config",
    "oa_image_generation",
    "oa_image_runtime",
    "oa_image_sources",
):
    sys.modules.pop(_module_name, None)

from oa_image_components import (
    AfterLLMStateHandler,
    AfterSendStateHandler,
    DrawImageCommand,
    GenerateImageAction,
    ImageCacheOnMessageHandler,
    PlanStateHandler,
    PostSendStateHandler,
    StopCommandTasksHandler,
)
from oa_image_config import _bot_appearance_text_for_action, _get_nested, _profile_catalog_text

from src.plugin_system import BasePlugin, ComponentInfo, ConfigField, register_plugin


@register_plugin
class OAImagePlugin(BasePlugin):
    plugin_name = "OAImage"
    enable_plugin = False
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name = "config.toml"
    config_section_descriptions = {
        "plugin": "插件基本信息",
        "profiles": "生图套餐",
        "behavior": "插件行为",
        "bot_appearance": "Bot 外形人设",
        "prompt": "上下文提示词",
    }
    config_schema = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=False, description="是否启用插件"),
            "enable_action": ConfigField(type=bool, default=True, description="是否注册 generate_image Action"),
            "config_version": ConfigField(type=str, default="1.0.0", description="配置文件版本"),
        },
        "profiles": {
            "default_profile": ConfigField(
                type=str,
                default="",
                description="默认生图套餐 ID",
                hint="当命令或 Action 没有指定 profile 时使用此套餐。",
            ),
            "items": ConfigField(
                type=dict,
                default={},
                description="生图套餐定义",
                input_type="json",
                hint="建议直接在 config.toml 中使用 [profiles.items.<id>] 形式维护套餐。",
            ),
        },
        "behavior": {
            "default_prompt": ConfigField(
                type=str, default="根据当前聊天内容生成一张图片", description="空提示词时使用的默认需求"
            ),
            "reference_image_max_age_seconds": ConfigField(type=int, default=900, description="最近参考图有效秒数"),
            "max_cached_images": ConfigField(type=int, default=8, description="每个聊天缓存的最近图片数量"),
            "skip_when_busy": ConfigField(type=bool, default=True, description="同一聊天已有生图任务时跳过新任务"),
            "dedupe_command_action": ConfigField(
                type=bool, default=True, description="避免命令触发后 Action 再重复生图"
            ),
            "command_action_dedupe_seconds": ConfigField(type=int, default=90, description="命令与 Action 去重秒数"),
            "allow_local_image_source": ConfigField(
                type=bool,
                default=False,
                description="是否允许 OpenAI 兼容图生图读取本地 image_source 文件",
            ),
            "local_image_base_dir": ConfigField(
                type=str,
                default="local_images",
                description="允许读取的本地参考图目录；相对路径按插件目录解析",
            ),
            "local_image_max_bytes": ConfigField(type=int, default=10485760, description="本地参考图最大字节数"),
            "storage_message": ConfigField(type=bool, default=True, description="是否把发送的图片写入消息库"),
            "send_error_messages": ConfigField(type=bool, default=False, description="失败时是否向聊天发送错误文本"),
        },
        "bot_appearance": {
            "enabled": ConfigField(type=bool, default=False, description="是否启用 Bot 外形人设注入"),
            "aliases": ConfigField(
                type=list,
                default=["你", "bot", "MaiBot"],
                description="LLM 判断画 Bot 时可参考的 Bot 名字或别名",
                item_type="string",
            ),
            "trigger_phrases": ConfigField(
                type=list,
                default=["画你", "你的头像", "你的立绘", "Bot 自画像"],
                description="暴露给 LLM 的典型触发说法示例；插件不会用它自动判断",
                item_type="string",
            ),
            "inject_mode": ConfigField(
                type=str,
                default="prepend",
                description="注入位置",
                choices=["prepend", "append"],
                hint="prepend 会把外貌参数放在 prompt 前面；append 会放在后面。",
            ),
            "tag_prompt": ConfigField(
                type=str,
                default="",
                description="Danbooru/tag 风格 Bot 外貌参数",
                input_type="textarea",
                rows=4,
            ),
            "natural_prompt": ConfigField(
                type=str,
                default="",
                description="自然语言风格 Bot 外貌描述",
                input_type="textarea",
                rows=4,
            ),
            "edit_prompt": ConfigField(
                type=str,
                default="",
                description="图生图/编辑风格 Bot 外貌约束",
                input_type="textarea",
                rows=4,
            ),
        },
        "prompt": {
            "include_context": ConfigField(
                type=bool, default=False, description="是否把聊天上下文和 bot 状态拼入生图提示词"
            ),
            "context_hours": ConfigField(type=float, default=2.0, description="读取最近多少小时的上下文"),
            "context_message_limit": ConfigField(type=int, default=8, description="最多读取多少条上下文消息"),
            "context_template": ConfigField(
                type=str,
                default=(
                    "用户的生图需求：{prompt}\n"
                    "生成模式：{mode}\n"
                    "当前群聊/私聊上下文：\n{chat_context}\n"
                    "bot 当前状态：{bot_state}\n"
                    "请只把这些信息转化为适合图像模型的画面描述，不要生成聊天回复文本。"
                ),
                description="上下文提示词模板",
                input_type="textarea",
                rows=8,
            ),
        },
    }

    def _migrate_config_values(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> Dict[str, Any]:
        migrated = super()._migrate_config_values(old_config, new_config)
        old_items = _get_nested(old_config, "profiles.items", {})
        if isinstance(old_items, dict) and old_items:
            migrated.setdefault("profiles", {})["items"] = old_items
        return migrated

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        self.config.setdefault("runtime", {})["plugin_dir"] = str(Path(self.plugin_dir).resolve())
        components: List[Tuple[ComponentInfo, Type]] = [
            (DrawImageCommand.get_command_info(), DrawImageCommand),
            (ImageCacheOnMessageHandler.get_handler_info(), ImageCacheOnMessageHandler),
            (PlanStateHandler.get_handler_info(), PlanStateHandler),
            (AfterLLMStateHandler.get_handler_info(), AfterLLMStateHandler),
            (PostSendStateHandler.get_handler_info(), PostSendStateHandler),
            (AfterSendStateHandler.get_handler_info(), AfterSendStateHandler),
            (StopCommandTasksHandler.get_handler_info(), StopCommandTasksHandler),
        ]
        if _get_nested(self.config, "plugin.enable_action", True):
            action_info = GenerateImageAction.get_action_info()
            catalog = _profile_catalog_text(self.config)
            if catalog:
                action_info.action_parameters["profile"] = (
                    "可选：套餐 ID 或显示名。根据下面的套餐介绍选择；不确定时留空使用默认套餐。"
                )
                action_info.action_require.append(
                    "可用生图套餐如下，选择 profile 时优先匹配用户需求、模型能力和介绍：\n" + catalog
                )
            bot_appearance_text = _bot_appearance_text_for_action(self.config)
            if bot_appearance_text:
                action_info.action_require.append(bot_appearance_text)
            components.insert(1, (action_info, GenerateImageAction))
        return components
