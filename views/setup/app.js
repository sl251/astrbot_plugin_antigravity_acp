const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView;
let state, pending = false, disposed = false, timer, revision = 0, cachedModels = [], modelSignature = "", effortSignature = "", providerSignature = "";
let themeMode = "auto";
let monitorTimer, monitorPending = false, monitorReady = false, monitorSnapshot;
const activeLogin = (login) => login.busy;
const stages = {discovery:"查找 CLI",isolation:"检查隔离目录",authentication:"检查认证",initialize:"ACP 握手","session/new":"验证登录",model_catalog:"读取模型目录",model_selection:"检查模型档位",complete:"检查通过"};
const loginStates = {idle:"等待登录",starting:"正在启动",waiting:"等待授权",verifying:"正在验证",complete:"登录已验证",failed:"登录未完成",cancelled:"已取消"};

function applyTheme() {
  const root = document.documentElement;
  if (!root) return;
  if (themeMode === "light" || themeMode === "dark") {
    root.dataset.theme = themeMode;
  }
  const labels = {auto: "主题：自动", light: "主题：浅色", dark: "主题：深色"};
  const btn = $("theme-toggle");
  if (btn) btn.textContent = labels[themeMode];
}

function cycleTheme() {
  const order = ["auto", "light", "dark"];
  const root = document.documentElement;
  if (themeMode === "auto" && root?.dataset) {
    themeMode = root.dataset.theme === "dark" ? "light" : "dark";
  } else {
    themeMode = order[(order.indexOf(themeMode) + 1) % order.length];
  }
  applyTheme();
}

function badge(id, text, kind = "") {
  $(id).textContent = text;
  $(id).dataset.state = kind;
}

function showError(message = "") {
  $("error").textContent = message;
  $("error").hidden = !message;
}

async function copy(input) {
  input.focus(); input.select();
  try {
    await navigator.clipboard.writeText(input.value);
    $("copy-status").textContent = "已复制";
  } catch {
    $("copy-status").textContent = "已选中，请按 Ctrl+C / ⌘C 复制";
  }
}

