"""GIF 命令、自然语言 Tool 和本地 HTTP 链路；不发送 QQ 消息。"""

from copy import deepcopy
from io import BytesIO
from unittest.mock import AsyncMock, Mock

import asyncio
import base64
import json
import re

from aiohttp import web
from PIL import Image
import pytest

import plugin as plugin_module
import oa_image_generation as generation
from oa_image_animation import extract_animation_options, gif_parameters, resolve_output_config
from oa_image_clients import OpenAIImageClient
from oa_image_openai_prompt import build_openai_rewrite_request
from test_novelai import PNG, config, server


PATTERN = re.compile(plugin_module.OAImagePlugin.handle_draw_image.__maibot_component_info__.command_pattern)


def gif_bytes() -> bytes:
    buffer = BytesIO()
    frames = [Image.new("RGB", (16, 16), color) for color in ("red", "blue")]
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    return buffer.getvalue()


@pytest.mark.parametrize(("command", "mode", "source"), [
    ("/动图", "text_to_image", ""),
    ("/gif", "text_to_image", ""),
    ("/GIF", "text_to_image", ""),
    ("/生图 --gif", "text_to_image", ""),
    ("/draw --gif", "text_to_image", ""),
    ("/图生图 --gif", "image_to_image", ""),
    ("/gif --source https://example.invalid/ref.png", "image_to_image", "https://example.invalid/ref.png"),
])
@pytest.mark.asyncio
async def test_gif_command_queues_once_and_preserves_multiline_brief(monkeypatch, command, mode, source):
    plugin = plugin_module.OAImagePlugin()
    cfg = config()
    cfg["profiles"]["default_profile"] = "novelai"
    run = AsyncMock()
    tasks = []
    monkeypatch.setattr(plugin, "_effective_config", lambda: cfg)
    monkeypatch.setattr(plugin, "_run_generation", run)
    monkeypatch.setattr(plugin_module, "mark_command_job", Mock())
    monkeypatch.setattr(plugin_module, "track_command_task", lambda task, _: tasks.append(task))
    text = command + " --frames 12 --frame-ms=205 小猫抬爪\n放下，首尾循环"
    result = await plugin.handle_draw_image(text=text, stream_id="gif-command", matched_groups=PATTERN.fullmatch(text).groupdict())
    assert result == (True, "", False)
    assert len(tasks) == 1
    await tasks[0]
    run.assert_awaited_once()
    args = run.await_args.kwargs
    assert args["output_format"] == "gif" and args["frame_count"] == 12 and args["frame_duration_ms"] == 210
    assert args["profile_name"] == "openai_default" and args["mode"] == mode
    assert args["raw_prompt"] == "小猫抬爪\n放下，首尾循环" and args["image_source"] == source
    assert args["use_recent_image"] is (mode == "image_to_image" and not source)


@pytest.mark.parametrize("text", ["/gifs cat", "/gift cat", "/动图鉴 cat"])
def test_gif_command_does_not_consume_other_words(text):
    assert PATTERN.fullmatch(text) is None


@pytest.mark.parametrize("text", ["/动图", "/gif --frames 9", "/生图 --gif"])
@pytest.mark.asyncio
async def test_empty_gif_only_shows_help(monkeypatch, text):
    plugin = plugin_module.OAImagePlugin()
    send = AsyncMock()
    run = AsyncMock()
    monkeypatch.setattr(plugin, "_send_text", send)
    monkeypatch.setattr(plugin, "_run_generation", run)
    await plugin.handle_draw_image(text=text, stream_id="gif-help", matched_groups=PATTERN.fullmatch(text).groupdict())
    assert "/动图" in send.await_args.args[0]
    run.assert_not_called()


@pytest.mark.parametrize("text", [
    "/gif --frames 1 cat", "/gif --frames 17 cat", "/gif --frames cat",
    "/gif --frames", "/gif --frame-ms 19 cat", "/gif --frame-ms 2001 cat",
    "/gif --frames 2 --frames 3 cat", "/生图 --frames 9 cat",
    "/gif --profile novelai cat", "/gif --profile nonexistent cat", "/图生图 --gif --frames 17 cat",
])
@pytest.mark.asyncio
async def test_invalid_gif_command_does_not_queue_or_charge(monkeypatch, text):
    plugin = plugin_module.OAImagePlugin()
    run, send, mark = AsyncMock(), AsyncMock(), Mock()
    monkeypatch.setattr(plugin, "_effective_config", config)
    monkeypatch.setattr(plugin, "_run_generation", run)
    monkeypatch.setattr(plugin, "_send_text", send)
    monkeypatch.setattr(plugin_module, "mark_command_job", mark)
    result = await plugin.handle_draw_image(text=text, stream_id="gif-invalid", matched_groups=PATTERN.fullmatch(text).groupdict())
    assert result[0] and result[2]
    send.assert_awaited_once()
    run.assert_not_called()
    mark.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("reference_message_id", ["", "image-1"])
