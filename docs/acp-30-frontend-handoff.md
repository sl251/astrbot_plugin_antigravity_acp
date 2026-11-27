# ACP #30 前端原生 Dashboard 重构 & #32 路由诊断交接文档

> 历史交接记录（#30 / PR #34）。下方 Vue 构建和 #31 验收指令属于当时方案。当前面板为原生 [views/setup/](../views/setup/)，后续状态见[索引](plan-status.md)。

> **当前状态声明**：前端源码交付，**离线 QA 仍进行中**。已针对 Hydrogen 真实 QA 首轮检出的阻塞项（`qa-logs/02-npm-typecheck.log` 的 5 处 TypeScript/`monitorPending` ref 缺陷、`qa-logs/04-check-sensitive.log` 的文档版本写法误报，以及 MIT/OFL 随包完整许可文本缺失）完成定向修复并交付复测。**本次返工未执行测试、依赖安装、类型检查或构建，按用户要求交 Hydrogen 复验。** 仍未执行实机、浏览器、截图或 AstrBot 4.30.x 验收；插件版本保留 `v0.7.5` 候选（不预设 `v0.8.0` 发布，不放宽 `metadata.yaml` 版本约束）。

---

## 1. P0 设计基准、一手源码依据与兼容矩阵

### 1.1 目标宿主与源码依据

- **基线提交**：`0a393de2ff88a8972cbe4849705548d79fbe43ae`
- **精确目标宿主**：AstrBot `v4.29.4`，Git commit `61335414433db721b3f7ecc93f90deafe0a59d78`
- **后端兼容范围**：`>=4.29.4,<4.30`（保持不变，不放宽）
- **后端契约源**：[acp-32-backend-handoff.md](acp-32-backend-handoff.md)（只读参照）
- **一手对照源码（本地证据目录 `../evidence/astrbot-v4.29.4/`）**：
  - `dashboard/package.json`：锁定核心兼容版本 `vue` (`3.3.4`)、`vuetify` (`3.7.11`)、`vite-plugin-vuetify` (`2.1.3`)、`@vitejs/plugin-vue` (`5.2.4`)、`vite` (`6.4.1`)、`typescript` (`5.1.6`)、`vue-tsc` (`1.8.8`)
  - `dashboard/src/plugins/vuetify.ts`：Vuetify 主题与默认属性（`VCard.rounded='lg'`、`VSnackbar.elevation=6, rounded='lg'`、`VTextField.rounded='lg'`、`VTooltip.location='top'`、`VBtn={}`）
  - `dashboard/src/theme/LightTheme.ts` / `DarkTheme.ts`：官方浅色/深色主色、表面、边框、文字与代码色板
  - `dashboard/src/scss/_variables.scss`：`$border-radius-root: 8px`、`lg = 16px`、`Outfit + Noto Sans + CJK fallback`、字阶（`body-1: 14px`、`body-2: 12px`、`h1-h6`）、卡片内边距（`20px 24px` / `24px`、`card-title: 18px`）
  - `dashboard/src/scss/_override.scss`、`layout/_container.scss`、`components/_VCard.scss`、`_VButtons.scss`、`_VField.scss`、`_VTextField.scss`：描边卡片边界（`rgba(var(--v-theme-borderLight), 0.36)`）、分割线透明度 `1`、按钮 `letter-spacing: 0`、深色遮罩/表面覆盖、菜单阴影、等宽代码字体
  - `dashboard/src/main.ts`：确认宿主自定义主色保存在宿主 `localStorage('themePrimary'/'themeSecondary')`，未通过 iframe Bridge 下发
  - `astrbot/dashboard/plugin_page_bridge.js` & `dashboard/src/views/PluginViewPage.vue` & `docs/zh/dev/star/guides/plugin-pages.md`：Plugin Views Bridge 能力、`sandbox="allow-scripts allow-forms allow-downloads"`、相对路径令牌资源机制、预注入 `data-theme` 与 `?theme=light|dark`

