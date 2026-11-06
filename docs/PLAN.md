# AstrBot × Antigravity CLI 原生 Provider 项目计划书

- **项目名：** astrbot-plugin-agy-provider（Python 包/插件建议名：`astrbot_plugin_agy_provider`）
- **建议仓库：** `sl251/sl251-astrbot-plugin-agy-provider`
- **仓库可见性：** Private（开发初期）
- **阶段：** 方案设计 / 未实现
- **目标宿主：** AstrBot 4.x（以实际运行版本和 Provider API 为准）
- **核心后端：** 已登录的官方 Google Antigravity CLI (`agy`)，优先 ACP/stdio
- **修订日期：** 2026-11-07

## 1. 背景与问题

现有 `astrbot_plugin_agy` 以 `/agy` 命令或 LLM 工具的方式发起一次 CLI 任务，适合编码任务，但不会直接成为 AstrBot 日常聊天使用的主模型 Provider。另一些 AstrBot Antigravity 插件使用 OAuth 号池和 OpenAI 兼容桥，既偏离「不做反代」目标，也提高了凭据与服务条款相关风险。

本项目要解决的是 **「AstrBot 正常的模型选择与聊天管线」到「官方 agy ACP 子进程」的协议适配**，并把对话状态和工具执行权牢牢留在 AstrBot 一侧。

必须明确：不做反代只改变技术实现，不会自动改变 Antigravity 订阅的使用许可，也不能证明封号风险较低。项目定位为技术研究与兼容性验证；生产部署的合规选项是 Gemini 官方 API。

## 2. 目标与非目标

### 2.1 本期目标（MVP）

1. 在 AstrBot WebUI 新增 `agy_acp` 聊天 Provider，可显示可用模型、进行连通性测试。
2. 通过 `agy acp` 的标准 ACP/stdin/stdout JSON-RPC 与官方 CLI 通信，不自行重放 Google OAuth 网络协议，不读/复制登录令牌。
3. 支持非流式普通文本聊天和多轮上下文；确保用户消息不会被重复注入。
4. 同群不同会话及私聊隔离，支持服务重启后的安全重建策略。
5. 支持超时、主动取消、退出回收和错误提示；不能让失败的 `agy` 请求阻塞 AstrBot 主事件循环。
6. 采用最小权限配置，默认拒绝 CLI 内置文件/命令执行请求，不自动开启 YOLO 模式。

### 2.2 后续目标

- 流式文本与可选的 reasoning 单独呈现。
- 图片输入（ACP 客户端能力允许时）；附件不落到共享工作目录。
- 将 AstrBot 准许的 `func_tool` 映射到受控 ACP 工具调用；坚持宿主权限审批。
- 用量/额度只读诊断、模型重新发现、兼容性回归测试。

### 2.3 明确不做

- Google OAuth 反向代理或 OpenAI 兼容 API 转发（对 Google 的请求）；
- 伪造 JetBrains/Zed 等客户端标识、提取或轮换令牌、号池、二进制 patch；
- 自动绕过配额、封禁、账号限制或上游服务条款；
- 将 `agy` 获得的 CLI 系统权限直接交给任何 QQ 群成员。

## 3. 技术选择

| 模块 | 设计 | 理由 |
|---|---|---|
| AstrBot 集成 | 新建 `Provider` 适配器并用 `register_provider_adapter` 注册 | 真正作为主模型出现在下拉框，而不是插件指令 |
| 模型传输 | `agy acp`，JSON-RPC over stdio | 不暴露额外 HTTP 端口、不自行反代 Google |
| 进程管理 | 异步子进程、请求 ID 映射、健康检查、退出清理 | 避免事件循环阻塞与孤儿进程 |
| 会话 | 以 AstrBot `session_id` 为主键，附平台/聊天隔离前缀 | 多人聊天不串上下文 |
| 模型目录 | 以 CLI/ACP 实际返回为准，不硬编码不存在的模型 | 避免选中未授权模型 |
| 工具 | MVP 全部禁用；后续单独受控桥接 | 避免模型绕过 AstrBot 权限边界 |
| 替代后端 | 官方 Gemini API（未来可选独立 Provider 配置） | 为合规部署保留选择 |

### 3.1 预期链路

```text
QQ / Telegram / WebChat
          │
          ▼
     AstrBot 消息管线
          │
          ▼
 Provider: agy_acp  ───► SessionRegistry（聊天隔离）
          │
          ▼
 ACPTransport（JSON-RPC / stdio）
          │
          ▼
     官方 agy acp 进程
          │
          ▼
      Google 模型服务

[第二阶段] ACP 工具请求 → AstrBot 权限门禁 → AstrBot 工具执行
```

### 3.2 接口草案

