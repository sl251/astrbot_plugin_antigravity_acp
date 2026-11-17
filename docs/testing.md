# 测试、验收与交付规范

适用基线 **v0.7.2**，Python **3.11+**、AstrBot **`>=4.29.4,<4.30`**。先读 [AGENTS.md](../AGENTS.md) 的安全边界，按 [架构索引](architecture.md#故障定位索引) 找既有测试。数量由本次运行得出，不把旧版本测试总数当作当前结果。

## 开发流程

1. **查证**：读取 issue 正文/评论，记录基线 SHA、宿主/CLI 版本、当前工作区与既有改动。以当前源码和测试核验历史复现，必要时独立 worktree；不要覆盖用户的修改。
2. **最小复现**：固定一个症状、模式、模型/档位与环境，使用合成 system/schema/参数。记录实际阶段、错误码、输出/执行计数和预期行为；缺证据标未知，先区分失败层级再设计修复。
3. **实现**：复用既有夹具；行为修复增加能区分修复前后且验证契约的必要回归。不能删掉正确断言、放宽 permission/schema/认证或关闭防重放来让测试通过。文档改动无需新增镜像实现的测试。
4. **定向检查**：先跑涉及模块的回归；确认命令确实加载到测试，不把导入错误或 0 项测试当作通过。
5. **全量检查**：运行行为变化后执行下列 Python/Node/编译/差异检查。文档专项执行渲染、链接/锚点、围栏、版本/契约核对和 `git diff --check`；若另跑离线回归，单列实际结果，不能扩写为实机验证。
6. **实机与审阅**：按授权范围分别验 CLI、宿主、生产与平台边界；没有环境就写未执行，不临时读取凭据或偷发模型请求。像 #16 这样要求独立 Agent 审读的任务，须另列审读结果并交维护者验收，不能用作者自审代替或直接合并。
7. **交付**：按本页模板列改动、证据、通过/失败/跳过、环境、限制和回滚；未完成项保留明确状态。

## 离线环境

在仓库根目录运行。插件无新增第三方 pip 依赖；测试使用 [AstrBot 替身](../tests/astrbot_stub.py)、[fake_cli.py](../tests/fake_cli.py)、[fake_tool_cli.py](../tests/fake_tool_cli.py)。真实 CLI 或官方登录态不是离线测试前提。依赖宿主 logger 的运行模块不可因此宣称支持脱离 AstrBot 独立导入；独立探测入口另有边界。

[CI](../.github/workflows/test.yml) 为 Ubuntu/Windows × Python 3.11/3.12，Node 22。平台、符号链接权限、可选宿主依赖等跳过项须保留实际原因；用 Python 3.12/Node 24 本地通过，不代表替代了所有 CI 单元。

## 定向回归

以下均为现存可执行模块名，不需复制旧 issue 内依赖过期实现的片段。每行可独立执行：

```sh
# #12：ACP 一次性许可与 #11：拒绝后无正文归因
python -m unittest tests.test_mcp_permissions tests.test_permission_attribution -v

# #13：原始 schema 与模型 schema 分离
python -m unittest tests.test_tool_schemas -v

# #14 / #19：本地能力声明过滤、Safe Mode 定点改写、429 候选与告警分类/冷却
python -m unittest tests.test_system_prompt tests.test_safe_mode_rule tests.test_content_429 tests.test_notices tests.test_failure_guard -v

# 工具交接及部分输出后禁止重放
python -m unittest tests.test_tool_pipeline tests.test_host_replay -v

# 用量、上下文与监控隐私/鉴权
python -m unittest tests.test_telemetry tests.test_monitoring -v

# 启动、认证、登录、模型保存
python -m unittest tests.test_auth_config tests.test_login tests.test_diagnostics tests.test_onboarding -v

# 原生设置页交互（DOM/bridge 替身）
node --test tests/setup.test.mjs
```

对于单项故障，可在已定位测试类后继续指定单个方法；不要假定测试文件名与旧分支相同。源路径映射见 [故障定位索引](architecture.md#故障定位索引)。

## 全量离线回归

```sh
python -m unittest discover -s tests -v
node --test tests/setup.test.mjs
python -m compileall -q main.py agy_provider tests
git diff --check
```

分别保留每条命令的退出码及摘要。`unittest` 的 `Ran N tests` 包含跳过；`OK (skipped=S)` 不等于 N 项均通过。有 failures/errors/expected failures/unexpected successes 时分别记录，不能笼统写“全过”。Node 的 tests/pass/fail/cancelled/skipped/todo 也分别统计；编译通过只证明语法可编译。

不通过时先核对实际代码、环境和夹具，不把 Windows 专属跳过计为 Linux 已验，不以重跑直到偶然全绿掩盖不稳定。通过后，仅在新增改动或未决失败需要时重复/扩大测试。

## 文档专项检查

- `git diff --name-only` / `git status --short` 核对包括新增文件在内的实际范围，确认没有 ACP、认证、Provider、WebUI 或配置行为修改。
- 渲染 README 的 HTML/details、表格和 Mermaid；检查新文档的标题层级、配对代码围栏及列表，不把代码块里的伪链接当作正文链接。
- 逐一验证修改文档中的相对文件/目录链接及本地标题锚点；有空格的真实路径须正确编码或用合适的 Markdown 链接。远程链接的可达性如未验证，单列限制。
- 对照 `metadata.yaml`、main 注册、ACP clientInfo、MCP serverInfo 和 CHANGELOG 的当前版本；文档专项保持与当前发布基线一致；版本升级由维护者统一发布。
- 当前基线 metadata 与 main 注册、ACP clientInfo、MCP serverInfo 已统一为 0.7.2（0.7.1 修复了此前的 0.6.1 标签漂移，本版随发布同步）。
- 扫描“本版/下一版/待验收/未发布”等易过时词语。历史文件保留原始证据，但在入口标注时间和后续状态；不要把历史计划中的未实现项改成当前已交付。
- 对 #12、#13、#14、#19 走一遍文档导航，确认无需先读旧施工书就能找到源码、测试和保守结论。特别核验：一次性许可不绕过宿主、原始 schema 不被收窄、Safe Mode 仅定点改写、429 候选不是根因、已有输出不重放。

## 实机验收

真实生成和工具可能使用额度或产生副作用，在用户请求授权的范围内执行；无生成检查不得偷偷添加提示。每层独立写状态：

| 层级 | 最小验收证据 | 不能推出 |
| --- | --- | --- |
| 离线 Python/Node | 当前提交、命令、计数、跳过原因 | 已登录、真实生成、生产工具可用 |
| 当前提交 CI | run 链接/SHA、各平台矩阵状态 | 生产、macOS 或真实认证后端均已验 |
| 官方 CLI 无生成 | 同用户/环境、版本、阶段、握手/会话/目录结果 | 账号有生成额度、MCP 工具可执行 |
| 真实 Provider 生成 | 固定无敏感短问题、模型/档位、流式/非流式结果与调用次数 | 所有工具、图片、长期刷新可用 |
| 工具链 | 无副作用 echo，permission、MCP、宿主执行/回填与原生 prompt 计数 | 所有生产工具或 QQ 完整链路均可用 |
| 生产宿主 | 部署 SHA、宿主版本、实际路由/会话和观察结果 | 其他 runner/平台、429 根因已查明 |
| WebUI | 原生 iframe、管理员身份、后台终态、主题/交互证据 | DOM 替身或静态预览可代替宿主 HTTP 验收 |

工具验收优先使用合成 `probe_echo`，一次执行、一次原生 `session/prompt`，回填后继续原 prompt。记录实际计数；未观测到不写 0。还要覆盖拒绝、取消、超时、重复/过期/跨会话和已输出后失败不重放，避免有副作用工具作为探针。

UI 修改需在原生宿主中检查窄屏（含 360px）、明暗主题、键盘/读屏、长 URL/模型名、复制失败、重复点击、迟到轮询、nonce 过期、登录归属和保存部分成功。验证 `monitor/status` 的匿名/非管理员/API Key 拒绝、管理员成功、no-store、隐私白名单与不启动模型请求。文档专项不必为这些路径虚构新实机验收。

认证需分 Windows 权限/链接、macOS 钥匙串、Linux 文件/keyring，另验刷新/并发刷新/长期运行。minimal 组件实验、真实压缩事件与 Windows 桥实机的状态分别记录，不能用其他场景通过替代。

## 交付报告

复制后填写真实结果，不预填成功或数量：

```text
范围：issue、基线 SHA、交付 SHA/分支；修改了哪些行为/文档。
证据：最小症状/输入；源码/测试依据；事实、推断、未知分别列出。
环境：日期、OS、Python、Node；真实测试另列 AstrBot/CLI、部署方式。
定向检查：命令、退出码；通过/失败/错误/跳过数及原因。
全量 Python：Ran、通过、失败、错误、跳过、预期失败/意外成功。
Node：tests/pass/fail/cancelled/skipped/todo；编译/差异检查结果。
文档：渲染、相对链接/锚点、围栏、版本和路径核对结果；远程检查范围。
CI：当前提交 run 链接与矩阵状态，未执行写未执行。
实机：无生成 / 真实生成 / 工具 / UI / 生产各自通过、失败或未执行。
审阅：独立审读者、结论/遗留项；维护者验收状态。
风险：剩余未知、平台边界、未做的场景，不引用旧记录冒充本次通过。
回滚：文档提交可 revert；行为发布恢复已验版本并重载，保留配置与登录态。
```

发布行为变更时同步 metadata、main 注册版本、transport clientInfo、mcp_bridge serverInfo、README 与 CHANGELOG；确认维护者验收后再合并/发布。旧版报告仅为其指定 SHA/环境的证据，索引见 [CHANGELOG](../CHANGELOG.md)。
