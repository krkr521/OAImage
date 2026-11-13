> **定制 fork**：本仓库基于 [A-Dawn/OAImage](https://github.com/A-Dawn/OAImage)。定制代码位于 `local/custom`，功能差异和安装限制见 [本地改动说明](LOCAL_CHANGES.md)。原作者及许可证保留。

# OAImage

OAImage 是适配 MaiBot 1.0 / maibot-plugin-sdk 2.x 的生图插件。插件通过“生图套餐”调用 OpenAI 兼容图片端点或 NovelAI 原生图片端点。

## NovelAI 与 DeepSeek 上下文生图

`novelai` 套餐使用 **NovelAI V5 Full**，支持普通、非露骨文生图。群聊发送：

```text
@机器人 /生图r 一只白猫坐在窗边，水彩风格
@机器人 /生图r 按刚才讨论的场景画一张
```

`/生图r` 固定选择 `novelai` 套餐，描述前需要空白，私聊直接发送。原 `/生图 --profile NovelAI 描述` 写法也可使用，但仍走普通套餐链路。

该入口固定使用 **V5 Full 纯文字生图**。人物名字作为文字处理，bot 设定图及角色库原图继续由普通生图套餐处理。

NovelAI 专用的 DeepSeek 提示词独立放在 `oa_image_novelai_prompt.py`：`INSTRUCTIONS` 是基础规则，`V5_FORMAT` 是 V5 Full 输出格式；`TAG_QUERY_FORMAT` 控制官方标签查询词，`TAG_RESULT_RULES` 控制如何使用真实返回数据。`LEGACY_FORMAT` 仅保留作旧版协议兼容。普通 `/生图` 的可选整理规则保留在 `oa_image_prompt.py`，不引用 NovelAI 模板；当前普通套餐仍未开启 DeepSeek 整理。第三方 novelai-assistant 技能资料按需从独立目录读取。模板文件已纳入插件热重载，修改后沿用宿主原有加载机制。

`/生图r` 是静默链路：命令链直接回一句随机的“收到/这就画”，随后拦截这条指令，不再交给 Planner/Replyer；生成完成后只把图片和必要的报错发回原会话，任务状态与识图描述都不写入内部历史，也不触发主动继续。唯一的例外是非成人画面：`rating` 为 `general` 或 `sensitive` 时写一条简短内部记录（`【已生成图片】rating:…；画面：…`），成人评级与失败仍然完全不进上下文；白名单由 `[behavior] context_rating_allow` 控制，置空即恢复全静默。其它仍走主链的写法（如 `/生图 --profile NovelAI 描述`）保留原行为：成功提交后在规划和回复上下文中给该指令追加说明 `【生图任务说明：此指令已交由独立后台任务自动处理，图片或报错由插件发回原会话，无需重复调用生图工具。】`，按原会话及真实消息编号匹配，不另发群消息，也不修改原消息或生图提示词。

指令提交后，后台任务读取**原会话**最近 30 分钟、最多 20 条消息，截取最多 8000 字符。读取时会先摘掉历史生图指令（宿主 `is_command` 标记优先，旧数据按指令文本兜底），只有本次指令明确要求延续上一张（接着/继续/上一张/再画一张一样的/同上/previous one 等）时才保留它们作为画面资料。DeepSeek 先选出 1–3 个相关标签查询词，插件使用同一 NovelAI Token 调用官方 `GET https://image.novelai.net/ai/generate-image/suggest-tags`（模型为 `nai-diffusion-5-full`），再将实际返回的标签、count 和 confidence 交给 DeepSeek，结合技能规则整理最终提示词，最后调用 NovelAI 出图并发回原会话。查询不是通用网页搜索，也不返回角色设定图；无匹配会明确标记，API 失败会回报原会话并停止生成，不伪造搜索成功。聊天消息不直接作为图片接口的提示词。

官方标签流程在 `[profiles.items.novelai.prompt]` 下使用 `tag_search_enabled=true`，每次查询超时 `tag_search_timeout_seconds=15`，每个词最多保留 `tag_search_result_limit=8` 条结果。查询词由 DeepSeek 根据本次相关上下文选择，凭据不传给 DeepSeek。接口适配位于 `oa_image_tag_search.py`。提示词整理和图片请求都有超时，拒绝或整理失败会停止并反馈，不回退到未整理原文。模型偶发拒绝时，标签查询按“无匹配”继续、提示词整理追加一次强化说明重试，两次都失败才终止。

网络瞬断时，仅对尚未建立 TCP/TLS 连接的失败重连一次。请求发出后的 HTTP 错误、超时、服务端断开或图片传输不完整不会自动重发；TLS 证书校验始终保留。群内错误会区分连接阶段及传输阶段，日志不包含 Token、完整提示词或图片数据。

DeepSeek 使用宿主 `model_config.toml` 中已配置的 `deepseek-v4-flash-fast`，不另存密钥。NovelAI 的 Persistent API Token 按普通套餐配置方式明文保存在 `config.toml` 的 `[profiles.items.novelai] api_key`，不写入日志。可在 WebUI 的 OAImage 套餐设置中填写，也可在本机 PowerShell 使用隐藏输入：

```powershell
pwsh -NoProfile -File tools/Set-NovelAIToken.ps1
```

追加 `-MakeDefault` 可同时把 `/生图` 的默认套餐切换到 NovelAI；未设置 Token 时保留原默认套餐。配置保存由 MaiBot 原生机制重新加载。

默认使用 `nai-diffusion-5-full`、832×1216、28 步、一次一张，所有会话共用一个 NovelAI 图片请求并发名额。同一会话已有任务时不会重复整理提示词或重复出图。`/生图r` 只发图，不唤醒模型、不启动生成后的额外对话；除 `general` / `sensitive` 的简短记录外不入上下文，成人评级不进。评分标签由 `generation.rating` 决定：默认 `auto` 尊重模型写出的 `rating:…`，只在命中成人解剖标签且漏写 rating 时补 `rating:explicit`，普通插画不会被打上 nsfw 评级；固定成 `general`/`sensitive`/`questionable`/`explicit` 才会强制统一尺度。任务在插件停止时取消，不跨重启恢复。参考图、图生图及角色库原图目前使用支持图片编辑的 OpenAI 兼容套餐。

### 已接入的 V5 提示词技能

[2332239652/novelai-assistant](https://github.com/2332239652/novelai-assistant) 固定到 **master@2e8601b9**（完整提交 `2e8601b998cfd3acc7c7f1cf88115e5626b17cca`），提示词技能需要安装者自行下载该锁定提交，并保留其 MIT 许可。配置项 `[profiles.items.novelai.prompt].skill_directory` 指向自行准备的目录，`SOURCE.json` 需记录来源和文件哈希；插件不执行第三方脚本，加载器验证锁定提交。

每次请求读取 `SKILL.md` 的 V5 写法章节，再以 `v5-field-corrections.md` 校正姿势、机位、角色栏、权重和文字规则。请求涉及画风时选读 `v5-confirmed-artist-strings.md`，点名配方优先只读该配方；涉及漫画时读取 `manga-page-playbook.md` 的版式和逐格规则；涉及已列出的游戏时补充 `game-character-tags.md`。完整安装不等于把所有表格和旧模型资料全部发送给模型。作者自用的第三方积分站、账户路径、默认喜好、计费承诺及交互流程不作为本机配置；画师串的验收记录仅代表原作者。来源校验和文件读取在后台线程完成，原会话资料单独作为用户消息输入。

DeepSeek 输出结构化的主提示词、最多 22 个角色栏、负面词和文字标记。客户端将角色栏分别写入 V5 请求，普通插画按角色栏统一人数，按实际需要设置角色坐标；质量预设由客户端追加一次。有图中文字时关闭自动质量后缀，避免其 `no text` 抑制文字。技能缺失、版本/哈希不符、两次整理都不合格时会停止并反馈。

## 自然语言参考图与任务结果

普通聊天可以先发图再说“按这张图画……”或把图片和需求一起发送，不要求一定引用。本次请求指定聊天图片作为编辑、服装、画风或构图来源时，`generate_image` 应填写 `reference_message_id`；多图消息用 `reference_image_index` 选择，从 0 开始。真实 QQ 消息编号按当前会话读取持久消息记录，因此上下文压缩后仍可读取原图；宿主短编号和工具媒体继续在当前上下文解析。读取失败会明确报错，不会悄悄替换成其他图片。

用户只点名已登记角色、没有要求修改某张聊天图片时，使用 `characters` 传入角色名、别名或 QQ 号，直接引用角色库保存的原图。不要因为摘要保留了旧设定图消息编号而重复绑定它；明确要求参考聊天图片时，仍可同时使用角色库与该图片。

未指定编号时，`use_recent_image=true` 使用当前会话最近的普通图片，默认有效期仍为 900 秒；表情包不占用这个参考。插件重载后可从同会话近期持久消息恢复原图，最多查询 200 条。活跃群中有多张不同参考图时应明确指定消息编号。

工具返回 `queued` 只代表排队，后续结果分别标为 `completed`、`failed` 或 `cancelled`。工具触发与普通 `/生图` 的结果会写入内部上下文，并在后续 Planner/Replyer 请求中同步（`/生图r` 除外，它在 `silent_context` 下完全不落上下文）；成功不等待图片描述完成，失败后允许用户明确重试。`trigger_continue_after_generation=false` 仍可关闭成功后的主动继续，但不会关闭内部结果记录；失败会唤醒 Planner，同一报错无需由模型重复发送。

## 基础开关

```toml
[plugin]
enabled = true
enable_action = true
```

`enabled` 控制整个插件是否启用。

`enable_action` 控制是否注册 `generate_image` Tool。关闭后，Bot/LLM 不会看到也不会调用这个生图工具；命令入口和收图 Hook 仍会注册，因此 `/生图`、`/画图`、`/图生图` 这类命令仍可使用。

## 生图套餐

所有上游请求配置都放在 `[profiles.items.<套餐ID>]` 下。一个套餐就是一组 provider、API 端点、密钥、模型参数、透传字段、显示名和介绍。

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
```

`default_profile` 是默认套餐。用户命令和 Tool 都没有指定套餐时，会使用这个套餐。

provider 类型、上游字段、透传参数、本地参考图和多套餐示例集中维护在 [PROVIDERS.md](PROVIDERS.md)。

## 给 Bot 看的套餐元信息

下面这些字段不会作为 API 请求参数发送给上游，而是暴露给 `generate_image` Tool，帮助 bot 判断该选哪个套餐，以及应该怎样写 prompt。

`display_name` 是套餐显示名。Tool 的 `profile` 参数可以填套餐 ID，也可以填这个显示名。

`description` 是套餐介绍，用来说明模型或端点的大致能力。

`supported_modes` 是套餐支持的模式。可选值通常是：

- `text_to_image`
- `image_to_image`

如果 Tool 选择了套餐不支持的模式，插件会拒绝执行，避免用错请求格式。

`prompt_style` 是提示词风格，例如：

- `danbooru_tags`：英文 tag 风格，适合二次元模型。多词概念应使用下划线，例如 `white_dress`、`blue_sky`、`looking_at_viewer`。
- `natural_language`：自然语言描述，适合通用图像模型。
- `edit_instruction`：图片编辑指令，适合图生图/编辑模型。

`use_cases` 是适用场景列表，bot 会根据用户需求匹配这些场景。

`requires_image_source` 表示这个套餐是否必须提供 `image_source`。OpenAI 兼容端点可以使用最近聊天图片、图片 URL，或在配置允许时读取本地图片路径。

`action_hint` 是直接写给 bot 的使用要求。这里可以描述 prompt 结构、禁忌、推荐写法、适合/不适合的任务。

## Bot 外形人设注入

插件支持在生成 Bot 本体、头像、立绘、自画像或外形图时，把预先配置好的 Bot 外貌参数注入到最终上游请求 prompt。

这个判断由 LLM 负责。插件不会自动根据用户文本猜测“是不是在画 Bot”。当 LLM 认为用户要画 Bot 本身时，需要在 `generate_image` Tool 里传：

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

## Tool 选择套餐

`generate_image` Tool 支持 `profile` 参数。这个值可以填套餐 ID，也可以填套餐 `display_name`。

插件注册 Tool 时会把当前配置中的套餐 ID、显示名和介绍追加到 Tool 说明里。这样 bot 在决定调用工具时可以根据说明选择合适套餐。

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

群聊发送 `@机器人 /生图 画面描述`，私聊直接发送 `/生图 画面描述`。`/画图`、`/draw`、`/image` 是同一入口的别名；修改图片可用 `/图生图` 或 `/改图`。

斜杠指令和画面描述之间需要空白，描述支持多行。只发送 `/生图` 时会返回用法，不会开始生成。

用户命令也可以临时指定套餐：

```text
/生图 --profile 精细竖图 1girl, solo, white dress, blue sky
/生图 --套餐 fast_square cat, meme, funny face
/图生图 --profile openai_edit --source "D:\Pictures\ref.png" 把背景改成夜晚街景
```

未指定套餐时使用默认套餐。

图生图的 provider 差异、参考图来源和本地目录限制见 [PROVIDERS.md](PROVIDERS.md)。

## GIF 动图

支持命令与自然语言生成 GIF，默认使用 `gpt-image-2.6-sunburst`。群聊命令前先真正 @机器人，私聊直接发送：

```text
/动图 一只小猫抬起爪子挥手，再放下，首尾平滑循环
/gif --frames 12 --frame-ms 150 小狐狸眨眼，镜头固定，循环播放
/生图 --gif 一盏小灯轻轻摇晃
/图生图 --gif 让图片里的角色眨眼并挥手，首尾平滑循环
```

发送静态参考图后，用 `/图生图 --gif` 将当前会话中最近收到的普通图片转成动图。已知图片地址时可用 `/gif --source <图片URL> 动作描述`。自然语言也可以说“让这张图里的角色眨眼，做成循环 GIF”，Bot 会用 `output_format="gif"`、`mode="image_to_image"` 和所选原图调用生图工具。

默认 9 帧、每帧 200 毫秒，一轮约 1.8 秒。帧数支持 2～16，每帧支持 20～2000 毫秒，间隔四舍五入到 10 毫秒。命令支持 `--frames` 和 `--frame-ms`，也可以写为 `--frame-count` 和 `--frame-duration-ms`。尺寸与帧数共同受上游总像素预算限制。

自然语言 Tool 中，普通静态图和参考图编辑的 `frame_count`、`frame_duration_ms` 应省略或传 `null`；即使模型填入动画默认值，静态分支也会排除这些字段，不会阻断生图或传给图片接口。GIF 未指定帧参数时使用下方配置，指定时仍严格校验范围。命令里使用 `--frames` 等选项仍需同时选择 `/动图`、`/gif` 或 `--gif`。

```toml
[animation]
profile = "openai_default"
frame_count = 9
frame_duration_ms = 200
```

GIF 默认使用 `animation.profile`。使用 `--profile` 可以指定有 GIF 预览权限的 GPT Image 2.6 Sunburst 或 Flare 套餐。

参考图使用 PNG、JPEG 或 WebP 静态图。工具可指定当前会话的参考消息与图片序号，也可在编辑模式使用已登记的 Bot 外形和角色库原图。明确指定的原图读取失败时，任务会报告缺图。

动图与普通生图共用异步任务、去重、取消、计费和结果反馈；返回的 GIF 字节直接交给 QQ 图片发送，不重编码为静态图。GIF 请求不发送 `stream`、`partial_images`、`output_compression` 或上游 `prompt_rewriter`；本插件自己的文字提示词整理仍保留，并描述动作、镜头和首尾循环。

## 角色知识私库参考图

开启 `[character_library].enabled` 后，`generate_image` 可以直接引用角色知识插件登记的原图：

```json
{
  "prompt": "DeepSeek 陪 GLM 在草地上玩耍，保留两人的不同外形",
  "characters": ["DeepSeek", "GLM"],
  "mode": "auto"
}
```

支持多人、角色别名、`character://DeepSeek?variant=chibi` 形式的 Q 版句柄。
没有传 `characters` 时，默认仅从本次画图需求匹配角色名；`personas` 可配置人物昵称到角色名的映射。
例如 `personas = { "软糯小鱼" = "DeepSeek" }`。显式角色列表优先于自动匹配。
角色不确定时可先调用角色知识插件的 `get_character_knowledge` 查询。

参考图来自角色知识私库 `characters.json` 的 `reference_images` 登记项；库中必须实际保存原图。
插件仅读取私库 `reference_images` 内已登记且校验通过的 PNG、JPEG、WebP，不需要打开任意本地路径读取。
每个角色选一张匹配变体的图，上传顺序与角色身份一起传入生图提示；Bot 本体、用户参考图和其他角色可同时引用。
尚未提供参考图的角色只按名字与已有文字设定绘制，不声称已使用该角色设定图。
多图需要 OpenAI 兼容套餐的 `generation.image_to_image_mode = "multipart_edit"`；不支持的套餐会明确报错。

```toml
[character_library]
enabled = false
auto_match = true
max_characters = 8
personas = {}
```

## 许可证

OAImage 使用 Apache-2.0 协议发布。


## OpenAI 画面整理（1.5.0）

OpenAI 默认套餐使用 `gpt-image-2.6-sunburst` 和 `quality=high`。独立整理默认使用 `rewrite_model = "planner"`，跟随宿主 Planner 的模型配置与原有选择策略，不固定为某个具体模型，参考本机 Codex imagegen 的提示词写法；补充参考 [openai-image-prompt-writer](https://github.com/gnipbao/openai-image-prompt-writer/blob/4edd0ab61a19f86d25b71377f10fdaaa1989cf21/SKILL.md)。规则在 `oa_image_openai_prompt.py`，来源和摘要在 `OPENAI_PROMPT_SOURCES.json`，不依赖远程 skill 或额外 CLI。

`profiles.items.openai_default.prompt` 独立开启 `rewrite_enabled` 与 `include_context`，优先于全局 `[prompt]`。默认只取当前会话最近半小时、最多 12 条消息和 5000 字符；保留精确文字、用户语言与指定画风。二次元按需求描述线条、脸眼设计、上色和配色，照片保持摄影风格，细化已有提示词时不添加无关元素。

附图清单来自本次真正上传的图片，按顺序区分用户素材、Bot 身份和角色身份。仅参考服装或画风的素材不替换目标人物；换装或改变动作时不锁死原图服装和姿势。文字整理模型接收清单与会话文字，不接收图片字节，不假装看过原图。

`profiles.items.openai_default.params.quality = "high"` 由现有透传机制分别写入 JSON 文生图和 multipart 图生图请求；模型、质量与尺寸不混进画面文字。整理拒绝、超时或返回无效结构时，会按任务失败反馈并停止图片 API 调用。NovelAI 的独立 V5 skill 与标签查询保持原配置。


`prompt.rewrite_model = "planner"` 会调用宿主 `task_name="planner"` 且不指定 `model_name`，以后调整 Planner 模型列表会自动跟随；填写具体模型名则保留显式选择行为。NovelAI 套餐原先单独配置的 `deepseek-v4-flash-fast` 不受 OpenAI 默认值调整影响。
