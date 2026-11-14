# 变更记录

## 0.5.2（正式发布）

上架审查整改版：只调整运行时日志来源，无功能、配置或协议行为变更。

### 问题修复

- **日志记录器统一为宿主 logger**：`agy_provider/onboarding.py`、`sessions.py`、`telemetry.py` 不再使用 Python 内置 `logging`，也不保留 `ImportError` 回退分支，一律 `from astrbot.api import logger`；`host_compat` 继续由调用方（`main.py`）显式注入宿主 logger，未注入直接 `TypeError`。`telemetry._emit_json_line` 由 `getattr(logger, "info", ...)` 兜底改为直接调用宿主 `logger.info`，单参数字符串调用，行为等价。
- **离线测试补齐宿主 logger 夹具**：新增 `tests/astrbot_stub.py`，在无 AstrBot 环境注入受 `spec` 约束的 logger 桩；原 `assertLogs` 断言改为 `patch.object`，保留调用次数、参数个数与“日志不含答案正文”校验。

### 升级说明

- 覆盖升级即可，无配置迁移；`probe.py`、`mcp_bridge.py` 等独立入口仍不依赖宿主 logger，可脱离 AstrBot 运行。
- 依赖宿主 logger 的模块（`onboarding` / `sessions` / `telemetry`）现在必须在 AstrBot 进程内导入；脱离宿主导入会直接 `ModuleNotFoundError: astrbot`，与 `main.py`、`provider.py` 原有行为一致。

### 验证

- 156 项 Python 离线回归（155 通过、1 项 Windows 专属跳过）与前端交互测试通过；Ubuntu / Windows × Python 3.11 / 3.12 四组 CI 全绿。

## 0.5.1（正式发布）

本版是从 **v0.4.3 升级**时需要阅读的完整更新说明：合并了原计划的 **0.5.0 功能线**与 **0.4.4 修复线**，这两个中间版本均未单独发布，直接升级至 **v0.5.1** 即可。

### 新增功能

- **更稳妥的会话复用**：区分历史追加、裁剪和改写，减少本地历史窗口变化引起的不必要重建。新增 `session_reuse_mode`（`strict` / `structured`），默认沿用保守的 `strict` 模式。
- **聊天 / 工具调用分流**：新增 `agy_tools_mode`（`inherit` / `chat` / `tools`）。`chat` 实例不向 AGY 下发工具；`tools` 实例仍受全局 `enable_tools` 与宿主权限约束。首次设置向导推荐纯聊天使用 `chat`。
- **消耗与缓存统计**：新增管理员命令 `/agy cost`，只读汇总最近最多 100 轮的 Token 使用、缓存、会话复用率、失败消耗及重建原因，不额外发起模型请求。可选开启 `cost_log_enabled` 输出脱敏轮次日志，默认关闭。
- **失败抑制与配额冷却**：重复 `EMPTY` 失败默认在 30 秒内抑制；`QUOTA` 错误默认冷却 60 秒，可参考上游 `retry_after`（最长 300 秒），冷却期后仅允许一个 half-open 探测请求。只记录失败状态，不缓存成功回答，不自动重放工具操作。

### 问题修复

- **#4 模型保存后不显示（部分修复）**：自动创建并持久化 `provider_sources`，为保存的模型绑定 `provider_source_id`，解决 AstrBot 4.29 模型管理页找不到模型的问题。**连通性测试耗时较长尚未解决**，仍在 #4 跟踪。
- **#5 配置填错后无法恢复**：启动时修复旧配置里的越界整数并保存（如 `tool_max_calls`、`idle_ttl`）；自动处理 `max_concurrent > max_sessions` 冲突。WebUI 数值项增加范围滑杆及提示；运行中的即时校验仍会拒绝非法输入。
- **#6 微信 JPEG 被误判无效**：结构化识别 JPEG 的合法结束标记（EOI），裁掉结束标记后的附加数据；真正缺失 EOI 的截断图片继续拒绝。
- **#7 上游重试告警泄漏到聊天**：过滤单行、跨行 URL 续行、跨 chunk 的 `⚠ API error (attempt N)` 及流中断提示；重试成功只展示正常正文，重试耗尽按 `NETWORK` / `QUOTA` 等真实类别交还 AstrBot 处理；终止标记不再覆盖已经识别的错误码。

