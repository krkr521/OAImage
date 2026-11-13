"""验证 OpenAI 整理、真实上传顺序及 Sunburst 的 JSON/multipart 参数。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import base64
import json

import pytest
from aiohttp import web

import plugin as plugin_module
import oa_image_generation as generation
from oa_image_config import _resolve_profile_config
from oa_image_openai_prompt import build_openai_rewrite_request
from oa_image_prompt import rewrite_image_prompt
from test_novelai import PNG, config, server


B64 = base64.b64encode(PNG).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("user_request", [
    '画一张二次元水彩海报，文字保持“夏日小憩！”，人物全身。',
    '拍摄感的真实海边照片，只参考第一张图的衣服，不换脸。',
])
async def test_openai_rewriter_receives_original_language_and_safe_reference_manifest(user_request):
    cfg = _resolve_profile_config(config(), "openai_default")[0]
    cfg["api"]["api_key"] = "do-not-forward-this-secret"
    cfg["runtime"] = {
        "generation_mode": "image_to_image", "private_path": "do-not-forward-this-path",
        "reference_images": [
            {"kind": "user_reference", "label": "服装", "binary_data_base64": B64},
            {"kind": "bot_identity", "label": "八千代"},
        ],
    }
    original = deepcopy(cfg)
    call = AsyncMock(return_value={"success": True, "response": json.dumps({
        "decision": "generate", "prompt": user_request,
    }, ensure_ascii=False)})
    assert await rewrite_image_prompt("same-chat scene data", cfg, call, request_text=user_request) == user_request
    payload = call.await_args.kwargs
    messages = payload["prompt"]
    assert [item["role"] for item in messages] == ["system", "user"]
    data = json.loads(messages[1]["content"])
    assert data["request"] == user_request
    assert data["mode"] == "image_to_image"
    assert data["reference_images"] == [
        {"index": 1, "kind": "user_reference", "label": "服装"},
        {"index": 2, "kind": "bot_identity", "label": "八千代"},
    ]
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "do-not-forward" not in serialized and B64 not in serialized
    assert payload["task_name"] == "planner" and "model_name" not in payload
    assert payload["max_tokens"] == 2200
    assert cfg == original


def test_no_reference_is_invented_from_scene_text():
    cfg = _resolve_profile_config(config(), "openai_default")[0]
    data = json.loads(build_openai_rewrite_request("之前群里有一张图", cfg)[1]["content"])
    assert data["reference_images"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["planner", " PLANNER ", "separately-configured-model"])
async def test_rewriter_follows_task_configuration_or_respects_explicit_model(model):
    cfg = _resolve_profile_config(config(), "openai_default")[0]
    cfg["prompt"]["rewrite_model"] = model
    call = AsyncMock(return_value={"success": True, "response": '{"decision":"generate","prompt":"海边插画。"}'})
    assert await rewrite_image_prompt("海边", cfg, call) == "海边插画。"
    route = call.await_args.kwargs
    if model.strip().casefold() == "planner":
        assert route["task_name"] == "planner" and "model_name" not in route
    else:
        assert route["task_name"] == "utils" and route["model_name"] == model


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["", None, 17])
async def test_invalid_model_configuration_does_not_silently_select_another_model(model):
    cfg = _resolve_profile_config(config(), "openai_default")[0]
    cfg["prompt"]["rewrite_model"] = model
    call = AsyncMock()
    with pytest.raises(ValueError, match="未配置"):
        await rewrite_image_prompt("海边", cfg, call)
    call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
async def test_sunburst_pipeline_sends_rewritten_prompt_and_high_quality_to_real_http(edit):
    received = []

    async def handler(request):
        if request.content_type == "application/json":
            data = await request.json()
        else:
            data = {}
            multipart = await request.multipart()
            while (part := await multipart.next()) is not None:
                data[part.name] = bytes(await part.read()) if part.filename else await part.text()
        received.append(data)
        return web.json_response({"data": [{"b64_json": B64}]})

    cfg = config()
    profile = cfg["profiles"]["items"]["openai_default"]
    profile.update(api_key="test-secret", generations_path="/ai/generate-image", edits_path="/ai/generate-image")
    prepared = '二维水彩海报，人物全身位于海边；只出现文字“夏日小憩！”。'
    call = AsyncMock(return_value={"success": True, "response": json.dumps({
        "decision": "generate", "prompt": prepared,
    }, ensure_ascii=False)})

    async def builder(prompt, stream_id, effective, mode):
        return await rewrite_image_prompt(prompt, effective, call, request_text=prompt)

    send = AsyncMock(return_value=True)
    async with server(handler) as url:
        profile["base_url"] = url
        success, _ = await generation.run_generation(
            stream_id="openai-http-edit" if edit else "openai-http-text", raw_prompt="画一张水彩二次元海报",
            config=cfg, profile_name="openai_default", mode="image_to_image" if edit else "text_to_image",
            reference_image_base64=B64 if edit else None, use_bot_appearance=False,
            delivery="chat", send_image=send, context_builder=builder,
        )
    assert success and send.await_count == 1 and len(received) == 1
    actual = received[0]
    assert actual["model"] == "gpt-image-2.6-sunburst"
    assert actual["quality"] == "high" and actual["size"] == "auto"
    assert prepared in actual["prompt"] and "response_format" not in actual
    assert actual.get("image") == PNG if edit else "image" not in actual
    data = json.loads(call.await_args.kwargs["prompt"][1]["content"])
    assert data["mode"] == ("image_to_image" if edit else "text_to_image")
    assert len(data["reference_images"]) == int(edit)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["not-json", '{"decision":"refuse","prompt":""}', '{"decision":"generate","prompt":""}'])
async def test_openai_rewrite_failure_stops_before_image_request(monkeypatch, response):
    client = SimpleNamespace(base_url="https://example.invalid", generate_text_to_image=AsyncMock())
    monkeypatch.setattr(generation, "OpenAIImageClient", lambda _: client)
    call = AsyncMock(return_value={"success": True, "response": response})

    async def builder(prompt, stream_id, effective, mode):
        return await rewrite_image_prompt(prompt, effective, call)

    success, _ = await generation.run_generation(
        stream_id="openai-rewrite-failure", raw_prompt="画海边", config=config(),
        profile_name="openai_default", mode="text_to_image", use_bot_appearance=False,
        delivery="chat", send_image=AsyncMock(), context_builder=builder,
    )
    assert not success
    client.generate_text_to_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_clothing_bot_identity_and_character_upload_order_are_consistent(monkeypatch):
    bot = base64.b64encode(PNG + b"bot").decode()
    char = base64.b64encode(PNG + b"character").decode()
    monkeypatch.setattr(generation, "_load_bot_appearance_image", lambda _: (bot, "image/png", "bot.png"))
    monkeypatch.setattr(generation, "resolve_character_images", lambda *_: ([SimpleNamespace(
        name="角色甲", image_base64=char, mime="image/png", filename="character.png",
    )], []))
    client = SimpleNamespace(base_url="https://example.invalid", generate_image_to_image=AsyncMock(return_value=B64))
    monkeypatch.setattr(generation, "OpenAIImageClient", lambda _: client)
    seen = []

    async def builder(prompt, stream_id, effective, mode):
        seen.append(effective["runtime"]["reference_images"])
        return "只参考 Image 1 的服装；Image 2 为八千代本人；Image 3 为角色甲。"

    cfg = config()
    original = deepcopy(cfg)
    success, _ = await generation.run_generation(
        stream_id="openai-reference-order", raw_prompt="你和角色甲换上参考衣服", config=cfg,
        mode="image_to_image", reference_image_base64=B64, profile_name="openai_default",
        use_bot_appearance=True, delivery="chat", send_image=AsyncMock(return_value=True),
        context_builder=builder,
    )
    assert success and cfg == original
    assert [item["kind"] for item in seen[0]] == ["user_reference", "bot_identity", "character_identity"]
    args = client.generate_image_to_image.await_args
    assert args.args[1] == B64
    assert [item[0] for item in args.kwargs["additional_images"]] == [bot, char]
    assert "clothing/style alone does not define the target's identity" in args.args[0]


@pytest.mark.asyncio
async def test_real_plugin_context_builder_keeps_recent_chat_scoped():
    plugin = plugin_module.OAImagePlugin()
    cfg = _resolve_profile_config(config(), "openai_default")[0]
    calls = []

    async def capability(name, **kwargs):
        calls.append((name, kwargs))
        if name == "message.get_recent":
            assert kwargs["chat_id"] == "only-this-chat"
            return {"success": True, "messages": [{"message_id": "current"}]}
        return {"success": True, "text": "只取本群：用户要水彩二次元。"}

    llm = AsyncMock(return_value={"success": True, "response": '{"decision":"generate","prompt":"水彩二次元海边画面。"}'})
    plugin._ctx = SimpleNamespace(call_capability=capability, llm=SimpleNamespace(generate=llm), logger=Mock())
    assert await plugin._build_context_prompt("海边", "only-this-chat", cfg, "text_to_image") == "水彩二次元海边画面。"
    data = json.loads(llm.await_args.kwargs["prompt"][1]["content"])
    assert "只取本群" in data["scene"] and len(calls) == 2
