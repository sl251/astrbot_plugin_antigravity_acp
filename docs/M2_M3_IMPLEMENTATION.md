# M2/M3 实现与验收交接

本分支基线 main@28203ef。**2026-11-07 losvps 实测注记**：外部探针的 M0 决策门及图片协议门已通过；本分支只运行离线测试，真实 Provider + AstrBot runner + AGY 验收仍由主代理执行。此页补充旧文档，不改写旧阶段的历史结论。

## 协议与宿主入口

`session/new.mcpServers` 注入 `astrbot` stdio server；AGY 启动 `mcp_bridge.py`，后者仅用 Unix socket 转发 MCP。宿主插件的 `ToolBroker` 校验、登记 pending-call，然后 Provider 返回 AstrBot v4.29.4 原生 `LLMResponse.tools_call_name / tools_call_args / tools_call_ids`。原 `session/prompt` 的后台任务持续拥有 ACP 会话与并发名额；下一次 contexts-only 调用校验完整 assistant/tool 交换，向原 MCP 请求回填。所有 handler 由原宿主 runner/executor 调用，桥进程和 Provider 都不调用 handler。

宿主字段核查固定于 AstrBot v4.29.4 / `61335414433db721b3f7ecc93f90deafe0a59d78` 的 `astrbot/core/provider/entities.py`、`astrbot/core/agent/tool.py`、`astrbot/core/agent/runners/tool_loop_agent_runner.py`。`tool_calls_result` 接受真实 `ToolCallsResult.to_openai_messages()`；不要同时在 contexts 重复提交它。

工具调用必须有非匿名 session/conversation 身份及同一个 `asyncio.Event` abort signal。Provider 实例各自拥有 Runtime，续轮还绑定人格、模型、思考档位、schema 快照及可选审批回调身份。没有上下文、未闭合/篡改历史、旧进程结果、变动许可均拒绝。调用者传入的完整 active `ToolSet` 表示本轮宿主已经授权的工具集合；插件不从消息文字授予权限，不用全局 monkeypatch 获取 runner 上下文。

`FunctionTool.active=False` 不暴露。MVP 要求 callable handler 及完整 object schema；无 handler 的 skills_like 占位与远程工具代理逐个跳过并记 WARN 日志（含跳过原因），不阻塞其余工具，全部不可用时等价于无工具。普通合法 MCP 名字保留；其他名称通过会话双向表映射为无碰撞编码名。JSON Schema 支持 object/array/基本类型、required、additionalProperties、items、enum/const、数值/长度约束、uniqueItems、anyOf/oneOf/allOf；`$ref`、pattern 等未实现约束导致该工具被跳过，不能静默忽略校验规则。参数始终为完整 JSON，ACP 通知中的部分参数不触发执行。

MCP `_meta` 中 `antigravity.google/conversation_id` 与 `root_conversation_id` 必须同时等于当前 ACP session ID。子 agent 的不同 conversation 暂不支持。MCP ID、随机宿主 ID、ACP `toolCallId` 分开保存；ACP 前缀 `mcp_astrbot_` 仅用于通知展示/预算校验，不通过 title/参数猜测 ACP 与 MCP ID 的关联。

## 审批、状态与期限

状态映射为生成 → 校验/审批 → 宿主交接 → 等待结果 → 继续生成 → 完成；取消、EOF、期限耗尽或停用进入终态。额外审批可由可信宿主调用者传入 `agy_tool_approver`：异步 `(original_name, arguments_copy, host_call_id) -> bool`，必须明确返回 `True`；False、异常、超时均回 MCP `isError=true`，没有原生工具响应，宿主执行次数为零。回调必须由宿主代码提供，不能来自消息或配置中的任意代码。

原版 runner 不自动传这个可选回调，其完整 ToolSet 必须已获宿主授权。需要进一步审批的工具应由宿主在提供 ToolSet 前完成审批，传入该回调，或使用能真正拒绝执行的宿主 executor。**v4.29.4 的 `on_tool_start` 抛错后仍可能继续执行，不能把普通 hook 异常当成拒绝机制。**