> **许可与原创说明**：主题色值、圆角、排版阶梯与组件样式在 `frontend/src/plugins/vuetify.ts` 和 `frontend/src/styles/astrbot-theme.css` 中独立编写转写，未从 AGPL 宿主仓库复制大段源码或整套 Dashboard 工程；第三方运行/构建依赖许可详见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

### 1.2 提取的设计规范 Tokens（对齐 AstrBot v4.29.4）

| 维度 | AstrBot v4.29.4 浅色基准 | AstrBot v4.29.4 深色基准 | 本前端落地方式 |
| --- | --- | --- | --- |
| 主色 / 辅色 | `#3c96ca` / `#2f86bd` | `#5ba4d4` / `#4a95c4` | Vuetify 主题 `AstrBotLight` / `AstrBotDark` + 未来宿主主色能力探测 |
| 语义色 | `success #00c853` / `warning #ffc107` / `error #f44336` / `info #03c9d7` | `success #52c41a` / `warning #faad14` / `error #ff4d4f` / `info #03c9d7` | 与官方色板一致，状态芯片/告警使用 tonal/outlined 保持可读性 |
| 背景 / 表面 / 容器 | `#ffffff` / `#ffffff` / `#fffffff4` | `#1a1a1a` / `#242424` / `#1a1a1a` | `v-application` 与 `v-card` 表面精确对齐，避免双层厚阴影 |
| 边框 / 分割线 | `borderLight #d0d0d0`（描边 `0.36` 透明度） | `borderLight #3a3a3a`（描边 `0.36` 透明度） | `.v-card--variant-outlined` 与 `.v-divider` 统一规则 |
| 文本 / 代码 | `primaryText #1b1c1d`、`codeBg #ececec` | `primaryText #e8eaed`、`codeBg #282833` | CSS 变量映射，深色模式代码高对比度 |
| 圆角体系 | root `8px`，`sm: 4px`，`lg: 16px`，`pill: 9999px` | 同左 | 卡片 `rounded="lg"` (`16px`)，输入/按钮/子块 `8px` |
| 字体栈 | `'Outfit', 'Noto Sans', 'PingFang SC', 'Hiragino Sans GB', 'Noto Sans CJK SC', 'Microsoft YaHei', 'Noto Sans SC', sans-serif` | 同左 | 通过 `@fontsource/outfit`（400/500/600/700）本地打包拉丁体，CJK 使用系统回退，**零 CDN** |
| 字阶与密度 | `body-1: 14px`、`body-2: 12px`、`card-title: 18px`、`input: 0.8rem~0.875rem` | 同左 | `astrbot-theme.css` 校准排版、间距与按钮 `letter-spacing: 0` |
| 图标体系 | MDI 风格 | MDI 风格 | Vuetify `mdi-svg` + `@mdi/js` 按需引入最小 SVG 路径集，无整包字体 |

### 1.3 Bridge 与宿主版本兼容矩阵

| 宿主版本 | Bridge 上下文能力 | 主题同步与自定义色 | 静态资源与沙箱 | 本次状态 |
| --- | --- | --- | --- | --- |
| **AstrBot v4.29.4**（`6133541443`） | `ready()`、`getContext()`、`onContext()`、`getLocale()`、`t()`、`apiGet()`、`apiPost()`、`upload()`、`download()`、`subscribeSSE()` | 仅下发 `isDark: boolean`、预注入 `<html data-theme>` 与 iframe query `?theme=light\|dark`；**不传递**宿主 `themePrimary`/`themeSecondary` 自定义色 | 相对路径 `./` 继承 `_t/<token>/`；`sandbox="allow-scripts allow-forms allow-downloads"`（无 `allow-same-origin`，不可访问父 DOM/Cookie/宿主 storage） | 已按一手源码精准实现，**待 Hydrogen 构建与 QA 实机验收** |
| **旧版/不完整 Bridge 降级** | 缺 `onContext` 或 `getContext().isDark` 非布尔值 | 回退读取 `<html data-theme>` → `?theme=` → `prefers-color-scheme`，支持手动切换（`localStorage` 不可用时降级为会话内存态） | 缺失 `window.AstrBotPluginView` 时展示明确只读提示，不报错崩溃 | 已在代码实现降级路径，**待离线单测与 QA 验证** |
| **未来宿主扩展（能力探测）** | 可选读取 `context.theme` / `context.colors` / `context.primaryColor` / `context.secondaryColor` | 若提供合法 Hex 颜色或 iframe `:root` CSS 变量（`--astrbot-theme-primary` / `--astrbot-primary`），动态覆盖 Vuetify 主辅色；缺失则无感回退 4.29.4 默认色 | 保持纯 Bridge 通信，不探测父 DOM | 仅做安全能力探测，不做假兼容承诺 |
| **AstrBot v4.30.x** | 无公开 `v4.30.0` tag（GitHub API 404） | 未知 | 未知 | **无固定版本源码/未测试，本次未验证，不放宽宿主 metadata（保留 `>=4.29.4,<4.30`）** |