function monitorNode(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

const monitorCount = value => Number.isFinite(value) ? value.toLocaleString() : "未知";
const monitorPercent = value => Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "未知";
function monitorTime(value) {
  const date = new Date(value);
  return value && Number.isFinite(date.getTime()) ? date.toLocaleString() : "未报告";
}

function monitorAgo(value) {
  const timestamp = value ? Date.parse(value) : NaN;
  if (!Number.isFinite(timestamp)) return "时间未报告";
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  return `${Math.floor(seconds / 3600)} 小时前`;
}

function monitorMetric(label, value) {
  const item = monitorNode("div", "monitor-metric");
  item.append(monitorNode("dt", "small", label), monitorNode("dd", "", value));
  return item;
}

function renderMonitor(data) {
  if (data.schema_version !== 1 || !Array.isArray(data.instances)) throw new Error("monitor schema");
  const cards = data.instances.map(row => {
    const card = monitorNode("article", "monitor-instance");
    const title = monitorNode("div", "monitor-instance-head");
    title.append(monitorNode("h3", "", row.provider_id), monitorNode("span", "badge", row.enabled ? "已启用" : "已停用"));
    card.append(title, monitorNode("p", "monitor-model", row.model || "尚未选择模型"));
    card.append(monitorNode("p", "small", `模式：${row.tools_mode || "inherit"} → ${row.effective_mode || "未知"} · 工具实际生效：${row.tools_enabled === true ? "开启" : row.tools_enabled === false ? "关闭" : "未知"}`));
    if (row.runtime_state !== "active") {
      card.append(monitorNode("p", "monitor-empty", "已配置未初始化，暂无运行统计。"));
      return card;
    }
    const pool = row.pool;
    const summary = row.summary;
    const metrics = monitorNode("dl", "monitor-metrics");
    metrics.append(
      monitorMetric("会话池 active / max", pool ? `${monitorCount(pool.active_sessions)} / ${monitorCount(pool.max_sessions)}` : "未知"),
      monitorMetric("存活进程 / 工具续轮", pool ? `${monitorCount(pool.live_processes)} / ${monitorCount(pool.active_tool_turns)}` : "未知"),
      monitorMetric("24h 记录轮数 / 实际发送", summary ? `${monitorCount(summary.total_turns)} / ${monitorCount(summary.request_sent_turns)}` : "未知"),
      monitorMetric("成功 / 失败", summary ? `${monitorCount(summary.success_turns)} / ${monitorCount(summary.failed_turns)}` : "未知"),
      monitorMetric("会话复用率", monitorPercent(summary?.reuse_rate)),
      monitorMetric("缓存读取占比", monitorPercent(summary?.cache_token_ratio)),
      monitorMetric("已知输入 Token（其他 / 缓存读取）", summary?.reported_usage_turns > 0 ? `${monitorCount(summary.total_input_other)} / ${summary.reported_usage_turns > summary.missing_cache_field_turns ? monitorCount(summary.total_input_cached) : "未知"}` : "未知"),
      monitorMetric("已知输出 Token", summary?.reported_usage_turns > 0 ? monitorCount(summary.total_output) : "未知"),
      monitorMetric("未报告 usage 轮数", monitorCount(summary?.unavailable_usage_turns)),
      monitorMetric("已报告 usage 中缺缓存字段", monitorCount(summary?.missing_cache_field_turns)),
    );
    card.append(metrics);
    if (summary?.missing_cache_field_turns > 0 || summary?.unavailable_usage_turns > 0) {
      card.append(monitorNode("p", "small", "Token 仅汇总已知部分；缺失用量或缓存字段的轮次不视为消耗为 0。缓存占比仅使用已报告缓存读取的样本。"));
    }
    const context = row.latest_context || {};
    const contextBox = monitorNode("div", "monitor-context");
    contextBox.append(monitorNode("h4", "", "最近采样的存活会话上下文"));
    const validContext = Number.isFinite(context.context_used) && Number.isFinite(context.context_window_size)
      && context.context_window_size > 0 && context.context_used >= 0 && context.observed_at;
    if (validContext) {
      const ratio = context.context_used / context.context_window_size;
      contextBox.append(monitorNode("p", "hint-text", `${monitorCount(context.context_used)} / ${monitorCount(context.context_window_size)} Token · ${monitorPercent(ratio)}`));
      const progress = monitorNode("progress", "monitor-progress");
      progress.max = context.context_window_size;
      progress.value = context.context_used;
      progress.setAttribute("aria-label", "最近采样的存活会话上下文占用");
      contextBox.append(progress, monitorNode("p", "small", `${context.model || row.model} · 采样于 ${monitorTime(context.observed_at)}`));
    } else {
      contextBox.append(monitorNode("p", "hint-text", "上游未报告"));
    }
    contextBox.append(monitorNode("p", "small", "显示一个存活会话的最近有效样本，不汇总各会话，也不以历史重放字节预算代替上下文窗口。"));
    card.append(contextBox);
    const recent = Array.isArray(row.recent_turns) ? row.recent_turns : [];
    const faults = recent.filter(turn => turn.error_code).slice(0, 3);
    const events = monitorNode("div", "monitor-events");
    events.append(monitorNode("h4", "", "最近异常（最近 20 轮内）"));
    if (faults.length) {
      const list = monitorNode("ul", "monitor-faults");
      faults.forEach(turn => {
        const item = monitorNode("li", "");
        item.append(monitorNode("code", "", turn.error_code), monitorNode("span", "small", monitorAgo(turn.recorded_at)));
        list.append(item);
      });
      events.append(list);
    } else {
      events.append(monitorNode("p", "small", recent.length ? "最近记录中暂无异常" : "暂无轮次记录"));
    }
    if (recent.length) events.append(monitorNode("p", "small", `最近一轮：${recent[0].reused ? "复用会话" : "新建或重建会话"} · ${monitorAgo(recent[0].recorded_at)}`));
    if (Array.isArray(summary?.rebuild_reasons) && summary.rebuild_reasons.length) {
      events.append(monitorNode("p", "small", `重建原因：${summary.rebuild_reasons.slice(0, 3).map(([reason, count]) => `${reason} × ${count}`).join(" · ")}`));
    }
    card.append(events);
    return card;
  });
  $("monitor-instances").replaceChildren(...cards);
  $("monitor").dataset.stale = "false";
  badge("monitor-badge", "本地快照");
  $("monitor-message").textContent = cards.length ? "运行时已初始化不代表上游实时在线；环境检测结果见下方向导。" : "尚未配置 AGY 实例，请在下方完成模型配置。";
  $("monitor-updated").textContent = `快照时间：${monitorTime(data.sampled_at)} · 每 10 秒刷新，页面隐藏时暂停`;
  monitorSnapshot = data;
}

async function refreshMonitor() {
  clearTimeout(monitorTimer);
  if (disposed || document.hidden || monitorPending || !monitorReady) return;
  monitorPending = true;
  $("monitor-refresh").disabled = true;
  $("monitor-refresh").textContent = "刷新中…";
  try {
    const data = await bridge.apiGet("monitor/status");
    if (!disposed) renderMonitor(data);
  } catch (error) {
    if (disposed) return;
    const status = error?.status || error?.statusCode || error?.status_code;
    const denied = status === 401 || status === 403 || /登录|权限|管理员|unauthorized|forbidden/i.test(error?.message || "");
    $("monitor").dataset.stale = "true";
    badge("monitor-badge", "数据过期", "error");
    $("monitor-message").textContent = denied
      ? "无权读取监控，请使用 AstrBot 管理员账号登录后刷新。"
      : monitorSnapshot ? "监控读取失败，以下为上一次快照，数据已过期。" : "暂时无法读取监控数据，请稍后刷新。";
    if (denied) {
      $("monitor-instances").replaceChildren();
      monitorSnapshot = undefined;
    }
  } finally {
    monitorPending = false;
    if (!disposed) {
      $("monitor-refresh").disabled = false;
      $("monitor-refresh").textContent = "刷新";
      if (!document.hidden) monitorTimer = setTimeout(refreshMonitor, 10000);
    }
  }
}

function render(data) {
  state = data;
  const r = data.report, login = data.login;
  const busy = pending || data.busy || activeLogin(login);
  const passed = r.status === "passed";
  if (Array.isArray(r.models)) cachedModels = r.models;
  else if (!busy) cachedModels = [];
  const models = cachedModels;
  $("connection").textContent = busy ? "正在同步状态…" : "已连接 AstrBot";
  $("environment").textContent = `${data.environment.system} · Python ${data.environment.python}${data.environment.container ? " · 容器环境" : ""}`;
  $("discovery-hint").textContent = data.environment.discovery_hint || "";
  $("install-hint").textContent = data.environment.install_hint;
  $("install-url").value = data.environment.install_url;
  $("install-command").value = data.environment.install_command;
  $("facts").replaceChildren(...[
    r.cli_version ? `CLI ${r.cli_version}` : null,
    r.customizations_mode ? `组件模式：${r.customizations_mode}` : null,
    r.cli_flags ? `精简参数：${r.cli_flags.include_default_customizations ? "支持（工具兼容性待验收）" : "未探测到"}` : null,
    r.tool_bridge_transport ? `工具桥：${({loopback_hmac:"本机认证连接",unix_socket:"Unix Socket",unsupported:"当前平台不支持"})[r.tool_bridge_transport] || "未知"}` : null,
    r.process_supervision ? `进程托管：${({windows_job:"Windows Job Object",windows_taskkill_fallback:"taskkill 回退",posix_process_group:"独立进程组"})[r.process_supervision] || "未知"}` : null,
    r.cli_update_status ? "CLI 更新：手动管理，未检查远程最新版" : null,
    r.proxy_mode ? `代理：${r.proxy_mode}` : null,
    r.model_count != null ? `${r.model_count} 个可用模型` : null,
    Number.isInteger(r.exit_code) ? `CLI 退出码：${r.exit_code}` : null,
    r.error_code && stages[r.stage] ? `失败阶段：${stages[r.stage]}` : null,
  ].filter(Boolean).map(text => {const span = document.createElement("span");span.className = "fact";span.textContent = text;return span;}));
  const errorLabels = {NOT_INSTALLED:"未找到 CLI",COMMAND_NOT_FOUND:"配置路径无效",STARTUP_EXIT:"握手前退出",CLI_WRAPPER:"启动脚本不兼容"};
  badge("check-badge", data.busy ? stages[r.stage] || "正在检测" : passed ? "环境就绪" : errorLabels[r.error_code] || r.error_code || "等待检测", passed ? "ready" : r.error_code ? "error" : "");
  $("check-hint").textContent = data.busy ? `正在${stages[r.stage] || "检查环境"}…` : data.hint;
  $("check").textContent = data.busy ? "检测中…" : "重新检测";
  $("check").disabled = busy;
  if (["NOT_INSTALLED", "COMMAND_NOT_FOUND", "STARTUP_EXIT", "CLI_WRAPPER"].includes(r.error_code)) $("install-details").open = true;
  badge("login-badge", passed ? "已连接" : loginStates[login.state] || "等待登录", passed ? "ready" : login.state === "failed" ? "error" : "");
  $("login").disabled = busy || !r.cli_version;
  $("login").textContent = passed ? "重新登录" : "开始官方登录";
  $("cancel-login").hidden = !activeLogin(login) || !login.owned;
  $("cancel-login").disabled = pending;
  $("login-hint").textContent = activeLogin(login)
    ? `${login.owned ? "请在浏览器完成授权" : "另一位管理员正在登录"}（剩余 ${login.expires_in}s）`
    : passed ? "Google 认证已通过验证。"
    : login.state === "failed" ? `登录未完成（${login.error_code}），请重试或使用终端登录。`
    : "首次使用请启动官方登录并在浏览器完成授权。";
  $("authorization").hidden = !login.url || !login.owned;
  $("auth-url").value = login.url || "";
  if (login.state !== "waiting") $("auth-code").value = "";
  $("submit-code").disabled = pending || login.state !== "waiting";

  const signature = JSON.stringify(models);
  if (modelSignature !== signature) {
    const previous = $("model").value;
    $("model").replaceChildren(new Option(models.length ? "请选择模型" : "先完成环境检测", ""), ...models.map(m => new Option(m.name, m.id)));
    const preferred = [previous, r.selected_model, data.providers[0]?.model].find(id => models.some(m => m.id === id));
    if (preferred) $("model").value = preferred;
    modelSignature = signature;
  }
  const selected = $("model").value;
  const efforts = r.selected_model === selected ? r.efforts || [] : [];
  const effortKey = JSON.stringify([selected, efforts, data.global_effort]);
  if (effortSignature !== effortKey && (!busy || r.selected_model === selected)) {
    const previous = $("effort").value;
    $("effort").replaceChildren(new Option(`跟随插件默认（${data.global_effort}）`, "default"), ...efforts.map(e => new Option({low:"低",medium:"中",high:"高"}[e],e)));
    if (["default", ...efforts].includes(previous)) $("effort").value = previous;
    effortSignature = effortKey;
  }
  $("model").disabled = busy || !models.length;
  $("effort").disabled = busy || !passed || !selected;
  if (!$("tools-mode").value) $("tools-mode").value = "chat";
  $("tools-mode").disabled = busy || !passed || !selected;
  $("apply").disabled = busy || !passed || !selected;
  $("use-default").disabled = busy;
  badge("model-badge", models.length ? `${models.length} 个模型` : "等待登录", passed ? "ready" : "");
  const configured = data.providers;
  badge("finish-badge", data.result ? "已保存" : configured.length ? "已配置" : "等待配置", configured.length ? "ready" : "");
  $("finish-hint").textContent = data.result
    ? `${data.result.default_selected ? "已设为全局默认模型。" : "模型已保存并启用。"} 可复制下方命令在指定会话中切换：`
    : configured.length ? "已配置的模型实例（复制命令可在会话中切换）：" : "保存模型后将在此生成会话切换命令。";
  const providerKey = JSON.stringify(configured);
  if (providerSignature !== providerKey) {
    $("configured").replaceChildren(...configured.map(row => {
      const wrapper = document.createElement("div");wrapper.className = "provider";
      const title = document.createElement("strong");title.textContent = `${row.model} · ${row.tools_mode || "inherit"} · ${row.enabled ? "已启用" : "已停用"}`;
      const line = document.createElement("div");line.className = "copy-row";
      const input = document.createElement("input");input.readOnly = true;input.value = `/agy use ${row.id}`;input.setAttribute("aria-label", "切换模型命令");
      const button = document.createElement("button");button.type = "button";button.className = "btn btn-sm";button.textContent = "复制命令";button.addEventListener("click", () => copy(input));
      line.append(input, button);wrapper.append(title,line);return wrapper;
    }));
    providerSignature = providerKey;
  }
  if (data.action_error) showError(`${data.action_error}：${data.action_hint}`);
}

async function action(name, body = {}) {
  if (!state || pending) return;
  revision++;
  pending = true; showError(); render(state);
  try {
    const data = await bridge.apiPost(`setup/${name}`, {...body, csrf: state.csrf});
    pending = false; render(data);
  } catch (error) {
    pending = false; render(state);showError(error.message || "请求失败，请刷新页面。");
  }
}

$("theme-toggle").onclick = cycleTheme;
$("monitor-refresh").onclick = refreshMonitor;
$("check").onclick = () => action("check");
$("login").onclick = () => action("login");
$("cancel-login").onclick = () => action("login-cancel", {ticket: state.login.ticket});
$("copy-auth").onclick = () => copy($("auth-url"));
$("copy-install").onclick = () => copy($("install-url"));
$("copy-command").onclick = () => copy($("install-command"));
$("code-form").onsubmit = async event => {
  event.preventDefault();
  const code = $("auth-code").value;$("auth-code").value = "";
  await action("login-code", {ticket: state.login.ticket, code});
};
$("model").onchange = () => {
  $("effort").value = "default";
  if ($("model").value) action("check", {model: $("model").value});
  else render(state);
};
$("model-form").onsubmit = event => {
  event.preventDefault();
  action("apply", {
    model: $("model").value,
    effort: $("effort").value,
    tools_mode: $("tools-mode").value || "chat",
    use_default: $("use-default").checked,
  });
};

async function poll() {
  if (disposed) return;
  try {
    if (!pending) {
      const currentRevision = revision;
      const data = await bridge.apiGet("setup/status");
      if (!pending && !disposed && currentRevision === revision) render(data);
    }
  } catch (error) {
    $("connection").textContent = "连接中断，请刷新页面";
    showError(error.message || "无法读取状态。");
  } finally {
    if (!disposed) timer = setTimeout(poll, state?.busy || (state && activeLogin(state.login)) ? 1200 : 5000);
  }
}
document.addEventListener("visibilitychange", () => {
  clearTimeout(monitorTimer);
  if (!document.hidden && !disposed) refreshMonitor();
});
window.addEventListener("pagehide", () => {disposed = true;clearTimeout(timer);clearTimeout(monitorTimer);monitorSnapshot = undefined;$("auth-code").value = "";$("auth-url").value = "";state = null;});
if (!bridge) {
  showError("请从 AstrBot WebUI 侧边栏或插件详情打开 Antigravity ACP 面板。");
  badge("monitor-badge", "不可用");
  $("monitor-message").textContent = "请从 AstrBot 管理面板打开运行概览。";
} else {
  try {
    await bridge.ready();
    monitorReady = true;
    refreshMonitor();
    await poll();
  } catch (error) {
    showError(error.message || "面板连接失败。");
    badge("monitor-badge", "不可用", "error");
    $("monitor-message").textContent = "面板连接失败，请刷新页面。";
  }
}
