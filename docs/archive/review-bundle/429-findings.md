# 429 实机排查结论（2026-11-15 21:19-21:47 CST，小鱼生产）
## 环境
- 测试使用同一已登录的 AGY 账号；账号标识、认证方式及关联账号元数据已从公开记录中移除。
## 时间线（全部真实调用）
- 21:19 provider 测试（小 prompt，gemini-3.8-flash）→ 成功 available
- 21:24 webchat R1（约 28K 输入 token）→ CLI attempt1+2 均 429 → PR9 notice guard 归类 RATE_LIMIT → AstrBot 切 fallback glm-5.2 兜底成功（用户 17s 收到回复）
- 21:29 webchat R2（约 15K 输入 token）→ 同样 429×2 → RATE_LIMIT → fallback 兜底成功
- 21:47 provider 测试（小 prompt）→ 再次成功（9.9s）
- 基线：11-13/14 真实 QQ 流量 25-40K 输入 token 的 agy_probe 轮全部成功（TTFT 7-40s）
## 真实 429 报文（CLI 1.5.8 ACP 流内 notice，唯一残留证据；今晚会话的 CLI glog 已被 PR9 退出清理删除）
⚠ API error (attempt 2): RESOURCE_EXHAUSTED (code 429): Resource has been exhausted (e.g. check quota).
## 分类行为核对（实机验证）
- notices.py classify_notice：QUOTA 正则不匹配该报文（quota 出现在 exhausted 之后、无 "(quota)" 字面量）；\b429\b 命中 → RATE_LIMIT_OR_CAPACITY；两个不同 attempt 的 notice 累计 → fast_fail "RATE_LIMIT"（PR9 设计行为"持续 429 快速中断"正常工作）
- errors.py remote_error 的 QUOTA 正则 resource.{0,32}exhausted.{0,16}\(quota\) 只匹配 PR9 验收文档里的 RPC 格式 "Resource has been exhausted (quota)"，不匹配 CLI 1.5.8 实际 notice 格式
- failure_guard.py:196 code in {"QUOTA","RATE_LIMIT"} 共用同一 quota_ttl 冷却 → 功能等价；差异仅在错误文案、telemetry fast_abort_reason 与诊断准确性
## 待解问题（请回答）
1. 是否值得交新 PR（还是修改仍为 Draft 未合并的 PR9）：分类正则要不要匹配 CLI 1.5.8 真实 notice 格式？通用 429 归 RATE_LIMIT 是否本就合理？
2. 若修，最小改动是什么（正则/测试串/验收文档）？注意 QUOTA 与 RATE_LIMIT 是否有除冷却外的语义分叉（no_retry、session/cancel、断言文案）。
3. 顺带评估：a) 429 notice 原文是否应进 telemetry 便于事后归因（现在只在异常 traceback 里出现）；b) CLI 自动重试 attempt2 的窗口（两轮实测各 ~12s 才 fast-fail）是否值得评估更快失败。
4. 明确结论：『交新 PR / 修改 PR9 / 不修』三选一 + 依据；若建议修，给出具体正则/代码片段与离线测试用例清单（≤30 行）。
## 交付要求
最终回复必须非空中文；只读分析，不修改任何文件。相关源码在 review-bundle/（pr9-notices.py、pr9-errors.py、diff.patch 为 PR9 完整 diff）。
