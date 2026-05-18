import asyncio
from typing import Tuple

from oa_image_config import _extract_image_source_option, _extract_profile_option, _get_nested, _to_bool
from oa_image_generation import run_generation
from oa_image_runtime import (
    cache_images,
    cancel_command_tasks,
    has_recent_command_job,
    mark_command_job,
    track_command_task,
    update_state,
)
from oa_image_sources import _extract_first_url, _extract_local_image_source, _looks_like_url, extract_images

from src.plugin_system import ActionActivationType, BaseAction, BaseCommand, BaseEventHandler, EventType, MaiMessages


class DrawImageCommand(BaseCommand):
    command_name = "draw_image"
    command_description = "触发生图或图生图，不拦截 MaiBot 的普通回复"
    command_pattern = r"^(?:/(?:draw|image|生图|画图|图生图|改图)|(?:生图|画图|图生图|改图))\s*(?P<prompt>.*)$"

    async def execute(self) -> Tuple[bool, str, int]:
        if not self.message.chat_stream or not self.message.chat_stream.stream_id:
            return False, "missing stream_id", 0
        stream_id = self.message.chat_stream.stream_id
        prompt = (self.matched_groups.get("prompt") or "").strip()
        prompt, profile_name = _extract_profile_option(prompt)
        prompt, image_source_option = _extract_image_source_option(prompt)
        images = extract_images(self.message.message_segment)
        max_cache = int(_get_nested(self.plugin_config, "behavior.max_cached_images", 8))
        cache_images(stream_id, images, max_items=max_cache, source="command")
        command_text = self.message.processed_plain_text or self.message.raw_message or ""
        mode = "image_to_image" if images or "图生图" in command_text or "改图" in command_text else "text_to_image"
        reference = images[-1] if images else None
        image_source = image_source_option or _extract_first_url(command_text)
        if not image_source:
            image_source = _extract_local_image_source(command_text)
        if not image_source and reference and _looks_like_url(reference):
            image_source = reference
        mark_command_job(stream_id, prompt)
        task = asyncio.create_task(
            run_generation(
                stream_id=stream_id,
                raw_prompt=prompt,
                config=self.plugin_config,
                mode=mode,
                reference_image_base64=reference,
                image_source=image_source,
                profile_name=profile_name,
            )
        )
        track_command_task(task, stream_id)
        # 不发送确认文本，让 MaiBot 的普通回复流程继续判断。
        return True, "", 0


class GenerateImageAction(BaseAction):
    action_name = "generate_image"
    action_description = "生成并发送图片，支持文字生图和使用最近图片作为参考的图生图"
    activation_type = ActionActivationType.ALWAYS
    parallel_action = True
    associated_types = ["text", "image", "emoji"]
    action_parameters = {
        "prompt": "图像生成提示词，描述要生成的画面；必须遵循所选套餐的提示词风格和使用要求",
        "mode": "可选：text_to_image、image_to_image 或 auto",
        "use_recent_image": "可选：是否使用当前聊天最近收到的图片作为图生图参考",
        "use_bot_appearance": "可选：当用户要求生成 Bot 本体、头像、立绘、自画像或外形图时设为 true；是否使用由 LLM 判断，插件不自动判断",
        "image_source": "可选：参考图来源；OpenAI 兼容端点可使用最近图片、图片 URL，或在配置允许时使用本地图片路径",
        "profile": "可选：套餐 ID 或显示名；根据套餐介绍选择最适合的上游模型和参数",
    }
    action_require = [
        "当你想直接给用户画一张图、发一张图、做梗图、画设定图时使用",
        "当用户要求生图、画图、配图、图生图、改图，且你判断应该直接发图片时使用",
        "如果需要参考用户刚发的图片，将 mode 设为 image_to_image，并将 use_recent_image 设为 true",
        "如果配置中存在多个套餐，可根据套餐显示名和介绍选择 profile；不确定时留空使用默认套餐",
        "选择 profile 后，prompt 必须遵循该套餐的提示词风格、适用场景和给 bot 的使用要求",
        "当用户明确要画 Bot 本体、头像、立绘、自画像或外形图时，将 use_bot_appearance 设为 true；其他情况不要设置为 true",
        "OpenAI 兼容端点可以使用最近图片、图片 URL，或在配置允许时使用本地图片路径；部分非 OpenAI JSON provider 可能只接受公网 URL",
        "这个 Action 会自己发送图片，不需要先发送正在生成之类的提示",
    ]

    async def execute(self) -> Tuple[bool, str]:
        prompt = str(self.action_data.get("prompt") or self.action_data.get("description") or "").strip()
        mode = str(self.action_data.get("mode") or "auto")
        image_source = str(self.action_data.get("image_source") or "").strip()
        profile_name = str(
            self.action_data.get("profile")
            or self.action_data.get("profile_name")
            or self.action_data.get("profile_id")
            or ""
        ).strip()
        use_recent_image = _to_bool(self.action_data.get("use_recent_image"), mode == "image_to_image")
        use_bot_appearance = _to_bool(self.action_data.get("use_bot_appearance"), False)
        if _get_nested(self.plugin_config, "behavior.dedupe_command_action", True):
            dedupe_seconds = int(_get_nested(self.plugin_config, "behavior.command_action_dedupe_seconds", 90))
            if has_recent_command_job(self.chat_id, dedupe_seconds):
                return True, "image generation already scheduled by command"
        success, message = await run_generation(
            stream_id=self.chat_id,
            raw_prompt=prompt or self.action_reasoning,
            config=self.plugin_config,
            mode=mode,
            use_recent_image=use_recent_image,
            image_source=image_source,
            profile_name=profile_name,
            use_bot_appearance=use_bot_appearance,
        )
        await self.store_action_info(
            action_build_into_prompt=False,
            action_prompt_display=f"生成图片：{prompt or self.action_reasoning}",
            action_done=success,
        )
        return success, message


