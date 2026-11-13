"""NovelAI 原生图片协议；单次请求只生成一张普通插画。"""

from collections.abc import Awaitable, Callable
from typing import Any

import asyncio
import base64
import binascii
import io
import json
import logging
import re
import secrets
import zipfile

import aiohttp

from oa_image_config import _get_nested
from oa_image_prompt_types import NovelAIPrompt
from oa_image_runtime import get_provider_semaphore

logger = logging.getLogger("OAImage")

# 评分标签是 V5 里最强的全局尺度控制；客户端只补空缺，不再无条件压成 SFW。
_RATING_TAG_PATTERN = re.compile(r"rating:\s*(?:general|sensitive|questionable|explicit)", re.IGNORECASE)
_RATING_ALIASES = {"safe": "general", "sfw": "general", "nsfw": "explicit", "r18": "explicit", "r-18": "explicit"}
_RATING_VALUES = ("auto", "general", "sensitive", "questionable", "explicit")
# 这些标签只出现在成人画面里；模型漏写 rating 时据此补成 rating:explicit。
_EXPLICIT_TAG_PATTERN = re.compile(
    r"\b(?:nude|naked|topless|nipples?|pussy|penis|vaginal|anal|penetration|creampie|blowjob|fellatio|"
    r"paizuri|ahegao|tentacle sex|bukkake|facial|cum on|cum in|spread legs|masturbation|orgasm)\b",
    re.IGNORECASE,
)


def resolve_prompt_rating(prompt: str, config: dict[str, Any]) -> str:
    """返回本次请求最终生效的评分标签，用于判断结果能否进入内部上下文。

    与 _apply_rating 使用同一套判定：固定配置优先，其次模型写出的 rating 标签，
    再次是命中成人解剖标签时补的 explicit，最后按普通插画计为 general。
    """

    configured = str(_get_nested(config, "generation.rating", "auto")).strip().casefold()
    configured = _RATING_ALIASES.get(configured, configured)
    if configured in {"general", "sensitive", "questionable", "explicit"}:
        return configured
    text = str(prompt or "")
    match = _RATING_TAG_PATTERN.search(text)
    if match:
        return match.group(0).split(":", 1)[1].strip().casefold()
    if _EXPLICIT_TAG_PATTERN.search(text):
        return "explicit"
    return "general"


