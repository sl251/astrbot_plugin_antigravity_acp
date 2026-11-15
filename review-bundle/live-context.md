# 目标生产环境（小鱼，腾讯云上海，docker）
- AstrBot 4.29.4（自建镜像 astrbot-local:agy-v4.29.4-20261107），容器 Python 3.12.13，Linux 宿主。
- 插件 astrbot_plugin_antigravity_acp 现网 v0.5.1（main 分支历史版，非 git 部署，文件覆盖安装）；插件目录含离线交接文档 HANDOFF_0.4.0.md、TEST_REPORT_0.4.x.md。
- 真实 AGY CLI 1.5.8 在容器内 /AstrBot/data/tools/agy/antigravity，经同目录 agy wrapper + auth-launcher.py 适配（仓库外部署适配，PR diff 不涉及，但 provider 会通过 command 配置调用它）。
- agy_probe 为该插件注册的 provider，是全站主模型（fallback: newapi/glm-5.2），QQ 真实用户流量会走它。
- 现网插件配置文件（utf-8-sig/BOM JSON，无 PR9 新键 agy_customizations_mode / context_compaction_min_peak / context_compaction_drop_percent，依赖 AstrBot 按 _conf_schema.json 补默认值）：command=/AstrBot/data/tools/agy/antigravity, auth_bridge=true, proxy_mode=inherit, thought_level=low, startup_timeout=60, request_timeout=180, queue_timeout=60, max_concurrent=2, max_sessions=4, max_pending=16, idle_ttl=600, history_bytes=160000, max_output_bytes=4194304, enable_tools=true, session_reuse_mode=strict, cost_log_enabled=false, empty_dedupe_ttl=30, quota_cooldown=60, enable_images=true, tool_max_calls=16, tool_max_concurrent=1, tool_approval_timeout=60, tool_execution_timeout=120, tool_result_bytes=262144, image_max_count=8, image_max_bytes=8388608, image_max_pixels=20000000, image_timeout=15, media_roots=[]。
- 近 24h 日志出现过一次真实失败：AgyError: AGY [EMPTY] AGY 没有返回正文（v0.5.1 行为）。
# 计划部署步骤（PR9 = v0.6.0，Draft，用户要求实机验收）
1. git archive pr9 打包 → 覆盖解压到插件目录（无文件删除，全部 M/A），清 __pycache__，先在容器内 staging 目录跑 compileall + unittest（198 项）。
2. docker restart astrbot（短暂中断 QQ 对话），看加载日志与 provider 注册。
3. 真实验收：webchat 端到端一轮、带工具续轮、压缩/会话退休日志、doctor 诊断、agy_customizations_mode=inherit vs minimal 的 ACP 工具可见性对比。
4. 验收后回滚到 v0.5.1 备份 tar（除非发现必须保留的修复），保持生产不跑 Draft。