---

## 2. P0 旧版功能 → 新版组件与 API 完整映射

### 2.1 旧版 UI 盘点与新模块映射

| 旧版 `views/setup/` 元素 / 能力 | 新版模块与组件 | 对应后端 API / 数据字段 | #28 / #32 关键语义保持 |
| --- | --- | --- | --- |
| 顶部连接状态 + 主题切换（自动/浅色/深色） | `App.vue` 顶栏 + `useThemeBridge.ts` | `window.AstrBotPluginView`（`ready`/`getContext`/`onContext`） | 首屏预读 `data-theme`/`?theme=` 降闪烁；`localStorage` 沙箱拒绝时降级内存态 |
| 全局错误横幅（区分 `action` 与 `poll`） | `App.vue` 双通道 `VAlert` + `useDashboardState.ts` | `setup/status` `action_error`/`action_hint` 与轮询异常 | 轮询恢复仅清 `poll` 错误，不抹掉未处理的 `action` 错误；保留原始错误码 |
| 运行监控三层卡片（概览/详细图表/高级诊断） | `MonitorTab.vue` + `OverviewTab.vue` + `MonitorCharts.vue` + `TurnDetailDialog.vue` | `GET monitor/status`（兼容 schema v1 与 v2） | 保持三层信息架构与 SVG 堆叠/环形/耗时图；缺失 usage/上下文显示「未知/上游未报告」，与 `0` 严格区分；展开状态、分页与筛选跨轮询保持 |
| 监控口径帮助 Popover (`monitor-help`) | `MonitorTab.vue` 帮助 `VDialog` | 无（静态口径说明） | 完整保留「只读不发请求 / 近 24h ≤100 轮 / 实时池 ≠ 额度 / 最近采样不累加」口径 |
| 01 运行环境（系统/Python/容器、事实 chips、重新检测、高级诊断、安装命令与文档复制） | `SetupWizardTab.vue`（Step 1）+ `AccountEnvTab.vue` + `CopyField.vue` | `GET setup/status`、`POST setup/check` | 退出码 `0` 正常显示；安装类错误自动展开安装指引且不覆盖用户手动收起；仅提供官方命令复制，不自动 shell 安装；明确「无生成预检 ≠ 实时健康」 |
| 02 Google 账号认证（开始/重新登录、取消、授权链接复制与新标签打开、授权码提交、终端命令） | `LoginAuthPanel.vue`（复用于 `SetupWizardTab.vue` Step 2 与 `AccountEnvTab.vue`） | `POST setup/login`、`setup/login-code`、`setup/login-cancel` | `authCode` 提交即清且仅驻内存；非发起管理员隐藏 URL/取消；`window.open` 被拦给出复制提示；页面销毁清敏感字段 |
| 03 模型配置（模型下拉触发刷新档位、思考强度、工具模式、设为默认、保存启用） | `SetupWizardTab.vue`（Step 3）+ `ModelsTab.vue` 快速配置 | `POST setup/check {model}`、`POST setup/apply` | 独立 form draft 防轮询覆盖；切模型重置档位为 `default` 并查新档位；`result` 变化才重置 `use_default`；`ROUTE_MISMATCH` 保留已创建实例，不给假绿勾 |
| 04 已启用实例列表 + `/agy use <id>` 复制 | `ModelsTab.vue` 实例管理卡片 + `ProviderImpactDialog.vue` | `setup.providers` + `POST setup/provider-impact` / `provider-default` / `provider-toggle` / `provider-delete` | 新增精确实例 ID 影响预览、显式设全局默认、启用；`can_disable=false`/`can_delete=false` 时严格禁用提交并展示阻止原因，不可二次确认绕过 |
| 新增：全局路由与真实服务观测（#32） | `RoutingSummaryCard.vue`（复用于 `OverviewTab.vue`、`ModelsTab.vue`、`MonitorTab.vue`） | `setup/status.routing` 与 `monitor/status.routing` | 配置主项/宿主解析/无效备用链路与 `last_observed_*` (`plugin_only`) 视觉分区；未观测 `null` 显示「未知」；标注全局路由 ≠ 群会话路由 |
| 新增：近期请求记录表格与详情、持久化失败事件（#32） | `MonitorTab.vue`（`VDataTable`）+ `TurnDetailDialog.vue` | `monitor/status` `instances[].recent_turns`（v2 扩展）与 `failure_events` / `failure_retention` | 支持搜索、状态/错误码/实例筛选与分页；详情展示 v2 字节计数（声明 ≠ Token）、交接标记、fallback 边界与 429 候选（声明仅候选遥测）；v1 响应优雅降级 |
| 新增：高级设置与脱敏诊断导出 | `AdvancedTab.vue` + `src/utils/diagnostics.ts` | 只读展示当前有效策略；白名单导出 `monitor` 投影 | 无后端写配置 API 时明确提示到宿主插件配置页修改，不造假开关；导出严格白名单，绝不含 `csrf`/登录票据/授权码/URL |