class ImageCacheOnMessageHandler(BaseEventHandler):
    event_type = EventType.ON_MESSAGE
    handler_name = "image_cache_on_message"
    handler_description = "缓存最近收到的图片，供图生图 Action 使用"
    weight = 0
    intercept_message = False

    async def execute(self, message: MaiMessages | None):
        if message and message.stream_id:
            update_state(message.stream_id, "received_message", message.plain_text[:80])
            images = extract_images(message.message_segments)
            max_cache = int(_get_nested(self.plugin_config, "behavior.max_cached_images", 8))
            cache_images(message.stream_id, images, max_items=max_cache, source="on_message")
        return True, True, None, None, None


class PlanStateHandler(BaseEventHandler):
    event_type = EventType.ON_PLAN
    handler_name = "image_state_on_plan"
    handler_description = "记录 bot 正在规划"

    async def execute(self, message: MaiMessages | None):
        if message and message.stream_id:
            update_state(message.stream_id, "planning", "")
        return True, True, None, None, None


class AfterLLMStateHandler(BaseEventHandler):
    event_type = EventType.AFTER_LLM
    handler_name = "image_state_after_llm"
    handler_description = "记录 bot 刚完成 LLM 生成"

    async def execute(self, message: MaiMessages | None):
        if message and message.stream_id:
            update_state(message.stream_id, "llm_done", (message.llm_response_content or "")[:80])
        return True, True, None, None, None


class PostSendStateHandler(BaseEventHandler):
    event_type = EventType.POST_SEND
    handler_name = "image_state_post_send"
    handler_description = "记录 bot 正在发送消息"

    async def execute(self, message: MaiMessages | None):
        if message and message.stream_id:
            update_state(message.stream_id, "sending_message", message.plain_text[:80])
        return True, True, None, None, None


class AfterSendStateHandler(BaseEventHandler):
    event_type = EventType.AFTER_SEND
    handler_name = "image_state_after_send"
    handler_description = "记录 bot 已发送消息"

    async def execute(self, message: MaiMessages | None):
        if message and message.stream_id:
            update_state(message.stream_id, "sent_message", message.plain_text[:80])
        return True, True, None, None, None


class StopCommandTasksHandler(BaseEventHandler):
    event_type = EventType.ON_STOP
    handler_name = "image_cancel_command_tasks"
    handler_description = "停止插件命令触发的后台生图任务"

    async def execute(self, message: MaiMessages | None):
        await cancel_command_tasks()
        return True, True, None, None, None
