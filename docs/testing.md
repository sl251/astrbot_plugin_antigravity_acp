# 测试、验收与交付规范

从[故障定位索引](troubleshooting.md#故障定位索引) 找测试。以下命令在仓库根目录执行。Python 3.11+，[CI](../.github/workflows/test.yml) 使用 Ubuntu / Windows × Python 3.11 / 3.12、Node 22；[AstrBot 替身](../tests/astrbot_stub.py) 和 [CLI 夹具](../tests/fake_cli.py) 无需登录。

## 开发流程

1. 写清任务范围、基线 SHA、复现与验收条件。先看 Issue 正文和评论，保护原有改动，避免无关重构。历史描述需用源码核对。并行 Agent 各用独立分支或 worktree，约定模块与文件归属、接口依赖及集成顺序。
2. 用合成输入固定失败阶段，保留改动前证据；行为修复加必要回归，保持认证、权限、隔离和防重放断言。先跑定向测试，确认加载了预期用例。
3. 行为变更再做全量离线检查。文档专项执行下方文档检查，按需要选择少量验证，通常无需全量回归或实机重测。
4. 交付写明改动、依据、基线/交付 SHA、命令和结果、未知项、回滚方式。离线、CI、真实 CLI、宿主实机和生产分别记录。维护者补做实机后，在关联 PR 或当前证据索引更新日期、部署 SHA、环境、步骤及实际结果，缺项标未知。
5. 高风险或指定任务须另附独立 Agent 审读，经维护者验收后方可合并，作者自审不能代替。

内部 Agent 调度 Issue 用于分工和收口，维护者可主动终止低优先级任务。外部反馈单独保留复现、影响与处理结论。关闭原因和验收证据分别记录，历史清单无需全部实现。见[计划状态](plan-status.md)。

## 定向回归

按任务选一行。前端改动加 `node --check views/setup/app.js` 和 `node --test tests/setup.test.mjs`。

| 任务 | 仓库根目录命令 |
| --- | --- |
| #36 模型、路由、配置漂移和迁移 | `python -m unittest tests.test_onboarding tests.test_routing tests.test_main_init -v` |
| #38 / #41 原生权限、共享预算、无 MCP、聊天指令 | `python -m unittest tests.test_issue38_permissions tests.test_issue41_readonly_builtins tests.test_mcp_permissions tests.test_permission_attribution tests.test_commands -v` |
| #13 原始与模型 schema | `python -m unittest tests.test_tool_schemas -v` |
| #38 / #43 额度刷新、每日用量和明细恢复 | `python -m unittest tests.test_issue38_quota tests.test_issue38_daily tests.test_onboarding tests.test_monitoring tests.test_telemetry tests.test_turn_history -v` |
| #45 超时回填、收尾、防重放 | `python -m unittest tests.test_tool_pipeline tests.test_host_replay tests.test_b06_side_effects -v` |
| #33 会话和持久历史 | `python -m unittest tests.test_tools_mode tests.test_tool_pipeline tests.test_sessions -v` |
| #14 / #19 告警与定点 system 兼容 | `python -m unittest tests.test_system_prompt tests.test_safe_mode_rule tests.test_content_429 tests.test_notices tests.test_failure_guard -v` |
| #24 / #32 当前图片与历史占位 | `python -m unittest tests.test_prompt_images tests.test_media tests.test_b07_b08_regressions -v` |
| CLI、登录与无生成检测 | `python -m unittest tests.test_process_hardening tests.test_auth_config tests.test_login tests.test_diagnostics -v` |

### 宿主源码离线检查

`AGY_HOST_RUNNER_SOURCE` 设为 AstrBot 4.29.4 原版 `tool_loop_agent_runner.py` 的本地绝对路径，再运行 #45 定向命令。`test_tool_pipeline` 用真实 runner 方法或内置替身，`test_host_replay` 的可选真实源码用例未提供时跳过。`test_b06_side_effects` 单独从夹具指定的 `evidence/astrbot-v4.29.4` 读取源码，缺失时跳过。CLI 与模型仍由夹具替代。对照 `test_late_tool_result_*`、`test_max_steps_*`，检查一次无工具 prompt、无额外工具调用与零 fallback；覆盖跨运行、篡改、重复、已死会话和生成失败后重交。

## 全量离线回归

```sh
python -m unittest discover -s tests -v
node --check views/setup/app.js
node --test tests/setup.test.mjs
python -m compileall -q main.py agy_provider tests
python tools/check_sensitive.py
git diff --check
```

报告退出码、成功/失败和跳过数及原因。`unittest` 的 `Ran` 含跳过，Node 的 `tests` 含非 pass 项。导入报错或 0 项测试需要单独定位。

不以重跑直到偶然全绿掩盖不稳定，不把 Windows 上的跳过计为 Linux 已验。缺失 usage、退出码和未观测计数均填未知，不得填成 0 或成功。依赖宿主 logger 的运行模块即使通过替身测试，也不可宣称脱离 AstrBot 独立导入。

## 文档专项检查

- 用 `git status --short`、`git diff --name-only` 核对范围，确认没有 ACP、认证、Provider、WebUI 或配置行为修改，用 `git diff --check` 检查空白。
- 核对相对文件/目录、GitHub 标题锚点、代码围栏、README 分区和表格渲染。测试命令对应实际模块、脚本及可选依赖；远程未检查项单列。
- 对照源码入口核验描述、注册端点和安全边界。[metadata.yaml](../metadata.yaml) 是版本入口，[_version.py](../agy_provider/_version.py) 的运行标识由 [test_release_hygiene.py](../tests/test_release_hygiene.py) 核对。历史证据注明范围，更新验收状态时保留已有记录。

## 实机验收

| 层级 | 记录内容 |
| --- | --- |
| 离线与 CI | 分列本地环境/命令、CI run 与提交/矩阵，各自记录跳过 |
| 官方 CLI 无生成 | 同一服务用户、CLI 版本、阶段、握手/会话/目录。额度另记冷缓存、并发合并、过期/手动、登录切换及真实查询数 |
| 宿主实机 | AstrBot 版本、部署 SHA 和步骤。真实生成用短文本，工具用无副作用 echo，记录 prompt、工具执行及 fallback 次数，确认正常回填后继续原 prompt。覆盖拒绝、取消、超时、重复/过期/跨会话和已输出后失败不重放 |
| WebUI | 原生 iframe 核对 360px 窄屏、主题、键盘/读屏、长 URL/模型名、复制及失败降级、重复点击、迟到轮询及操作终态、nonce 过期、登录归属和保存部分成功。验证 `monitor/status` 拒绝匿名/非管理员/API Key 请求、管理员成功、`no-store`、隐私白名单及不启动模型请求 |
| 认证与跨平台 | 分平台核对 Windows 权限/链接、macOS 钥匙串和 Linux 文件/keyring，并验刷新、并发刷新与长期运行。`minimal`、真实压缩事件及 Windows 桥的实机结果分别记录，不可互相替代 |
| 生产 | 同样记录版本、部署、观察路径及结果，仅覆盖实际执行的场景。完整重启要核对持久统计连续、各轮只计一次、会话池如实重建 |

真实生成、工具和部署仅在授权范围内执行。无生成检查不得发送模型 prompt。使用 [计划状态](plan-status.md) 查既有证据范围。

## 交付报告

按改动范围写基线/交付 SHA、源码和测试依据、命令/退出码及各环境结果，列出仍未知的事项和回滚路径。文档提交可直接 revert。行为回滚保留宿主配置、登录态及统计文件。

发布前确认维护者验收。发布行为变更时同步 README 与 CHANGELOG，版本由维护者统一发布，不预设下个版本号。
