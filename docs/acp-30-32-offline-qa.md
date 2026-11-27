# ACP #30 / #32 独立离线 QA 与候选构建报告（精简收口版）

> 历史候选构建与离线证据（#30 / #32）。源码、测试和日志路径对应当时工作区；当前验证按[测试规范](testing.md)，#31 状态见[索引](plan-status.md)。

- **执行角色**：Hydrogen（`gemini-4-hydrogen-xhigh`），仅执行离线测试、类型检查与前端候选构建；未修改产品源码（`agy_provider/**`、`main.py`、业务 `frontend/src/**`、`vite.config.ts`、`package.json` 依赖版本、`LICENSES/**`、`tools/check_sensitive.py`）。
- **任务根与工作树**：`/root/hermes-workspace/acp-30-32-2026-11-24/candidate`
- **基线与候选提交**：
  - 原始未修复基线：`0a393de2ff88a8972cbe4849705548d79fbe43ae`（离线对照副本位于 `/root/.hermes/cache/scratch/acp-30-32-2026-11-24/baseline-source`）
  - 后端集成父提交：`d11b7a2`（`d11b7a2276419d16e02de402d4bf344611e5`）
  - 当前候选 `HEAD`：`0e69c8fae5b379ba1fac49013b217c2d548643cd`（分支 `fix/acp-30-32-20261124`，领先 `origin/main` 2 个提交，另含工作区未提交的 Neon 修复、新增测试用例、锁文件、构建产物与 QA 报告）
- **环境说明**：Python `3.11.2`（`tenacity` 未安装，host runner AST 接缝使用测试内嵌重试桩）、Node `v26.12.1`、npm `11.20.0`（注意 `.github/workflows/test.yml` 使用 Node 22）。未执行任何实机 CLI、真实 AstrBot、浏览器/Playwright/截图、SSH/Docker/QQ 或 OAuth 凭据读取。

---

## 1. 工作区未提交文件清单（生成报告前 `git status` 快照）

- **前端产品与合规文档（Neon 先前落地，QA 未改动）**：
  - `M frontend/src/components/MonitorCharts.vue`
  - `M frontend/src/composables/useDashboardState.ts`
  - `M frontend/src/utils/diagnostics.ts`
  - `M THIRD_PARTY_NOTICES.md`
  - `M docs/acp-30-frontend-handoff.md`
  - `?? LICENSES/`（`vue.txt`、`vuetify.txt`、`outfit.txt`、`mdi.txt`）
- **QA 迁移与新增测试 / 构建产物**：
  - `M tests/test_onboarding.py`、`M tests/test_monitoring.py`、`M tests/test_prompt_images.py`、`M tests/test_release_hygiene.py`
  - `?? tests/test_routing.py`、`?? tests/test_b06_side_effects.py`、`?? tests/test_b07_b08_regressions.py`、`?? tests/test_failure_events.py`
  - `?? frontend/package-lock.json`
  - `D views/setup/app.js`、`D views/setup/style.css`、`M views/setup/index.html`、`?? views/setup/assets/`
  - `?? docs/acp-30-32-offline-qa.md`（本报告，另任务根生成 `../qa-results.json` 与 `../qa-logs/`）

---

## 2. 已验证项与最新实跑结果

### 2.1 本次收口命令 1：前端 TypeScript / `vue-tsc` 类型检查

- 命令：`cd frontend && npm run typecheck`
- 退出码：`0`（`PASS`）
- 原始日志：`../qa-logs/13-typecheck-final.log`
- 说明：首轮检查曾因 `useDashboardState.ts` 的 `monitorPending` ref 误用（TS2588 ×3）、`MonitorCharts.vue`（TS18047）、`diagnostics.ts`（TS18047）失败（`../qa-logs/02-npm-typecheck.log`，`rc=2`）；Neon 定向修复后，本次复跑 `vue-tsc --noEmit -p tsconfig.json && tsc --noEmit -p tsconfig.node.json` 零错误通过。

### 2.2 前端构建与产物一致性（上一轮已完成，沿用结果）

