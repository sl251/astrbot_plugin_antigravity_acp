# WebUI 运行监控与上下文观测面板：开发计划 / TODO

> 状态：待开发（仅设计文档，未修改功能代码）  
> 基线：`main` 的 v0.6.1（2026-11-16）；目标：下一功能版本，版本号由维护者定。  
> 设计原则：**把已经有的数据可视化；监控必须只读、低开销、零模型请求；不猜测未上报的额度或上下文。**

## 0. 为什么做

当前 `views/setup/` 已实现完整的原生 AstrBot WebUI 向导：①运行环境、②Google 账号认证、③模型与思考档位、④已启用实例。`/agy cost` 有成本、缓存与会话复用摘要，但用户无法从面板直观看到：

- 我到底用掉多少上下文？当前模型报告的窗口是多少？
- 哪个模型、哪个运行模式更耗 Token？缓存究竟有没有命中？
- 会话为什么重建、历史回放裁了多少、429 后有没有进入冷却？
- AGY CLI、ACP 会话池、工具桥目前工作状态如何？

本任务以**现有面板升级**为主，不另起独立站点，不替换 AstrBot 模型管理，也不靠轮询 `agy` 生成内容测活。

## 1. 当前代码基线（已核对）

| 现成能力 / 数据 | 代码来源 | 复用方式与限制 |
| --- | --- | --- |
| 环境、CLI 版本、握手探测、登录、模型目录、已配置实例 | `agy_provider/onboarding.py` 的 `snapshot()`、`providers()`；`views/setup/app.js` | 保留原有配置流程，不把“已通过上次检测”伪装成实时在线 |
| 每实例成本、复用、重建原因、回收原因、模式分组 | `agy_provider/provider.py:instance_cost_snapshots()` → `telemetry.summarize()` | 内存内最近 24h / 最多 100 轮；重启清空 |
| 进程池 | `sessions.py:pool_stats()` | 已有 `active_sessions / max_sessions / live_processes / active_tool_turns` |
| 单轮统计：Token、缓存、延迟、warm/cold、`prompt_bytes`、`replay_bytes`、`omitted_messages`、错误码 | `telemetry.py` 的 `agy.turn` 记录 | 不直接暴露原始日志；后端生成白名单摘要 |
| 上下文占用与窗口 | `context_usage.py` 的 ACP `usage_update.used/size`；`TurnMeter.context` | `context_used / context_window_size` 可能为 `null`，仅表示当前观测，不是账户额度 |
| 压缩检测 | `ContextUsage` 的“峰值至少 64000 且下降到阈值以下”启发式 | 仅标“疑似”，不能宣称上游确已压缩 |
| WebUI 状态轮询和主题 | `views/setup/app.js` 每 5s 轮询 `setup/status`，操作时更快；`style.css` 自带暗色主题 | 复用外观/桥接，但监控接口与登录等操作隔离 |

`history_bytes=160000` **是冷启动/重建时历史重放的 UTF-8 字节预算**，不是上下文窗口，更不是 Token 数；复用成功时通常仅发当轮消息。`context_compaction_min_peak=64000` 只是压缩检测条件。

## 2. 页面信息架构

保持现有单页面入口，改为顶部“**运行概览｜用量与上下文｜会话与诊断｜配置与登录**”四个视图/页内区块。原来的 01～04 向导完整保留在“配置与登录”，已有书签与 AstrBot 插件视图入口不变。

### 2.1 运行概览（第一屏）

- [ ] **运行状态**：插件已加载、最近一次环境检测及其时间、CLI 版本、ACP 状态（“最近检测通过 / 未检测 / 检测失败”），不要用静态 `passed` 误报“实时连接正常”。
- [ ] **实例卡片**：provider ID、模型 ID、chat/tools/inherit、是否启用、工具总闸实际生效状态、思考档位（有可验证来源才显示）。
- [ ] **会话池卡片**：活跃会话数/上限、存活进程数、进行中的工具续轮数；按实例展示，不跨实例混算上限。
- [ ] **最近 24h 摘要**：实际发送轮数、成功/失败、复用率、缓存读取占比、已知输入/输出 Token、未知 usage 轮数。未上报的 Token 显示“未知”，不并入 0。
- [ ] **最近异常**：只显示安全错误码（`QUOTA` / `RATE_LIMIT` / `EMPTY` / `TOOL_PERMISSION` / `TIMEOUT` 等）及相对时间；不展示消息文本或调用参数。
- [ ] **刷新状态**与采样时间：可手动刷新；自动刷新仅本地读统计，默认约 10 秒，隐藏页面暂停；不能启动 `agy acp` 请求。

