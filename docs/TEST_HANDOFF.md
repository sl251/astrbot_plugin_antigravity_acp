# 测试交接

## 状态

- 接口基线：AstrBot v4.29.4，原生部署优先。
- 已做：源码接口核对、Ruff 静态检查/格式化、Python 编译/语法、配置 JSON 解析与差异空白检查。
- 未做：单元/模拟 ACP 测试、安装运行、真实 AGY 聊天及三平台验证。
- 实现工作区没有 AGY CLI 或登录态。不要把编译通过写成真实可用。

建议顺序：纯 Python 消息/配置层 → fake_agy 进程与会话层 → 真实 AstrBot 插件/WebUI → 官方 CLI 最小联调 → 多轮与异常竞态。测试方可新增测试代码及必要修复，请记录真实结果。

## fake_agy 合约

模拟可执行文件需要支持：

- --version 返回版本；acp --help 显示 --gemini-dir、--app-data-dir、--agent，可选 --disable-telemetry。
- 正常启动：acp --gemini-dir <独立目录> --app-data-dir agy --agent astrbot-chat。
- stdin/stdout 按行传输 UTF-8 JSON-RPC 2.0；日志仅写 stderr。
- initialize 返回 protocolVersion: 1 和 agentCapabilities。
- session/new 返回 sessionId，以及 configOptions 和/或 models.availableModels。
- session/set_config_option 接收 sessionId/configId/value，返回更新 configOptions；legacy inventory 场景覆盖 session/set_model。
- session/prompt 接收文本块，发送 session/update 后再返回 stopReason: end_turn。
- session/cancel 是无响应通知。

通知示例：

```json
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s1","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"你好"}}}}
```

agent_thought_chunk 对应 reasoning。最终 usage 可含 inputTokens/outputTokens/cachedReadTokens/cachedWriteTokens。AGY 参考实现中输入桶互不重叠，AstrBot input_other 合并未缓存输入与 cache-write，input_cached 使用 cache-read。真实测试需复核口径；缺失数字不能伪造。

session/request_permission 应收到 outcome: {outcome: cancelled}；文件/终端等服务端请求返回 -32601。工具执行通知必须使当前请求失败并回收进程；不能只看权限回包便声称内置工具隔离已验证。

## 重点回归

1. 顶层 model、来源与模型配置合并；WebUI 模型列表临时实例释放。
2. 禁用插件后冷启动，不因导入模块就注册 provider；重启、停用、重载恢复。
3. 同会话并发、排队取消、模型切换；匿名调用不复用主聊天进程。
4. contexts 已含当前问题/system/extra parts 时不重复追加。
5. 上轮回答被宿主过滤、编辑或未保存时必须重建。
6. 取消发生在 subprocess 创建、initialize、session/new、prompt、消费 chunk、最终响应等阶段时无进程/任务泄漏。
7. 满队列、超长帧、损坏 JSON、重复/未知 ID、stdout EOF、持续 stderr、空正文。
8. 部分正文后超时不自动重试，后续按宿主历史重建。
9. 重载/停用与模型发现、排队、活动流并发；不移除别的插件的同名 provider。
10. Windows 原生 agy.exe 参数与 taskkill /T，POSIX 子孙进程回收。
11. 真实 AGY 自定义 agent body 生效，规则/技能/MCP 隔离，官方 keyring 登录可用。
12. AGY 额度/认证/限流错误正确分类；不记录原始错误中的凭据。
13. 上轮 assistant 同时含 AstrBot ThinkPart 与 TextPart 时，仅将正文纳入历史比较/回放，不能因 reasoning 而拒绝下一轮。

## 环境记录

记录 AstrBot/AGY/Python 版本、OS/架构、原生或容器、测试项、结果、耗时和错误代码。只记录账号套餐类型，不记录邮箱/token/完整敏感 prompt 或原始 stderr。WebUI test() 会调用模型，应使用获准的测试账号。
