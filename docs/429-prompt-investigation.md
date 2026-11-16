# 内容相关 429：开源实践对照与插件处置方案

- 日期：2026-11-17
- 适用：`astrbot_plugin_antigravity_acp` 0.6.x（AstrBot 4.29.4 / 4.30 开发线，AGY CLI 1.5.x）
- 关联：#14（纯聊天仍携带宿主本地工具说明；596B 说明可独立复现 429）、#10（限流后停留 fallback，独立缺陷）
- 结论边界：本文区分【已证实】【合理推断】【未核实】。Google 从未公开承认“按 system prompt 字面量拦截”，因此机制部分按推断表述，不写成官方结论。

## 0. 摘要

1. **这不是普通配额 429。** 同一账号、同一模型、同一请求，只扰动 `systemInstruction` 中的特定字面文本，就能在 429 与 200 之间稳定切换。oh-my-pi、CLIProxyAPI、Antigravity-Manager 三个项目已各自独立复现；最强证据是 20 次请求、间隔 75 秒的重复 A/B（原句 429 → 改写 200 → 原句 429 → 改写 200）。
2. **没有项目找到可靠的客户端判据。** 假 429 的响应体与真配额 429 可以逐字节相同：`Resource has been exhausted (e.g. check quota).`，无 `details`、无 `RetryInfo`。因此不能靠响应文本自动区分，只能靠“同 system 复现 + 无输出”做启发式归因，而且这只是辅助特征，不是充分判据。
3. **没有项目靠“改 prompt”根治。** oh-my-pi 先改标签名，再改写 RFC 句子，再省略 `requestType`，每次都被新指纹追上，最终在 2026-11-14 暂停整个 Antigravity provider；CLIProxyAPI 从零宽混淆转向“删除优先”的可配置规则；Antigravity-Manager 持续增加清洗规则，但 429 仍会锁账号。
4. **dsh（DeepSeek Harness）的答案不是清洗，而是“不组合”。** prompt 与工具 schema 由同一次 `assemble()` 产出，工具插件关闭则其说明一起消失；`toolOrder` 点名未注册工具会在组装期直接抛错。ACP 协议没有工具清单下行消息，客户端无法核对 agent 的工具集，所以正确方向是“不说”或“通过 MCP 真的挂上”，而不是扫描文本猜。
5. **对本插件：v0.6.1 删除 596B 块的方向正确，而且这不是单纯“绕过”。** 该块宣称会话中存在 `astrbot_shell_session` 等本地工具，但纯聊天 / tools 模式的 ACP 会话都没有这些工具——删除它首先是在修正失实的能力声明。剩下的工作是把它从“精确匹配 4.29.4 文案”升级为结构化过滤，并把这类 429 的归因数据留下来。
6. **不建议做的事：** 零宽字符、改 User-Agent / `requestType`、对确定性 429 做长退避、删除整个 system、或在当前代码结构下强行实现“净化重试”。本期的 429 部分只做“可观测的候选归因”，不改冷却语义；真正要改冷却与重试，需要单独立项。

## 1. 现象与已有证据

#14 的受控对照（同一服务器、同一账号、`gemini-3.8-flash`、`enable_tools=false`）：

| 输入 | 结果 |
|---|---|
| 通用 system 28B + 固定短问题 | 成功，438 input tokens |
| 上述 596B 本地工具说明 + 同一短问题 | 429 × 2 |
| 中性 system 15,316B | 成功，3,935 input tokens |
| 中性 system 24,900B | 成功，6,131 input tokens |
| 完整 system 15,316B（含该块），不带历史 | 429 × 2 |
| 完整 system 移除该块，其余 14,068B 保留 | 成功，4,087 input tokens |
| 普通原生 print，同模型 / high | 成功，9,524 input tokens |

这组对照排除了“账号不可用”“请求太长”“只是 token 超限”三种解释，但没有取得后端真实的判定维度。上游报文只有：

```text
⚠ API error (attempt 2): RESOURCE_EXHAUSTED (code 429): Resource has been exhausted (e.g. check quota).
```

## 2. 开源项目怎么处理（按证据强度）

### 2.1 oh-my-pi（OMP）：四次“修复”，最后停用 provider

