"""图片提示词的结构化交付，避免角色栏在客户端中被拼成一段文字。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class NovelAICharacterPrompt:
    prompt: str
    negative_prompt: str = ""
    center: tuple[float, float] | None = None


@dataclass(frozen=True)
class NovelAIPrompt:
    prompt: str
    character_prompts: tuple[NovelAICharacterPrompt, ...] = ()
    negative_prompt: str = ""
    contains_text: bool = False
    quality_preset: str = "standard"
    uc_preset: str = "light"
    skill_modules: tuple[str, ...] = ()
    skill_version: str = ""
