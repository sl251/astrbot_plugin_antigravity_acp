<p align="center">
  <img src="logo.png" width="160" alt="AstrBot Antigravity ACP" />
</p>

# AstrBot Antigravity ACP (`astrbot_plugin_antigravity_acp`)

![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows%20%7C%20Docker-blue)
![Python](https://img.shields.io/badge/Python-3.11+-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.29.4%2C%3C4.30-orange)
![Status](https://img.shields.io/badge/Status-v0.7.2-brightgreen)

通过 Google 官方 `agy` 客户端内置的 ACP（Agent Client Protocol）协议，把 Antigravity 模型安全、稳定地接入 AstrBot，作为日常对话主力。

> 💡 **版本速递（v0.7.2，2026-11-17）**：修复 AstrBot 4.29.4 Safe Mode 第 4 条规则触发的上游伪 429（detail-free `RESOURCE_EXHAUSTED`）——定点等义改写该句中的 `style/role-play`，安全约束保持不变。详细改动见 [CHANGELOG.md](CHANGELOG.md)。

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

做这个插件最大的初衷，就是因为**被 Google 杀号杀怕了**。为了稳稳用上官方模型，本插件坚持**纯合规路线**：
- **不碰逆向，不写黑科技**：只调用本机官方 `agy` 客户端，走标准 stdio 管道和 ACP 协议通信。
- **免 API Key，零中转**：本地客户端自带鉴权，不需要搭建任何第三方 HTTP 反代服务，登录即用。
- **权限安全物理熔断**：`agy` 原生拥有读写本地文件和执行终端命令的 Agent 权限。为了防止机器人在聊天中被提示词注入破坏系统，插件在启动时**强制关闭了自带的所有本地文件与终端工具**，外部工具调用完全交由 AstrBot 自身的权限体系托管。

### 📌 功能边界一览

| ✅ 支持的功能 (Do) | ❌ 坚决不做的事 (Don't) |
| :--- | :--- |
| 调用本机官方 `agy` 客户端，走标准 stdio 管道通信 | **不做号池切换与多号轮询**（单实例仅绑定本机当前登录账号） |
| 把 AstrBot 的对话上下文、图片和工具调用桥接给 `agy` | **不做提示词破限与客户端伪造**（仅为已知宿主兼容问题定点处理 system，不改 IDE 指纹） |
| 提供 WebUI 向导，辅助完成官方登录与模型选择 | **不提取或存储 OAuth Token**（登录凭证全归官方客户端保管） |
| 独立的会话运行沙箱与进程清理机制 | **不开放未受控的本地终端与文件系统操作** |

---

## 2. 核心特性

- **官方通道直连**：原生 stdio 管道通信，享受 Google 官方客户端的稳定连接与低风控保障（这不构成账号安全或上游可用性的绝对保证）。
- **双模能力支持**：支持多模态识图（Vision）以及内置 MCP 桥接工具调用（Tool Use），可无缝调用 AstrBot 的各种技能插件。
- **WebUI 运行监控**：在 AstrBot 插件详情 →「首次设置」查看，按「概览 / 详细统计 / 高级诊断」分层展示会话池、近 24 小时用量与上下文缓存情况。纯本地轻量渲染，只读脱敏快照，不额外消耗模型额度。
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
在 AstrBot WebUI 插件管理页上传 [主分支 ZIP 包](https://github.com/sl251/astrbot_plugin_antigravity_acp/archive/refs/heads/main.zip)，或者直接在 AstrBot 根目录下克隆：
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
  - `/agy cost`：查看各实例只读缓存复用、近 24 小时 Token 统计与进程池摘要（不发任何模型请求）。

---

## 5. 更新日志（最近版本）

*(完整历史见 [`CHANGELOG.md`](./CHANGELOG.md))*

- **v0.7.2**：定点兼容 AstrBot 4.29.4 Safe Mode 第 4 条规则触发的上游伪 429（`style/role-play` 等义改写，fail-open，不改冷却语义）。
- **v0.7.1**：429 结构化标签过滤扩展至 AstrBot 4.30 已知声明，新增内容相关 429 候选遥测（`content_429_candidate`，不改冷却语义）；WebUI 监控三层重构与纯 SVG 可视化。
- **v0.6.1**：MCP 权限分层、工具失败归因、数组 schema 宿主契约与本地能力声明过滤。
- **v0.6.0**：疑似压缩后终态退役、强错误提前中断、部分输出防重放、Windows 认证 MCP 桥与 Job Object。

---

# 🤖 给 Agent 看的上下文

先读 [AGENTS.md](AGENTS.md)。当前发布基线为 **v0.7.2**；仅支持 AstrBot **`>=4.29.4,<4.30`**、Python **3.11+**。本节只保留当前有效契约，历史实现和既有验收见 [CHANGELOG](CHANGELOG.md)。

| 当前契约 | 详细说明与证据入口 |
| --- | --- |
| 模型请求只走官方 `agy acp` stdio；每会话隔离目录，认证由官方 CLI 管理，不读取或记录凭据内容 | [架构与认证](docs/architecture.md)，[启动/登录排障](docs/troubleshooting.md#启动登录与模型设置) |
| CLI 内置文件、终端、web search/browser 继续拒绝；只有当前活跃 prompt 中已观测且匹配的快照 MCP 调用可获唯一 `allow_once`，宿主审批/校验/预算仍有效 | [MCP 权限与 schema](docs/architecture.md#mcp-的两层权限与两份-schema)，[#12 排障](docs/troubleshooting.md#empty权限拒绝与-12) |
| 模型 schema 适配与宿主原始契约独立；执行按原始 schema 校验，两者均参与工具指纹 | [#13 参数排障](docs/troubleshooting.md#tool_arguments-与-13) |
| 默认严格会话复用；已输出正文/思考或交接工具后失败禁止重试/fallback，避免重复副作用 | [会话与防重放](docs/architecture.md#会话历史与防重放) |
| system 仅过滤已知宿主能力声明并定点兼容改写已知 Safe Mode 单句；4.30 文案处理不是 4.30 宿主兼容证明；`content_429_candidate` 是候选遥测，不是上游根因 | [过滤与 429 边界](docs/architecture.md#system-过滤与-429-的证据边界)，[#14 / #19 排障](docs/troubleshooting.md#rate_limit--quota-与-14) |
| 检测/保存不等于真实生成；用量缺失保留未知；监控只读现有脱敏快照，不发模型请求 | [排障 SOP](docs/troubleshooting.md)，[实机验收分层](docs/testing.md#实机验收) |
| 工具/图片默认关闭，要求插件与宿主双开关；不支持音频、视频、PDF 或图片生成 | [关键配置](docs/architecture.md#关键配置)、[配置 schema](_conf_schema.json) |

开发时按 [测试与交付规范](docs/testing.md) 记录通过/失败/跳过、环境与实机边界。定位错误直接用 [故障 → 源码 → 测试索引](docs/architecture.md#故障定位索引)，无需先读旧施工书。

---

## 致谢与协议

- 灵感来源：感谢 L 站大佬的 `dsh-antigravity-acp` 插件提供的思路。
- 开源协议：详见 [`LICENSE`](./LICENSE) 与 [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)。
- 免责声明：本插件仅作为本地官方 `agy` 客户端与 AstrBot 之间的协议适配器，与 Google 官方无关。请遵守 Google Antigravity 官方服务条款使用。
