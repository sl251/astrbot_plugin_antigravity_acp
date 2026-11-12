# v0.4.0 改造交接：README 与测试

## 状态与边界

- 用户已授权实现首次使用引导；明确由其他模型负责 README 和测试。本轮未修改 README、未新增或运行功能测试、未执行真实登录或模型生成。
- 基线：`7522812c31f3a9251835e073da4aa27230193252`（v0.3.3）。本轮工作是本地未提交改动，未推送、未发布。
- 当前实现版本：v0.4.0；宿主声明仍为 `>=4.29.4,<4.30`，Python 3.11+。未放宽跨平台兼容声明。
- 已做 Python 3.11 语法解析、JS module 语法检查、JSON 解析及 diff 空白检查。这些不能替代离线回归、真实宿主、登录和页面验收。
- 原项目参考提交：`lili-luo/dsh-plugin-bundle@0a90ec37c69430608084e33ebc2f891e328c9e89`。来源与许可已补充至 `THIRD_PARTY_NOTICES.md`。

## 用户实际入口与流程

1. 安装并启用插件后，默认后台运行无生成预检，不阻塞 AstrBot 初始化。`setup_auto_check=false` 可关闭自动预检。
2. 打开 **AstrBot WebUI → 插件 → Antigravity ACP → 首次设置**。原生视图位于 `views/setup/index.html`，无需额外端口或独立 Web 服务。管理员 `/agy setup` 返回入口说明、检测提示和已有提供商 ID。
3. 第一步显示系统、Python、CLI 版本、代理模式、模型数量及检查阶段。CLI 缺失时展示针对运行环境的官方安装命令和官方文档地址。用户在实际运行 AstrBot 的环境安装后，点「重新检测」。插件本身不执行下载安装脚本。
4. 第二步复用现有认证；需要时点「开始官方登录」。插件以 AstrBot 的系统用户运行 `agy acp --login`，共用插件代理设置，不改 HOME、系统钥匙串或全局 CLI 配置。授权由官方 CLI 自行保存。
5. 授权 URL 复制到外部浏览器打开；如果官方流程返回授权码，在管理页面回填。AstrBot 原生 iframe 限制弹窗和剪贴板访问，因此本页不依赖自动打开浏览器；复制失败时选中文本，提示 Ctrl+C / ⌘C。
6. CLI 退出成功后，必须继续通过实际 ACP 握手、建会话和模型目录检查才显示登录验证通过。CLI 退出码不是唯一成功依据。
7. 第三步选择账号实际返回的 Gemini 模型。切换模型会重新探测该模型的思考档位。`default` **继承插件全局 thought_level**；全局也为 default 时才使用 CLI 默认。
8. 点「保存并启用模型」，再次以新 ACP 会话校验模型和最终档位，然后调用 AstrBot 提供商 API。相同模型和相同显式档位复用现有 AGY 提供商；其他提供商不覆盖。可以勾选设为全局默认，默认不勾选。
9. 第四步显示已保存提供商及 `/agy use <provider_id>`。管理员在目标聊天会话执行该命令，只修改该会话的模型偏好。全局默认变更不覆盖已经有独立偏好的会话。
10. 检测和保存不发送模型提示。真实生成验证继续使用 AstrBot 模型连通测试或发送对话，须明确它会使用额度。

重载后显示的配置来自 AstrBot 提供商配置，而不是另一份插件状态文件；检测重新执行。未完成的登录不会跨重载恢复，需要重新发起。刷新页面可看到同一 Dashboard 用户仍在等待的登录，登录流程总预算 300 秒，等待 URL 最多 45 秒。

## 实现索引

| 文件 | 改动职责 |
| --- | --- |
| `main.py` | 注册引导服务、后台预检、`/agy setup`、`/agy use`、doctor 复用、停用清理 |
| `agy_provider/onboarding.py` | 状态、下一步提示、Web API、互斥操作、提供商复用/创建/启用/默认切换 |
| `agy_provider/login.py` | 官方登录子进程、跨 stdout/stderr 分片 URL 识别、授权码 stdin 提交、超时与取消、成功后检测 |
| `agy_provider/probe.py` | 可选管理员模型详情、阶段进度、指定模型与思考档位的无生成校验 |
| `agy_provider/models.py` | 保留模型名称、继续过滤真实 Gemini 目录；选择函数返回最新配置选项供向导使用 |
| `agy_provider/auth.py` | 文件缺失不提前判未登录；不安全文件/父目录/链接失败报告 AUTH_BRIDGE |
| `agy_provider/process.py` | Windows 默认安装路径、原生 exe 发现、Windows 子进程隐藏窗口 |
| `agy_provider/notices.py`、`transport.py` | 网络/高负载告警的有界跨分片识别；工具通知前刷新候选告警 |
| `views/setup/*` | 四步原生引导页面，明暗主题、移动布局、轮询、取消、模型保存、命令复制 |
| `.astrbot-plugin/i18n/*.json` | 宿主显示的视图标题和描述；页面正文当前为中文 |
| `_conf_schema.json`、`config.py` | setup_auto_check；路径与认证桥说明 |
| `metadata.yaml`、`transport.py`、`mcp_bridge.py` | 版本同步为 0.4.0 |

