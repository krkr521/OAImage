import base64
from typing import Any, Dict, List

import aiohttp
from oa_image_config import _form_value, _get_nested, _merge_passthrough_params, _parse_size
from oa_image_sources import _clean_base64, _download_as_base64, _looks_like_url, _safe_image_filename


async def _extract_image_from_json(session: aiohttp.ClientSession, payload: Any, timeout: int) -> str:
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
                        return await _download_as_base64(session, value, timeout)
                    return _clean_base64(value)
            url = item.get("url")
            if isinstance(url, str) and url:
                return await _download_as_base64(session, url, timeout)
        elif isinstance(item, str) and item:
            if _looks_like_url(item):
                return await _download_as_base64(session, item, timeout)
            return _clean_base64(item)
    return ""


class OpenAIImageClient:
    def __init__(self, config: dict):
        self.config = config
        self.base_url = str(_get_nested(config, "api.base_url", "")).rstrip("/")
        self.api_key = str(_get_nested(config, "api.api_key", ""))
        self.model = str(_get_nested(config, "api.model", "gpt-image-1"))
        self.size = str(_get_nested(config, "generation.size", "1024x1024"))
        self.timeout = int(_get_nested(config, "api.timeout_seconds", 120))

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def generate_text_to_image(self, prompt: str) -> str:
        path = str(_get_nested(self.config, "api.generations_path", "/v1/images/generations"))
        payload = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
            "n": 1,
            "response_format": str(_get_nested(self.config, "generation.response_format", "b64_json")),
        }
        payload = _merge_passthrough_params(self.config, payload, "text_to_image")
        return await self._post_json_for_image(self._url(path), payload)

    async def generate_image_to_image(
        self,
        prompt: str,
        image_base64: str,
        image_mime: str = "image/png",
        image_filename: str = "reference.png",
    ) -> str:
        mode = str(_get_nested(self.config, "generation.image_to_image_mode", "multipart_edit"))
        if mode == "json_generation":
            path = str(_get_nested(self.config, "api.generations_path", "/v1/images/generations"))
            payload = self._json_image_payload(prompt, image_base64)
            return await self._post_json_for_image(self._url(path), payload)
        if mode == "json_edit":
            path = str(_get_nested(self.config, "api.edits_path", "/v1/images/edits"))
            payload = self._json_image_payload(prompt, image_base64)
            return await self._post_json_for_image(self._url(path), payload)
        return await self._post_multipart_edit(prompt, image_base64, image_mime, image_filename)

    def _json_image_payload(self, prompt: str, image_base64: str) -> Dict[str, Any]:
        image_field = str(_get_nested(self.config, "generation.json_image_field", "image"))
        payload = {
            "model": self.model,
            "prompt": prompt,
            "size": self.size,
            "n": 1,
            "response_format": str(_get_nested(self.config, "generation.response_format", "b64_json")),
        }
        payload[image_field] = image_base64
        return _merge_passthrough_params(self.config, payload, "image_to_image")

    async def _post_json_for_image(self, url: str, payload: Dict[str, Any]) -> str:
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            async with session.post(url, json=payload, timeout=self.timeout) as response:
                if response.headers.get("content-type", "").startswith("image/"):
                    return base64.b64encode(await response.read()).decode("ascii")
                response.raise_for_status()
                data = await response.json(content_type=None)
                return await _extract_image_from_json(session, data, self.timeout)

    async def _post_multipart_edit(
        self,
        prompt: str,
        image_base64: str,
        image_mime: str = "image/png",
        image_filename: str = "reference.png",
    ) -> str:
        path = str(_get_nested(self.config, "api.edits_path", "/v1/images/edits"))
        image_bytes = base64.b64decode(_clean_base64(image_base64))
        form = aiohttp.FormData()
        form.add_field("model", self.model)
        form.add_field("prompt", prompt)
        form.add_field("size", self.size)
        form.add_field("n", "1")
        form.add_field(
            "response_format",
            str(_get_nested(self.config, "generation.response_format", "b64_json")),
        )
        form.add_field(
            "image",
            image_bytes,
            filename=_safe_image_filename(image_filename),
            content_type=image_mime if image_mime.startswith("image/") else "image/png",
        )
        extra_payload = _merge_passthrough_params(
            self.config,
            {
                "model": self.model,
                "prompt": prompt,
                "size": self.size,
                "n": "1",
                "response_format": str(_get_nested(self.config, "generation.response_format", "b64_json")),
                "image": image_base64,
            },
            "image_to_image",
        )
        for key, value in extra_payload.items():
            if key in {"model", "prompt", "size", "n", "response_format", "image"}:
                continue
            form.add_field(key, _form_value(value))
        async with aiohttp.ClientSession(headers=self._headers()) as session:
            async with session.post(self._url(path), data=form, timeout=self.timeout) as response:
                if response.headers.get("content-type", "").startswith("image/"):
                    return base64.b64encode(await response.read()).decode("ascii")
                response.raise_for_status()
                data = await response.json(content_type=None)
                return await _extract_image_from_json(session, data, self.timeout)


class JsonImageClient:
    def __init__(self, config: dict):
        self.config = config
        self.base_url = str(_get_nested(config, "api.base_url", "")).rstrip("/")
        self.api_key = str(_get_nested(config, "api.api_key", ""))
        self.path = str(_get_nested(config, "api.generations_path", "/api/v1/generate_image"))
        self.timeout = int(_get_nested(config, "api.timeout_seconds", 180))

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
                    return base64.b64encode(await response.read()).decode("ascii")
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"json_api HTTP {response.status}: {text[:300]}")
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = text
                return await _extract_image_from_json(session, data, self.timeout)
