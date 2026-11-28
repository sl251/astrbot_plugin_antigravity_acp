# Agent 排障 SOP

先记录部署 SHA、插件/宿主/Python/CLI 版本、服务用户和容器内路径。管理员可执行 `/agy doctor`，保留脱敏 `stage`、`error_code`、CLI `exit_code`、是否已发 prompt、可见输出、工具交接/结果与 fallback 次数。未知字段保留未知。沿发现/启动 → 认证 → 握手/目录 → 保存 → 生成 → 工具审批/执行/回填定位阶段。输出或工具副作用之后停止自动重试，详细边界见[架构](architecture.md#会话历史与防重放)。

不以重登、重装、清空目录或放开内置能力作为通用修复。

## 故障定位索引

源码与测试列给出首读入口，命令见[定向回归](testing.md#定向回归)。

| 症状 / 错误码 | 源码 | 测试 | 排障 |
| --- | --- | --- | --- |
| 找不到或启动失败 `NOT_INSTALLED` / `COMMAND_NOT_FOUND` / `PERMISSION` / `LAUNCH` / `STARTUP_EXIT` / `CLI_WRAPPER` | [process.py](../agy_provider/process.py)、[paths.py](../agy_provider/paths.py)、[windows_job.py](../agy_provider/windows_job.py) | [启动与清理](../tests/test_process_hardening.py)、[路径](../tests/test_paths.py)、[启动诊断](../tests/test_diagnostics.py)、[ACP 握手与包装脚本](../tests/test_acp.py) | [同用户检测](#无生成检测与实际服务对照) |
| `AUTH` / `AUTH_BRIDGE`、登录后仍无模型 | [auth.py](../agy_provider/auth.py)、[login.py](../agy_provider/login.py)、[probe.py](../agy_provider/probe.py) | [认证](../tests/test_auth_config.py)、[登录](../tests/test_login.py)、[自检](../tests/test_diagnostics.py) | [启动与登录](#启动登录与模型设置) |
| `MODEL` / `NO_MODELS`、默认路由异常、漂移、`REFERENCE_BLOCKED`、保存部分成功 | [onboarding.py](../agy_provider/onboarding.py)、[routing.py](../agy_provider/routing.py)、[main.py](../main.py) | [路由](../tests/test_routing.py)、[设置](../tests/test_onboarding.py)、[初始化](../tests/test_main_init.py)、[DOM](../tests/setup.test.mjs) | [模型设置](#启动登录与模型设置) |
| `RATE_LIMIT` / `QUOTA`、重试告警出现在正文、#14/#19 | [notices.py](../agy_provider/notices.py)、[errors.py](../agy_provider/errors.py)、[failure_guard.py](../agy_provider/failure_guard.py)、[system_prompt.py](../agy_provider/system_prompt.py) | [告警](../tests/test_notices.py)、[候选](../tests/test_content_429.py)、[宿主能力声明](../tests/test_system_prompt.py)、[Safe Mode](../tests/test_safe_mode_rule.py)、[冷却](../tests/test_failure_guard.py) | [429](#rate_limit--quota-与-14) |
| `EMPTY` / `TOOL_PERMISSION`、合法 MCP 被取消 | [transport.py](../agy_provider/transport.py) `_permission_option`、[tool_turn.py](../agy_provider/tool_turn.py) | [一次性权限](../tests/test_mcp_permissions.py)、[拒绝归因](../tests/test_permission_attribution.py)、[工具链](../tests/test_tool_pipeline.py) | [权限及空回复](#empty权限拒绝与-12) |
| `TOOL_ARGUMENTS` / `TOOL_SCHEMA`，合法数组被拒 | [tools.py](../agy_provider/tools.py) `ToolSnapshot` / `ToolBroker.dispatch` | [schema](../tests/test_tool_schemas.py) | [原始契约](#tool_arguments-与-13) |
| `TOOLS_DISABLED` / `TOOL_CHOICE`、原生读取不可用或实验搜索被中止 | [builtin_tools.py](../agy_provider/builtin_tools.py)、[provider.py](../agy_provider/provider.py)、[sessions.py](../agy_provider/sessions.py) | [策略](../tests/test_issue38_permissions.py)、[只读/无 MCP](../tests/test_issue41_readonly_builtins.py)、[DOM](../tests/setup.test.mjs) | [工具检查](#工具图片与监控) |
| `TOOL_TIMEOUT` / `TOOL_STALE`，长工具返回后无关回复或尝试 fallback | [tools.py](../agy_provider/tools.py)、[sessions.py](../agy_provider/sessions.py) `TimedOutTool`、[tool_turn.py](../agy_provider/tool_turn.py) `chat_events` | [迟到结果/收尾](../tests/test_tool_pipeline.py)、[防重放](../tests/test_host_replay.py) | [超时回填](#宿主工具执行超时后的回填) |
| `TOOL_CONTEXT` / `TOOL_HISTORY`、工具图片续轮、步数用尽或交接后发生重放 | [host_compat.py](../agy_provider/host_compat.py) `max_steps_notice`、[provider.py](../agy_provider/provider.py)、[messages.py](../agy_provider/messages.py) `tool_continuation_tail`、[tool_turn.py](../agy_provider/tool_turn.py) | [图片回填与收尾](../tests/test_tool_pipeline.py)、[runner fallback](../tests/test_host_replay.py)、[副作用](../tests/test_b06_side_effects.py) | [工具图片](#工具图片与监控)、[步数收尾](#步数耗尽后的收尾) |
| `IMAGE_PATH 图片不在已授权媒体目录内`，带附加图片的消息整轮 fallback | [media.py](../agy_provider/media.py) `_local` 与 `media_roots` 校验 | [test_auth_config.py](../tests/test_auth_config.py)、[test_b07_b08_regressions.py](../tests/test_b07_b08_regressions.py) | — |

| `history_rewrite` 频发、图片重复或丢失 | [messages.py](../agy_provider/messages.py)、[sessions.py](../agy_provider/sessions.py)、[media.py](../agy_provider/media.py) | [会话池](../tests/test_sessions.py)、[模式](../tests/test_tools_mode.py)、[历史图片](../tests/test_prompt_images.py)、[媒体](../tests/test_media.py) | [会话复用](#会话复用与历史重写)、[图片检查](#工具图片与监控) |
| 额度为空、过期或登录中 `LOGIN_BUSY` | [quota.py](../agy_provider/quota.py)、[onboarding.py](../agy_provider/onboarding.py)、[app.js](../views/setup/app.js) | [缓存与环境](../tests/test_issue38_quota.py)、[端点与 CSRF](../tests/test_onboarding.py)、[页面刷新](../tests/setup.test.mjs) | [额度与统计](#额度与统计) |
| `STORAGE_UNAVAILABLE`、「仅内存统计」、监控 401/403 | [monitoring.py](../agy_provider/monitoring.py)、[daily_usage.py](../agy_provider/daily_usage.py)、[turn_history.py](../agy_provider/turn_history.py)、[telemetry.py](../agy_provider/telemetry.py) | [监控](../tests/test_monitoring.py)、[每日](../tests/test_issue38_daily.py)、[明细](../tests/test_turn_history.py)、[失败记录](../tests/test_failure_events.py) | [额度与统计](#额度与统计) |
| 私聊 `/agy` 落入 LLM | [main.py](../main.py)、[onboarding.py](../agy_provider/onboarding.py) `command_scope_warning` | [命令路由](../tests/test_commands.py) | [可用插件](#私聊中-agy-cost-被当成对话) |

## 无生成检测与实际服务对照

在 AstrBot 实际服务用户和环境、插件根目录运行以下命令。替换 CLI 绝对路径，Windows 用 `agy.exe`。

```sh
/absolute/path/to/agy --version
/absolute/path/to/agy acp --help
python -m agy_provider.probe --command /absolute/path/to/agy --timeout 60
```

独立 probe 继承终端代理，附加参数仅 `--data-root`。终端通过而面板失败时，对比服务的用户/HOME、command、代理与数据根。probe 退出 0 表示检查通过，2 为阻塞；报告里的 `exit_code` 来自 CLI，仍须结合握手/目录阶段。

`NETWORK`、`TLS`、`TIMEOUT` 先查阶段、出口/DNS、证书、代理与时限；`PROXY_AUTH` / `PROXY` 查 407、代理协议和容器可达性。`TRANSPORT` / `PROTOCOL` 查 JSON-RPC、EOF 和实际退出码。显式 RPC 错误不被泛化 stderr 覆盖，只有同阶段已识别的 stderr 信号才可辅助细化通用错误。通用 `-32000` 或 403 无法单独判断登录或封禁。错误映射见 [errors.py](../agy_provider/errors.py)。

## RATE_LIMIT / QUOTA 与 #14

1. 区分 RPC 错误、CLI 告警与答案内容。查看 `request_sent`、`usage_status`、`has_output`、`origin_turn_id` 和 `retry_after`。后两项可指向本地冷却或去重。只有明确耗尽信号才能支持额度归因，`e.g. check quota` 等示例文案不证明余额为零。
2. #14 查宿主本地能力声明是否属于定点过滤；#19 查 4.29.4 Safe Mode 第 4 条是否在完整规则列表内等义改写。用合成 system 验证，避免复制完整真实提示词。`content_429_candidate` 可随已有输出出现，归因仍未知。参见[定点兼容](compatibility.md)。
3. 冷却默认 60 秒、最长 300 秒，期满允许单个 half-open 探测；EMPTY 默认去重 30 秒。确认无输出或副作用且在授权范围后，可手工进行单次同模型/档位、同环境、无工具的最小对照。保持防重放、冷却和原 system 语义。

## EMPTY、权限拒绝与 #12

按单轮追踪工具观察、ACP permission、MCP `tools/call`、宿主审批/执行、结果回填和最终正文。交接后暂时无正文是中间态。上游明确错误优先；无上游错误且本轮权限拒绝导致无正文时看 `TOOL_PERMISSION`，其余 `EMPTY` 再核对终态。计费输出 Token 无法代替正文证据。

合法 MCP 被拒时核对活跃 prompt、名称/ID/状态、唯一 `allow_once` 与终态时序。终态后、过期、重复、跨会话、未知工具及永久许可（`allow_always`）继续拒绝。已许可却无 `tools/call` 查 CLI/relay；到宿主后查原始 schema、预算、审批和回填。本轮尚无正文、工具执行及交接时，已识别的内置拒止可由 Provider 本地提示结束；只有搜索通知文案提示请求可能已发出。见[工具策略](api.md#工具策略)。

## TOOL_ARGUMENTS 与 #13

用合成工具对比 `to_host_schema` 和模型 `inputSchema`，定位错误发生在模型、桥校验还是宿主执行。验证缺失 `items`、显式 string、空/嵌套数组以及源对象不变。两份 schema 变化均应更新指纹。参见[权限与 schema](architecture.md#mcp-的两层权限与两份-schema)。

## 启动、登录与模型设置

- 核对同用户原生 CLI 入口、退出码、挂载、文件元数据和隔离目录。已有不安全认证文件须修复元数据。保留数据根、CLI HOME 与登录态。通过 WebUI 或官方 `agy acp --login`，不支持时按 CLI 提示交互登录。WebUI 只收 1–4096 字符的单行授权码，属于当前登录发起者；完整回调 URL 不能提交。登录后重做 ACP 目录检查。
- 目录或档位不符时显式刷新目录，按缓存重新选择。换模型且保留已有 ID 用原位编辑。先读后台终态，再按 `result` 和 `action_error` 判断部分成功。宿主自带模型连通测试会生成一次，插件环境检测及模型保存检查无生成。
- 路由异常分别核对配置主项、实际解析、服务观测、当前会话偏好。看到「需同步」先查看 `config_errors`，使用单项或批量修复。停用/删除先看影响预览，确认替代默认或显式清空后再核对实际路由。全库会话及辅助引用需独立检查。见[模型管理 API](api.md#模型管理与路由)。

## 会话复用与历史重写

同会话、同模型/档位、人格和工具连续发送两条短文本，核对 `reused`、`history_relation`、`primary_reason`。反复 `history_rewrite` 时检查真实历史编辑、摘要替换和 `_no_save` 临时文本匹配；`no_identity` 先查宿主会话身份。上游缓存率和 ACP 复用率各有独立口径。见[复用机制](architecture.md#会话历史与防重放)。

## 步数耗尽后的收尾

核对告警属于主 runner 还是子 Agent，再检查是否到达宿主强制收尾、`func_tool` 是否已清空、是否还有属于该运行的保留 prompt。受支持的收尾使用闭合文本历史生成，MCP 与原生工具均关闭。只有同运行、活会话且匹配已完成收尾的一个闭合交换可以随后纳入，旧 `TOOL_STALE` 或一般 `TOOL_CONTEXT` 不能靠放行所有空工具集请求解决。见[收尾契约](architecture.md#无工具收尾与超时恢复)。

## 宿主工具执行超时后的回填

先关联脱敏交接记录、执行时限（默认 120 秒）、宿主完成时间和会话存活，区分工具执行超时、审批超时、取消与重载。宿主可能仍在执行，禁止重发工具。允许的迟到结果由宿主带回，同一 signal 和历史等条件通过后生成一次无工具总结。核对已识别的超时失效/重复返回 `TOOL_TIMEOUT`，重载后无超时证据的旧结果返回 `TOOL_STALE`。恢复失败后也不得自动 retry/fallback。若日志记录禁止 fallback 却看到切换，核对宿主版本及真实 runner 的实际备用 Provider 调用数。见[完整校验条件](architecture.md#无工具收尾与超时恢复)、[回归](testing.md#宿主源码离线检查)。

## 工具、图片与监控

- 工具总闸、实例模式、宿主 `tool_use` 与 `tool_choice` 分别核对。MCP 另查 ToolSet、身份和取消信号；原生工具可不带 MCP，但仍按版本、策略、当前会话和预算核验。保存权限后重载并读 `effective`。搜索关闭时的取消仅是事后观察。见[工具策略](api.md#工具策略)。
- 图片核对总闸、宿主 `image`、CLI 能力及文件格式。带图续轮遇到 `TOOL_HISTORY` 时，检查闭合交换是否位于尾部、图片标注与缓存段落是否逐一匹配，以及宿主预览是否截掉了缓存段落。报告仅收脱敏数量、来源类型和错误码。
- iframe 复制在用户手势内尝试 Clipboard API，再同步 `execCommand("copy")`，失败则选中文本。HTTP 或 sandbox 下 Clipboard API 可能缺失，弹窗可能被宿主拦截。轮询勿重写未变化的输入值，避免折叠选区。

### 额度与统计

- 额度先查 `fetched_at`、`stale`、`error.code` 及当前登录环境。登录中等待结束；暂态失败等冷却或手动刷新；认证错误走官方登录。重置倒计时只显示已采样结果。见[额度接口](api.md#官方-cli-额度)。
- 「仅内存统计」核对实例身份标记、数据目录和 SQLite 文件/旁路文件的权限、类型及锁。纠正故障后重载再检查，保留统计文件。每日库故障单独呈现。活跃会话池和上下文应从新运行时建立。见[保留上限与降级](architecture.md#监控明细存储)。
- 401/403 核对 Dashboard 登录及配置中的管理员用户名，聊天管理员和 API key 均不能替代面板身份。见[面板错误](api.md#面板错误)。

## 检查通过但聊天仍失败

自检通过后继续核对真实调用的模型/档位、会话偏好、前置上下文、图片、工具模式与输出/队列限制。按[验收分层](testing.md#实机验收)记录真实生成和宿主路径。

## 私聊中 /agy cost 被当成对话

在该会话实际使用的 AstrBot 配置把插件加入「可用插件」（`plugin_set`），检查会话级插件开关、唤醒前缀和管理员 ID。用管理员发送 `/agy help` / `/agy cost`，再确认非管理员只收到权限提示。`/new` 单独有效不说明 `/agy` 已被分发。
