# AGY Gemini provider 实施计划

更新：2026-11-06。状态：首版代码已实现，待独立功能测试和真实环境验收。

## 确定范围

默认原生安装，AstrBot 和官方 AGY CLI 同机、同系统用户运行，无需 Docker。以 AstrBot v4.29.4 为接口基线；CLI 使用启动时版本、帮助和 ACP 结果判断能力，不指定未经验证的最低版本。

首版交付原生 provider、Gemini 模型发现、普通/流式文本、reasoning、思考档位、会话隔离、取消、超时与资源回收。多模态、工具桥、跨机运行和持久恢复另行实施。

## 相对初始计划的调整

1. 会话组合来源与 conversation_id，匿名调用不复用。
2. 普通与流式共用生成逻辑，末尾返回完整 LLMResponse。
3. 适配顶层 model、Message/字典历史以及 runner 已组装当前输入的方式。
4. provider 模板与插件配置分开；仅启用时注册，停用/热重载回收实例。
5. 一个保留会话对应一个进程，不依赖 --process-per-session。
6. 独立自定义 agent body 承载系统提示，无需私有 ACP 扩展。
7. 工具桥单独迭代，首版不给 AGY 发送宿主工具 schema。
8. 测试交给后续测试方；未执行的项目不标记通过。

## 模块

| 文件 | 职责 |
|---|---|
| main.py | 启停、provider 注册与重载恢复 |
| _conf_schema.json | CLI 路径、默认思考档位和资源限制 |
| agy_provider/provider.py | AstrBot 接口、流式转换、取消和用量 |
| agy_provider/process.py | 本机启动、能力探测、agent 文件、进程树回收 |
| agy_provider/transport.py | ACP v1 JSON-RPC、请求 ID、通知、权限拒绝 |
| agy_provider/sessions.py | 有界会话池、锁、历史确认与空闲回收 |
| agy_provider/messages.py | 消息归一化、指纹、预算回放 |
| agy_provider/models.py | Gemini 目录、模型与思考档位选择 |
| agy_provider/errors.py | 脱敏错误分类 |

## 历史与生命周期

prompt 参数存在时按宿主合约追加；contexts-only 模式不再次添加 extra_user_content_parts。系统提示写入独立 agent，变化后重建。只有当前历史前缀与上轮完整输入输出的指纹完全相符时才复用。失败/取消不提交历史；编辑、回滚、压缩、模型/思考档位变化重建。重放保留最新问题，移除较旧完整轮次并标记数量。

首次模型发现/聊天懒启动；缺少 CLI 不阻塞宿主启动。同会话串行、跨会话限流，排队有界。取消通知后回收该进程树。乱序响应按 ID 匹配；未知/过期 ID 丢弃；帧、队列和输出有限额。正常结束会话删除目录，强制断电留下的数据由用户停机后清理。

## 待验收

所有功能验收待测试，特别是：真实 AGY agent 加载、隔离目录下官方登录、多平台进程回收、WebUI 模型选择、20 轮历史、取消及重载竞态。无真实联调记录前不标记稳定发布。

后续图片阶段先验证 ACP 能力；工具阶段单独实现 MCP 挂起/回填及 AstrBot 授权流程。
