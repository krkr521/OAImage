"""回放静态图调用误带 GIF 默认值的真实参数组合。"""

from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import base64

from aiohttp import web
import pytest

import plugin as plugin_module
import oa_image_generation as generation
from oa_image_animation import resolve_output_config
from oa_image_clients import OpenAIImageClient
from test_novelai import PNG, config, server


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_recorded_reference_edit_with_gif_defaults_queues_as_static(monkeypatch, nested):
    plugin = plugin_module.OAImagePlugin()
    run = AsyncMock()
    tasks = []
    monkeypatch.setattr(plugin, "_effective_config", config)
    monkeypatch.setattr(plugin, "_run_generation", run)
    monkeypatch.setattr(plugin_module, "mark_command_job", Mock())
    monkeypatch.setattr(plugin_module, "track_command_task", lambda task, _: tasks.append(task))
    arguments = {
        "characters": [], "delivery": "both", "frame_count": 9, "frame_duration_ms": 200,
        "image_source": "", "mode": "image_to_image", "output_format": "auto",
        "profile": "openai_default", "prompt": "保留参考人物，把背景改成夜晚街道。",
        "reference_image_index": 0, "reference_message_id": "1983880463",
        "use_bot_appearance": False, "use_recent_image": False,
    }
    result = await plugin.handle_generate_image(
        stream_id="recorded-static-parameters", **({"action_data": arguments} if nested else arguments),
    )
    assert result["success"] and result["status"] == "queued" and len(tasks) == 1
    await tasks[0]
    run.assert_awaited_once()
    actual = run.await_args.kwargs
    assert actual["frame_count"] is None and actual["frame_duration_ms"] is None
    assert actual["output_format"] == "auto" and actual["mode"] == "image_to_image"
    assert actual["reference_message_id"] == arguments["reference_message_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
async def test_static_http_payload_drops_animation_fields_in_json_and_multipart(edit):
    received = []
    b64 = base64.b64encode(PNG).decode()

    async def endpoint(request):
        if request.content_type == "application/json":
            payload = await request.json()
        else:
            payload = {}
            reader = await request.multipart()
            while (part := await reader.next()) is not None:
                payload[part.name] = bytes(await part.read()) if part.filename else await part.text()
        received.append(payload)
        return web.json_response({"data": [{"b64_json": b64}]})

    cfg = config()
    profile = cfg["profiles"]["items"]["openai_default"]
    profile.update(generations_path="/ai/generate-image", edits_path="/ai/generate-image")
    # 同时覆盖 Tool 位置参数与旧透传配置中的动画字段，两者均不能污染静态 API。
    profile["params"].update(frame_count=9, frame_duration_ms=200)
    send = AsyncMock(return_value=True)
    async with server(endpoint) as url:
        profile["base_url"] = url
        original = deepcopy(cfg)
        success, _ = await generation.run_generation(
            stream_id="static-edit-isolation" if edit else "static-text-isolation",
            raw_prompt="把背景改成夜晚街道", config=cfg,
            mode="image_to_image" if edit else "text_to_image", output_format="auto",
            frame_count=9, frame_duration_ms=200, reference_image_base64=b64 if edit else None,
            use_bot_appearance=False, delivery="chat", send_image=send,
        )
    assert success and cfg == original and len(received) == 1
    send.assert_awaited_once()
    payload = received[0]
    assert not {"frame_count", "frame_duration_ms", "response_format", "output_format"} & payload.keys()
    assert payload["quality"] == "high" and payload["size"] == "auto"
    assert payload["model"] == "gpt-image-2.6-sunburst"
    assert (payload.get("image") == PNG) if edit else "image" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_mode", ["json_edit", "json_generation"])
async def test_json_edit_client_also_excludes_frame_parameters(edit_mode):
    received = []
    b64 = base64.b64encode(PNG).decode()

    async def endpoint(request):
        received.append(await request.json())
        return web.json_response({"data": [{"b64_json": b64}]})

    cfg = config()
    profile = cfg["profiles"]["items"]["openai_default"]
    profile["generation"]["image_to_image_mode"] = edit_mode
    profile.update(generations_path="/ai/generate-image", edits_path="/ai/generate-image")
    profile["params"].update(frame_count=9, frame_duration_ms=200)
    async with server(endpoint) as url:
        profile["base_url"] = url
        effective, _, _ = resolve_output_config(cfg)
        result = await OpenAIImageClient(effective).generate_image_to_image("edit", b64)
    assert result == b64 and len(received) == 1
    assert not {"frame_count", "frame_duration_ms"} & received[0].keys()
    assert received[0]["image"] == b64


@pytest.mark.asyncio
async def test_gif_nullable_arguments_read_animation_defaults_and_remain_advertised(monkeypatch):
    plugin = plugin_module.OAImagePlugin()
    cfg = config()
    cfg["animation"].update(frame_count=15, frame_duration_ms=120)
    run = AsyncMock()
    tasks = []
    monkeypatch.setattr(plugin, "_effective_config", lambda: cfg)
    monkeypatch.setattr(plugin, "_run_generation", run)
    monkeypatch.setattr(plugin_module, "mark_command_job", Mock())
    monkeypatch.setattr(plugin_module, "track_command_task", lambda task, _: tasks.append(task))
    result = await plugin.handle_generate_image(
        prompt="小猫挥手，循环", stream_id="nullable-gif", output_format="gif", frame_count=None, frame_duration_ms=None,
    )
    assert result["success"]
    await tasks[0]
    effective, _, _ = resolve_output_config(cfg, output_format=run.await_args.kwargs["output_format"])
    assert effective["runtime"]["frame_count"] == 15 and effective["runtime"]["frame_duration_ms"] == 120
    component = next(c for c in plugin.get_components() if c["name"] == "generate_image")
    properties = component["metadata"]["parameters_raw"]["properties"]
    for key in ("frame_count", "frame_duration_ms"):
        assert properties[key]["type"] == ["integer", "null"] and properties[key]["default"] is None
    assert "普通静态图" in component["metadata"]["description"]
