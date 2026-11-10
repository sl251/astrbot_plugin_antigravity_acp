# ACP 原生执行兼容核验

> 历史设计与验收记录：以下版本、测试数量及待办保留当时状态。当前 0.2.0 的认证桥、代理、自检与兼容范围以 [README](../README.md) 和 [CHANGELOG](../CHANGELOG.md) 为准；历史实例成功不替代本次发布验收。

本轮范围：小鱼 AstrBot v4.29.4 + AGY CLI 1.5.2；模型保持 `gemini-3.8-flash`，fallback 保持 `newapi/glm-5.2`。

## 已确认并修复

`ToolSnapshot` 原按 `handler` 和 HandoffTool/MCPTool 白名单判执行能力，与 AstrBot 原生 provider 的 schemas-only 契约不符。`MimoTTSSpeakTool`、文件操作、定时/多消息工具通过原生 `FunctionTool.call()/run()` 执行，常无 handler。修复仅序列化宿主传入的 active ToolSet，保留名称/schema校验、许可/预算/调用关联验证，仍由 AstrBot 原生执行器执行。

- 117 项离线测试通过；ruff通过。
- 部署后真实 AstrBot `ToolSet` 的 full/light/param-only 投影均保留工具且无跳过。
- 生产工具执行模式仍为 full；投影能序列化不代表 skills_like 参数重询兼容。
- 生产更新前备份 `/opt/astrbot/backups/agy-provider-native-tools-20261107-162726.tar.gz`；生产已复读 tools/provider SHA256，与本地一致；AstrBot已重启并重新注册mimo工具。

## 独立审计确认的其它边界

以下是原生源码调用链与内存验证，不当作已完成真实模型复现：

1. **skills_like**：第一次选工具后尚未执行，runner使用param-only subset追加user指令重询。ACP pending turn要求相同schema fingerprint与工具结果，不是同一协议阶段。不能忽略fingerprint来掩盖。
2. **子代理身份**：`Context.tool_loop_agent` 默认空session/conversation ID；同ACP provider带工具子代理会被bridge拒绝。应给每个runner独立run身份。父任务持有并发slot时，嵌套同provider仍可能容量不足，不能只补ID后声称完全兼容。
3. **direct-result清理**：工具yield None使宿主DONE，不再请求provider回填；retained ACP prompt仍占slot。应宿主finally关闭对应turn，不发送第二条消息、不重执行工具，未回填结果仍保守记录unknown。
4. **历史扩展**：图片工具结果附加user图片、同call_id多次yield、宿主压缩或步数耗尽，与当前精确两条续轮校验冲突。需明确闭合旧call后重建，不能简单放松历史关联。
5. **超时**：ACP整个prompt固定deadline包含所有工具时间，而宿主工具可有更长许可；需区分模型/工具/总任务期限和传播取消。
6. **schema与状态**：$defs/$ref等关键字兼容窄；CallToolResult.isError/执行failed未结构化回传；工具阶段usage不完整。

## 429 附加调查

失败链是 `ContextAware._get_image_caption` → `zhipu/GLM-4V-Flash` → 智谱直连 `https://open.bigmodel.cn/api/paas/v4/`，不经New API，不是ACP主模型。

2026-11-07 容器现场同一配置：文本请求200；连续三次带图请求200、429、200。429响应 `error.code=1113`，`余额不足或无可用资源包,请充值。`。只可定性为上游间歇报资源包/余额错误，不能由此断言账户彻底没额度或普通RPM限流。未改变配置、未充值。

## 真实链路验收（已完成）

容器证据：`/AstrBot/data/native-tool-check/native-2ea244e9170646df/evidence.json`，总结果 PASS。

- 真实 AGY ACP，模型 `gemini-3.8-flash`，无 fallback；原生 `FunctionToolExecutor`、`AstrAgentContext`、start/end hooks 正常。
- full 阶段：模型请求2次，原生工具执行2次（call echo、MiMo语音），hooks start/end各2次。
- 保留真实 MiMo `None` direct-result；没有 JSON receipt 适配。`mimo-v2.5-tts-voiceclone` HTTP200，真实TTS请求仅1次；捕获有效RIFF/WAVE，单声道24kHz、16-bit PCM，384044字节。未送QQ。
- 语音宿主DONE后 pending turn=0，callers=0；echo结果completed，已执行但未ACP回填的语音unknown，保守状态未误报failed，未重放。
- 额外skills_like run-echo请求仅对ACP转full，实际执行1次/结果读回正常/零残留；不是宣称原两阶段取参协议已实现。
- `scripts/native_runner_fake_check.py` 真实AstrBot runner/executor + 显式fake ACP（零网络）三case PASS：full echo、skills_like缺省身份echo、direct-result缺省身份；均原生执行一次/hooks各一次，末尾turns=callers=0。
- 133项离线测试通过，其中16项host_compat覆盖取消/异常/提前close/第三方patch安装链；ruff与diff检查通过。

## 本轮追加兼容适配

`agy_provider/host_compat.py` + plugin initialize/terminate挂接：仅owner匹配的ACP provider使用full；缺少session/conversation时复制request赋独立run-id，不修改调用者request；在step_until_done finally关闭匹配本runner abort signal的pending turn。可撤销/幂等，保留第三方patch链，不修改其他provider或全局模型。

Native handoff的无身份拦截已被适配；但本轮未真实递归同ACP有工具子代理，父占满并发slot的容量问题仍在，不能宣称完整递归handoff兼容。图片结果后缀/压缩/多yield以及长工具deadline等仍待专项实现。

## 验收脚本路径陷阱

认证wrapper要求session在plugin_data授权根，不能把runtime放到捕获输出目录。Unix socket有路径字节上限，过长TMPDIR会在模型启动前触发AF_UNIX path too long，被泛化为TOOL_PROTOCOL；本脚本随机输出目录用短nonce维持隔离。失败轮均TTS0；只有最终PASS轮调用1次真实TTS。
