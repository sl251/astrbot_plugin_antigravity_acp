# astrbot-plugin-agy-provider

> 规划阶段 / Planning only — 尚无可安装的 AstrBot 插件实现。

将本机已登录的 Google Antigravity CLI (`agy`) 通过 **ACP / stdio** 作为 AstrBot 的模型 Provider 接入，目标是让它出现在 AstrBot 的聊天模型选择器中，而不只是通过 `/agy` 指令执行任务。

## 项目定位

- **主要目标：** 提供 `agy_acp` AstrBot Provider，支持普通多轮聊天、流式输出、会话隔离、取消请求和模型选择。
- **接入方式：** 本地官方 `agy` 子进程 + Agent Client Protocol（ACP）通信；不构建 Google OAuth 反向代理、不导出登录凭据、不提供 OpenAI 兼容 HTTP 转发服务。
- **迭代顺序：** 文本聊天 → 会话/流式与稳定性 → 多模态 → 受控工具桥接。
- **当前状态：** 只有计划和测试规范，不包含已经验证的运行代码。

## 重要限制

直接调用官方 CLI **不等于** Google 许可第三方机器人使用消费级 Antigravity 订阅，不能保证账号安全。请先核对最新的服务条款；如果需要长期稳定、合规运行，优先选择 Google 官方 Gemini API。项目不实现客户端身份伪装、令牌复用、二进制补丁、账号池或封禁规避功能。

## 文档

- [完整开发计划](docs/PLAN.md)
- [测试与验收矩阵](docs/TEST_MATRIX.md)

## 参考实现

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [astrbot_plugin_agy（命令模式参考）](https://github.com/KardeniaPoyu/astrbot_plugin_agy)
- [DSH ACP Antigravity（架构参考，不直接移植）](https://github.com/lili-luo/dsh-plugin-bundle/tree/main/dsh-llm-antigravity-acp)
- [AstrBot 自定义 Provider 示例](https://github.com/zxx624/astrbot_plugin_hermes_ecosystem)

## 当前状态

仓库已初始化，现阶段为**开发计划与测试规范**，尚无可安装版本。后续按照 [计划书](docs/PLAN.md) 分阶段实现并测试。
