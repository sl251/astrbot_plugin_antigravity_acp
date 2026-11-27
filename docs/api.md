# WebUI 后端接口

路径相对 AstrBot 插件视图 bridge。下文是 `apiGet/apiPost` 解包后的 `data`。[Onboarding.handle()](../agy_provider/onboarding.py) 和 [Monitoring.handle()](../agy_provider/monitoring.py) 二次校验 Dashboard 管理员，拒绝 API key。响应 `Cache-Control: no-store`。`setup/*` POST 需要 `application/json`、至多 16 KiB 及本次加载的 `setup/status.csrf`。聊天指令使用独立的宿主管理员判定。

## 面板错误

处理器错误保留 HTTP 状态，JSON 形如 `{"status":"error","message":"[AGY:CODE] 说明","data":{"code":"CODE"}}`。AstrBot 4.29.4 iframe bridge 仅转发 `message`，按前缀 `^\[AGY:([A-Z][A-Z0-9_]*)\] ` 取稳定码。宿主提前拒绝时可能仅有宿主文案。

| HTTP | 稳定码 |
| --- | --- |
| 401 | `UNAUTHENTICATED`（无面板会话） |
| 403 | `FORBIDDEN`（身份不符）、`CSRF_EXPIRED` |
| 409 | `BUSY`（操作冲突）、`LOGIN_BUSY`（额度查询遇到登录中） |
| 503 | `CLOSED`（`monitor/*`、`setup/tool-policy`、`setup/quota-refresh`）、`STORAGE_UNAVAILABLE`、`QUOTA_UNAVAILABLE` |
| 400 | `CONFIG`、`UNSUPPORTED` 等业务错误；原有 `setup/*` 停用时 `CLOSED` 也是 400 |
| 500 | `INTERNAL`（本地操作失败） |

## 模型管理与路由

GET `setup/status` 返回 `cached_models`、`efforts_by_model`、`providers`、`routing`、`busy`、`operation`、`report`、`result` 和 `action_error` 等。切换下拉模型使用缓存，显式刷新目录走 `setup/check`，暂态失败保留已发现的目录。后台操作先返回快照，轮询至 `busy=false` 并检查结果。新建若保存成功但切默认失败，`result.provider_id` 仍保留，`default_selected=false` 和 `action_error` 表示部分成功。

| POST（均带 `csrf`） | 参数与行为 |
| --- | --- |
| `setup/check` | 可选 `model`。无生成启动、握手及目录检查。 |
| `setup/apply` | `model` 来自目录，`effort` 默认 `default`，`use_default` 和 `create_only` 默认 `false`，可选 `tools_mode`。先复检；同模型/档位/模式的唯一匹配可复用，`create_only=true` 或多重匹配时报错。旧请求省略模式时查重匹配任意模式，新建使用 `chat`。 |
| `setup/provider-update` | `provider_id`、`model`、`effort`、`tools_mode`。原地更新保留 ID，按需复检并重载，换模型重建会话。`effort` 取 `default/low/medium/high`，`tools_mode` 取 `inherit/chat/tools`。 |
| `setup/provider-repair` | `provider_id` 或 `all: true`，二选一。修复无效档位/模式、来源、配置与运行态漂移，重算模态并移除残留 `audio`。 |
| `setup/provider-default` | `provider_id`、`confirm: true`，设置并核验全局默认路由。`/agy use` 单独切换会话偏好。 |
| `setup/provider-impact` | `provider_id`。同步返回 `{impact}`，预览已加载配置中的主项、fallback、已知辅助字段及已观测会话引用，`session_scan="unknown"` 表示未全库扫描。 |
| `setup/provider-toggle` / `setup/provider-delete` | `provider_id`、`confirm: true`；toggle 另需布尔 `enabled`。停用/删除按影响要求选择可解析的 `migrate_default_to`，或用空串加 `force_confirm: true` 显式清空全局主项；有其他可见引用或需确认的情形还需 `force_confirm`。 |

[routing.py](../agy_provider/routing.py) 以宿主合并配置计算有效档位、工具模式、`expected_modalities` 与 `config_errors`。`runtime_active` 只表示实例已加载。`routing` 分别给出 `configured_primary_id`、`resolved_primary_id`、`resolution_reason`、显式/失效 fallback、非默认 profile 覆盖数及最近服务观测。下线时在宿主锁内处理全局路由和实例变更；只清理显式确认下的全局 fallback 引用，其他 profile、辅助项和会话偏好需另行核对。切换或清空后重新读取实际解析，宿主仍可能隐式选首个实例。

## 工具策略

GET `setup/status` 的 `tool_policy` 字段与 POST `setup/tool-policy` 的成功响应使用相同的策略对象。POST 至少带一个已知工具字段，`read_file: "deny"|"allow_once"`、`search_web: "deny"|"allow"`，二者默认拒绝。仅持久化提交的 `builtin_read_file` / `builtin_search_web`；存盘失败回滚。工具行的 `policy` 为保存值，`effective` 为加载策略在所需条件满足时的全局可用性，`choices` 由后台决定。任一保存值待重载则顶层 `reload_required=true`。总闸由宿主插件配置保存，实例模式走 `provider-update`。

