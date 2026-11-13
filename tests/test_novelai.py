"""验证原生协议、提示词整理、后台任务与会话隔离；不调用图片服务或发送 QQ。"""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio
import base64
import copy
import io
import json
import sys
import zipfile

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plugin as plugin_module
import oa_image_generation as generation
import oa_image_runtime as runtime
from oa_image_config import _resolve_profile_config
from oa_image_novelai import NovelAIImageClient
from oa_image_prompt import rewrite_image_prompt


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jJ1kAAAAASUVORK5CYII=")


def config():
    result = plugin_module.OAImagePlugin.build_default_config()
    result["plugin"]["enabled"] = True
    result["profiles"]["items"]["novelai"]["api_key"] = "test-secret"
    # 既有协议/回发测试继续覆盖 V4.5；V5 与资料读取在独立测试中验证。
    result["profiles"]["items"]["novelai"]["model"] = "nai-diffusion-4-5-curated"
    result["behavior"]["send_error_messages"] = True
    return result


def resolved():
    return _resolve_profile_config(config(), "novelai")[0]


@asynccontextmanager
async def server(handler):
    app = web.Application()
    app.router.add_post("/ai/generate-image", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["json", "zip"])
async def test_native_http_protocol_and_decoding(response_kind):
    observed = []

    async def handler(request):
        observed.append(await request.json())
        assert request.headers["Authorization"] == "Bearer test-secret"
        if response_kind == "json":
            return web.json_response({"images": [{"image": base64.b64encode(PNG).decode()}]}, status=201)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("image_0.png", PNG)
        return web.Response(body=buffer.getvalue(), content_type="application/zip", status=201)

    async with server(handler) as url:
        c = resolved()
        c["api"]["base_url"] = url
        image = await NovelAIImageClient(c).generate_text_to_image("cat, blue_sky")
    assert base64.b64decode(image) == PNG
    assert len(observed) == 1
    body = observed[0]
    assert body["action"] == "generate"
    assert body["parameters"]["n_samples"] == 1
    assert body["parameters"]["width"] == 832
    assert body["parameters"]["v4_prompt"]["caption"]["base_caption"] == body["input"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403, 429, 500])
async def test_http_error_does_not_retry_or_echo_response(status):
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        return web.Response(status=status, text="private-upstream-body test-secret")

    async with server(handler) as url:
        c = resolved()
        c["api"]["base_url"] = url
        with pytest.raises(ValueError, match=f"HTTP {status}") as error:
            await NovelAIImageClient(c).generate_text_to_image("cat")
    assert "private-upstream-body" not in str(error.value)
    assert "test-secret" not in str(error.value)
    assert count == 1


@pytest.mark.parametrize("size", ["invalid", "0x64", "8192x8192", "833x1216"])
def test_invalid_size_is_rejected_without_silent_clamping(size):
    c = resolved()
    c["generation"]["size"] = size
    with pytest.raises(ValueError):
        NovelAIImageClient(c).build_payload("cat")


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["", "plain text", "{}", '{"decision":"generate","prompt":""}', '{"decision":"refuse","prompt":""}'])
async def test_invalid_or_refused_prompt_retries_once_then_stops(response):
    call = AsyncMock(return_value={"success": True, "response": response})
    with pytest.raises(ValueError):
        await rewrite_image_prompt("request", resolved(), call)
    # 第一次拒绝或无法解析时追加一次强化说明；两次都不可用才停止。
    assert call.await_count == 2
    assert "RETRY" in call.await_args.kwargs["prompt"]


@pytest.mark.asyncio
async def test_refused_first_answer_is_retried_and_can_succeed():
    call = AsyncMock(side_effect=[
        {"success": True, "response": '{"decision":"refuse","prompt":""}'},
        {"success": True, "response": '{"decision":"generate","prompt":"cat, window"}'},
    ])
    assert await rewrite_image_prompt("request", resolved(), call) == "cat, window"
    assert call.await_count == 2