### 升级说明与已知限制

- 插件要求 **AstrBot >=4.29.4,<4.30**、**Python 3.11+**，还需在 AstrBot 实际运行环境中安装并登录官方 `agy` CLI。
- 原有默认值 `enable_tools=false`、`history_bytes=160000` 不变；新建实例的 `agy_tools_mode` 缺省为 `inherit`。手动选 `chat` 模式后，该实例不会执行工具调用。
- `structured` 复用依赖宿主提供可信的 `agy_context_parts`；目前 AstrBot 未提供该信息时会自动退回 `strict`，不是开启开关就一定减少 Token。
- **#4 响应慢问题仍开放**：连通性测试会进行真实生成，请求可能花费 40–80 秒；本版没有缩短这一流程。

### 验证

- **156 项 Python 离线回归测试**与 **12 项前端交互测试**通过；修正 Windows Fake CLI 的 UTF-8 编码问题后，Windows / Ubuntu × Python 3.11 / 3.12 四组 CI 均通过。
- 在真实 **AstrBot 4.29.4 + AGY CLI 1.5.8** 部署中执行修复相关的集成验收；使用受控网络断流验证重试告警不泄漏、错误能正确分类。
- 详细实现与测试样例保留在下方 **0.5.0 / 0.4.4 开发分支记录**，供维护者查阅.

## 0.5.0（开发分支，未单独发布；已并入 0.5.1）

- **A 安全会话复用**：
  - 新增 `session_reuse_mode`（`strict` | `structured`，默认 `strict`），保持 `history_bytes: 160000`、`enable_tools: false` 向后兼容默认值不变。
  - 引入 `PromptContext` / `prepare_context`（`stable_system` 与 `transient_data` 分层）；仅当配置为 `structured` 且宿主提供经校验的可信 `agy_context_parts` 时拆分瞬态块（以 `<agy_context_data>` 注入用户轮次消息），未提供或校验失败时自动退回 `strict` 严格整段比较，支持 `clear` 清空与 `retract` 强制重建。
  - 新增双账本（`host_expected` 与 `sent_window` 分离）、`compare_history`（`exact` / `host_trim` / `rewrite` / `append_gap` / `unknown`）与 `decide_reuse` / `classify_rebuild` 细粒度重建原因判定（含淘汰/退出 tombstone 记录）；工具续轮冻结 `source_revision` 与 `transient_hash`；`ToolSnapshot` 在发布与指纹计算前按 wire 名称排序。
- **C 聊天 / 工具分流**：
  - 支持提供商配置 `custom_extra_body.agy_tools_mode`（`inherit` | `chat` | `tools`，默认 `inherit`）与 WebUI「首次设置」实例角色选择（默认推荐 `chat`）。
  - `chat` 模式绝不下发工具、走精简自定义代理（`--agent astrbot-chat`，`tools: []` + `excludeDefaultComponents: true`），并拒绝工具续轮（`TOOLS_DISABLED`）；`tools` 模式仍严格受插件总闸 `enable_tools` 与 `tool_choice=none` 约束，不可越权绕过。
