"""验证仅建立连接前重连，已提交请求及证书错误不重发；不调用外部服务。"""

from types import SimpleNamespace

import asyncio
import base64
import ssl

import aiohttp
import pytest
from aiohttp import web

from oa_image_novelai import NovelAIImageClient
from oa_image_tag_search import NovelAITagSearch
from test_novelai import PNG, config, resolved, server


class FailingRequest:
    def __init__(self, error):
        self.error = error

    async def __aenter__(self):
        raise self.error

    async def __aexit__(self, *args):
        return False


def connection_error():
    key = SimpleNamespace(host="127.0.0.1", port=1, ssl=True, is_ssl=True)
    return aiohttp.ClientConnectorError(key, ConnectionResetError(104, "private-detail test-secret"))


@pytest.mark.asyncio
async def test_preconnect_reset_reconnects_once_then_submits_exactly_one_generation(monkeypatch):
    entered, received, seeds = [], [], []
    original = aiohttp.ClientSession.post

    def post(session, *args, **kwargs):
        entered.append(session)
        seeds.append(kwargs["json"]["parameters"]["seed"])
        if len(entered) == 1:
            return FailingRequest(connection_error())
        return original(session, *args, **kwargs)

    async def handler(request):
        received.append(await request.json())
        return web.json_response({"images": [{"image": base64.b64encode(PNG).decode()}]})

    monkeypatch.setattr(aiohttp.ClientSession, "post", post)
    async with server(handler) as url:
        cfg = resolved()
        cfg["api"]["base_url"] = url
        assert await NovelAIImageClient(cfg).generate_text_to_image("cat")
    assert len(entered) == 2 and entered[0] is not entered[1]
    assert len(received) == 1 and seeds[0] == seeds[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,attempts,message", [
    ("reset", 2, "连接被重置"), ("disconnect", 1, "服务端中断"),
    ("certificate", 1, "证书校验失败"), ("timeout", 1, "超时"),
])
async def test_failures_are_bounded_and_preserve_specific_safe_error(monkeypatch, kind, attempts, message):
    entered = []

    def post(session, *args, **kwargs):
        entered.append(True)
        if kind == "reset":
            error = connection_error()
        elif kind == "disconnect":
            error = aiohttp.ServerDisconnectedError("private-detail test-secret")
        elif kind == "certificate":
            error = aiohttp.ClientConnectorCertificateError(
                SimpleNamespace(host="127.0.0.1", port=1, ssl=True, is_ssl=True),
                ssl.CertificateError("private-detail test-secret"),
            )
        else:
            error = asyncio.TimeoutError("private-detail test-secret")
        return FailingRequest(error)

    monkeypatch.setattr(aiohttp.ClientSession, "post", post)
    with pytest.raises(ValueError, match=message) as error:
        await NovelAIImageClient(resolved()).generate_text_to_image("cat")
    assert len(entered) == attempts
    assert "test-secret" not in str(error.value) and "private-detail" not in str(error.value)


@pytest.mark.asyncio
async def test_incomplete_image_response_does_not_repeat_billable_request():
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        response = web.StreamResponse(status=200, headers={"Content-Length": "99999"})
        await response.prepare(request)
        await response.write(b"partial-image")
        request.transport.close()
        return response

    async with server(handler) as url:
        cfg = resolved()
        cfg["api"]["base_url"] = url
        with pytest.raises(ValueError, match="传输不完整"):
            await NovelAIImageClient(cfg).generate_text_to_image("cat")
    assert count == 1


@pytest.mark.asyncio
async def test_post_redirect_is_not_followed_or_repeated():
    async def handler(request):
        return web.Response(status=302, headers={"Location": "/another-generation"})

    async with server(handler) as url:
        cfg = resolved()
        cfg["api"]["base_url"] = url
        with pytest.raises(ValueError, match="HTTP 302"):
            await NovelAIImageClient(cfg).generate_text_to_image("cat")


@pytest.mark.asyncio
async def test_tag_lookup_reconnects_once_before_http_get(monkeypatch):
    original = aiohttp.ClientSession.get
    count = 0

    def get(session, *args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return FailingRequest(connection_error())
        return original(session, *args, **kwargs)

    async def handler(request):
        return web.json_response({"tags": [{"tag": "cat", "count": 10}]})

    app = web.Application()
    app.router.add_get("/ai/generate-image/suggest-tags", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)
    try:
        cfg = config()
        from oa_image_config import _resolve_profile_config
        cfg = _resolve_profile_config(cfg, "novelai")[0]
        cfg["api"]["base_url"] = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        results = await NovelAITagSearch(cfg).search(["cat"])
        assert results[0]["tags"][0]["tag"] == "cat" and count == 2
    finally:
        await runner.cleanup()