@pytest.mark.asyncio
async def test_prompt_model_is_selected_explicitly_and_envelope_is_supported():
    call = AsyncMock(return_value={"success": True, "result": {
        "success": True, "response": '{"decision":"generate","prompt":"cat, blue_sky"}',
    }})
    assert await rewrite_image_prompt("current scene", resolved(), call) == "cat, blue_sky"
    assert call.await_args.kwargs["model_name"] == "deepseek-v4-flash-fast"
    assert call.await_args.kwargs["task_name"] == "utils"
    assert call.await_args.kwargs["timeout_ms"] == 60000


@pytest.mark.asyncio
async def test_two_concurrent_chats_keep_context_and_delivery_separate(monkeypatch):
    plugin = plugin_module.OAImagePlugin()
    ctx = SimpleNamespace(send=SimpleNamespace(image=AsyncMock(return_value=True)), logger=Mock())

    async def capability(name, **kwargs):
        if name == "message.get_recent":
            assert kwargs["chat_id"] in {"group-A", "group-B"}
            return {"success": True, "result": {"success": True, "messages": [{"stream": kwargs["chat_id"]}]}}
        return {"success": True, "text": "context for " + kwargs["messages"][0]["stream"]}

    async def llm(**kwargs):
        text = kwargs["prompt"]
        prompt = "blue_umbrella" if "context for group-A" in text else "red_umbrella"
        return {"success": True, "response": json.dumps({"decision": "generate", "prompt": prompt})}

    class FakeClient:
        base_url = "https://example.invalid"
        def __init__(self, config):
            pass
        def validate_configuration(self):
            pass
        async def generate_text_to_image(self, prompt):
            await asyncio.sleep(0)
            return prompt

    ctx.call_capability = AsyncMock(side_effect=capability)
    ctx.llm = SimpleNamespace(generate=AsyncMock(side_effect=llm))
    plugin._ctx = ctx
    monkeypatch.setattr(plugin, "_effective_config", config)
    monkeypatch.setattr(generation, "NovelAIImageClient", FakeClient)
    results = await asyncio.gather(*[
        plugin._run_generation(stream_id=stream, raw_prompt="按刚才说的画一把伞", profile_name="novelai", delivery="chat")
        for stream in ["group-A", "group-B"]
    ])
    assert all(result[0] for result in results)
    assert {(call.args[0], call.args[1]) for call in ctx.send.image.await_args_list} == {
        ("blue_umbrella", "group-A"), ("red_umbrella", "group-B"),
    }


@pytest.mark.parametrize("request_text,keeps_command", [
    ("画一只白猫坐在窗边", False),
    ("接着上一张改，把背景换成夜晚", True),
    ("再画一张一样的", True),
])
def test_previous_image_commands_only_survive_explicit_continuation(request_text, keeps_command):
    messages = [
        {"is_command": True, "processed_plain_text": "/生图r 红发少女站在雨里"},
        {"is_command": False, "processed_plain_text": "今天好热"},
    ]
    result = plugin_module._drop_previous_image_commands(messages, request_text)
    assert any(message.get("is_command") for message in result) is keeps_command
    assert len(result) == (2 if keeps_command else 1)


def test_command_text_is_recognised_without_is_command_flag():
    messages = [
        {"processed_plain_text": "/生图 一只黑猫"},
        {"processed_plain_text": "普通的聊天"},
    ]
    kept = plugin_module._drop_previous_image_commands(messages, "画一只白猫")
    assert [message["processed_plain_text"] for message in kept] == ["普通的聊天"]


