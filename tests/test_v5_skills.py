"""验证资料真实接入、V5 字段编译和文字设置，不请求外部模型或 QQ。"""

from pathlib import Path
from unittest.mock import AsyncMock

import hashlib
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oa_image_novelai import NovelAIImageClient
from oa_image_prompt import rewrite_image_prompt
from oa_image_prompt_types import NovelAICharacterPrompt, NovelAIPrompt
from oa_image_skill import SKILL_COMMIT, SKILL_REPOSITORY, SKILL_VERSION, load_skill_guidance


@pytest.fixture
def bundle(tmp_path):
    paths = {
        "SKILL.md": "# Author setup\nUse api.novelai.me and the author's credentials.\n"
        "## 1.1 Structure\nNOVELAI ASSISTANT CORE GUIDE: Keep fields separate.\n"
        "## 1.2 Weights\nUse numeric weights.\n## 1.3 Artist names\nKeep spellings.\n"
        "## 1.4 Characters\nUse character captions.\n## 1.7 Special features\nPreserve lettering.\n"
        "## 3.1 Tags\nUse precise tags.\n## 3.4 Author preferences\nUnrelated author preference.\n"
        "## 3.6 Expressions\nUse expressions.\n# Other provider\nDo not load provider setup.\n",
        "v5-field-corrections.md": "# Corrections\n" + "".join(
            f"## {number}. Correction\nCORRECTION {number}.\n" for number in range(1, 13)
        ),
        "manga-page-playbook.md": "# Comics\n" + "".join(
            f"## {number}. Panel rule\nUse panels.\n" for number in range(12)
        ) + "## 12. Other workflow\nUnrelated workflow.\n",
        "v5-confirmed-artist-strings.md": "# Recipes\n"
        "## 清新日常 ✅\n0.9::artist:shiromanta::\n"
        "## 厚涂质感 ✅\n1.3::chen bin::\n"
        "## 社区A ✅\nCOMMUNITY_A_RECIPE\n"
        "## 社区A1 ✅\nCOMMUNITY_A1_RECIPE\n"
        "## 质感1 ✅\nTEXTURE_1_RECIPE\n",
        "game-character-tags.md": "# Game tags\nGenshin character spellings.\n",
    }
    files = []
    for name, text in paths.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        files.append({"path": name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    manifest = {"repository": SKILL_REPOSITORY, "version": SKILL_VERSION, "commit": SKILL_COMMIT, "skill_count": 1, "files": files}
    (tmp_path / "SOURCE.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def cfg(bundle):
    return {
        "api": {"provider": "novelai", "model": "nai-diffusion-5-full", "base_url": "https://image.novelai.net", "api_key": "test"},
        "generation": {"size": "832x1216", "steps": 28},
        "prompt": {"skill_directory": str(bundle), "rewrite_model": "deepseek-v4-flash-fast"},
        "profile_meta": {"prompt_style": "novelai_v5"},
    }


def answer(**kwargs):
    return {"decision": "generate", "prompt": "a cat at a window", "character_prompts": [], "contains_text": False, **kwargs}


def llm(data):
    return AsyncMock(return_value={"success": True, "response": json.dumps(data)})


def test_skill_loader_selects_actual_relevant_source_and_checks_hash(bundle):
    text, modules = load_skill_guidance(str(bundle), "一只猫")
    assert "NOVELAI ASSISTANT CORE GUIDE" in text and "CORRECTION 6" in text
    assert "api.novelai.me" not in text and "Unrelated author preference" not in text
    assert "CORRECTION 10" not in text and "provider setup" not in text
    assert modules == ("novelai-assistant", "v5-field-corrections")
    text, modules = load_skill_guidance(str(bundle), "画四格漫画")
    assert "manga-page-playbook" in modules and "Use panels" in text
    assert "Unrelated workflow" not in text
    assert text.index("SOURCE: SKILL.md") < text.index("SOURCE: v5-field-corrections.md") < text.index("SOURCE: manga-page-playbook.md")
    (bundle / "SKILL.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="校验失败"):
        load_skill_guidance(str(bundle), "一只猫")


def test_named_artist_recipe_is_loaded_without_mixing_other_styles(bundle):
    text, modules = load_skill_guidance(str(bundle), "清新日常，画一只猫")
    assert "v5-confirmed-artist-strings" in modules
    assert "0.9::artist:shiromanta::" in text and "1.3::chen bin::" not in text


def test_numbered_recipes_do_not_match_shorter_names_or_require_generic_style_words(bundle):
    text, modules = load_skill_guidance(str(bundle), "用社区A1画猫")
    assert "v5-confirmed-artist-strings" in modules
    assert "COMMUNITY_A1_RECIPE" in text and "COMMUNITY_A_RECIPE" not in text
    text, modules = load_skill_guidance(str(bundle), "用质感1画猫")
    assert "v5-confirmed-artist-strings" in modules and "TEXTURE_1_RECIPE" in text


def test_unrelated_context_does_not_select_extra_sources(bundle):
    scene = "之前聊到画师、原神和四格漫画"
    _, modules = load_skill_guidance(str(bundle), "画一只猫", scene)
    assert modules == ("novelai-assistant", "v5-field-corrections")
    text, modules = load_skill_guidance(str(bundle), "继续刚才讨论的画面", scene)
    assert {"v5-confirmed-artist-strings", "game-character-tags", "manga-page-playbook"} <= set(modules)
    assert "Genshin character spellings" in text


def test_old_repository_manifest_stops_instead_of_silently_loading(bundle):
    path = bundle / "SOURCE.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(repository="https://github.com/Miint-Sunny/nai5-prompting", skill_count=11)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="版本不符"):
        load_skill_guidance(str(bundle), "a cat")


def test_missing_bundle_is_not_silently_replaced_with_old_prompt(tmp_path):
    with pytest.raises(ValueError, match="来源清单"):
        load_skill_guidance(str(tmp_path), "a cat")


@pytest.mark.asyncio
async def test_skill_source_is_sent_to_deepseek_in_system_role(bundle):
    call = llm(answer())
    plan = await rewrite_image_prompt("User: draw a cat", cfg(bundle), call, request_text="draw a cat")
    assert isinstance(plan, NovelAIPrompt) and plan.skill_version == SKILL_VERSION
    messages = call.await_args.kwargs["prompt"]
    assert messages[0]["role"] == "system" and "NOVELAI ASSISTANT CORE GUIDE" in messages[0]["content"]
    assert messages[1]["role"] == "user" and "User: draw a cat" in messages[1]["content"]
    assert "User: draw a cat" not in messages[0]["content"]
    assert "api.novelai.me" not in messages[0]["content"]


@pytest.mark.asyncio
async def test_comic_count_is_not_overwritten_from_reusable_character_captions(bundle):
    plan = await rewrite_image_prompt(
        "two panels", cfg(bundle), llm(answer(
            prompt="2girls, comic, two panels", character_prompts=[{"prompt": "girl, red coat"}],
        )), request_text="画两格漫画",
    )
    assert plan.prompt.startswith("2girls,") and "manga-page-playbook" in plan.skill_modules


@pytest.mark.asyncio
async def test_v5_characters_count_and_native_fields(bundle):
    data = answer(prompt="2girls, 1boy, 2girls, 1boy, solo, forest, Character 1: boy, source#hug, Character 2: girl, target#hug",
                  character_prompts=[{"prompt": "boy, black hair, blue jacket"}, {"prompt": "girl, brown hair, green coat"}])
    plan = await rewrite_image_prompt("two travelers", cfg(bundle), llm(data))
    assert plan.prompt.startswith("1boy, 1girl,")
    assert "2girls" not in plan.prompt and "solo" not in plan.prompt
    assert "\nCharacter 1:" in plan.prompt
    body = NovelAIImageClient(cfg(bundle)).build_payload(plan)
    p = body["parameters"]
    assert body["model"] == "nai-diffusion-5-full" and p["params_version"] == 4
    assert "qualityToggle" not in p and "ucPreset" not in p
    assert p["qualityPresetId"] == "standard" and p["noise_schedule"] == "karras"
    assert len(p["v4_prompt"]["caption"]["char_captions"]) == 2
    assert len(p["v4_negative_prompt"]["caption"]["char_captions"]) == 2
    assert p["v4_prompt"]["use_coords"] is False
    assert body["input"].count("masterpiece") == 1


@pytest.mark.asyncio
async def test_on_image_text_disables_no_text_and_quality_suffix(bundle):
    data = answer(prompt='a wooden sign, no text, Text: "OPEN"', contains_text=True)
    plan = await rewrite_image_prompt("write OPEN on the sign", cfg(bundle), llm(data))
    body = NovelAIImageClient(cfg(bundle)).build_payload(plan)
    assert body["parameters"]["qualityPresetId"] == "none"
    assert '"OPEN"' in body["input"] and "no text" not in body["input"]
    assert "masterpiece" not in body["input"]


@pytest.mark.parametrize("center", [{"x": -0.1, "y": 0.5}, {"x": True, "y": 0.5}, {"x": 0.2}, {"x": float("nan"), "y": 0.5}])
@pytest.mark.asyncio
async def test_invalid_character_coordinates_stop_before_image_request(bundle, center):
    data = answer(character_prompts=[{"prompt": "boy, blue shirt", "center": center}])
    with pytest.raises(ValueError, match="位置无效"):
        await rewrite_image_prompt("a person", cfg(bundle), llm(data))


def test_explicit_character_coordinates_are_preserved(bundle):
    plan = NovelAIPrompt("1girl", (NovelAICharacterPrompt("girl, red coat", center=(0.13, 0.83)),))
    p = NovelAIImageClient(cfg(bundle)).build_payload(plan)["parameters"]
    assert p["v4_prompt"]["use_coords"] is True
    assert p["v4_prompt"]["caption"]["char_captions"][0]["centers"] == [{"x": 0.13, "y": 0.83}]


@pytest.mark.asyncio
async def test_refusal_and_invalid_character_fields_do_not_fall_back(bundle):
    for data in [{"decision": "refuse", "prompt": ""}, answer(character_prompts="wrong")]:
        call = llm(data)
        with pytest.raises(ValueError):
            await rewrite_image_prompt("a scene", cfg(bundle), call)
        # V5 同样只追加一次强化重试，第二次仍不合格才停止。
        assert call.await_count == 2


@pytest.mark.asyncio
async def test_v5_refusal_is_retried_with_strengthened_contract(bundle):
    refused = {"decision": "refuse", "prompt": ""}
    call = AsyncMock(side_effect=[
        {"success": True, "response": json.dumps(refused)},
        {"success": True, "response": json.dumps(answer(prompt="1girl, nude, rating:explicit"))},
    ])
    plan = await rewrite_image_prompt("a nude girl", cfg(bundle), call)
    assert plan.prompt == "1girl, nude, rating:explicit"
    second = call.await_args_list[1].kwargs["prompt"]
    assert "RETRY" in second[0]["content"]
