# agy_acp 0.5.0 任务书：会话复用与额度成本治理

评审基线：0.4.3，提交 `7aad59b`。本文是下一版施工设计，本次只新增本文档，不修改代码、配置或测试，不部署。代码行号均指此基线。

## 目录

1. [背景与实测证据](#1-背景与实测证据)
2. [根因分析](#2-根因分析)
3. [改动清单与施工方案](#3-改动清单与施工方案)
4. [版本与兼容迁移](#4-版本与兼容迁移)
5. [测试计划](#5-测试计划)
6. [量化验收与灰度](#6-量化验收与灰度)
7. [明确不做的范围](#7-明确不做的范围)
8. [待主控确认的开放问题](#8-待主控确认的开放问题)

## 1. 背景与实测证据

插件将官方 Antigravity CLI（agy）封装为 AstrBot LLM provider，服务 QQ 机器人“小鱼”。线上运行于腾讯云上海容器，目录名 `astrbot_plugin_antigravity_acp`，用户确认与本仓库逐字节一致。已有“21 小时烧光一周额度”的反馈，作者已承诺优化会话复用、提高缓存命中。本版目标是减少重复输入与无效请求，并让每轮成本可解释。

以下为用户提供的实测事实，原样引用；本次没有重新执行线上查询或 AGY 生成：

| 来源 | 实测证据 | 可得结论 |
| --- | --- | --- |
| 线上 AstrBot `data_v4.db`，`provider_stats` 的 `agy_probe` 行 | `token_input_other`：33,946 / 36,162 / 36,499 / 40,679 / 40,740；`token_input_cached` **全部为 0**；`token_output` 17–46 | 这些样本每轮 3.4–4 万 input，缓存一次都没命中；输出短不能说明请求便宜 |
| 本机 `agy -p "只回复:ok" --output-format json` | 默认 agent（全部内置工具）：`input_tokens 10861`、`output 32`；自定义 agent `tools: []` + `excludeDefaultComponents: true`：`input_tokens 405`；同一会话第二轮：`input 13152 / cache_read 8807`，约 67% 命中 | 工具模式将底座从 405 抬到 10,861；真正复用时缓存可以生效。这是指定实验的底座数，不是所有 ACP 请求的固定收费 |
| headless 日志统计 | 一个 272 字符会话日志中，`streamGenerateContent` 共 286 次，其中 113 次为 `Monitor sir_flash_lite`；每个工具步骤触发安全评估，无法用配置关闭 | 工具步骤有不可忽略的内部调用放大；调用次数不能直接换算成主模型 token 或额度 |
| 线上 `data/config/astrbot_plugin_antigravity_acp_config.json` | `enable_tools: true`、`history_bytes: 160000`、`idle_ttl: 600`、`max_sessions: 4`、`max_concurrent: 2`、`thought_level: low`；模型 `gemini-3.8-flash` | 大重放预算、工具路径、回收策略需要一起检查；低思考档位没有解决输入成本 |
| 线上错误与回落记录 | 曾出现 `AgyError("EMPTY")` 后回落 `newapi/glm-5.2`；失败的 agy 请求同样消耗 input tokens | 必须记录失败成本，抑制对同一失败请求的重复调用；回落并不退还前一次消耗 |

## 2. 根因分析

### 2.1 已证实的执行链

1. **身份入口**：`agy_provider/provider.py:222–227` 对 `[session_id, conversation_id]` 做哈希。两者均缺失时 key 为 `None`；`sessions.py:273–274,186–194` 使用随机临时会话并在结束时清理。`host_compat.py:123–134` 还会给缺身份的 runner 分配每次 reset 独立的 run ID。此路径不具备跨轮复用条件，不能用群号或最近会话兜底，否则可能串上下文。
2. **system 精确哈希**：`messages.py:73–91,127–130` 将 `system_prompt` 与 contexts 中 system/developer 合并；`messages.py:55–57` 对完整字符串计算哈希。动态记忆、时间或其他内容只要改变字符串，就改变哈希。
3. **全部条件同时成立才复用**：`sessions.py:277–288` 检查客户端存活、system/model/effort/tool_hash 一致、broker 去重记录少于 4000、expected 非空且 `expected == request.hashes[:-1]`。`messages.py:51–53` 的哈希覆盖规范化后完整消息对象。
4. **不满足即销毁重建**：`sessions.py:293–301` 对完整请求调用 `prompt_blocks`，随后 `open()`；`sessions.py:214–242` 关闭旧会话、重建 home/进程并 `session/new`。`sessions.py:91–105` 会删旧 home、清空 expected。
5. **历史重放**：纯文本由 `messages.py:133–167,272–279` 的 `replay()` 包装成 `astrbot_history` JSON；工具/图片历史走 `messages.py:280–330`。它是引用文本重放，不是原生 ACP 历史恢复，也不是把每条旧消息各生成一次。上限 160000 是重放文本的字节预算，**不含独立写入的 system、默认 agent 底座和宿主工具 schema**。
6. **成功提交锚点**：`sessions.py:354–356` 将完整 `request.hashes` 加清洗后的 assistant 答案哈希存入 expected；命中时 `sessions.py:289–292` 只发送本轮最新消息。工具续轮由 `tool_turn.py:224–229` 更新同一个 request 的工具交换与 consumed_text，不能简单认定工具对话必然哈希不一致。

**结论**：用户提出的“判据失败 → open → 新 home/进程 → 历史重放”链路成立。若线上 system 每轮变、身份每轮变或宿主持续改写历史，则对应请求必然走冷启动路径。但仓库没有真实逐轮请求快照/重建原因，五条 usage 不能单独证明“线上每一轮都是冷启动重放”，也不能定位究竟先失败于哪个判据。应将其列为首要假设，用本版日志闭环，不能在发布说明中冒充已证实事实。

3.4–4 万不是“AGY 固定自带几万提示词”的证据。10,861 的工具底座之外，system、重放历史、工具 schema/结果和上游内部生成均可能贡献输入；具体占比须记录输入组成并核对实际 usage。

### 2.2 必然失败条件与偶发失效

“必然”指条件一旦出现即不复用，不代表线上每轮一定出现该条件。

| 条件 | 分类与代码证据 | 处置结论 |
| --- | --- | --- |
| 无稳定身份、每轮新 run ID、provider 实例重新创建 | 此调用模式下持续无法跨轮复用；`provider.py:134–136,222–227`、`host_compat.py:123–134` | 修宿主身份传递；不能猜身份 |
| system 内容每轮变化 | 每次变化必然失败；`sessions.py:281` | 分离已知来源的瞬时数据；未知 system 改动仍重建 |
| 宿主删除、裁剪、摘要替换、编辑历史，或回传 assistant 文本被改写 | 每次 expected 不一致必然失败；`sessions.py:287,354–356` | 分类诊断；编辑、遗忘、摘要语义变化必须重建 |
| 插件自己的 `prompt_blocks` 省略旧历史 | **并非下一轮必然失败**：expected 仍记录未裁剪的完整 request | 区分宿主逻辑历史与实际发送窗口，不把两者混为一谈 |
| abort、EMPTY、QUOTA、超时、流中断 | 事件性失效；`sessions.py:362–365` 所有未完成轮关闭 | 保持关闭；不复用半轮上下文 |
| idle 回收、池满 LRU、进程死亡、重载 | 事件性失效；`sessions.py:168–177,186–194,367–379` | 记录原因；不要直接无限加 TTL/池大小 |
| model、effort、工具内容变化；broker.seen ≥ 4000 | 事件性强制重建；`sessions.py:282–285` | 保留隔离，单独计数 |
| 仅工具列表顺序变化 | 当前也会失败；`tools.py:263–311` 保留列表顺序后哈希 | 对工具集合稳定排序；描述/参数/权限真实变化不能忽略 |

### 2.3 成本观测盲区

`provider.py:51–69` 按当前 ACP 约定映射互斥桶：`input_other = inputTokens + cachedWriteTokens`，`input_cached = cachedReadTokens`。缺失 cached 字段当前会按 0 处理，因此数据库的零值还需要结合原始字段是否存在核验；不得把 headless 的 snake_case 字段直接套入 ACP。

只有 `provider.py:395–406` 收到 done 后才向宿主交 usage。`sessions.py:324–348` 在 EMPTY/告警-only QUOTA 时提前抛错；`transport.py:467–481` 在 result 含 error 或异常 stopReason 时也先抛错。于是已收到的 usage 可能丢失，宿主成功记录不足以覆盖失败成本。`request_max_retries` 在 provider 签名中接收/透传，但现有代码没有据此执行插件重试；需区分宿主重试、CLI 内部重试和插件行为。

## 3. 改动清单与施工方案

按收益/风险排序为 A → B → C → D → E；**实施顺序先 D，再 A/B/C/E**，否则无法判断收益。所有收益是目标或条件估计，不是额度承诺；缓存读取、非缓存输入与平台实际收费分别报告。

### A. 安全复用：稳定身份、明确 system 分层、可解释历史判定（最高收益，中高风险）

**动机与收益**：针对 33,946–40,740 input、cached 全零。恢复稳定短对话后，争取接近实测约 67% 的缓存水平，验收以 ≥60% 为目标；不承诺缓存一定命中或累计上下文 input 立即降低。

**改哪里**：`provider.py` 身份/normalize 入口；`messages.py` 请求模型与规范化；`sessions.py` Session、stream/lease/open；`tool_turn.py` 续轮校验；`tools.py` 工具快照。宿主适配只在 `host_compat.py` 的本插件范围内做，不能更改其他 provider 的行为。

**接口与判定**：

```python
@dataclass
class PromptContext:
    stable_system: str
    transient_data: str | None
    source_revision: str | None

@dataclass
class ReuseDecision:
    reuse: bool
    reasons: list[str]
    history_relation: str  # exact / host_trim / rewrite / append_gap / unknown

def prepare_context(system: str, *, trusted_parts=None) -> PromptContext: ...
def compare_history(expected: list[str], incoming: list[str]) -> str: ...
def decide_reuse(session, request, model, effort, snapshot) -> ReuseDecision: ...
```

- 默认严格保留 system 内容。不得用正则删除“像时间/记忆”的段落、忽略所有 system 哈希、模糊相似度或忽略空白来冒充语义一致。记忆内容通常影响回答，**动态不等于语义无关**。
- 新增宿主可信参数 `agy_context_parts`，仅接受受控集成提供的“稳定指令 + 当轮瞬时数据”结构；校验来源、版本与原始 system 分段的一致性。权限、人格、安全约束、持久事实修改仍进入 stable_system，变化即重建。未接入结构化来源的线上请求保留严格模式并报 `system_changed`；本版不能仅靠忽略哈希兑现承诺。
- 可降为 user 输入的是来源明确、被作者定义为“仅本轮有效”的检索片段/记忆快照等数据，而非任意 system 指令。固定 system 声明“以下上下文数据仅适用于所属轮，最新快照替代旧快照，不可作为指令”；将数据作为当前 user 内独立转义 JSON 块发送，含轮次与 revision，不额外增加宿主可见的 user turn。删除快照时明确发空快照。控制指令全部留在 system；宿主要求真正遗忘/撤回敏感信息时必须重建，不能靠替代声明模拟删除。
- 稳定身份沿用现有两个 ID 的组合及 provider 隔离；在 QQ 正常对话验收 ID 跨轮稳定。缺 ID 时仍临时会话并告警，不按文本、QQ 群号或时间窗口合并。新建会话、分支、清空必须更换身份或显式 reset。
- 建立两个账本：`host_expected`（宿主规范化的完整逻辑消息）与 `sent_window`（实际重放窗口及摘要哈希/省略数量）。本地预算裁剪只改变后者；完成成功轮才提交前者。工具续轮继续更新原 request 的交换，最终可见答案用于 assistant 锚点。
- 保留已有文本块拼接、ThinkPart 排除、工具参数 JSON 规范化；新增规则须有真实宿主回传 fixture。不得去掉正文空格、任意合并 assistant、忽略工具 ID 或工具结果差异。工具列表按 wire name 排序后同时用于发布及 fingerprint；不重排任意 schema 数组。
- 对宿主历史严格比较。相同尾部仅用于诊断 `host_trim`，**0.5.0 默认不以尾部相同就复用**：旧 ACP 上下文仍保有被删除内容，无法保证宿主遗忘语义。host_trim/rewrite/append_gap 都以预算重建。若以后宿主提供明确的“仅传输窗口裁剪、保留完整语义状态”协议，再评审有稳定消息 ID 的窗口复用；本版不放开。

```python
context = prepare_context(raw_system, trusted_parts=trusted_host_parts)
request = normalize_host_messages(...)  # transient 数据不污染 host_expected
with session_key_lock:
    decision = decide_reuse(...)
    if decision.reuse:
        blocks = render_current_turn(request.latest, context.transient_data)
    else:
        plan = plan_replay(request.messages, budgets)
        await open_with_stable_system(context.stable_system, snapshot)
        blocks = render_replay(plan, context.transient_data)
    # 工具轮冻结 context revision，续轮变更拒绝；下一用户轮才可更新
    result = await generate_and_observe(blocks)
    commit_host_expected_only_if_success(result)
```

**配置与 schema**：新增 `session_reuse_mode`，string，`strict|structured`，默认 `strict`；description：“会话复用模式”，hint：“structured 仅用于已接入可信上下文分层的宿主；未知 system 变化仍重建，禁止忽略人格与权限变化。”对既有无分层请求，structured 也回到严格比较。仅开启开关不会自动解析记忆插件。

**风险/回滚/怎么验**：分层可能改变指令优先级或保留过时事实，必须通过来源契约和语义测试再给“小鱼”开启。默认 strict；异常时切 strict 并清理本插件运行会话，保留日志。验收比较 native session ID/home 创建次数、实际第二轮 prompt、宿主答案回传后的 expected；不能只测布尔函数。需覆盖记忆撤回、人设修改、工具执行中 revision 变化与跨会话隔离。

### B. 双重历史预算与明确裁剪策略（高收益，中风险）

**动机与收益**：160000 字节允许冷启动重放大量历史，是 3.4–4 万 input 的可控放大项。默认降到 **32768 字节（32 KiB）**，另加 **6000 估算 token** 约束。字节上限下降约 80%，不等于总 input 或费用也下降 80%；短聊天路径总 input 中位数目标 ≤12k，工具路径单列。

**改哪里/接口**：`config.py`、`_conf_schema.json`、`messages.py:replay/prompt_blocks/normalize`，`sessions.py` 消费预算结果。system 上限从 history_bytes 解耦，避免下调历史预算顺带拒绝现有长人格。

```python
def estimate_text_tokens(text: str) -> int: ...
def plan_replay(messages, *, byte_limit: int, token_limit: int) -> ReplayPlan: ...
# ReplayPlan: blocks, kept_turn_ids, omitted_messages, encoded_bytes, estimated_tokens
# 当前问题完整保留；按完整 user turn 从旧到新移除，直到最终编码同时满足双限。
```

无经过校准的 tokenizer 时，先用 UTF-8 字节数作为保守的文本 token 预算代理，标记 `estimate_method=utf8_upper_proxy`；它会多裁剪，不宣称是精确 token 上界。后续可用本地 tokenizer/离线样本校准，不为估数调用模型。检查最终 JSON 转义、包裹文字、瞬时数据后的大小，避免只估原始消息。图片另记数量/字节，token 无可靠估算则 unknown；不把 base64 长度当模型 token。

默认**保尾，不自动保最早 user 头部**：人格/稳定规则由独立 system 保留；任意旧 user 内容不应永久固定。完整工具调用与结果随所属 user turn 一起保留或省略；当前问题超限抛 `INPUT_LIMIT`，不静默截断。省略情况显式标记；本版不调用模型生成摘要。历史预算只约束本次重放/输入，不能清理已复用 ACP 的累计上下文；若长会话累计成本持续上涨，先按日志评估再单独设计轮换，不自动每 N 轮强制冷启。

**配置与 schema**：

| 项 | 默认与校验 | `_conf_schema.json` description / hint |
| --- | --- | --- |
| `history_bytes`（变更） | 32768；仍允许 4096–160000 | “历史重放字节预算”；“当前输入完整保留，按完整轮保留最近历史；与 token 预算取更严限制” |
| `history_tokens`（新增） | 6000；0 或 1024–32000 | “重放文本估算 token 预算”；“0 关闭估算限制；估算不含 agent 底座、独立 system、工具 schema 与图片 token，非计费承诺” |
| `system_bytes`（新增） | 160000；4096–160000 | “系统提示字节上限”；“与历史预算独立，过大直接拒绝；建议精简人格，实际长度写入成本日志” |

**风险/回滚/怎么验**：旧细节被省略，保守估算可能过度裁剪；用固定中文、英文、代码和工具交换 fixture 验证可见性与边界。回滚为 `history_bytes=160000, history_tokens=0`，不需要改动宿主历史；system_bytes 默认保留旧容忍度。不保证因预算而省略的旧消息能被模型回答。

### C. 明确区分聊天与工具 provider（高收益，低至中风险）

**动机与收益**：实测底座 10,861 对比 405；从工具默认 agent 转纯聊天的底座差额为 10,456 token，约 96% 的底座降幅，并非整轮降幅。工具模式还附加宿主 schema，以及无法配置关闭的 Monitor 调用。

**现状修正**：`provider.py:248–252` 只在 enable_tools 且 tool_choice=auto 时建立非空快照；`sessions.py:218–240` 仅 snapshot.tools 非空时用默认 agent。`process.py:266–285` 注明已验证的 AGY 1.5.2 自定义 agent 无法把 session MCP 工具传给 LLM，故不能直接把工具 agent 改成 `tools: []` 并声称等价。

**方案/改哪里**：同一 provider 类型下建两个不同 ID 的实例，如 `agy_chat`、`agy_tools`，继续使用各自 Runtime，不新建协议、不用模型自动判断是否需要工具。新增 provider 级 `custom_extra_body.agy_tools_mode = inherit|chat|tools`，默认 inherit；解析函数：

```python
def resolve_tools_mode(plugin_enabled: bool, provider_mode: str,
                       tool_choice: str) -> bool:
    # plugin enable_tools 为总闸；chat 永远不传工具。
    # tools 不能绕过总闸或 tool_choice=none；无非空 schema 则仍用 chat agent。
    ...
```

修改 `provider.py` 快照构造和历史校验、`onboarding.py` provider 配置保存/展示、`main.py` 能力声明与 `/agy use` 使用说明。保留插件全局 `enable_tools=false` 默认（当前已经是 false），线上已有 true 不强制覆盖。新用户向导推荐 chat 实例；有需要的人格/会话固定选择 tools 实例，沿用宿主选择器或 `/agy use`，同一 ACP 工具轮中不能切模式。chat 实例收到工具历史应给出新建纯聊天会话的明确提示，不能静默丢工具记录。

**配置/schema 文案**：`enable_tools` hint 增补“总闸；非空工具集会启用默认 agent，实测底座明显高于纯聊天”。provider 级字段不是插件顶层配置，需在向导及 provider 配置说明中明确写入位置、默认 inherit、选项含义；Settings 不新增一个与其冲突的全局路由开关。保留 `tool_max_calls=16` 默认，工具实例可在后续按实测降限；不能把降低上限宣传成关闭 Monitor。

**风险/回滚/怎么验**：chat 不具备工具能力，切换不应伪装成功执行；双实例会各占池容量，必须展示每实例池/进程统计，不自动翻倍 max_sessions。回滚 provider mode 为 inherit；原 enable_tools 生效。验证 chat 的 argv 含 `--agent astrbot-chat`、tools 非空路径不含该参数、空工具路径仍走 chat；工具能力不能由单个 kwargs 绕过总闸。对 schema 顺序的稳定化由 A 实施。

### D. 成本与重建原因日志、自检（必要前置，低风险）

**动机与收益**：五条 cached 全零但缺重建原因；EMPTY 成本不可见。直接节省 0 token，目标是 100% 请求可追踪、失败不漏记、同轮不重复计数。

**改哪里/接口**：新增 `agy_provider/telemetry.py`（下一版实施时），在 `transport.py` 收到响应时先捕获 usage，在 `sessions.py` 决策和清理处记录原因，在 `provider.py` 统一逻辑轮 ID，在 `main.py` 增加只读管理员命令 `/agy cost`。

```python
class TurnMeter:
    def observe_usage(self, result: dict, *, source: str) -> None: ...
    def finish(self, *, status: str, error_code=None) -> None: ...  # 幂等

def classify_rebuild(session, request, tombstone) -> list[str]: ...
```

原始 ACP usage 存白名单数字桶及字段存在性，仍按基线互斥桶契约计算 input_other/cached/output；通过真实脱敏 ACP 样本锁定口径，若 CLI 变为累计计数则按同一 prompt 的累计快照去重，不能把更新与终态相加。headless 实测只作对照。收到 result 后在异常判定前 observe；错误响应的可信数字同样采集。无 usage 的中断记录 null 和 `usage_status=unavailable`，不可伪报 0。不直接写 AstrBot 数据库，不用伪造成功 LLMResponse 记录失败。

重建原因完整集合：`cold_start/no_identity/system_changed/history_host_trim/history_rewrite/history_append_gap/abort/idle_expired/pool_evicted/process_dead/tool_hash_changed/tool_ledger_limit/model_changed/effort_changed/previous_error`。记录全部命中原因，另定稳定优先级选 primary。回收时在 Runtime 保存最多 1024 条、TTL 一小时的原因墓碑；下次获取同 key 可查 abort/idle/池淘汰，而不是全部写成 cold_start。重载后无墓碑则记录 runtime_restart 或明确 unknown。`local_history_trim` 是输入裁剪事件，不冒充复用失败原因。

建议每个完整 ACP prompt 终态输出一行 JSON，工具交接/续轮共享同一 turn_id：

```json
{"event":"agy.turn","v":1,"provider":"agy_chat","turn_id":"opaque-id","session_key":"opaque-hmac","mode":"chat","reused":true,"rebuild_reasons":[],"history_relation":"exact","prompt_bytes":80,"replay_bytes":0,"system_bytes":420,"tool_schema_bytes":0,"omitted_messages":0,"input_other":4200,"input_cached":8800,"output":32,"usage_status":"reported","usage_source":"acp_terminal","status":"ok","error_code":null}
```

此行数字仅为格式示例。补充 `model/effort/native_session_tag/elapsed_ms/cache_fields_present/estimate_method/tool_calls/request_sent`；不记录提示词、答案、原始异常、用户 ID、凭据或完整工具参数。身份用运行期带密钥 HMAC/随机映射，避免低熵 ID 普通哈希可被反查。预检查失败或短路也有终态行，`request_sent=false`、本次新增 usage 为 0；初次失败成本用 origin_turn_id 关联，不能重复累计。

`/agy cost` 不发模型请求，仅汇总本实例最近 100 轮有界内存记录：复用率、缓存 token 占比、input_other/总 input 中位数、失败且有 usage 的消耗、未知 usage 比例、重建原因排行、有效预算/路由、回收次数与当前进程数。启动以来样本不足和缺失字段明确显示。日志用于跨重载汇总；现有 `/agy doctor` 无生成语义保留。

**配置/schema**：新增 `cost_log_enabled: bool=true`，description：“记录每轮成本与重建原因”，hint：“仅结构化数值与脱敏标识；覆盖失败，未知 usage 不按零计”。关开关只关闭逐轮日志，保留有界内存自检；回滚可关日志，无需改变请求行为。测试每条 JSON 可解析、必需字段齐全、同轮一次终态、失败先采 usage、无敏感内容；目标终态覆盖率 100%。

### E. 失败去重与短期熔断（中收益，低至中风险）

**动机与收益**：EMPTY 后回落仍已支付本次 input。若避免一次相同冷请求重试，可避免再付约 3.4–4 万 input（条件估计，不是失败退款）；受保护窗口内重复请求新增上游调用数应为 0。

**方案/改哪里**：在 `provider.py` 规范化后、创建进程前调用 Runtime 共享的 `FailureGuard`；逻辑轮级状态与 `tool_turn.py` 对齐，不能把工具续轮当重复请求。`errors.py` 扩展安全 retry_after 元信息，不回传原始上游内容。

```python
async def check_failure_guard(scope, request_fingerprint, logical_request_id): ...
def record_failure(scope, fingerprint, code, *, origin_turn_id, retry_after=None): ...
# 指纹含 provider/稳定会话、全部规范化历史、system、瞬时数据、model/effort、工具指纹。
# 检查与首次请求注册在同一会话锁内，避免并发穿透。
```

- 插件继续不自动重试 EMPTY/QUOTA，也不因 request_max_retries 增大而自行重放。EMPTY 只缓存**失败标记** 30 秒：若有宿主稳定逻辑请求 ID，同 ID 精确去重；无 ID 时仅同会话同完整指纹短期抑制，注明可能抑制用户有意重复的问题。缺会话身份且无请求 ID 不跨用户做内容去重。
- QUOTA 目前混合额度不足与容量不足（`errors.py:111`，以及告警过滤），先采用 provider 实例 + model 范围的 60 秒熔断；保留结构化来源信息，可信 retry_after 可延长但上限 300 秒。范围不扩展到其他 provider，不读取账号凭据建全局号池。到期只放一个半开探测，由真实用户请求触发，成功恢复，失败续期。
- 不缓存空答案/部分答案作为成功，不返回旧答案骗取命中，不重放状态未知的工具副作用。已交接工具的取消仍沿用现有 unknown/禁止重放约束。
- 短路抛安全错误，附 origin_turn_id 与剩余等待秒数，允许宿主按既有策略回落；在可用宿主接口上标记不应重试本 provider，但不承诺能阻止宿主所有调用。CLI 内部 286 次调用无法由此消除，不修改其安全评估机制。

**配置/schema**：新增 `empty_dedupe_ttl: int=30`（0–300），“EMPTY 相同请求抑制秒数”；新增 `quota_cooldown: int=60`（0–300），“额度/容量错误冷却秒数”。hint 均注明“0 关闭；不缓存成功答案，不退款，工具副作用不重放”。失败表有界 1024 条且按 TTL 清理。

**风险/回滚/怎么验**：短冷却可能暂时阻止已恢复的模型，范围过大影响无关请求；需测不同会话/provider/model 隔离和半开并发。回滚两 TTL 为 0。用可控时钟和 fake CLI 验证 EMPTY 首次调用 1 次、重复 0 次、过期恢复；工具续轮不拦截，失败 usage 只计首次，QUOTA 同范围并发最多一个半开。

## 4. 版本与兼容迁移

版本 **0.4.3 → 0.5.0**。后续施工修改 `metadata.yaml`、CHANGELOG、README、向导配置说明与 schema；此次不改这些文件。维持 provider type、插件目录名、用户登录方式与 AstrBot `>=4.29.4,<4.30` 边界，未验证前不扩大兼容区间。

**默认值不等于已存值**：旧配置已显式保存 `history_bytes=160000`，仅改 dataclass/schema 默认不会让它变成 32768。方案如下：

1. 新安装：history_bytes=32768、history_tokens=6000、system_bytes=160000；enable_tools 仍 false；reuse strict；成本日志启用。
2. 升级：保留已有 history_bytes 和 enable_tools，不猜 160000 是默认还是用户有意设置。新增 history_tokens 缺省取 6000，因此旧 history_bytes=160000 下仍会受到新 token 预算限制，启动日志/向导必须明确告知这是行为变化，而非“完全不变”。
3. 向导提供明确的“应用推荐预算”操作，显示旧/新值，仅把 history_bytes 改为 32768、history_tokens 改为 6000；通过宿主正常保存接口持久化。无法区分框架补齐值与原始文件值时，展示有效值即可，不推断用户意图。
4. “小鱼”上线清单：备份部署配置；采用推荐预算；按人格创建/选择 chat 与 tools 实例；保持模型 gemini-3.8-flash、thought_level=low；接好可信动态上下文来源后才开 structured；重载并用 `/agy cost` 确认有效值。此清单是下一版部署步骤，本次不执行。
5. 保留 idle_ttl=600、max_sessions=4、max_concurrent=2；只有发现 idle/LRU 是主要原因且容器内存有余量，再由作者调整。双 provider 要评估总进程数。
6. 回滚：恢复配置备份及 0.4.3 发布包，重载插件；无数据库迁移、无历史格式改写、无全局认证变更。单项灰度先关 structured/冷却，恢复 inherit 与旧预算；完整回滚才撤去代码新增行为。

## 5. 测试计划

本次为只读评审与文档交付，未运行 pytest，避免缓存文件等额外写入；不得写“测试已通过”。现有测试以 `unittest.IsolatedAsyncioTestCase`、mock 与 `tests/fake_cli.py` 为基础，可由 pytest 收集。下一版按下表补行为级测试，不只比对实现内部变量。

| 文件/建议新增位置 | 必补场景与断言 |
| --- | --- |
| 新增 `tests/test_sessions.py` | 两轮完全一致前缀只 open 一次，第二轮仅发送新输入；稳定 native ID；无身份每轮新建；不同会话/provider 不串上下文；同 key 并发串行 |
| 同上 + `tests/fake_cli.py` | system 真实改变必须重建；仅可信瞬时数据变、删除快照可复用且发送新数据；未知格式退严格；撤回/人格修改重建；工具续轮上下文变化拒绝 |
| 新增 `tests/test_messages.py` | 本地历史裁剪后完整宿主前缀仍可复用；宿主裁剪/摘要/编辑必须重建并分别记原因；文本块/ThinkPart/规范 JSON 等价；正文空白真实差异不可忽略；assistant 回传不一致不能强行命中 |
| 同上 | 中文/ASCII/转义包裹双预算边界，当前输入超限报错；完整工具交换不截半；图片不以 base64 估 token；独立 system_bytes 不被 history_bytes 降低连带改变 |
| `tests/test_acp.py` + 新增会话测试 | abort/timeout/进程 EOF 后无复用、无孤儿进程；idle/LRU/工具 ledger 上限保留原因；相同 schema 顺序变化不重建，真实参数/描述变化重建 |
| 工具/宿主适配测试（新增独立文件） | 多工具续轮结束后 expected 与宿主历史一致；不重复 native generate；权限/审批回调不被跨轮替换；chat/tools 参数与隔离正确，chat 拒绝旧工具历史 |
| `tests/test_upstream_notice_filter.py` | 保留现有告警过滤、EMPTY/QUOTA、清洗答案 expected 用例；新增失败仍记录 usage，不将原始告警/正文写入日志 |
| 新增 `tests/test_telemetry.py` | 成功、EMPTY、QUOTA、transport error、取消、无 usage、缺 cached 字段、工具多次交接、重复终态；JSON 字段齐全，usage 不重计，unknown 不当 0，脱敏 |
| 新增 `tests/test_failure_guard.py` | 精确重复短路、TTL、独立 provider/model、无身份不跨用户抑制、并发半开一次、工具续轮不拦截、失败表有界、短路新增 input 为 0 |
| `tests/test_auth_config.py`、`tests/test_onboarding.py`、`tests/test_commands.py` | 新默认/范围/schema 一致；旧 160000 保留但新 token 限制生效；推荐迁移幂等且保留工具总闸；非管理员不能查成本；`/agy cost` 不触发生成 |

实施者先跑相关定向测试，再 `pytest -q`；向导有变更时再运行现有 `tests/setup.test.mjs` 对应前端检查。真实 CLI 验证需另设少量样本预算，不把在线生成放入自动单测。0.4.3 现有通知过滤和权限拒绝回归必须保留。

## 6. 量化验收与灰度

指标必须注明口径，不能把“模型缓存 token 占比”和“原生会话复用率”都叫缓存命中率：

- `total_input = input_other + input_cached`；缓存 token 占比 = Σinput_cached / Σtotal_input，仅统计字段齐全且口径确认的请求；另报 unknown 数量和覆盖率。不要将 headless input/cache_read 的定义未经核对混入计算。
- 原生复用率 = reused 请求数 / 可复用候选请求数；候选为稳定身份、同模型/模式/稳定 system、前轮成功、未 idle/淘汰的后续用户轮。全流量复用率与剔除原因同时给出，不能只挑有利样本。
- input 中位数同时报告 total_input 和 input_other；复用让非缓存输入下降，不保证累计上下文总量下降。工具流量、失败流量、冷首轮分别分组；usage 不代表 Monitor 全量计费账单。

**发布门槛**：

1. 离线确定性场景：连续 20 个稳定短对话轮只新建一次；所有明确改变语义的场景 100% 按规则重建/拒绝，无跨身份复用和工具重复执行。
2. 线上灰度选择至少 3 个会话、累计 ≥100 个用户轮，固定 model/effort，记录配置和 CLI 版本；其中稳定后续轮 ≥50 个。主要观察稳定短对话（无图片、无工具调用，稳定 system ≤2000 估算 token），原生复用率 ≥90%，缓存 token 占比 **≥60%**，总 input 中位数 **≤12k**。长累计上下文可能不满足 12k，必须单列分布，不通过剔除失败/未知来制造达标。
3. 工具组同模型独立对照，报告 10,861 底座之外的 system/schema/重放组成及 actual usage；以稳定后续轮非缓存 input 相对匹配的 0.4.3 重建对照下降 ≥50% 为目标。达不到先查是否实际复用及字段口径，不把 chat 成绩冒充 tools 成绩。
4. 每次调用（含短路和失败）都有终态成本记录；每次重建有原因；收到 usage 的 EMPTY/QUOTA 留存率 100%；因上游未返回而未知的样本明确计数。短路窗口内相同失败请求新增 `session/prompt` 为 0。
5. `/agy cost` 无生成，能回答“为什么重建、输入从哪里来、哪些失败仍消耗”；不泄露提示词/凭据；进程数不超过实例配置，取消与回收无泄漏。

先部署日志与严格模式采基线，再启用预算/chat 分流，最后对接 structured；至少观察一个实际高峰时段。缓存由上游控制，若原生复用达标而 cached 仍全零，阻止“缓存已修复”的发布结论，检查 ACP usage 字段、CLI 版本及上游缓存条件，不能继续放宽历史正确性换数字。

## 7. 明确不做的范围

- 号池、跨账号调度：扩大额度来源，不能修复单请求重复输入，还会引入身份隔离与运维复杂度。
- 上下文破限：预算治理是减少输入，不绕过模型上下文限制；不静默截断当前问题。
- ToS 规避、关闭/绕过内部安全 Monitor、修改 CLI 内部计费逻辑：不属于插件适配职责，也不是已验证的成本优化接口。
- 本版不做持久化原生进程恢复、模糊历史复用、自动模型摘要、自动意图工具路由、自动换模型或回放未知状态工具调用。它们会扩大正确性风险，先交付可核验的复用、预算、分流和观测。

## 8. 待主控确认的开放问题

以下是发布取舍，不阻塞本文施工设计；未确认前按文中保守默认实施：

1. 新用户向导是否明确默认推荐纯聊天？建议是；插件 enable_tools 原本已默认 false，线上已有 true 是否主动改由作者决定，不静默改。
2. “小鱼”动态 system 的具体来源由哪个插件/宿主钩子提供？需作者指定可信分段契约与负责人。若只能拿到拼接后的字符串，0.5.0 保持 strict，不宣称已解决该来源的每轮 system 漂移。
3. 老用户是否接受新增 history_tokens=6000 立即限制重放？建议接受并显著提示；若要求完全保持旧行为，则旧配置迁移为 0，只在“应用推荐预算”时启用，相关验收按实际配置分组。
4. “记忆删除”是否必须保证模型彻底不再保有旧内容？建议默认是，触发重建；只有纯检索快照更新才适用瞬时数据替代语义。
5. 60 秒 QUOTA 冷却和 30 秒 EMPTY 去重是否符合宿主回落体验？是否能取得宿主稳定请求 ID，以减少无 ID 时的重复问题误抑制？
6. 灰度实际流量、进程内存预算与工具组 token 目标是否批准？若工具不可关闭，应单独接受工具底座及 Monitor 的客观成本，不用纯聊天 405 的实测承诺工具模式费用。