@pytest.mark.asyncio
@pytest.mark.parametrize("request_text,expect_command", [
    ("画一只白猫坐在窗边", False),
    ("接着上一张改，把背景换成夜晚", True),
])
async def test_context_prompt_excludes_previous_image_command(monkeypatch, request_text, expect_command):
    plugin = plugin_module.OAImagePlugin()
    seen = []

    async def capability(name, **kwargs):
        if name == "message.get_recent":
            return {"success": True, "messages": [
                {"is_command": True, "processed_plain_text": "/生图r 红发少女站在雨里"},
                {"is_command": False, "processed_plain_text": "今天好热"},
            ]}
        if name == "message.build_readable":
            seen.append([message.get("processed_plain_text") for message in kwargs["messages"]])
            return {"success": True, "text": "聊天上下文"}
        return {"success": True}

    plugin._ctx = SimpleNamespace(call_capability=AsyncMock(side_effect=capability), logger=Mock())
    config_data = {
        "api": {"provider": "openai"},
        "prompt": {
            "include_context": True, "context_hours": 0.5, "context_message_limit": 20,
            "context_character_limit": 8000, "rewrite_enabled": False,
        },
    }
    scene = await plugin._build_context_prompt(request_text, "stream", config_data, "text_to_image")
    assert seen and ("/生图r 红发少女站在雨里" in seen[0]) is expect_command
    assert request_text in scene


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt,expected_note", [
    ("1girl, blue sky, watercolor", True),
    ("1girl, nude, rating:explicit", False),
])
async def test_silent_novelai_result_enters_context_only_when_rating_is_safe(monkeypatch, prompt, expected_note):
    plugin = plugin_module.OAImagePlugin()
    appended = []

    async def capability(name, **kwargs):
        if name == "maisaka.context.append":
            appended.append(kwargs)
        return {"success": True}

    ctx = SimpleNamespace(
        call_capability=AsyncMock(side_effect=capability), logger=Mock(),
        send=SimpleNamespace(image=AsyncMock(return_value=True), text=AsyncMock(return_value=True)),
    )

    class FakeClient:
        base_url = "https://example.invalid"
        def __init__(self, selected):
            pass
        def validate_configuration(self):
            pass
        async def generate_text_to_image(self, request_prompt):
            return base64.b64encode(PNG).decode()

    plugin._ctx = ctx
    monkeypatch.setattr(plugin, "_effective_config", config)
    monkeypatch.setattr(plugin, "_build_context_prompt", AsyncMock(return_value=prompt))
    monkeypatch.setattr(generation, "NovelAIImageClient", FakeClient)
    success, _ = await plugin._run_generation(
        stream_id="group-A", raw_prompt="画一张", profile_name="novelai", silent_context=True,
    )
    assert success
    assert bool(appended) is expected_note
    if expected_note:
        assert appended[0]["source_kind"] == "oaimage.generation.safe_result"
        assert appended[0]["replyable"] is False
        assert "rating:general" in appended[0]["visible_text"]
    # 无论评级如何都不唤醒 Planner。
    assert not any(call.args[0] == "maisaka.proactive.trigger" for call in ctx.call_capability.await_args_list)
    assert plugin._generation_feedback.text("group-A") == ""


@pytest.mark.asyncio
async def test_silent_novelai_respects_configured_rating_allow(monkeypatch):
    plugin = plugin_module.OAImagePlugin()
    cfg = config()
    cfg["behavior"]["context_rating_allow"] = []
    appended = []

    async def capability(name, **kwargs):
        if name == "maisaka.context.append":
            appended.append(kwargs)
        return {"success": True}

    ctx = SimpleNamespace(
        call_capability=AsyncMock(side_effect=capability), logger=Mock(),
        send=SimpleNamespace(image=AsyncMock(return_value=True), text=AsyncMock(return_value=True)),
    )

    class FakeClient:
        base_url = "https://example.invalid"
        def __init__(self, selected):
            pass
        def validate_configuration(self):
            pass
        async def generate_text_to_image(self, request_prompt):
            return base64.b64encode(PNG).decode()

    plugin._ctx = ctx
    monkeypatch.setattr(plugin, "_effective_config", lambda: cfg)
    monkeypatch.setattr(plugin, "_build_context_prompt", AsyncMock(return_value="1girl, blue sky"))
    monkeypatch.setattr(generation, "NovelAIImageClient", FakeClient)
    assert (await plugin._run_generation(
        stream_id="group-A", raw_prompt="画一张", profile_name="novelai", silent_context=True,
    ))[0]
    assert appended == []


