# 下一版本候选：Agent 文档与排障入口重构

> **历史归档（已完成）**：#16 文档重构已在 v0.7.1 合并、发布；此处的“下一版本”“尚未验收”是施工阶段表述，不再代表当前待办。原始验证记录仍见 [验证记录](agent-docs-validation.md)，不能将当时未测试项目补写成通过。当前导航以 [AGENTS.md](../AGENTS.md) 与 [计划状态索引](plan-status.md) 为准。
> 当前入口：[AGENTS.md](../AGENTS.md)、[架构](architecture.md)、[排障](troubleshooting.md)、[测试](testing.md)。下列清单记录文档实施范围；勾选不代表实机验证或最终验收。关联 [#16](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/16)。

## 现状与问题

- README 的 Agent 折叠区混合当前架构、0.6.0 / 0.6.1 / 0.7.0 历史实现；检索时容易将过期细节误当作当前契约。
- 排障 SOP 主要覆盖找不到 CLI、登录失败、工具/图片不触发、连通测试后聊天失败，尚未形成针对 #11–#14 的可操作故障路径。
- 缺少面向开发 Agent 的工作流程、回归测试路径、交付格式与事实/推断/未验证的区分。
- 目录表只能告诉 Agent 文件存在，缺少「问题类型 → 入口模块 → 对应测试」索引。
- README 尾部存在孤立的 Markdown 代码围栏，需清理。

## P0：建立项目级 Agent 入口

- [x] 在仓库根目录新增简短的 `AGENTS.md`，作为 Agent 项目入口；只包含项目定位、适用版本、严格安全/兼容边界、工作流程、测试指令及专题文档索引。
- [x] 以源码和现行测试核对所有断言；文档不得宣称「已验证」未经过的上游环境。保留当前 AstrBot 运行兼容范围 `>=4.29.4,<4.30`；4.30 已知提示文案兼容处理不等于完成 4.30 宿主兼容验收。
- [x] 把 README Agent 区的按历史版本叙述改为「当前有效契约」简表和按需阅读入口；历史实现细节留在 CHANGELOG 与已有专项文档，避免复制成多个真相来源。
- [x] 明确不可突破的约束：官方 `agy acp`、凭据不外泄、禁止内置高风险工具、仅当前活动会话的合法 MCP 一次性授权、宿主仍负责审批/校验、不重放部分输出或已交接工具的副作用。
- [x] 在文档中明确：`content_429_candidate` 只是诊断候选，不能证明内容过滤、风控或额度根因；离线回归成功不等于上游 429 已消失。

## P0：可执行的排障与开发流程

- [x] 建立 `docs/troubleshooting.md`：症状/错误码 → 优先证据 → 源码入口 → 测试/安全检查 → 保守结论。至少包含 RATE_LIMIT、QUOTA、EMPTY、TOOL_PERMISSION、TOOL_ARGUMENTS、NETWORK、登录/启动及模型设置异常。
- [x] 避免「能握手/能保存模型 ⇒ 必定能生成」一类错误推断；严格区分 CLI 登录、ACP 握手、模型持久化、真实生成。
- [x] 建立 `docs/testing.md`：调研→最小复现→实现→定向回归→全量回归→实机验收→PR 交付。给出当前仓库可执行的 Python / Node 测试命令与适用条件；命令须先实际核对。
- [x] 规定交付报告字段：改动范围、原因与证据、测试通过/失败/跳过数量、环境及版本、真实 CLI/宿主验证边界、剩余风险、回滚方式。
- [x] 规定先找源码/测试证据再设计修复；不因让测试变绿而改变正确的契约；不得读取或记录 OAuth、代理密码、完整提示词或敏感工具参数。

## P1：模块地图与按需加载

- [x] 建立 `docs/architecture.md`，集中描述当前数据流、会话复用、工具权限、提示词过滤、错误处理、遥测与 WebUI 边界；避免复制冗长的历史版本日志。
- [x] 增加经源码核验的「问题 → 代码 → 测试」索引，至少覆盖：
  - 系统提示词：`agy_provider/system_prompt.py` / `tests/test_system_prompt.py`
  - 429/告警/错误：`agy_provider/notices.py`、`sessions.py` / `tests/test_notices.py`、`test_content_429.py`
  - ACP 消息/权限：`transport.py`、`tool_turn.py` / `tests/test_mcp_permissions.py`、`test_permission_attribution.py`
  - MCP schema：`tools.py` / `tests/test_tool_schemas.py`
  - 用量/监控：`telemetry.py`、`monitoring.py` / `tests/test_telemetry.py`、`test_monitoring.py`
  - WebUI：`views/setup/` / `tests/setup.test.mjs`
- [x] `AGENTS.md` 只提供最短入口和链接，具体问题只读对应专题；README 主要服务用户，Agent 技术详情不要全部常驻。

## P2：文档一致性与维护

- [x] 清理 README 最后一行孤立的 ``` 围栏；检查 Markdown 渲染、链接与标题锚点。
- [x] 扫描陈旧表述（版本徽章、上一正式版本、尚未发布、待验收）；README / AGENTS.md / metadata.yaml / CHANGELOG 的当前发布基线已对齐为 0.7.1，三个运行源码标签也在 0.7.1 发布时统一。
- [x] 为关键技术声明标注事实来源：源码文件/测试/正式更新日志；过期或无法核实的描述删去或标明待验证。
- [x] 不为文档重构引入运行时依赖，也不顺手改动 ACP、认证、Provider 或 WebUI 行为。

## 验收标准

1. 全新 Agent 阅读 `AGENTS.md` 即可知道项目入口、安全边界、何时读取哪份资料；不用默认通读历史版本说明。
2. 用 #12 MCP permission、#13 数组 schema、#14 内容相关 429 作为样例，按照排障索引能快速找到正确代码及测试；不能把 429 一概称为配额耗尽。
3. 文档当前行为与 v0.7.0 源码及测试一致，历史说明不再伪装成当前实现；4.30 文案支持和 4.30 宿主兼容清楚区分。
4. 相关 Markdown 无孤立代码围栏、死链及显著过时标记；README 人类安装入口仍完整。
5. PR 仅包含文档变动；记录静态检查结果。没有执行测试/实机验证时不得宣称已通过。

## 建议提交与验收顺序

1. 先抽取当前有效安全契约与模块地图，核对代码和测试。
2. 新增 `AGENTS.md`、architecture / troubleshooting / testing 三份专题。
3. 精简 README 旧 Agent 段并添加入口链接，清理 Markdown 残留。
4. 交给独立 Agent 从三个真实 Issue 场景审读文档，检查是否遗漏约束或产生误导；人工审核后再合并。

相关背景：README Agent 折叠区、CHANGELOG v0.7.0、`docs/429-prompt-investigation.md`、`docs/pr9-acceptance.md`。旧 `docs/next-version-task.md` 是 0.5.0 历史施工书，不作为当前待执行路线图。