async def test_natural_language_tool_queues_gif_and_advertises_schema(monkeypatch, nested, reference_message_id):
    plugin = plugin_module.OAImagePlugin()
    cfg = config()
    monkeypatch.setattr(plugin, "_effective_config", lambda: cfg)
    monkeypatch.setattr(plugin, "_run_generation", AsyncMock())
    monkeypatch.setattr(plugin_module, "mark_command_job", Mock())
    tasks = []
    monkeypatch.setattr(plugin_module, "track_command_task", lambda task, _: tasks.append(task))
    parameters = {
        "prompt": "给我做一只小猫挥爪的循环动图", "output_format": "gif",
        "frame_count": 12, "frame_duration_ms": 150, "reference_message_id": reference_message_id,
        "mode": "image_to_image" if reference_message_id else "text_to_image", "use_recent_image": False,
    }
    result = await plugin.handle_generate_image(stream_id="natural-gif", **({"action_data": parameters} if nested else parameters))
    assert result["success"] and result["background_task"] and result["status"] == "queued"
    await tasks[0]
    args = plugin._run_generation.await_args.kwargs
    assert args["output_format"] == "gif" and args["frame_count"] == 12 and args["frame_duration_ms"] == 150
    assert args["profile_name"] == "openai_default" and not args["use_recent_image"]
    assert args["reference_message_id"] == reference_message_id and args["mode"] == parameters["mode"]
    components = plugin.get_components()
    tool = next(c for c in components if c["name"] == "generate_image")
    assert "output_format=gif" in tool["metadata"]["description"]
    assert plugin_module._GENERATE_IMAGE_TOOL_PARAMETERS["properties"]["output_format"]["enum"] == ["auto", "gif"]
    assert plugin_module._GENERATE_IMAGE_TOOL_PARAMETERS["properties"]["frame_count"]["maximum"] == 16


@pytest.mark.parametrize("options", [
    {"frame_count": True}, {"frame_count": "9"}, {"frame_duration_ms": 0},
    {"frame_count": 17, "mode": "image_to_image"}, {"reference_image_index": 8},
    {"reference_message_id": "image-1", "image_source": "https://example.invalid/a.png"},
])
@pytest.mark.asyncio
async def test_invalid_tool_gif_rejected_before_queue(monkeypatch, options):
    plugin = plugin_module.OAImagePlugin()
    monkeypatch.setattr(plugin, "_effective_config", config)
    mark = Mock()
    monkeypatch.setattr(plugin_module, "mark_command_job", mark)
    result = await plugin.handle_generate_image(prompt="make a GIF", stream_id="invalid-tool-gif", output_format="gif", **options)
    assert not result["success"] and "content" in result
    mark.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_gifs_preserve_all_frames_and_keep_shared_config_unchanged(monkeypatch):
    raw = gif_bytes()
    encoded = base64.b64encode(raw).decode()
    received, sent = [], []

    async def endpoint(request):
        received.append(await request.json())
        return web.json_response({"data": [{"b64_json": encoded}]})

    async def send(image, *args, **kwargs):
        content = base64.b64decode(image)
        assert content == raw and Image.open(BytesIO(content)).n_frames == 2
        sent.append(image)
        return True

    cfg = config()
    cfg["bot_appearance"].update(enabled=True, natural_prompt="silver hair, blue eyes", reference_image="must-not-be-opened.png")
    profile = cfg["profiles"]["items"]["openai_default"]
    profile.update(api_key="offline-fixture", generations_path="/ai/generate-image")
    profile["params"].update(stream=True, partial_images=2, output_compression=80, prompt_rewriter=True)
    monkeypatch.setattr(generation, "_load_bot_appearance_image", Mock(side_effect=AssertionError("GIF must not read image")))
    monkeypatch.setattr(generation, "resolve_character_images", Mock(side_effect=AssertionError("GIF must not read character images")))
    builder = AsyncMock(side_effect=lambda prompt, *_: prompt)
    async with server(endpoint) as url:
        profile["base_url"] = url
        before = deepcopy(cfg)
        results = await asyncio.gather(*[
            generation.run_generation(
                stream_id=f"concurrent-gif-{count}", raw_prompt="wave and loop", config=cfg, output_format="gif",
                frame_count=count, frame_duration_ms=205, mode="text_to_image", use_bot_appearance=True,
                characters=["Character A"], delivery="chat", send_image=send, context_builder=builder,
            ) for count in (9, 12)
        ])
    assert all(ok for ok, _ in results) and len(sent) == len(received) == 2 and cfg == before
    assert {p["frame_count"] for p in received} == {9, 12}
    for payload in received:
        assert payload["output_format"] == "gif" and payload["frame_duration_ms"] == 210
        assert payload["model"] == "gpt-image-2.6-sunburst" and payload["quality"] == "high"
        assert not {"stream", "partial_images", "output_compression", "prompt_rewriter", "image", "response_format"} & payload.keys()
        assert "silver hair" in payload["prompt"] and "Character A" in payload["prompt"]
    for call in builder.await_args_list:
        cfg_for_rewrite = call.args[2]
        data = json.loads(build_openai_rewrite_request("scene", cfg_for_rewrite)[1]["content"])
        assert not data["reference_images"] and data["api_settings"]["output_format"] == "gif"


