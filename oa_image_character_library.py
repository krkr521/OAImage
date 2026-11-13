"""从角色知识插件的私库读取具名参考图，不开放任意本地文件读取。"""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
import base64
import hashlib
import json
import re

from oa_image_config import _get_nested


@dataclass(frozen=True)
class CharacterImage:
    name: str
    image_base64: str
    mime: str
    filename: str


def _library_path(config: dict) -> Path:
    plugin_dir = Path(str(_get_nested(config, "runtime.plugin_dir", Path(__file__).parent)))
    return plugin_dir.resolve().parent.parent / "data/plugins/github.QlzqQlzq.character-knowledge-plugin/characters.json"


def _mentions(text: str, name: str) -> bool:
    if not name:
        return False
    if name.isascii():
        return bool(re.search(r"(?<![A-Za-z0-9_-])" + re.escape(name) + r"(?![A-Za-z0-9_-])", text, re.IGNORECASE))
    return name.casefold() in text.casefold()


def _read_image(root: Path, entry: dict, max_bytes: int) -> tuple[str, str, str]:
    relative = Path(str(entry.get("file") or ""))
    allowed_root = (root / "reference_images").resolve()
    path = (root / relative).resolve()
    if relative.is_absolute() or not allowed_root.is_relative_to(root.resolve()) or not path.is_relative_to(allowed_root):
        raise ValueError("角色参考图路径超出私库 reference_images 目录")
    if not path.is_file():
        raise ValueError("角色参考图文件缺失，请重新添加该角色的参考图")
    if not 0 < path.stat().st_size <= max_bytes:
        raise ValueError("角色参考图超过大小限制")
    with path.open("rb") as source:
        data = source.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("角色参考图超过大小限制")
    digest = str(entry.get("sha256") or "")
    if not digest or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("角色参考图校验失败，请重新添加该角色的参考图")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("生图角色参考图必须是 PNG、JPEG 或 WebP")
    return base64.b64encode(data).decode("ascii"), mime, path.name


def resolve_character_images(
    config: dict, prompt: str, characters: list[str] | None = None,
) -> tuple[list[CharacterImage], list[str]]:
    """显式角色优先；未指定时只在本次画图需求中匹配名称，不扫描聊天历史。"""
    if characters is not None and (not isinstance(characters, list) or any(not isinstance(item, str) for item in characters)):
        raise ValueError("characters 必须是角色名字符串数组")
    if not _get_nested(config, "character_library.enabled", False):
        if characters:
            raise ValueError("角色库生图参考尚未启用")
        return [], []
    path = _library_path(config)
    if not path.is_file():
        if characters:
            raise ValueError("角色私库不存在")
        return [], []
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("characters")
    if not isinstance(entries, list):
        raise ValueError("角色私库 characters 格式错误")
    catalog = [item for item in entries if isinstance(item, dict)]
    personas = _get_nested(config, "character_library.personas", {})
    aliases = {str(k).casefold(): str(v) for k, v in personas.items()}
    requested = list(characters or [])
    if not requested and _get_nested(config, "character_library.auto_match", True):
        for entry in catalog:
            if any(_mentions(prompt, str(name)) for name in [entry.get("name", ""), *entry.get("aliases", [])]):
                requested.append(str(entry["name"]))
        requested.extend(value for name, value in personas.items() if _mentions(prompt, str(name)))
    images: list[CharacterImage] = []
    missing: list[str] = []
    seen: set[str] = set()
    max_characters = int(_get_nested(config, "character_library.max_characters", 8))
    for requested_name in requested:
        name = str(requested_name).strip()
        variant = "chibi" if re.search(r"Q版|chibi", prompt + " " + name, re.IGNORECASE) else "standard"
        if name.startswith("character://"):
            uri = urlsplit(name)
            if uri.path not in {"", "/"}:
                raise ValueError("角色参考句柄格式错误")
            name = unquote(uri.netloc)
            variant = parse_qs(uri.query).get("variant", [variant])[0]
        name = aliases.get(name.casefold(), name)
        target = next((entry for entry in catalog if name.casefold() in {
            str(value).casefold() for value in [entry.get("name", ""), *entry.get("aliases", [])]
        }), None)
        canonical = str(target["name"]) if target else name
        if not canonical or canonical.casefold() in seen:
            continue
        seen.add(canonical.casefold())
        if len(seen) > max_characters:
            raise ValueError(f"单次最多引用 {max_characters} 个角色")
        refs = target.get("reference_images", []) if target else []
        if not refs:
            missing.append(canonical)
            continue
        selected = next((ref for ref in refs if ref.get("variant") == variant), refs[0])
        b64, mime, filename = _read_image(
            path.parent, selected, int(_get_nested(config, "behavior.local_image_max_bytes", 10 * 1024 * 1024)),
        )
        images.append(CharacterImage(canonical, b64, mime, filename))
    return images, missing


def reference_prompt(
    prompt: str, labels: list[str], missing: list[str], roles: list[str] | None = None,
) -> str:
    """将上传顺序与身份绑定，防止多人场景中把两个角色的外形混在一起。"""
    if not labels and not missing:
        return prompt
    lines = ["Character reference mapping (input images in upload order):"]
    for index, name in enumerate(labels, 1):
        role = roles[index - 1] if roles and index <= len(roles) else "character_identity"
        purpose = (
            "User reference: apply only the requested role; clothing/style alone does not define the target's identity."
            if role == "user_reference" else "Identity reference for this character."
        )
        lines.append(f"Image {index}: {name}. {purpose}")
    if labels:
        lines.append(
            "Preserve each character's face, hair, eyes and identity from their assigned identity reference only. "
            "Keep the characters distinct. Follow the requested scene, composition and clothing changes; "
            "do not copy reference-sheet text, layout or unrelated characters."
        )
    if missing:
        lines.append("Requested characters with NO library reference image: " + ", ".join(missing) + ".")
    return "\n".join(lines) + "\n\nScene request:\n" + prompt
