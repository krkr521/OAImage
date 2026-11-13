# Provider 配置说明

本插件通过“生图套餐”配置上游服务。公开文档以 OpenAI 兼容图片端点为主，便于用户迁移到不同供应商的兼容 API。

每个套餐位于 `[profiles.items.<套餐ID>]` 下，包含 provider 类型、API 端点、密钥、模型参数、透传字段和给 Bot 看的说明。

## NovelAI 原生图片端点

`provider = "novelai"` 使用 [NovelAI 官方图片协议](https://image.novelai.net/docs/index.html)，不是 OpenAI 图片格式。请求发送至 `POST /ai/generate-image`，使用 Persistent API Token；接收官方 JSON/base64 响应，也兼容 ZIP 图片包。不会自动重试计费请求，401、403、429 等错误只返回状态摘要。

插件已提供 `novelai` 套餐。以下字段填写在该套餐中：

```toml
[profiles.items.novelai]
display_name = "NovelAI"
provider = "novelai"
base_url = "https://image.novelai.net"
api_key = "pst-..."
model = "nai-diffusion-5-full"
timeout_seconds = 180
supported_modes = ["text_to_image"]
prompt_style = "novelai_v5"

[profiles.items.novelai.generation]
size = "832x1216"
steps = 28
scale = 5.0
sampler = "k_euler_ancestral"
noise_schedule = "karras"
seed = -1
max_concurrency = 1
negative_prompt = "lowres, bad_anatomy, blurry, watermark"

[profiles.items.novelai.prompt]
include_context = true
context_hours = 0.5
context_message_limit = 20
context_character_limit = 8000
rewrite_enabled = true
rewrite_model = "deepseek-v4-flash-fast"
rewrite_timeout_seconds = 60
rewrite_max_tokens = 3000
skill_directory = 'D:\qqbot-data\oaimage\novelai-assistant-2e8601b9'

[profiles.items.novelai.behavior]
default_delivery = "chat"
trigger_continue_after_generation = false
```

尺寸必须为 64 的倍数且总像素不超过 1048576，步数限制为 1–28，`seed = -1` 表示随机，一次请求固定一张。这些是本插件的运行限制，不代表账户的免费额度承诺。

NovelAI 固定执行非露骨图片提示词整理；上下文、用户描述视作画面资料，不能覆盖整理任务要求。DeepSeek 调用使用宿主已配置模型，拒绝、无效 JSON 或服务异常都会停止，不切换模型继续尝试。

V5 使用 `params_version = 4`，保留官方协议的 `v4_prompt` / `v4_negative_prompt` 字段名，并分别传递角色 captions 和位置。噪声计划固定为 `karras`，质量和 UC 预设使用 `qualityPresetId` / `ucPresetId`。客户端把预设转换为实际提示词，文字画面使用 `qualityPresetId = "none"`；一张图片请求可包含多个角色，但不等于上传了角色参考图。V4.5 配置仍按既有协议处理。

V5 套餐需要安装锁定提交的技能，并配置 `prompt.skill_directory`。本机使用 [novelai-assistant](https://github.com/2332239652/novelai-assistant/tree/2e8601b998cfd3acc7c7f1cf88115e5626b17cca)，提交 `2e8601b998cfd3acc7c7f1cf88115e5626b17cca`；23 个原始文件和 MIT 许可完整保留，`SOURCE.json` 记录文件哈希。每次只读适用的 V5 规则和相关资源，不执行作者的通道/凭据/MCP 配置。`rewrite_max_tokens` 控制结构化整理结果的输出上限，不是图片模型的提示词额度。

`prompt` 中新增 `rewrite_enabled`、`rewrite_model`、`rewrite_timeout_seconds` 和 `context_character_limit`；OpenAI 与 NovelAI 套餐分别独立启用整理；未配置的自定义套餐沿用全局 prompt 开关。套餐级 `behavior` 可覆盖默认交付方式及生成后继续对话开关，不改变其他套餐。

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

## 多角色参考图

`character_library` 需要角色知识插件已登记原图；只有文字外观卡不能作为图片输入。
OpenAI 兼容套餐使用 `multipart_edit` 上传多张独立文件，多图字段为重复的 `image[]`，单图继续使用 `image`。
参考图上传顺序写入提示词，避免多角色身份混合；最多引用 8 个私库角色，整个编辑请求最多 16 张图。
`json_generation`、`json_edit` 的旧单图行为保留；多图请求不会静默丢弃其他图片，会明确提示改用 `multipart_edit`。
非 OpenAI JSON 套餐不能使用私库图片。
协议依据：[OpenAI 图片生成文档](https://developers.openai.com/api/docs/guides/image-generation)。

## 兼容说明

插件代码里保留了非 OpenAI JSON 端点的兼容路径，用于已有本地配置迁移和自定义上游。公开文档不推荐任何具体第三方站点；新配置建议优先使用 `provider = "openai"`。


## 当前会话参考图（1.4.2）

OpenAI 兼容图片编辑套餐可使用 `reference_message_id` 与 `reference_image_index`，由宿主的同会话图片读取能力提供原始字节并识别 PNG/JPEG/WebP/GIF MIME。它与 `image_source` 互斥；指定消息时不再用“最近图”替代。仅 `use_recent_image=true` 时读取当前聊天的普通原图缓存，或在配置的有效期内读取同会话持久消息；不会读取其他聊天流。

原图选择独立于 DeepSeek 提示词整理。缺图在整理模型与图片上游调用前报错。NovelAI 纯文字套餐保持不接收参考图；没有新增上游参数、费用重试或图片端点请求。

工具触发与普通 `/生图` 的后台结果进入内部任务状态与上下文；成功关闭主动继续也保留内部完成记录。失败结果经过相同密钥脱敏后再进入上下文和原会话错误消息。`/生图r` 走静默链路：只回一句确认并拦截指令本身，结果不入上下文、不唤醒 Planner。


OpenAI 默认采用 GPT Image 2.5 Sunburst；`params.quality = "high"` 同时用于文生图和图生图。OpenAI 的自然语言整理规范独立于 NovelAI 标签规范，详见 README「OpenAI 画面整理」。


OpenAI 独立整理使用 `prompt.rewrite_model = "planner"` 跟随宿主 Planner 任务配置；该值为任务路由，不是名为 planner 的具体模型。若明确填写其他宿主模型名，则按该具体模型调用。NovelAI 当前仍保留其套餐级 DeepSeek 配置。
# GPT Image 2.6 GIF 输出

文生 GIF 使用 `POST /v1/images/generations`，参考图转 GIF 使用 `POST /v1/images/edits`。两个接口均传 `output_format="gif"`、`frame_count`、`frame_duration_ms`，并在 `data[].b64_json` 中返回完整动图。帧数 2～16，默认 9。帧间隔 20～2000 毫秒，默认 200，舍入到 10 毫秒。`size` 是每帧尺寸，默认沿用套餐的 `auto`，实际仍受模型的总像素预算限制。

命令入口包括 `/动图`、`/gif`、`/生图 --gif` 和 `/图生图 --gif`。自然语言工具用 `generate_image(output_format="gif")` 选择动图格式，指定参考图时用 `mode="image_to_image"`。默认从 `[animation].profile` 选择有 GIF 预览权限的 GPT Image 2.6 套餐，也可以用 `--profile` 或工具 `profile` 指定。

GIF 请求移除不适用的 `stream`、`partial_images`、`output_compression`、上游 `prompt_rewriter`。独立文字整理在插件内执行，与上游 `prompt_rewriter` 无关。保留 `quality`、`size` 和原有用量上报。

参考图编辑套餐使用 `generation.image_to_image_mode="multipart_edit"` 上传 PNG、JPEG 或 WebP 静态图。GIF 输出的蒙版参数会在发送前报错。原图读取和参考清单复用现有编辑路径，送图时保留上游 GIF 字节。

来源：[OpenAI GIF 文档](https://developers.openai.com/api/docs/guides/image-generation#generate-animated-gif-output)。