### 2.2 用量与上下文（本次重点）

- [ ] **上下文进度条**：`context_used / context_window_size`，显示已用/总量、百分比、采样时间、模型与实例；分母为 null/0 时显示“上游未报告”，不画 0%。
- [ ] **每会话数据边界**：只展示有当前有效采样的实例/会话；明确区分最近一次轮次记录和仍在运行的会话。不同模型或重新建 session 后不得沿用旧窗口。
- [ ] **压缩提示**：“检测到疑似上下文压缩”+命中时间；解释是启发式判断，不能写“已压缩成功”。
- [ ] **Token 消耗趋势**：最近 24h 最多 100 轮的 input_other、input_cached、output，区分请求状态、模型、chat/tools；无数据给空态。
- [ ] **缓存表现**：已报告缓存读取的占比、缓存字段缺失轮次；字段缺失 ≠ 缓存为 0。汇总统计口径与 `/agy cost` 保持一致。
- [ ] **历史重放**：每轮 `prompt_bytes`、`replay_bytes`、`omitted_messages`；标明字节不等于 Token、复用时 0 回放不是空上下文。
- [ ] **用量口径说明**：“模型上下文占用 / 本地已观测 Token / Google 订阅配额”分别是三件事。**Google 账户剩余次数、每周额度、真实计费金额没有可靠来源时一律显示‘不可用’，不得估造。**

### 2.3 会话与诊断

- [ ] **会话复用分析**：warm/cold 比率、最近重建原因与频次（`system_changed`、`host_trim`、`model_changed`、`no_identity` 等按实际枚举）、平均/中位输入成本对比（仅限有样本时）。
- [ ] **故障分布**：最近 24h 失败类别、失败已报告 Token、`RATE_LIMIT/QUOTA` 冷却状态（冷却具体到期时间只有后端可靠提供才展示），避免把 429 当成确定的账号封禁。
- [ ] **资源情况**：进程池/活跃工具轮数、会话驱逐原因和次数、`compaction_turns`、`fast_abort_turns`。
- [ ] **最近事件表**：时间（客户端本地时区）、模型、模式、状态、错误码、延迟、复用/重建、Token、上下文采样；限制 20 行并允许展开“无敏感信息的详情”。
- [ ] **诊断入口**：可跳转/复制 `/agy doctor`、`/agy cost` 命令；手动执行 doctor 是显式操作，**不能**在页面轮询时自动触发握手/登录/模型调用。
- [ ] **一键复制诊断摘要**：仅脱敏版本、状态、代码、统计口径和时间戳；不包括 user prompt、system、session ID、OAuth/授权链接、代理凭证、工具参数、路径中的身份信息。

### 2.4 配置与登录（保持功能）

- [ ] 原来的环境检测、认证、模型选择、思考档位、chat/tools 实例切换功能**原样工作**；监控接口异常不能导致登录/保存不可用。
- [ ] 保留浅色/深色/自动主题与现有卡片视觉；适配窄屏 360px / 390px、平板和桌面，图表不溢出。
- [ ] 在重要配置旁显示说明（例如历史回放预算、session_reuse_mode、cost_log_enabled）；**首版只读**，不要临时增加“清会话、强制重启、修改配额”等危险按钮。

## 3. 后端接口与数据契约

建议新增独立**只读** Web API：`GET /astrbot_plugin_antigravity_acp/monitor/status`，前端通过 `bridge.apiGet("monitor/status")` 调用；不要把大量轮次数据直接塞进现有 `setup/status` 的 5 秒向导轮询响应。