### 2.2 后端 API 清单（严格对齐 `docs/acp-32-backend-handoff.md`）

| 端点（Bridge 相对路径） | 方法 | 请求体（严格键集合） | 前端处理约定 |
| --- | --- | --- | --- |
| `setup/status` | GET | 无 | 轮询快照（busy/登录中 1.2s，空闲 5s，异常指数退避 5/10/20/30s）；按 `refreshEpoch` + `actionRevision` 丢弃迟到响应 |
| `setup/check` | POST | `{ csrf }` 或 `{ csrf, model }` | 无生成环境预检或按模型刷新思考档位；受单槽 busy 与重复点击锁保护 |
| `setup/login` | POST | `{ csrf }` | 启动官方 `agy acp --login` 内存流程 |
| `setup/login-code` | POST | `{ csrf, ticket, code }` | 提交一次性授权码，前端在发请求前立即清空输入框 |
| `setup/login-cancel` | POST | `{ csrf, ticket }` | 仅当前发起管理员（`owned && busy`）可取消 |
| `setup/apply` | POST | `{ csrf, model, effort, tools_mode, use_default }` | 保存/启用实例，可选设全局默认；若 `action_error` 非空且 `result.provider_state==='created'`，提示保留实例并告警 `ROUTE_MISMATCH` |
| `setup/provider-impact` | POST | `{ csrf, provider_id }` | 同步只读，返回 `{ impact }`；不改变 `setup.result`，用于影响预览与停用/删除准入判定 |
| `setup/provider-default` | POST | `{ csrf, provider_id, confirm: true }` | 入队单任务槽，轮询至 `busy=false` 且 `!action_error && result?.applied===true` 才视为成功 |
| `setup/provider-toggle` | POST | `{ csrf, provider_id, enabled: boolean, confirm: true }` | 仅在 `enabled=true` 或未来 `impact.can_disable===true` 时提交；4.29.4 停用按 `can_disable=false` 在前端拦截展示原因 |
| `setup/provider-delete` | POST | `{ csrf, provider_id, confirm: true }` | 仅在未来 `impact.can_delete===true` 时可提交；4.29.4 按 `can_delete=false` 在前端拦截展示原因 |
| `monitor/status` | GET | 无 | 10s 自动轮询，只读不触发 CLI/模型；兼容 `schema_version: 1` 和 `2`；401/403 立即清空旧实例数据 |

