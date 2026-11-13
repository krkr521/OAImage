"""从标准输入接收 Token，按用户选择明文保存在现有生图套餐配置。"""

from pathlib import Path

import json
import sys

import tomlkit


def main() -> None:
    payload = json.load(sys.stdin)
    token = str(payload.get("token") or "").strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError("Token 为空或包含空白，未更改配置")
    path = Path(__file__).resolve().parents[1] / "config.toml"
    document = tomlkit.parse(path.read_text(encoding="utf-8-sig"))
    document["profiles"]["items"]["novelai"]["api_key"] = token
    if payload.get("make_default"):
        document["profiles"]["default_profile"] = "novelai"
    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    print("NovelAI Token 已保存到生图套餐配置；未输出密钥。")
    if payload.get("make_default"):
        print("默认套餐已设为 NovelAI。")


if __name__ == "__main__":
    main()
