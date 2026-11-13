"""捕获命令调度和帮助回复，不调用上游或发送 QQ 消息。"""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import asyncio
import importlib.util
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("oaimage_command_test_plugin", ROOT / "plugin.py")
plugin_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin_module)
PATTERN = re.compile(plugin_module.OAImagePlugin.handle_draw_image.__maibot_component_info__.command_pattern)


@pytest.mark.parametrize("command", ["/生图", "/画图", "/draw", "/image", "/图生图", "/改图"])
def test_slash_aliases_accept_separated_multiline_description(command):
    match = PATTERN.fullmatch(f"{command} 一只猫\n坐在窗边")
    assert match is not None
    assert match["prompt"] == "一只猫\n坐在窗边"


@pytest.mark.parametrize("text", ["/生图xxx", "/画图鉴", "/drawing cat", "/images cat", "/改图标"])
def test_slash_command_does_not_consume_longer_words(text):
    assert PATTERN.fullmatch(text) is None


@pytest.mark.parametrize("command", ["生图", "画图", "图生图", "改图"])
def test_legacy_chinese_phrases_still_accept_adjacent_descriptions(command):
    match = PATTERN.fullmatch(f"{command}一只猫")
    assert match is not None
    assert match["prompt"] == "一只猫"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/生图", "/生图   ", "/生图 --profile default"])
async def test_empty_description_only_sends_usage(monkeypatch, text):
    plugin = plugin_module.OAImagePlugin()
    send_text = AsyncMock(return_value=True)
    run_generation = AsyncMock()
    mark_job = Mock()
    monkeypatch.setattr(plugin, "_send_text", send_text)
    monkeypatch.setattr(plugin, "_run_generation", run_generation)
    monkeypatch.setattr(plugin_module, "mark_command_job", mark_job)

    result = await plugin.handle_draw_image(
        text=text, stream_id="test-stream", matched_groups=PATTERN.fullmatch(text).groupdict()
    )

    assert result == (True, "已发送生图用法", True)
    assert "/生图 画面描述" in send_text.await_args.args[0]
    assert send_text.await_args.args[1] == "test-stream"
    run_generation.assert_not_called()
    mark_job.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("text,mode", [
    ("/生图 --profile default 一只猫\n坐在窗边", "text_to_image"),
    ("/图生图 把背景改成夜晚", "image_to_image"),
])
async def test_command_reuses_one_existing_generation_task(monkeypatch, text, mode):
    plugin = plugin_module.OAImagePlugin()
    tasks = []
    mark_job = Mock()
    run_generation = AsyncMock()
    monkeypatch.setattr(plugin, "_effective_config", lambda: {})
    monkeypatch.setattr(plugin, "_run_generation", run_generation)
    monkeypatch.setattr(plugin_module, "mark_command_job", mark_job)
    monkeypatch.setattr(plugin_module, "track_command_task", lambda task, stream_id: tasks.append((task, stream_id)))

    result = await plugin.handle_draw_image(
        text=text, stream_id="test-stream", matched_groups=PATTERN.fullmatch(text).groupdict()
    )
    assert result == (True, "", False)
    assert len(tasks) == 1
    assert tasks[0][1] == "test-stream"
    await asyncio.gather(tasks[0][0])
    run_generation.assert_awaited_once()
    options = run_generation.await_args.kwargs
    assert options["mode"] == mode
    assert options["stream_id"] == "test-stream"
    assert "--profile" not in options["raw_prompt"]
    assert options["profile_name"] == ("default" if mode == "text_to_image" else "")
    assert options["use_recent_image"] is (mode == "image_to_image")
    mark_job.assert_called_once_with("test-stream", options["raw_prompt"], job_id=options["job_id"])
    assert re.fullmatch(r"[0-9a-f]{16}", options["job_id"])


@pytest.mark.asyncio
async def test_novelai_command_acks_and_stops_main_chain(monkeypatch):
    plugin = plugin_module.OAImagePlugin()
    config = {
        "plugin": {"enabled": True},
        "profiles": {
            "default_profile": "novelai",
            "items": {"novelai": {"display_name": "NovelAI", "provider": "novelai"}},
        },
        "behavior": {"default_delivery": "chat"},
    }
    send_text = AsyncMock(return_value=True)
    run_generation = AsyncMock()
    tasks = []
    monkeypatch.setattr(plugin, "_effective_config", lambda: config)
    monkeypatch.setattr(plugin, "_send_text", send_text)
    monkeypatch.setattr(plugin, "_run_generation", run_generation)
    monkeypatch.setattr(plugin_module, "mark_command_job", Mock())
    monkeypatch.setattr(plugin_module, "track_command_task", lambda task, stream_id: tasks.append((task, stream_id)))

    text = "/生图r 一只猫"
    result = await plugin.handle_draw_image(
        text=text, stream_id="test-stream", matched_groups=PATTERN.fullmatch(text).groupdict()
    )

    assert result[0] is True
    assert result[1] in plugin_module._SUBMIT_ACK_TEXTS
    # 第三个返回值是拦截级别：/生图r 不再进入 Planner/Replyer 主链。
    assert result[2] is True
    assert send_text.await_args.args == (result[1], "test-stream")
    await asyncio.gather(tasks[0][0])
    options = run_generation.await_args.kwargs
    assert options["silent_context"] is True
    assert options["profile_name"] == "novelai"
    # 静默链路不刷新 Planner/Replyer 的任务状态注解。
    assert plugin._generation_feedback.text("test-stream") == ""


@pytest.mark.asyncio
async def test_plain_command_keeps_main_chain_and_has_no_ack(monkeypatch):
    plugin = plugin_module.OAImagePlugin()
    send_text = AsyncMock(return_value=True)
    run_generation = AsyncMock()
    tasks = []
    monkeypatch.setattr(plugin, "_effective_config", lambda: {})
    monkeypatch.setattr(plugin, "_send_text", send_text)
    monkeypatch.setattr(plugin, "_run_generation", run_generation)
    monkeypatch.setattr(plugin_module, "track_command_task", lambda task, stream_id: tasks.append(task))

    result = await plugin.handle_draw_image(
        text="/生图 一只猫", stream_id="test-stream", matched_groups=PATTERN.fullmatch("/生图 一只猫").groupdict()
    )

    assert result == (True, "", False)
    send_text.assert_not_awaited()
    await asyncio.gather(*tasks)
    assert run_generation.await_args.kwargs["silent_context"] is False
