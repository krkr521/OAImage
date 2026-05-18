# Provider 配置说明

本插件通过“生图套餐”配置上游服务。公开文档以 OpenAI 兼容图片端点为主，便于用户迁移到不同供应商的兼容 API。

每个套餐位于 `[profiles.items.<套餐ID>]` 下，包含 provider 类型、API 端点、密钥、模型参数、透传字段和给 Bot 看的说明。

## OpenAI 兼容端点

`provider = "openai"` 使用 OpenAI 兼容图片接口格式。

基础字段：

- `base_url`：服务商的 API 根地址。
- `api_key`：服务商密钥。
- `model`：图片模型名称。
- `generations_path`：文生图端点路径。
- `edits_path`：图生图/图片编辑端点路径。
- `timeout_seconds`：请求超时时间。

文生图核心 payload：

- `model`
- `prompt`
- `size`
- `n`
- `response_format`

图生图支持三种模式，由套餐中的 `generation.image_to_image_mode` 控制：

- `multipart_edit`：以 multipart/form-data 发送图片，适合 `/images/edits` 一类接口。
- `json_generation`：以 JSON 发送图片字段到 `generations_path`。
- `json_edit`：以 JSON 发送图片字段到 `edits_path`。

图生图参考图来源可以是：

- 最近聊天图片：Tool 传 `mode = "image_to_image"` 和 `use_recent_image = true`。
- 图片 URL：Tool 或命令传 `image_source`，插件会先下载成 base64 再按 OpenAI 兼容格式发送。
- 本地图片路径：需要先在 `[behavior]` 中开启 `allow_local_image_source = true`。本地文件必须位于 `local_image_base_dir` 指定目录内。

本地路径支持 `.png`、`.jpg`、`.jpeg`、`.webp`、`.gif`，并受 `local_image_base_dir` 和 `local_image_max_bytes` 限制。

## 基础套餐示例

```toml
[profiles]
default_profile = "openai_default"

[profiles.items.openai_default]
display_name = "OpenAI 兼容默认套餐"
description = "适合通用文生图，也可按上游能力配置图生图。"
supported_modes = ["text_to_image", "image_to_image"]
prompt_style = "natural_language"
use_cases = ["通用配图", "头像", "海报主视觉", "图片编辑"]
requires_image_source = false
action_hint = "prompt 使用自然语言描述画面，写清主体、场景、构图、光照、风格和用途；图生图时写清需要保留与修改的内容。"
provider = "openai"
base_url = "https://api.example.com/v1"
api_key = "sk-..."
model = "image-generation-model"
generations_path = "/images/generations"
edits_path = "/images/edits"
timeout_seconds = 120

[profiles.items.openai_default.generation]
size = "1024x1024"
response_format = "b64_json"
image_to_image_mode = "multipart_edit"
json_image_field = "image"

[profiles.items.openai_default.passthrough]
enabled = false
allow_override_core_params = false
shared_params = {}
text_to_image_params = {}
image_to_image_params = {}
```

## 图生图套餐示例

```toml
[profiles.items.openai_edit]
display_name = "OpenAI 兼容图片编辑"
description = "适合参考图编辑、局部重绘、风格转换。"
supported_modes = ["image_to_image"]
prompt_style = "edit_instruction"
use_cases = ["图生图", "图片编辑", "风格转换"]
requires_image_source = false
action_hint = "prompt 写成明确编辑指令，说明保留什么、修改什么、输出风格是什么。"
provider = "openai"
base_url = "https://api.example.com/v1"
api_key = "sk-..."
model = "image-edit-model"
edits_path = "/images/edits"
timeout_seconds = 120

[profiles.items.openai_edit.generation]
size = "1024x1024"
response_format = "b64_json"
image_to_image_mode = "multipart_edit"
```

## 给 Bot 看的套餐字段

下面这些字段不会作为 API 请求参数发送给上游，而是暴露给 `generate_image` Tool，帮助 Bot 判断该选哪个套餐，以及应该怎样写 prompt。

- `display_name`：套餐显示名，Tool 的 `profile` 参数可以填套餐 ID，也可以填这个显示名。
- `description`：套餐介绍，用来说明模型或端点的大致能力。
- `supported_modes`：套餐支持的模式，例如 `text_to_image`、`image_to_image`。
- `prompt_style`：提示词风格，例如 `danbooru_tags`、`natural_language`、`edit_instruction`。
- `use_cases`：适用场景列表。
- `requires_image_source`：是否必须提供 `image_source`。
- `action_hint`：直接写给 Bot 的使用要求。

## generation 参数

请求参数放在套餐的 `generation` 子表里。

```toml
[profiles.items.openai_default.generation]
size = "1024x1024"
response_format = "b64_json"
image_to_image_mode = "multipart_edit"
json_image_field = "image"
```

不同上游会读取不同字段。未被当前 provider 使用的字段会被忽略。

## 透传参数

如果上游需要插件没有内置的字段，可以在套餐内开启透传。

```toml
[profiles.items.openai_default.passthrough]
enabled = true
allow_override_core_params = false
shared_params = { quality = "high" }
text_to_image_params = { style = "natural" }
image_to_image_params = { strength = 0.65 }
```

`shared_params` 会应用到所有请求。

`text_to_image_params` 只应用到文生图。

`image_to_image_params` 只应用到图生图或图片编辑。

默认情况下，透传字段不能覆盖核心字段。只有当上游确实要求用透传方式覆盖字段时，才建议把 `allow_override_core_params` 改成 `true`。

## 本地参考图限制

OpenAI 兼容端点不需要公网 URL。它可以直接使用最近聊天图片，也可以读取指定目录内的本地图片：

```toml
[behavior]
allow_local_image_source = true
local_image_base_dir = "local_images"
local_image_max_bytes = 10485760
url_image_max_bytes = 10485760
allow_non_image_url_content_type = false
```

为了避免群聊用户诱导 Bot 读取本机文件，本地路径默认关闭。开启后，本地 `image_source` 仍必须位于 `local_image_base_dir` 内。

URL 参考图会校验响应类型和大小。默认只接受 `Content-Type` 为 `image/*` 的响应，并通过 `url_image_max_bytes` 限制最大下载体积。如果上游图片服务没有正确返回图片类型，可把 `allow_non_image_url_content_type` 改为 `true`，但不建议在开放群聊中这样做。

`local_image_base_dir` 是允许读取的参考图目录。相对路径会按插件目录解析，因此默认值 `local_images` 实际对应：

```text
plugins/OAImage/local_images
```

即使传入绝对路径或带 `..` 的相对路径，最终解析后的文件也必须位于这个目录内部，否则插件会拒绝读取。

## 兼容说明

插件代码里保留了非 OpenAI JSON 端点的兼容路径，用于已有本地配置迁移和自定义上游。公开文档不推荐任何具体第三方站点；新配置建议优先使用 `provider = "openai"`。