@pytest.mark.asyncio
async def test_png_response_to_gif_request_reports_failure_without_sending():
    async def endpoint(request):
        return web.json_response({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    cfg = config()
    profile = cfg["profiles"]["items"]["openai_default"]
    profile["generations_path"] = "/ai/generate-image"
    send, feedback = AsyncMock(), AsyncMock()
    async with server(endpoint) as url:
        profile["base_url"] = url
        success, message = await generation.run_generation(
            stream_id="bad-gif-result", raw_prompt="wave", config=cfg, output_format="gif",
            delivery="chat", send_image=send, result_callback=feedback,
        )
    assert not success and "其他图片格式" in message
    send.assert_not_awaited()
    assert feedback.await_args.kwargs["success"] is False


@pytest.mark.asyncio
async def test_reference_gif_preserves_original_bytes_and_uses_edit_multipart():
    received = []
    reference = base64.b64encode(PNG).decode()
    animated = base64.b64encode(gif_bytes()).decode()

    async def endpoint(request):
        reader = await request.multipart()
        payload = {}
        while (part := await reader.next()) is not None:
            payload[part.name] = bytes(await part.read()) if part.filename else await part.text()
        received.append(payload)
        return web.json_response({"data": [{"b64_json": animated}]})

    cfg = config()
    profile = cfg["profiles"]["items"]["openai_default"]
    profile.update(generations_path="/unexpected-text-generation", edits_path="/ai/generate-image")
    profile["params"].update(stream=True, partial_images=2, output_compression=80)
    send = AsyncMock(return_value=True)
    original = AsyncMock(return_value="data:image/png;base64," + reference)
    async with server(endpoint) as url:
        profile["base_url"] = url
        before = deepcopy(cfg)
        success, message = await generation.run_generation(
            stream_id="reference-gif", raw_prompt="让图中的角色挥手", config=cfg, mode="auto",
            reference_resolver=original, output_format="gif", frame_count=3, frame_duration_ms=150,
            delivery="chat", send_image=send,
        )
    assert success, message
    original.assert_awaited_once()
    send.assert_awaited_once()
    assert send.await_args.args[0] == animated and cfg == before
    assert len(received) == 1
    payload = received[0]
    assert payload["image"] == PNG and payload["output_format"] == "gif"
    assert payload["frame_count"] == "3" and payload["frame_duration_ms"] == "150"
    assert payload["quality"] == "high"
    assert not {"stream", "partial_images", "output_compression", "response_format"} & payload.keys()


@pytest.mark.asyncio
async def test_missing_gif_reference_does_not_fall_back_to_text(monkeypatch):
    client = Mock(side_effect=AssertionError("missing reference must not start a request"))
    monkeypatch.setattr(generation, "OpenAIImageClient", client)
    resolver = AsyncMock(return_value=None)
    send = AsyncMock()
    success, message = await generation.run_generation(
        stream_id="missing-reference-gif", raw_prompt="让图中的角色挥手", config=config(), mode="auto",
        reference_resolver=resolver, output_format="gif", delivery="chat", send_image=send,
    )
    assert not success and "缺少可用参考原图" in message
    resolver.assert_awaited_once()
    client.assert_not_called()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_gif_client_rejects_animated_input_before_http():
    cfg, _, _ = resolve_output_config(config(), output_format="gif")
    client = OpenAIImageClient(cfg)
    with pytest.raises(ValueError, match="静态图"):
        await client.generate_image_to_image("animate", base64.b64encode(gif_bytes()).decode(), "image/gif", "ref.gif")


def test_animation_defaults_rounding_and_static_profile_are_independent():
    cfg = config()
    cfg["profiles"]["default_profile"] = "novelai"
    cfg["animation"].update(frame_count=15, frame_duration_ms=125)
    gif, profile, _ = resolve_output_config(cfg, output_format="gif")
    still, static_profile, _ = resolve_output_config(cfg)
    assert profile == "openai_default" and static_profile == "novelai"
    assert gif["runtime"]["frame_count"] == 15 and gif["runtime"]["frame_duration_ms"] == 130
    assert "output_format" not in still.get("runtime", {})
    assert gif_parameters(16, 2000) == {"frame_count": 16, "frame_duration_ms": 2000}
    assert extract_animation_options("--gif --帧数 2 --帧间隔 20 眨眼")[1]["frame_count"] == 2
