"""验证真实官方查询结果进入 DeepSeek，以及固定 V5 时不会读取角色库图片。"""

from pathlib import Path
from unittest.mock import AsyncMock

import base64
import json
import sys

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import oa_image_generation as generation
import oa_image_prompt as prompts
from oa_image_config import _resolve_profile_config
from oa_image_prompt_types import NovelAIPrompt
from oa_image_tag_search import NovelAITagSearch, select_and_search_tags
from test_novelai import config, PNG


def tag_config(url="http://127.0.0.1:1"):
    cfg = _resolve_profile_config(config(), "novelai")[0]
    cfg["api"].update(model="nai-diffusion-5-full", base_url=url)
    cfg["prompt"].update(tag_search_enabled=True, tag_search_result_limit=2)
    return cfg


def final_response():
    return {"success": True, "response": json.dumps({
        "decision": "generate", "prompt": "1girl, hatsune miku, park", "character_prompts": [],
        "contains_text": False, "quality_preset": "standard", "uc_preset": "light", "negative_prompt": "",
    })}


@pytest.mark.asyncio
async def test_official_search_results_are_supplied_to_second_deepseek_request(monkeypatch):
    observed = []

    async def handler(request):
        assert request.method == "GET"
        assert request.query["model"] == "nai-diffusion-5-full"
        assert request.headers["Authorization"] == "Bearer test-secret"
        observed.append(request.query["prompt"])
        return web.Response(text=json.dumps({"tags": [{"tag": "hatsune miku", "count": 100, "confidence": 0.8}]}), content_type="text/plain")

    app = web.Application()
    app.router.add_get("/ai/generate-image/suggest-tags", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    call = AsyncMock(side_effect=[
        {"success": True, "response": json.dumps({"decision": "search", "queries": ["hatsune miku"]})},
        final_response(),
    ])
    monkeypatch.setattr(prompts, "load_skill_guidance", lambda *args: ("skill rules", ("novelai-assistant",)))
    try:
        plan = await prompts.rewrite_image_prompt("初音未来在公园", tag_config(f"http://127.0.0.1:{port}"), call)
    finally:
        await runner.cleanup()
    assert isinstance(plan, NovelAIPrompt)
    assert observed == ["hatsune miku"] and call.await_count == 2
    payload = json.loads(call.await_args_list[1].kwargs["prompt"][1]["content"])
    assert payload["official_tag_search"][0]["tags"][0] == {"tag": "hatsune miku", "count": 100, "confidence": 0.8}
    assert "test-secret" not in json.dumps([c.kwargs for c in call.await_args_list])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_tag_query_error_is_not_reported_as_empty_results(status):
    async def handler(request):
        return web.Response(status=status, text="private-upstream-body test-secret")

    app = web.Application()
    app.router.add_get("/ai/generate-image/suggest-tags", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        with pytest.raises(ValueError, match=f"HTTP {status}") as error:
            await NovelAITagSearch(tag_config(url)).search(["cat"])
        assert "test-secret" not in str(error.value) and "private-upstream" not in str(error.value)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("queries", [[], ["a", "b", "c", "d"], ["line\nbreak"], [""]])
async def test_invalid_search_plan_stops_without_network(queries):
    call = AsyncMock(return_value={"success": True, "response": json.dumps({"decision": "search", "queries": queries})})
    with pytest.raises(ValueError, match="查询"):
        await select_and_search_tags("scene", tag_config(), call)


@pytest.mark.asyncio
async def test_refused_plan_falls_back_to_no_tags():
    call = AsyncMock(return_value={"success": True, "response": '{"decision":"refuse","queries":[]}'})
    # 标签只是增强，拒绝不应让整张图失败。
    assert await select_and_search_tags("scene", tag_config(), call) == []
    assert call.await_count == 1


@pytest.mark.asyncio
async def test_v5_text_generation_skips_existing_character_reference_images(monkeypatch):
    cfg = config()
    cfg["profiles"]["items"]["novelai"]["model"] = "nai-diffusion-5-full"
    cfg["character_library"]["enabled"] = True

    def forbidden(*args):
        raise AssertionError("NovelAI text-only generation must not read character images")

    class Client:
        base_url = "https://example.invalid"

        def __init__(self, selected):
            assert selected["api"]["model"] == "nai-diffusion-5-full"

        def validate_configuration(self):
            pass

        async def generate_text_to_image(self, prompt):
            return base64.b64encode(PNG).decode()

    monkeypatch.setattr(generation, "resolve_character_images", forbidden)
    monkeypatch.setattr(generation, "_load_bot_appearance_image", forbidden)
    monkeypatch.setattr(generation, "NovelAIImageClient", Client)
    success, _ = await generation.run_generation(
        config=cfg, stream_id="v5-no-reference", raw_prompt="DeepSeek 和 Claude 在公园", characters=["DeepSeek", "Claude"],
        profile_name="novelai", delivery="chat", send_image=AsyncMock(return_value=True),
        context_builder=AsyncMock(return_value=NovelAIPrompt("two people in a park")),
    )
    assert success