---

## 3. 前端目录结构与构建说明

### 3.1 目录结构

```text
frontend/
├─ index.html                      # Vite 构建入口（含首屏 data-theme / ?theme= 防闪烁脚本）
├─ package.json                    # 精确对齐 AstrBot v4.29.4 的最小依赖集与脚本（无 package-lock.json，由 Hydrogen 生成）
├─ tsconfig.json                   # 应用源码 TypeScript 严格检查配置（vue-tsc）
├─ tsconfig.node.json              # vite.config.ts Node 类型检查配置
├─ vite.config.ts                  # base: './'，默认输出 ../views/setup，--outDir dist 用于 CI 无破坏比对
├─ scripts/
│  └─ verify-dist.mjs              # 校验 views/setup 与 frontend/dist 一致且无外网 CDN 链接
└─ src/
   ├─ env.d.ts                     # Vite 客户端类型与 window.AstrBotPluginView 声明
   ├─ main.ts                      # 引入本地 @fontsource/outfit 400-700、vuetify/styles、astrbot-theme.css 并挂载应用
   ├─ App.vue                      # 统一顶栏、6 Tab VTabs/VWindow、双通道错误告警与全局 Snackbar
   ├─ plugins/
   │  └─ vuetify.ts                # AstrBotLight / AstrBotDark 主题、Vuetify defaults、mdi-svg 图标集
   ├─ styles/
   │  └─ astrbot-theme.css         # 独立编写的 AstrBot v4.29.4 圆角/字阶/边框/暗色/响应式/打印与无障碍样式
   ├─ types/
   │  └─ api.ts                    # setup/monitor/routing/impact/failure_events 类型定义及 v1/v2 响应归一
   ├─ utils/
   │  ├─ icons.ts                  # 按需引入的最小 @mdi/js SVG path 集合
   │  ├─ labels.ts                 # 阶段、登录、工具模式、错误码、路由原因与回退原因中文映射（保留原始错误码）
   │  ├─ format.ts                 # 数字/百分比/字节/时长/时间/相对时间格式化（null→未知，不误填 0）
   │  ├─ copy.ts                   # #28 安全复制：同点击同步 execCommand 兜底 + Clipboard Promise + 手动选区
   │  └─ diagnostics.ts            # 严格白名单脱敏诊断 JSON 构造与 Blob 下载（绝不含 csrf/ticket/url/code）
   ├─ composables/
   │  ├─ useThemeBridge.ts         # Bridge ready/getContext/onContext、主题循环、沙箱 storage 降级、未来宿主主色探测
   │  └─ useDashboardState.ts      # 单一响应式 controller：独立表单 draft、epoch/revision 防覆盖、退避轮询、visibility/BFCache 恢复
   ├─ components/
   │  ├─ CopyField.vue             # 可选区只读输入框 + 安全复制按钮（2s 状态恢复与卸载清定时器）
   │  ├─ RoutingSummaryCard.vue    # #32 全局路由与 plugin_only 服务观测分区卡片
   │  ├─ LoginAuthPanel.vue        # 官方 CLI 登录面板（内存 URL/code、提交即清、non-owner 与过期提示）
   │  ├─ ProviderImpactDialog.vue  # #32 实例影响范围预览与受限操作弹窗（can_disable/can_delete=false 禁绕过）
   │  ├─ MonitorCharts.vue         # 24h Token 堆叠图、复用/缓存双环图、近期耗时柱图与重建原因标签
   │  └─ TurnDetailDialog.vue      # 请求回合与持久化失败事件详情弹窗（v1/v2 字段与 429 候选说明）
   └─ views/
      ├─ OverviewTab.vue           # 1. 概览（已配默认页；无示例数字；CLI/认证/实例/异常/真实路由）
      ├─ SetupWizardTab.vue        # 2. 首次设置（VStepper 三步引导、失败恢复、ROUTE_MISMATCH 保留实例提示）
      ├─ ModelsTab.vue             # 3. 模型管理（实例列表、精确 ID 设默认/启用/影响检查/受限停用删除、快速配置）
      ├─ AccountEnvTab.vue         # 4. 账号与环境（登录管理、容器/command/代理提示、全量无生成预检诊断）
      ├─ MonitorTab.vue            # 5. 运行监控（三层卡片、VDataTable 搜索筛选分页、v2 失败事件、口径说明）
      └─ AdvancedTab.vue           # 6. 高级设置（宿主插件配置入口声明、实时只读策略、白名单脱敏导出、管理命令）
```

