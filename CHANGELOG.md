# 变更记录

## 0.9.1（2026-11-28）

- **纯聊天与无工具路径改用默认 Agent + `AGENTS.md`（#52 / [PR #53](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/53)）**：自 Antigravity CLI `1.5.17` 起自定义 Agent（`--agent`、插件 `agents`）按 4 倍速率消耗 Gemini 额度。现在纯聊天（`chat` / `enable_tools=false` / `tool_choice=none` / 空工具集）、无工具收尾与超时恢复及 `probe` 自检均不再生成 `astrbot-chat.md` 或传入 `--agent astrbot-chat`，统一改用默认 Agent 并将人设与无工具护栏写入 `workspace/AGENTS.md`；`settings.json` 保持 `toolPermission=strict` 与 `transport.py` 拒止边界，`discover()` 移除 `--agent` 必选限制，`agy_customizations_mode=minimal` 统一覆盖全部默认 Agent 会话（默认仍为 `inherit`）。
- **默认 Agent 纯聊天路径自检与拒止加固（#52 跟进）**：`probe.py` 同步传递 `agy_customizations_mode` 以在自检阶段即实测 `--include-default-customizations=false` 并在不支持的 CLI 上明确阻断（避免旧 CLI 开启 `minimal` 时自检报绿、对话全报 `UNSUPPORTED`），`onboarding.py` 补充对应阻断回退指引；`transport.py` 无工具路径遇到未登记内置工具时补齐 `no_retry` / `no_fallback` 并更新过时报错文案；`sessions.py` 与 `provider.py` 区分无工具会话权限拒绝文案，不再向纯聊天用户建议切换纯聊天或使用已启用工具；`_guardrails(mcp_tools=False)` 补全全部声明工具与 `read_url`/`execute_url`/`update_steps`/`ask_question`/`swarm` 禁令，并在架构与 API 文档中明确默认 Agent 下 `search_web` 等免事前审批工具仍受事后中止约束。
- **验证与限制**：PR #53 [CI](https://github.com/sl251/astrbot_plugin_antigravity_acp/actions/runs/46149404290) 与 PR #54 [CI](https://github.com/sl251/astrbot_plugin_antigravity_acp/actions/runs/46152054838) 的 Ubuntu/Windows × Python 3.11/3.12 回归通过；合并后 2026-11-28 本地（Linux）Python 394 项通过（17 项按平台跳过）、Node 47/47、`compileall`、`check_sensitive.py` 与 `git diff --check` 通过。PR 记载 Linux Docker / AstrBot 4.29.4 / 官方 CLI `1.5.19` 实机：`inherit` 与 `minimal` 两种模式下自检通过、纯聊天两轮会话复用（`reused=true`、`history_relation="exact"`）、要求调用 `run_command`/`search_web` 时模型按护栏拒绝（哨兵文件未创建）、容器 `/AstrBot/AGENTS.md` 未穿透进插件隔离工作区。**未验证**：单账号精确额度倍率 A/B（沿用上游 [#1890](https://github.com/google-antigravity/antigravity-cli/issues/1890) 实测结论）；webchat / QQ 多轮长对话；Windows、macOS 实机；CLI `1.5.19` 原生 `read_file` 仍为 `unverified`。无数据迁移，可回退 v0.9.0。

## 0.9.0（2026-11-28）

