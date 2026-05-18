# OAImage 代码规范

这份规范适用于 OAImage。它以 MaiBot 0.12.2 插件系统为基础，参考内置插件的写法，同时补充本插件需要的安全、异步和配置约束。

## 适用边界

- 本插件是 MaiBot 0.12.2 社区插件，可以直接依赖 `src.plugin_system`、`src.common.logger`、`maim_message` 和插件系统 API。

## 基本要求

- 使用 Python 3.11+。
- 公共函数、组件 `execute` 方法、HTTP 客户端方法必须写参数和返回值类型。
- 复杂的运行时对象优先使用 `dataclass`，例如缓存项、任务元数据、结构化响应。
- 配置读取集中走 `_get_nested`、`_dict_config` 这类 helper，避免散落深层索引。
- 注释和 docstring 使用中文；协议字段、Action 参数名、配置键名保持 ASCII。
- 代码可以使用 MaiBot 现有的 `typing.List`、`typing.Dict` 风格，以便和 0.12.2 代码保持一致。

## 模块布局

- `plugin.py` 只保留插件注册、配置 schema、组件装配和入口导入兼容逻辑。
- `oa_image_config.py` 负责配置读取、profile 合并、planner 可见说明和 Bot 外形注入 prompt 处理。
- `oa_image_sources.py` 负责图片来源解析、base64 清洗、URL 下载、本地图片目录校验和参考图读取。
- `oa_image_runtime.py` 负责聊天维度的图片缓存、生成状态、锁、命令后台任务和上下文 prompt 拼接。
- `oa_image_clients.py` 负责不同 provider 的 HTTP 请求和响应图片提取。
- `oa_image_generation.py` 编排一次生图请求：选 profile、取参考图、注入外形、调用 client、发送图片。
- `oa_image_components.py` 只放 Command、Action 和 EventHandler。
- 新模块命名使用 `oa_image_` 前缀，避免插件目录被加入 `sys.path` 后与其它插件的通用模块名冲突。

## 插件组件

- 组件类继承 MaiBot 提供的 `BasePlugin`、`BaseCommand`、`BaseAction`、`BaseEventHandler`。
- 组件注册必须通过 `get_plugin_components()` 返回 `(ComponentInfo, Type)`。
- 可选组件必须由配置显式控制，例如 `[plugin].enable_action`。
- Action 暴露给 planner 的说明必须写清使用条件、参数含义和边界；不要依赖隐藏规则让 planner 猜。
- Command 默认不要打断普通回复流程，除非命令设计明确要求拦截。

## 配置

- 新增配置必须同时更新：
  - `config.toml`
  - `config_schema`
  - `README.md`
- 配置项默认值必须保守，尤其是本地文件读取、错误回显、自动 Action 注册等能力。
- API key 只允许放在本地配置中，不写入 README、测试输出、日志或提交说明。
- 示例 key 使用 `sk-...` 这类占位符。
- 多提供商能力使用 profile/套餐表达，不再新增旧式全局 `[api]` 配置。

## 异步任务

- 后台任务必须通过任务集合登记，并添加 `done_callback` 清理和记录异常。
- 命令触发的后台任务必须有停止路径；本插件通过 `EventType.ON_STOP` 取消命令任务。
- HTTP 请求必须设置 timeout。
- 任务异常不能泄露 API key、完整请求头或用户本地文件内容。
- 不在后台任务里使用裸 `print`。

## HTTP 与安全

- 对外 HTTP 默认使用 `Authorization: Bearer <api_key>`。
- 不记录完整请求头、Authorization、API key、图片 base64 或完整上游响应。
- 本地参考图默认关闭，开启后只能读取 `local_image_base_dir` 内的图片。
- 本地图片路径必须 `resolve()` 后校验仍在允许目录内部，防止 `..` 或绝对路径逃逸。
- 非 OpenAI JSON 端点的 `image_source` 通常只接受公网 URL；OpenAI 兼容 provider 才允许 URL、本地目录图片或最近聊天图片。

## Prompt 与 Action 语义

- profile 中给 planner 看的字段包括 `display_name`、`description`、`supported_modes`、`prompt_style`、`use_cases`、`action_hint`。
- 插件不自动猜测“是否画 Bot 本体”；Bot 外形注入只能由 Action 显式传 `use_bot_appearance=true` 触发。
- `prompt_style = "danbooru_tags"` 时，注入文本和示例应使用英文 tag、逗号分隔、多词下划线。
- `prompt_style = "natural_language"` 时，使用完整自然语言描述。

## 日志

- 使用 `src.common.logger.get_logger`。
- 正常流程尽量用 `debug`，失败但可恢复用 `warning`，任务或请求失败用 `error`。
- 日志可以包含错误摘要，但不要包含密钥、完整图片 base64、完整 prompt 历史上下文或本地敏感路径之外的文件内容。

## 文档

- README 面向用户，说明配置、用法、边界和常见示例。
- CODE_STYLE 面向维护者，说明开发规范和不可破坏的行为约束。
- 修改配置行为时，应同步更新 README 对应章节。

## 验证命令

至少运行：

```powershell
Get-ChildItem -Filter *.py | ForEach-Object { python -m py_compile $_.FullName }
python -m ruff check .
```

涉及配置时额外运行 TOML 解析：

```powershell
@'
import tomllib
from pathlib import Path
with Path("config.toml").open("rb") as f:
    tomllib.load(f)
print("ok")
'@ | python -
```

涉及 Action 注册、Bot 外形注入、本地图片读取、profile 选择时，应补充小型捕获测试，避免真实发送消息或真实请求生图上游。
