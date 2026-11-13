import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Dict, List

import aiohttp
from oa_image_animation import prepare_gif_payload
from oa_image_config import _form_value, _get_nested, _merge_passthrough_params, _parse_size
from oa_image_sources import (
    _clean_base64,
    _download_as_base64,
    _looks_like_url,
    _read_limited_image_response,
    _safe_image_filename,
)

logger = logging.getLogger("OAImage")


async def _extract_image_from_json(
    session: aiohttp.ClientSession,
    payload: Any,
    timeout: int,
    max_bytes: int,
    allow_non_image_content_type: bool,
) -> str:
    candidates: List[Any] = []
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            candidates.extend(data)
        elif isinstance(data, (dict, str)):
            candidates.append(data)
        for key in ("result", "output", "image", "images"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, (dict, str)):
                candidates.append(value)
        candidates.append(payload)
    elif isinstance(payload, list):
        candidates.extend(payload)
    for item in candidates:
        if isinstance(item, dict):
            for key in (
                "b64_json",
                "base64",
                "image",
                "image_base64",
                "image_url",
                "output_url",
                "result_url",
                "file_url",
            ):
                value = item.get(key)
                if isinstance(value, str) and value:
                    if _looks_like_url(value):
                        return await _download_as_base64(
                            session,
                            value,
                            timeout,
                            max_bytes,
                            allow_non_image_content_type,
                        )
                    return _clean_base64(value)
            url = item.get("url")
            if isinstance(url, str) and url:
                return await _download_as_base64(session, url, timeout, max_bytes, allow_non_image_content_type)
        elif isinstance(item, str) and item:
            if _looks_like_url(item):
                return await _download_as_base64(session, item, timeout, max_bytes, allow_non_image_content_type)
            return _clean_base64(item)
    return ""