## 后端接口与生命周期

注册路由前缀：`/astrbot_plugin_antigravity_acp/setup/`。页面通过 `AstrBotPluginView.apiGet/apiPost` 调用 `setup/...`，不直接读取 Dashboard token。

| 方法 / endpoint | 请求字段 | 行为 |
| --- | --- | --- |
| GET `setup/status` | 无 | 脱敏检查结果、环境提示、已有模型、任务状态、CSRF nonce；只对登录发起的 Dashboard 用户返回等待中的 URL/ticket |
| POST `setup/check` | `csrf`, 可选 `model` | 开始后台无生成检测；轮询 status 取结果 |
| POST `setup/login` | `csrf` | 开始官方登录，轮询取 URL 与状态 |
| POST `setup/login-code` | `csrf`, `ticket`, `code` | 一次性授权码交给当前登录进程，HTTP 请求不等待完整授权结束 |
| POST `setup/login-cancel` | `csrf`, `ticket` | 校验归属并终止登录进程 |
| POST `setup/apply` | `csrf`, `model`, `effort`, `use_default` | 后台复检并保存、可选切换默认；轮询取结果 |

- 使用宿主 `astrbot.api.web` 请求/响应接口和 Dashboard 身份验证，handler 另检查 username。写操作要求 JSON、当前插件实例 nonce；请求体上限 16 KiB。
- 授权码长度 1–4096，拒绝控制字符；不入插件配置、业务日志或聊天回复。URL 仅接受官方 `accounts.google.com` HTTPS 地址，解析窗口有界。
- 普通检测、登录、保存互斥；登录码和取消针对当前随机 ticket，旧 ticket 不影响新流程。多页面同一 Dashboard 用户共享一个登录流程。
- 插件停用会移除本实例注册的 API handler，取消检测/登录，回收进程及临时目录。模型保存的宿主临界阶段屏蔽外部取消，先完成保存，再允许停用清理实例。
- 新建 ID 为 `agy_setup_<模型与档位摘要>`，冲突时加数字后缀。已存在匹配 AGY 模型按配置复用，停用实例重新启用。宿主已创建但设置默认失败时，结果和已有实例保留供重试。
- 登录 URL、ticket、nonce 都不写入磁盘；临时登录 cwd 在插件数据根下，持久认证仍由官方 CLI 维护。

## README 编辑任务

请以**本次实现与实际测试结果**为准修改，至少覆盖：

1. 将快速上手改成上述原生引导路径，增加首次设置页面截图（真实宿主验收后采集）。保留手动添加 Provider 的高级路径。
2. 明确“已有 CLI 时按引导即可配置”，不要写成自动安装 CLI、自动完成 Google 授权或跨平台全部验证通过。
3. 登录命令以官方文档确认的 `agy acp --login` 为主；不支持时交互运行 `agy`。旧 README 中的 `agy login` 需要重新核实或替换。
4. 说明 Windows 自动搜索 `%LOCALAPPDATA%/agy/bin/agy.exe`、系统服务用户和 Docker 容器边界。
5. 更新 `auth_bridge` 语义：缺文件时允许官方钥匙串认证；存在但不安全或符号链接创建失败仍阻断。Windows 文件桥的权限需求仍存在；关闭桥不是通用修复，只适用于真实可用的原生认证后端。
6. 加入 setup_auto_check、三个管理员命令、页面/API 授权范围、nonce 过期后刷新、取消/超时/重载行为。
7. 解释模型自动创建和复用、思考 default 继承规则、全局默认与会话模型的区别。
8. 错误表新增 AUTH_BRIDGE、LOGIN_URL、LOGIN_EXPIRED；NETWORK/RATE_LIMIT 现在也可能来自模型文本流中的 CLI 告警。
9. 将“已登录”“检查通过”“模型已保存”和“真实对话成功”区分开。不要把无生成探测宣称为完整连通性测试。
10. 删除或修正“规避封号风险”等无法保证的宣传措辞。本轮已调整 metadata 的对应描述，README 未动。
11. 更新当前版本及功能表；保留工具桥仅 POSIX、图片范围、长期 token 刷新未验证等现有实际限制。

