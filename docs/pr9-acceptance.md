# PR #9 离线验收记录

2026-11-15，作者要求“验收并测试 PR9，暂时不合并入 main”。此指示更新了最初实现阶段“不运行测试”的约束。

## 验收结论

本机 Windows 的离线验收通过；已补充跨平台 CI 测试。PR 保持 Draft，未合并、未发布、未部署。
真实 AGY/Google 模型、完整 AstrBot 部署和组件精简 A/B 尚未验收，不据此宣称线上可用或节省 Token。

起点为 `bb6aa06`：原有 Python 165 项在本机出现 13 个失败、12 个错误、2 项跳过；原 PR 的 Linux/Windows × Python 3.11/3.12 四项 CI 也全部失败。
其中大部分是新参数、错误码和 unknown 缓存语义没有同步旧夹具/断言；本次调整保留了原断言含义，并补充针对新行为的测试。

## 实际缺陷与修复

1. **RPC 额度错误漏分类**：`Resource has been exhausted (quota)` 被归为 UPSTREAM，漏掉 FailureGuard 冷却。补充明确 quota 诊断匹配，429/503 容量错误仍保留 RATE_LIMIT。
2. **MCP relay 输出编码错误**：原 relay 通过系统默认编码输出 Unicode，中文/emoji 在非 UTF-8 环境失败。固定通过 stdout.buffer 输出 UTF-8 字节，并在 `PYTHONIOENCODING=ascii` 下执行真实 relay 子进程回归。
3. **Windows 后代进程清理竞态**：Job Object 关闭是异步的；仅等待前台 CLI 退出可能在后代仍持有工作目录时删除目录，产生 WinError 32。改为显式 TerminateJobObject，并有界查询活动进程归零后关闭 Job 句柄；保留 kill-on-close 和绑定失败时的 taskkill 回退。

## 已执行验证

本机环境：Windows、Python 3.12.10、Node.js 24.11.1。

| 检查 | 结果 |
|---|---|
| `python -W error::ResourceWarning -m unittest discover -s tests -v`，提供可选官方 runner 源码 | 198 项，195 通过、3 项按平台/权限跳过，16.467 秒；无 ResourceWarning |
| `node --test tests/setup.test.mjs` | 12 项通过 |
| `python -B -m compileall -q main.py agy_provider tests` | 通过 |
| `git diff --check` | 通过 |

Windows 跳过项：POSIX 文件属主/权限检查、Unix socket 专项、官方认证文件符号链接权限不满足（此权限的拒绝路径另有覆盖）。没有跳过本次新增的 Windows Job Object 或 MCP relay 测试。

可选官方宿主契约测试使用 [AstrBot v4.29.4 runner 源码](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/agent/runners/tool_loop_agent_runner.py)：只用 AST 选择原始 `_iter_llm_responses_with_fallback` 方法，注入测试边界依赖执行，不启动完整 AstrBot、不接触账号。AgyError 不属于 EmptyModelOutputError，重试适配桩在这些路径只执行一次，与原生重试条件一致。
源码保存在工作区外；设置 `AGY_HOST_RUNNER_SOURCE` 指向该源码即可复现。普通离线 CI 未提供时只跳过这个可选项，另有始终执行的宿主契约夹具测试。

## 新增覆盖