- **带图工具结果的续轮校验与 MCP 图片回传（#48 / [PR #49](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/49)）**：AstrBot 4.29.4 在工具返回图片时，会在 `role=tool` 结果之后再追加一条承载图片的 `role=user` 消息。旧版只接受「末尾恰好一条工具结果」，带图的续轮一律抛 `TOOL_HISTORY` 并切到 fallback，用户收到与图片无关的回复。现在闭合交换可以是 `assistant / tool / user(图片)` 三段且必须位于尾部：标注文本须与本次工具名及文本结果中的缓存路径逐一匹配、路径保序、`id` 只允许等于 `path`，其余历史仍逐条对照已审计记录；带图续轮同样经过 `TOOL_STALE` 迟到结果拦截。
- **续轮判定的连带修复**：`provider.py` 的续轮判定改为识别完整尾部（原先只看末尾是否 `role=tool`，带图时判错）；`media.py` 不再把用户当前轮的图片拼进工具图片那条消息，同图去重不再打乱「标注 / 图片」配对；伪装或残缺的工具图片消息仍按 `TOOL_HISTORY` 拒绝。
- **MCP 图片回传**：工具结果中的图片经 MCP `tools/call` 结果的 `image` 块交给官方 CLI；CLI 未声明 `promptCapabilities.image` 时只送文本标注。
- **每日用量热力图与「历史调用记录」面板（[PR #51](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/51)）**：热力图悬停改为不改变几何尺寸的高亮，页脚详情行锁定高度，`mouseleave` 收口到网格容器，消除悬停抽动与月标重叠（如「10月1月」）；监控页新增默认折叠的「历史调用记录」卡片，展开时按需读取新增只读接口 `GET monitor/turns`（Dashboard 管理员鉴权、`Cache-Control: no-store`、白名单脱敏、近 24 小时每实例最多 100 轮，不触发 CLI 或模型请求），支持实例与状态筛选、20 条分页和逐行展开。
- **验证与限制**：PR #49 [CI](https://github.com/sl251/astrbot_plugin_antigravity_acp/actions/runs/46103670914) 与 PR #51 [CI](https://github.com/sl251/astrbot_plugin_antigravity_acp/actions/runs/46107590439) 四组 Ubuntu/Windows × Python 3.11/3.12 回归通过；合并后本地 Python 389 项通过（17 项按平台跳过）、Node 47/47、`compileall`、`check_sensitive.py` 与 `git diff --check` 通过。#49 的运行时代码已于 2026-11-28 部署到 Linux Docker / AstrBot 4.29.4 / 官方 CLI 实机，并用 webchat 复现：修复前工具回看图片后立即 `TOOL_HISTORY` 并切到备用模型，修复后同一用例续轮成功、答出图中内容。**未验证**：工具真正返回图片时 MCP `image` 块送达 CLI 的路径（实机自动附图使工具返回 0 张，仅离线测试覆盖）。v0.9.0 发布包已于 2026-11-28 整目录部署到该实机：容器内 Py3.12 回归 389 项通过，加载无报错，`agy_acp` 仍为默认模型，同一 webchat 用例冒烟通过；#51 的面板交互由维护者验收，后端 `monitor/turns` 与面板未经自动化实机点击验证；Windows、macOS 与 AstrBot 4.30 未验证。
- **升级须知**：`media_roots` 需包含宿主落盘图片的目录（AstrBot Docker 默认 `/AstrBot/data/temp`），否则 context_aware 自动附图等经宿主落盘的本地图片会报 `IMAGE_PATH` 并整轮 fallback（[#50](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/50)，属配置而非代码缺陷）。无数据迁移，可回退 v0.8.6。

## 0.8.6（2026-11-27）

- **工具执行超时后的迟到结果重建（#45 / [PR #46](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/46)）**：宿主工具执行超过 `tool_execution_timeout` 后，插件不再关闭 ACP 会话，而是在会话上记一份超时交接。宿主晚带回闭合的工具结果时，校验同一宿主运行、未过 `idle_ttl`、会话存活、模型与上下文一致、历史前缀与原工具调用完全匹配、结果未提交过；全部通过则把真实结果按文本历史送入一次无工具生成。任一条件不满足时返回 `TOOL_TIMEOUT`（禁止重试与 fallback），不再误报「进程重载」；真正的进程重载或过期回填仍返回 `TOOL_STALE`。
- **步数用尽后的收尾请求不再 `TOOL_CONTEXT`**：AstrBot 4.29.4 跑满 `max_steps` 后拔掉工具集再请求一次，带 `role=tool` 尾巴的请求续接带工具的旧 prompt 会抛 `TOOL_CONTEXT`。现在收尾请求以已闭合的文本历史重建、不带工具；跨运行、改写或重复的回填仍被拒绝。
- **验证与限制**：PR #46 [CI](https://github.com/sl251/astrbot_plugin_antigravity_acp/actions/runs/45871142042) 四组 Ubuntu/Windows × Python 3.11/3.12 回归通过；本地另用 AstrBot 4.29.4 的 `tool_loop_agent_runner.py` 原文跑同一套 372 项通过，两组新回归走宿主真实代码。2026-11-27 已在一台 Linux Docker / AstrBot 4.29.4 实机部署并完成 webchat 一轮验证。线上触发该问题的长工具场景未在实机复现；当时「插件记录为不允许 fallback、宿主仍切到 fallback」的差异尚未查明。排障见 [troubleshooting](docs/troubleshooting.md)。

## 0.8.5（2026-11-26）

- **账号额度自动刷新（#43 / [PR #44](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/44)）**：首次进入监控页、页面恢复及登录结束后先读取缓存，缺失或过期才通过官方 CLI 查询；60 秒成功缓存、并发合并、失败冷却与原手动强制刷新并存，GET 无副作用，无常驻额度轮询。
- **近 24 小时监控明细跨重载恢复**：新增独立 `turn-history-v1.sqlite3` 与持久匿名实例关联，保存每实例最多 100 轮、全库最多 2,000 轮脱敏统计，主库上限 8 MiB；恢复耗时、请求计数、Token 与复用率，不重复累计既有每日用量或失败事件。存储异常降级为内存统计；ACP 会话池、当前上下文及进程状态仍来自本次运行时，不保存对话正文、凭据或工具参数。
- **验证与限制**：PR #44 [CI](https://github.com/sl251/astrbot_plugin_antigravity_acp/actions/runs/45840247494) 四组 Ubuntu/Windows × Python 3.11/3.12 回归通过；PR 报告记录 Node 45 项通过、Linux Docker / AstrBot 4.29.4 / CLI 1.5.18 真实额度查询、并发缓存、运行时重建与独立进程恢复验证。管理员 iframe 交互及带生产历史样本的宿主整进程重启仍待验收。旧版未落盘明细不可回填；可回退至 v0.8.4 并保留宿主配置与统计文件。

## 0.8.4（2026-11-26）

- **内置工具独立权限与实验性搜索（#38 / [PR #39](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/39)）**：新增独立「工具权限」页签与 `POST setup/tool-policy` 接口，支持单独持久化 `builtin_read_file` 与实验性 `builtin_search_web` 而不覆盖既有提供商配置，展示未生效总闸告警与待重载琥珀色徽标并折叠收纳固定拒绝项；`search_web` 关闭时经模型护栏与事后观察中止，开启后按通知计入与 MCP 共用的 `tool_max_calls` 预算，面板与聊天提示均如实说明事后观测局限。
- **官方 CLI 账号额度与本地每日用量（#38 / [PR #39](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/39)）**：新增 `QuotaService` 按需查询官方 CLI 账号额度（不读 OAuth 凭据，60 秒 TTL、并发合并、30 秒超时、跨环境失效与脱敏错误码），「运行监控」新增中文化额度窗口与超 3 项模型列表折叠抽屉；新增 `daily-usage-v1.sqlite3` 按宿主本地日历持久化每日用量并提供 90/30/365 天纯 SVG 热力图与模型/指标筛选。
- **私聊管理员指令与面板 UX 优化**：重构 `/agy help|setup|use|quota|usage|cost|doctor|status|models` 输出格式为简洁中文可读文本（剔除原始 JSON 与 UTC ISO 时间戳，`/agy quota` 转宿主本地 `MM/DD HH:MM` 与倒计时以避免 QQ 识别日期链接）；未就绪阻塞态增加顶部「前往配置 →」引导条，活跃 0 请求实例收敛为单行紧凑状态；非管理员裸 `/agy` 不再泄漏子指令清单。
- **CLI `1.5.18` 只读原生工具实测、统一注册表与无 MCP 解耦（#41 / [PR #42](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/42)）**：完成 Linux 官方 `Antigravity ACP Server 1.5.18`（无 MCP `mcpServers: []`）实测，确认 `read_url`/`list_dir`/`find_by_name`/`grep_search` 在 `agy acp 1.5.18` 未暴露（目录与检索均退化为 `run_command` 任意 shell 执行，继续锁定拒绝），详见 [CLI 1.5.18 原生工具调查报告](docs/issue41-builtin-tools-probe.md)；新增 `BuiltinToolSpec` 与 `BUILTIN_SPECS` / `KNOWN_BUILTIN_NAMES` 统一注册表覆盖 `edit_file` 与 1.5.18 全量内部调度/子代理工具拒止；开放 `read_file` 至 Linux CLI `1.5.17`/`1.5.18` 并支持 `StartLine`/`EndLine`/`ByteOffset` 严格整数边界与帧间一致性校验（拒绝 `file:///` URI）；解除原生工具对宿主 MCP 非空的依赖（纯内置模式不启动多余 `mcp_bridge.py` 子进程），并修复非搜索内置工具拒止误报 `SEARCH_ABORTED_NOTICE` 的问题。
- **验证与实机**：Windows 本地 Python 离线回归 352 项通过（23 项 Linux/POSIX 跳过）、Linux 容器（Python 3.12.13）离线回归 352 项通过（17 项 Windows 跳过）、前端 `node --test tests/setup.test.mjs` 42/42 通过、`compileall` / `check_sensitive.py` / `git diff --check` 与 PR #39 / #42 GitHub Actions（Ubuntu/Windows × Python 3.11/3.12）全部通过；已在生产测试宿主（`xiaoyu`，AstrBot 4.29.4 Docker + CLI `1.5.18`）验证无生成 `probe()`（`verified_read=true`）、无 MCP `read_file` 按行分页真实调用（`calls=grants=1`, `server=None`）、插件热部署加载及 `/agy` 指令格式；Windows、macOS、CLI `>=1.5.19` 与 AstrBot `4.30` 尚未完成实机工具验收。

## 0.8.3（2026-11-25）

- **WebUI 三标签页与交互重构（#36 / [PR #37](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/37)）**：保留 AstrBot 4.x 原生风格与完整纯 SVG 三层「运行监控」默认首页，新增单行全局默认摘要条与顶部隐式回退（`#route-alert`）/ 配置漂移（`#drift-alert`）告警横幅；「模型管理」新增「添加模型」弹窗入口与原地「编辑」弹窗（`setup/provider-update` 保留原 `provider_id` 避免旧绑定失效），思考默认档位按当前全局配置明示为 `跟随全局（CLI默认 / 低 / 中 / 高）`；切换下拉模型改用本地 `efforts_by_model` 缓存并暂停自动 `setup/check`，弹窗打开期间禁止后台轮询覆盖表单字段，暂态检测失败保留已有目录缓存。
- **路由与配置一致性修复**：`agy_provider/routing.py`、`onboarding.py` 与 `main.py` 统一基于合并配置读取实例、计算 `modalities`（移除残留 `audio` 并对齐全局 `enable_images`）、统计非默认会话 Profile 覆盖数并检测漂移；新增 `setup/provider-repair`（单实例/批量同步修复）与 `setup/provider-impact` 受控下线流程（停用/删除当前全局默认时支持原子 `migrate_default_to` 与 `force_confirm` 显式确认）；`setup/apply` 支持 `create_only` 避免覆盖同底层模型配置；初始化与重载恢复后同步校准 `curr_provider_inst` 指针，同时保持持有 `manager.resource_lock` 期间不重入宿主更新/删除方法。
- **验证**：全量离线 Python 回归 303 项运行通过（11 项平台/权限条件跳过）、Node 前端交互/监控回归 30/30、`compileall`、`check_sensitive.py` 与 `git diff --check` 全部通过（PR #37 Ubuntu/Windows × Python 3.11/3.12 矩阵通过）；已在测试宿主（AstrBot 4.29.4 容器）预部署验证零报错加载与服务启动，仍未覆盖真实 CLI 批量工具生成、macOS 及 AstrBot 4.30 环境。

## 0.8.2（2026-11-25）

- 修复 AstrBot 在持久化历史时移除临时用户文本，导致后续轮次误报 `history_rewrite` 并重建 ACP 会话的问题。可确认的临时文本仍发给模型，预期回放按宿主实际保存的内容计算。真实用户编辑仍触发重建。[#33](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/33) · [PR #35](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/35)
- 离线回归覆盖严格及结构化模式、思考块回放、真实历史编辑和工具回填。PR 的 Ubuntu 与 Windows 矩阵已通过，真实 AGY 与生产效果待验证。[CI](https://github.com/sl251/astrbot_plugin_antigravity_acp/actions/runs/45433687834)

## 0.8.1（2026-11-24；实机验收待 #31）

- 前端选用 [feat/ui-redesign](https://github.com/sl251/astrbot_plugin_antigravity_acp/tree/feat/ui-redesign) 的最终可视化设计：环形占比、会话池分段条、上下文进度、Token 构成与耗时柱图、四步进度、连接状态、响应式布局和宿主主题。
- 仅替换前端，保留 v0.8.0 的 #32 后端路由、历史图片预算、输入组织、防重放和脱敏持久失败记录。适配监控 schema v2、只读路由与失败诊断；设置与保存继续使用原生 Bridge 与 nonce。高级实例操作按钮不随旧界面恢复，可使用宿主原生模型管理；后端 API 保留。
- 恢复原生 DOM 回归，保留 HTTP/受限 iframe 剪贴板降级和后台轮询生命周期。移除 Vue 构建目录与旧产物，分发不需要 Node.js。
- 本版由跨平台 Python 回归及原生 JS 测试门控。实机、真实 CLI、生产与视觉验收待 #31；保留 v0.8.0 供回退。

## 0.8.0（2026-11-24；实机验收待 #31）

- **原生 Dashboard 重构（#30）**：Vue 3 / Vuetify 3 / TypeScript / Vite 六模块界面，使用宿主 Plugin Views Bridge、相对资源路径及本地字体/图标；保留 #28 的复制降级与轮询边界。
- **模型路由（#32）**：默认模型保存同时校验 runner 配置与实际解析结果，区分配置主项、解析主项和服务观测；删除/停用前检查引用，扫描不完整时阻止操作。不全局修改宿主 resolver/delete_provider，不按同名模型自动迁移 ID。
- **上下文与图片**：当前用户输入置于历史封套外，保持当前附件顺序；历史图片使用占位及指纹，不再占用本轮真实附件预算，保留当前图验证与会话复用边界。
- **失败与副作用**：无有效正文、无工具交接的失败保留显式 fallback 资格；可见正文、工具交接及工具续轮失败禁止自动重放。工具前后分段过滤真实 CLI 告警，新增有界脱敏持久失败诊断，与 cost log 解耦。
- **版本与分发**：统一 metadata、运行时单点版本常量与前端 package/lock；AstrBot `>=4.29.4,<4.30`，Python 3.11+，部署使用已构建的静态资源，无需 Node.js。
- **验证与限制**：[#34](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/34) 最新实现提交 `107bef7` 的 [CI](https://github.com/sl251/astrbot_plugin_antigravity_acp/actions/runs/45379518123) 六组全部成功（Linux/Windows 前端构建一致性检查与 Python 3.11/3.12 回归）。旧 DOM 测试因旧 app.js 移除而跳过，不等同于新版交互验收。真实 AGY、AstrBot iframe、QQ、视觉与生产验收尚未执行（[#31](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/31)）；#32 §5 并发争用、池锁及专用 Agent 调查延期。建议先备份配置并在非生产环境验证；可回退至 v0.7.5。

## 0.7.5（正式发布，2026-11-22）

- **修复 #28：设置面板复制按钮全部失效（含卡住 Google 登录）**：AstrBot 4.29.4 把插件页放进 `sandbox="allow-scripts allow-forms allow-downloads"` 的 iframe（无 `allow-same-origin`、无 `allow="clipboard-write"`、无 `allow-popups`），面板又通常经 `http://<ip>:6185` 访问（非安全上下文），此时 `navigator.clipboard` 为 `undefined`；原 `copy()` 只有 Clipboard API 一条路径，失败仅向页脚写一行小字。现改为三级降级：`navigator.clipboard?.writeText` → 点击手势内同步执行临时 `textarea` + `document.execCommand("copy")` → 两者都失败才选中原文并提示快捷键；4 类复制入口（安装命令、官方文档、授权链接、`复制命令`）均把按钮自身传入并就地显示「已复制 ✓ / 请手动复制」，`#copy-status` 改为底部居中浮层、3s 自动消失。授权链接新增「在新标签打开」，`window.open` 被 sandbox 拦截时明确提示改用复制。
- **轮询不再破坏手工操作**：`#install-url`、`#install-command`、`#auth-url` 赋值前比较，`#facts`、`#env-diag-list` 改为内容签名差量渲染；此前 5s 轮询会把用户手工选区折叠回末尾，使复制表现为随机失败。`poll()` 失败改为 5/10/20/30s 退避并在成功后只清除自身错误横幅（与 action 错误分来源管理），标签页隐藏时暂停、可见时立即刷新；`pagehide` 判断 `event.persisted`，bfcache 返回经 `pageshow` 恢复轮询与监控；`render()` 对 `report`/`login`/`environment`/`providers` 加缺省值，`action()` 在 `await` 后判 `disposed`，避免二次抛错；「CLI 安装与路径」仅在错误码变化时自动展开一次。
- **可访问性与视觉修正**：浅色主题新增 `--ok-text: #047857`、`--bad-text: #b91c1c`（`#f4f4f5` 上 4.99:1 / 5.89:1，满足 WCAG AA 小字号要求），供 `.tone-ok`/`.tone-error`/`.monitor-faults code` 使用；监控区按快照签名跳过重建，保留展开状态与列表滚动位置；`monitor-metric-sub` 移入 `dd` 内使 `dl` 语义合法；`.copy-row` 窄屏换行、顶栏吸顶、`use-default` 保存后复位、字节数走 `formatBytes`（不再出现「未知B」）、展开键加入实例索引、主题改为 auto→light→dark 三态并持久化（`try/catch` 兼容 sandbox 存储异常，auto 优先继承宿主 `isDark`）、`login.error_code` 与 `action_error` 中文化并保留原始码。
- **测试与验收**：`tests/setup.test.mjs` 假 DOM 补齐 `document.body`、`document.execCommand`、`localStorage`、`Element.remove`，新增三级降级复制、弹窗拦截提示、bfcache 恢复、三态主题、轮询退避、重复实例 ID 展开隔离等用例；离线全量回归 278 项（266 通过 / 0 失败 / 12 项平台跳过）、`node --test` 29/29、`compileall`、`node --check`、`git diff --check` 全部通过；独立只读验收 21 条逐项核对通过（PR [#29](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/29)）。
- **实机验收（2026-11-22，小鱼 / shanghai，AstrBot 4.29.4）**：分支前端预部署后由管理员按清单手动验收 12 项通过——4 类复制入口均写入正确内容、授权链接打开被拦截时提示明确、选中文本静置 10s 不被轮询打断、监控展开后焦点与滚动位置保持、主题三态与刷新后持久、窄屏复制行换行；控制台无报错。已知限制：`execCommand` 已被标准标记弃用，但它是非安全上下文 + sandbox iframe 下唯一可用路径，若未来浏览器移除则只剩「选中文本 + 快捷键」兜底；`clipboard.writeText` 与 `execCommand` 会先后尝试，在个别会为 `writeText` 弹权限确认的浏览器上可能出现一次多余提示。
- **文档**：README、AGENTS.md、`docs/architecture.md`、`docs/troubleshooting.md` 同步 WebUI 剪贴板边界与新的降级路径说明。

## 0.7.4（正式发布，2026-11-19）

- **修复 #24：会话重建时历史图片被重复提取为当前轮附件**：`prompt_blocks()` 的 `build_window()` 此前未区分当前最新用户轮与历史轮，把窗口内所有历史用户消息的图片一并提取为当前 `session/prompt` 末尾的真实 ACP `image` 块附件——用户发过一次图后，后续纯文本轮只要触发会话重建（system 变化、空闲过期、进程重启等），旧图即随请求重发，造成视觉 token 浪费、模型误判当前轮收到新图（围绕旧图幻觉作答），无视觉能力 CLI 环境下纯文本轮还会误报 `IMAGE_CAPABILITY`。修复后以 `last_user_idx` 反向定位最新用户消息（无 user 消息时安全返回 `-1`），`enumerate(tail, start=cut_index)` 保持裁剪前原始下标对齐，仅当前轮图片作为附件发送并按出现顺序编号；历史图片在转录 JSON 中保留 `{"image_sha256": ..., "status": "historical"}` 占位（不带 `attachment` 键）；历史轮不写入 `image_ids`，跨轮显式重发同图仍正常附带。会话复用路径（仅发最后一条消息）不受影响。
- **行为权衡与已知限制**：会话重建后模型不再看到历史图的原始像素，只能依赖历史文字描述与哈希占位——重建后需重看旧图细节请重新发送该图；`prepare_media()` 仍按原样预检历史图片并计入媒体预算（跳过会破坏 `request.hashes` 哈希契约、摧毁会话复用），长会话累计图片超限或旧图源失效仍可能在预检阶段报错，留待后续另案处理。
- **测试**：新增 `tests/test_prompt_images.py` 8 项单元回归（历史图不重发、多轮多图、当前轮多图+历史混合、跨轮重发同图、轮内重复图去重、预算裁剪偏移对齐、工具交换历史、单消息捷径与无 user 防御）；`tests/test_sessions.py` 新增 3 项会话级集成场景（发图后纯文本重建不带旧图、发图后纯文本命中复用、无图能力 CLI 下纯文本重建不误报 `IMAGE_CAPABILITY`）；补强 `tests/test_release_hygiene.py` 弱断言（原断言保留，新增 budget=900 历史占位与 budget=1000 当前轮带图二分裁剪 subTest）；`tests/fake_cli.py` 新增 `no-image` 能力场景。
- **验证**：全量离线回归 278 项（266 通过 / 0 失败 / 12 项平台跳过）；`node --test` 19/19；`compileall`、`git diff --check`、`check_sensitive.py` 通过；Ubuntu / Windows × Python 3.11 / 3.12 CI 矩阵全绿（PR [#25](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/25)）。
- **生产实机验收（2026-11-19，小鱼 / shanghai，AstrBot 4.29.4 + AGY CLI 1.5.9，webchat JWT 端到端）**：分支构建预部署后，当前轮发图正常识图；`docker restart` 强制会话重建后的纯文本轮，模型明确回答「在你当前这条消息里，我看不到任何图片」，该轮输入 token 与含图轮差值恰为图片视觉开销（约 2.3K），证实旧图不再随请求重发；重发同一张图可正常识别（跨轮去重保护生效），无 fallback 切换、无 `AgyError`、无 429。验收同时观测到一项与本修复无关的既有行为：webchat 内存态历史图为本地路径且 `media_roots` 为空时，同进程次轮在 `prepare_media` 预检报 `IMAGE_PATH` 并回落 fallback（v0.7.3 同场景同样触发，QQ 平台图片为 http URL 不受影响）；如需 webchat 图片追问体验，可将宿主附件目录加入 `media_roots`。

## 0.7.3（正式发布，2026-11-18）

- **修复 Python 3.12+ 工具桥关闭死锁与 Windows 目录释放竞态（PR #22）**：Python 3.12.1+ 中 `asyncio.Server.wait_closed()` 会等待所有活跃客户端连接协程退出后才返回；`ToolBroker._close()` 调整为先关闭监听、取消待处理调用 `future` 与连接任务并 `gather`，最后再 `await server.wait_closed()`，消除工具交接后取消或插件停用时阻塞至 `tool_execution_timeout`（默认 120s）的死锁。同时新增 `remove_tree_robust()` 有界重试并在 Windows 无 Job Object 且前端进程先退出时仍执行 `taskkill /T`，消除后代进程 `cwd` 句柄短暂延迟释放引发的 `WinError 32`。
- **同会话并发请求隔离与排队保护**：活跃 `ToolTurn` 等待原调用方回填工具结果期间，通过 `signal is existing_turn.signal` 严格区分续轮所有权——同会话外部请求的预检失败不再误关闭或污染原工具轮与 `TurnMeter`；正常并发新消息在 `queue_timeout` 内通过 `done_event` 排队等待前序工具轮结束再接力执行，超时仅自身返回 `BUSY`；非当前拥有者的迟到 `tool` 续轮安全拒绝为 `TOOL_STALE`。
- **补全 Gemini 复合数组 Schema 适配**：`_sanitize_for_gemini()` 递归覆盖 `anyOf` / `oneOf` / `allOf`（如 Pydantic `list | None = None`）、字典形态 `additionalProperties`、联合类型列表 `["array", "null"]`，递归深度上限对齐 `validate_schema`（32 层）；宿主执行校验仍使用独立深拷贝的原始 `to_host_schema`，不收窄合法参数范围。
- **PNG 压缩辅助块无损剥离与索引色兼容**：在校验块长度与 CRC32 后剥离 `zTXt` / `iCCP` / `iTXt` 辅助块再重组 PNG（既消除元数据解压放大风险，又兼容携带 Display P3 `iCCP` 或截图元数据的 macOS/iOS/Windows 截图）；新增 8 位调色板索引色 PNG（`color == 3` 及前置 `PLTE` 校验）支持。
- **探测缓存、异步磁盘 I/O 与长历史二分裁剪**：`discover()` 并行采集 `--version` 与 `acp --help`，按二进制路径与文件 `stat` 缓存并记录 `supports_login` 供 `LoginFlow` 直接复用；异步路径中的 `make_home()` 与本地媒体读取 `_local()` 移入 `asyncio.to_thread`；`replay()` 与 `prompt_blocks()` 改用二分查找定位裁剪边界并预计算图片哈希，保持与逐条裁剪字节级等价。
- **版本单点化、孤儿目录清扫与发布归档卫生**：新增零依赖 `agy_provider/_version.py` 统一供给 `main.py`、ACP `clientInfo`、MCP `serverInfo` 并由 `tests/test_release_hygiene.py` 与 `metadata.yaml` 强校验；插件初始化时后台清扫超过空闲阈值的非活跃 `preflight-*` / `login-*` / `runtime-*` 残留目录；补齐 `_conf_schema.json` 的 `enable_images` 与 `media_roots` 提示；新增 `.gitattributes` 在 `main.zip` / Release 归档中排除 `tests`、`docs`、`review-bundle`、`tools`、`.github` 等非运行文件；增强 `tools/check_sensitive.py`（支持 `--staged`、Google OAuth `ya29.` / `1//`、`gh[ousr]_` 及内嵌账密代理 URL 检测）。
- **验证**：离线全量 Python 回归 267 项（Windows 本地 263 通过、4 按平台/权限跳过、0 失败），前端 `node --test` 19/19 通过，`compileall`、`check_sensitive.py`、`git diff --check` 通过；Ubuntu / Windows × Python 3.11 / 3.12 CI 矩阵全绿。
- **生产实机验收（2026-11-18，小鱼 / shanghai，AstrBot 4.29.4 + Python 3.12.13 + AGY CLI 1.5.8）**：容器全量回归 267 项 OK（12 项平台跳过）；0.7.3 纯净包部署后插件零报错加载、host_compat 安装、`agy_acp(agy_probe)` 注册为默认 provider，部署文件与 tag 逐字节一致；webchat 端到端纯聊天经完整 pipeline 正常应答（22,957 prompt tokens，TTFT 40.5s，未触发 429、未回落 fallback）。工具轮实测遇上游 `gemini-3.8-flash-low` 连续 503 `No capacity available`，重试耗尽 180s `request_timeout` 后超时并回落 fallback；该轮无插件侧报错，未归因本版代码，工具链实机验收待上游容量正常时补测。

## 0.7.2（正式发布，2026-11-17）

- **修复 #19：Safe Mode 第 4 条规则相关的上游 429**。AstrBot 4.29.4 在 system prompt 最前端加入 6 条 Safe Mode 规则。在报告者与维护者的对照环境中，含第 4 条 `Follow style/role-play instructions …` 的请求出现 detail-free `429 RESOURCE_EXHAUSTED`，同环境仅改写措辞则正常返回；不同账号和出口的独立测试也观察到相似现象。单变量实验表明特定句式与错误高度相关，但**无法据此确认 Google 内部的匹配算法、违规判定机制，或排除其他环境下的额度/网络因素**。上游已有相关修改提案，4.29.4 存量用户仍需插件侧的定点兼容。
- 修复方式：在该结构锚定的 Safe Mode 列表内，仅把已知单句中的 `style/role-play` 定点改写为 `style and role-play`，保留 `these rules` 指代、行首 `- `、其余文字、尾随空白与 LF/CRLF，改写幂等；style 与 role-play 均仍受安全规则约束，不删句、不删规则、不改 messages 与 telemetry。
- 匹配边界：独占行 `You are running in Safe Mode.`、可选空行、`Follow these rules:` 之后的连续顶格 `- ` 列表；4.30 形态要求完整、无嵌套、内部只有该块的 `<safety_instructions>` 包裹。仅容忍大小写与句末句号，不放宽到已验证安全的其它拼写；未知变体、残缺/嵌套标签、缩进、引用块、代码围栏一律 fail-open，只记录无正文 debug 原因码；前置短路增加 Safe Mode 哨兵。
- **实机验收（小鱼 / shanghai，AstrBot 4.29.4）**：容器回归 256 项 OK（12 项平台跳过）；webchat 端到端经完整 pipeline 由 `agy_probe`（gemini-3.8-flash）正常应答，此次验收未触发 429、未回落 fallback；对宿主真实 `LLM_SAFETY_MODE_SYSTEM_PROMPT` 执行过滤，差异仅第 4 条一行（`style/role-play` → `style and role-play`），其余字节不变。

## 0.7.1（正式发布，2026-11-17）

> 本版在 0.7.0 功能基础上完成 #16 Agent 文档重构与 #18 安全清理，并把三处源码版本标签统一为 0.7.1。0.7.0 发布后未被 AstrBot 插件市场收录，其 GitHub 发布（release 与 tag）已撤回，以 0.7.1 重新发布。

### 文档与 Agent 入口（#16）

- 新增精简 `AGENTS.md` 与当前架构/源码测试索引、分类排障 SOP、测试验收和交付规范；README Agent 区改为当前契约摘要。
- 明确 AstrBot `>=4.29.4,<4.30`，区分已知 4.30 文案识别与宿主兼容；纠正授权码输入、保存与生成、未知用量及 429 候选的说明。
- 核对 `telemetry.py` / `test_content_429.py`：已有输出的失败轮仍可能记录候选，必须同时检查 `has_output`；候选不能证明 Google 上游根因。保留安全边界和防重复执行/重放规则。
- 清理 Markdown 残留，为历史计划/报告补当前入口；本次验证与独立审读/维护者验收状态见 [文档验证记录](docs/agent-docs-validation.md)。

### 安全清理（#18）

- Git 历史中 `review-bundle/429-findings.md` 的账号标识与认证元数据已脱敏重写；v0.6.0 / v0.6.1 的 Release ZIP 已用脱敏后代码重新打包替换。源码、发布功能、版本号与安装方式不变，但受影响 tag 的目标 SHA 变化，旧克隆需重新同步。
- 新增 `tools/check_sensitive.py` 与 CI 敏感扫描（邮箱、手机号、API Key 形态、认证元数据字段），提交前可本地运行；处理方案与剩余事项见 [仓库安全卫生](docs/security-hygiene.md)。

### 版本统一

- `main.py` 注册、ACP `clientInfo`、MCP `serverInfo` 的版本声明由 0.6.1 统一为 0.7.1，与 `metadata.yaml` 一致。

## 0.7.0（正式发布，2026-11-17）

- **429 内容相关（P0）结构化标签过滤**：`filter_host_capabilities()` 升级为「结构化标签优先、精确文案兜底」——整块移除 AstrBot 4.30 `<execution_environment runtime="local">` 宿主块（须含宿主已知小节标记）；前置短路补标签检查，防宿主改写首句后静默失效；4.30 Windows AppContainer 尾段两变体经上游 6a782b7 源码 AST 机械提取，逐字节一致；残缺/嵌套/未知 runtime/人格同名标签 fail-open 保留并记无正文 debug 原因，正常命中不产生日志；4.29.4 精确块继续保留。只读工具名审计器（P0.3）与净化重试（P2）按调研结论本期暂缓/不做。
- **429 候选归因遥测（P1）**：`RetryNoticeGuard` 识别 detail-free 429（CLI 摘要级，排除结构化细节形态）；失败轮在 `agy.turn` 记录 `content_429_candidate`（system_hash8 / system_bytes / has_output / attempt，绝不落 system 原文），`has_output` 覆盖非空白正文、thought 与工具产出；独立 quota 文案/结构化细节测试场景不记候选，detail-free 失败轮即使已有输出仍可记候选，必须同时查看 `has_output`。这只是候选诊断，不证明上游根因。**不修改 FailureGuard 冷却键、TTL 与 record_success 语义**。
- **WebUI 信息精简与监控面板重构**（按管理员 UI/UX 计划执行）：运行监控三层——概览（默认展开紧凑双列，口径标签区分实时会话池 / 近 24h / 最近采样，缺数据显示「上游未报告」不伪造 0%）＋详细统计（按需展开，纯 SVG Token 构成堆积条、复用率与缓存占比双环、耗时分布、重建原因、历史记录）＋高级诊断（默认折叠，ACP 错误 / 429 候选 / telemetry 字段）；环境页深层技术参数收进折叠「高级诊断」抽屉；认证与模型配置文案精简，复杂说明入帮助弹窗；移动端 360px 适配、深色对比度、折叠交互反馈。可视化零外部依赖；监控数据源不变（`monitor/status` 只读快照），后端 Python 零改动。
- `monitor/status` 保持仅面板管理员可读、`Cache-Control: no-store`、递归白名单投影；429 端到端测试夹具对齐生产实机 CLI 1.5.8 单行告警形态。

验证与实机验收（2026-11-17）：本机与生产容器各跑一轮全量离线回归，246 项全过（12 项按平台跳过，0 失败，0 预期失败）；前端 node --test 19/19；Windows × Python 3.11 / 3.12 CI 矩阵全绿。生产实机（AstrBot 4.29.4 + AGY CLI 1.5.8）部署后：插件零报错加载、host_compat 正常、容器内回归通过、`monitor/status` 200 + no-store、webchat 端到端（agy_probe）正常；前端面板经管理员人工验收通过。计划先行经 agy gemini-4-hydrogen-xhigh 独立复核（REVISE 意见全部吸收）。已知待办：面板程序化访问的 JWT cookie 注入被前端路由守卫拒绝（后端已核实支持 ApiKey 认证），记录在调研目录 dashboard-login-notes.md，不在本仓库。

## 0.6.1（正式发布，2026-11-16）

- **#11 内置工具拒绝后空回复归因**：本轮权限拒绝改为有界结构化记录（method、工具名、调用 ID），排除跨会话、过期及上轮记录；`end_turn` 无正文且有拒绝证据时返回 `TOOL_PERMISSION`，保留上游限流分类优先级，不进入 EMPTY 去重或额度冷却。守卫提示明确禁止内置 web search / browser；这是提示与诊断缓解，不能修复 CLI 未回喂拒绝结果的问题。
- **#12 MCP 权限分层**：采用并扩展 Hazukimaki 的 `d24b97d` 参考补丁，仅放行当前活跃 prompt 内已观测、名称/ID/状态匹配的快照 MCP 工具，且只能选择唯一有效 `allow_once`；重复、未知、跨会话、过期及内置工具继续拒绝。接收 prompt 终态帧时同步关闭授权窗口；宿主审批、参数校验、预算及执行隔离保持有效。
- **#13 数组 schema 契约**：独立保存宿主原始 schema 和模型适配 schema；模型端仍保守补 string items，实际参数校验改用原始契约，允许原本合法的数字、对象、空及嵌套数组。原始契约纳入快照指纹，显式 string items 约束保持有效。
- **#14 失实本地能力声明**：chat / tools 模式均精确剥离 AstrBot 4.29.4 已知完整本地电脑工具声明，保留周边人格及用户内容；未匹配文案保持原样并记录无正文 debug。此规则与宿主文案版本耦合，上游对该内容返回 429 的根因未确定。

验证：实现阶段通过 `compileall`、`git diff --check` 及 26 项直接相关最小自测。完整回归与实机验收（2026-11-16）：Linux 本地与生产容器（AstrBot 4.29.4 / Python 3.12）各跑一轮全量离线回归，222 项中 210 通过、12 项按平台/可选依赖跳过、0 失败；实机部署后 `agy_probe` 正常注册为默认 provider，webchat 端到端纯聊天链路（agent runner → provider → ACP CLI → Gemini，含缓存命中）回复正常。#11 的 TOOL_PERMISSION 归因、#12 的放行边界由新增 24 项专项测试覆盖；#14 的声明剥离逻辑有离线测试，但生产实例未开启宿主本地能力声明，429 规避效果未在生产复现路径上验证（根因仍待上游）。Windows × Python 3.11 / 3.12 矩阵由 CI 覆盖。本期不处理 #10（限流风暴后停留 fallback）。

## 0.6.0（正式发布，2026-11-15）

- 分离 ACP 上下文占用和计费 usage；同会话连续可信占用疑似压缩后，在本轮终态回收，下一轮按宿主历史重建。新增阈值配置，缺字段安全跳过。
- 分块重试 banner 区分账号额度、持续 429、暂态 503 与致命认证错误；强信号提前结束请求。修复失败状态阻止 session/cancel 的问题；RATE_LIMIT 与 QUOTA 共用原 FailureGuard，保留各自错误码。
- 已输出内容/交接工具后禁止自动重放；扩展原生 runner 兼容层，拦截 AstrBot 4.29.4 在异常后继续调用 fallback provider 的路径。
- 新增默认 inherit 的 `agy_customizations_mode`；minimal 仅在支持参数的 CLI 工具 Agent 启用，未做真实 MCP/A-B 验收。
- `/agy cost` 新增上下文/压缩/快速中断摘要，以及模式、模型、结果、冷热分组。内存样本限制为 24 小时内最近 100 轮；持久日志仍默认关闭。
- Windows 工具桥使用仅绑定 127.0.0.1 的随机端口及每会话双向挑战认证；POSIX 继续使用私有 Unix socket。拒绝非活跃 prompt 的工具调用。
- Windows 增加 Job Object 进程树托管，失败时回退 taskkill；重复清理共享同一任务，spawn 多次取消不放弃子进程所有权。
- doctor/设置页显示 CLI 参数能力、组件实验状态、桥类型及实际进程托管。CLI 探测输出有界。更新仍由管理员按官方指引手动操作，远程最新版明确未检查。
- 动态 MCP/模型热更新、CLI 自动下载安装与热替换未交付，原因与后续验收矩阵见 [实施与交接计划](docs/liftgate-hardening-plan.md)。

后续验收：修复明确 quota RPC 错误漏分类、MCP relay 非 UTF-8 默认编码下 Unicode 结果失败、Windows Job 关闭后后代未完全退出导致目录清理 WinError 32 的竞态。补齐专项回归并同步旧测试夹具；本机 Windows 198 项 Python 测试中 195 通过、3 项按平台/权限跳过，设置页 12 项通过。详见 [PR #9 验收记录](docs/pr9-acceptance.md)。实机验收（2026-11-15，生产 AstrBot 4.29.4 + AGY CLI 1.5.8）：覆盖部署后容器内 198 项回归通过；真实 Google 429 限流风暴下 fast-fail → fallback 兜底按设计工作（用户约 17s 收到回复）；直连 Gemini、配置迁移与会话/缓存遥测验证通过。minimal A/B、真实上游压缩事件与 Windows 桥实机仍留待后续；已知跟进：限流风暴结束后主链路可能停留在 fallback 直至重启（#10）。

### 升级说明

- 覆盖升级即可；新增配置键（`agy_customizations_mode`、`context_compaction_min_peak`、`context_compaction_drop_percent`）由宿主按 schema 自动补默认值，无需手改。
- 回滚旧版本请整目录替换并清理 `__pycache__`：本版新增若干文件，仅覆盖解压会残留不兼容旧文件。

## 0.5.3（正式发布）

Windows 深路径修复版：只调整文件系统路径处理，无功能、配置或协议行为变更。

### 问题修复

- **#8 Windows 深路径下首聊失败**：宿主数据目录过深时，首轮对话直接失败并报 `FileNotFoundError: ...agents\astrbot-chat.md`。实际原因不是父目录缺失，而是 Win32 `MAX_PATH`（260 字符）拒绝打开文件，错误被 Python 映射成与「目录不存在」同形的 `errno 2`。现在 Windows 下会话数据根目录统一转为扩展长度绝对路径（本地盘 `\\?\`、UNC `\\?\UNC\`），目录创建、文件写入、权限设置、失败清理，以及自检与登录临时目录都继承该前缀；相对路径与 `..` 在加前缀前先规范化，已有 `\\?\` / `\\.\` 前缀保持不变。Linux / Docker 短路径环境行为完全不变，Windows 用户不再需要开启 `LongPathsEnabled`。
- **回归覆盖**：新增 `tests/test_paths.py`，覆盖非 Windows 路径不变、盘符 / UNC / 命名空间前缀规则、相对路径规范化、259 / 260 / 400 / 800 字符边界、工具模式、写入失败清理、Runtime 与自检清理、登录临时目录；Windows 专属用例在其他平台自动跳过。

### 升级说明

- 覆盖升级即可，无配置迁移。
- Windows 下 `--gemini-dir`、子进程工作目录与 ACP `session/new` 收到的是扩展长度路径（`\\?\C:\...`）。若所用 `agy` 版本对前缀处理异常，请附 `agy --version` 在 issue 反馈。

### 验证

- Linux（Python 3.11 / 3.12）：`compileall` 通过，165 项离线回归中 157 项通过、8 项 Windows 专属用例按预期跳过。
- Windows（Python 3.11 / 3.12，windows-latest）：165 项中 163 项通过、2 项 POSIX 专属用例跳过，含 7 项长路径专属用例（259 / 260 / 400 / 800 字符边界、工具模式、写入失败清理、Runtime 与自检清理、登录临时目录）。

## 0.5.2（正式发布）

上架审查整改版：只调整运行时日志来源，无功能、配置或协议行为变更。

### 问题修复

- **日志记录器统一为宿主 logger**：`agy_provider/onboarding.py`、`sessions.py`、`telemetry.py` 不再使用 Python 内置 `logging`，也不保留 `ImportError` 回退分支，一律 `from astrbot.api import logger`；`host_compat` 继续由调用方（`main.py`）显式注入宿主 logger，未注入直接 `TypeError`。`telemetry._emit_json_line` 由 `getattr(logger, "info", ...)` 兜底改为直接调用宿主 `logger.info`，单参数字符串调用，行为等价。
- **离线测试补齐宿主 logger 夹具**：新增 `tests/astrbot_stub.py`，在无 AstrBot 环境注入受 `spec` 约束的 logger 桩；原 `assertLogs` 断言改为 `patch.object`，保留调用次数、参数个数与“日志不含答案正文”校验。

### 升级说明

- 覆盖升级即可，无配置迁移；`probe.py`、`mcp_bridge.py` 等独立入口仍不依赖宿主 logger，可脱离 AstrBot 运行。
- 依赖宿主 logger 的模块（`onboarding` / `sessions` / `telemetry`）现在必须在 AstrBot 进程内导入；脱离宿主导入会直接 `ModuleNotFoundError: astrbot`，与 `main.py`、`provider.py` 原有行为一致。

### 验证

- 156 项 Python 离线回归（155 通过、1 项 Windows 专属跳过）与前端交互测试通过；Ubuntu / Windows × Python 3.11 / 3.12 四组 CI 全绿。

## 0.5.1（正式发布）

本版是从 **v0.4.3 升级**时需要阅读的完整更新说明：合并了原计划的 **0.5.0 功能线**与 **0.4.4 修复线**，这两个中间版本均未单独发布，直接升级至 **v0.5.1** 即可。

### 新增功能

- **更稳妥的会话复用**：区分历史追加、裁剪和改写，减少本地历史窗口变化引起的不必要重建。新增 `session_reuse_mode`（`strict` / `structured`），默认沿用保守的 `strict` 模式。
- **聊天 / 工具调用分流**：新增 `agy_tools_mode`（`inherit` / `chat` / `tools`）。`chat` 实例不向 AGY 下发工具；`tools` 实例仍受全局 `enable_tools` 与宿主权限约束。首次设置向导推荐纯聊天使用 `chat`。
- **消耗与缓存统计**：新增管理员命令 `/agy cost`，只读汇总最近最多 100 轮的 Token 使用、缓存、会话复用率、失败消耗及重建原因，不额外发起模型请求。可选开启 `cost_log_enabled` 输出脱敏轮次日志，默认关闭。
- **失败抑制与配额冷却**：重复 `EMPTY` 失败默认在 30 秒内抑制；`QUOTA` 错误默认冷却 60 秒，可参考上游 `retry_after`（最长 300 秒），冷却期后仅允许一个 half-open 探测请求。只记录失败状态，不缓存成功回答，不自动重放工具操作。

### 问题修复

- **#4 模型保存后不显示（部分修复）**：自动创建并持久化 `provider_sources`，为保存的模型绑定 `provider_source_id`，解决 AstrBot 4.29 模型管理页找不到模型的问题。**连通性测试耗时较长尚未解决**，仍在 #4 跟踪。
- **#5 配置填错后无法恢复**：启动时修复旧配置里的越界整数并保存（如 `tool_max_calls`、`idle_ttl`）；自动处理 `max_concurrent > max_sessions` 冲突。WebUI 数值项增加范围滑杆及提示；运行中的即时校验仍会拒绝非法输入。
- **#6 微信 JPEG 被误判无效**：结构化识别 JPEG 的合法结束标记（EOI），裁掉结束标记后的附加数据；真正缺失 EOI 的截断图片继续拒绝。
- **#7 上游重试告警泄漏到聊天**：过滤单行、跨行 URL 续行、跨 chunk 的 `⚠ API error (attempt N)` 及流中断提示；重试成功只展示正常正文，重试耗尽按 `NETWORK` / `QUOTA` 等真实类别交还 AstrBot 处理；终止标记不再覆盖已经识别的错误码。

### 升级说明与已知限制

- 插件要求 **AstrBot >=4.29.4,<4.30**、**Python 3.11+**，还需在 AstrBot 实际运行环境中安装并登录官方 `agy` CLI。
- 原有默认值 `enable_tools=false`、`history_bytes=160000` 不变；新建实例的 `agy_tools_mode` 缺省为 `inherit`。手动选 `chat` 模式后，该实例不会执行工具调用。
- `structured` 复用依赖宿主提供可信的 `agy_context_parts`；目前 AstrBot 未提供该信息时会自动退回 `strict`，不是开启开关就一定减少 Token。
- **#4 响应慢问题仍开放**：连通性测试会进行真实生成，请求可能花费 40–80 秒；本版没有缩短这一流程。

### 验证

- **156 项 Python 离线回归测试**与 **12 项前端交互测试**通过；修正 Windows Fake CLI 的 UTF-8 编码问题后，Windows / Ubuntu × Python 3.11 / 3.12 四组 CI 均通过。
- 在真实 **AstrBot 4.29.4 + AGY CLI 1.5.8** 部署中执行修复相关的集成验收；使用受控网络断流验证重试告警不泄漏、错误能正确分类。
- 详细实现与测试样例保留在下方 **0.5.0 / 0.4.4 开发分支记录**，供维护者查阅.

## 0.5.0（开发分支，未单独发布；已并入 0.5.1）

- **A 安全会话复用**：
  - 新增 `session_reuse_mode`（`strict` | `structured`，默认 `strict`），保持 `history_bytes: 160000`、`enable_tools: false` 向后兼容默认值不变。
  - 引入 `PromptContext` / `prepare_context`（`stable_system` 与 `transient_data` 分层）；仅当配置为 `structured` 且宿主提供经校验的可信 `agy_context_parts` 时拆分瞬态块（以 `<agy_context_data>` 注入用户轮次消息），未提供或校验失败时自动退回 `strict` 严格整段比较，支持 `clear` 清空与 `retract` 强制重建。
  - 新增双账本（`host_expected` 与 `sent_window` 分离）、`compare_history`（`exact` / `host_trim` / `rewrite` / `append_gap` / `unknown`）与 `decide_reuse` / `classify_rebuild` 细粒度重建原因判定（含淘汰/退出 tombstone 记录）；工具续轮冻结 `source_revision` 与 `transient_hash`；`ToolSnapshot` 在发布与指纹计算前按 wire 名称排序。
- **C 聊天 / 工具分流**：
  - 支持提供商配置 `custom_extra_body.agy_tools_mode`（`inherit` | `chat` | `tools`，默认 `inherit`）与 WebUI「首次设置」实例角色选择（默认推荐 `chat`）。
  - `chat` 模式绝不下发工具、走精简自定义代理（`--agent astrbot-chat`，`tools: []` + `excludeDefaultComponents: true`），并拒绝工具续轮（`TOOLS_DISABLED`）；`tools` 模式仍严格受插件总闸 `enable_tools` 与 `tool_choice=none` 约束，不可越权绕过。
- **D 可观测性与 `/agy cost`**：
  - 新增 `agy_provider/telemetry.py`：`cost_log_enabled`（默认 `false`，避免刷日志）开启后，每个完整 ACP 轮在 `INFO` 级别输出一行脱敏 JSON（`event="agy.turn"`），记录 `input_other` / `input_cached` / `output`、`reused`、`rebuild_reasons`、`primary_reason`、`history_relation`、`usage_status`、`usage_source`、`cache_fields_present` 及字节指标；失败或取消前也会尽力保留最后一次观测到的 usage；无 usage 时输出 `null` 与 `unavailable`；使用 HMAC 对会话/底层会话标识脱敏，绝不输出提示词、正文、用户 ID 或凭据。
  - 新增只读管理员命令 `/agy cost`（不发起任何模型/探针请求），按实例汇总最近 100 轮缓存命中、复用率、失败消耗、不可用占比与主要重建原因，以及进程池水位和有效配置。
- **E 失败防重放与短期冷却**：
  - 新增 `agy_provider/failure_guard.py`：同会话/请求指纹的 `EMPTY` 响应默认 `empty_dedupe_ttl=30` 秒内短期抑制；按 `(provider_id, model)` 维度实施 `QUOTA` 冷却（默认 `quota_cooldown=60` 秒，支持从上游 `retry_after` 结构化提取并封顶 `300` 秒）及单一并发 half-open 探针恢复；只缓存失败标记（从不缓存成功正文），工具续轮与缺少身份标识的匿名请求不参与跨请求误抑制。

## 0.4.4（修复分支，未单独发布；已并入 0.5.1）

- 修复历史配置中的非法数值导致插件无法加载、且 WebUI 改回合法值仍无法保存/重载的问题（#5）：`Settings.from_dict` 新增 `on_repair` 启动修复模式，无效整数（如 `tool_max_calls>128`、`idle_ttl>3600`）自动恢复为默认值，`max_concurrent>max_sessions` 冲突自动钳制，修正值写盘并告警；探测与向导的即时校验保持严格拒绝。`_conf_schema.json` 为全部数值项补充合法范围滑杆与提示。
- 修复微信等客户端 JPEG 图片因 EOI 之后存在附加元数据被整体判为「图片无效」的问题（#6）：`_jpeg` 改为结构化遍历标记与熵流，在首个合法 EOI 处截止并裁剪尾部附加字节（包括附加数据中再嵌 FFD9 的情况）；真截断（缺 EOI）仍拒绝并报 `IMAGE_EOI`。
- 修复上游 CLI 重试告警「⚠ API error (attempt N): …」被当作助手正文发给用户的问题（#7）：新增 `RetryNoticeGuard` 流式过滤，跨 chunk 拆分的重试行不再泄漏分片；真实告警可能折行成多行（CLI 错误转储的 `"https://…"` URL 续行）或结尾为裸 `: EOF`，续行按行形状识别并随头行一并吞入诊断，`unexpected EOF` 与裸 `EOF` 尾变体均归类 `NETWORK`；新增过滤流中断续流提示「⚠ The stream was interrupted. Please continue …」并归类 `NETWORK`（CLI 状态文本全走 stdout 消息流、stderr 为空，为实测结论）；重试后带正常正文时只保留正文，整轮仅剩重试时按真实错误分类（`QUOTA`/`NETWORK` 等）抛出并触发宿主 fallback。告警形态基于真实 CLI v1.5.8 受控断流实测与 #7 野外样本对齐。
- 修复已保存模型不出现在「模型提供商」页的问题（#4，部分）：插件启动与向导保存时自动创建并持久化 `provider_sources` 来源、为模型绑定 `provider_source_id`（AstrBot 4.29 模型页按来源分组过滤，缺失绑定即不显示）；旧版宿主缺相应接口时跳过绑定并告警，不影响向导保存。连通性测试耗时较长（40–80 秒级）未在本轮处理，测试按钮仍执行真实生成请求。
- 测试：新增/扩充 37 项离线用例（配置自愈、JPEG 结构与裁剪、重试行全切分点与多行续行过滤、真实告警逐字夹具、流中断提示过滤、来源绑定回滚幂等与旧宿主降级），全量 133 项通过（1 项平台相关跳过）；`node --test tests/setup.test.mjs` 11/11。

## 0.4.3

- 修复上游 Antigravity CLI 容量不足（`503 UNAVAILABLE: No capacity available`）时，CLI 的重试告警被当作助手正文发给用户的问题：新增 `strip_upstream_capacity_notices`，过滤 `⚠ API error (attempt N)` 告警行、保留真实回答。
- 若整段只有告警、没有正文，改为抛出 `QUOTA` 错误交给 AstrBot 触发 fallback（如 `glm-5.2`），不再把告警原文发到聊天；日志只记录过滤条数，不记录正文或凭据。
- 新增 `tests/test_upstream_notice_filter.py`（告警+正文、纯告警、正常含 error 字样、空行/缩进变体、会话与 provider 路径），并保持原有离线测试全通过（96 项）。

## 0.4.2

- 重构插件前端面板 UI（`views/setup`）：移除大号衬线欢迎标语与冗余说明文字，改为现代极简黑白双栏卡片布局（移动端自适应单栏），自带浅色/深色主题一键切换并兼容宿主主题同步。
- 优化侧边栏入口命名：视图标题由「首次设置」调整为「Antigravity ACP」（`.astrbot-plugin/i18n/zh-CN.json`、`en-US.json`），避免在宿主左侧导航栏显示突兀的「首次设置」字样。
- 精简面板交互提示与折叠抽屉，完整保留无生成检测、官方 CLI 登录、动态思考档位探测、模型持久化与错误分类诊断语义。

## 0.4.1

- 改进 CLI 发现提示：“未在当前运行环境找到”不再暗示未安装；首次设置页显示默认搜索范围或显式 command 模式，强调 Docker 容器内路径。
- 新增 `COMMAND_NOT_FOUND`，区分显式路径无效与默认搜索未找到；显式配置仍不自动回退到其他 CLI。
- 新增 `STARTUP_EXIT` 与 `CLI_WRAPPER`：区分 ACP 握手前进程退出、旧包装脚本拒绝隔离目录，以及握手后的管道故障。只在观察到进程退出时记录退出码，支持 stdout EOF 与 stdin 关闭的竞态路径。
- 探测报告和页面补充失败阶段与退出码；识别已知脚本拒绝信息时保持脱敏，明确的 RPC 认证/协议错误不被 stderr 覆盖，成功请求不因背景告警误报失败。
- CLI 文件存在但脚本解释器或动态加载器缺失时报告 `LAUNCH`；版本/能力查询失败保留脱敏类别和退出码。
- 增补离线回归，并在现有 Linux Docker 部署验证旧脚本分类与原生 CLI 的无生成 ACP 检测。完整范围见 `TEST_REPORT_0.4.1.md`。

## 0.4.0

- 新增原生插件视图「首次设置」，串联环境检测、官方 CLI 登录、真实模型与思考档位选择、提供商保存和会话切换提示。
- 默认后台执行无生成预检；新增 `setup_auto_check` 开关、管理员 `/agy setup` 与 `/agy use <provider_id>`，`/agy doctor` 与向导共用检测服务和并发限制。
- 官方登录由 `agy acp --login` 完成：仅在管理页面展示授权链接与接收授权码，支持取消、超时和登录后 ACP 验证；不读取或自行刷新 OAuth token。
- 补充 Windows 原生 CLI 搜索路径；缺少 token 文件时允许 CLI 尝试系统钥匙串，现有不安全文件或链接失败仍明确报告 `AUTH_BRIDGE`。
- 保存前重新校验模型及档位，使用 AstrBot 接口创建、复用和启用提供商，可选设为默认；配置保存在宿主，重载后恢复显示。
- 识别跨流式分片的网络异常和高负载告警，分别报告 `NETWORK` / `RATE_LIMIT`；不新增自动重试或工具重放。
- 按交接文档更新 README，补充离线回归与 Linux/Windows、Python 3.11/3.12 CI；测试范围与真实宿主待验项见 `TEST_REPORT_0.4.0.md`。
- 修复宿主持久化失败后重试复用实例却不重新写盘的问题；复用保存通过宿主更新接口重新持久化并重载。
- 修复认证桥链接创建阶段的文件缺失错误被误当成原生认证回退的问题；仅官方源文件或目录缺失允许回退，链接失败继续阻断。

## 0.3.3

- 插件 ID 改为 `sl251/astrbot_plugin_antigravity_acp`，同步更新注册名、会话数据目录和安装路径。
- README 补充旧版配置迁移步骤。新旧插件共用 `agy_acp` 模型提供商类型，安装新版前请先停用旧插件。

## 0.3.2

- 移除 runner 兼容层的 Python 内置日志回退，统一使用插件入口传入的 AstrBot 日志记录器，符合插件市场的日志要求。

## 0.3.1

- 项目更名为 AstrBot Antigravity ACP，同步更新插件显示名和安装链接，保留原有插件 ID。

## 0.3.0

- 面向插件市场精简仓库：移除离线单元测试、内部开发文档与开发脚本，CI 改为源码语法检查；运行时代码与配置项与 0.2.0 完全一致。
- 协议合规完善：LICENSE 补充上游 [lili-luo/dsh-plugin-bundle](https://github.com/lili-luo/dsh-plugin-bundle) 版权行，README 新增「许可与致谢」章节。
- 元数据与描述统一改为 0.3.0（metadata、插件注册、ACP clientInfo/serverInfo）。

## 0.2.0

- 增加默认启用的文件认证桥：隔离会话只链接同用户的官方 token 文件；启动前检查存在性、大小、归属、父目录权限和符号链接边界。`auth_bridge=false` 恢复纯隔离行为。
- 增加插件级 `proxy_mode`（inherit/custom/direct）与 `proxy_url`，代理环境贯穿版本、能力、自检、目录和聊天的 AGY 子进程启动；不修改全局环境，不影响图片下载器。
- 增加 PROXY_AUTH、PROXY、NETWORK、TLS 错误分类，保留 AUTH、限流和额度分类；诊断持续排空 stderr，只保留有界的白名单类别与阶段，避免原始输出泄露。
- 增加管理员 `/agy doctor` 无生成自检，读取当前面板配置，设置总时限和并发保护；自检与 Runtime 使用同一插件数据根。
- 改善 CLI 未找到、不可执行和能力不足的反馈；显式错误路径不会静默回退。
- 重写市场用户安装与模型配置引导，补充认证桥、代理和 Docker 说明；插件初始化提示新增 `agy_acp` 来源。
- 统一 metadata、插件注册和 ACP clientInfo 为 0.2.0；补充市场短描述及标签。宿主范围收窄为 `>=4.29.4,<4.30`：兼容层包装内部 runner，只有 4.29.4 的真实宿主证据，不承诺未验证版本。
- 增加 Python 3.11 离线回归、Ruff 与配置/schema 默认值一致性 CI；运行时仍无新增第三方依赖。

已知限制：**凭据自然刷新与并发刷新行为未验证**，建议同一账号避免 CLI 与插件并发高频使用；CLI 原子替换符号链接后的持久化仍需真实观察。工具和图片默认关闭，Windows/macOS 认证及完整市场/WebUI 链路仍需单独验收。此次离线验证不发送真实模型请求。

## 0.1.0

- 基线提供官方 AGY CLI 的 ACP stdio Gemini provider、模型目录、文本/流式/多轮、思考档位、隔离会话与有界进程池。
- 提供默认关闭的宿主工具桥、PNG/JPEG 图片输入，以及仅作用于本 provider 的原生 runner 兼容层。
- 提供取消、重载、进程树回收、错误脱敏及 stdlib fake ACP 离线回归；市场改造起点为 151 项测试。
- 认证路径适配原由部署侧完成，尚无通用认证桥与插件级代理配置。