OMP 是证据链最完整的项目，时间线如下：

1. **2026-09-11，误判为真配额。** 其分类器把无结构 `details` 的通用 429 判成 `QUOTA_EXHAUSTED`，合成 30 分钟 backoff，并把凭据写入封锁表。维护者在 #11689 里承认：该响应体“carries no structured `ErrorInfo`/`RetryInfo`”，其实是 per-minute/per-region throttle，不是每日额度。
2. **2026-09-11～12，确认内容相关。** #11794 用 1KB–96KB 纯 `x` padding 全部 200，排除长度；逐行二分发现“单独每一行都 200”，组合才 429。同期 CLIProxyAPI #5751 给出更细的最小对照：`<system-conventions>` 单独 200，`RFC 2119: MUST, REQUIRED, ... OPTIONAL.` 单独 200，两者一起 429；截断到 `MAY` 又 200。这高度指向**字面文本组合或特征阈值规则**（具体服务端匹配算法未核实），不是单纯语义判断。
3. **2026-09-14，改标签名。** 提交 `10fb0ac325478b96982ae4b0e511475c1a72b0c8` 把三个 prompt 文件的 `<system-conventions>` 改为 `<conventions>`，随 v18.2.1 发布；v18.2.6 仍复现。
4. **2026-09-26，改写 RFC 句子。** 提交 `6274932f46` 把首行改为 `RFC 2119 keywords: ...`，语义不变；一个多月后 #18706（2026-11-10）报告这句话本身又成了新指纹。
5. **2026-10-20，省略 `requestType`。** PR #11849 合并，去掉 `requestType: "agent"`；但这只对 OMP 自建 envelope 有效，不代表官方 `agy` 的行为。
6. **2026-11-14，停用 provider。** 提交 `9a37046d3a`（`feat(auth): pause Google Antigravity`）在 KDL 中写入：

   > `disabled "Google Antigravity is paused: Google's terms do not allow third-party sign-in and Google has suspended accounts that used it (see #18341). Use GEMINI_API_KEY (AI Studio) or google-vertex instead."`

   关联 #18341（403 `PERMISSION_DENIED` / ToS violation）与 #19223（再次暂停）。

**可借鉴的：** 错误分类不能把无结构 429 一律当 quota；响应体缺失 `RetryInfo` 时应保守地当瞬态。**不可照搬的：** 它自己造 `requestType` 与 envelope，而本插件走官方 `agy`，改不了也不该改。**要记住的：** 逐句改写是猫鼠游戏，社区担忧对抗升级会增加账号风控风险（2026-11 有 403 `TOS_VIOLATION` 封禁波，但因果尚未证实）。

### 2.2 CLIProxyAPI：把“改哪句”下放给配置

CLIProxyAPI 明确定性为“上游对 systemInstruction 的内容过滤”，并拒绝把关键词硬编码进核心（#7049：上游经常改，硬编码很快过期）。它提供三层配置：

- `sensitive-words`：在 system / message 中插入不可见字符混淆。最初用 U+200B；#7433 报告上游开始先剥离 U+200B 再匹配后，实现改为 U+034F。
- `sensitive-words-replace`：字面替换。
- `sensitive-patterns-replace`：有序 Go RE2 正则替换。`config.example.yaml` 明确写 “deleting flagged text with `""` is recommended over obfuscation for Antigravity”，并直接给出 OMP 两个指纹的删除规则（`^RFC 2119...`、`<system[-_](conventions|directive)>...`）。

分类上，Antigravity 专用 `decideAntigravity429` 默认 `soft_retry`，只有命中结构化 `ErrorInfo` 或 `quota exhausted` 连续词才升级为硬配额；但它仍保留 `requestType: "agent"`，通用 429 路径也仍可能冷却凭据，实际靠 `disable-cooling` / `request-scoped-errors` 手动规避。

**可借鉴的：** 规则可配置、删除优先于混淆、不要把上游指纹写死。**警示：** #7540 报告者指出，使用混淆后账号出现 `403 TOS_VIOLATION`（因果未证实），并认为“deleting flagged text is clearly safer than disguising it”；项目 `config.example.yaml` 也持相同倾向。

