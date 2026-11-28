# 计划与历史资料状态索引

基线核对于 2026-11-28，main `9bdc8a1`（含 #52 跟进加固）。当前实现与入口以 [AGENTS.md](../AGENTS.md) 为准。下列证据仅适用于所述提交、环境和场景。Issue 的关闭与验收是独立状态。

## 近期已实现及证据范围

| Issue / PR | 已实现 | 已验证与仍缺证据 |
| --- | --- | --- |
| [#36](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/36) / [#37](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/37) | 模型原位编辑、配置漂移修复、受控默认迁移及影响预览 | PR 有离线和 Ubuntu/Windows CI，发行记录有 AstrBot 容器加载。真实 iframe 的主题、剪贴板和交互矩阵未完整记录。 |
| [#38](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/38) / [#39](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/39) | 工具逐项权限、实验性搜索、官方额度、每日用量和管理员指令 | PR 有离线、CI 和 CLI 搜索观测；事后取消且未收到结果帧的记录不能证明联网请求未发出。QQ 完整端到端覆盖未确认。 |
| [#41](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/41) / [#42](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/42) | 原生注册表、Linux 只读分页、无宿主 MCP 解耦 | PR 有离线、CI 及 Linux Docker / 官方 CLI 1.5.18 的无生成与无 MCP 分页冒烟。Windows/macOS 的原生读取未验证，其他 CLI 版本按当前能力门槛。写入梯队主动延期，继续拒绝。 |
| [#43](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/43) / [#44](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/44) | 额度按需刷新，明细跨重载恢复 | PR 有离线、CI，及 Linux Docker 真实 CLI 额度、运行时重建和另启进程恢复。管理员 iframe 验收及带生产历史样本的宿主整进程重启未记录。 |
| [#45](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/45) / [#46](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/46) | 执行超时后的迟到结果恢复、步数用尽的无工具收尾 | [发行记录](../CHANGELOG.md#0862026-11-27) 记载 [CI](https://github.com/sl251/astrbot_plugin_antigravity_acp/actions/runs/45871142042) 四组通过、AstrBot 4.29.4 runner 源码离线回归，以及 Linux Docker / AstrBot 4.29.4 实机部署与一轮 webchat 验证。长工具场景未在实机复现，原故障中 fallback 日志与实际切换不符的原因仍未知。 |
| [#48](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/48) / [#49](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/49) | 带图工具结果的续轮校验、MCP 图片回传与续轮判定连带修复 | [发行记录](../CHANGELOG.md#0902026-11-28) 记载 CI 四组通过与合并后离线回归；2026-11-28 在 Linux Docker / AstrBot 4.29.4 / 官方 CLI 实机以 webchat 复现并确认修复（`TOOL_HISTORY` 48 小时 5 次降为 0）。工具真正返回图片时 MCP `image` 块送达 CLI 的路径仅离线覆盖。连带排查出的 [#50](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/50) 为线上 `media_roots` 配置问题，已改配置验证，不涉及代码。 |
| [#51](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/51) | 热力图悬停与月标修复、只读 `monitor/turns` 与历史调用记录面板 | PR 记载离线 Python/Node 回归与 CI 四组通过；v0.9.0 已部署 Linux Docker / AstrBot 4.29.4 实机（容器内回归 389 项通过、加载无报错），面板交互由维护者验收，`monitor/turns` 未经自动化实机调用验证。 |
| [#52](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/52) / [#53](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/53) | 纯聊天与无工具路径改用默认 Agent + `AGENTS.md`，以及 `probe` 精简透传、未登记工具防重放与护栏/文档跟进加固 | PR #53 与跟进分支均有 Windows/Linux 离线回归（394 项，含 AstrBot 4.29.4 真实 runner 源码）及 Linux Docker / CLI `1.5.19` 实机 `probe`（`inherit`/`minimal`）、两轮会话复用、护栏拒止与 `/AstrBot/AGENTS.md` 无穿透核查。单账号精确额度倍率 A/B（沿用上游 `#1890`）、QQ/webchat 长对话与 Windows/macOS 实机未验证；CLI `1.5.19` 原生 `read_file` 保持 `unverified`。 |

#45 Issue 正文混合了主 runner 和子 Agent 的步数日志。原线上触发为工具交接后的执行超时，主 runner 的耗尽收尾是另一路。见[维护者的根因更正](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/45#issuecomment-7199090573)。PR #40 已关闭，文档说明随后随 #42 合入。

## 关闭、延期与未验证

- [#31](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/31)（内部独立 QA）与 [#10](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/10)（fallback 滞留调查）以不计划继续关闭。旧的「待 #31 验收」不构成任务或通过记录。
- [#32](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/32) 主要修复已合入，§5 的并发争用、池锁和专用 Agent 调查主动延期。历史 WebUI 计划的 P1/P2 仅供追溯，已交付能力按当前源码及上表。
- AstrBot 4.30、跨平台认证、长期运行和 QQ 的覆盖范围以实际记录为准。兼容调整参见[兼容登记](compatibility.md)和[实机验收方法](testing.md#实机验收)。

## 历史记录

| 材料 | 追溯用途 |
| --- | --- |
| [#30 前端](acp-30-frontend-handoff.md)、[#32 后端](acp-32-backend-handoff.md)、[离线 QA](acp-30-32-offline-qa.md) | 旧 Vue 构建、候选交接及测试证据。当前面板使用原生 `views/setup/`。 |
| [观测面板计划](webui-observability-dashboard-plan.md)、[429 调研](429-prompt-investigation.md) | 早期方案和调查假设。 |
| [#41 工具探针](issue41-builtin-tools-probe.md)、[PR #9 验收](pr9-acceptance.md)、[#16 验证](agent-docs-validation.md) | 固定版本、场景的真实或离线证据，核对各自环境和跳过项。 |
| [Agent 旧施工书](agent-docs-next-release-plan.md)、[LiftGate](liftgate-hardening-plan.md)、[0.5.0 任务书](next-version-task.md)与[实现记要](impl-notes-0.5.0.md)、[v0.4.0 交接](archive/HANDOFF_0.4.0.md)与[v0.4.1 测试记录](archive/TEST_REPORT_0.4.1.md) | 保留当时的交付范围和原始记录。 |

任务管理和新增验收证据按[开发与交付规范](testing.md#开发流程)更新，保留历史正文。
