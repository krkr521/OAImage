"""按请求读取锁定的 novelai-assistant 提示词资料，不执行仓库内容。"""

from pathlib import Path
from typing import Any

import hashlib
import json
import re


SKILL_VERSION = "master@2e8601b9"
SKILL_COMMIT = "2e8601b998cfd3acc7c7f1cf88115e5626b17cca"
SKILL_REPOSITORY = "https://github.com/2332239652/novelai-assistant"

_CORE_SECTIONS = ("1.1", "1.2", "1.3", "1.4", "1.7", "3.1", "3.6")
# 3.7 是 SKILL.md 里的 NSFW 指南，只在成人向需求时才进上下文，避免影响普通插画。
_NSFW_SECTIONS = ("3.7",)
_CORRECTION_SECTIONS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "11", "12")
_ARTIST_STYLES = (
    "社区A", "清新日常", "厚涂质感", "水墨黑白", "Q版贴纸", "厚涂1", "质感1",
    "魔裁画风（梅まろ）", "茉子串 V5（ningsen_mame 单画师版）", "均衡画风 V5",
    "凉佬 V5", "轻漫画", "社区A1", "赛璐璐1", "二游渲染", "质感2",
)
_COMIC_PATTERN = r"漫画|四格|分镜|comic|manga"
_ARTIST_PATTERN = r"画师|画风|风格|艺术|厚涂|水墨|赛璐|清新日常|社区[AＡ]|Q版|贴纸|水彩|油画|artist[:\s]|\bstyle\b|watercolou?r"
_GAME_PATTERN = r"碧蓝航线|蔚蓝档案|明日方舟|原神|星穹铁道|azur.lane|blue.archive|arknights|genshin|star.rail"
_NSFW_PATTERN = (
    r"nsfw|r-?18|18\+|成人|里番|本子|色情|涩图|裸|全裸|半裸|脱衣|做爱|性交|性爱|性行为|口交|后入|骑乘|"
    r"触手|自慰|指交|射精|内射|颜射|乳交|潮吹|高潮|乳头|阿黑颜|ahegao|hentai|erotic|explicit|nude|"
    r"naked|lewd|tentacle|blowjob|fellatio|penetration|creampie|orgasm|pussy|penis|nipples?"
)
# 模板 1-12 与工具人写法都在 prompt-templates.md 里，命中模板编号或成人向需求时按需加载。
_TEMPLATE_REFERENCE_PATTERN = r"模板\s*[0-9０-９一二三四五六七八九十]+|template\s*\d+|第\s*[0-9一二三四五六七八九十]+\s*套"
_TEMPLATE_BASE_SECTIONS = ("通用提示词框架", "场景模板")
_TEMPLATE_NSFW_SECTIONS = ("NSFW 模板专题", "工具人男角色 & POV 视角")


def _read_verified(root: Path, relative: str, hashes: dict[str, str]) -> str:
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path.is_symlink():
        raise ValueError("NovelAI 技能资料路径无效")
    try:
        if path.stat().st_size > 600_000:
            raise ValueError("NovelAI 技能单文件过大")
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError("NovelAI 技能资料缺失，请重新安装锁定版本") from exc
    if hashlib.sha256(data).hexdigest() != hashes.get(relative):
        raise ValueError("NovelAI 技能资料校验失败，请核对版本或本地修改")
    return data.decode("utf-8-sig")


def _numbered_sections(text: str, numbers: tuple[str, ...]) -> str:
    """保留选定章节原文；作者自己的通道、账户、参数和交互流程不参与整理。"""
    selected = {}
    for section in re.split(r"(?=^#{1,2} )", text, flags=re.MULTILINE):
        heading = re.match(r"^## (\d+(?:\.\d+)*)\.?\s", section)
        if heading and heading[1] in numbers:
            selected[heading[1]] = section
    if set(numbers) - selected.keys():
        raise ValueError("NovelAI 技能正文缺少必需章节，请核对锁定版本")
    return "".join(selected[number] for number in numbers)


def _present_sections(text: str, numbers: tuple[str, ...]) -> tuple[str, ...]:
    """只保留资料里真实存在的章节号；可选章节缺失时继续加载其余内容。"""
    present = {match[1] for match in re.finditer(r"^## (\d+(?:\.\d+)*)\.?\s", text, flags=re.MULTILINE)}
    return tuple(number for number in numbers if number in present)


def _named_sections(text: str, names: tuple[str, ...]) -> str:
    """按二级标题名收录章节原文（模板库用编号以外的标题组织）。"""
    selected = {}
    for section in re.split(r"(?=^## )", text, flags=re.MULTILINE):
        heading = re.match(r"^## (.+?)\s*$", section.split("\n", 1)[0])
        if heading:
            selected[heading[1]] = section
    return "".join(selected[name] for name in names if name in selected)


