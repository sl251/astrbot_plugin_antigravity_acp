# Antigravity ACP 0.5.0 实现说明

本次在 `0.4.3` 基线上按 `docs/next-version-task.md` 的 **A（安全复用）**、**C（聊天/工具分流）**、**D（可观测）**、**E（失败熔断）** 四节实施 `0.5.0`，显式**跳过 B 节**，并保持 `enable_tools: false` 与 `history_bytes: 160000` 默认值不变。

## 1. 修改与新增文件

### 新增模块与文档

- `agy_provider/telemetry.py`：单轮 JSON 遥测（`TurnMeter` / `TelemetryTracker`）、`extract_acp_usage`、`REBUILD_REASON_PRIORITY` / `pick_primary_reason`、HMAC 会话标识脱敏、1024 条 / 3600 秒 bounded tombstones，以及 `/agy cost` 的只读格式化报告 `format_cost_report`。
- `agy_provider/failure_guard.py`：`FailureGuard` 实现同会话/请求指纹 `EMPTY` 抑制（默认 `empty_dedupe_ttl=30`）与按 `(provider_id, model)` 的 `QUOTA` 冷却（默认 `quota_cooldown=60`，`retry_after` 封顶 300s + 单并发 half-open 探针），仅缓存失败标记，不缓存成功回答，容量封顶 1024 条。
- `tests/test_sessions.py`：Section A 离线单测与子进程回归。
- `tests/test_tools_mode.py`：Section C 离线单测与子进程回归。
- `tests/test_telemetry.py`：Section D 离线单测与子进程回归。
- `tests/test_failure_guard.py`：Section E 离线单测与子进程回归。
- `docs/impl-notes-0.5.0.md`：本实现说明。

### 修改文件

- `agy_provider/config.py`、`_conf_schema.json`：新增 `session_reuse_mode`（`strict|structured`，默认 `strict`）、`cost_log_enabled`（默认 `True`）、`empty_dedupe_ttl`（`30`，0–300）、`quota_cooldown`（`60`，0–300）；保持 `history_bytes: 160000` 与 `enable_tools: false` 不变。
- `agy_provider/errors.py`：`AgyError` 增加 `retry_after`、`origin_turn_id`、`no_retry` 字段；`remote_error` 增加结构化 `extract_retry_after`（封顶 300s）。
- `agy_provider/messages.py`：新增 `PromptContext`、`prepare_context`、`render_transient_block`、`attach_transient_data` 与 `TRANSIENT_SYSTEM_NOTICE`；`normalize` 接收 `trusted_parts` 与 `reuse_mode`。
- `agy_provider/tools.py`：`ToolSnapshot` 在预算校验与 `fingerprint` 计算前按 wire 名称排序，记录 `schema_bytes`，空工具集 `fingerprint=""`。
- `agy_provider/transport.py`、`agy_provider/mcp_bridge.py`：版本号升至 `0.5.0`；`AcpClient` 在 `session/update`、RPC error 与 terminal result 路径早于异常检查采集 `latest_usage` 并派发 `usage` 事件。
- `agy_provider/sessions.py`：新增 `SentWindow`、`ReuseDecision`、`compare_history`、`classify_rebuild`、`decide_reuse`；`Session` 分离 `host_expected`（与 `.expected` 同步）与 `sent_window`；`Runtime` 接入 `TelemetryTracker` 与 `FailureGuard`，记录淘汰 tombstone，在成功/失败/取消路径按单一语义提交 usage；短回路拦截（`no_retry=True`）不刷新失败 TTL、不误关可用会话。
- `agy_provider/tool_turn.py`：`ToolTurn.accept` 冻结 `system_structured`、`source_revision` 与 `transient_hash`；工具续轮共享所属 `TurnMeter`。
- `agy_provider/provider.py`：`DEFAULT_CONFIG["custom_extra_body"]` 增加 `"agy_tools_mode": "inherit"`，新增 `resolve_tools_mode`、`active_instances`、`instance_cost_snapshots`，透传可选 `agy_context_parts`，并接入 `FailureGuard` 与预检阶段 `TurnMeter`。
- `agy_provider/onboarding.py`、`views/setup/index.html`、`views/setup/app.js`：向导新增实例角色 `tools_mode`（默认推荐 `chat`），`chat` 实例不添加 `"tool_use"` 能力标记。
- `main.py`：`@register` 版本升至 `0.5.0`；新增只读管理员命令 `/agy cost`；更新 `/agy setup` 与 `/agy use` 提示。
- `metadata.yaml`、`CHANGELOG.md`、`README.md`：更新版本号与 `0.5.0` 变更说明。
- `tests/fake_cli.py`、`tests/test_auth_config.py`、`tests/test_commands.py`、`tests/test_onboarding.py`、`tests/setup.test.mjs`：补充离线场景与回归断言。

## 2. 关键取舍

1. **安全复用默认走 `strict`**：`session_reuse_mode` 默认为 `"strict"`，且即便设置为 `"structured"`，当宿主未提供合法的 `agy_context_parts` 或拆分文本与原始 `system` 不一致时，一律无条件回退到完整 `system` 严格比较，确保现阶段在线上不依赖未上线的 AstrBot 宿主钩子。
2. **双账本判断优先以 `host_expected` 为准**：本地 `history_bytes` 裁剪仅截断首轮重放的 `sent_window`，不误触发次轮无意义重建；仅当宿主侧历史真实发生裁剪、改写或跳跃时判定为 `host_trim` / `rewrite` / `append_gap`。
3. **chat 实例严格不可变隔离**：`agy_tools_mode="chat"` 恒走 `--agent astrbot-chat`（`tools: []`、`excludeDefaultComponents: true`），在 `provider` 入口拒绝历史工具续轮（`TOOLS_DISABLED`）；`agy_tools_mode="tools"` 受插件总闸 `enable_tools` 与 `tool_choice="none"` 约束。
4. **usage 宁缺毋假**：ACP 未返回 usage 时记录 `usage_status="unavailable"` 与 `null`，不推算、不填 `0`；流中 update 与结束 terminal 仅覆盖更新、绝不相加。
5. **只读 `/agy cost` 与失败哨兵短回路不重放**：`/agy cost` 仅汇总内存环形缓冲区与池状态；短回路拒发只读失败标记，不触发 CLI，也不刷新原有失败条目过期时间。

## 3. 未做或依赖宿主的部分

- **跳过 Section B**：按要求未修改 `history_bytes`（保持 `160000`），未新增 `history_tokens` 或 `system_bytes` 等文本裁剪预算与 tokenizer 配置。
- **宿主结构化注入**：AstrBot 核心当前尚未在模型调用中传递 `agy_context_parts`，因此 `structured` 模式目前仅完成了插件侧协议解析、校验与隔离注入能力，真实宿主联调待宿主侧支持后启用；当前线上保持 `strict`。
- **真实 CLI / pytest 环境**：离线环境未联网；本机 Python 环境未安装 `pytest`（测试通过标准库 `unittest` 与 Node `--test` 运行），也未执行针对线上真实 Google Antigravity CLI 账号的联网回归。

## 4. 验证方式

离线验证命令（仓库根目录执行）：

```bash
python3 -B -m compileall -q main.py agy_provider tests
python3 -m unittest discover -s tests -v
node --test tests/setup.test.mjs
```