### 2.3 Antigravity-Manager：清洗已知第三方提示词，但 429 仍锁账号

它处理过多类触发：历史消息里的 `You are Codex, an agent based on GPT-5.`（#3442）、自身 sanitizer 留下的畸形标点（#3915）、Kelivo 的 `<shell>` 57 字符句（#3965）、OpenCode 的 `tools.opencode.session_move`（#3975）、Pi Agent 的 `codemode` 单词（#3981，改成 `codem0de` 即 200）。

做法是：身份归一化、标点折叠、第三方固定句清洗、WebUI 可配置的 prompt rewrite。但 `policy.rs` 对所有 429/529 仍返回 `RateLimited`，而 `should_lock_account()` 对 `RateLimited` 返回 `true`；只靠“无 `retryDelay` 的 429 首轮 GraceRetry、attempt 0 不锁账号”来防连坐。

**可借鉴的：** 请求级 429 不连坐、结构化信号（`QuotaFailure` / `RetryInfo`）优先于关键词。**结论：** 它也没有做到“自动识别假 429 并豁免冷却”。

### 2.4 dsh / ACP 生态：组合即声明，不靠文本清洗

- DeepSeek Harness 的 system prompt 是注册表：prompt section 与 tool schema 同一次 `assemble()` 产出；`toolOrder` 点名未注册工具会在组装期直接抛错，**错误声明在组装期就失败，不会发给模型**。
- `dsh-acp-enhanced` 的 `minimal` preset 通过 `cordis.patch.yml` 逐条 `disabled` 掉基座所有 model-facing 行，做到“宿主层零泄漏”；它不扫描宿主文本。
- ACP 直到 2026-11-06 才稳定 `sessionCapabilities.systemPrompt`（`append` / `override` 两级能力）；在此之前客户端只能走 `_meta`、厂商扩展或把 `[System]` 拼进 user message。ACP 没有“工具清单下行”消息，客户端唯一能影响工具集的通道是 `session/new` 的 `mcpServers`。

**结论：** 主流 ACP 实现没有一家做“宿主自由文本的工具名白名单清洗”。它们要么不让宿主文本进入 agent，要么通过 MCP 真的把工具挂上。这与本插件的处境最接近，也最值得借鉴。

## 3. 机制判断

| 判断 | 强度 | 依据 |
|---|---|---|
| 特定 `systemInstruction` 字面文本可让 Cloud Code Assist 返回 429，扰动后 200 | 已证实（第三方受控实验） | #11794 padding 对照与逐行二分；#12655 重复 A/B；CLIProxyAPI #5751 最小 2 行复现 |
| 响应体与真配额 429 无法区分 | 已证实（第三方） | #11689：无 `details` / `RetryInfo`；#12029 维护者明确说“byte-identical” |
| 触发是字面量组合/阈值，不是语义 | 合理推断（多源一致） | 单独每行 200、组合 429；截断即恢复；改无关 XML 句无效；具体算法未核实 |
| 上游存在 WAF/指纹规则，并故意伪装成 429 | 合理推断（Google 未承认） | 三个项目独立复现；官方论坛只解释 60s 滚动窗口 / TPM |
| OMP 的 `requestType: "agent"` 结论适用于本插件 | 未核实，且不应假设 | 本插件走官方 `agy`，envelope 由 CLI 决定；#12655 明确提醒两条链路不可假设相同 |
| 本插件 596B 块的具体触发 anchor | 未核实 | 只知道“移除整块恢复”，没有做逐句/组合二分 |

一个必须保留的竞争假设：Antigravity 官方文档说配额“与 agent 完成的工作量相关，逐 prompt 不同”。所以“某个 prompt 更容易 429”也可能只是它让 agent 干了更多活。但 #11794 的同长度 padding 对照和 #12655 的重复 A/B 已经把“长度/工作量”这条路排除到很小。

## 4. 对本插件的方案

### P0：保持“删除失实声明”，但升级识别方式

现状 `agy_provider/system_prompt.py` 用 `_LOCAL_TOOL_BLOCKS` 精确匹配 AstrBot 4.29.4 的 5 种完整文案；未匹配时原样保留，只打 debug。优点是零误杀，缺点是**宿主一改文案就静默失效**——而失效方向恰好是最坏的方向（把声明发给模型）。

