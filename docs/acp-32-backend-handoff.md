# ACP #32 后端与 #30 面板交接

> 历史交接记录（#32 / PR #34）。当前路由和工具恢复契约见[接口](api.md#模型管理与路由)、[架构](architecture.md#无工具收尾与超时恢复)。延期与验收见[索引](plan-status.md)。

基线为 `0a393de2ff88a8972cbe4849705548d79fbe43ae`。目标宿主为 AstrBot 4.29.4，插件版本仍为 v0.7.5。本交接记录实现与待验收契约。测试未执行，按用户要求由Hydrogen负责。

## 面板接口

所有路径以 `/astrbot_plugin_antigravity_acp/` 开头。沿用宿主的 `json_response` / `error_response` 包装。下表描述成功包装内的业务数据。设置和监控均要求已登录、用户名匹配宿主配置的 Dashboard 管理员，拒绝 API key 身份。响应带 `Cache-Control: no-store` 和 `Referrer-Policy: no-referrer`。所有设置 POST 都要求 `application/json`、至多 16384 字节及快照颁发的 `csrf`。

| 路径与方法 | 请求 | 业务数据与完成条件 |
| --- | --- | --- |
| GET `setup/status` | 无 | 原设置快照，增加 `routing` |
| POST `setup/check`、`login`、`login-code`、`login-cancel`、`apply` | 原契约 | 原快照。apply 继续单独验证模型目录与档位。保存完成的 result 含 `provider_id, model, tools_mode, default_selected, provider_state`，最后一项取 `created` 或 `existing` |
| POST `setup/provider-impact` | `{csrf, provider_id}` | `{impact}`，同步只读，无须确认，不改变上一项操作的结果 |
| POST `setup/provider-default` | `{csrf, provider_id, confirm:true}` | 排入设置任务，返回快照。按精确 ID 选择已启用且已加载的 ACP 实例 |
| POST `setup/provider-toggle` | `{csrf, provider_id, enabled:boolean, confirm:true}` | 同上。启用使用宿主配置和实例加载接口。4.29.4 的停用请求受扫描边界阻止 |
| POST `setup/provider-delete` | `{csrf, provider_id, confirm:true}` | 同上。4.29.4 的删除请求受扫描边界阻止 |
| GET `monitor/status` | 无 | schema v2。保留 `sampled_at, window, instances`，增加 `routing, failure_events, failure_retention` |

新增操作严格校验字段集合，`provider_id` 是非空字符串，最长 1024 字符。列表中的模型名、来源 ID、近似 ID 均不能替代实例 ID。写操作使用首次设置的单任务槽，请轮询 `setup/status` 至 `busy=false`。成功要求 `action_error=""` 且 `result.applied=true`。任务受理的 HTTP 200 只表明取得快照。管理任务结果为 `{provider_id, applied, impact}`。影响检查建立前的异常可使 result 为 null。阻止破坏性操作时返回 `action_error="REFERENCE_BLOCKED"`、`applied=false` 和影响预览。非本地 runner 或路由不匹配为 `ROUTE_MISMATCH`，宿主接口、保存或补偿失败为 `HOST`。参数、非 ACP 目标等错误为 `CONFIG`。同步错误使用原错误包装，未登录 401、身份或 CSRF 不符 403、操作竞争 409、无效参数 400。

### 全局路由和影响范围

`setup/status`、`monitor/status` 返回同一 `routing` 投影，管理员显式发起的 `/agy doctor` 报告也带此字段。

