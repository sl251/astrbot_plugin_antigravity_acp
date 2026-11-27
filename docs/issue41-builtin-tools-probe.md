# Issue #41 阶段一调查与只读原生工具实测报告

> 实测记录限定于下述 CLI、平台和场景。当前授权契约见[工具策略](api.md#工具策略)，验收边界见[状态索引](plan-status.md)。

日期：2026-11-26  
实测环境：Linux（Docker 容器 `astrbot`，宿主 `xiaoyu`）、官方 CLI `Antigravity ACP Server 1.5.18`（`--gemini-dir` + `--app-data-dir` 隔离，无宿主 MCP `mcpServers: []`、`settings.json={"toolPermission":"strict","agentMode":""}`）。

## 1. 实测结论摘要

1. **CLI `1.5.18` ACP 实际声明的内置工具（共 15 项）**：
   `run_command`、`read_file`、`edit_file`、`write_file`、`update_steps`、`manage_task`、`schedule`、`ask_question`、`search_web`、`define_subagent`、`invoke_subagent`、`manage_subagents`、`send_message`、`run_workflow`、`swarm`。
2. **候选只读能力对照结论**：
   - **`read_file`（可开放）**：在 Linux `1.5.18` 下严格保持 `tool_call(in_progress)` → `tool_call_update(pending)` → `session/request_permission(pending, allow_once/reject_once)` 事前授权时序；拒绝（`cancelled`）后立即返回 `failed` 且不泄漏文件内容；**无宿主 MCP（`mcpServers: []`）时同样正常工作**；支持可选分页参数 `StartLine` / `EndLine` / `ByteOffset`。
   - **`read_url` / `execute_url` / `browser`（不存在于 `agy acp 1.5.18`）**：模型明确反馈当前 ACP 会话未声明 `read_url`，实际内置仅保留 `search_web`。虽 CLI 二进制链接了历史引擎符号，但在 `agy acp` 服务端并未暴露，继续保留锁定 `deny`。
   - **目录浏览 / 文件查找 / 内容检索（`list_dir` / `find_by_name` / `grep_search`）**：在 `agy acp 1.5.18` 中**未提供独立只读工具**；要求模型执行列目录（`ls`）、查文件名（`find`）或内容检索（`grep`）时，模型全部退化为调用 **`run_command`** 执行任意 shell 字符串 `CommandLine`。按“无法可靠约束的能力不默认开放、终端能力暂不开放”边界，绝不能通过 `run_command` 白名单模拟只读搜索，继续保持拒绝。
3. **1.5.18 重要路径与版本细节**：
   - 官方 CLI `1.5.18` 更新说明提到修复了 `file:///` URI 的文件权限解析；实测传入 `file://<WS>/lines.txt` 时，初始 `tool_call` 的 `locations` 携带 `file://` URI，后续 `pending`/`permission_request` 的 `locations` 被 CLI 规范化为实际路径，而 `rawInput.Path` 保持原始 URI。插件侧 `read_target()` 继续要求规范路径与工作区直属 `.txt` 一致，不接受 `file:///` 形式以消除帧间漂移。
   - 原 `supports_read()` 硬编码仅 `1.5.17`，已扩展至实测通过的 `{"1.5.17", "1.5.18"}`（仍限 Linux；Windows / macOS 与未来未验证 CLI 版本保持 `unverified`）。

## 2. 工具能力与安全策略矩阵

| 工具名 | CLI 1.5.18 ACP 声明 | 实测授权机制 | 无 MCP 可用 | 隔离工作区约束 | 当前策略与说明 |
| --- | --- | --- | --- | --- | --- |
| `read_file` | 是 | 事前 `session/request_permission`（可取消阻止读取） | 是 | 顶层单硬链普通 `.txt`、同用户权限、可选 `StartLine/EndLine/ByteOffset` 校验 | **开放（`allow_once`）**：Linux + CLI `1.5.17` / `1.5.18`，计入 `tool_max_calls` |
| `search_web` | 是 | 事后观测 `tool_call`（无 `permission_request`，发现前请求可能已发出） | 是 | 仅限精确 `query` 字段与预算统计 | **实验性开关（默认 `deny`，可选 `allow`）** |
| `edit_file` / `write_file` | 是 | 事前 `session/request_permission` | — | 涉及写盘、符号链接与容量配额边界 | **固定 `deny`**（写操作单独评审） |
| `run_command` | 是 | 事前 `session/request_permission` | — | 接收任意 shell `CommandLine`，承担 `ls`/`find`/`grep` | **固定 `deny`**（命令执行暂不开放） |
| `schedule` / `invoke_subagent` / `define_subagent` / `manage_subagents` / `run_workflow` / `swarm` / `ask_question` / `update_steps` / `manage_task` / `send_message` | 是 | 后台调度、子代理、脚本、群体协作或交互控制 | — | 不可控副作用或非聊天语义 | **固定 `deny`**（统一纳入后台 `KNOWN_BUILTIN_NAMES` 拒止） |
| `read_url` / `execute_url` / `browser` | 否（1.5.18 未暴露） | 仅见历史二进制/规则表符号 | 否 | 无实际可用声明 | **固定 `deny`** |
| `list_dir` / `find_by_name` / `grep_search` | 否（退化为 `run_command`） | 经 `run_command` shell 执行 | 否 | 退化为任意命令执行风险 | **固定 `deny`** |

## 3. 本轮代码落地对应项

- 统一注册表：[builtin_tools.py](../agy_provider/builtin_tools.py) 新增 `BuiltinToolSpec`、`BUILTIN_SPECS` 与 `KNOWN_BUILTIN_NAMES`，统一管理 `read_file`、`search_web` 与 1.5.18 全部拒止内置工具。
- 放开 CLI `1.5.18` 与分页参数：`supports_read()` 允许 `{"1.5.17", "1.5.18"}`；`read_target()` 支持 `StartLine`、`EndLine`、`ByteOffset` 严格整数范围校验，并在 [tools.py](../agy_provider/tools.py) / [transport.py](../agy_provider/transport.py) 校对更新帧与权限帧参数一致性。
- 无宿主 MCP 解耦：[provider.py](../agy_provider/provider.py)、[sessions.py](../agy_provider/sessions.py)、[process.py](../agy_provider/process.py) 与 [tool_turn.py](../agy_provider/tool_turn.py) 在开启总闸且启用任一原生工具时，不再要求 `snapshot.tools` 非空，直接以 `default_agent` + 空 `mcpServers: []` 挂载 `ToolBroker` 并记录 `tools` 模式，不启动多余 `mcp_bridge.py` 子进程。
- 非搜索拒止文案修正：[transport.py](../agy_provider/transport.py) 在 `tool_broker is None` 时按工具区分提示，只有 `search_web` 返回联网搜索已发出风险提示，其余内置工具返回准确的未授权内置工具拒止提示。

## 4. 平台与未验证边界

- **Linux (`1.5.17` / `1.5.18`)**：已完成 ACP 真实探针，`read_file` 与 `search_web` 均可在有/无宿主 MCP 下按契约运行。
- **Windows / macOS / 未来 CLI 版本（如 `>=1.5.19`）**：未完成实机权限探针，`read_file` 保持 `unverified` 锁定拒绝；待后续实测补证后再逐版本开放。