### 3.2 构建与校验命令（交 Hydrogen 执行）

Node.js 版本对齐 CI：**Node 22**。当前工作树**保留旧版 `views/setup/{index.html,app.js,style.css}` 未改动**，待 Hydrogen 安装依赖并真实构建时自动替换。

```sh
# 1. 首次安装并生成 frontend/package-lock.json（由 Hydrogen 执行）
cd frontend
npm install --ignore-scripts

# 2. 严格 TypeScript 与 Vue SFC 类型检查
npm run typecheck

# 3. 正式构建输出至 ../views/setup/（清空旧版 app.js / style.css 并生成 index.html + assets/*）
npm run build

# 4. CI 同口径校验（不破坏工作区 views/setup，临时构建至 frontend/dist 后逐文件比对并检查零 CDN）
npm run build:ci
npm run verify:dist
```

CI 流程（[.github/workflows/test.yml](../.github/workflows/test.yml)）在 `ubuntu-latest` 与 `windows-latest`、Node 22 下执行：
`npm ci --ignore-scripts` → `npm run typecheck` → `npm run build:ci` → `npm run verify:dist`，并继续在 `test` job 运行 `tools/check_sensitive.py`、`compileall`、`unittest discover` 与 `node --test tests/setup.test.mjs`（待 QA 根据新构建产物补齐/更新前端交互测试入口；未在 CI 中硬编码尚未编写的新测试文件名）。

### 3.3 打包与忽略规则

- [.gitattributes](../.gitattributes)：将 `/frontend` 与 `/frontend/**` 标记为 `export-ignore`，发行归档（`git archive` / Release zip）不包含前端源码、`node_modules` 或构建工具；**`/views/setup/**` 与 `/LICENSES/**` 均明确保留**，用户部署无需 Node.js 且随包保留第三方完整许可文本。同时统一 `/frontend/**` 与 `/views/setup/**` 文本文件为 `eol=lf`，`.woff2/.woff/.png/.ico` 标记 `binary`。
- [.gitignore](../.gitignore)：忽略 `frontend/node_modules/`、`frontend/dist/`、`frontend/.vite/` 与 `frontend/*.tsbuildinfo`；保留 `frontend/package-lock.json`、`views/setup/` 与 `LICENSES/` 纳入版本控制。

---

## 4. 真实前端依赖与许可清单

详见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。因构建配置 `esbuild.legalComments: 'none'` 去除产物内注释横幅，所有打包进入 `views/setup/` 的第三方运行时上游版权与完整许可原件均随仓库及发行归档保留于 `LICENSES/` 目录：

