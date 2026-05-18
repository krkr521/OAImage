import base64
import mimetypes
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import aiohttp
from oa_image_config import _get_nested

LOCAL_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

try:
    from maim_message import Seg
except Exception:
    Seg = None  # type: ignore[assignment]


def _clean_base64(value: str) -> str:
    if not value:
        return ""
    if "," in value and value.strip().lower().startswith("data:"):
        value = value.split(",", 1)[1]
    return value.strip()


def _data_url_mime(value: str) -> str:
    match = re.match(r"^\s*data:([^;,]+);base64,", value or "", flags=re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _iter_segments(segment_or_segments):
    if not segment_or_segments:
        return
    if Seg is not None and isinstance(segment_or_segments, Seg):
        if segment_or_segments.type == "seglist":
            for child in segment_or_segments.data or []:
                yield from _iter_segments(child)
        else:
            yield segment_or_segments
        return
    if isinstance(segment_or_segments, list):
        for item in segment_or_segments:
            yield from _iter_segments(item)
        return
    if isinstance(segment_or_segments, dict):
        if segment_or_segments.get("type") == "seglist":
            yield from _iter_segments(segment_or_segments.get("data") or [])
        else:
            yield segment_or_segments
        return
    if hasattr(segment_or_segments, "type"):
        yield segment_or_segments


def _segment_type(seg: Any) -> str:
    if isinstance(seg, dict):
        return str(seg.get("type") or "")
    return str(getattr(seg, "type", "") or "")


def _segment_data(seg: Any) -> Any:
    if isinstance(seg, dict):
        return seg.get("binary_data_base64") or seg.get("base64") or seg.get("data") or seg.get("content")
    data = getattr(seg, "data", None)
    if isinstance(data, dict):
        return data.get("binary_data_base64") or data.get("base64") or data.get("data") or data.get("content")
    return data


def extract_images(segment_or_segments) -> List[str]:
    images: List[str] = []
    for seg in _iter_segments(segment_or_segments):
        if _segment_type(seg) in ("image", "emoji"):
            data = _segment_data(seg)
            if not isinstance(data, str):
                continue
            image_base64 = _clean_base64(data)
            if image_base64:
                images.append(image_base64)
    return images


async def _download_as_base64(session: aiohttp.ClientSession, url: str, timeout: int) -> str:
    async with session.get(url, timeout=timeout) as response:
        response.raise_for_status()
        return base64.b64encode(await response.read()).decode("ascii")


def _guess_image_mime(name: str, default: str = "image/png") -> str:
    mime_type, _ = mimetypes.guess_type(name)
    if mime_type and mime_type.startswith("image/"):
        return mime_type
    return default


def _safe_image_filename(name: str, default: str = "reference.png") -> str:
    filename = Path(name).name.strip()
    if filename and Path(filename).suffix.lower() in LOCAL_IMAGE_SUFFIXES:
        return filename
    return default


async def _download_reference_image(session: aiohttp.ClientSession, url: str, timeout: int) -> Tuple[str, str, str]:
    async with session.get(url, timeout=timeout) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        parsed = urlparse(url)
        filename = _safe_image_filename(unquote(Path(parsed.path).name), "reference.png")
        mime_type = content_type if content_type.startswith("image/") else _guess_image_mime(filename)
        return base64.b64encode(await response.read()).decode("ascii"), mime_type, filename


def _looks_like_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _extract_first_url(text: str) -> str:
    match = re.search(r"https?://[^\s，。),）\]]+", text or "")
    return match.group(0) if match else ""


def _path_from_file_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme.lower() != "file":
        return ""
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = f"//{parsed.netloc}{path}"
    if re.match(r"^/[a-zA-Z]:[\\/]", path):
        path = path[1:]
    return path


def _is_relative_to_path(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _local_image_root(config: Optional[dict]) -> Path:
    configured = str(_get_nested(config, "behavior.local_image_base_dir", "local_images") or "local_images").strip()
    root = Path(configured).expanduser()
    if not root.is_absolute():
        plugin_dir = str(_get_nested(config, "runtime.plugin_dir", "") or "").strip()
        root_parent = Path(plugin_dir).expanduser() if plugin_dir else Path.cwd()
        root = root_parent / root
    return root.resolve()


def _local_path_from_source(value: str, config: Optional[dict] = None) -> Optional[Path]:
    source = (value or "").strip().strip('"').strip("'")
    if not source:
        return None
    if source.lower().startswith("file:"):
        source = _path_from_file_url(source)
    path = Path(source).expanduser()
    if path.suffix.lower() not in LOCAL_IMAGE_SUFFIXES:
        return None
    root = _local_image_root(config)
    if not path.is_absolute():
        path = root / path
    resolved_path = path.resolve()
    if not _is_relative_to_path(resolved_path, root):
        return None
    return resolved_path


def _looks_like_local_image_source(value: str, config: Optional[dict] = None) -> bool:
    return _local_path_from_source(value, config) is not None


def _looks_like_any_local_image_source(value: str) -> bool:
    source = (value or "").strip().strip('"').strip("'")
    if not source or _looks_like_url(source):
        return False
    if source.lower().startswith("file:"):
        source = _path_from_file_url(source)
    return Path(source).suffix.lower() in LOCAL_IMAGE_SUFFIXES


def _extract_local_image_source(text: str) -> str:
    if not text:
        return ""
    suffixes = r"png|jpg|jpeg|webp|gif"
    patterns = [
        rf"file://[^\s\"'<>|]+\.(?:{suffixes})",
        rf"[\"']([^\"']+\.(?:{suffixes}))[\"']",
        rf"([a-zA-Z]:[\\/][^\r\n\"<>|]+\.(?:{suffixes}))(?=$|\s|[，。,.；;])",
        rf"((?:\.{{1,2}}[\\/]|[\w.-]+[\\/])[\w .()\-\\/]+\.(?:{suffixes}))(?=$|\s|[，。,.；;])",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        return (match.group(1) if match.lastindex else match.group(0)).strip()
    return ""


def _load_local_reference_image(config: dict, image_source: str) -> Tuple[str, str, str]:
    root = _local_image_root(config)
    path = _local_path_from_source(image_source, config)
    if not path:
        raise ValueError(f"local image_source must be inside {root}")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"local image_source not found: {path}")
    max_bytes = int(_get_nested(config, "behavior.local_image_max_bytes", 10 * 1024 * 1024))
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"local image_source is too large: {size} bytes > {max_bytes} bytes")
    return base64.b64encode(path.read_bytes()).decode("ascii"), _guess_image_mime(path.name), path.name