- [ ] `onboarding.py` 或新模块 `agy_provider/monitoring.py` 注册并清理监控路由；沿用 `request.username` 校验、AstrBot 管理面板鉴权以及 `Cache-Control: no-store`；权限需做到**仅管理员可见**，如宿主接口无法可靠区分管理员，则阻止开放明细，先验证权限模型。
- [ ] `provider.py` 增加监控专用聚合入口，从已激活 provider 的 Runtime/Telemetry 取**原子或一致性快照**；不要执行 `probe`、`test`、`session/new`、`set_model` 或其他真实 AGY 请求。
- [ ] `telemetry.py` 提供 `safe_recent_turns(limit=20)` 之类只返回白名单字段的方法；不向客户端返回原始 `history` 对象、提示词、session_key/native_session_tag、tool schema、执行参数等。
- [ ] 服务端归一化状态码、数字、采样时间和 `null`；固定响应版本 `schema_version=1`，便于后续扩展。
- [ ] 按 provider ID 分组。若实例未激活，返回“已配置但运行时未初始化”；不能将该实例与另一实例的统计拼接。
- [ ] 明确定义采样寿命：内存中的最近 24h / 100 轮按实例保留；无真实 `usage_update` 就 `context_window_size=null`，页面刷新不会改变这个事实。
- [ ] 限制响应体积/记录数量，轮询只走内存读取；前端请求失败显示上一次快照的“已过期”标识，而非继续显示“运行正常”。
- [ ] 默认不持久化监控历史；`cost_log_enabled=false` 时仪表盘依然能看**当前内存聚合**，但重启后记录归零。

建议响应轮廓（字段名可调整，开发时固定契约并写测试）：

```json
{
  "schema_version": 1,
  "sampled_at": "ISO-8601 UTC",
  "window": {"hours": 24, "max_turns_per_instance": 100},
  "instances": [
    {
      "provider_id": "agy_chat",
      "model": "upstream-model-id",
      "effective_mode": "chat",
      "pool": {
        "active_sessions": 1,
        "max_sessions": 4,
        "live_processes": 1,
        "active_tool_turns": 0
      },
      "summary": {
        "request_sent_turns": 12,
        "reuse_rate": 0.75,
        "unavailable_usage_turns": 2,
        "cache_token_ratio": 0.61
      },
      "latest_context": {
        "context_used": null,
        "context_window_size": null,
        "observed_at": null,
        "source": "not_reported"
      },
      "recent_turns": []
    }
  ]
}
```

上述数值仅作契约示例，**不是任何用户账号的实测记录**。

## 4. 分阶段 TODO（建议按顺序实施）

### P0：数据可信 + MVP（第一批交付）

- [ ] P0-1 后端新增只读监控聚合接口、管理员鉴权、白名单过滤、响应大小限制。
- [ ] P0-2 提供有效上下文样本的 `used/size/observed_at` 语义；修正“历史最后一轮数据”被误当成“当前活跃上下文”的情况。
- [ ] P0-3 接入实例、模型、模式、会话池、24h Token、usage 缺失、复用率、重建原因。
- [ ] P0-4 WebUI 首屏总览 + 上下文占用进度条 + 最近故障/复用状态 + 刷新时间。
- [ ] P0-5 加载/空态/无权限/过期数据/上游未报告状态，保持登录与模型配置功能可用。
- [ ] P0-6 为接口契约、权限、采样清理、未知 usage、实例隔离增加自动化测试。

### P1：历史分析 + 故障定位（第二批）

- [ ] P1-1 最近轮次趋势图、成本构成（uncached/cached/output）及失败消耗统计。
- [ ] P1-2 warm/cold 对照、重建原因分布与本地 history replay 字节/裁剪次数。
- [ ] P1-3 最近 20 轮脱敏事件列表，支持按实例、模型、chat/tools、成功/失败筛选。
- [ ] P1-4 诊断摘要复制（敏感数据回归测试）、错误码提示与可操作修复建议。
- [ ] P1-5 对齐 `/agy cost` 的数字口径，尤其 `unknown usage`、缓存字段缺失、疑似压缩。