| 依赖包 | 版本 | 用途 | 许可与随包原件 | 是否打入 `views/setup/` |
| --- | --- | --- | --- | --- |
| `vue` | `3.3.4` | Vue 3 运行时（对齐 AstrBot 4.29.4，含 `@vue/*` 子包） | MIT（[LICENSES/vue.txt](https://github.com/sl251/astrbot_plugin_antigravity_acp/blob/107bef746a686c941ff6ecd575735b20e1b0d86e/LICENSES/vue.txt)） | 是（tree-shaken） |
| `vuetify` | `3.7.11` | Vuetify 3 组件库（对齐 AstrBot 4.29.4） | MIT（[LICENSES/vuetify.txt](https://github.com/sl251/astrbot_plugin_antigravity_acp/blob/107bef746a686c941ff6ecd575735b20e1b0d86e/LICENSES/vuetify.txt)） | 是（`vite-plugin-vuetify` 按需引入） |
| `@mdi/js` | `^7.4.47` | 最小 SVG path 图标集（无整包字体） | Pictogrammers Free License / Apache-2.0 图标 / MIT 代码（[LICENSES/mdi.txt](https://github.com/sl251/astrbot_plugin_antigravity_acp/blob/107bef746a686c941ff6ecd575735b20e1b0d86e/LICENSES/mdi.txt)） | 是（仅打包实际引用的约 40 个 path） |
| `@fontsource/outfit` | `^5.0.0` | 本地打包 Outfit 400/500/600/700 字体（零 CDN） | SIL OFL 1.1 字体 / MIT 包（[LICENSES/outfit.txt](https://github.com/sl251/astrbot_plugin_antigravity_acp/blob/107bef746a686c941ff6ecd575735b20e1b0d86e/LICENSES/outfit.txt)） | 是（仅 woff2 静态文件） |
| `vite` | `6.4.1` | 打包构建工具 | MIT | 否（devDependency） |
| `@vitejs/plugin-vue` | `5.2.4` | Vue SFC 编译插件 | MIT | 否（devDependency） |
| `vite-plugin-vuetify` | `2.1.3` | Vuetify 组件按需自动引入 | MIT | 否（devDependency） |
| `typescript` | `5.1.6` | 类型系统 | Apache-2.0 | 否（devDependency） |
| `vue-tsc` | `1.8.8` | Vue SFC 类型检查器 | MIT | 否（devDependency） |
| `@types/node` | `^20.5.7` | `vite.config.ts` Node 类型定义 | MIT | 否（devDependency） |

---

## 5. Hydrogen 首轮 QA 发现与定向返工记录（待复测）

针对 Hydrogen 真实 QA 日志（`qa-logs/02-npm-typecheck.log`、`qa-logs/04-check-sensitive.log`）与合规检查发现的阻塞项，已做如下最小修复，**均待 Hydrogen 重新执行 typecheck / build / 敏感扫描 / 离线回归复验**：

1. **`useDashboardState.ts` `monitorPending` ref 误用（阻塞监控轮询 + TS2588）**：`refreshMonitor` 守卫与赋值、`pauseRefresh` 重置全部修正为 `monitorPending.value`，保持对外返回 ref 契约不变。
2. **`MonitorCharts.vue` 空值收窄（TS18047）**：`rebuildReasons` 及 Token 计算改用局部可选变量，避免 `props.summary` 为 `null` 时的类型报错，不使用非空断言。
3. **`diagnostics.ts` 空值收窄（TS18047）**：`failure_events`、`failure_retention`、`window` 改用局部可选变量收窄，保持严格白名单不导出 `csrf`/`ticket`/`code`/`url`。
4. **文档敏感扫描误报**：`docs/acp-30-frontend-handoff.md` 第 16 行由「包名@版本」类邮箱形态改为「包名 (版本)」表格形式，保留精确版本号且不触发邮箱正则，未改动扫描器或白名单。
5. **上游完整许可文本补齐**：从实际安装依赖原样引入 `LICENSES/vue.txt`、`LICENSES/vuetify.txt`、`LICENSES/outfit.txt`、`LICENSES/mdi.txt`，并在 `THIRD_PARTY_NOTICES.md` 与本文档建立显式链接。

### 5.1 待 Hydrogen / QA 验证步骤

1. **依赖与安全**：在 `frontend/` 执行 `npm install --ignore-scripts` 生成 `package-lock.json`，随后验证 `npm ci --ignore-scripts` 可复现；确认构建后运行 `python tools/check_sensitive.py` 无敏感扫描误报（`vite.config.ts` 已配置 `esbuild.legalComments: 'none'`）。
2. **类型检查与构建**：执行 `npm run typecheck`、`npm run build`、`npm run build:ci`、`npm run verify:dist`，确认 `views/setup/` 仅包含新生成的 `index.html` 与 `assets/*`（旧 `app.js`/`style.css` 已随 `emptyOutDir` 清除）、相对路径为 `./assets/...` 且无任何外网 CDN URL。
3. **离线回归与单测补齐**：
   - 后端联调与 Python 离线回归（`python -m unittest discover -s tests -v`）。
   - 针对新前端模块化源码更新或新增离线单测（覆盖 `#28` 同步 `execCommand` + Clipboard Promise 竞态、手动选区、独立 form draft 防覆盖、POST vs 迟到 GET `actionRevision`、`refreshEpoch`、`visibilitychange`、`pagehide`/`pageshow` BFCache、脱敏白名单导出与 `#32` `routing` / `provider-impact` / `ROUTE_MISMATCH` / `can_disable=false` 拦截、v1/v2 监控兼容）。
4. **独立端到端验收（对应 #31，本次明确未执行）**：
   - 待后续独立 QA 在真实 AstrBot v4.29.4 宿主环境通过浏览器/Playwright 验收桌面 `1440×900` 与移动端 `390×844`、浅/深色主题、HTTP 与受限 sandbox iframe 复制、键盘导航及与官方页面同尺寸对比。

---

## 6. 本次明确未执行项（禁止误报通过）

- **未执行** `npm install` / `npm ci`（尚未生成 `frontend/package-lock.json`）。
- **未执行** `npm run typecheck` / `vue-tsc` / `tsc`。
- **未执行** `npm run build` / `npm run build:ci` / `npm run verify:dist`（`views/setup/` 仍保持旧版文件，无伪造构建产物）。
- **未执行** Python 离线回归、`tests/setup.test.mjs` 或新增单元测试。
- **未执行** 真实 AstrBot 宿主安装、浏览器渲染、Playwright 截图或视觉对齐验收。
- **未执行** 真实 AGY CLI 登录、模型探针或生成请求。
- **未验证** AstrBot `v4.30.x`（无公开 `v4.30.0` tag；未放宽 `metadata.yaml` 版本范围）。

---

## 7. 升级、回滚与已知限制

### 7.1 升级与回滚

- **版本策略**：当前仍保留 `v0.7.5` 候选，不预设 `v0.8.0` 发布；现有用户升级后配置（`_conf_schema.json`）、AstrBot Provider 实例与官方 CLI 凭据均无需迁移。
- **回滚方式**：
  - 旧版 Release（如 `v0.7.5`）的原生 `views/setup/{index.html,app.js,style.css}` 仍完全可用；回滚前端仅需恢复基线提交 `0a393de2ff88a8972cbe4849705548d79fbe43ae` 对应的 `views/setup/`、`.github/workflows/test.yml`、`.gitattributes`、`.gitignore`、`.astrbot-plugin/i18n/` 与 `THIRD_PARTY_NOTICES.md`，并移除 `frontend/` 与本文档。
  - 若在生产环境使用了 `#32` 设全局默认操作，宿主持久化主 ID 仍保留，按后端交接说明在回退前显式切回原启用实例即可。

### 7.2 已知限制

1. **宿主自定义主色**：AstrBot v4.29.4 的 `PluginViewPage.vue` 仅下发 `isDark`，不下发用户自定义的 `themePrimary`/`themeSecondary`；前端已做安全能力探测，但 4.29.4 下固定对齐官方默认浅/深色主色（`#3c96ca` / `#5ba4d4`）。
2. **实例停用与删除受限**：AstrBot v4.29.4 无法在资源锁内枚举全部历史会话覆盖与未加载 profile，因此后端对停用/删除均返回 `can_disable: false` / `can_delete: false`；前端按契约展示影响与阻止原因且不允许绕过。
3. **高级参数写入口**：后端未开放插件主配置写入接口，高级设置页仅提供实时只读策略展示、宿主配置路径引导与白名单脱敏诊断导出。
4. **字体与离线策略**：拉丁数字/标题使用本地打包的 `@fontsource/outfit`（400/500/600/700），中文字符使用系统 CJK 字体回退，不打包数 MB 中文字体也不请求任何外网 CDN。
5. **非实机验证**：所有构建、类型检查、单测与真实宿主/视觉验证均待 Hydrogen 与独立 QA 后续执行。