建议改为“结构化优先、精确文案兜底”：

1. **识别 AstrBot 的结构化标签。** AstrBot 4.30.0-alpha.1 已把 computer-use 说明包进：

   ```text
   <execution_environment runtime="local">
   ## Shell and sessions
   ...
   ## Workspace
   ...
   </execution_environment>
   ```

   固定引用（commit `6a782b7be6591aa36b3e30f9e836c7c061c9ae50`，`v4.30.0-alpha.1` 已包含）：`https://github.com/AstrBotDevs/AstrBot/blob/6a782b7be6591aa36b3e30f9e836c7c061c9ae50/astrbot/core/astr_agent_runtime/main_agent/computer_use.py#L294-L297`。插件应以该标签为删除单元：当当前会话没有对应的本地工具时，整块不转发。标签是宿主自己给出的 owner 标记，比逐字匹配稳定。
2. **保留 4.29.4 的精确块**作为旧版本兜底；同时补上 4.30 的 Windows 新文案。后者在 `_build_local_mode_prompt()` 末尾追加，原文（同文件 L129-L134）：

   ```text
    Restricted Windows execution uses AppContainer. PowerShell starts at Workspace:\, a PSDrive mapped to the real workspace. Use relative paths or Workspace:\ paths with cmdlets; native programs need relative or actual filesystem paths, not PSDrive paths. ...
   ```

   这些文案在 `v4.30.0-alpha.1` 已存在，但 4.29.4 没有；精确匹配 4.29.4 旧文案会漏掉它。
3. **工具名审计器只做只读观测。** 只从反引号、`tool`/`工具` 邻近窗口、`snake_case` 标识符里抽候选工具名，与插件自己的 `capabilities`（MCP `tools/list` + 已配置的 MCP server）比对；命中“声明了但不存在”的段落时，只记 `section_hash`、`bytes`、`anchor`，**不记正文、不自动删除**。理由：ACP 没有工具清单下行，`tools` 模式下插件也拿不到 agy 内置工具全集；基于词法的默认删除会误伤人格，且 Antigravity-Manager #3975 已经证明反引号豁免有盲区。确认要删的，只限 P0.1 的宿主结构化标签与 P0.2 的已知完整文案。
4. **fail-open 到原文**只适用于“未知段落”；已知的宿主工具声明必须 fail-closed（不转发）。两者不要混。

### P1：只做“候选归因”，不改冷却语义

当前 `failure_guard.py` 对 `QUOTA` 与 `RATE_LIMIT` 共用 `(provider, model)` 级冷却；一段 596B 的坏 system 会让同模型所有会话一起被挡住 60 秒。但这不是 #10：**#10 指的是限流风暴结束、冷却过期后，新会话仍静默直连 fallback（疑似 host_compat 层状态或宿主 runner 行为）**，属于独立缺陷，需另行排查。把两者混为一谈会误导维护者。

本期建议只做观测：

- 在 `notices.py` 的 `RetryNoticeGuard` 丢弃上游原文之前，提取一个布尔标记（如 `detail_free_exhausted`）：CLI 摘要为通用 `RESOURCE_EXHAUSTED`、且文本是 `Resource has been exhausted (e.g. check quota).` 一类无结构细节的形态。注意这是 **CLI 摘要**，不是抓包确认过的原始 HTTP body；原始 body 是否真的没有 `details` / `RetryInfo` 仍未核实。
- 在 `sessions.py` / `provider.py` 的失败路径上记录 telemetry：`content_429_candidate`、`system_hash` 前 8 位、`system_bytes`、是否已有正文/tool 产出、attempt 数。**不落 system 原文。**
- 不改 `FailureGuard` 的冷却键、TTL 和 `record_success` 语义。原因：当前 `FailureGuard.check()` / `record_success()` 只支持单个二元组 scope；只改失败写入键会导致检查不命中、成功不清理。`system_hash` 又绑定在会话/CLI 进程与 home 目录上，不是一次请求内的轻量字段。要做对，需要单独立项，而不是顺手改一行。
- 明确“同 system_hash 复现 + 无输出”只是启发式辅助特征：真实持续限流或配额耗尽时同样成立，不能单独区分内容 429。

