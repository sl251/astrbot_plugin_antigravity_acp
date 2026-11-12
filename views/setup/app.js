const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView;
let state, pending = false, disposed = false, timer, revision = 0, cachedModels = [], modelSignature = "", effortSignature = "", providerSignature = "";
let themeMode = "auto";
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
      const title = document.createElement("strong");title.textContent = `${row.model} · ${row.enabled ? "已启用" : "已停用"}`;
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
  action("apply", {model: $("model").value, effort: $("effort").value, use_default: $("use-default").checked});
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
window.addEventListener("pagehide", () => {disposed = true;clearTimeout(timer);$("auth-code").value = "";$("auth-url").value = "";state = null;});
if (!bridge) {
  showError("请从 AstrBot WebUI 侧边栏或插件详情打开 Antigravity ACP 面板。");
} else {
  try {
    await bridge.ready();
    await poll();
  } catch (error) {showError(error.message || "面板连接失败。");}
}
