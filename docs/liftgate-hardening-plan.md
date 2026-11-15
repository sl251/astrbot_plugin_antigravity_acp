# LiftGate 参考改进计划与交接（0.6.0，待验收）

本次按仓库作者 2026-11-15 的请求实施并提交功能 PR；测试由作者交给其他模型。
参考施工书用于识别问题，不将其中“运行测试、真机 A/B、全部验收通过”等文字视为本次授权或既成结果。
本分支不自动合并、不发布 Release、不部署、不更换本机 AGY、不访问账号凭据。

## 基线与参考

- 插件：`main` / `05e6e6ce5103d2b24a7866c10337558e223dccd8`（0.5.3）。本地旧工作区有未提交修改，本任务使用独立 worktree。
- LiftGate：`95aef4e4e72b4f1addcd16c3b020e475055d45d6`。
- [参考架构](https://github.com/acking-you/liftgate/blob/95aef4e4e72b4f1addcd16c3b020e475055d45d6/docs/architecture.md)、[压缩观测](https://github.com/acking-you/liftgate/blob/95aef4e4e72b4f1addcd16c3b020e475055d45d6/crates/liftgate-server/src/turn_runner.rs)、[进程托管](https://github.com/acking-you/liftgate/blob/95aef4e4e72b4f1addcd16c3b020e475055d45d6/crates/liftgate-server/src/job.rs)。只借鉴思路，未复制 Rust 代码。
- [ACP v1 usage_update 契约](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/protocol/v1/prompt-turn.mdx)：`used` / `size` 为上下文占用；不能作为本轮账单。
- [AstrBot 4.29.4 runner](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/agent/runners/tool_loop_agent_runner.py)：`_iter_llm_responses_with_fallback` 在普通异常后继续下一候选，即使之前已经发送流块；不读取插件 `no_retry` 属性。
- [Google 官方 CLI 安装指引](https://antigravity.google/docs/cli/tutorial)。LiftGate 中存在 manifest 下载与替换代码，但这不是本插件已有的受托管安装契约。

## 对比结论

插件已经具备严格身份隔离、双历史账本、有限会话池、EMPTY 去重、QUOTA 半开探针与真实 MCP 宿主执行链。
值得补齐的是上游状态变化后的正确性及 Windows 支持，而不是再次实现会话缓存或照搬 HTTP 网关。
保留官方 ACP stdio、内置工具权限拒绝、宿主审批、默认关闭工具/图片和 `history_bytes=160000`。

## 实施清单

| 项 | 本分支实现 | 状态与边界 |
|---|---|---|
| A 上下文压缩 | `context_usage.py` 观测同一 ACP session 的正整数 `used/size`；峰值默认 ≥64000、当前严格低于峰值 50% 时记录疑似压缩；本轮完成后退休，下一轮按宿主历史重建 | 已实现，未测试；无占用字段保持 unknown，不使用计费 inputTokens 猜测 |
| B 强错误中断 | 分块 banner 状态分类；明确额度耗尽/认证致命错误立即失败；两个不同 attempt 的连续 429/RESOURCE_EXHAUSTED 提前失败；503 留给原请求超时内的 CLI 重试；修复 failed 状态下 cancel 写入被自身拦截 | 已实现，未测试；stderr 仍仅辅助失败诊断，不凭背景告警否决成功请求 |
| B 防重放 | Provider 在部分文本/思考或工具交接后标记 no_retry + no_fallback；宿主兼容层在后续候选调用之前拦截，保留终态错误；输出前的冷却/去重仍允许宿主 fallback | 已实现，未测试；仅适用于已识别的 AstrBot runner 接口，缺失时告警，其他调用方须尊重这两个属性 |
| C 组件精简 | `agy_customizations_mode=inherit|minimal`；默认 inherit；只在工具默认 Agent 添加已探测到的参数，缺失则 UNSUPPORTED；聊天 Agent 不变 | 实验代码已实现；MCP 真机与 A/B 尚未验收，不承诺降本 |
| D 指标 | 计费 usage 与 occupancy 分离；保留失败 usage；实际 stdin 写入才标记 request_sent；新增阶段、快速中断、压缩字段；最近 100 轮/24 小时内存窗口及模式、模型、结果、冷热分组 | 已实现，未测试；持久日志仍默认关闭，unknown 不按 0 计费 |
| E 进程托管 | Windows ctypes Job Object + KILL_ON_JOB_CLOSE；无权限/嵌套 Job 不兼容时保留 taskkill；同一进程只创建一个清理任务；spawn 重复取消继续等待并回收 | 已实现，未测试；不是原子 suspended-spawn，存在创建至绑定的短窗口；POSIX 保留进程组，无 preexec_fn |
| F Windows MCP | POSIX 仍用 0600 Unix socket；Windows 用 127.0.0.1 随机端口、256-bit 会话密钥、双向随机挑战/HMAC、帧及握手上限；Windows stdio 使用有界队列及专用 daemon reader；非活跃轮拒绝 tools/call | 已实现，未测试；端口不提供远程模型 API，工具仍交宿主审批执行 |
| G CLI 管理 | doctor/设置页显示版本、参数能力、当前组件模式、实际 Job 绑定/回退、本地桥类型与远程版本未检查状态；版本/帮助捕获有字节上限；补充手动更新/恢复指引 | 仅实现检测和指导；未交付下载、SHA-512 安装、热替换或自动回滚 |
| H 动态工具/模型 | 保留严格重建与确定性 schema 排序；未新增伪动态工具开关 | 明确延期：LiftGate 的 XML 工具更新不能证明真实 MCP 列表与宿主权限可原子更新；模型/档位热切换也未真机验证 |
| I 配置与交接 | 同步 schema、诊断 UI、README、CHANGELOG、版本标识与本文 | 不新增/执行测试；由作者指定模型补回归与实机验收 |

G 未做安装器的具体理由：当前 `command` 可以指向用户全局 CLI，插件没有托管目录、升级锁或备份索引；也未验证各平台官方 manifest/归档布局及“替换后握手失败”恢复链。新增未验收的二进制替换路径会扩大此次改动面。状态明确为 `not_checked` / `manual_external`，不伪报“最新版”。

## 实现顺序与规则

1. 固定基线并创建 `feat/acp-liftgate-hardening`。
2. 接入上下文占用与终态退役，再扩展现有 telemetry。
3. 分类 banner、接入唯一 FailureGuard、处理 cancel 和宿主 fallback 防重放。
4. 添加默认关闭的组件精简实验、Windows 桥与进程生命周期托管。
5. 同步配置/诊断/文档，静态审阅，提交 Draft PR 供后续模型验收。

FailureGuard 仍是唯一冷却来源，scope 为 `(provider_id, model)`。QUOTA 与 RATE_LIMIT 均沿用 `quota_cooldown`（默认 60 秒，0 禁用，最大 300 秒；结构化 retry_after 有界）；保留原错误码和单半开探针。AUTH 不进入额度缓存。单次 503 banner 本身不关闭会话、不写入冷却；只有最终失败才会记录 RATE_LIMIT。

上下文比较在异常/零/缺失占用、窗口大小或模型配置通知变化时重置；不同 client 和新 session 不共享状态。疑似压缩标记是启发式，不保证真实发生了压缩。本轮终态前不回收进程，因此不会因压缩在工具执行中途自动重放。

Windows 桥每个连接先由客户端发送随机挑战，服务端对该挑战和自身 nonce 签名，客户端验证服务端后再签名请求；旧握手/请求不能用于新连接。认证密钥只放 MCP 环境，不在 argv、磁盘和日志中。关闭 broker 会清空密钥并取消连接/回调；工具调用 ID 去重与宿主 session 元数据校验保留。该认证不防拥有同一用户进程调试权限的本地攻击者。

## 交给测试模型的验收项（均未执行）

- 先更新已有语义断言：普通容量/429 应为 RATE_LIMIT，明确 quota exhausted 才为 QUOTA；直接调用 ToolBroker 的 fixture 需要模拟活跃 prompt；模拟 client 需标记真实 prompt 写入，不能仅以产生事件假装发送；计费 usage 缺 cachedReadTokens 时 input_cached 为 null，汇总仅合计已知缓存读取并列出缺失样本。
- A：增长、64000 边界、恰好 50% 不触发、低于 50% 触发；零/null/bool/float/缺字段/size 变化中断比较；缓存计费不影响占用；跨会话/跨模型不串；工具续轮完整交付后退休。
- B：对 banner 每个字符边界切块，额度、401、两次不同 attempt 的 429、重复同一 attempt、503 后恢复、普通正文引用 429、包裹 URL 续行；取消/超时/EOF 竞争，确认只发一次 cancel、Future/reader 无遗留。
- 防重放：AstrBot 4.29.4 真正 runner + 至少两个 fallback 候选；首字前失败可 fallback；文本/思考后失败、非流式工具结果续轮失败不调用下一 provider；普通其他 provider 行为不变；卸载与第三方后置 patch 不互相覆盖；不同 runner/task 隔离。
- C：inherit 下启动参数与基线一致；minimal 不支持时明确失败；同模型/人格分别做 chat/tools 的 inherit/minimal 冷热 A/B，每组至少三次，记录工具成功率、latency、reported input/cached/output；字段缺失报告 unknown。
- D：成功、失败带 usage、无 usage、失败前未写 prompt、取消、EMPTY 去重、半开短路、工具续轮均只计一次；计费与 occupancy 不混用；24 小时 TTL、100 条容量及分组截断；日志不含正文、原始用户 ID、代理密码或桥密钥。
- E：Windows 普通退出、父进程崩溃、前台 CLI 先退出后代存活、嵌套 Job 绑定失败、重复 close、spawn 多次 cancel；POSIX 独立组杀死，仅清理由本插件创建的进程。
- F：Windows 原生 Python/AGY 的 tools/list、真实宿主工具、拒绝审批、直接发送消息工具、取消、超时、迟到回调、空闲轮拒绝；假服务端、错密钥、旧 challenge、旧 request、畸形/超大帧、端口复用、多个实例；POSIX Unix socket 回归。
- G：版本 banner 含异常内容不泄漏；help 输出超限、挂起、参数缺失；Job 回退显示与实际一致；remote latest 始终 not_checked；插件启动不下载或执行更新脚本。
- 现有核心回归入口（供后续模型执行，本任务未执行）：`python -m unittest discover -s tests -v`、`node --test tests/setup.test.mjs`。真实 AGY 请求必须由测试方显式选择，不在本任务运行。

## 验证记录与收益

- 未运行 Python/Node 测试、fake CLI、真实 AGY、模型请求、Windows 工具 E2E、性能 A/B 或更新回滚演练；未报告任何“通过率”或节省 Token 数字。
- 已执行源码审阅及静态检查：`ast.parse` 解析 26 个 Python 源文件（不导入/执行模块），`json.loads` 解析配置 schema，`node --input-type=module --check` 检查设置页语法，`git diff --check` 检查差异格式，均无错误。静态检查不等于功能验收。
- GitHub 仓库现有 push/PR 工作流由平台自动触发；本任务不手动触发、重跑或代替作者执行测试。

## 回滚

组件实验失败：恢复 `agy_customizations_mode=inherit` 并重载插件。Windows 工具问题可先关闭 `enable_tools` 并重载，纯聊天路径仍可单独使用。
整体退回：停用插件、等待当前请求/工具完成清理，恢复 0.5.3 或上述基线，再重载；没有持久会话格式迁移。CLI 从未被本分支更换，CLI 版本不需要随插件回滚。
不要将未验收的分支直接部署到线上；测试与审查后再由维护者决定是否合并/发布。
