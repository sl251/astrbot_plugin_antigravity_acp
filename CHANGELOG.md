# 变更记录

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