### P2：暂不实现“净化重试”，先写清前置条件

**当前不建议实现。** v0.6.1 已在请求入口（`provider.py:412`）无条件剥离已知失实声明；如果 P2 的 sanitized_system 也只是“删已知失实声明”，到达 P2 时 `sanitized == original` 恒成立，会被“若相等则不重试”100% 短路，成为死路径。同时，把同一操作在 P0 叫“语义修正”、在 P2 叫“规避风险”，前后矛盾。

如果未来仍要做，必须同时满足：

1. 有原始 CLI notice 文本或等价布尔标记，且能确认是 detail-free 形态；
2. 有一个独立于 `FailureGuard` 的 per-`system_hash` 短窗计数器，不写全局冷却；
3. 重试要在同一个 turn 内关闭旧 ACP 会话、重建新进程（system 写在 home/AGENTS.md，不能只改请求字段）；
4. 只在“无任何正文/tool 产出”时触发，最多一次；成功清计数，失败走 fallback；
5. 默认 `off`，配置显式开启，并记录审计（只记 hash/长度/命中 anchor）。

达不到这五条，就不要做。对确定性 429，重试同一 payload 的成功率是 0%；错误实现的“净化重试”只会把真限流变成重试风暴。

### P3：上游化

- 给 AstrBot 提 issue/PR：计算机使用说明应按 provider 能力注入，或至少保留 `<execution_environment>` 这类结构化 owner 标记（4.30 已经在做）；插件消费标签，而不是猜文案。
- 长期方向是 taskB §5.1 的“按 owner 拆分宿主 prompt”：人格/行为约束可转发，宿主工具清单永不转发，环境事实由 agent 自己产生。插件侧用白名单消费，而不是持续扩展文本黑名单。
- 若 `agy` 未来广告 ACP `sessionCapabilities.systemPrompt`，改用能力位 `append` 传人格；**不要用 `override`**——规范明确 override 后工具指导的责任转移到 client，而 client 并不知道 agent 有哪些工具。

### 明确不做

- 零宽字符 / 同形字 / 不可见控制符（最容易被识别为对抗，且上游会先规范化）。
- 改 `User-Agent`、`x-goog-api-client`、OAuth client 标识或 `requestType`（插件走官方 CLI，本来也改不了）。
- 对确定性 429 做指数退避到 30 分钟（成功率 0%，只会放大冷却）。
- 删除整个 system（语义退化，且不比删除失实块更安全）。
- 基于启发式词法规则默认删除未知段落。
- 在 P1 还没拿到真实归因数据前，改冷却键或默认开启重试。

## 5. 与竞品方案的差异（为什么不能照抄）

| 项目 | 它的位置 | 能做什么 | 本插件不能照抄的原因 |
|---|---|---|---|
| OMP | 自建 envelope + 自己的 OAuth | 改 `requestType`、改 prompt 标签 | 本插件走官方 `agy`，envelope 不归插件管；改 envelope 也不是插件该做的事 |
| CLIProxyAPI | 网关 | 对任意 system 做正则替换 / 混淆 | 插件只拿到 AstrBot 传来的 system 字符串，且不能对用户人格做通用改写 |
| Antigravity-Manager | 网关 | 清洗第三方客户端固定句、账号池调度 | 插件的职责边界更窄；但“请求级 429 不连坐”可以借鉴 |
| dsh / dsh-acp | agent 本体 | prompt 与工具 schema 同源组装 | 插件不是 agent 本体；但“不组合失实声明”与“组装期硬校验”可以借鉴 |

## 6. 验证计划

离线（不烧上游额度）：

1. `filter_host_capabilities()`：4.29.4 五种文案、4.30 Windows 新文案、`<execution_environment>` 包裹、改写过的工具段（含 `astrbot_shell_session` 但措辞不同）、纯人格段落逐字保留。
2. 标签过滤：完整标签块被删；残缺/嵌套/未知 `runtime` 值保持原样并记 debug；人格里的同名标签不被误删（只匹配宿主已知形态）。
3. `CONTENT_429` 候选：detail-free 摘要 + 无输出 → candidate；带 `RetryInfo` / `QuotaFailure` → QUOTA；普通 429 → RATE_LIMIT；telemetry 不含 system 正文。
4. 回归：现有 222 项离线测试全过；Windows × Python 3.11/3.12 矩阵不退化。

