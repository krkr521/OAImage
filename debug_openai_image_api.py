from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import aiohttp

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from oa_image_config import _get_nested, _merge_passthrough_params, _resolve_profile_config


PROMPT = "A simple cute beach illustration test."


def _redacted_config_value(key: str, value: Any) -> Any:
    if "key" in key.lower() or "token" in key.lower() or "authorization" in key.lower():
        return "<redacted>"
    return value


def _load_effective_config() -> dict[str, Any]:
    config_path = PLUGIN_DIR / "config.toml"
    with config_path.open("rb") as file:
        raw_config = tomllib.load(file)
    effective_config, profile_id, profile_label = _resolve_profile_config(
        raw_config,
        str(_get_nested(raw_config, "profiles.default_profile", "")),
    )
    print(f"profile={profile_id or '<none>'} label={profile_label or '<none>'}")
    print(
        "api="
        + json.dumps(
            {
                key: _redacted_config_value(key, _get_nested(effective_config, f"api.{key}", ""))
                for key in ("provider", "base_url", "api_key", "model", "generations_path", "edits_path")
            },
            ensure_ascii=False,
        )
    )
    return effective_config


def _url(config: dict[str, Any], path_key: str, default_path: str) -> str:
    base_url = str(_get_nested(config, "api.base_url", "")).rstrip("/")
    path = str(_get_nested(config, f"api.{path_key}", default_path))
    if path.startswith(("http://", "https://")):
        return path
    return f"{base_url}/{path.lstrip('/')}"


def _headers(config: dict[str, Any]) -> dict[str, str]:
    api_key = str(_get_nested(config, "api.api_key", ""))
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def _print_response(label: str, response: aiohttp.ClientResponse) -> None:
    body = await response.text()
    print(f"\n=== {label} ===")
    print(f"status={response.status}")
    print(f"content_type={response.headers.get('content-type', '')}")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        print(body[:2000])
        return
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if response.status < 400 and isinstance(data, list):
        compact = dict(parsed)
        compact_data = []
        for item in data:
            if not isinstance(item, dict):
                compact_data.append(item)
                continue
            compact_item = dict(item)
            b64 = compact_item.get("b64_json")
            if isinstance(b64, str):
                compact_item["b64_json"] = f"<base64 length={len(b64)}>"
            compact_data.append(compact_item)
        compact["data"] = compact_data
        print(json.dumps(compact, ensure_ascii=False, indent=2)[:4000])
        return
    print(json.dumps(parsed, ensure_ascii=False, indent=2)[:4000])


def _generation_payload(config: dict[str, Any], *, include_response_format: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": str(_get_nested(config, "api.model", "gpt-image-1")),
        "prompt": PROMPT,
        "size": str(_get_nested(config, "generation.size", "1024x1024")),
        "n": 1,
    }
    if include_response_format:
        payload["response_format"] = str(_get_nested(config, "generation.response_format", "b64_json"))
    return _merge_passthrough_params(config, payload, "text_to_image")


def _timeout(config: dict[str, Any], override_seconds: int | None) -> int:
    return int(override_seconds or _get_nested(config, "api.timeout_seconds", 120))


async def test_generation(
    config: dict[str, Any],
    *,
    include_response_format: bool,
    timeout_override: int | None,
) -> None:
    payload = _generation_payload(config, include_response_format=include_response_format)
    print(
        f"\nrequest=generation include_response_format={include_response_format} "
        f"payload={json.dumps(payload, ensure_ascii=False)}"
    )
    timeout = _timeout(config, timeout_override)
    async with aiohttp.ClientSession(headers=_headers(config)) as session:
        async with session.post(
            _url(config, "generations_path", "/v1/images/generations"),
            json=payload,
            timeout=timeout,
        ) as response:
            await _print_response(f"generation include_response_format={include_response_format}", response)


async def test_edit(
    config: dict[str, Any],
    *,
    include_response_format: bool,
    timeout_override: int | None,
) -> None:
    image_path = PLUGIN_DIR / "local_images" / str(_get_nested(config, "bot_appearance.reference_image", ""))
    if not image_path.is_file():
        print(f"\n=== edit skipped ===\nmissing image: {image_path}")
        return
    form = aiohttp.FormData()
    form.add_field("model", str(_get_nested(config, "api.model", "gpt-image-1")))
    form.add_field("prompt", PROMPT)
    form.add_field("size", str(_get_nested(config, "generation.size", "1024x1024")))
    form.add_field("n", "1")
    if include_response_format:
        form.add_field("response_format", str(_get_nested(config, "generation.response_format", "b64_json")))
    with image_path.open("rb") as file:
        image_bytes = file.read()
    form.add_field("image", image_bytes, filename=image_path.name, content_type="image/png")
    extra_payload = _merge_passthrough_params(
        config,
        {
            "model": str(_get_nested(config, "api.model", "gpt-image-1")),
            "prompt": PROMPT,
            "size": str(_get_nested(config, "generation.size", "1024x1024")),
            "n": "1",
            "image": "<binary>",
            **({"response_format": str(_get_nested(config, "generation.response_format", "b64_json"))} if include_response_format else {}),
        },
        "image_to_image",
    )
    for key, value in extra_payload.items():
        if key in {"model", "prompt", "size", "n", "response_format", "image"}:
            continue
        form.add_field(key, str(value).lower() if isinstance(value, bool) else str(value))
    printable_fields = {key: value for key, value in extra_payload.items() if key != "image"}
    printable_fields["image"] = f"<{image_path.name}, {len(image_bytes)} bytes>"
    print(
        f"\nrequest=edit include_response_format={include_response_format} "
        f"fields={json.dumps(printable_fields, ensure_ascii=False)}"
    )
    timeout = _timeout(config, timeout_override)
    async with aiohttp.ClientSession(headers=_headers(config)) as session:
        async with session.post(
            _url(config, "edits_path", "/v1/images/edits"),
            data=form,
            timeout=timeout,
        ) as response:
            await _print_response(f"edit include_response_format={include_response_format}", response)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--variant",
        choices=("current", "without-response-format", "both"),
        default="current",
        help="current matches OAImage's current request shape.",
    )
    parser.add_argument("--target", choices=("generation", "edit", "both"), default="both")
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args()

    config = _load_effective_config()
    variants = [True] if args.variant == "current" else [False] if args.variant == "without-response-format" else [True, False]
    for include_response_format in variants:
        if args.target in {"generation", "both"}:
            await test_generation(
                config,
                include_response_format=include_response_format,
                timeout_override=args.timeout,
            )
        if args.target in {"edit", "both"}:
            await test_edit(
                config,
                include_response_format=include_response_format,
                timeout_override=args.timeout,
            )


if __name__ == "__main__":
    asyncio.run(main())