- 上下文增长、64000/50% 边界、异常/null/零/格式变化、缓存计费不可用于压缩判断、模型/窗口/会话隔离、当前回复完整交付后退役及下轮重建。
- 每个分块边界的额度/认证告警、两个不同 attempt 的持续 429、重复 attempt、503 恢复、正常正文引用错误码；真实 stdio 下快速失败后只发一次 session/cancel、保留失败 usage、回收 reader/Future。
- FailureGuard RATE_LIMIT 错误码保留、冷却、单半开探针与恢复；telemetry 单轮幂等、100 条容量、24 小时 TTL、分组、未知缓存字段与身份脱敏。
- Windows 回环认证桥的 tools/list、tools/call、Unicode 结果、错误密钥、实例隔离、旧连接请求重放、畸形/超大帧、握手超时、空闲轮/错误会话/schema 拒绝、审批拒绝/超时、连接取消和迟到结果拒绝。
- 完整离线链路：fake ACP → 实际 MCP relay 子进程 → ToolBroker → ToolTurn 宿主审批/交接 → 工具结果续轮；验证内置 session/request_permission 始终拒绝、压缩不会在工具执行中途退役、成功只交接一次、拒绝不交给执行器、取消后状态为 unknown。
- Provider 部分输出后 no_retry/no_fallback；宿主候选拦截、输出前冷却允许 fallback、其他 Provider 不受影响、并发 runner 隔离、卸载不覆盖第三方后置 wrapper。
- 实际 Windows Job Object 绑定、绑定失败回退、前台退出后后代存活再清理、宿主 os._exit 模拟异常退出后的子进程清理；重复 shutdown、spawn 重复取消、CLI 探测输出超限与超时。
- 组件精简默认关闭、支持/不支持参数、聊天 Agent 隔离、配置非法值和范围。

## 尚未验证

- 本机 PATH、`%LOCALAPPDATA%/agy/bin` 和用户 `.local/bin` 均未找到原生 AGY，未调用真实模型或 Google 账号。fake ACP 不等于真实 AGY。
- 未启动完整 AstrBot、真实聊天平台或外部业务工具。工具执行器在离线边界模拟，传输、会话、审批及续轮使用真实插件代码。
- `minimal` 的工具可见性、相同模型/人格的冷暖 Token/延迟 A/B、上游真实压缩事件均待有账号环境验收，实验默认 inherit。
- CLI 自动更新/二进制热替换和动态 MCP/模型热更新仍是明确延期项，不在此 PR 宣称交付。
- Windows 创建进程至绑定 Job 的短窗口仍存在；POSIX 未新增父死亡信号，宿主 SIGKILL 的限制不变。

## CI

修复提交推送后，以 PR 当前 HEAD 的 Regression tests 为准核对 Linux/Windows × Python 3.11/3.12 四项结果；本记录不沿用原始失败提交的结果。

## 实机验收（2026-11-15，小鱼生产环境）

生产环境：docker AstrBot 4.29.4（自建镜像）+ AGY CLI 1.5.8（consumer OAuth + auth bridge 部署适配），插件由 v0.5.1 覆盖部署为本分支 HEAD（8ff6513）。

| 项 | 结果 |
|---|---|
| 容器内离线回归 | 198 项，186 通过、12 项预期跳过（Python 3.12.13），compileall 通过 |
| 插件加载与注册 | 0.6.0 加载成功，host_compat 兼容层安装，agy_probe 保持全站主模型（fallback newapi/glm-5.2） |
| 配置迁移 | 3 个新键由宿主自动补默认（inherit / 64000 / 50%），`command` 未变；部署目录与仓库无仓库外热修改 |
| 宿主 provider 测试 | available / error:null（gemini-3.8-flash，两小时窗口内两次直连真实成功） |
| 真实 429 风暴（21:24–21:34 三轮，CLI attempt 1/2 均 RESOURCE_EXHAUSTED） | notice guard 归类 RATE_LIMIT、约 12s fast-fail、冷却短路后宿主切 fallback，用户约 17s 收到回复；无用户可见失败 |
| 工具轮 | fallback provider 下 web_search 工具调用与续轮正常；真实 Gemini 侧 MCP 续轮本次未覆盖 |
| 会话/缓存遥测 | provider_stats 正常落库；会话复用与缓存 token 观测可见 |

本轮未覆盖：minimal 模式 A/B、真实上游压缩事件、Windows 桥实机、RATE_LIMIT 冷却与 `max_concurrent` 并发排队联动。

已知跟进：限流风暴结束且冷却过期后，新会话 pipeline 轮次仍静默直连 fallback（无报错、无 Switched 日志），同期直连 provider 测试正常；疑似 host_compat 层状态或宿主 runner 行为，复现步骤与证据见 #10。