- `tool_approval_timeout` 默认 60 秒：MCP 到达至合法宿主交接的上限，含可选审批与等待消费者。
- `tool_execution_timeout` 默认 120 秒：交接至宿主结果回填的独立上限。原 prompt 同时受 `request_timeout` 限制，先到者生效。
- `tool_max_calls=16`、`tool_max_concurrent=1`、`tool_result_bytes=262144`；调用尝试计数包含无效调用，结果字节为整轮合计，不截断。可提高待处理并发至 8；宿主交接仍按原 runner 的串行 API 逐个返回，后台 MCP 请求可同时 pending。
- 会话级 ID 集合有 4096 上限，接近上限时下一轮重建。队列、帧、连接及结果均有界。
- `tool_choice=none` 不注入 MCP，并重建已有工具会话；`auto` 支持。`required` 和指定函数明确拒绝。
- 仅接受文本或宿主序列化的 JSON 文本结果，二进制/图片工具结果明确拒绝。

审计区分 `not_executed`（未交给 runner）、`completed`（已收到匹配结果）、`unknown`（已交接但未确认结果）。`broker.audit` 保存当前会话状态；`runtime.tool_outcomes` 保留最近 256 个无内容的随机调用 ID/状态，在进程回收后仍可检查。不把交接推定为已开始执行；原 runner 没有执行开始回调。超时/崩溃后的未知副作用不会自动重放；进程内防重不等于跨崩溃 exactly-once。

完整原生工具响应是合法交接点：Provider 流结束后后台继续等待，以兼容原版 runner 收到 final 后立即结束迭代。停在普通 chunk 时关闭流会回收；合法工具交接后 `abort_signal`、EOF、MCP EOF、超时、插件停用都会解除等待。**若宿主遗弃已交付工具响应且既不设置 abort signal，也不回填结果，无法仅由 Provider 区分“执行中”和“已遗弃”，按硬期限回收。** 不宣称原版 runner 提供了旧设计拟议的 finally 租约接口。需要即时结束的直接 API 调用者应在 finally 设置同一 abort signal。

## 历史与图片

存活会话校验完整 assistant/tool 单元，20 轮工具历史复用已离线覆盖。工具或附件历史编辑、压缩、回滚、重载后按完整用户轮重建；未知/孤立/重复工具结果拒绝。重建使用带角色和工具关联的 **JSON 转录数据**，不执行历史工具，也不是 Gemini 原生工具历史恢复。转录中的分隔符转义，工具文本不提升为 system 指令。真实工具结果仍是模型可能接触的不可信文本，角色封装不承诺消除所有提示注入。

图片入口 `image_urls`、`extra_user_content_parts`、contexts-only ImageURLPart 统一转为 ACP `{type:"image", mimeType, data}`。同一输入按内容去重，纯图可只发 image block。最新 user 已有的附件可在工具续轮重复传；续轮新增图片拒绝。历史指纹包含实际图片字节，编辑原文件触发重建；无跨请求路径缓存，不删除用户原件。

