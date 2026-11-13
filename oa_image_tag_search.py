"""让独立 DeepSeek 先选查询词，再读取 NovelAI 官方标签补全数据。"""

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import asyncio
import json
import logging
import math

import aiohttp

from oa_image_config import _get_nested, _prompt_model_route
from oa_image_runtime import get_provider_semaphore

import oa_image_novelai_prompt as templates

logger = logging.getLogger("OAImage")


class NovelAITagSearch:
    def __init__(self, config: dict[str, Any]):
        self.base_url = str(_get_nested(config, "api.base_url", "https://image.novelai.net")).rstrip("/")
        self.api_key = str(_get_nested(config, "api.api_key", "")).strip()
        self.model = str(_get_nested(config, "api.model", "nai-diffusion-5-full"))
        self.timeout = max(3, min(30, int(_get_nested(config, "prompt.tag_search_timeout_seconds", 15))))
        self.limit = max(1, min(10, int(_get_nested(config, "prompt.tag_search_result_limit", 8))))
        parsed = urlsplit(self.base_url)
        official = parsed.scheme == "https" and parsed.netloc == "image.novelai.net"
        local_test = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        if not official and not local_test:
            raise ValueError("NovelAI 官方标签查询必须使用 image.novelai.net")
        if not self.api_key or self.api_key in {"sk-...", "pst-..."}:
            raise ValueError("尚未配置 NovelAI Token，无法查询官方标签库")

    async def _query(self, session: aiohttp.ClientSession, query: str) -> dict[str, Any]:
        for attempt in range(2):
            try:
                return await self._query_once(session, query)
            except ValueError as exc:
                cause = exc.__cause__
                if isinstance(cause, aiohttp.ClientConnectorError):
                    logger.warning("NovelAI tag pre-request connection error: type=%s attempt=%d", type(cause).__name__, attempt + 1)
                    if attempt == 0 and not isinstance(cause, (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError)):
                        await asyncio.sleep(0.5)
                        continue
                raise

    async def _query_once(self, session: aiohttp.ClientSession, query: str) -> dict[str, Any]:
        async with get_provider_semaphore("novelai_tags", 2):
            try:
                async with session.get(
                    self.base_url + "/ai/generate-image/suggest-tags",
                    params={"model": self.model, "prompt": query, "lang": "en"},
                    headers={"Authorization": "Bearer " + self.api_key, "Accept": "application/json"},
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise ValueError(f"NovelAI 官方标签查询失败（HTTP {response.status}）")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        body.extend(chunk)
                        if len(body) > 1_048_576:
                            raise ValueError("NovelAI 官方标签响应过大")
                # 官方接口也可能以 text/plain 返回 JSON。
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(payload.get("tags"), list):
                    raise ValueError("NovelAI 官方标签响应结构无效")
                tags = []
                for item in payload["tags"][:self.limit]:
                    if not isinstance(item, dict) or not isinstance(item.get("tag"), str) or not item["tag"].strip():
                        raise ValueError("NovelAI 官方标签条目无效")
                    row: dict[str, Any] = {"tag": item["tag"][:200]}
                    count = item.get("count")
                    confidence = item.get("confidence")
                    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                        row["count"] = count
                    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and math.isfinite(confidence):
                        row["confidence"] = confidence
                    tags.append(row)
                return {"query": query, "status": "ok" if tags else "no_matches", "tags": tags}
            except asyncio.TimeoutError as exc:
                raise ValueError("NovelAI 官方标签查询超时，未启动图片生成") from exc
            except aiohttp.ClientError as exc:
                raise ValueError("NovelAI 官方标签网络查询失败，未启动图片生成") from exc
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ValueError("NovelAI 官方标签响应不是有效 JSON") from exc

    async def search(self, queries: list[str]) -> list[dict[str, Any]]:
        async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
            return await asyncio.gather(*(self._query(session, query) for query in queries))


async def select_and_search_tags(
    scene: str, config: dict[str, Any], generate: Callable[..., Awaitable[Any]],
) -> list[dict[str, Any]]:
    """模型选择相关关键词；接口错误或模型无效输出会停止，不伪造已搜索结果。"""
    route = _prompt_model_route(config)
    timeout = max(5, min(180, int(_get_nested(config, "prompt.rewrite_timeout_seconds", 60))))
    request = [
        {"role": "system", "content": templates.INSTRUCTIONS + "\n" + templates.TAG_QUERY_FORMAT},
        {"role": "user", "content": json.dumps({"scene": scene}, ensure_ascii=False)},
    ]
    try:
        result = await asyncio.wait_for(generate(
            prompt=request, **route,
            temperature=0.15, max_tokens=512, timeout_ms=timeout * 1000,
        ), timeout=timeout + 2)
    except asyncio.TimeoutError as exc:
        raise ValueError("标签查询规划超时") from exc
    except Exception as exc:
        raise ValueError("标签查询规划调用失败") from exc
    if isinstance(result, dict) and result.get("success") is True and isinstance(result.get("result"), dict):
        result = result["result"]
    if not isinstance(result, dict) or result.get("success") is not True:
        raise ValueError("标签查询规划失败")
    try:
        data = json.loads(result.get("response", ""))
    except (ValueError, TypeError) as exc:
        raise ValueError("整理模型未返回有效的标签查询结构") from exc
    if not isinstance(data, dict):
        raise ValueError("标签查询结构无效")
    if data.get("decision") == "refuse":
        # 标签查询只是提示词增强：模型拒绝时按“无匹配”继续，不阻断出图。
        logger.info("标签查询规划被模型拒绝，本次跳过官方标签补全")
        return []
    queries = data.get("queries")
    if data.get("decision") != "search" or not isinstance(queries, list) or not 1 <= len(queries) <= 3:
        raise ValueError("标签查询须提供 1 到 3 个关键词")
    if any(not isinstance(query, str) or not query.strip() or len(query) > 96 or not query.isprintable() for query in queries):
        raise ValueError("标签查询关键词无效")
    queries = list(dict.fromkeys(query.strip() for query in queries))
    return await NovelAITagSearch(config).search(queries)