class OpenAIImageClient:
    def __init__(self, config: dict, usage_callback: Callable[..., Awaitable[Any]] | None = None):
        self.config = config
        self.usage_callback = usage_callback
        self.base_url = str(_get_nested(config, "api.base_url", "")).rstrip("/")
        self.api_key = str(_get_nested(config, "api.api_key", ""))
        self.model = str(_get_nested(config, "api.model", "gpt-image-1"))
        self.size = str(_get_nested(config, "generation.size", "1024x1024"))
        self.timeout = int(_get_nested(config, "api.timeout_seconds", 300))
        self.url_image_max_bytes = int(
            _get_nested(
                config,
                "behavior.url_image_max_bytes",
                _get_nested(config, "behavior.local_image_max_bytes", 10 * 1024 * 1024),
            )
        )
        self.allow_non_image_url_content_type = bool(
            _get_nested(config, "behavior.allow_non_image_url_content_type", False)
        )

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _supports_response_format(self) -> bool:
        return not self.model.startswith("gpt-image-")

    def _add_response_format(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._supports_response_format():
            payload["response_format"] = str(_get_nested(self.config, "generation.response_format", "b64_json"))
        return payload

    def _drop_unsupported_params(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._supports_response_format():
            return payload
        payload.pop("response_format", None)
        return payload

    async def generate_text_to_image(self, prompt: str) -> str:
        path = str(_get_nested(self.config, "api.generations_path", "/v1/images/generations"))
        payload = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
            "n": 1,
        }
        payload = self._add_response_format(payload)
        payload = _merge_passthrough_params(self.config, payload, "text_to_image")
        payload = self._drop_unsupported_params(payload)
        payload = prepare_gif_payload(payload)
        return await self._post_json_for_image(self._url(path), payload, reference_images=0)

    async def generate_image_to_image(
        self,
        prompt: str,
        image_base64: str,
        image_mime: str = "image/png",
        image_filename: str = "reference.png",
        additional_images: list[tuple[str, str, str]] | None = None,
    ) -> str:
        params = _merge_passthrough_params(self.config, {}, "image_to_image")
        mode = str(_get_nested(self.config, "generation.image_to_image_mode", "multipart_edit"))
        if str(params.get("output_format", "")).casefold() == "gif":
            if mode != "multipart_edit":
                raise ValueError("参考图转 GIF 需要使用 multipart_edit 套餐")
            reference_types = {"image/png", "image/jpeg", "image/webp"}
            images = [(image_base64, image_mime, image_filename), *(additional_images or [])]
            if any(mime not in reference_types for _, mime, _ in images):
                raise ValueError("参考图转 GIF 请使用 PNG、JPEG 或 WebP 静态图")
        if additional_images and mode != "multipart_edit":
            raise ValueError("multiple reference images require generation.image_to_image_mode=multipart_edit")
        if mode == "json_generation":
            path = str(_get_nested(self.config, "api.generations_path", "/v1/images/generations"))
            payload = self._json_image_payload(prompt, image_base64)
            return await self._post_json_for_image(self._url(path), payload, reference_images=1)
        if mode == "json_edit":
            path = str(_get_nested(self.config, "api.edits_path", "/v1/images/edits"))
            payload = self._json_image_payload(prompt, image_base64)
            return await self._post_json_for_image(self._url(path), payload, reference_images=1)
        return await self._post_multipart_edit(prompt, image_base64, image_mime, image_filename, additional_images)

    def _json_image_payload(self, prompt: str, image_base64: str) -> Dict[str, Any]:
        image_field = str(_get_nested(self.config, "generation.json_image_field", "image"))
        payload = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
            "n": 1,
        }
        payload = self._add_response_format(payload)
        payload[image_field] = image_base64
        payload = _merge_passthrough_params(self.config, payload, "image_to_image")
        return prepare_gif_payload(self._drop_unsupported_params(payload))

    async def _report_usage(self, data: Any, reference_images: int) -> None:
        """成功生成即记账；下载和后续发送失败都不应丢失供应商费用。"""
        if self.usage_callback is None:
            return
        try:
            await self.usage_callback(
                provider="openai",
                model=self.model,
                request_type="plugin_image_generation",
                usage=data.get("usage") if isinstance(data, dict) else None,
                units={"reference_images": reference_images, "input_kind": "text" if not reference_images else "mixed"},
            )
        except Exception:
            logger.exception("OpenAI 生图用量上报失败，继续交付已生成图片")

    async def _post_json_for_image(self, url: str, payload: Dict[str, Any], *, reference_images: int = 0) -> str:
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            async with session.post(url, json=payload, timeout=self.timeout) as response:
                if response.headers.get("content-type", "").startswith("image/"):
                    response.raise_for_status()
                    await self._report_usage(None, reference_images)
                    image_base64, _ = await _read_limited_image_response(
                        response,
                        self.url_image_max_bytes,
                        self.allow_non_image_url_content_type,
                    )
                    return image_base64
                if response.status >= 400:
                    body = await response.text()
                    logger.error(f"OpenAI 图片接口错误: status={response.status} url={url} body={body[:2000]}")
                    raise ValueError(f"OpenAI image API error {response.status}: {body[:1000]}")
                response.raise_for_status()
                data = await response.json(content_type=None)
                await self._report_usage(data, reference_images)
                return await _extract_image_from_json(
                    session,
                    data,
                    self.timeout,
                    self.url_image_max_bytes,
                    self.allow_non_image_url_content_type,
                )

    async def _post_multipart_edit(
        self,
        prompt: str,
        image_base64: str,
        image_mime: str = "image/png",
        image_filename: str = "reference.png",
        additional_images: list[tuple[str, str, str]] | None = None,
    ) -> str:
        path = str(_get_nested(self.config, "api.edits_path", "/v1/images/edits"))
        images = [(image_base64, image_mime, image_filename), *(additional_images or [])]
        if len(images) > 16:
            raise ValueError("image edits accepts at most 16 reference images")
        form = aiohttp.FormData()
        form.add_field("model", self.model)
        form.add_field("prompt", prompt)
        form.add_field("size", self.size)
        form.add_field("n", "1")
        if self._supports_response_format():
            form.add_field(
                "response_format",
                str(_get_nested(self.config, "generation.response_format", "b64_json")),
            )
        for encoded, mime, filename in images:
            form.add_field(
                "image[]" if len(images) > 1 else "image",
                base64.b64decode(_clean_base64(encoded)),
                filename=_safe_image_filename(filename),
                content_type=mime if mime.startswith("image/") else "image/png",
            )
        extra_payload = _merge_passthrough_params(
            self.config,
            {
                "model": self.model,
                "prompt": prompt,
                "size": self.size,
                "n": "1",
                "image": image_base64,
            },
            "image_to_image",
        )
        extra_payload = prepare_gif_payload(self._drop_unsupported_params(extra_payload))
        for key, value in extra_payload.items():
            if key in {"model", "prompt", "size", "n", "response_format", "image", "image[]"}:
                continue
            form.add_field(key, _form_value(value))
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            async with session.post(self._url(path), data=form, timeout=self.timeout) as response:
                if response.headers.get("content-type", "").startswith("image/"):
                    response.raise_for_status()
                    await self._report_usage(None, len(images))
                    image_base64, _ = await _read_limited_image_response(
                        response,
                        self.url_image_max_bytes,
                        self.allow_non_image_url_content_type,
                    )
                    return image_base64
                if response.status >= 400:
                    body = await response.text()
                    logger.error(f"OpenAI 图片编辑接口错误: status={response.status} url={self._url(path)} body={body[:2000]}")
                    raise ValueError(f"OpenAI image edit API error {response.status}: {body[:1000]}")
                response.raise_for_status()
                data = await response.json(content_type=None)
                await self._report_usage(data, len(images))
                return await _extract_image_from_json(
                    session,
                    data,
                    self.timeout,
                    self.url_image_max_bytes,
                    self.allow_non_image_url_content_type,
                )