- **D 可观测性与 `/agy cost`**：
  - 新增 `agy_provider/telemetry.py`：`cost_log_enabled`（默认 `false`，避免刷日志）开启后，每个完整 ACP 轮在 `INFO` 级别输出一行脱敏 JSON（`event="agy.turn"`），记录 `input_other` / `input_cached` / `output`、`reused`、`rebuild_reasons`、`primary_reason`、`history_relation`、`usage_status`、`usage_source`、`cache_fields_present` 及字节指标；失败或取消前也会尽力保留最后一次观测到的 usage；无 usage 时输出 `null` 与 `unavailable`；使用 HMAC 对会话/底层会话标识脱敏，绝不输出提示词、正文、用户 ID 或凭据。
  - 新增只读管理员命令 `/agy cost`（不发起任何模型/探针请求），按实例汇总最近 100 轮缓存命中、复用率、失败消耗、不可用占比与主要重建原因，以及进程池水位和有效配置。
- **E 失败防重放与短期冷却**：
  - 新增 `agy_provider/failure_guard.py`：同会话/请求指纹的 `EMPTY` 响应默认 `empty_dedupe_ttl=30` 秒内短期抑制；按 `(provider_id, model)` 维度实施 `QUOTA` 冷却（默认 `quota_cooldown=60` 秒，支持从上游 `retry_after` 结构化提取并封顶 `300` 秒）及单一并发 half-open 探针恢复；只缓存失败标记（从不缓存成功正文），工具续轮与缺少身份标识的匿名请求不参与跨请求误抑制。

## 0.4.4（修复分支，未单独发布；已并入 0.5.1）

- 修复历史配置中的非法数值导致插件无法加载、且 WebUI 改回合法值仍无法保存/重载的问题（#5）：`Settings.from_dict` 新增 `on_repair` 启动修复模式，无效整数（如 `tool_max_calls>128`、`idle_ttl>3600`）自动恢复为默认值，`max_concurrent>max_sessions` 冲突自动钳制，修正值写盘并告警；探测与向导的即时校验保持严格拒绝。`_conf_schema.json` 为全部数值项补充合法范围滑杆与提示。
- 修复微信等客户端 JPEG 图片因 EOI 之后存在附加元数据被整体判为「图片无效」的问题（#6）：`_jpeg` 改为结构化遍历标记与熵流，在首个合法 EOI 处截止并裁剪尾部附加字节（包括附加数据中再嵌 FFD9 的情况）；真截断（缺 EOI）仍拒绝并报 `IMAGE_EOI`。
- 修复上游 CLI 重试告警「⚠ API error (attempt N): …」被当作助手正文发给用户的问题（#7）：新增 `RetryNoticeGuard` 流式过滤，跨 chunk 拆分的重试行不再泄漏分片；真实告警可能折行成多行（CLI 错误转储的 `"https://…"` URL 续行）或结尾为裸 `: EOF`，续行按行形状识别并随头行一并吞入诊断，`unexpected EOF` 与裸 `EOF` 尾变体均归类 `NETWORK`；新增过滤流中断续流提示「⚠ The stream was interrupted. Please continue …」并归类 `NETWORK`（CLI 状态文本全走 stdout 消息流、stderr 为空，为实测结论）；重试后带正常正文时只保留正文，整轮仅剩重试时按真实错误分类（`QUOTA`/`NETWORK` 等）抛出并触发宿主 fallback。告警形态基于真实 CLI v1.5.8 受控断流实测与 #7 野外样本对齐。
- 修复已保存模型不出现在「模型提供商」页的问题（#4，部分）：插件启动与向导保存时自动创建并持久化 `provider_sources` 来源、为模型绑定 `provider_source_id`（AstrBot 4.29 模型页按来源分组过滤，缺失绑定即不显示）；旧版宿主缺相应接口时跳过绑定并告警，不影响向导保存。连通性测试耗时较长（40–80 秒级）未在本轮处理，测试按钮仍执行真实生成请求。
- 测试：新增/扩充 37 项离线用例（配置自愈、JPEG 结构与裁剪、重试行全切分点与多行续行过滤、真实告警逐字夹具、流中断提示过滤、来源绑定回滚幂等与旧宿主降级），全量 133 项通过（1 项平台相关跳过）；`node --test tests/setup.test.mjs` 11/11。

