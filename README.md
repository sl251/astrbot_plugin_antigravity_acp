# AstrBot AGY Gemini Provider

将本机已登录的官方 Google Antigravity CLI（agy）通过 ACP / stdio 接入 AstrBot，作为可选主聊天模型。**默认原生部署，不依赖 Docker，也不需要额外 HTTP 服务。**

> v0.1.0 开发版：离线回归已通过；2026-11-07 在 Linux 容器的 AstrBot **v4.29.4** / AGY 1.5.2 上，通过实例级认证路径适配完成真实文本生成和宿主模型测试。该认证适配尚未合入通用代码，刷新、工具、多模态及跨平台仍未验收；不把单实例成功视为稳定发布。见[测试交接](docs/TEST_HANDOFF.md)和[下一阶段任务书](docs/NEXT_PHASE_TASKBOOK.md)。

## 当前能力

- 注册 agy_acp（AGY Gemini (ACP)），从实际 ACP 目录读取 Gemini 模型。
- 普通/流式文本、多轮上下文、独立 reasoning 和模型实际支持的思考档位。
- 每个保留会话独立 AGY 进程，限制并发、排队、会话数量和空闲时间。
- 历史压缩、编辑、人格/模型变化、失败或取消后按 AstrBot 历史重建。
- 插件停用/重载清理；API Key 留空，使用官方 CLI 登录机制。

默认仅启用文本。M2 宿主工具桥和 M3 PNG/JPEG 图片输入已实现，分别由 `enable_tools` / `enable_images` 显式开启；真实 Provider + AstrBot runner 闭环须由维护者执行验收脚本。音频、视频、PDF、图片生成和跨机 IPC 保持关闭。受限 agent 配置禁止 AGY 内置工具，ACP 文件/终端/权限请求均拒绝。配置隔离不等于操作系统沙箱。

## 原生安装