class JsonImageClient:
    def __init__(self, config: dict):
        self.config = config
        self.base_url = str(_get_nested(config, "api.base_url", "")).rstrip("/")
        self.api_key = str(_get_nested(config, "api.api_key", ""))
        self.path = str(_get_nested(config, "api.generations_path", "/api/v1/generate_image"))
        self.timeout = int(_get_nested(config, "api.timeout_seconds", 180))
        self.url_image_max_bytes = int(
            _get_nested(
                config,
                "behavior.url_image_max_bytes",
                _get_nested(config, "behavior.local_image_max_bytes", 10 * 1024 * 1024),
            )
        )
        self.allow_non_image_url_content_type = bool(
            _get_nested(config, "behavior.allow_non_image_url_content_type", False)
        )

    def _url(self) -> str:
        if self.path.startswith("http://") or self.path.startswith("https://"):
            return self.path
        return f"{self.base_url}/{self.path.lstrip('/')}"

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _common_payload(self, prompt: str) -> Dict[str, Any]:
        default_width, default_height = _parse_size(str(_get_nested(self.config, "generation.size", "512x512")))
        payload = {
            "prompt": prompt,
            "negative_prompt": str(_get_nested(self.config, "generation.negative_prompt", "")),
            "width": int(_get_nested(self.config, "generation.width", default_width)),
            "height": int(_get_nested(self.config, "generation.height", default_height)),
            "steps": int(_get_nested(self.config, "generation.steps", 20)),
            "cfg": float(_get_nested(self.config, "generation.cfg", 7.0)),
            "model_index": int(_get_nested(self.config, "generation.model_index", 0)),
            "seed": int(_get_nested(self.config, "generation.seed", -1)),
        }
        return _merge_passthrough_params(self.config, payload, "text_to_image")

    async def generate_text_to_image(self, prompt: str) -> str:
        return await self._post_for_image(self._common_payload(prompt))

    async def generate_image_to_image(self, prompt: str, image_source: str) -> str:
        if not image_source:
            raise ValueError("json_api 图生图需要可访问的 image_source URL")
        payload = {
            "prompt": prompt,
            "image_source": image_source,
            "model_index": int(_get_nested(self.config, "generation.model_index", 0)),
        }
        payload = _merge_passthrough_params(self.config, payload, "image_to_image")
        return await self._post_for_image(payload)

    async def _post_for_image(self, payload: Dict[str, Any]) -> str:
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            async with session.post(self._url(), json=payload, timeout=self.timeout) as response:
                if response.headers.get("content-type", "").startswith("image/"):
                    image_base64, _ = await _read_limited_image_response(
                        response,
                        self.url_image_max_bytes,
                        self.allow_non_image_url_content_type,
                    )
                    return image_base64
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"json_api HTTP {response.status}: {text[:300]}")
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = text
                return await _extract_image_from_json(
                    session,
                    data,
                    self.timeout,
                    self.url_image_max_bytes,
                    self.allow_non_image_url_content_type,
                )
