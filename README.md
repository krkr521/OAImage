# OAImage

OAImage 是适配 MaiBot 0.12.2 的生图插件。插件通过“生图套餐”调用上游服务，主要面向 OpenAI 兼容图片端点，并允许额外传递参数。

## 基础开关

```toml
[plugin]
enabled = true
enable_action = true
```

`enabled` 控制整个插件是否启用。

`enable_action` 控制是否注册 `generate_image` Action。关闭后，Bot/planner 不会看到也不会调用这个 Action；命令入口和事件缓存组件仍会注册，因此 `/生图`、`/画图`、`/图生图` 这类命令仍可使用。

## 生图套餐

所有上游请求配置都放在 `[profiles.items.<套餐ID>]` 下。一个套餐就是一组 provider、API 端点、密钥、模型参数、透传字段、显示名和介绍。

```toml
[profiles]
default_profile = "openai_default"

[profiles.items.openai_default]
display_name = "OpenAI 兼容默认套餐"
description = "适合通用文生图，也可按上游能力配置图生图。"
supported_modes = ["text_to_image"]
prompt_style = "natural_language"
use_cases = ["通用配图", "头像", "海报主视觉"]
requires_image_source = false
action_hint = "prompt 使用自然语言描述画面，写清主体、场景、构图、光照、风格和用途。"
provider = "openai"
base_url = "https://api.example.com/v1"
api_key = "sk-..."
model = "image-generation-model"
generations_path = "/images/generations"
edits_path = "/images/edits"
timeout_seconds = 120
```

`default_profile` 是默认套餐。用户命令和 Action 都没有指定套餐时，会使用这个套餐。

provider 类型、上游字段、透传参数、本地参考图和多套餐示例集中维护在 [PROVIDERS.md](PROVIDERS.md)。

## 给 Bot 看的套餐元信息

下面这些字段不会作为 API 请求参数发送给上游，而是暴露给 `generate_image` Action，帮助 bot 判断该选哪个套餐，以及应该怎样写 prompt。

`display_name` 是套餐显示名。Action 的 `profile` 参数可以填套餐 ID，也可以填这个显示名。

`description` 是套餐介绍，用来说明模型或端点的大致能力。

`supported_modes` 是套餐支持的模式。可选值通常是：

- `text_to_image`
- `image_to_image`

如果 Action 选择了套餐不支持的模式，插件会拒绝执行，避免用错请求格式。

`prompt_style` 是提示词风格，例如：

- `danbooru_tags`：英文 tag 风格，适合二次元模型。多词概念应使用下划线，例如 `white_dress`、`blue_sky`、`looking_at_viewer`。
- `natural_language`：自然语言描述，适合通用图像模型。
- `edit_instruction`：图片编辑指令，适合图生图/编辑模型。

`use_cases` 是适用场景列表，bot 会根据用户需求匹配这些场景。

`requires_image_source` 表示这个套餐是否必须提供 `image_source`。OpenAI 兼容端点可以使用最近聊天图片、图片 URL，或在配置允许时读取本地图片路径。

`action_hint` 是直接写给 bot 的使用要求。这里可以描述 prompt 结构、禁忌、推荐写法、适合/不适合的任务。

## Bot 外形人设注入

插件支持在生成 Bot 本体、头像、立绘、自画像或外形图时，把预先配置好的 Bot 外貌参数注入到最终上游请求 prompt。

这个判断由 LLM/planner 负责。插件不会自动根据用户文本猜测“是不是在画 Bot”。当 planner 认为用户要画 Bot 本身时，需要在 `generate_image` Action 里传：

```json
{
  "prompt": "standing in a sunny classroom, smiling",
  "mode": "text_to_image",
  "profile": "精细竖图",
  "use_bot_appearance": true
}
```

配置示例：

```toml
[bot_appearance]
enabled = true
aliases = ["你", "麦麦", "MaiBot", "bot"]
trigger_phrases = ["画你", "你的头像", "你的立绘", "你的自画像"]
inject_mode = "prepend"
tag_prompt = "1girl, solo, silver_hair, blue_eyes, short_hair"
natural_prompt = "A cheerful young girl with short silver hair and blue eyes."
edit_prompt = "Keep the character consistent with the bot persona: short silver hair and blue eyes."
```

字段说明：

- `enabled`：是否启用外形注入。
- `aliases`：告诉 LLM 哪些称呼可能指向 Bot。
- `trigger_phrases`：给 LLM 的典型触发说法示例，不用于插件自动判断。
- `inject_mode`：`prepend` 放在用户 prompt 前，`append` 放在后。
- `tag_prompt`：当套餐 `prompt_style = "danbooru_tags"` 时使用。
- `natural_prompt`：当套餐 `prompt_style = "natural_language"` 时使用。
- `edit_prompt`：当套餐 `prompt_style = "edit_instruction"` 时优先使用。

如果 `use_bot_appearance` 没有传 `true`，插件不会注入这些外貌参数。

## Action 选择套餐

`generate_image` Action 支持 `profile` 参数。这个值可以填套餐 ID，也可以填套餐 `display_name`。

插件注册 Action 时会把当前配置中的套餐 ID、显示名和介绍追加到 Action 说明里。这样 bot 在规划 Action 时可以根据说明选择合适套餐。

例如：

```json
{
  "prompt": "1girl, solo, white dress, blue sky",
  "mode": "text_to_image",
  "profile": "精细竖图"
}
```

如果 bot 不确定应该用哪个套餐，可以不传 `profile`，插件会使用 `[profiles].default_profile`。

## 命令选择套餐

用户命令也可以临时指定套餐：

```text
/生图 --profile 精细竖图 1girl, solo, white dress, blue sky
/生图 --套餐 fast_square cat, meme, funny face
/图生图 --profile openai_edit --source "D:\Pictures\ref.png" 把背景改成夜晚街景
```

未指定套餐时使用默认套餐。

图生图的 provider 差异、参考图来源和本地目录限制见 [PROVIDERS.md](PROVIDERS.md)。

## 许可证

OAImage 使用 Apache-2.0 协议发布。