class NovelAIImageClient:
    """发送原生请求并读取 JSON 或 ZIP 图片响应，不自动重试计费请求。"""

    def __init__(self, config: dict[str, Any], usage_callback: Callable[..., Awaitable[Any]] | None = None):
        self.config = config
        self.usage_callback = usage_callback
        self.base_url = str(_get_nested(config, "api.base_url", "https://image.novelai.net")).rstrip("/")
        self.api_key = str(_get_nested(config, "api.api_key", "")).strip()
        self.model = str(_get_nested(config, "api.model", "nai-diffusion-5-full"))
        self.timeout = int(_get_nested(config, "api.timeout_seconds", 180))
        self.max_bytes = 20 * 1024 * 1024

    def validate_configuration(self) -> None:
        if not self.api_key or self.api_key in {"sk-...", "pst-..."}:
            raise ValueError("尚未配置 NovelAI Persistent API Token，请在生图套餐 novelai 的 api_key 中填写")
        if not self.base_url.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
            raise ValueError("NovelAI base_url 必须使用 HTTPS")

    def _apply_rating(self, prompt: str) -> tuple[str, str]:
        """返回去掉固定评分标签后的 base caption，以及要补到末尾的评分标签。"""
        configured = str(_get_nested(self.config, "generation.rating", "auto")).strip().casefold()
        configured = _RATING_ALIASES.get(configured, configured)
        if configured not in _RATING_VALUES:
            raise ValueError("NovelAI generation.rating 须为 auto、general、sensitive、questionable 或 explicit")
        if configured == "auto":
            # 模型自己写了 rating 就尊重它，绝不再补第二个评分标签。
            if _RATING_TAG_PATTERN.search(prompt):
                return prompt, ""
            # 已经写了实名成人标签却漏了 rating 时补成 explicit，避免 V5 按普通插画渲染。
            if _EXPLICIT_TAG_PATTERN.search(prompt):
                return prompt, "rating:explicit"
            return prompt, ""
        # 固定尺度时先撤掉已有评分标签，避免一句话里同时出现 rating:general 与 rating:explicit。
        stripped = ", ".join(tag.strip() for tag in _RATING_TAG_PATTERN.sub("", prompt).split(",") if tag.strip())
        return stripped, f"rating:{configured}"

    def build_payload(self, prompt: str | NovelAIPrompt) -> dict[str, Any]:
        prepared = prompt if isinstance(prompt, NovelAIPrompt) else NovelAIPrompt(prompt=str(prompt))
        is_v5 = self.model.startswith("nai-diffusion-5")
        size = re.fullmatch(r"(\d+)[xX](\d+)", str(_get_nested(self.config, "generation.size", "832x1216")))
        if size is None:
            raise ValueError("NovelAI 图片尺寸格式须为 宽x高")
        width, height = map(int, size.groups())
        if width < 64 or height < 64 or width % 64 or height % 64 or width * height > 1_048_576:
            raise ValueError("NovelAI 图片尺寸须为 64 的倍数，总像素不超过 1048576")
        steps = int(_get_nested(self.config, "generation.steps", 28))
        if not 1 <= steps <= 28:
            raise ValueError("NovelAI steps 须在 1 到 28 之间")
        seed = int(_get_nested(self.config, "generation.seed", -1))
        if seed == -1:
            seed = secrets.randbits(32)
        if not 0 <= seed <= 0xFFFFFFFF:
            raise ValueError("NovelAI seed 须为 -1 或 32 位无符号整数")
        positive, rating_suffix = self._apply_rating(prepared.prompt.strip())
        negative = str(_get_nested(self.config, "generation.negative_prompt", "lowres, bad_anatomy, blurry, watermark"))
        if _get_nested(self.config, "generation.ban_explicit_tags", False):
            # 只有显式要求过滤时才叠加；默认不往 UC 写 nsfw/nude，否则和成人画面互相抵消。
            negative = "nsfw, nude, explicit, " + negative
        if prepared.negative_prompt:
            negative = f"{negative}, {prepared.negative_prompt}"
        quality = prepared.quality_preset
        if is_v5:
            # 与官网一致：预设由客户端装配到实际提示词，不依赖元数据字段生效。
            quality = "none" if prepared.contains_text else quality
            if quality != "none":
                quality_tags = ["very aesthetic", "masterpiece" if quality == "standard" else "amazing quality", "no text"]
                existing = {tag.strip().casefold() for tag in positive.split(",")}
                added = [tag for tag in quality_tags if tag not in existing]
                if added:
                    positive += ", " + ", ".join(added)
            if prepared.uc_preset == "light":
                negative += ", lowres, bad hands, bad anatomy, artistic error, sepia, white haze, worst quality, very displeasing, jpeg artifacts, 0::ai-generated::"
            if prepared.contains_text:
                negative = ", ".join(tag.strip() for tag in negative.split(",") if tag.strip().casefold() not in {"text", "no text"})
        if rating_suffix:
            # 放在整句最末：V5 里靠前的 rating 权重更高，末尾标签才能盖住默认尺度。
            positive = f"{positive}, {rating_suffix}" if positive else rating_suffix
        parameters: dict[str, Any] = {
            "params_version": 4 if is_v5 else 3,
            "width": width,
            "height": height,
            "scale": float(_get_nested(self.config, "generation.scale", 5.0)),
            "sampler": str(_get_nested(self.config, "generation.sampler", "k_euler_ancestral")),
            "steps": steps,
            "n_samples": 1,
            "seed": seed,
            "negative_prompt": negative,
            "noise_schedule": str(_get_nested(self.config, "generation.noise_schedule", "karras")),
            "qualityToggle": True,
            "ucPreset": 0,
            "cfg_rescale": 0,
            "dynamic_thresholding": False,
            "legacy": False,
        }
        if is_v5:
            parameters.pop("qualityToggle", None)
            parameters.pop("ucPreset", None)
            parameters.update(
                qualityPresetId=quality, ucPresetId=prepared.uc_preset, noise_schedule="karras",
                add_original_image=True, legacy_uc=False, skip_cfg_above_sigma=None,
            )
        if self.model.startswith(("nai-diffusion-4", "nai-diffusion-5")):
            def captions(negative: bool = False) -> list[dict[str, Any]]:
                return [{
                    "char_caption": char.negative_prompt if negative else char.prompt,
                    "centers": [{"x": (char.center or (0.5, 0.5))[0], "y": (char.center or (0.5, 0.5))[1]}],
                } for char in prepared.character_prompts]
            parameters["v4_prompt"] = {
                "caption": {"base_caption": positive, "char_captions": captions()},
                "use_coords": any(char.center is not None for char in prepared.character_prompts),
                "use_order": True,
            }
            parameters["v4_negative_prompt"] = {
                "caption": {"base_caption": negative, "char_captions": captions(negative=True)},
                "legacy_uc": False,
            }
        return {"input": positive, "model": self.model, "action": "generate", "parameters": parameters}

    def _validate_image(self, data: bytes) -> str:
        if not data or len(data) > self.max_bytes:
            raise ValueError("NovelAI 返回图片为空或超出大小限制")
        if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff") or (
            data.startswith(b"RIFF") and data[8:12] == b"WEBP"
        )):
            raise ValueError("NovelAI 返回的内容不是支持的图片")
        return base64.b64encode(data).decode("ascii")

    def decode_response(self, body: bytes, content_type: str) -> str:
        if "zip" in content_type or body.startswith(b"PK\x03\x04"):
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    files = archive.infolist()
                    if len(files) > 16:
                        raise ValueError("NovelAI ZIP 响应文件过多")
                    for info in files:
                        if not info.is_dir() and info.filename.lower().endswith((".png", ".webp", ".jpg", ".jpeg")):
                            if info.file_size > self.max_bytes:
                                raise ValueError("NovelAI ZIP 图片超出大小限制")
                            return self._validate_image(archive.read(info))
            except zipfile.BadZipFile as exc:
                raise ValueError("NovelAI ZIP 响应损坏") from exc
            raise ValueError("NovelAI ZIP 响应没有图片")
        try:
            payload = json.loads(body)
            images = payload.get("images") if isinstance(payload, dict) else None
            if not isinstance(images, list) or not images or not isinstance(images[0], dict):
                raise ValueError("missing images")
            encoded = images[0].get("image")
            if not isinstance(encoded, str):
                raise ValueError("missing image")
            return self._validate_image(base64.b64decode(encoded, validate=True))
        except (ValueError, binascii.Error, TypeError) as exc:
            raise ValueError("NovelAI JSON 响应没有有效图片") from exc

    async def generate_text_to_image(self, prompt: str | NovelAIPrompt) -> str:
        self.validate_configuration()
        payload = self.build_payload(prompt)
        limit = max(1, min(3, int(_get_nested(self.config, "generation.max_concurrency", 1))))
        semaphore = get_provider_semaphore("novelai", limit)
        async with semaphore:
            # 仅 TCP/TLS 连接尚未建立的失败可以重连；HTTP 请求发出后不重试计费生成。
            for attempt in range(2):
                status, received = None, 0
                try:
                    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                        async with session.post(
                            self.base_url + "/ai/generate-image",
                            json=payload,
                            headers={"Authorization": "Bearer " + self.api_key, "Accept": "application/json"},
                            allow_redirects=False,
                        ) as response:
                            status = response.status
                            if status not in {200, 201}:
                                descriptions = {401: "Token 无效或已失效", 402: "余额或订阅不足", 403: "请求被拒绝", 429: "请求过于频繁，请稍后再试"}
                                raise ValueError(f"NovelAI HTTP {status}：{descriptions.get(status, '上游服务请求失败')}")
                            body = bytearray()
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                body.extend(chunk)
                                received = len(body)
                                if received > self.max_bytes * 2:
                                    raise ValueError("NovelAI 响应超出大小限制")
                            # ZIP 响应通常不带 Anlas，保留计费参数并交给账本标记待核算。
                            # 不发余额差分请求，避免并发调用被归到错误的一张图片。
                            if self.usage_callback is not None:
                                units = {
                                    key: payload["parameters"][key]
                                    for key in ("width", "height", "steps")
                                    if key in payload["parameters"]
                                }
                                if "json" in response.headers.get("Content-Type", ""):
                                    try:
                                        metadata = json.loads(body)
                                    except (ValueError, UnicodeDecodeError):
                                        metadata = {}
                                    if isinstance(metadata, dict):
                                        # A bare "anlas" may be an account balance, not this request's cost.
                                        for key in ("anlas_cost", "anlas_used", "anlas_spent"):
                                            if isinstance(metadata.get(key), (int, float)) and not isinstance(metadata[key], bool):
                                                units["anlas"] = metadata[key]
                                                break
                                try:
                                    await self.usage_callback(
                                        provider="novelai", model=self.model,
                                        request_type="plugin_image_generation", units=units,
                                    )
                                except Exception:
                                    logger.exception("NovelAI 生图用量上报失败，继续交付已生成图片")
                            return await asyncio.to_thread(self.decode_response, bytes(body), response.headers.get("Content-Type", ""))
                except asyncio.TimeoutError as exc:
                    raise ValueError(f"NovelAI 图片生成超时（{self.timeout} 秒），未自动重试") from exc
                except aiohttp.ClientConnectorError as exc:
                    certificate_error = isinstance(exc, (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError))
                    cause = exc.certificate_error if isinstance(exc, aiohttp.ClientConnectorCertificateError) else exc.os_error
                    logger.warning("NovelAI pre-request connection error: type=%s cause=%s attempt=%d",
                                   type(exc).__name__, type(cause).__name__, attempt + 1)
                    if not certificate_error and attempt == 0:
                        await asyncio.sleep(0.5)
                        continue
                    detail = "TLS 证书校验失败" if certificate_error else ("连接被重置" if isinstance(cause, ConnectionResetError) else "网络或代理连接失败")
                    raise ValueError(f"NovelAI 建立连接失败（{detail}），请稍后再试") from exc
                except aiohttp.ClientError as exc:
                    logger.warning("NovelAI request/response network error: type=%s status=%s received_bytes=%d",
                                   type(exc).__name__, status, received)
                    if isinstance(exc, aiohttp.ClientPayloadError):
                        detail = "图片响应传输不完整"
                    elif isinstance(exc, aiohttp.ServerDisconnectedError):
                        detail = "服务端中断了连接"
                    else:
                        detail = "请求或接收响应时连接失败"
                    raise ValueError(f"NovelAI {detail}，未自动重试") from exc