实机（需要上游额度，按 #14 的方法）：

1. 20 次请求、间隔 ≥ 75s，original / sanitized 交替；记录 429/200、首 token 延迟、usage。
2. 先做长度对照（纯 padding 1KB–96KB），再做逐段/组合二分；不要只测单行（单行可能都 200）。
3. 观测一周：`CONTENT_429` 候选计数、`system_hash` 分布、`filter` 未匹配的 debug 日志；暂不据此自动改行为。

## 7. 风险与合规

- 2026-11 存在 `403 PERMISSION_DENIED` / `TOS_VIOLATION` 的账号级封禁报告（OMP #18341、#19223；OMP 因此暂停 provider）。Google 未确认其与第三方 harness 的直接因果；同期论坛也有官方 App 的误判报告（第三方转述，未独立核实）。
- 插件已经走官方 `agy` CLI，这是比自建 envelope 更接近官方路径的形态；不要为了绕 429 去改 UA / requestType / OAuth 标识。
- “删除失实能力声明”与“改写用户人格以躲避指纹”是两件事：前者是语义修正，后者是规避行为。前者可以做，后者应默认关闭并明确告知风险。
- 不要承诺“修好 429”。能承诺的是：不再发送已知失实的工具声明；把内容 429 的候选归因数据留下来；以及不因一个坏 system 而误改全局冷却或默认重试。

## 8. 证据索引

**本仓库 / AstrBot**
- #14（现象与对照）：https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/14
- AstrBot `computer_use.py`（`<execution_environment>` 包装，commit `6a782b7be6591aa36b3e30f9e836c7c061c9ae50`，含于 `v4.30.0-alpha.1`）：https://github.com/AstrBotDevs/AstrBot/blob/6a782b7be6591aa36b3e30f9e836c7c061c9ae50/astrbot/core/astr_agent_runtime/main_agent/computer_use.py#L294-L297
- AstrBot 4.29.4 `_build_local_mode_prompt()`：https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/astr_main_agent.py

**oh-my-pi**
- #11689 误分类根因：https://github.com/can1357/oh-my-pi/issues/11689
- #11794 padding / 逐行二分 / 标签改名：https://github.com/can1357/oh-my-pi/issues/11794
- #11809 单变量矩阵与维护者立场：https://github.com/can1357/oh-my-pi/issues/11809
- #12655 重复 A/B（429/200/429/200）：https://github.com/can1357/oh-my-pi/issues/12655
- #12029 分类器“无法区分”的原文：https://github.com/can1357/oh-my-pi/issues/12029
- #18706 新指纹（RFC 2119 keywords）：https://github.com/can1357/oh-my-pi/issues/18706
- 改名提交 `10fb0ac325`：https://github.com/can1357/oh-my-pi/commit/10fb0ac325478b96982ae4b0e511475c1a72b0c8
- RFC 句改写提交 `6274932f46`：https://github.com/can1357/oh-my-pi/commit/6274932f46
- 省略 `requestType` 的 PR #11849：https://github.com/can1357/oh-my-pi/pull/11849
- 暂停 provider 的提交 `9a37046d3a`：https://github.com/can1357/oh-my-pi/commit/9a37046d3a
- 账号 403 issue #18341：https://github.com/can1357/oh-my-pi/issues/18341

**CLIProxyAPI**
- #5751 最小 2 行复现与 `<system-conventions>`：https://github.com/router-for-me/CLIProxyAPI/issues/5751
- #5848 `instructions` 触发：https://github.com/router-for-me/CLIProxyAPI/issues/5848
- #7049 拒绝硬编码：https://github.com/router-for-me/CLIProxyAPI/issues/7049
- #7433 U+200B 被上游剥离：https://github.com/router-for-me/CLIProxyAPI/issues/7433
- #7540 混淆后 403 报告：https://github.com/router-for-me/CLIProxyAPI/issues/7540
- `config.example.yaml`（`sensitive-patterns-replace`）：https://github.com/router-for-me/CLIProxyAPI/blob/main/config.example.yaml