### P2：体验与可维护性（第三批，可选）

- [ ] P2-1 图表、响应式布局、暗色主题、筛选条件持久于当前页面会话（不存敏感内容）。
- [ ] P2-2 可配置低频自动刷新 / 手动刷新；后台标签页停止轮询；停止/重载后卸载路由与定时任务。
- [ ] P2-3 按实例的观察性告警阈值（如复用率连续偏低、错误率升高），只在面板本地提示，不加模型请求、外部通知或自动重试。
- [ ] P2-4 更新 README 截图/使用指南、字段词典、CHANGELOG；完成真实 AstrBot WebUI 验收。

## 5. 修改范围建议

| 文件 | 预期变更 |
| --- | --- |
| `agy_provider/telemetry.py` | 采样时间、脱敏的最近轮次投影、上下文有效期、统计摘要 |
| `agy_provider/provider.py` | 按运行实例导出监控快照，不跨实例混账 |
| `agy_provider/onboarding.py` / `agy_provider/monitoring.py` | GET 只读接口、管理员权限与响应契约、生命周期清理 |
| `agy_provider/context_usage.py` / `transport.py` | 核实 ACP `usage_update` 来源与采样状态，必要时补 observed_at，不改计费含义 |
| `views/setup/index.html` / `app.js` / `style.css` | 总览、选项卡、图表、表格、低频轮询和响应式 |
| `tests/test_telemetry.py` / `tests/test_onboarding.py` / 新增监控测试 | 单元、契约、隐私、无 AGY 调用、生命周期回归 |
| `README.md` / `CHANGELOG.md` | 功能完成后更新用户文档；**当前文档提交不宣称已实现** |

## 6. 必过验收清单

- [ ] 生产机上打开面板、静置 10 分钟，**没有**额外 AGY 生成、`session/new`、token 费用或 CLI 登录请求；轮询只有只读本地 API。
- [ ] A/B 两个实例分别有统计且不会串会话；模型切换/重启/登录状态变化不会展示旧模型的上下文窗口。
- [ ] 上游 `used/size` 缺失、无 usage、缓存字段缺失、空会话、只有失败轮等情况，均准确显示“未知/未报告”，无伪造 0。
- [ ] 触发 429、EMPTY、TOOL_PERMISSION 等失败时，最新事件与 `/agy cost` 的统计互相对得上；429 不被误报为封号。
- [ ] 模拟重建、宿主历史裁剪、疑似压缩、进程回收：原因显示正确；疑似压缩只作提示。
- [ ] 未授权 / 非管理员请求无法取得成本及事件明细；响应和复制内容中无 prompt、OAuth、完整 session 标识、敏感环境变量或工具参数。
- [ ] 360px 手机、桌面、浅/深主题可读；无左右横向滚动；刷新中的任务不会覆盖用户正在进行的登录/保存操作。
- [ ] Python 离线测试通过；AstrBot 4.29.4 + 实际 AGY CLI 的完整面板冒烟和原有登录/切模型回归通过。

## 7. 不在本计划内

- 不提供 Google 账号“剩余额度、周额度、美元费用”的推测值；除非官方稳定暴露可验证接口并单独评审。
- 不自动缩减 `history_bytes`、改复用策略、清理会话、关闭工具或切换模型。监控先解决“看得见”，性能优化另开任务。
- 不通过代理/抓包/逆向接口读取 Google 配额；不收集 OAuth Token、用户消息、系统提示词。
- 不替换 `/agy doctor` / `/agy cost`：命令保留作为无 WebUI 场景的入口。

## 8. 完成定义

**P0 完成**即可让用户在现有 WebUI 一眼看到“哪个实例在运行、最近是否报错、用了多少已知 Token、复用是否正常、上游有没有报告上下文窗口”；**P1 完成**后才算具备排查“为什么 21 小时烧完周额度”的完整可观测能力。P2 属于产品打磨，不阻塞 P0/P1 上线。

> 实施时每个 P 阶段单独 PR、测试和验收，先交付 P0 再逐步扩展；这份文档只是 TODO，不代表功能已开发。