## 0.4.3

- 修复上游 Antigravity CLI 容量不足（`503 UNAVAILABLE: No capacity available`）时，CLI 的重试告警被当作助手正文发给用户的问题：新增 `strip_upstream_capacity_notices`，过滤 `⚠ API error (attempt N)` 告警行、保留真实回答。
- 若整段只有告警、没有正文，改为抛出 `QUOTA` 错误交给 AstrBot 触发 fallback（如 `glm-5.2`），不再把告警原文发到聊天；日志只记录过滤条数，不记录正文或凭据。
- 新增 `tests/test_upstream_notice_filter.py`（告警+正文、纯告警、正常含 error 字样、空行/缩进变体、会话与 provider 路径），并保持原有离线测试全通过（96 项）。

## 0.4.2

- 重构插件前端面板 UI（`views/setup`）：移除大号衬线欢迎标语与冗余说明文字，改为现代极简黑白双栏卡片布局（移动端自适应单栏），自带浅色/深色主题一键切换并兼容宿主主题同步。
- 优化侧边栏入口命名：视图标题由「首次设置」调整为「Antigravity ACP」（`.astrbot-plugin/i18n/zh-CN.json`、`en-US.json`），避免在宿主左侧导航栏显示突兀的「首次设置」字样。
- 精简面板交互提示与折叠抽屉，完整保留无生成检测、官方 CLI 登录、动态思考档位探测、模型持久化与错误分类诊断语义。

## 0.4.1

- 改进 CLI 发现提示：“未在当前运行环境找到”不再暗示未安装；首次设置页显示默认搜索范围或显式 command 模式，强调 Docker 容器内路径。
- 新增 `COMMAND_NOT_FOUND`，区分显式路径无效与默认搜索未找到；显式配置仍不自动回退到其他 CLI。
- 新增 `STARTUP_EXIT` 与 `CLI_WRAPPER`：区分 ACP 握手前进程退出、旧包装脚本拒绝隔离目录，以及握手后的管道故障。只在观察到进程退出时记录退出码，支持 stdout EOF 与 stdin 关闭的竞态路径。
- 探测报告和页面补充失败阶段与退出码；识别已知脚本拒绝信息时保持脱敏，明确的 RPC 认证/协议错误不被 stderr 覆盖，成功请求不因背景告警误报失败。
- CLI 文件存在但脚本解释器或动态加载器缺失时报告 `LAUNCH`；版本/能力查询失败保留脱敏类别和退出码。
- 增补离线回归，并在现有 Linux Docker 部署验证旧脚本分类与原生 CLI 的无生成 ACP 检测。完整范围见 `TEST_REPORT_0.4.1.md`。

## 0.4.0

- 新增原生插件视图「首次设置」，串联环境检测、官方 CLI 登录、真实模型与思考档位选择、提供商保存和会话切换提示。
- 默认后台执行无生成预检；新增 `setup_auto_check` 开关、管理员 `/agy setup` 与 `/agy use <provider_id>`，`/agy doctor` 与向导共用检测服务和并发限制。
- 官方登录由 `agy acp --login` 完成：仅在管理页面展示授权链接与接收授权码，支持取消、超时和登录后 ACP 验证；不读取或自行刷新 OAuth token。
- 补充 Windows 原生 CLI 搜索路径；缺少 token 文件时允许 CLI 尝试系统钥匙串，现有不安全文件或链接失败仍明确报告 `AUTH_BRIDGE`。
- 保存前重新校验模型及档位，使用 AstrBot 接口创建、复用和启用提供商，可选设为默认；配置保存在宿主，重载后恢复显示。
- 识别跨流式分片的网络异常和高负载告警，分别报告 `NETWORK` / `RATE_LIMIT`；不新增自动重试或工具重放。
- 按交接文档更新 README，补充离线回归与 Linux/Windows、Python 3.11/3.12 CI；测试范围与真实宿主待验项见 `TEST_REPORT_0.4.0.md`。
- 修复宿主持久化失败后重试复用实例却不重新写盘的问题；复用保存通过宿主更新接口重新持久化并重载。
- 修复认证桥链接创建阶段的文件缺失错误被误当成原生认证回退的问题；仅官方源文件或目录缺失允许回退，链接失败继续阻断。

