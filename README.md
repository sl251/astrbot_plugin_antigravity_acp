# AGY Gemini Provider for AstrBot

通过本机已登录的官方 **Google Antigravity CLI（`agy`）**，将 Gemini 接入 AstrBot 的原生模型提供商流程。使用 ACP over stdio，支持文本、流式回复和多轮会话，无需额外 HTTP 服务或 pip 依赖。

**前置条件：先在 AstrBot 实际运行的用户和环境中按[官方 CLI 安装说明](https://github.com/google-antigravity/antigravity-cli)安装，并运行 `agy` 完成登录。** 插件不安装 CLI、不接管 OAuth，也不需要填写 Google token、API Key 或 URL。当前宿主兼容范围为 **AstrBot `>=4.29.4,<4.30`**；真实宿主只验证过 4.29.4，CLI 探针基线为 AGY 1.5.2。工具和图片默认关闭。

## 五步开始使用

1. **市场安装**：在 AstrBot 插件市场安装并启用 **AGY Gemini Provider**。市场条目尚未同步时，可在 WebUI 使用[公开仓库](https://github.com/sl251/sl251-astrbot-plugin-agy-provider)地址安装，或下载 ZIP 本地安装。手工安装时，`main.py`、`metadata.yaml`、`agy_provider/` 应直接位于 `data/plugins/astrbot_plugin_agy_provider/`。
2. **配置 CLI 和代理**：确认同一运行环境中的 `agy --version`、`agy acp --help` 可运行，并已执行 `agy` 登录。`command` 通常留空；服务进程找不到 CLI 时，填写可执行文件绝对路径。需要代理时选 `proxy_mode=custom` 并填写 `proxy_url`，保存后重载插件。
3. **无生成自检**：由 AstrBot 管理员执行 `/agy doctor`。自检读取当前插件面板配置，检查 CLI、隔离认证、ACP 握手、会话与模型目录；不发送 `session/prompt`。只有阶段完成、目录可用，才进入下一步。它不证明额度、指定模型生成或长期刷新可用。
4. **新增来源并选择聊天模型**：在模型提供商页面新增 **AGY Gemini (ACP)**，类型为 `agy_acp`。**Key / URL 无需填写**。获取模型目录，选择实际返回的 Gemini ID，添加并启用模型，再将它选为需要使用的聊天模型。插件注册模板不会自动创建来源或更改主模型、fallback。默认能力只勾选文本。
5. **宿主连通测试**：点击模型的连通测试，确认实际生成可用。这一步会向模型发送短请求，可能消耗额度。之后再开始聊天。

服务或 GUI 的 PATH 可能不同于终端。显式填写了错误的 `command` 时会报错，不会静默改用另一份 CLI。Windows 须使用原生 `agy.exe`，不支持 `.cmd` / `.bat` 包装器。

## 插件配置

启动、代理、认证桥和资源限制放在**插件配置**；模型 ID 放在 AstrBot 的模型配置。修改插件配置后重载插件，已有会话才会按新设置重建。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `command` | 空 | AGY 可执行文件绝对路径；空时查找 PATH、`~/.local/bin` |
| `auth_bridge` | `true` | 把隔离会话的认证入口链接到同一用户的官方 token 文件；关闭后恢复纯隔离行为 |
| `proxy_mode` | `inherit` | `inherit` 原样继承 AstrBot 环境；`custom` 覆盖 AGY 环境代理；`direct` 移除 AGY 环境代理 |
| `proxy_url` | 空 | `custom` 必填，仅支持 `http://`、`socks5h://`；必须有主机和端口；其他模式忽略残留值 |
| `thought_level` | `default` | CLI 默认，或模型实际支持的 `low` / `medium` / `high` |
| `startup_timeout` | `60` | 单次版本、能力、握手或会话操作时限；也是 doctor 总执行预算，秒（超时后仍等待进程清理） |
| `request_timeout` | `180` | 单次生成时限，秒 |
| `queue_timeout` | `60` | 等待会话锁与并发名额的合计时限，秒 |
| `max_concurrent` | `2` | 每个模型实例的最大并发，低内存机器可设为 1 |
| `max_sessions` | `4` | 每个模型实例最多保留进程数，不能低于并发数 |
| `max_pending` | `16` | 每个模型实例最多排队请求数 |
| `idle_ttl` | `600` | 空闲会话回收时间，秒 |
| `history_bytes` | `160000` | 历史重放 UTF-8 字节预算，范围 4096–160000 |
| `max_output_bytes` | `4194304` | 单轮正文与 reasoning 的合计字节上限 |
| `enable_tools` / `enable_images` | `false` / `false` | 显式启用宿主工具桥 / PNG、JPEG 图片输入 |
| `tool_max_calls` / `tool_max_concurrent` | `16` / `1` | 单轮最大工具调用尝试数 / 同时 pending 数 |
| `tool_approval_timeout` / `tool_execution_timeout` | `60` / `120` | 工具交接审批 / 交接后结果等待时限，秒 |
| `tool_result_bytes` | `262144` | 单轮工具结果总字节上限，不静默截断 |
| `media_roots` | `[]` | 明确授权的本地媒体绝对目录，不含符号链接 |
| `image_max_count` / `image_max_bytes` | `8` / `8388608` | 整个输入的唯一图片数 / 原始总字节上限 |
| `image_max_pixels` / `image_timeout` | `20000000` / `15` | 每图像素上限 / 读取时限，秒 |

代理示例：`http://127.0.0.1:7890`、`socks5h://127.0.0.1:1080`，IPv6 写作 `http://[::1]:7890`。URL 不能包含空白、控制字符、反斜线、query、fragment 或业务路径；代理认证中的特殊字符需百分号编码。请勿将带凭据的 URL 粘贴到诊断消息或公开 issue。

`custom` 对 AGY 子进程的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及小写键使用同一 URL，并仅让回环地址免代理（`localhost,127.0.0.1,::1`）。`direct` 删除这六个代理键，保留其余环境；系统 VPN、透明代理或外部包装器仍可能影响网络。代理失败不会静默直连。设置仅影响 AGY 子进程，**不改变 AstrBot、其他插件或图片下载器**；图片下载保持独立直连策略。

多个模型实例的资源池彼此独立，上限会累加。相同会话串行，不同会话可并行；超限时优先淘汰最久未使用的空闲会话。单模型可在 `custom_extra_body` 设置 `{"agy_thought_level":"high"}`；`default` 继承插件设置。`temperature`、`top_p`、`max_tokens` 没有 ACP 映射，不生效。

## 认证与 Docker

默认认证桥使用官方文件后端：`~/.gemini/antigravity-cli/antigravity-oauth-token`。插件只检查文件元数据和边界，并在隔离目录建立 `agy/antigravity-oauth-token` 符号链接；不读取、打印、上传、复制或自行刷新 token 内容，不链接整个 `~/.gemini`。目标须存在且非空，归属和父目录权限须安全，异常符号链接会被拒绝。会话清理删除链接本身，不跟随链接删除持久 token。`--app-data-dir` 保持相对路径 `agy`。

`auth_bridge=false` 会恢复旧版纯隔离、无桥接行为。其他 keyring 后端、Windows/macOS 的真实认证与符号链接权限尚未验证，不能据 Linux 文件方案推断兼容性。

Docker 用户需同时注意：

- CLI、登录文件及其归属必须对**容器内运行 AstrBot 的用户**可用；宿主机已安装、已登录不等于容器内可用。只授权必要路径，勿把整个宿主 HOME 当作会话目录。
- **容器内 `127.0.0.1` 指容器自身**。宿主机代理需使用容器可达的宿主地址并配置其监听和访问权限；具体地址由部署网络决定。
- 保存插件配置后重载。无生成自检与 Runtime 使用同一插件数据根下的独立临时目录，避免路径授权不同造成假失败。

## 自检与排错

管理员指令 `/agy doctor` 有总时限和并发保护；普通用户不可用。输出只包含阶段、CLI 版本、代理模式、模型目录状态等脱敏信息。不输出完整代理 URL、系统用户名、OAuth 地址、账号标识或原始 stderr。

也可在 AstrBot 对应环境运行 `python3 -B -m agy_provider.probe --command /absolute/path/to/agy` 做终端预检。终端入口使用自己的命令行配置，不会自动读取面板配置；代理默认继承终端环境。请从 AstrBot 工作目录运行，或用 `--data-root` 指向 Runtime 使用的插件数据根（`data/plugin_data/astrbot_plugin_agy_provider/`）；面板配置的验收以管理员指令为准。自检退出 0 表示通过，退出 2 表示阻塞。

| 错误类别 | 下一步 |
|---|---|
| `CONFIG` | 检查命令路径、代理模式与 URL 格式，修改后重载 |
| `NOT_INSTALLED` | 在 AstrBot 实际运行环境安装官方 CLI，或填写正确绝对路径 |
| `PERMISSION` / `LAUNCH` | 检查可执行权限、服务用户与平台子进程支持；不要改成 shell 命令 |
| `UNSUPPORTED` | 检查官方 CLI 的 ACP 与隔离启动能力，按官方方式处理版本 |
| `AUTH` | 用相同系统用户运行 `agy` 登录；检查官方 token 文件、归属、父目录权限与认证桥开关 |
| `PROXY_AUTH` | 代理认证失败（例如 HTTP 407）；检查代理服务认证，不要反复登录 Google |
| `PROXY` | 检查代理地址、端口、协议、容器可达性及代理服务状态 |
| `NETWORK` | 检查 DNS、网络出口及连接；不要将代理密码或原始日志发到聊天 |
| `TLS` | 检查系统时间、证书链及代理的 TLS 设置 |
| `TIMEOUT` | 按失败阶段检查网络与 CLI 响应，确认无卡住的代理，再考虑适当增加时限 |
| `RATE_LIMIT` | 请求频率受限，降低并发并稍后再试 |
| `QUOTA` | 检查官方 CLI 的额度或上游容量；不自动换账号或重放请求 |
| `UPSTREAM` / `TRANSPORT` / `PROTOCOL` | 按官方 CLI 检查模型和 ACP 兼容性，报告脱敏类别、阶段与版本 |

未知 JSON-RPC `-32000` 不直接等同于未登录，403 也不等同于封号。模型目录最多缓存 5 分钟；实际生成时仍会校验模型。获取目录、自检和宿主连通测试是不同操作。

## 已知限制与数据生命周期

- **凭据自然刷新和并发刷新行为尚未验证**。短会话成功不证明长期刷新可靠；建议同一账号避免 CLI 与插件并发高频使用。符号链接被 CLI 原子替换后的持久化行为仍需真实验收。
- 工具、图片默认关闭。已有模型开启这些能力时，除插件开关外还需勾选对应 `tool_use` / `image`，图片还须通过 CLI capability 检查。工具桥需要 POSIX Unix socket。
- 工具描述来自宿主 active ToolSet；原生 `FunctionTool.call()/run()`、MCP 和 handoff 由 AstrBot 原生执行器执行，不要求额外 callable `handler`。仅名称或 schema 无效时跳过。`skills_like` 在本 provider 内转为 `full`，并非实现原来的两阶段取参协议；不可靠的 `required` / 指定函数模式明确拒绝。
- 兼容层包装宿主内部 runner，当前只承诺已限制的宿主版本范围。递归同 ACP 子代理的容量、图片工具结果后缀、多次 yield、历史压缩及长工具超时仍有专项边界，见[原生兼容核验](docs/NATIVE_COMPAT_AUDIT.md)。
- AGY 内置文件、终端和权限请求仍拒绝；配置隔离不等于操作系统沙箱。音频、视频、PDF、图片生成及跨机 IPC 未实现。
- 历史按带角色的 JSON 转录重放，只有完整前缀一致时复用会话；人格、模型、历史编辑或失败后重建。旧历史按完整轮次移除，当前输入超限时报错。用量只映射最终响应实际提供的计数，缺失时保持未知。
- 会话文件位于 `data/plugin_data/astrbot_plugin_agy_provider/`，可能含系统提示和聊天数据，正常回收时删除。强制结束可能留下临时文件；停止 AstrBot 后只清理确认不再使用的 runtime/session 目录。**官方 token 位于独立认证目录，勿作为临时文件删除。**
- 本轮代码和 fake 测试不代替真实 AstrBot 从市场安装、完整 WebUI、长期认证刷新及跨平台验收。历史实例联调证据保留在文档中，不将其视为本次所有路径已实测。

## 开发验证与资料

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests
python3 -B -m scripts.check_defaults
ruff check .
git diff --check
```

离线测试只使用 stdlib、fake CLI 和临时假凭据，不需要账号、不发送真实 prompt、不消耗额度。CI 使用 Python 3.11 运行同类检查；AstrBot 自身的 Python 最低版本按宿主要求，不能用插件测试版本代替。

- [变更记录](CHANGELOG.md)
- [测试交接与历史证据](docs/TEST_HANDOFF.md)
- [M2/M3 实现与验收](docs/M2_M3_IMPLEMENTATION.md)
- [后续任务书](docs/NEXT_PHASE_TASKBOOK.md)
- [工具桥设计](docs/TOOL_BRIDGE_DESIGN.md)
- [第三方参考与许可](THIRD_PARTY_NOTICES.md)

真实生成验收脚本保留在 `scripts/`，须由维护者在明确授权的账号和环境中单独执行。插件不自动安装或升级 CLI，不接收 Google token，不自动建 provider、不更改主模型，也不实现账号池或 OAuth 接管。