- 默认 `enable_images=false`；音频、视频、PDF、图片生成保持关闭。
- 本地根目录须在 `media_roots` 明确授权；绝对路径、逐级 `openat + O_NOFOLLOW`，拒绝任一符号链接、穿越、设备/FIFO。没有该接口的平台明确拒绝本地路径。
- 公网 HTTP(S) 仅标准 80/443 端口；每个跳转重新检查 URL 和全部 DNS 结果，拒绝私网/回环/链路本地/元数据及 IPv6 地址映射/转换；连接固定到校验过的 IP，TLS 校验原主机名，不使用环境代理或浏览器凭据。
- 最多 8 张唯一图片、总原始字节 8 MiB、每张 2000 万像素、单次下载 15 秒；源地址数、响应头、重定向次数和压缩响应也有限制。数量/字节预算覆盖整个传入历史，超限明确报错。
- PNG 做签名、CRC、尺寸和有界 zlib 光栅校验，仅非交错 8 位灰度/RGB/RGBA；动画和压缩文本/ICC 元数据拒绝。JPEG 限 8 位灰度/RGB baseline/progressive，做帧尺寸与 marker envelope 校验，**不包含自建 JPEG 熵解码器**。其他格式、MIME 不符、损坏/失效附件明确报错。
- CLI 的 `promptCapabilities.image` 必须为 true，且模型仍须属于允许的 Gemini 目录。该 CLI 没有逐模型视觉 capability 字段，因此不能凭目录声明每个模型实测支持；不支持时报上游错误，不静默丢图。
- 宿主模型能力需勾选 `image`/`tool_use`，否则 runner 会在 Provider 前过滤。新建 Provider 模板随插件开关更新；已有配置需手动勾选。宿主若已删除附件或清理临时原图，插件无法恢复，须保留有效媒体来源。

## 实际验收

```sh
python3 -m unittest discover -s tests -v
ruff check .
ruff format --check .
python3 -m compileall -q agy_provider main.py
python3 scripts/real_loop_check.py
python3 scripts/real_loop_check.py --with-image
```

最后两条仅主代理在有官方 CLI 1.5.2、已登录 token、SOCKS 出口 `127.0.0.1:10808`、可导入 AstrBot 4.29.4 的环境执行；必要时使用宿主 Python 或 `--astrbot-source`。脚本以原版 ToolLoopAgentRunner/FunctionToolExecutor 执行无害 echo，校验模型参数、一次宿主执行、运行时随机标记回读、宿主 ID 回填、MCP completed 审计，以及同一个 outstanding ACP prompt。`--with-image` 再做图文/纯图 7391 读回。输出 `PASS/FAIL M2_REAL_LOOP`、可选图片项及 `PASS/FAIL REAL_LOOP_CHECK`、`EVIDENCE_JSON <path>`；成功退出 0，失败退出 1。

认证 symlink 只存在于这个独立验收脚本；插件不读取、复制或刷新任何凭据。脚本只落白名单证据，不落账号/原始 stderr/模型思考；home 路径替换为 `<HOME>`。不运行聊天平台，不发送真实用户消息。

## 与旧文档/探针摘要的差异

| 原表述 | 当前证据与实现 |
|---|---|
| TOOL_BRIDGE_DESIGN 的 M0 未通过、MCP/图片待探针 | 2026-11-07 losvps 外部探针已通过；本分支宿主完整真实验收仍待主代理运行。 |
| 任务书早期描述 ACP 工具执行请求/泛化 pending broker | 执行请求在 MCP `tools/call`；ACP 只有状态通知及权限请求，后者仍 fail-closed。 |
| 是否有真实 `name`、MCP `_meta` 会话归属尚未知 | fixture 已显示前缀通知名、原始 MCP 名和双 conversation ID；本实现严格验证。 |
| 旧工具设计拟议宿主 lease/finally 扩展 | 原版 v4.29.4 没有该接口；本实现绑定实际 session/conversation、同一 abort Event、完整 schema 和精确历史；遗弃交接依赖硬期限。 |
| 首版拒绝所有工具历史、图片仅 capability 广告 | 现支持闭合工具转录重建、真正图片块及安全限制；不伪称原生历史恢复或全部媒体支持。 |
| report/probe_results 用 `session/set_option` 描述模型 pinning | 可执行 acp_probe.py 第 276/427 行实际使用 `session/set_config_option`，保持现有正确实现。 |
| 探针 MCP initialize 请求 2025-11-25 | echo_probe_server.py 原响应仍写 2024-11-05；AGY 允许该响应。本桥明确响应 2025-11-25。 |
| 受限 agent 对照“兼容” | raw 对照证明 discover/initialize/tools/list 加载；该对照未发生成 prompt，不能声称其工具执行闭环已实测。验收脚本将覆盖受限 agent 的完整执行。 |