## 0.3.3

- 插件 ID 改为 `sl251/astrbot_plugin_antigravity_acp`，同步更新注册名、会话数据目录和安装路径。
- README 补充旧版配置迁移步骤。新旧插件共用 `agy_acp` 模型提供商类型，安装新版前请先停用旧插件。

## 0.3.2

- 移除 runner 兼容层的 Python 内置日志回退，统一使用插件入口传入的 AstrBot 日志记录器，符合插件市场的日志要求。

## 0.3.1

- 项目更名为 AstrBot Antigravity ACP，同步更新插件显示名和安装链接，保留原有插件 ID。

## 0.3.0

- 面向插件市场精简仓库：移除离线单元测试、内部开发文档与开发脚本，CI 改为源码语法检查；运行时代码与配置项与 0.2.0 完全一致。
- 协议合规完善：LICENSE 补充上游 [lili-luo/dsh-plugin-bundle](https://github.com/lili-luo/dsh-plugin-bundle) 版权行，README 新增「许可与致谢」章节。
- 元数据与描述统一改为 0.3.0（metadata、插件注册、ACP clientInfo/serverInfo）。

## 0.2.0

- 增加默认启用的文件认证桥：隔离会话只链接同用户的官方 token 文件；启动前检查存在性、大小、归属、父目录权限和符号链接边界。`auth_bridge=false` 恢复纯隔离行为。
- 增加插件级 `proxy_mode`（inherit/custom/direct）与 `proxy_url`，代理环境贯穿版本、能力、自检、目录和聊天的 AGY 子进程启动；不修改全局环境，不影响图片下载器。
- 增加 PROXY_AUTH、PROXY、NETWORK、TLS 错误分类，保留 AUTH、限流和额度分类；诊断持续排空 stderr，只保留有界的白名单类别与阶段，避免原始输出泄露。
- 增加管理员 `/agy doctor` 无生成自检，读取当前面板配置，设置总时限和并发保护；自检与 Runtime 使用同一插件数据根。
- 改善 CLI 未找到、不可执行和能力不足的反馈；显式错误路径不会静默回退。
- 重写市场用户安装与模型配置引导，补充认证桥、代理和 Docker 说明；插件初始化提示新增 `agy_acp` 来源。
- 统一 metadata、插件注册和 ACP clientInfo 为 0.2.0；补充市场短描述及标签。宿主范围收窄为 `>=4.29.4,<4.30`：兼容层包装内部 runner，只有 4.29.4 的真实宿主证据，不承诺未验证版本。
- 增加 Python 3.11 离线回归、Ruff 与配置/schema 默认值一致性 CI；运行时仍无新增第三方依赖。

已知限制：**凭据自然刷新与并发刷新行为未验证**，建议同一账号避免 CLI 与插件并发高频使用；CLI 原子替换符号链接后的持久化仍需真实观察。工具和图片默认关闭，Windows/macOS 认证及完整市场/WebUI 链路仍需单独验收。此次离线验证不发送真实模型请求。

## 0.1.0

- 基线提供官方 AGY CLI 的 ACP stdio Gemini provider、模型目录、文本/流式/多轮、思考档位、隔离会话与有界进程池。
- 提供默认关闭的宿主工具桥、PNG/JPEG 图片输入，以及仅作用于本 provider 的原生 runner 兼容层。
- 提供取消、重载、进程树回收、错误脱敏及 stdlib fake ACP 离线回归；市场改造起点为 151 项测试。
- 认证路径适配原由部署侧完成，尚无通用认证桥与插件级代理配置。
