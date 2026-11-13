"""验证上下文压缩后的精确原图读取，以及具名角色直接使用私库原图。"""

from copy import deepcopy
from unittest.mock import AsyncMock

import base64
import hashlib
import json
import time

from maibot_sdk import PluginContext
import pytest

import oa_image_reference as references
import oa_image_runtime as runtime
from test_reference_feedback import B64, image_message, instance
from test_novelai import PNG


@pytest.fixture(autouse=True)
def clean_runtime():
    runtime.STREAM_IMAGES.clear()
    runtime.STREAM_COMMAND_JOBS.clear()
    runtime.STREAM_LOCKS.clear()
    yield


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_stream", ["group-A", "group-B"])
async def test_persisted_reference_uses_the_sdk_unwrapped_message(stored_stream):
    message_id = "2021122644"
    message = image_message(stream=stored_stream, message_id=message_id, binary=True)
    rpc = AsyncMock(return_value={"success": True, "message": message})
    ctx = PluginContext("a-dawn.oaimage", rpc_call=rpc)

    if stored_stream == "group-A":
        assert await references.resolve_reference(ctx, "group-A", message_id=message_id) == "data:image/png;base64," + B64
    else:
        with pytest.raises(references.ReferenceImageError, match="当前会话不匹配"):
            await references.resolve_reference(ctx, "group-A", message_id=message_id)
    rpc.assert_awaited_once_with("cap.call", "a-dawn.oaimage", {
        "capability": "message.get_by_id",
        "args": {"message_id": message_id, "chat_id": "group-A", "include_binary_data": True},
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id,index", [("-1489143249", 0), ("1864376348", 1)])
async def test_raw_qq_id_reads_exact_persisted_image_after_context_compaction(monkeypatch, message_id, index):
    plugin, client = instance(monkeypatch)
    actual = image_message(message_id=message_id, binary=True)
    actual["timestamp"] = str(time.time() - 7200)
    alternate = base64.b64encode(PNG + b"second-image").decode()
    actual["raw_message"].append({"type": "image", "binary_data_base64": alternate})
    runtime.cache_images("group-A", [alternate])

    async def capability(name, **kwargs):
        if name == "message.get_by_id":
            assert kwargs == {"message_id": message_id, "chat_id": "group-A", "include_binary_data": True}
            return {"success": True, "message": actual}
        if name == "maisaka.context.resolve_image":
            return {"success": False, "error": "没有找到消息：msg_id=" + message_id}
        return {"success": True, "queued": True}

    plugin.ctx.call_capability.side_effect = capability
    success, detail = await plugin._run_generation(
        stream_id="group-A", raw_prompt="按指定旧图画", mode="image_to_image",
        reference_message_id=message_id, reference_image_index=index, delivery="chat",
    )
    assert success, detail
    args = client.generate_image_to_image.await_args.args
    assert args[1] == "data:image/png;base64," + (B64 if index == 0 else alternate)
    calls = plugin.ctx.call_capability.await_args_list
    assert not any(call.args[0] in {"message.get_recent", "maisaka.context.resolve_image"} for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "foreign_chat", "other_id", "no_image", "missing_binary", "bad_index", "host_error"])
async def test_invalid_explicit_persisted_reference_never_uses_another_image(monkeypatch, failure):
    plugin, client = instance(monkeypatch)
    actual = image_message(message_id="-1489143249", binary=True)
    runtime.cache_images("group-A", [B64])
    if failure == "missing":
        actual = None
    elif failure == "foreign_chat":
        actual["session_id"] = "group-B"
    elif failure == "other_id":
        actual["message_id"] = "other"
    elif failure == "no_image":
        actual["raw_message"] = [{"type": "emoji", "binary_data_base64": B64}]
    elif failure == "missing_binary":
        actual["raw_message"][0].pop("binary_data_base64")

    async def capability(name, **kwargs):
        if name == "message.get_by_id":
            return {"success": False, "error": "database unavailable"} if failure == "host_error" else {"success": True, "message": actual}
        if name == "maisaka.context.resolve_image":
            return {"success": False, "error": "not in live context"}
        return {"success": True, "queued": True}

    plugin.ctx.call_capability.side_effect = capability
    success, _ = await plugin._run_generation(
        stream_id="group-A", raw_prompt="按指定旧图画", mode="image_to_image",
        reference_message_id="-1489143249", reference_image_index=1 if failure == "bad_index" else 0,
        delivery="chat",
    )
    assert not success
    client.generate_image_to_image.assert_not_awaited()
    plugin._build_context_prompt.assert_not_awaited()
    calls = plugin.ctx.call_capability.await_args_list
    assert sum(call.args[0] == "message.get_by_id" for call in calls) == 1
    assert not any(call.args[0] in {"message.get_recent", "maisaka.context.resolve_image"} for call in calls)


@pytest.mark.asyncio
async def test_named_character_uses_saved_reference_without_old_chat_message(monkeypatch, tmp_path):
    plugin, client = instance(monkeypatch)
    config = deepcopy(plugin._effective_config())
    project = tmp_path / "project"
    config["runtime"] = {"plugin_dir": str(project / "plugins/a-dawn_oaimage")}
    config["character_library"] = {"enabled": True, "auto_match": True, "max_characters": 8, "personas": {}}
    data = project / "data/plugins/github.QlzqQlzq.character-knowledge-plugin"
    reference = data / "reference_images/xiaoluobao/reference.png"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(PNG)
    (data / "characters.json").write_text(json.dumps({"characters": [{
        "id": "xiaoluobao", "name": "小洛包", "aliases": ["1000000004"],
        "reference_images": [{"file": reference.relative_to(data).as_posix(), "variant": "standard", "sha256": hashlib.sha256(PNG).hexdigest()}],
    }]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(plugin, "_effective_config", lambda: config)

    success, detail = await plugin._run_generation(
        stream_id="group-A", raw_prompt="画小洛包吃白饭", characters=["小洛包"], delivery="chat",
    )
    assert success, detail
    args = client.generate_image_to_image.await_args.args
    assert base64.b64decode(args[1]) == PNG
    assert "小洛包" in args[0]
    calls = plugin.ctx.call_capability.await_args_list
    assert not any(call.args[0] in {"message.get_recent", "message.get_by_id", "maisaka.context.resolve_image"} for call in calls)