- `npm --prefix frontend run build`（`../qa-logs/10-npm-build.log`，`rc=0`）：Vite 6.4.1 转换 322 个模块，**无 esbuild const 赋值告警**，生成 `views/setup/index.html` 与 `views/setup/assets/` 共 20 个资源文件（`vue` / `vuetify` / `index` JS、`style` CSS、16 个 `outfit` woff/woff2 本地字体，总计 21 个构建产物），旧 `views/setup/app.js` 与 `style.css` 已由 `emptyOutDir` 清除。
- `npm --prefix frontend run build:ci`（`../qa-logs/11-npm-build-ci.log`，`rc=0`）：生成 `frontend/dist`（322 模块）。
- `npm --prefix frontend run verify:dist`（`../qa-logs/12-npm-verify-dist.log`，`rc=0`）：输出 `Verified 21 built artifact(s) in views/setup with zero CDN links.`。

### 2.3 本次收口命令 2：全量 Python 离线单元测试

- 命令：`python3 -B -m unittest discover -s tests -q`
- 退出码：`1`（`Ran 298 tests in 34.199s`：`pass=281`、`fail=4`、`error=1`、`skipped=12`）
- 原始日志：`../qa-logs/14-python-final.log`
- 与基线初期对比：首轮全量（`../qa-logs/00-initial-python-unittest.log`，`Ran 278`，`16 fail / 2 error / 12 skip`）中因新契约变更引起的全部 18 项失败/错误（`test_onboarding.py`、`test_monitoring.py`、`test_prompt_images.py`、`test_release_hygiene.py`）在本轮已全部通过；新增回归中 `test_routing.py`（5/5）、`test_b07_b08_regressions.py`（6/6）、`test_b06_side_effects.py` 的 3 个独立方法及成对用例的 `pre_handoff` / `continuation_stale`、`test_failure_events.py` 的 3 个 B09 用例均通过。

#### 2.4 全量 Python 失败与错误分类（仅记录，未修改或重跑）

1. **[测试夹具缺陷]** `tests/test_failure_events.py:455` (`ERROR: test_b10_zero_generation_read_only_routes_auth_and_doctor`)
   - 实际输出：`ModuleNotFoundError: No module named 'astrbot.api.event'; 'astrbot.api' is not a package`（执行 `import main as main_module` 时触发）。
   - 根因：`tests/astrbot_stub.py` 仅将 `astrbot.api` 设为非包模块，而该新测试未临时注入 `astrbot.api.event`、`astrbot.api.star` 与 `astrbot.core.provider.register` 桩；同文件前 3 项 B09 测试通过。
2. **[测试夹具生命周期缺陷]** `tests/test_b06_side_effects.py:495,511` (`FAIL` ×3 个子测试：`test_comment32_paired_non_idempotent_side_effect_pre_vs_post_handoff` 中的 `ordinary_exc_after_write`、`agy_timeout_after_write`、`cancelled_after_write`)
   - 实际输出：前两项 `AssertionError: 'assistant' != 'err'`，第三项 `AssertionError: CancelledError not raised`，且后台 `Task` 打印 `finally` 中 `aclose()` 抛出的未取回异常。
   - 根因：4.29.4 `_iter_llm_responses_with_fallback` 在收到 `is_chunk=False` 的工具调用响应后立即 `yield resp; return`，测试包装函数 `host_post_handoff_fail` 把故障放在生成器 `finally:`，导致故障只在异步生成器 GC 时触发（已脱离 `_iter_llm_responses_with_fallback`），并非产品跳过拦截；同方法内的 `(a) pre_handoff (writes=0 -> backup.calls=1)` 与 `(b) continuation_stale (writes=1 -> backup.calls=0, call.state="unknown")` 均通过。
3. **[疑似产品并发竞争缺陷 — P2]** `tests/test_bridge_hardening.py:134` (`FAIL: test_approval_rejection_timeout_and_connection_cancel`)
   - 实际输出：`AssertionError: {'agy_...': PendingCall(..., future=<Future cancelled>, state='unknown', delivered=True, handed_off=<Event [set]>)} is not false`。
   - 根因定位：`agy_provider/tools.py` 第 434–447 行（`ToolBroker._connection` 的 `finally:` 非屏蔽 `await asyncio.gather(work, eof)`）与第 631–638 行（`ToolBroker._close` 取消 `self.tasks`）。客户端取消连接后，`_close()` 并发取消 `_connection` 任务，可打断 `_connection` 对 `work`（`dispatch`）的等待，使第 536 行 `self.pending.pop(call.id, None)` 尚未执行完毕即返回 `_close()`。该基线原有测试在首轮 `00-initial-python-unittest.log` 中曾通过、在本轮偶然复现，交由 Astra 排查（QA 未改产品代码）。