| routing 字段 | 类型与语义 |
| --- | --- |
| `scope` | 固定 `global`。使用默认 profile 和空 UMO |
| `configured_primary_id, resolved_primary_id` | string 或 null。分别为本地 runner 主 ID、宿主 resolver 返回实例的 ID |
| `primary_exists, primary_enabled` | boolean 或 null。前者检查配置行，后者取配置启用位。行缺失时启用位为 null |
| `resolution_reason` | `configured_primary`、`implicit_first_provider`、`missing_primary`、`non_local_runner`、`unknown`。悬空主 ID 且宿主返回首个模型时显示首项隐式选择；没有可解析实例时显示 missing_primary |
| `is_acp` | boolean 或 null，指解析实例的类型 |
| `explicit_fallback_ids, invalid_fallback_ids` | string[]，按原顺序。校验备用项是否存在、启用并加载为聊天实例。未知或非本地时结合 reason 解读空列表 |
| `last_observed_provider_id, last_observed_at, last_observed_scope` | 最近一次插件观察到的、已发送 prompt 并成功结束的 ACP 回合，时间为 ISO UTC，范围为 `plugin_only`。插件启动后尚无样本时为 null、null、`unknown`。可包含管理员显式进行的真实生成。面板无法观察其他 provider 的服务时间或任一群的最终路由 |

设置全局默认使用 `manager.resource_lock`、`acm.default_conf` 和 `save_config()`。在锁内保存 `agent_runner.config.model.provider_id`，通过 `get_conf(None)` 回读，并校验原宿主 resolver 的实例身份。同步全局 `set_provider` 偏好后再次校验。宿主配置采用临时文件和原子替换。配置和 shared preference 属于两个存储，错误时补偿主 ID 与旧的当前实例，发生连续存储错误须人工核对。向导新建成功但默认选择失败时保留显式的 `created` 中间结果。已有实例的启用和设默认共用宿主锁。普通保存不写 runner 主 ID。已有会话覆盖、fallback 顺序和辅助引用不参与改写。向导发现多条匹配已有配置时要求管理员选择精确实例。

`impact` 的形状如下。计数是可见命中数，不能用于证明零引用。

```ts
type Impact = {
  provider_id: string
  enabled: boolean
  references: {
    primary: number
    fallback: number
    auxiliary: number
    observed_session_overrides: number
    global_resolved: boolean | null
  }
  profiles_scanned: number
  session_scan: "unknown"
  auxiliary_scan: "known_config_fields"
  can_delete: false
  can_disable: false
  block_reason: "referenced" | "incomplete_scan"
}
```

扫描默认配置和 `acm.confs` 中已加载的 profile，包含 runner 模型的主项、备用项、runner 配置和 provider 设置中的 provider ID 引用。通过宿主原生 provider 变更 hook 观察本次插件生命周期内发生的会话偏好选择，内存最多保存 1024 个使用随机密钥 HMAC 的会话标签，只返回目标实例命中数。启动前偏好、已淘汰记录、未加载 profile 和其他插件私有配置均可能不可见。4.29.4 没有在 provider 资源锁内完整枚举会话覆盖的契约。因此本次前端应按 `can_delete/can_disable` 展示阻止原因，不能以二次确认绕开。宿主原生删除页面仍可能留下悬空引用；本插件通过快照检测，管理员须显式重新选定目标实例。宿主 resolver 和宿主删除方法没有安装补丁。

## 请求、历史和防重放