Provider 侧按实际 AstrBot 版本实现 `test()`、`get_models()`、`text_chat()`、`text_chat_stream()`、`terminate()`；支持模型切换和必要的配置模板。适配器只接收/返回 AstrBot 官方的数据类型，如 `LLMResponse`、消息上下文和 `ToolCallsResult`。

ACPTransport 负责 `initialize`、`session/new`、`session/prompt`、`session/cancel`、`session/update`，以及当前官方 CLI 实际实现的模型配置接口。必须通过能力协商决定可用方法，不能假定所有版本都有 `session/resume`、`--process-per-session` 或自定义系统提示注入能力。

### 3.3 会话一致性（重点）

AstrBot 会在每轮提供 `contexts` 历史，而 agy ACP 可能自身保存会话。**不能把 AstrBot 的完整历史每轮原样追加给已存在的 agy 会话**，否则会重复消费上下文。

建议实现 `HistoryCoordinator`：

- 新 ACP 会话：合成系统提示、历史与本轮输入；对单条输入施加字节上限并记录截断信息。
- 已存在 ACP 会话：仅发送本轮新增输入；维护已确认轮次序列与摘要。
- AstrBot 历史编辑、压缩、回滚或模型切换：关闭旧 ACP 会话，按安全的上下文预算重建；防止双重历史。
- 插件重启：默认保守重建，除非确认 CLI 提供稳定可靠的恢复接口。
- 群内会话键至少包含平台、会话和必要的发言人维度；由配置决定群聊是否共享上下文。

### 3.4 安全边界

- 启动官方 `agy` 进程时不设置危险权限跳过开关，不修改或导出官方 Token。
- 禁止将 ACP 进程暴露为公网 API；如内置 HTTP MCP 仅允许 loopback，且具备随机令牌与单会话授权。
- 不允许模型直接触发 CLI 内置命令执行、文件修改或浏览器等能力；未知权限请求一律拒绝。
- 即使未来接入工具，也必须由 AstrBot 权限系统审批，并做路径白名单、时限与操作日志。
- 管理命令仅管理员可用；用户输入不可成为 shell 参数拼接或 system prompt 之外的控制字段。
- 对失败/429/认证错误进行明确分类；不自动轮换账号、改客户端身份或试探上游限制。

## 4. 项目结构（拟定）

```text
astrbot_plugin_agy_provider/
├── __init__.py
├── main.py                    # AstrBot Provider 注册与入口
├── _conf_schema.json          # WebUI 配置
├── metadata.yaml
├── requirements.txt
├── agy_provider/
│   ├── config.py
│   ├── transport.py            # ACP 进程和 JSON-RPC
│   ├── sessions.py             # 会话/历史一致性
│   ├── messages.py             # 消息、图片、reasoning 转换
│   ├── models.py               # 模型发现与选择
│   ├── errors.py               # 异常与重试边界
│   └── tools.py                # 后期：受控工具桥接
├── tests/
│   ├── fake_agy.py
│   ├── test_transport.py
│   ├── test_sessions.py
│   ├── test_provider.py
│   └── test_permissions.py
├── docs/
│   ├── PLAN.md
│   └── TEST_MATRIX.md
└── README.md
```

注：以上是**目标目录**，不是已经生成或可安装的实现。

## 5. 分阶段实施清单

### P0：确认环境及协议可用性

- [ ] 锁定 AstrBot 版本，阅读当版 `Provider` 与 `register_provider_adapter` 接口。
- [ ] 检查操作系统、`agy --version`、`agy models`、`agy acp --help` 和官方账户登录状态。
- [ ] 用与 AstrBot 相同的运行用户验证 ACP `initialize` 和单次聊天；记录 stdin/stdout 真实格式。
- [ ] 确认 CLI 用户协议与账号授权范围；如果不允许第三方集成，则不在生产账号上继续测试。
- [ ] 评估容器部署：`agy` 必须运行在容器内，或通过**受控本地 IPC** 访问；不要开放公共 ACP 服务。

**完成标准：** 有最小 ACP 探针记录与平台差异清单；不触碰任何登录凭据。

### P1：文本主 Provider MVP

- [ ] 注册 `agy_acp`，提供模型列表、模型配置、连通测试。
- [ ] 实现 ACP 进程初始化、JSON-RPC 请求响应/通知分发和关闭。
- [ ] 实现非流式 `text_chat` 和系统提示注入（依赖 CLI 支持）。
- [ ] 实现对话隔离与基于历史变化的重建。
- [ ] 设计受限工作目录，拒绝未知 ACP 文件/权限请求。

**完成标准：** AstrBot 主模型能选 `agy_acp`；单聊 20 轮不串话、不会重复历史；重启后表现可预期。

### P2：流式、稳定性和可观测性