@pytest.mark.asyncio
async def test_missing_token_fails_before_llm_and_reports_to_original_chat():
    c = config()
    c["profiles"]["items"]["novelai"]["api_key"] = ""
    prepare, send_image, send_text = AsyncMock(), AsyncMock(), AsyncMock()
    success, message = await generation.run_generation(
        config=c, stream_id="original-group", raw_prompt="a cat", profile_name="novelai",
        context_builder=prepare, send_image=send_image, send_text=send_text,
    )
    assert success is False and "Token" in message
    prepare.assert_not_awaited()
    send_image.assert_not_awaited()
    assert send_text.await_args.args[1] == "original-group"


@pytest.mark.asyncio
async def test_failed_context_read_does_not_call_llm():
    plugin = plugin_module.OAImagePlugin()
    llm = AsyncMock()
    plugin._ctx = SimpleNamespace(call_capability=AsyncMock(return_value={"success": False, "error": "unavailable"}), llm=SimpleNamespace(generate=llm))
    with pytest.raises(ValueError, match="读取生图上下文失败"):
        await plugin._build_context_prompt("a cat", "original", resolved(), "text_to_image")
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_limits_concurrent_http_requests():
    runtime.PROVIDER_SEMAPHORES.clear()
    active, maximum = 0, 0
    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        await asyncio.sleep(0.015)
        active -= 1
        return web.json_response({"images": [{"image": base64.b64encode(PNG).decode()}]}, status=201)
    async with server(handler) as url:
        c = resolved()
        c["api"]["base_url"] = url
        client = NovelAIImageClient(c)
        await asyncio.gather(*[client.generate_text_to_image("cat") for _ in range(3)])
    assert maximum == 1


@pytest.mark.asyncio
async def test_busy_stream_does_not_call_prompt_model_twice(monkeypatch):
    c = config()
    entered, release = asyncio.Event(), asyncio.Event()
    async def prepare(*args):
        entered.set()
        await release.wait()
        return "cat"
    call = AsyncMock(side_effect=prepare)
    fake = SimpleNamespace(base_url="https://example.invalid", validate_configuration=Mock(), generate_text_to_image=AsyncMock(return_value="image"))
    monkeypatch.setattr(generation, "NovelAIImageClient", lambda c: fake)
    kwargs = dict(config=c, stream_id="busy-test", raw_prompt="a cat", profile_name="novelai", delivery="chat", context_builder=call, send_image=AsyncMock(return_value=True))
    task = asyncio.create_task(generation.run_generation(**kwargs))
    await entered.wait()
    try:
        assert await generation.run_generation(**kwargs) == (False, "stream is busy")
        assert call.await_count == 1
    finally:
        release.set()
        await task


def test_profile_changes_do_not_mutate_existing_defaults():
    c = config()
    before = copy.deepcopy(c)
    native, _, _ = _resolve_profile_config(c, "novelai")
    original, _, _ = _resolve_profile_config(c, "openai_default")
    assert native["behavior"]["default_delivery"] == "chat"
    assert native["prompt"]["rewrite_enabled"] is True
    assert original["behavior"]["default_delivery"] == "both"
    assert original["prompt"]["rewrite_enabled"] is True
    assert original["prompt"]["rewrite_max_tokens"] == 2200
    assert native["prompt"]["rewrite_max_tokens"] == 3000
    assert c == before


@pytest.mark.asyncio
async def test_unload_cancels_background_work_and_releases_locks():
    lock = runtime.get_stream_lock("cancel-test")
    started = asyncio.Event()
    async def job():
        async with lock:
            started.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(job())
    runtime.track_command_task(task, "cancel-test")
    await started.wait()
    await runtime.cancel_command_tasks()
    assert task.cancelled()
    assert not lock.locked()
    assert not runtime.COMMAND_TASKS
