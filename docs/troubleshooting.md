# Agent 排障 SOP

适用基线 **v0.7.5 / AstrBot `>=4.29.4,<4.30`**。安全边界见 [AGENTS.md](../AGENTS.md)，源码/测试映射见 [故障定位索引](architecture.md#故障定位索引)。以下流程不要求读取任何凭据或完整人格内容。

## 先确定失败阶段

1. 记录插件/宿主/Python/CLI 版本、部署方式、AstrBot 实际系统用户、容器内路径、请求模式及模型/档位；区分“最初出错提交”和“当前排查提交”。
2. 管理员执行 `/agy doctor` 或在首次设置页重新检测。收集脱敏的 `stage`、`error_code`、CLI `exit_code`；只收必要字段，不粘贴整份环境变量、配置、stdout/stderr 或授权材料。
3. 分开判断：发现/启动 CLI → 官方登录 → `initialize` → `session/new` / 目录 → 保存 Provider → `session/prompt` 生成 → MCP 许可/宿主执行/回填 → 最终正文。某阶段通过不能代替下一阶段通过。
4. 失败后先检查本轮是否已有正文、thought 或工具交接；有则停止自动重试/fallback，避免副作用重复。先诊断，不以重新登录、重装、清空目录或放开 CLI 内置能力作为通用修复。

`/agy setup` 提供设置入口；`/agy use <provider_id>` 只更改命令所在目标会话的偏好。聊天命令需要聊天管理员身份；普通用户无响应不等于 CLI 故障。

## 无生成检测与实际服务对照

以下命令在**运行 AstrBot 的同一环境、同一用户、插件根目录和相应 Python 环境**执行；替换成真实原生可执行文件路径，Windows 使用 `agy.exe`：

```sh
/absolute/path/to/agy --version
/absolute/path/to/agy acp --help
python -m agy_provider.probe --command /absolute/path/to/agy --timeout 60
```

`probe` 仅有 `--command`、`--timeout`、`--data-root`，不读取 WebUI 插件配置，没有代理 CLI 参数，默认继承终端代理。终端通过而面板失败时，比较服务的 command、用户/HOME、代理模式和数据根；不要输出密码或全量 env。`probe` 退出 0 是检查通过、2 是阻塞；report 中 `exit_code` 是 CLI 退出码，缺失为未知，0 不等于 ACP 握手必成功。

## 错误分类决策表

| 错误/症状 | 优先证据与安全检查 | 可下的结论/下一步 |
| --- | --- | --- |
| `NOT_INSTALLED` / `COMMAND_NOT_FOUND` | 同一用户 PATH、默认目录、配置的绝对路径及容器挂载 | 只能说明当前入口找不到，不能说整台机器未安装；显式路径失败不静默换 CLI |
| `PERMISSION` / `LAUNCH` / `STARTUP_EXIT` / `CLI_WRAPPER` | initialize 前后阶段、真实退出码、执行权限/架构/解释器、旧包装脚本路径限制 | `--version` 成功不证明隔离启动可用；比较原生入口，不先当成未登录 |
| `AUTH` / `AUTH_BRIDGE` / 登录失败 | 同一用户的官方登录；认证源文件/父目录元数据、安全链接与登录流程归属 | 文件缺失可尝试原生认证；已有不安全文件不能忽略；不读 token，不复制其他用户登录态 |
| `MODEL` / `NO_MODELS` / `UNSUPPORTED` | 当前账号真实目录、当前模型档位、CLI ACP/隔离参数能力 | 刷新并重新选择，不硬填模型或沿用上一模型档位；4.30 文案识别不证明宿主 4.30 可用 |
| `RATE_LIMIT` | 本轮结构化错误/完整告警形态、attempt、`rpc_stage`、是否发 prompt/已有输出、候选遥测 | 持续限流或容量分类，不等于已证实账号额度耗尽；见下文 429 流程 |
| `QUOTA` | 是否明确含账号/日/月额度或 credits 耗尽信号；与泛化 `e.g. check quota` 区分 | 保留分类证据和冷却；通用示例文案不能证明余额为零 |
| `EMPTY` | end_turn/max_tokens、正文与 thought、permission 拒绝、tools/call 与宿主执行计数 | 只说明没有最终正文；不是所有 EMPTY 都由权限或 429 引起，见下文 |
| `TOOL_PERMISSION` | 当前活跃 prompt 的拒绝记录、工具名称/ID/状态、终态及限流证据 | 内置能力应拒绝；合法 MCP 按 #12 检查，不全局跳过 permission |
| `TOOL_ARGUMENTS` / `TOOL_SCHEMA` | 宿主原始 schema、模型 wire schema、最小合成参数、是否尚未进入宿主执行 | 按原始契约判断参数是否合法；schema 适配不能收窄宿主约束，见 #13 |
| `TOOLS_DISABLED` / `TOOL_CONTEXT` / `TOOL_CHOICE` / `TOOL_HISTORY` | enable_tools、tool_use、实例模式、真实 ToolSet、身份/abort_signal、auto/none 和回填历史 | 不通过开启 CLI 内置 shell 或重复执行工具补救；工具历史/结果不能重复提交 |
| `NETWORK` / `TLS` / `TIMEOUT` | 出错阶段、脱敏网络诊断、DNS/出口/系统时间/证书、代理和时限 | 只检查相应路径；管道 EOF 不直接等于网络错误；不禁用 TLS 或无限加超时 |
| `PROXY_AUTH` / `PROXY` | 407/代理分类、协议/端口/服务可达性、容器路由 | 先修代理；127.0.0.1 是当前容器，不要求重新登录 Google |
| `TRANSPORT` / `PROTOCOL` | JSON-RPC 格式、CLI 存活、EOF 与实际退出状态、版本/参数能力 | 通用 `-32000` 不直接等于 AUTH；403 不直接证明封号 |
| `HOST` / `LOCAL` / 保存部分成功 | 后台 report、action_error、result、宿主持久化与默认切换状态 | HTTP 已受理不是保存完成；已保存但未设默认要保留部分成功结果 |
| `IMAGE*` / `BACKPRESSURE` / `OUTPUT_LIMIT` | 图片格式/SSRF/目录/预算、宿主是否已过滤图片、消费者速度/输出上限 | 安全预算错误不是登录失败；不放宽目录/IP 边界或静默截断 |

显式 RPC 认证/协议错误不被泛化 stderr 覆盖；背景 stderr 也不能否定已完成的成功调用。CLI 告警过滤为有界启发式，不能宣称对自然语言零误判。对应代码与定向测试均在 [索引](architecture.md#故障定位索引)。

已知的宿主定点兼容及退役检查清单见 [兼容补丁登记](compatibility.md)；不要仅因某个 Issue 已关闭就删除兼容分支。

## RATE_LIMIT / QUOTA 与 #14

1. 确认错误来自哪个通道/阶段。独立 CLI 告警、结构化 RPC 错误和答案里提到“429”是不同证据。仅保留脱敏类别、attempt、阶段及计数。
2. 看 `request_sent`、`usage_status`、`has_output`，区分真实上游失败与本地 FailureGuard 短路；短路通常有 `origin_turn_id` / `retry_after`，本次未发送不表示原请求没消耗。
3. 遇到本机能力相关现象，核对 `provider.py` 的规范化与 `system_prompt.filter_host_capabilities()` 是否在请求入口生效，宿主文案是否属于已知完整块。未知文案不能通用正则盲删人格；以合成样例复现，禁止记录真实完整 system。
4. 检查 `content_429_candidate` 的 system 哈希/字节数、attempt、`has_output`。**候选存在不证明内容过滤、风控或账号额度根因；候选可能带 `has_output=true`，这种轮次更不能重放。** 没有候选也不能排除相关现象，CLI 摘要可能不完整。
5. 在用户已授权的真实测试范围内，才用同用户/账号、CLI、模型/档位、代理做最小 A/B：中性 system 与已公开的宿主说明，固定短问题，无工具。记录输出/错误与调用次数，不搬运私有人格或改变 system/user 位置后宣称语义等价。
6. 确认本轮无输出/副作用且冷却结束后，可人工发起一个新的最小请求；不增加并发、换号或实现自动“净化重试”。冷却默认 60 秒、最多 300 秒，期满单个 half-open 探测；EMPTY 去重默认 30 秒。标签不改变冷却逻辑。

对照 [#14 原始报告](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/14) 与 [历史调研](429-prompt-investigation.md)：596B 宿主声明足以在报告环境触发 429，是已记录现象；上游真实判定维度仍未知。当前精确/结构化过滤先修正失实能力声明，离线测试成功不能证明该环境的上游 429 已消失。4.30 只作为文案样本，宿主支持范围仍小于 4.30。

**#19 Safe Mode 独立路径**：若 AstrBot 4.29.4 开启 Safe Mode，先确认实际 system 存在官方第 4 条规则，再检查 `filter_host_capabilities()` 是否在完整宿主安全规则列表内将 `style/role-play` 定点改写为 `style and role-play`；独立裸句、人格引用、代码块不应被泛化替换。v0.7.2 已在报告对应类型的真实 webchat 链路通过一次端到端验收，不能据此推断其他所有 429 均消失。见 [#19](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/19)、[版本验收记录](../CHANGELOG.md#072正式发布2026-11-17) 和 [定向测试](testing.md#定向回归)。

离线入口：`tests.test_system_prompt`、`tests.test_safe_mode_rule`、`tests.test_content_429`、`tests.test_notices`、`tests.test_failure_guard`；命令见 [testing.md](testing.md#定向回归)。

## EMPTY、权限拒绝与 #12

按同一轮时间顺序记录：tools/list → tool_call/update → request_permission → MCP tools/call → 宿主审批/执行 → 回填 → 最终正文；缺失计数写未知，不写 0。

| 本轮证据 | 当前行为 |
| --- | --- |
| 有有效工具交接但尚无正文 | 正常中间态，宿主执行回填后继续原 prompt；不能立即判 EMPTY |
| 无正文但有明确上游重试错误/容量告警 | 保留 RATE_LIMIT/QUOTA 等上游分类，优先于权限归因 |
| end_turn 无正文且当前 prompt 有权限拒绝 | `TOOL_PERMISSION`；不进入 EMPTY 去重或额度冷却 |
| 无上述证据仍无最终正文 | `EMPTY`；usage 的 output tokens 不证明正文已返回 |
| 已有正常正文且记录过拒绝 | 拒绝记录不覆盖答案；之后失败仍遵守防重放 |

对 [#12](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/12)，先核对是否实际运行了修复后的代码，再检查当前 broker/session、prompt 活跃窗口、严格 MCP 名称映射、相同调用 ID 的已观测 pending/in_progress，以及唯一有效 `allow_once`。终态后、重复、跨会话、未知工具及永久许可继续拒绝；拒绝内置 shell/file/web/browser 是预期边界。

若许可已通过但没有 tools/call，继续查 CLI/relay；若已到宿主，检查原始参数 schema、预算、审批与回填，不再归因“一次性许可被取消”。不要把 #12 的离线/回声桩结论扩大到所有生产工具和 QQ 链路。

`TOOL_PERMISSION` 和工具守卫仅缓解 #11 的错误归因/误用；CLI 在内置工具被拒后未回喂模型就结束的问题仍存在。不要把 issue 关闭理解为该上游行为已经修复，或把未独立观测的宿主执行次数当作已知。

入口：[transport.py](../agy_provider/transport.py)、[tools.py](../agy_provider/tools.py)、[tool_turn.py](../agy_provider/tool_turn.py)、[sessions.py](../agy_provider/sessions.py)。测试：`tests.test_mcp_permissions`、`tests.test_permission_attribution`、`tests.test_tool_pipeline`。

## TOOL_ARGUMENTS 与 #13

1. 用一个合成工具复现，记录原始 schema、wire schema 的相关片段与合成参数；实际业务参数保持私密。
2. 在 `ToolSnapshot` 检查 `to_host_schema` 与 `tools[].inputSchema` 是否独立，`ToolBroker.dispatch()` 是否按前者校验。模型端补 string items 不应导致宿主原本允许的数字/对象数组被拒。
3. 验证缺失 items、显式 string items、空数组、嵌套数组与源对象未修改；两份 schema 变化都应使指纹失效。显式 string 数组拒绝数字是正确行为。
4. 判断失败发生在模型生成前、桥校验还是宿主执行；离线校验通过只证明桥契约，没有证明模型端所有 schema 可表达或会选出正确参数。

入口：[tools.py](../agy_provider/tools.py)；直接回归：`python -m unittest tests.test_tool_schemas -v`。[#13](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/13) 的旧 wire-schema 复现不能作为当前 dispatch 仍出错的证明。

## 启动、登录与模型设置

- 每个 ACP 子进程使用隔离目录，原生 CLI、包装脚本和认证后端须在同一服务用户下核验。不要删除整个插件数据根、CLI HOME 或 `.gemini`；其中可能有持久登录态。
- 登录入口是 WebUI 或官方 `agy acp --login`；不支持该参数时由用户在同环境交互运行 `agy`，之后重新检测。不默认推荐未经当前 CLI 核验的 `agy login`。
- 当前后端仅接收去除首尾空白后 1–4096 字符的单行授权码，拒绝控制字符；**没有解析完整 localhost 回调 URL**。用户只在自己的设置页提交，不发到聊天/issue。URL 只对当前拥有者可见，总预算 300 秒、取 URL 最多 45 秒；插件重载会取消旧流程，旧 ticket 不复用。
- 登录 CLI 退出 0 后还需 ACP 会话及模型目录复检；`auth_bridge=false` 只关闭文件桥，不保证隔离会话能使用钥匙串，也不是通用修复。
- 模型来自账号实际目录，Provider 列表有缓存；保存会复检。切换模型后重置并读取档位选项，不能承诺每个模型都支持 low/medium/high。相同模型及相同配置档位才复用 Provider。
- 保存结果非空但 `default_selected=false` / `action_error=HOST` 可能是部分成功；全局默认不覆盖已有会话独立偏好。检查目标聊天的 `/agy use`、实例启用状态和 agy_tools_mode。
- 旧名配置迁移须备份并避开宿主并发写；新配置存在则合并所需字段，不覆盖整份。禁用旧插件防止 `agy_acp` 重复注册，另查 command 包装脚本是否绑定旧数据目录。

## 工具、图片与监控

- 工具/图片要同时开启插件总闸和宿主对应 modalities，修改插件设置后重载。工具还要有真实 ToolSet、稳定身份和取消信号；`tool_choice=required` 或指定函数不受支持。chat 模式不会注入工具。
- 图片还需 CLI image 能力；默认 8 张是可配置值而非固定硬上限。校验 PNG/JPEG 的真实编码，限制像素/字节，下载遵守 DNS/IP/重定向 SSRF 边界，本地路径限定 `media_roots`。宿主提前过滤的图片无法由 Provider 恢复。
- **v0.7.4 图片修复的边界（#24）**：会话重建时，`prompt_blocks()` 仅附带当前用户轮的图片，旧图改为哈希占位；但 `prepare_media()` 仍对历史消息中的图片源逐一读取、验证并计入图片数量/字节限制。旧图失效或长历史累积过多可能在发给 AGY 之前报 `IMAGE*`/预算错误；这不是已修复的重复发送，也不能靠调高预算或放宽 SSRF/本地目录边界作为通用修复。优先收集脱敏的消息数、历史图片数、源类型、具体错误码，见 [计划状态索引](plan-status.md#待处理与待验证)。
- `/agy cost` 和 `monitor/status` 只读本地样本。实时池、近 24 小时、最近上下文采样是不同口径；未知 usage/占用不能显示成 0 或余额。重启后无历史不代表从未消耗。
- monitor 返回 401/403 时核对 Dashboard 配置管理员；API Key 被该 endpoint 明确拒绝，不关闭鉴权。不要把聊天管理员权限当作 Dashboard 身份。
- WebUI 使用原生 iframe bridge、当前 nonce 与轮询终态；旧响应、登录归属、复制、窄屏/主题等按 [实机验收](testing.md#实机验收) 单独检查。
- 面板复制按钮依赖「用户手势内」的同步降级链：宿主 iframe 为 `sandbox="allow-scripts allow-forms allow-downloads"`（无 `allow-same-origin`/`clipboard-write`），且面板多为 `http://<ip>:6185` 非安全上下文，`navigator.clipboard` 不存在属预期。只能先试 Clipboard API，再同步 `execCommand("copy")`，最后退回选中文本；不要改成 `await` 之后再复制，那会丢掉手势导致 `execCommand` 返回 false。授权链接无法新开窗口是宿主未授 `allow-popups`，页面只能提示改用复制。

## 检查通过但聊天仍失败

无生成自检只证明当时配置下的启动、认证/握手、会话与目录链路。继续核对真实调用的模型/档位、实例开关、会话旧偏好、历史预算、图片与工具上下文、输出/队列限制，再判断生成阶段的上游错误。

按 [报告模板](testing.md#交付报告) 记录已确认事实、待验证推断、环境差异与下一步；真实生成、工具、跨平台认证、长期刷新分别验收，不用“检测通过”概括为全面可用。
