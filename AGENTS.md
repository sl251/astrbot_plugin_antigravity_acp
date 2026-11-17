# Agent 工作入口

本仓库把官方 `agy acp`（stdio）接为 AstrBot 原生 `agy_acp` Provider。当前发布基线 **v0.7.2**，Python **3.11+**，AstrBot **`>=4.29.4,<4.30`**；版本以 [metadata.yaml](metadata.yaml) 为准。识别 4.30 的已知宿主提示文案，不代表兼容 4.30 的 runner/API。Hermes、Codex、AGY 从本页按任务读取资料，无需通读历史施工书。

## 必须保持的边界

- 模型请求只走官方 CLI；不做 HTTP 反代、指纹伪造、号池或破限。隔离 `--gemini-dir`、`--app-data-dir` 与 workspace，拒绝 CLI 内置文件、终端、web search、browser 能力。
- 认证由官方 CLI 管理。文件桥只核验元数据并创建单文件链接；不得读取、解析、复制或记录 OAuth 内容、系统凭据。授权 URL/code/ticket、代理密码、完整提示词与敏感工具参数不得进入日志、报告或聊天。
- ACP 仅为当前活跃 prompt 中已观测、名称/ID/状态匹配的快照 MCP 调用选择唯一有效 `allow_once`；不得永久授权。跨会话、过期、重复、未知和内置工具继续拒绝。放行不等于执行，宿主仍负责审批、原始 schema 校验、预算与执行隔离。
- 模型适配 schema 与宿主原始 schema 分开保存；执行按原始契约校验，两者均参与快照指纹。不得为兼容 Gemini 收窄宿主允许的数组值。
- 已输出正文/思考或交接工具后失败，保留 `no_retry` + `no_fallback`，禁止自动重放。修改宿主兼容层须核对声明版本的真实 runner，不能只依赖异常属性。
- system 只过滤已知失实宿主能力声明，并对结构锚定的 Safe Mode 已知单句做 #19 等义改写；保留其他人格与用户历史，不做通用删词或净化重试。`content_429_candidate` 仅为候选遥测，不能证明 Google 内容过滤、风控或额度根因。
- 无生成自检、保存、真实生成分别验收；缺失 usage/退出码是未知，不能填成 0 或成功。监控只读，不得额外发模型请求或泄漏原始会话身份。

## 工作顺序

1. 先看 issue 正文/评论、`git status --short --branch` 和实际基线；保护已有改动，必要时新建 worktree。
2. 从下表定位源码与既有测试，先保留最小失败证据，再修改。区分事实、推断、未验证，不用放宽安全边界或删除正确断言让测试通过。
3. 按 [测试与交付规范](docs/testing.md) 执行定向检查；行为变更再跑全量离线回归。真实 CLI/宿主验证单独记录，不把离线桩当作生产证据。
4. 检查文档与源码一致并交付差异、测试结果、限制和回滚方式。文档任务仅改文档，不顺带改运行行为，不预设下个版本号。

## 按需阅读

| 任务 | 首读 | 关键入口 |
| --- | --- | --- |
| 安装、配置与日常使用 | [README](README.md) | [配置 schema](_conf_schema.json)、[Settings](agy_provider/config.py) |
| ACP、会话复用、工具交接、防重放 | [架构与源码导航](docs/architecture.md) | `provider.py` → `sessions.py` / `tool_turn.py` → `transport.py` |
| 报错、启动、登录、工具、模型、监控 | [排障 SOP](docs/troubleshooting.md) | 按错误码找证据、源码及测试 |
| #12 MCP 权限 / #13 数组 schema / #14 本地能力 429 / #19 Safe Mode 429 | [故障定位索引](docs/architecture.md#故障定位索引) | 源码与测试按症状直达，#19 先读 [429 排障](docs/troubleshooting.md#rate_limit--quota-与-14) |
| 最小复现、测试命令、实机验收与报告 | [测试与交付规范](docs/testing.md) | [CI 矩阵](.github/workflows/test.yml) |
| 发布历史与既有验收证据 | [CHANGELOG](CHANGELOG.md) | 仅在追溯指定版本时读历史计划/报告 |

常用全量离线命令（仓库根目录，Python 3.11+、Node.js；CI 使用 Node 22）：

```sh
python -m unittest discover -s tests -v
node --test tests/setup.test.mjs
python -m compileall -q main.py agy_provider tests
git diff --check
```

本地通过不能代替当前提交的 CI、真实 CLI、生产或独立审读；交付时分别写明。
