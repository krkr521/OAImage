import asyncio
import logging
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from oa_image_config import _get_nested

logger = logging.getLogger("OAImage")


@dataclass
class CachedImage:
    image_base64: str
    timestamp: float
    source: str = "message"


STREAM_IMAGES: Dict[str, List[CachedImage]] = {}
STREAM_STATES: Dict[str, Dict[str, Any]] = {}
STREAM_LOCKS: Dict[str, asyncio.Lock] = {}
STREAM_COMMAND_JOBS: Dict[str, Dict[str, Any]] = {}
COMMAND_TASKS: Set[asyncio.Task] = set()
DEFAULT_RUNTIME_STATE_TTL_SECONDS = 24 * 3600
DEFAULT_MAX_RUNTIME_STREAMS = 1024


def _latest_image_time(stream_id: str) -> float:
    bucket = STREAM_IMAGES.get(stream_id) or []
    return max((item.timestamp for item in bucket), default=0.0)


def _stream_last_seen(stream_id: str) -> float:
    return max(
        _latest_image_time(stream_id),
        float((STREAM_STATES.get(stream_id) or {}).get("time", 0.0) or 0.0),
        float((STREAM_COMMAND_JOBS.get(stream_id) or {}).get("time", 0.0) or 0.0),
    )


def _drop_stream_state(stream_id: str) -> None:
    STREAM_IMAGES.pop(stream_id, None)
    STREAM_STATES.pop(stream_id, None)
    STREAM_COMMAND_JOBS.pop(stream_id, None)
    lock = STREAM_LOCKS.get(stream_id)
    if lock is not None and not lock.locked():
        STREAM_LOCKS.pop(stream_id, None)


def cleanup_runtime_state(
    max_age_seconds: int = DEFAULT_RUNTIME_STATE_TTL_SECONDS,
    max_streams: int = DEFAULT_MAX_RUNTIME_STREAMS,
) -> None:
    """清理长期未使用的运行时状态，避免 stream 维度字典无限增长。"""

    stream_ids = set(STREAM_IMAGES) | set(STREAM_STATES) | set(STREAM_COMMAND_JOBS) | set(STREAM_LOCKS)
    if not stream_ids:
        return
    if max_streams <= 0:
        for stream_id in list(stream_ids):
            _drop_stream_state(stream_id)
        return
    now = time.time()
    if max_age_seconds > 0:
        cutoff = now - max_age_seconds
        for stream_id in list(stream_ids):
            last_seen = _stream_last_seen(stream_id)
            lock = STREAM_LOCKS.get(stream_id)
            if (not last_seen or last_seen < cutoff) and (lock is None or not lock.locked()):
                _drop_stream_state(stream_id)
    stream_ids = set(STREAM_IMAGES) | set(STREAM_STATES) | set(STREAM_COMMAND_JOBS) | set(STREAM_LOCKS)
    if len(stream_ids) <= max_streams:
        return
    oldest_streams = sorted(stream_ids, key=_stream_last_seen)[: len(stream_ids) - max_streams]
    for stream_id in oldest_streams:
        lock = STREAM_LOCKS.get(stream_id)
        if lock is None or not lock.locked():
            _drop_stream_state(stream_id)


def cleanup_runtime_state_from_config(config: dict) -> None:
    cleanup_runtime_state(
        max_age_seconds=int(_get_nested(config, "behavior.runtime_state_ttl_seconds", DEFAULT_RUNTIME_STATE_TTL_SECONDS)),
        max_streams=int(_get_nested(config, "behavior.max_runtime_streams", DEFAULT_MAX_RUNTIME_STREAMS)),
    )


def cache_images(stream_id: str, images: List[str], max_items: int = 8, source: str = "message") -> None:
    if not stream_id:
        return
    if max_items <= 0:
        STREAM_IMAGES.pop(stream_id, None)
        return
    if not images:
        return
    bucket = STREAM_IMAGES.setdefault(stream_id, [])
    now = time.time()
    for image_base64 in images:
        bucket.append(CachedImage(image_base64=image_base64, timestamp=now, source=source))
    del bucket[:-max_items]


def get_latest_cached_image(stream_id: str, max_age_seconds: int) -> Optional[str]:
    bucket = STREAM_IMAGES.get(stream_id) or []
    cutoff = time.time() - max_age_seconds
    for item in reversed(bucket):
        if item.timestamp >= cutoff and item.image_base64:
            return item.image_base64
    return None


def update_state(stream_id: Optional[str], state: str, detail: str = "") -> None:
    if not stream_id:
        return
    STREAM_STATES[stream_id] = {"state": state, "detail": detail, "time": time.time()}


def get_state_text(stream_id: str) -> str:
    state = STREAM_STATES.get(stream_id)
    if not state:
        return "unknown"
    detail = state.get("detail") or ""
    return f"{state.get('state', 'unknown')}: {detail}".strip()


def get_stream_lock(stream_id: str) -> asyncio.Lock:
    if stream_id not in STREAM_LOCKS:
        STREAM_LOCKS[stream_id] = asyncio.Lock()
    return STREAM_LOCKS[stream_id]


def mark_command_job(stream_id: str, prompt: str) -> None:
    if stream_id:
        STREAM_COMMAND_JOBS[stream_id] = {"prompt": prompt, "time": time.time()}


def has_recent_command_job(stream_id: str, seconds: int) -> bool:
    job = STREAM_COMMAND_JOBS.get(stream_id)
    if not job:
        return False
    return time.time() - float(job.get("time", 0)) <= seconds


def track_command_task(task: asyncio.Task, stream_id: str) -> None:
    COMMAND_TASKS.add(task)

    def _on_done(done_task: asyncio.Task) -> None:
        COMMAND_TASKS.discard(done_task)
        try:
            done_task.result()
        except asyncio.CancelledError:
            logger.debug(f"命令生图任务已取消: {stream_id}")
        except Exception as e:
            logger.error(f"命令生图任务异常: {e}")
            logger.debug(traceback.format_exc())

    task.add_done_callback(_on_done)


async def cancel_command_tasks() -> None:
    if not COMMAND_TASKS:
        return
    tasks = list(COMMAND_TASKS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    COMMAND_TASKS.difference_update(tasks)


def build_context_prompt(
    raw_prompt: str,
    stream_id: str,
    config: dict,
    mode: str,
    chat_context: str = "",
) -> str:
    if not _get_nested(config, "prompt.include_context", False):
        return raw_prompt
    template = _get_nested(
        config,
        "prompt.context_template",
        (
            "用户的生图需求：{prompt}\n"
            "生成模式：{mode}\n"
            "当前群聊/私聊上下文：\n{chat_context}\n"
            "bot 当前状态：{bot_state}\n"
            "请只把这些信息转化为适合图像模型的画面描述，不要生成聊天回复文本。"
        ),
    )
    try:
        return template.format(
            prompt=raw_prompt,
            mode=mode,
            chat_context=chat_context or "无",
            bot_state=get_state_text(stream_id),
        )
    except Exception:
        return raw_prompt