**Antigravity-Manager**
- #3442 / PR #3444 过期 Codex 身份：https://github.com/lbjlaq/Antigravity-Manager/issues/3442
- #3915 / PR #3913 标点残缺与请求级 429 防连坐：https://github.com/lbjlaq/Antigravity-Manager/issues/3915
- #3965 / PR #3967 Kelivo `<shell>` 57 字符：https://github.com/lbjlaq/Antigravity-Manager/issues/3965
- #3981 `codemode`：https://github.com/lbjlaq/Antigravity-Manager/issues/3981
- `policy.rs` 的 `should_lock_account`：https://github.com/lbjlaq/Antigravity-Manager/blob/main/src-tauri/src/proxy/pipeline/policy.rs

**dsh / ACP**
- dsh system-prompt README（assemble / toolOrder）：https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/system-prompt/README.md
- dsh-acp-enhanced `minimal` preset：https://github.com/grunmin/dsh-acp-enhanced
- ACP `systemPrompt` RFD：https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/rfds/client-system-prompt.mdx
- ACP v1 session-setup：https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/protocol/v1/session-setup.mdx

**官方文档**
- Gemini API errors（429 三分类）：https://ai.google.dev/gemini-api/docs/api-errors
- Gemini API troubleshooting（退避建议）：https://ai.google.dev/gemini-api/docs/troubleshooting
- Antigravity Plans（配额按工作量）：https://antigravity.google/docs/plans

## 9. 未核实清单

| # | 未核实项 | 怎么验证 |
|---|---|---|
| 1 | 官方 `agy` 是否发送 `requestType`、值是什么 | 抓包看 `v1internal:streamGenerateContent` body |
| 2 | 596B 块的确切 anchor（哪一句/哪个组合） | 按 §6 做逐段 + 组合二分，original/variant 交替 |
| 3 | 入口 429 是否计费 | 对比账单 / usage 与本地请求数 |
| 4 | Google 的匹配算法与阈值 | 无法验证；只能观测“扰动→200” |
| 5 | 2026-11 封禁波与第三方 harness 的因果关系 | 无官方结论；不作为方案依据 |
| 6 | `agy` 是否支持 ACP `systemPrompt` 能力位 | 抓 `initialize` 响应 |
| 7 | CLI 摘要之外的原始 HTTP body 是否真的无 `details` / `RetryInfo` | 抓包或 CLI debug 日志；当前只有摘要文本 |

## 10. 复核记录

- 复核者：agy（gemini-4-hydrogen-high），2026-11-17，conversation `cbebdc5f-5a4d-47d2-9c87-318750037ccc`，结果 `SUCCESS`。
- 结论：`revise`。已据此修订：
  1. 删除 P2 的“只删已知失实声明”死路径，改为“暂不实现 + 五项前置条件”；
  2. P1 从“改冷却键”降为“只做候选归因”，并说明 #10 是独立缺陷；
  3. 补充 `notices.py` 丢弃原文前需提取布尔标记的实现前提，并把“detail-free body”降级为“CLI 摘要形态（原始 body 未抓包）”；
  4. 将 `<system-conventions>` / 截断到 `MAY` 的实验明确归到 CLIProxyAPI #5751，并把“字面量组合/阈值”改为推断；
  5. 补上 OMP 2026-09-26 `6274932f46` 的时间线；
  6. 把 AstrBot 标签引用从可变 master 改为固定 commit `6a782b7be6591aa36b3e30f9e836c7c061c9ae50` 的 permalink（该 commit 含于 `v4.30.0-alpha.1`），并补上 4.30 Windows `AppContainer` / `Workspace:\` 文案摘录；
  7. 工具名审计器改为只读观测，不自动删除；
  8. 弱化“最终风险是账号”的确定性表述，并修正 #7540 的说话人。
- 复审者：agy（gemini-4-hydrogen-high），2026-11-17，结果 `pass`；上一轮 10 条 issue 中 9 条 resolved、1 条 partial（AstrBot 链接仍用 tag），已按 partial 意见改为固定 commit permalink 并补 Windows 文案摘录。