| 能力 | 运行边界 |
| --- | --- |
| 宿主 MCP | 受总闸与实例模式控制，快照来自宿主，审批与执行仍由宿主管理。[两层权限与 schema](architecture.md#mcp-的两层权限与两份-schema) |
| `read_file` | 仅 Linux 官方 CLI `1.5.17` / `1.5.18`，版本探测与实际 ACP 握手均须通过，并使用 `strict`。活跃调用需匹配唯一 `allow_once` 及权限帧。仅当前私有工作区顶层、同属主、无组/其他写权限的普通单链接 `.txt`。合法 `StartLine` / `EndLine` / `ByteOffset` 为严格整数，参数在更新/权限帧之间须一致；拒绝 `file:///`、跨目录与软链接。插件核验元数据，CLI 读取。许可回包前计入次数预算。面板未确认时只提供 `deny`，启用请求返 `UNSUPPORTED`。 |
| `search_web` | 实验性。CLI 自动执行，关闭时先用模型护栏约束，收到通知仍会中止；发现时联网请求可能已发出。开启时每个新调用在通知后计入次数预算，超预算中止当前轮。策略行显示 `pre_execution_approval=false`、`enforcement="post_observation"`。预算无法保证实际联网次数。 |
| 固定拒绝 | 终端、写/改文件、网页读取/执行、浏览器、CLI 调度、子代理及工作流等。保持未知调用拒绝；以 [BUILTIN_SPECS](../agy_provider/builtin_tools.py) 为准。 |

两项可选原生工具都需要 `enable_tools`，受 `agy_tools_mode` 和 `tool_choice` 限制，可在没有宿主 MCP 时独立激活。MCP、读取和搜索共享本轮 `tool_max_calls`。预算及超时配置以 [schema](../_conf_schema.json) 为准。CLI 调查仅用于[追溯实测证据](issue41-builtin-tools-probe.md)。

## 官方 CLI 额度

GET `monitor/quota` 仅读内存缓存并核验环境，不启动 CLI。进入监控、页面恢复或登录结束时先 GET，缺失或过期才尝试一次按需 POST。GET 失败、缺少面板就绪条件或登录忙碌时跳过自动 POST。常规监控轮询与重置倒计时不刷新额度。

POST `setup/quota-refresh` 仅接受 `csrf` 与可选严格布尔值 `force`，省略或 `true` 跳过成功缓存和失败冷却。`force=false` 与 `/agy quota` 走 60 秒缓存；可观察的同一登录环境中，暂态失败冷却 60 秒，`AUTH`、`NOT_INSTALLED` 或 `UNSUPPORTED` 等待手动刷新或环境变化。并发合并。`QuotaService` 先检查 CLI ≥ 1.5.17，再在临时工作目录运行官方 `agy -p /usage --output-format json`，含版本检查总超时 30 秒。只接受 `usage` 命令结果，存在 `num_turns` 时必须为整数 0。子进程继承服务用户的认证环境，插件仅读认证文件元数据。代理由插件 `proxy_mode` 决定，`custom` 注入 `proxy_url`，`direct` 清除 HTTP/HTTPS/ALL 代理变量（含大小写），`inherit` 继承原环境。

快照为 schema v1，`source="official_cli"`。`fetched_at` 是实际 UTC 采样时间，初始为 `null`，`groups=[]`、`error=null`。`groups[]` 含 `name`、`models_shared` 和 `buckets`，每个 bucket 有 `id`、`name`、`window`、`remaining_fraction`（0–1）、`reset_time`（UTC 或 `null`）。同组共享额度，不推算绝对额度。`capability` 为 `unknown/supported/unsupported`，`cli_version` 可为 `null`。

查询失败通过 HTTP 200 快照内的脱敏 `error.code/message` 表达，代码见 [quota.py](../agy_provider/quota.py)。同环境暂态失败可保留上次成功值并标 `stale=true`。登录流程、环境变更及认证失效废弃旧值；`login_env` 为服务内匿名标记。不可观察的原生认证环境只返回当次查询结果，后续不复用快照和冷却。插件停用取消查询并清空缓存。

## 近 24 小时运行统计

GET `monitor/status` 使用 schema v2，含 `routing`、失败摘要、窗口及 `instances`。每实例 `summary` 和 `recent_turns` 使用合并后的实时和已恢复样本；保留最多 100 轮，接口返回最近 20 轮。`/agy cost` 同源。`historical: true` 表示已恢复样本，`history_storage` 为 `available` / `memory_only`，未加载为 `null`。`pool`、`latest_context` 与运行状态来自当前 runtime。缺失用量保持 `null`，复用率按实际发出的轮次计算。额度查询不进入生成统计。[落盘字段、保留期限及降级](architecture.md#监控明细存储)

## 每日用量

GET `monitor/daily/30`、`monitor/daily/90`、`monitor/daily/365` 返回 schema v1，`timezone="host_local"`。`range` 含 `days/start/end`，`end` 是宿主今天。`models` 为区间模型列表，`days` 仅含有终结轮的日期，含 `date` 及 `by_model`，同一组数值列在日级、模型级均有。

- `requests` 含失败、取消及本地短路，`success` 只计 `ok`，其余计 `failed`。
- `input_tokens` 含 `cached_tokens`，`total_tokens` 是已知输入与输出（`output_tokens`）之和。全未知列为 `null`，已知零和无记录分别显示。
- `usage_unknown_turns` 为已发送但未获可用 usage 的轮次。模型名以实际本地轮次为准；额度组另由官方 CLI 提供。每日库不可用时返回 503。

## 聊天管理员指令

| 指令 | 用途 |
| --- | --- |
| `/agy`、`/agy help` | 帮助 |
| `/agy setup`、`/agy models` | 设置入口、模型和实例 |
| `/agy use <provider_id>` | 切换当前会话 |
| `/agy status`、`/agy doctor` | 加载状态、无生成自检 |
| `/agy cost`、`/agy quota` | 本地成本统计、官方额度 |
| `/agy usage [30&#124;90&#124;365]` | 每日用量摘要，默认 90 天，附最近 28 天文本热力图 |

需插件启用且宿主已分发该消息。非管理员裸命令或已注册子命令只收到 `FORBIDDEN`。`setup/status.command_scope_warning` 只检查默认配置的插件范围，会话独立配置由管理员核对。