### 2.5 先前已采集证据与静态检查（沿用记录）

- **基线 vs 候选 Red/Green 合成接缝**：
  - `../qa-logs/repro-baseline-incident.log`（`rc=1`，`Ran 6`，`fail=2, error=1`）→ `../qa-logs/repro-candidate-incident.log`（`rc=1`，`Ran 6`，`fail=1`）：`wizard_default_selected` 与 `historical_images` 已转绿；仅 `test_regression_deleting_selected_primary_must_not_fall_into_unrelated_vlm` 仍 fail（归类 **`HOST-DEFERRED`**：AstrBot 4.29.4 原生 `delete_provider` 全局行为，本次按作用域仅检测提示、不做全局宿主补丁）。
  - `../qa-logs/repro-baseline-review.log`（`rc=1`，`Ran 8`，`fail=5`）→ `../qa-logs/repro-candidate-review.log`（`rc=0`，`Ran 8`，`OK`）：8/8 全部通过。
- **敏感扫描残留（Neon 待修）**：`../qa-logs/06b-check-sensitive.log`（`rc=1`，在最终 `build` 之前运行）：命中 `docs/acp-30-frontend-handoff.md:191 [email] pkg@x.….y.z`。Neon 修复第 16 行时在第 191 行示例文本保留了同构写法，需 Neon 删除该模式；QA 未改文档或扫描器。
- **第三方许可证文本核验（SHA-256 比对记录）**：
  - `LICENSES/vue.txt`、`LICENSES/vuetify.txt` 与 `frontend/node_modules` 对应许可证字节完全一致；
  - `LICENSES/mdi.txt` 正文一致，仅换行符 LF vs 上游 CRLF；
  - `LICENSES/outfit.txt` 第 86、92 行免责条款文本（`...THE SOFTWARE...`）与 `frontend/node_modules/@fontsource/outfit/LICENSE` SIL OFL 1.1 原文（`...THE FONT SOFTWARE...`）存在字面差异，建议 Neon 按上游原件字节覆盖确认。

---

## 3. 因精简未验证 / 未完成项（`NOT-RUN` 显式清单）

因额度收紧，以下项目在本轮精简收口中明确标为 **`NOT-RUN`**（其中 `npm audit` 仅保留先前原始导出，未做全量人工复核与修复验收）：

| 检查项 | 状态 | 说明 |
| --- | --- | --- |
| `npm audit` 分类与修复验收 | `NOT-RUN`（仅早期原始采样） | 早期仅导出一次 `../qa-logs/09-npm-audit.json`（`AUDIT_RC=1`，12 条告警：7 high / 5 moderate / 0 critical，涉 `@vue/server-renderer`、`vite`、`minimatch`、`vue-template-compiler` 及传递依赖），未完成正式人工可达性分类报告与修复验证。 |
| 新版 Vue/TS UI 原生 Node 单测与 `tests/setup.test.mjs` 迁移 | `NOT-RUN` | 未编写/运行针对新 `frontend/src/**` 的组件/Composable/Bridge/诊断导出 Node 测试；`tests/setup.test.mjs` 仅在早期针对已删除的旧 `views/setup/app.js` 跑过（`../qa-logs/00-initial-node-setup.log`，29/29），对新版 `views/setup/` 构建产物无效。 |
| `npm ci --ignore-scripts` 洁净可重复性复现 | `NOT-RUN` | 仅在早期生成了 `frontend/package-lock.json`（`../qa-logs/01-npm-install.log`），未执行独立 `npm ci` 洁净安装复现验证。 |
| 构建后 `tools/check_sensitive.py`、`compileall`、`git diff --check`、`git archive` 打包清单验证 | `NOT-RUN` | 本轮精简后未执行。 |
| B01–B10 新增用例对 `baseline-source` 的逐项 Red 复现、F02–F08 细粒度单测 | `NOT-RUN` | — |
| 实机 `agy acp`、真实 AstrBot 4.29.4 容器/QQ、浏览器/视觉截图、Windows CI 矩阵 | `NOT-RUN` | 超出本次离线任务范围。 |