- **历史与当前输入**。纯文本沿用 `replay()`。多模态和工具历史仅把过去的轮次放入转义 JSON 的 `astrbot_history`，当前用户文本和去重后的真实图片保持原顺序。按当前块总文本字节和历史封套字节裁剪，只能在用户轮边界裁历史。若显式构造含当前交换的转录，assistant 调用及 tool 结果在独立的转义 `astrbot_tool_exchange` 中；实际 retained continuation 仍只按已审计调用回填结果。
- **历史图片**。旧 URL 不下载，也不做 DNS 请求。旧本地路径通过授权根、逐级无符号链接打开和文件元数据检查，不读取历史文件正文。内存缓存按实例、会话和媒体来源关联已验证指纹，最多 512 项；本地键还含设备、inode、大小、mtime/ctime 纳秒。未知来源、文件改写、过期或缓存淘汰可使用独立的来源指纹，从而保守重建。历史占位不会进入本轮数量和字节预算。当前图片每次按原有路径/网络、MIME、结构、像素及总预算完整验证。缓存不能授权发送旧图。
- **fallback**。过滤后的 text/thought chunk、非空白正文、已流式交付的正文前缀和宿主工具交接分别计数。仅思考、空白、重试告警或零 chunk 失败可交给宿主显式 fallback。非流式已见可见正文前缀仍阻止重放。工具计数与实际 handoff 分开，已交接工具、带工具结果的续轮失败均保守阻止重放。4.29.4 runner 捕获普通异常后会继续遍历候选，兼容层在原有本插件请求边界阻止后续模型调用。不会根据答案的语义质量切换模型。
- **CLI 告警**。工具通知先调用 `end_segment(before_tool=True)` 冲刷待决文本，下一段从行首识别 retry、terminal 和 agent 终态行。保留本 prompt 的告警计数。普通正文内的引用不因包含错误关键词而丢弃。CLI 权限判定、MCP once-only、宿主工具执行契约及认证流程沿用原有实现。

## 运行观察与保留

`runtime_state="active"` 表示已初始化，setup 检查的 `passed` 仅表示该项检查通过。GET 轮询和 impact 不启动 CLI 或发起模型请求。监控 v1 客户端须容许 v2 扩展；前端读取 v1 时，新字段应显示未知。

`instances[].recent_turns` 保留原字段并增加以下数据。字节数是 provider 适配层收到的正文与思考 UTF-8 字节，包含空白。它们不能替代 token usage。未取得 ACP usage 的已发送请求继续显示未知。

| 字段 | 契约 |
| --- | --- |
| `turn_id, origin_turn_id, logical_request_tag` | 随机回合 ID、抑制来源回合 ID、可选的运行时 HMAC 请求标签。缺失为 null |
| `attempt, cli_retry_attempt` | 宿主候选 attempt 当前为 null。CLI 告警中观察到的数字单列，缺失为 null |
| `model, negotiated_model` | 请求的已知模型、ACP 报告可核对的选中模型。未知请求标签沿用空字符串；缺乏协商证据或收到模型变更通知时协商模型为 null |
| `chunk_seen, body_seen, body_streamed, tool_handed_off, tool_result_supplied` | 是否收到正文/思考块、出现非空白正文、是否交付流式正文前缀、是否观察到工具交接、是否收到工具结果续轮 |
| `body_bytes, thought_bytes, stop_reason, permission_denial_count, filtered_notice_count` | 字节数、白名单终止原因、本 prompt 的权限拒绝次数及流式过滤器识别的告警数。客户端未观测值为 null |
| `fallback_allowed, fallback_reason` | 插件对失败回合的重放边界。原因取 `visible_prefix`、`tool_handoff`、`tool_continuation`、`no_visible_prefix`；成功或取消等非候选状态为 null。真实宿主是否尝试或完成 fallback 仍需宿主证据 |

`failure_events` 为插件全局数组，最新在前。每行有 `recorded_at, status, error_code, mode, provider_tag` 以及上表的关联标签、计数、交接与 fallback 字段；不含模型名。可用 `instances[].provider_tag` 对应配置实例。provider 标签是使用本地随机盐的 HMAC，盐仅在私有文件中保存，从不返回面板。持久行不保存会话身份、会话标签、名称、聊天内容、工具参数/schema、认证信息或 URL。

`failure_retention` 为 `{ttl_seconds:604800, max_events:128, max_bytes:65536, storage_state:"available"|"unavailable"}`。全插件共用数据根下的 `failure-events-v1.json`，单份已提交快照上限 64 KiB。原子替换最多另有一份同上限的临时文件。启动重新校验投影并清理过期行，运行中定时到期清理，读取时也过滤过期行。停机期间的清理在下次启动执行。存储不可用时仍返回内存内的有效失败记录，状态标为 unavailable。关闭 cost log 不影响此功能。管理员可从 monitor 响应导出已投影的失败行和保留参数，接口不提供文件路径下载或原始会话导出。请勿把带 CSRF、登录票据的 setup 快照作为诊断导出。