1. 原生安装并正常运行 AstrBot 4.29.4 或更新的 4.x，使用宿主要求的 Python 环境。本插件无额外 pip 依赖，代码使用 Python 3.11+ 功能。
2. 在**同机、运行 AstrBot 的同一系统用户**下安装[官方 AGY CLI](https://github.com/google-antigravity/antigravity-cli)，运行 agy 完成登录。
3. 确认终端中 agy --version 和 agy acp --help 可运行。服务/GUI 启动的 AstrBot 可能没有终端 PATH，此时填写插件 command 绝对路径。
4. 把插件放到 AstrBot 数据目录的 plugins/astrbot_plugin_agy_provider/，其中直接包含 main.py、metadata.yaml 和 agy_provider/，不要再嵌套仓库目录。

主分支安装示例，在 AstrBot 根目录运行（Git 已具有该私有仓库的读取权限）：

```sh
git clone --branch main https://github.com/sl251/sl251-astrbot-plugin-agy-provider.git data/plugins/astrbot_plugin_agy_provider
```

仓库目前私有，AstrBot URL 安装器不一定有访问权限。也可下载主分支 ZIP，通过 WebUI 本地安装或手工解压。不要把 GitHub token 填入 provider Key。

5. 重启 AstrBot，启用插件并保存插件配置。
6. 在模型/提供商页面新增 **AGY Gemini (ACP)**（类型 agy_acp），无需 URL 或 Key。获取模型列表，选择实际返回的 Gemini ID，添加并启用模型。默认能力仅勾选**文本**。开启插件工具/图片开关后，已有模型配置还需勾选对应 `tool_use` / `image`；工具桥当前需要 POSIX Unix socket，图片须通过 CLI capability 检查。
7. 选择该模型作为聊天模型。WebUI 连通测试会实际生成一条短回复，可能消耗额度；获取模型列表不主动发送聊天。

### 操作系统

- Linux/macOS：查找 PATH，其次 ~/.local/bin/agy；用独立进程组回收子进程。
- Windows：指定官方原生 agy.exe，不支持 .cmd/.bat 包装器；使用 taskkill /T 回收进程树。
- 官方 CLI 管理 keyring/钥匙串登录。服务用户没有登录态时，用相同用户完成官方登录，插件不复制其他用户凭据。
- 三个平台的真实运行均待测试。Docker 不是前提；自行使用容器时，AGY 与登录环境也必须在容器可用。

## 配置

启动和资源限制放在**插件配置**，模型 ID 放在 AstrBot 模型配置。修改插件配置后重新加载插件。

| 字段 | 默认值 | 作用 |
|---|---:|---|
| command | 空 | AGY 可执行文件绝对路径；空则自动查找 |
| thought_level | default | CLI 默认档位，或 low / medium / high |
| startup_timeout | 60 | 每次启动检查/握手/模型配置超时，秒 |
| request_timeout | 180 | 单次生成超时，秒 |
| queue_timeout | 60 | 等待会话锁与并发名额的合计时限，秒 |
| max_concurrent | 2 | 每个模型实例最大并发；低配建议 1 |
| max_sessions | 4 | 每个模型实例最多保留进程；不能低于并发数 |
| max_pending | 16 | 每个模型实例最大排队数 |
| idle_ttl | 600 | 空闲回收时间，秒 |
| history_bytes | 160000 | 历史重放 UTF-8 字节预算，最大 160000 |
| max_output_bytes | 4194304 | 单轮正文与 reasoning 的合计字节上限 |

多个 AstrBot 模型实例的资源池独立，上限会累加。相同会话串行，跨会话可并行；超出保留数量时优先淘汰最久未使用的空闲会话。

单个模型可在 custom_extra_body 覆盖思考档位：

```json
{"agy_thought_level": "high"}
```

此处 default 表示继承插件默认值；插件也为 default 时使用 CLI 默认值。不可用档位明确报错。temperature、top_p、max_tokens 等采样参数无 ACP 映射，不生效。达到 AGY 输出上限时返回已有内容并记录提示，不自动续写或重试。

## 会话与数据

会话键组合 provider 实例、AstrBot session_id（来源）和 conversation_id。匿名 API/摘要/健康检查使用临时会话。群聊是否共享上下文由 AstrBot 决定。

新建/重建时，以带角色的 JSON 转录回放历史，再附最新问题；这与底层 Gemini API 原生多消息历史不同。只有历史前缀与此前完整轮次一致时才复用。旧历史超预算时按完整轮次移除并标记数量；当前问题不截断，超限直接报错。

data/plugin_data/astrbot_plugin_agy_provider/ 下的独立运行目录保存本会话 agent、系统提示和 CLI 设置，AGY 可能写入自己的会话数据。正常回收删除会话目录，强制结束/断电可能留下文件；停止 AstrBot 后只清理已确认的临时 runtime/session 目录。**不要整目录删除**：实例可能在同一数据树保存持久认证状态，必须区分并备份。插件不输出完整 prompt 或 CLI stderr，不提取/复制登录凭据；实例认证适配的边界见任务书。

启动要求 CLI 提供 acp、--gemini-dir、--app-data-dir、--agent；不要求 --process-per-session，不依赖私有 systemPrompt 扩展或会话恢复方法。模型列表最多缓存 5 分钟，真正建会话时再次校验。

## 当前边界

- 不实现客户端身份伪装、二进制补丁、账号池或 OAuth 反向代理。
- 工具开关默认关闭。启用后仅提供本轮 active 完整 ToolSet，支持 auto/none；required/指定函数不可靠实现，拒绝。HandoffTool 与 MCPTool（无 handler 但宿主可执行）可桥接；其他无 callable handler 的工具（如 skills_like 占位）自动跳过并记 WARN 日志，不中断请求。工具结果由宿主 runner 执行和回填；闭合旧历史按转录重建。
- AstrBot 若在 provider 前删除了不支持的媒体，插件无法恢复附件；请用宿主图片描述功能或其他视觉模型。
- 用量只映射最终响应实际提供的计数，缺失时保持未知，不读取 CLI SQLite 或猜测上下文窗口。
- 原生 CLI 调用不等于 Google 授权第三方机器人使用订阅，也不保证账号安全，使用范围以官方条款为准。

## 文档

- [实施计划](docs/PLAN.md)
- [下一阶段任务书：工具调用、多模态与可靠认证](docs/NEXT_PHASE_TASKBOOK.md)
- [M0 工具桥决策与预检交接](docs/TOOL_BRIDGE_DESIGN.md)（历史记录及后续实测补充）
- [测试矩阵](docs/TEST_MATRIX.md)
- [测试交接与协议契约](docs/TEST_HANDOFF.md)
- [第三方参考与许可](THIRD_PARTY_NOTICES.md)

离线回归：`python -m unittest discover -s tests -v`；基础静态检查：`python -m compileall -q .` 和 `git diff --check`。模拟 ACP 测试不使用账号、不调用模型；真实登录、AstrBot 完整安装和平台兼容仍须单独验证。

不生成内容的隔离预检：`python -m agy_provider.probe`，可加 `--command /absolute/path/to/agy`。
输出仅含脱敏阶段、错误代码和白名单能力，退出 0 表示建会话成功，退出 2 表示阻塞；不证明工具或图片可用。
`Authentication required` / ACP `-32000` 现在归类为 `AUTH`：普通 CLI 已登录仍失败时，应检查隔离认证搜索路径。
旧配置无需迁移，新工具/图片开关默认 false，无新增依赖；认证存储与刷新适配仍沿既有 M1 边界。完整配置、审批、取消与格式限制见 [M2/M3 实现交接](docs/M2_M3_IMPLEMENTATION.md)。


## M2/M3 开关与验收

| 新配置 | 默认值 | 作用 |
|---|---:|---|
| enable_tools / enable_images | false / false | 显式开启宿主工具桥 / 图片输入 |
| tool_max_calls / tool_max_concurrent | 16 / 1 | 单轮调用尝试 / 同时 pending 数 |
| tool_approval_timeout / tool_execution_timeout | 60 / 120 | 交接审批 / 交接后结果等待，秒 |
| tool_result_bytes | 262144 | 单轮工具结果总字节，不截断 |
| media_roots | [] | 明确授权的本地媒体目录，绝对路径且无符号链接 |
| image_max_count / image_max_bytes | 8 / 8388608 | 整个输入唯一图片数 / 原始总字节 |
| image_max_pixels / image_timeout | 20000000 / 15 | 每图像素 / 下载超时秒 |

本地图片必须位于授权目录；公网下载按 DNS、跳转和实际格式校验。纯图可以输入，重复附件按内容去重；临时原图失效会明确报错。工具直接 API 调用必须提供稳定 session/conversation 身份及 `abort_signal=asyncio.Event()`，在结束时设置该事件；原版 runner 已提供事件。额外审批使用可信宿主异步 `agy_tool_approver`，不能依赖 `on_tool_start` 抛错阻止执行。

主代理真实验收（本次实现者不运行）：

```sh
python3 scripts/real_loop_check.py
python3 scripts/real_loop_check.py --with-image
```

脚本要求官方 AGY 1.5.2、已有官方登录、SOCKS 出口 `127.0.0.1:10808`，以及可导入 AstrBot 4.29.4 的 Python 环境；支持 `--astrbot-source`。输出机器可读 PASS/FAIL 和 `EVIDENCE_JSON` 路径。认证 symlink 只在验收脚本，插件不访问凭据。成功标准包括原版宿主 runner 一次执行、运行时随机结果读回和原 ACP prompt 持续 outstanding；不能仅凭模型声称调用成功验收。