官方来源（2026-11-12 查阅）：

- https://antigravity.google/docs/getting-started
- https://antigravity.google/docs/cli/acp/
- https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/docs/zh/dev/star/guides/plugin-pages.md
- https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/provider/manager.py

## 测试接手清单（本轮未执行）

建议优先用假 CLI 与假宿主做离线覆盖，再进入真实 AstrBot 4.29.4。用户授权后才做真实账户登录与生成验证，不要把授权材料放进测试产物。

| 优先级 | 场景 | 应确认的行为 |
| --- | --- | --- |
| P0 | 原生视图发现、bridge 初始化 | 插件详情出现首次设置，module 脚本正常加载，API endpoint 前缀一致，明暗主题可读 |
| P0 | 未授权、错误 nonce、跨用户 ticket | 读写拒绝；URL 不暴露给非发起用户；旧 ticket 不取消新登录 |
| P0 | 全新未安装 / 已安装未登录 / 已登录 | 分别展示安装引导、登录步骤、真实模型目录；后台失败不阻断 AstrBot |
| P0 | stdout/stderr URL 分片与 ANSI 终止、极长输出 | 不显示截断 URL、不输出原始日志、内存有界；无 URL 45 秒退出并回收 |
| P0 | 授权成功 / 非零退出 / 退出零但 ACP AUTH | 只有登录后 ACP 通过才成功；错误提示准确，密码输入清空 |
| P0 | 登录取消、页面刷新、插件停用和连续重载 | 无孤儿 CLI、无遗留 route、无临时目录残留；官方持久凭据不删除 |
| P0 | Linux 文件桥 / 原生钥匙串 / 不安全文件 | 文件缺失尝试原生认证；不安全的现有文件拒绝；不读取或复制 token 内容 |
| P0 | 保存模型、重复保存、已有禁用实例、ID 冲突 | 不重复创建匹配实例，不覆盖其他 Provider；重启后宿主配置仍在 |
| P0 | 创建失败、保存持久化失败、默认切换失败、保存中重载 | 状态不虚报成功；失败后可恢复；宿主和内存状态一致；取消不会留下半配置 |
| P0 | default/low/medium/high、模型切换、动态目录变化 | 仅显示当前模型报告的档位，保存前复检；default 按全局设置解析；目录失效明确报错 |
| P0 | `/agy use` 普通用户/管理员、禁用或非 AGY ID | 普通用户拒绝；只允许启用的 AGY 实例；只改变目标会话偏好 |
| P1 | 网络/高负载告警逐字分片、跨换行、带/不带 ⚠ Error: | 映射 NETWORK/RATE_LIMIT，候选缓存不超过 4096 字符，不当成功答复 |
| P1 | 正常引用告警后继续解释、思考流、工具前告警 | 普通解释保留，reasoning 不被当告警；工具交接前终止失败流；不重放工具 |
| P1 | 原有 Agent execution terminated 错误与流式队列 | 保持旧行为；取消、背压、输出预算和历史会话不回归 |
| P1 | Windows 默认路径、空格路径、.cmd/.bat、隐藏窗口 | 原生 exe 正常，包装脚本拒绝，不弹后台控制台；另验符号链接权限不足路径 |
| P1 | Linux/macOS/Windows 真实认证、Docker/systemd | 各后端真实验证，宿主和容器不能混淆；未验平台不要标注支持已确认 |
| P1 | 代理 inherit/custom/direct | 版本查询、探测、登录、真实对话一致；未修改宿主全局环境 |
| P1 | 多页面并发、旧轮询迟到、重复点击、网络中断 | 不重复登录/保存；旧响应不覆盖新操作；刷新恢复状态；复制失败有手动操作提示 |
| P1 | 320px/手机、明暗主题、键盘、读屏状态 | 表单可操作，无横向溢出；状态/error 可感知；敏感字段不持久化 |
| P1 | 真实文本/流式/工具/图片 smoke | 原有 Provider 能力不回归；无生成检测与真实聊天分别记录结果 |

流式告警识别的固有限制：ACP 把部分 CLI 错误作为普通文本发送，因此模型恰好只输出完全相同的告警句子仍可能被当作错误；无标记句子会等到消息结束再判定，后续解释文字会原样放行。请在测试中记录这项行为，不要宣传成可靠的结构化错误通道。

## 没有迁入的上游功能

本轮不包含自动安装 CLI、多账号轮换、OAuth 凭据提取/刷新、内部 Cloud Code API、客户端身份替换、二进制解锁、重启后 ACP session/load 恢复、长工具续等或 SQLite 精细用量。现有图片与工具边界保持原实现；认证与向导的真实跨平台结果仍待验收。
