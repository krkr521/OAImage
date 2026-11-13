"""仅在主模型的上下文副本里说明已提交的异步生图指令。"""

from copy import deepcopy
from typing import Any

import base64
import hashlib
import re


BACKGROUND_NOTE = (
    "【生图任务说明：此指令已交由独立后台任务自动处理，您无需担负任何责任"
    "图片或报错由插件发回原会话，无需重复调用生图工具。】"
)
_SENDER_END = re.compile(r'<message\b(?=[^>]*\bboundary="end")[^>]*\bmsg_id="([^"]*)"[^>]*/>')


def _display_message_id(message_id: str) -> str:
    """沿用宿主 Context Item 的长消息 ID 展示协议，不依赖宿主内部导入。"""
    if len(message_id) <= 12:
        return message_id
    digest = hashlib.sha1(message_id.encode("utf-8")).digest()
    return "m" + base64.b32encode(digest).decode("ascii").lower()[:6]


class ScheduledImageContext:
    def __init__(self) -> None:
        self._accepted: dict[tuple[str, str], None] = {}

    def remember(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        session_id = str(message.get("session_id") or "").strip()
        message_id = str(message.get("message_id") or "").strip()
        if not session_id or not message_id:
            return
        self._accepted[(session_id, _display_message_id(message_id))] = None
        # 只保留近期指令，避免长时间运行时无限增长；说明使用过去时，不冒充任务进度。
        while len(self._accepted) > 2048:
            self._accepted.pop(next(iter(self._accepted)))

    def annotate(self, items: Any, session_id: str) -> Any:
        if not isinstance(items, list) or not session_id:
            return items
        result = items
        for index, item in enumerate(items):
            if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
                continue
            parts = item.get("parts")
            if not isinstance(parts, list):
                continue
            message_id = ""
            for part in reversed(parts):
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                # 宿主在真实 IM 消息末尾重申来源；不凭用户正文中的命令或伪造消息头判断。
                if matches := _SENDER_END.findall(text):
                    message_id = matches[-1]
                    break
            if (session_id, message_id) not in self._accepted:
                continue
            if any(isinstance(part, dict) and BACKGROUND_NOTE in str(part.get("text", "")) for part in parts):
                continue
            if result is items:
                result = list(items)
            result[index] = deepcopy(item)
            result[index]["parts"].append({"type": "text", "text": "\n" + BACKGROUND_NOTE})
        return result