def _artist_guidance(text: str, selection: str) -> str:
    """只选当前服务适用的 V5 画风配方；点名配方时不混入其他画风。"""
    sections = {}
    for section in re.split(r"(?=^## )", text, flags=re.MULTILINE):
        heading = re.match(r"^## (.*?) ✅", section)
        if heading and heading[1] in _ARTIST_STYLES:
            sections[heading[1]] = section
    names = [name for name in sections if re.search(re.escape(name) + r"(?![A-Za-z0-9])", selection, re.IGNORECASE)]
    return "".join(sections[name] for name in (names or list(sections)))


def load_skill_guidance(directory: str, request_text: str, scene: str = "") -> tuple[str, tuple[str, ...]]:
    """使用 V5 核心和校正文，按需补充画师配方、漫画规范或角色标签。"""
    if not directory:
        raise ValueError("尚未配置 NovelAI 提示词技能目录")
    root = Path(directory).resolve()
    try:
        manifest: dict[str, Any] = json.loads((root / "SOURCE.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("NovelAI 技能来源清单缺失或无效") from exc
    if (manifest.get("repository") != SKILL_REPOSITORY or manifest.get("version") != SKILL_VERSION
            or manifest.get("commit") != SKILL_COMMIT or manifest.get("skill_count") != 1):
        raise ValueError("NovelAI 技能版本不符，需要安装 novelai-assistant " + SKILL_VERSION)
    hashes = {item["path"]: item["sha256"] for item in manifest.get("files", [])}
    modules = ["novelai-assistant"]
    selection = request_text
    if re.search(r"刚才|上面|之前|继续|按.*讨论|as.before|continue", request_text, re.IGNORECASE):
        selection += "\n" + scene[-8000:]
    nsfw = bool(re.search(_NSFW_PATTERN, selection, re.IGNORECASE))
    skill_text = _read_verified(root, "SKILL.md", hashes)
    numbers = _present_sections(skill_text, _CORE_SECTIONS + (_NSFW_SECTIONS if nsfw else ()))
    sources = [("SKILL.md", _numbered_sections(skill_text, numbers))]
    if any(number in numbers for number in _NSFW_SECTIONS):
        modules.append("nsfw-guide")
    # 模板库与编号模板（模板 1-12）必须真实进上下文，否则「用模板 9」对整理模型是空词。
    template_names: tuple[str, ...] = ()
    if re.search(_TEMPLATE_REFERENCE_PATTERN, selection, re.IGNORECASE):
        template_names += _TEMPLATE_BASE_SECTIONS
    if nsfw:
        template_names += _TEMPLATE_NSFW_SECTIONS
    if template_names and "prompt-templates.md" in hashes:
        pack = _named_sections(_read_verified(root, "prompt-templates.md", hashes), template_names)
        if pack:
            sources.append(("prompt-templates.md", pack))
            modules.append("prompt-templates")
    if re.search(_ARTIST_PATTERN, selection, re.IGNORECASE) or any(name.casefold() in selection.casefold() for name in _ARTIST_STYLES):
        relative = "v5-confirmed-artist-strings.md"
        sources.append((relative, _artist_guidance(_read_verified(root, relative, hashes), selection)))
        modules.append("v5-confirmed-artist-strings")
    if re.search(_GAME_PATTERN, selection, re.IGNORECASE):
        relative = "game-character-tags.md"
        sources.append((relative, _read_verified(root, relative, hashes)))
        modules.append("game-character-tags")
    # 校正文优先于旧核心章节；漫画专用规则最后覆盖普通插画的分工建议。
    relative = "v5-field-corrections.md"
    sources.append((relative, _numbered_sections(_read_verified(root, relative, hashes), _CORRECTION_SECTIONS)))
    modules.append("v5-field-corrections")
    if re.search(_COMIC_PATTERN, selection, re.IGNORECASE):
        relative = "manga-page-playbook.md"
        sources.append((relative, _numbered_sections(_read_verified(root, relative, hashes), tuple(str(n) for n in (*range(9), 10, 11)))))
        modules.append("manga-page-playbook")
    parts = []
    for relative, text in sources:
        parts.append(f"\n--- SOURCE: {relative} ({SKILL_VERSION}) ---\n{text}")
    guidance = "".join(parts)
    if len(guidance) > 100_000:
        raise ValueError("本次 NovelAI 技能资料过多，请将画面需求拆分后重试")
    return guidance, tuple(modules)
