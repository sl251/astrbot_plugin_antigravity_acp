<p align="center">
  <img src="logo.png" width="160" alt="AstrBot Antigravity ACP" />
</p>

# AstrBot Antigravity ACP (`astrbot_plugin_antigravity_acp`)

![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20Docker-blue)
![Python](https://img.shields.io/badge/Python-3.11+-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.29.4%2C%3C4.30-orange)
[![主分支版本](https://img.shields.io/badge/main-%E7%89%88%E6%9C%AC-brightgreen)](metadata.yaml)

通过 Google 官方 `agy` 客户端内置的 ACP（Agent Client Protocol）协议，把 Antigravity 模型接入 AstrBot，作为日常对话主力。

> 💡 **主分支 v0.9.1（2026-11-28）**：纯聊天与无工具路径改用官方 CLI 的默认 Agent + `AGENTS.md`，不再使用 `--agent` 自定义智能体，避免 CLI 1.5.17+ 把这类会话按 4 倍计入额度（[#52](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/52) / [PR #53](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/53)）；跟进加固自检透传、无工具路径防重放与护栏（[PR #54](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/54)）。详见 [CHANGELOG](CHANGELOG.md#0912026-11-28)。

---

## 💡 为什么这份 README 分成了两部分？

现在大家装插件、查报错或者改代码，很多时候会直接把仓库丢给各种 AI 助手。但**人类和 AI 读文档的需求完全是反过来的**：
- **人类看文档**：这插件干啥用的、好不好用、怎么最快装上并不踩坑。
- **AI 看文档**：协议契约、会话生命周期、参数规范和排障 SOP。

遇到疑难杂症时，直接把本 README 或仓库链接丢给你的 AI 助手，它读完下半部分就能帮你定位，然后烧你的token帮我修插件（目移）

📖 **快速跳转**：
- [👤 给人类看的说明（痛点背景 / 快速上手 / 避坑指南）](#-给人类看的说明)
- [🤖 给 Agent 看的上下文（架构契约 / 配置规范 / 排障入口）](#-给-agent-看的上下文)

---

# 👤 给人类看的说明

## 1. 为什么做这个插件？

折腾过反代、抓包提取 OAuth Token、或者接过野生号池的朋友应该都懂：**账号风控是玄学，哪天睡醒号就没了**。

做这个插件最大的初衷，就是因为**被 Google 杀号杀怕了**。所以这个插件选择直接走官方 CLI 和 ACP，而不是反代：
- **不碰逆向，不写黑科技**：只调用本机官方 `agy` 客户端，走标准 stdio 管道和 ACP 协议通信。
- **免 API Key，零中转**：本地客户端自带鉴权，不需要搭建任何第三方 HTTP 反代服务，登录即用。
- **权限安全默认熔断**：`agy` 原生拥有本地文件与命令执行能力；为防止提示词注入越权破坏系统，插件默认拒绝所有内置工具，仅在开启工具总闸后支持于 Linux CLI `1.5.17` / `1.5.18` 按需单次授权读取隔离会话工作区顶层 `.txt`（或独立开启实验性 `search_web`），文件编辑/写入、终端与浏览器保持锁定拒绝，其余外部工具调用交由 AstrBot 自身权限体系托管。

近期社区也有 ACP 客户端用户反馈账号受限，具体原因尚不明确。官方协议不代表所有接入场景都获得了授权，使用前建议了解 [服务条款](https://antigravity.google/terms) 和 [官方 FAQ](https://antigravity.google/docs/faq)。

### 📌 功能边界一览

| ✅ 支持的功能 (Do) | ❌ 坚决不做的事 (Don't) |
| :--- | :--- |
| 调用本机官方 `agy` 客户端，走标准 stdio 管道通信 | **不做号池切换与多号轮询**（单实例仅绑定本机当前登录账号） |
| 把 AstrBot 的对话上下文、图片和工具调用桥接给 `agy` | **不做提示词破限与客户端伪造**（仅为已知宿主兼容问题定点处理 system，不改 IDE 指纹） |
| 提供 WebUI 向导，辅助完成官方登录与模型选择 | **不提取或存储 OAuth Token**（登录凭证全归官方客户端保管） |
| 独立的会话运行沙箱与进程清理机制 | **不开放未受控的本地终端与文件系统操作** |

---

## 2. 核心特性

- **官方通道直连**：通过官方 CLI 进行原生 stdio 管道通信，无需额外搭建反代服务。
- **双模能力支持**：支持多模态识图（Vision）以及内置 MCP 桥接工具调用（Tool Use），可无缝调用 AstrBot 的各种技能插件。
- **可视化运行监控与管理面板**：运行监控、模型管理、环境与认证、工具权限页签协同，含会话池分段条、环形统计、Token 堆叠与延迟图，支持原位编辑、漂移修复、受控默认迁移及主题切换；路由和失败诊断为只读展示，不额外发起模型请求；账号额度按需刷新，近 24 小时脱敏请求明细跨重载保存。
- **智能防雪崩**：具备 429 限流快速熔断、错误提前中断与疑似压缩后的上下文退役机制，避免异常重试浪费资源。

---

## 3. 快速上手（三步完成）

### 第一步：安装官方 `agy` 客户端
确保运行 AstrBot 的机器上已安装 Google 官方命令行工具 `agy`（必须与 AstrBot 运行在**同一个系统用户**下）。参考 [Google 官方文档](https://antigravity.google/docs/getting-started) 安装完成后，在终端验证：
```bash
agy --version
agy acp --help
```
> 注：插件不会自动下载二进制文件。Windows 用户请确保使用的是原生 `agy.exe`，不支持 `.cmd` 或 `.bat` 包装脚本。

### 第二步：安装本插件
在 AstrBot WebUI 插件管理页上传 [最新 Release 纯净包](https://github.com/sl251/astrbot_plugin_antigravity_acp/releases/latest) 或 [主分支 ZIP 包](https://github.com/sl251/astrbot_plugin_antigravity_acp/archive/refs/heads/main.zip)（已自动剔除测试与开发文件），也可以直接在 AstrBot 根目录下克隆：
```bash
git clone --branch main https://github.com/sl251/astrbot_plugin_antigravity_acp.git data/plugins/astrbot_plugin_antigravity_acp
```
安装后重载或重启插件。

### 第三步：配置与启用（首次设置）
打开 **AstrBot WebUI → 插件 → Antigravity ACP → 首次设置**（也可以用管理员账号给机器人发送 `/agy setup`）：
1. **环境检测**：页面会自动检测本机 `agy` 是否就绪。
2. **官方登录**：点击「开始官方登录」，复制生成的授权链接到浏览器完成登录；需要手动提交时只粘贴浏览器给出的单行授权码（当前不解析完整 localhost 回调 URL），**注意不要把授权码发到公开群聊里**。
3. **选择模型**：拉取当前账号实际可用的模型列表，挑选所需模型并调节思考档位（`default` / `low` / `medium` / `high`），点击 **「保存并启用模型」**。

之后想换模型，用 `/agy use <provider_id>`，或直接在 AstrBot 的提供商列表里切换。纯聊天建议在首次设置中选 `chat` 角色，避免工具 schema 额外占用上下文空间。

---

## 4. 避坑指南（FAQ）

- **国内网络连接超时**：
  若服务器直连 Google 不通，可以在插件配置中将 `proxy_mode` 设为 `custom`，并在 `proxy_url` 填入本地代理地址（如 `http://127.0.0.1:7890`）。该代理仅注入给 `agy` 子进程，不影响 AstrBot 里的其他国内模型。
  *(Docker 部署注意：容器内的 `127.0.0.1` 指向容器自身，请填写宿主机内网 IP 或 `host.docker.internal`)*
- **工具调用（Tool Use）与识图（Vision）没反应？**
  本插件采用双重安全门禁，两处均需开启：
  1. 先在 **插件配置** 中开启 `enable_tools` 或 `enable_images`，保存并重载插件；
  2. 再去 AstrBot 的 **模型 / 提供商配置** 中，为对应模型勾选 `tool_use` 或 `image` 能力。

  从 `v0.5.1` 起还可用 `custom_extra_body.agy_tools_mode`（`chat` / `tools` / `inherit`）做聊天/工具分流：纯聊天用 `chat`（不注入工具 schema），需要工具的会话用 `tools`（仍受插件总闸 `enable_tools` 控制）。
- **关于连通测试与额度**：
  插件「首次设置」里的环境检测、登录与拉取模型列表完全免费、不消耗模型额度；但 AstrBot 模型列表自带的「连通测试」会实际发送一条打招呼测试消息，消耗少量额度。另外，保存成功只代表协议握手通过，不等于实际对话一定可用。
- **触发 429 限流问题**：
  0.7.1 起已自动过滤 AstrBot 4.29.4 与 4.30 已知的失实本地能力声明，但这不等于完整支持 4.30（当前支持 `>=4.29.4,<4.30`）。若开启「使用电脑能力 → 本机环境」后仍出现 429，可暂在模型配置中改回「不允许任何环境」；这是针对该现象的临时对照，不是所有 429 的通用修复。
  0.7.2 另对 [#19](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/19) 的 Safe Mode 第 4 条规则实施结构锚定的等义改写，保留安全约束（实机已验收：不再出现该伪 429）；详见 [CHANGELOG](CHANGELOG.md)。
- **只读诊断与统计指令**：
  - `/agy doctor`：进行无损连通性与会话自检。
  - `/agy cost` 查看各实例近 24 小时的缓存复用、Token 与进程池摘要。已结算的脱敏统计可跨插件重载恢复。
  - `/agy quota` 手动查询官方 CLI 账号额度，`/agy usage [30|90|365]` 查看本地每日用量，`/agy help` 列出全部指令。
  - 进入运行监控时按需更新账号额度，60 秒内优先使用缓存。也可点击「刷新额度」立即查询。剩余额度通过官方 CLI 的 `/usage` 获取，目前 ACP 尚未提供原生额度查询接口，该方式在第三方客户端中的支持情况仍待官方确认。
  - QQ 私聊中 `/new` 正常、`/agy cost` 被当成聊天时，请在该会话实际使用的 AstrBot 配置「可用插件」中勾选 `astrbot_plugin_antigravity_acp` 并保存，再发 `/agy help`。
- **工具跑得久之后出现 `TOOL_STALE` / `TOOL_TIMEOUT`，或回复与工具结果无关**：确认已加载含 #45 修复的版本。宿主迟到的工具结果满足会话与原调用校验时可用于一次无工具生成，禁止重发工具。详见[宿主工具执行超时后的回填](docs/troubleshooting.md#宿主工具执行超时后的回填)。
- **同一会话连续出现 `history_rewrite`**：升级至含 #33 修复的 v0.8.2 后，在同一会话连续发送消息，检查后续轮是否显示 `reused=true`。真实历史编辑等情况仍会触发重建。详情见 [会话复用排障](docs/troubleshooting.md#会话复用与历史重写)。
- **工具回看图片后回复与图片无关，日志有 `TOOL_HISTORY` 并切到 fallback**：升级至含 #48 修复的 v0.9.0。宿主会在工具结果后追加一条图片消息，旧版把它当作被改写的历史。带图续轮仍要求标注与缓存路径逐一匹配，不匹配时照旧拒绝。
- **带图消息报 `IMAGE_PATH 图片不在已授权媒体目录内`**：`media_roots` 未包含该图片所在的本地目录。宿主会把附加图片块（含其他插件注入的自动附图）落盘到 AstrBot 的 `data/temp`，Docker 下即 `/AstrBot/data/temp`，需加入 `media_roots` 后重载插件；QQ 直接发送的 http 图片走下载分支，不受此限（[#50](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/50)）。

---

## 5. 更新日志（最近版本）

*(完整历史见 [`CHANGELOG.md`](./CHANGELOG.md))*

- **v0.9.1**（2026-11-28）
  - 纯聊天、禁用工具、`tool_choice=none`、无工具收尾与自检不再走 `--agent` 自定义智能体，统一用官方 CLI 的默认 Agent + `workspace/AGENTS.md` 承载人设与护栏。自 CLI `1.5.17` 起自定义 Agent 按 4 倍计入 Gemini 额度，此改动让日常 `chat` 流量回到 1 倍（[#52](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/52) · [PR #53](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/53)）。
  - 自检同步透传 `agy_customizations_mode`，旧 CLI 开 `minimal` 时自检即阻断，不再出现自检报绿而对话全 `UNSUPPORTED`；无工具会话遇到未登记内置工具补齐 `no_retry`/`no_fallback`，拒止文案不再向纯聊天用户建议切换模式；护栏补齐全部声明工具及 `read_url`/`execute_url`/`swarm` 等禁令（[PR #54](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/54)）。
  - 注意：默认 Agent 会向模型声明内置工具，纯聊天对 `search_web` 等免事前审批工具改为依赖护栏 + 事后中止，不再有旧版 `tools: []` 的声明级隔离。
  - 验证范围与未验证项见 [CHANGELOG 0.9.1](./CHANGELOG.md#0912026-11-28)。
- **v0.9.0**（2026-11-28）
  - 修复：模型调用看图类工具后，回复与图片无关或直接掉到备用模型（`TOOL_HISTORY`）。宿主会在工具结果之后再追加一条图片消息，旧版把它当作被改写的历史；现在带图的续轮正常完成，不匹配的仍照旧拒绝。
  - 工具结果里的图片经 MCP 回传给官方 CLI；CLI 不支持图片时只送文本标注。
  - 每日用量热力图修复悬停抽动与月标重叠；监控页新增可折叠的「历史调用记录」面板（只读接口 `GET monitor/turns`）。
  - 升级须知：`media_roots` 需包含宿主落盘图片的目录（Docker 默认 `/AstrBot/data/temp`），否则自动附图会报 `IMAGE_PATH` 并整轮 fallback。
  - 验证范围与未验证项见 [CHANGELOG 0.9.0](./CHANGELOG.md#0902026-11-28)。
- **v0.8.6**：工具执行超时后晚带回的结果在会话、运行与历史都匹配时重建一次无工具生成，不再抛 `TOOL_STALE` 或切到 fallback；不匹配时返回 `TOOL_TIMEOUT`。跑满步数后的收尾请求不再报 `TOOL_CONTEXT`（[#45](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/45) · [PR #46](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/46)）。
- **v0.8.5**：额度按需自动刷新、60 秒缓存与失败冷却；新增有界 SQLite 监控明细持久化（24 小时、每实例最多 100 轮），重载后恢复请求、耗时、Token 与复用统计，存储异常时降级为内存统计（[#43](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/43) · [PR #44](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/44)）。
- **v0.8.4**：新增独立「工具权限」页签（`setup/tool-policy`）、官方 CLI 账号额度查询（`setup/quota-refresh` / `monitor/quota`）与本地每日用量热力图（`monitor/daily/*`），精简工具权限说明、汉化额度窗口与折叠长列表并重构 `/agy` 管理员指令可读性（[#38](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/38) · [PR #39](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/39)）；完成 CLI `1.5.18` 原生工具协议实测，开放 `read_file` 至 `1.5.18` 与只读分页校验，解除原生工具对宿主 MCP 非空的依赖并修正非搜索拒止文案（[#41](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/41) · [PR #42](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/42)）。
- **v0.8.3**：重构「运行监控 / 模型管理 / 环境与认证」三标签页、新增原位编辑（`provider-update`）与配置漂移修复（`provider-repair`）、支持受控默认迁移及思考「跟随全局」明示（[#36](https://github.com/sl251/astrbot_plugin_antigravity_acp/issues/36) · [PR #37](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/37)）。
- **v0.8.2**：修复临时用户文本导致的会话误重建，覆盖普通续聊及工具回填。[#35](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/35)
- **v0.8.0**：原生 Dashboard 重构、默认模型路由核对、当前用户输入与历史分离、历史图片预算修复、无正文且未交接工具时的安全 fallback、持久失败诊断（[#34](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/34)）。
- **v0.7.5**：修复设置面板复制按钮在 sandbox iframe + 非 HTTPS 下全部失效（#28）——三级降级复制、按钮就地反馈、授权链接「在新标签打开」；轮询差量渲染与 5/10/20/30s 退避、bfcache 恢复、监控重建签名跳过、浅色对比度达 AA、三态主题持久化等 21 项 UI 修正（[#29](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/29)）。
- **v0.7.4**：修复会话重建时历史窗口图片被重复提取为当前轮附件（#24）——仅当前最新用户消息的图片随请求附带，历史图在转录中保留哈希占位；跨轮重发同图不被误去重，预算裁剪不丢当前轮图片；当时仍会预检历史图片（该限制在 v0.8.0 修复）；新增 8 项单元回归与 3 项会话级集成测试（[#25](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/25)）。
- **v0.7.3**：修复 Python 3.12+ 工具桥关闭死锁与同会话并发请求误杀活跃工具轮；补全 `anyOf`/联合类型数组 schema 适配；无损剥离 PNG 压缩辅助块并支持 8 位索引色；CLI 探测缓存、长历史二分裁剪与 ZIP 归档瘦身（[#22](https://github.com/sl251/astrbot_plugin_antigravity_acp/pull/22)）。
- **v0.7.2**：定点兼容 AstrBot 4.29.4 Safe Mode 第 4 条规则触发的上游伪 429（`style/role-play` 等义改写，fail-open，不改冷却语义）。
- **v0.7.1**：429 结构化标签过滤扩展至 AstrBot 4.30 已知声明，新增内容相关 429 候选遥测（`content_429_candidate`，不改冷却语义）；WebUI 监控三层重构与纯 SVG 可视化。
- **v0.6.1**：MCP 权限分层、工具失败归因、数组 schema 宿主契约与本地能力声明过滤。
- **v0.6.0**：疑似压缩后终态退役、强错误提前中断、部分输出防重放、Windows 认证 MCP 桥与 Job Object。

---

# 🤖 给 Agent 看的上下文

先读 [AGENTS.md](AGENTS.md)，版本与宿主范围见 [metadata.yaml](metadata.yaml)。模型与认证只走官方 CLI，内置工具默认拒绝。输出或工具交接后的失败禁止自动重放。已核验的工具结果可作为文本历史用于一次无工具生成。

- 改模型、路由、权限、额度或监控接口，读[对应接口](docs/api.md)。
- 改会话、工具桥、防重放或持久化，定位[架构中的对应章节](docs/architecture.md)。
- 查报错，直接用[症状 → 源码 → 测试 → 排障](docs/troubleshooting.md#故障定位索引)。
- 开发与交付按[测试规范](docs/testing.md)，历史验收范围查[计划状态](docs/plan-status.md)。

---

## 致谢与协议

- 灵感来源：感谢 L 站大佬的 `dsh-antigravity-acp` 插件提供的思路。
- 开源协议：详见 [`LICENSE`](./LICENSE) 与 [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)。
- 免责声明：本插件仅作为本地官方 `agy` 客户端与 AstrBot 之间的协议适配器，与 Google 官方无关。请遵守 Google Antigravity 官方服务条款使用。