## Hydrogen 验收接缝

| 项目 | 入口与对照 |
| --- | --- |
| A | 使用证据目录中的真实 4.29.4 resolver、set_provider 和配置保存方法。在默认、指定 profile、会话 override 三种情况下核对选择与重载；分别模拟非本地、缺接口、写盘失败及 resolver 不匹配。确认新建中间态、旧主 ID 补偿、备用顺序及辅助引用。悬空 ID 的对照要调用宿主原始删除；插件侧验收调用管理 API |
| A / #30 | 管理员与 API key、CSRF、体积、严格布尔值、未知/非 ACP ID、操作竞争及停用过程。对照显式主项、隐式首项、fallback、辅助、已观察的会话覆盖和零命中但 unknown。预览无副作用，删除/停用被拦，启用与按 ID 选默认可独立完成 |
| B / D | 分别走 prompt 与 contexts-only，历史图/工具历史、当前文本交错多图、同图重发、含标签样式的历史数据、工具 continuation；强制裁剪时整组调用和结果不能分离。对照历史 9 图当前 0 图与当前 9 图；覆盖失效 URL/路径、文件同路径改写、未改写时会话复用、显式重发时媒体校验 |
| C | 对流式及非流式分别注入零 chunk、thought、空白、CLI 告警、可见前缀、已 handoff 后失败和工具结果续轮失败。以真实 `_iter_llm_responses_with_fallback` 的候选调用次数为准，同时断言异常标志、工具实际执行次数和 telemetry |
| E | 通过真实 `_on_update` 喂无换行前缀、分片告警、工具通知、后续正常正文及终态。检查无告警文本泄漏、正常引用逐字保留、error_code 和 filtered_notice_count |
| F | 禁止 CLI、spawn、probe 和模型入口后连续轮询两个快照。cost log 关闭时失败、短路与取消写入；重启、TTL、条数/字节淘汰、损坏文件、不可写路径及管理员身份分别检查。向非投影字段植入敏感哨兵，确认 JSON、导出和磁盘均无原始内容 |

此前红例在 `../evidence/repro/test_incident_seams.py` 与 `test_review_findings.py`。旧测试中假定所有当前文本/图片引用也在历史 JSON 内、仅有 v1 固定键集合、或仅凭 set_provider 即选默认的断言需要按新契约核对。覆盖管理员配置、活动聊天实例列表、真实 resolver 的宿主桩由 QA 负责。离线结果、实机验证及 CI 应分别记录。

## 源码依据与回滚

宿主对照在 `../evidence/astrbot-v4.29.4/astrbot/core/`。使用 `provider/manager.py` 的 `set_provider`、`_resolve_using_provider`、变更 hook、`reload/update_provider/delete_provider`，`astrbot_config_mgr.py` 的 `confs/default_conf/get_conf`，`config/astrbot_config.py` 的 `save_config/_write_config_snapshot`，及 `agent/runners/tool_loop_agent_runner.py` 的两个 response iterator。捕获版 `../evidence/repro/host/host-manager.py` 与指定官方提交 `61335414433db721b3f7ecc93f90deafe0a59d78` 对应。

实现入口为 `onboarding.py / routing.py / monitoring.py / main.py`，请求路径为 `provider.py / messages.py / media.py / models.py / process.py / sessions.py / tool_turn.py / host_compat.py`，告警与观测为 `transport.py / notices.py / telemetry.py / failure_events.py`，除 main.py 外均位于 agy_provider。#32 §5 的争用、池锁和专用 agent 调查延期。回滚代码可恢复该基线对应后端文件并移出新增模块；旧版本忽略诊断文件。已显式保存的宿主主 ID 持久存在，如需回退路由，应在回退代码前显式选回仍启用的原实例。此交付未提交、推送、部署、合并或发布。