- [ ] 支持 `text_chat_stream`；解析正文、reasoning 和最终结束状态。
- [ ] 取消任务能停止对应的 ACP 请求，不影响其它聊天。
- [ ] 处理认证失效、配额不足、网络超时、子进程崩溃、空输出和客户端异常退出。
- [ ] 限制并发进程、队列长度、空闲时间、单轮输出及历史回放大小。
- [ ] 提供脱敏日志：记录模型、会话短 ID、耗时与错误类型；不记录凭据与敏感完整 prompt。

**完成标准：** 能稳定运行 2 个独立会话；主动取消有效；异常后不会遗留进程。

### P3：图片与工具（可选）

- [ ] 先确认官方 ACP 对图片输入的支持范围，再适配 AstrBot 附件。
- [ ] `func_tool` 映射到受控宿主工具，明确拒绝不能映射的本地动作。
- [ ] 工具调用必须遵守 AstrBot 的授权配置，默认关闭危险工具。
- [ ] 验证工具结果、二次模型调用与消息历史一致性。

**完成标准：** 工具执行只经过 AstrBot 权限层；没有越权文件写入或命令执行路径。

### P4：发布前审查

- [ ] 完成文档、安装说明、兼容版本、许可证与第三方代码署名。
- [ ] 检查机密文件、示例环境变量、日志中是否存在账号标识和 token。
- [ ] 在不同 AstrBot 版本与环境跑回归测试，注明未测试组合。
- [ ] 发布前再评估 Google 授权政策：若不符合，不宣称免封号或适合公开部署。

## 6. 需求优先级

| 优先级 | 功能 | 发布条件 |
|---|---|---|
| P0 | 主模型注册、文本请求、会话隔离、权限拒绝、取消 | MVP 必须 |
| P1 | 流式输出、模型列表、运行状态、错误分类 | 稳定版必须 |
| P2 | 图片、reasoning 增强、额度诊断 | 视协议实际能力 |
| P3 | AstrBot 工具桥、可持久恢复 ACP 会话 | 最后实现 |

## 7. 风险与对策

| 风险 | 后果 | 对策 |
|---|---|---|
| Google 对第三方 Agent 使用订阅有限制 | 账号限制或封禁 | 研究场景隔离；生产使用官方 Gemini API；不宣称安全 |
| agy 升级改变 ACP 消息形状 | 插件失效 | 明确依赖版本、协议能力检测、fake-agy 回归测试 |
| AstrBot 历史与 agy 历史重复 | Token 激增、答复混乱 | 仅增量发送 + 哈希校验 + 回滚重建 |
| 群消息触发 CLI 权限 | 文件/命令安全事故 | 禁内置工具、最小权限、权限白名单、管理员限制 |
| 云端 AstrBot 与本地 agy 不同机 | 无法直接 stdio | 首选同宿主部署；跨机另设计授权 IPC，不公网暴露 |
| 低配机器资源不足 | 卡顿、超时 | 并发上限 1、空闲回收、异步子进程和超时 |

## 8. 参考代码与许可

- [KardeniaPoyu/astrbot_plugin_agy](https://github.com/KardeniaPoyu/astrbot_plugin_agy)：可参考 CLI 调用、会话映射与 AstrBot 交互，但它不是主模型 Provider。
- [lili-luo/dsh-plugin-bundle](https://github.com/lili-luo/dsh-plugin-bundle)：可研究 JSON-RPC、ACP、会话恢复与工具桥接；不采用其客户端身份伪装或二进制修改方案。
- [zxx624/astrbot_plugin_hermes_ecosystem](https://github.com/zxx624/astrbot_plugin_hermes_ecosystem)：可参考 `Provider` 注册与 `text_chat`/`text_chat_stream` 接口。
- [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)：唯一应作为当版接口真相的宿主项目。

复制代码前检查上游许可证和版本，保留必要版权声明。本文仅是实施设计，不是对第三方插件安全性、稳定性的背书。

## 9. 最终验收

1. AstrBot 能选中 `agy_acp` 作为主聊天模型，模型选择与错误提示准确。
2. 文本非流式/流式正常，单聊连续 20 轮上下文不重复。
3. 两个并行的会话互不串话；取消一方不会中断另一方。
4. CLI 退出、超时、认证失败或额度不足不会导致 AstrBot 挂死。
5. 默认无命令执行/越权文件访问，所有权限请求有明确拒绝路径。
6. 无 Google API 反代端口、无令牌导出/缓存、无账号轮换或规避逻辑。
7. README 明确写出政策与合规限制，并提供官方 Gemini API 替代方案。

**里程碑判定：** 1—5 是技术上线条件；6—7 是安全与披露条件。若上游服务条款禁止当前使用方式，即使技术验收通过，也不能宣称适合生产使用。