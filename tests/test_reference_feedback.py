"""复现原图已在会话中但缓存为空，以及后台失败后仍认为已提交的链路。"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio
import base64
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plugin as plugin_module
import oa_image_generation as generation
import oa_image_reference as references
import oa_image_runtime as runtime
from oa_image_feedback import GenerationFeedback
from test_novelai import PNG, config


B64 = base64.b64encode(PNG).decode()


@pytest.fixture(autouse=True)
def clean_runtime():
    runtime.STREAM_IMAGES.clear()
    runtime.STREAM_COMMAND_JOBS.clear()
    runtime.STREAM_LOCKS.clear()
    yield


def image_message(stream="group-A", message_id="1864376348", *, binary=False):
    image = {"type": "image", "data": "图片描述"}
    if binary:
        image["binary_data_base64"] = B64
    return {"session_id": stream, "message_id": message_id, "timestamp": str(time.time()), "raw_message": [image]}


def instance(monkeypatch, *, public_errors=True):
    plugin = plugin_module.OAImagePlugin()
    cfg = config()
    cfg["profiles"]["items"]["openai_default"]["base_url"] = "http://127.0.0.1:1/v1"
    cfg["behavior"]["send_error_messages"] = public_errors
    monkeypatch.setattr(plugin, "_effective_config", lambda: cfg)
    monkeypatch.setattr(plugin, "_build_context_prompt", AsyncMock(return_value="cat"))

    async def capability(name, **kwargs):
        if name == "message.get_recent":
            return {"success": True, "messages": []}
        if name == "maisaka.context.resolve_image":
            return {"success": True, "image": {"binary_data_base64": B64}}
        if name == "message.get_by_id":
            return {"success": True, "message": image_message(
                stream=kwargs["chat_id"], message_id=kwargs["message_id"], binary=True,
            )}
        return {"success": True, "queued": True}

    plugin._ctx = SimpleNamespace(
        call_capability=AsyncMock(side_effect=capability), logger=Mock(),
        send=SimpleNamespace(image=AsyncMock(return_value=True), text=AsyncMock(return_value=True)),
    )
    client = SimpleNamespace(base_url="http://127.0.0.1:1/v1", generate_text_to_image=AsyncMock(return_value=B64),
                             generate_image_to_image=AsyncMock(return_value=B64))
    monkeypatch.setattr(generation, "OpenAIImageClient", lambda cfg: client)
    return plugin, client


@pytest.mark.asyncio
async def test_current_receive_hook_hands_original_bytes_to_edit_without_quoting(monkeypatch):
    plugin, client = instance(monkeypatch)
    message = image_message(binary=True)
    original = deepcopy(message)
    await plugin.handle_image_cache_on_message(message=message)
    assert message == original
    success, _ = await plugin._run_generation(
        stream_id="group-A", raw_prompt="按刚才的衣服画海边照片", mode="image_to_image",
        use_recent_image=True, delivery="chat",
    )
    assert success
    args = client.generate_image_to_image.await_args.args
    assert args[1] == "data:image/png;base64," + B64
    client.generate_text_to_image.assert_not_awaited()
    components = plugin.get_components()
    hook = next(item for item in components if item["name"] == "image_cache_on_message")
    assert hook["type"] == "HOOK_HANDLER"
    assert "chat.receive.after_process" in str(hook)


@pytest.mark.asyncio
async def test_emoji_does_not_replace_cached_reference(monkeypatch):
    plugin, _ = instance(monkeypatch)
    await plugin.handle_image_cache_on_message(message=image_message(binary=True))
    before = runtime.get_latest_cached_image("group-A", 900)
    await plugin.handle_image_cache_on_message(message={
        "session_id": "group-A", "raw_message": [{"type": "emoji", "binary_data_base64": B64}],
    })
    assert runtime.get_latest_cached_image("group-A", 900) == before


@pytest.mark.asyncio
async def test_reload_recovers_recent_original_from_same_chat(monkeypatch):
    plugin, client = instance(monkeypatch)
    metadata = image_message()
    actual = image_message(binary=True)
    async def capability(name, **kwargs):
        if name == "message.get_recent":
            assert kwargs["chat_id"] == "group-A" and kwargs["include_binary_data"] is False
            assert kwargs["filter_mai"] is True
            foreign = image_message("group-B", "foreign", binary=True)
            foreign["timestamp"] = str(time.time() + 1)
            return {"success": True, "messages": [foreign, metadata]}
        if name == "message.get_by_id":
            assert kwargs == {"message_id": "1864376348", "chat_id": "group-A", "include_binary_data": True}
            return {"success": True, "message": actual}
        return {"success": True}
    plugin.ctx.call_capability.side_effect = capability
    assert (await plugin._run_generation(stream_id="group-A", raw_prompt="按参考图画", mode="image_to_image",
                                         use_recent_image=True, delivery="chat"))[0]
    client.generate_image_to_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_message_id_and_image_index_use_context_and_never_latest(monkeypatch):
    plugin, client = instance(monkeypatch)
    assert (await plugin._run_generation(stream_id="group-A", raw_prompt="按第2张图画", mode="auto",
                                         reference_message_id="mshort", reference_image_index=1, delivery="chat"))[0]
    calls = plugin.ctx.call_capability.await_args_list
    assert calls[0].args == ("maisaka.context.resolve_image",)
    assert calls[0].kwargs == {"stream_id": "group-A", "source_id": "mshort", "index": 1}
    assert not any(call.args[0] in {"message.get_recent", "message.get_by_id"} for call in calls)
    client.generate_image_to_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_reference_failure_does_not_switch_to_unrelated_recent_image(monkeypatch):
    plugin, client = instance(monkeypatch)
    runtime.cache_images("group-A", [B64])
    async def capability(name, **kwargs):
        if name == "maisaka.context.resolve_image":
            return {"success": False, "error": "not in this chat"}
        return {"success": True}
    plugin.ctx.call_capability.side_effect = capability
    assert not (await plugin._run_generation(stream_id="group-A", raw_prompt="按图画", mode="image_to_image",
                                             reference_message_id="foreign", delivery="chat"))[0]
    client.generate_image_to_image.assert_not_awaited()
    assert not any(c.args[0] == "message.get_recent" for c in plugin.ctx.call_capability.await_args_list)


@pytest.mark.asyncio
async def test_missing_reference_fails_before_prompt_model_and_wakes_planner_even_without_public_error(monkeypatch):
    plugin, client = instance(monkeypatch, public_errors=False)
    runtime.mark_command_job("group-A", "按图画", job_id="job-failed")
    success, detail = await plugin._run_generation(stream_id="group-A", raw_prompt="按图画", mode="image_to_image",
                                                   use_recent_image=True, delivery="chat", job_id="job-failed")
    assert not success and "参考原图" in detail
    plugin._build_context_prompt.assert_not_awaited()
    client.generate_image_to_image.assert_not_awaited()
    plugin.ctx.send.text.assert_not_awaited()
    calls = plugin.ctx.call_capability.await_args_list
    appended = next(c for c in calls if c.args[0] == "maisaka.context.append")
    trigger = next(c for c in calls if c.args[0] == "maisaka.proactive.trigger")
    assert appended.kwargs["replyable"] is False
    assert '"state": "failed"' in appended.kwargs["visible_text"]
    assert trigger.kwargs["metadata"]["state"] == "failed"
    assert not runtime.has_recent_command_job("group-A", 90)


@pytest.mark.asyncio
@pytest.mark.parametrize("hook", ["annotate_planner_commands", "annotate_replyer_commands"])
async def test_failure_overrides_old_scheduled_result_in_later_requests(monkeypatch, hook):
    plugin, _ = instance(monkeypatch)
    await plugin._run_generation(stream_id="group-A", raw_prompt="按图画", mode="image_to_image",
                                  use_recent_image=True, delivery="chat")
    items = [{"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "原系统说明"}]},
             {"item_type": "FunctionCallOutputItem", "tool_name": "generate_image", "output": "image generation scheduled"}]
    original = deepcopy(items)
    result = await getattr(plugin, hook)(session_id="group-A", items=items)
    revised = result["modified_kwargs"]["items"]
    assert items == original and revised[1] is items[1]
    assert '"state": "failed"' in revised[0]["parts"][-1]["text"]
    assert await getattr(plugin, hook)(session_id="group-B", items=items) is None
    assert await getattr(plugin, hook)(session_id="group-A", items=revised) is None


@pytest.mark.asyncio
async def test_silent_job_without_rating_keeps_model_context_clean(monkeypatch):
    plugin, client = instance(monkeypatch)
    describe = Mock()
    monkeypatch.setattr(generation, "_describe_generated_image", describe)
    runtime.mark_command_job("group-A", "猫", job_id="silent-job")
    success, _ = await plugin._run_generation(
        stream_id="group-A", raw_prompt="猫", job_id="silent-job", silent_context=True,
    )
    assert success
    calls = plugin.ctx.call_capability.await_args_list
    assert not any(call.args[0] in {"maisaka.context.append", "maisaka.proactive.trigger"} for call in calls)
    assert all(call.args[0] == "message.get_recent" for call in calls)
    assert plugin._generation_feedback.text("group-A") == ""
    # 套餐默认交付为 both 时也要强制只发图，不生成内部识图描述。
    describe.assert_not_called()
    assert runtime.has_recent_command_job("group-A", 90)
    client.generate_text_to_image.assert_awaited_once()
    plugin.ctx.send.image.assert_awaited_once()


@pytest.mark.asyncio
async def test_novelai_silent_failure_releases_dedupe_without_context_note(monkeypatch):
    plugin, client = instance(monkeypatch, public_errors=False)
    client.generate_text_to_image.side_effect = ValueError("upstream down")
    runtime.mark_command_job("group-A", "猫", job_id="silent-failed")
    success, detail = await plugin._run_generation(
        stream_id="group-A", raw_prompt="猫", job_id="silent-failed", silent_context=True,
    )
    assert not success and "upstream down" in detail
    calls = plugin.ctx.call_capability.await_args_list
    assert not any(call.args[0] in {"maisaka.context.append", "maisaka.proactive.trigger"} for call in calls)
    assert not runtime.has_recent_command_job("group-A", 90)


@pytest.mark.asyncio
async def test_chat_only_success_publishes_completion_without_waiting_for_description(monkeypatch):
    plugin, client = instance(monkeypatch)
    assert (await plugin._run_generation(stream_id="group-A", raw_prompt="猫", delivery="chat"))[0]
    calls = plugin.ctx.call_capability.await_args_list
    assert sum(call.args[0] == "maisaka.context.append" for call in calls) == 1
    assert next(call for call in calls if call.args[0] == "maisaka.proactive.trigger").kwargs["metadata"]["state"] == "completed"
    client.generate_text_to_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_both_delivery_reports_success_while_description_is_still_pending(monkeypatch):
    plugin, _ = instance(monkeypatch)
    waiting, release = asyncio.Event(), asyncio.Event()
    async def delayed_description(**kwargs):
        waiting.set()
        await release.wait()
    monkeypatch.setattr(generation, "_describe_generated_image", delayed_description)
    assert (await plugin._run_generation(stream_id="group-A", raw_prompt="猫", delivery="both"))[0]
    await asyncio.wait_for(waiting.wait(), timeout=2)
    assert '"state": "completed"' in plugin._generation_feedback.text("group-A")
    assert any(c.args[0] == "maisaka.proactive.trigger" for c in plugin.ctx.call_capability.await_args_list)
    pending = list(runtime.COMMAND_TASKS)
    release.set()
    await asyncio.gather(*pending)


@pytest.mark.asyncio
async def test_reference_id_is_never_silently_ignored_by_text_mode(monkeypatch):
    plugin, client = instance(monkeypatch)
    assert (await plugin._run_generation(stream_id="group-A", raw_prompt="按图画", mode="text_to_image",
                                         reference_message_id="1864376348", delivery="chat"))[0]
    client.generate_image_to_image.assert_awaited_once()
    client.generate_text_to_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_failure_allows_explicit_retry_and_each_retry_has_a_new_id(monkeypatch):
    plugin, client = instance(monkeypatch)
    pending = []
    monkeypatch.setattr(plugin_module, "track_command_task", lambda task, stream: pending.append(task))
    result = await plugin.handle_generate_image(stream_id="group-A", prompt="按图画", mode="image_to_image", delivery="chat")
    await asyncio.gather(*pending)
    assert result["status"] == "queued"
    assert result["background_task"] is True
    assert "Replyer" in result["content"]
    assert not runtime.has_recent_command_job("group-A", 90)
    retry = await plugin.handle_generate_image(stream_id="group-A", prompt="重试按图画", mode="image_to_image", delivery="chat")
    await asyncio.gather(*pending)
    assert retry["task_id"] != result["task_id"]
    client.generate_image_to_image.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_succeeds", [False, True])
async def test_failed_job_reports_whether_error_was_already_visible(monkeypatch, delivery_succeeds):
    plugin, _ = instance(monkeypatch)
    plugin.ctx.send.text.return_value = delivery_succeeds
    success, _ = await plugin._run_generation(
        stream_id="group-A", raw_prompt="按图画", mode="image_to_image", use_recent_image=True,
        delivery="chat", job_id="error-delivery",
    )
    assert not success
    job = plugin._generation_feedback._streams["group-A"]["error-delivery"]
    assert job["error_notified"] is delivery_succeeds
    trigger = next(call for call in plugin.ctx.call_capability.await_args_list
                   if call.args[0] == "maisaka.proactive.trigger")
    assert trigger.kwargs["metadata"]["error_notified"] is delivery_succeeds


@pytest.mark.asyncio
async def test_error_send_exception_still_wakes_planner(monkeypatch):
    plugin, _ = instance(monkeypatch)
    plugin.ctx.send.text.side_effect = ConnectionError("send failed")
    success, _ = await plugin._run_generation(
        stream_id="group-A", raw_prompt="按图画", mode="image_to_image", use_recent_image=True,
        delivery="chat", job_id="send-failed",
    )
    assert not success
    assert plugin._generation_feedback._streams["group-A"]["send-failed"]["error_notified"] is False
    assert any(call.args[0] == "maisaka.proactive.trigger" for call in plugin.ctx.call_capability.await_args_list)


@pytest.mark.asyncio
async def test_upstream_error_is_redacted_before_internal_feedback(monkeypatch):
    plugin, client = instance(monkeypatch)
    cfg = plugin._effective_config()
    cfg["profiles"]["items"]["openai_default"]["api_key"] = "sensitive-test-key"
    client.generate_text_to_image.side_effect = ValueError("rejected sensitive-test-key")
    assert not (await plugin._run_generation(stream_id="group-A", raw_prompt="猫", delivery="chat"))[0]
    text = plugin._generation_feedback.text("group-A")
    assert "sensitive-test-key" not in text and "[redacted]" in text


@pytest.mark.asyncio
async def test_cancelled_job_publishes_terminal_state_and_does_not_block_retry(monkeypatch):
    plugin, client = instance(monkeypatch)
    entered = asyncio.Event()
    async def wait(prompt):
        entered.set()
        await asyncio.Event().wait()
    client.generate_text_to_image.side_effect = wait
    runtime.mark_command_job("group-A", "猫", job_id="cancel-job")
    task = asyncio.create_task(plugin._run_generation(stream_id="group-A", raw_prompt="猫", delivery="chat", job_id="cancel-job"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert '"state": "cancelled"' in plugin._generation_feedback.text("group-A")
    assert not runtime.has_recent_command_job("group-A", 90)
    plugin.ctx.send.text.assert_not_awaited()


def test_older_task_completion_cannot_release_newer_job():
    runtime.mark_command_job("group-A", "新请求", job_id="new-job")
    runtime.finish_command_job("group-A", "old-job", "failed")
    assert runtime.has_recent_command_job("group-A", 90)


def test_feedback_is_bounded_and_distinguishes_old_failed_and_new_pending_jobs():
    feedback = GenerationFeedback()
    feedback.update("group-A", "old", state="failed", prompt="旧请求", detail="缺图")
    feedback.update("group-A", "new", state="queued", prompt="新请求")
    text = feedback.text("group-A")
    assert '"task_id": "old", "state": "failed"' in text
    assert '"task_id": "new", "state": "queued"' in text
    for i in range(20):
        feedback.update("group-A", str(i), state="completed", prompt="猫")
    assert len(feedback._streams["group-A"]) == 8


@pytest.mark.asyncio
async def test_recent_history_with_expired_or_only_foreign_images_has_no_reference():
    stale = image_message()
    stale["timestamp"] = str(time.time() - 901)
    call = AsyncMock(return_value={"success": True, "messages": [stale, image_message("group-B")]})
    assert await references.resolve_reference(SimpleNamespace(call_capability=call), "group-A") is None
    call.assert_awaited_once()


@pytest.mark.parametrize("value", ["图片描述", base64.b64encode(b"not an image").decode(), "http://example.org/x.png"])
def test_descriptions_urls_and_non_images_are_not_treated_as_raw_reference(value):
    with pytest.raises(ValueError):
        references.image_data_url(value)
