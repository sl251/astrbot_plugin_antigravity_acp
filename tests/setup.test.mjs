import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../views/setup/app.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../views/setup/index.html", import.meta.url), "utf8");
const base = () => ({
  csrf: "test-nonce", busy: false, operation: "", report: {
    status: "passed", stage: "complete", cli_version: "1.5.2", selected_model: "gemini-test",
    models: [{id: "gemini-test", name: "Gemini Test"}], efforts: ["low", "high"],
  }, hint: "checked", environment: {system: "Test OS", python: "3.11", install_url: "https://example.test/install"},
  login: {state: "idle", busy: false, owned: false, url: "", ticket: ""},
  providers: [], result: null, action_error: "", global_effort: "default",
});

class Element {
  constructor(tag = "div") {
    this.tag = tag; this.value = ""; this.dataset = {}; this.children = [];
    this.hidden = false; this.disabled = false; this.checked = false; this.textContent = "";
    this.open = false; this.className = ""; this.attributes = {};
    this._classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(n => this._classes.add(n)),
      remove: (...names) => names.forEach(n => this._classes.delete(n)),
      toggle: (name, force) => {
        const next = force === undefined ? !this._classes.has(name) : Boolean(force);
        if (next) this._classes.add(name); else this._classes.delete(name);
        return next;
      },
      contains: name => this._classes.has(name) || this.className.split(/\s+/).includes(name),
    };
  }
  replaceChildren(...children) {
    this.children = children;
    if (this.tag === "select") this.value = children[0]?.value || "";
  }
  append(...children) {this.children.push(...children);}
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    const index = this.children.indexOf(child);
    assert.notEqual(index, -1);
    this.children.splice(index, 1);
    child.parentNode = null;
    return child;
  }
  remove() {this.parentNode?.removeChild(this);}
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") {
      this.className = String(value);
      this._classes = new Set(this.className.split(/\s+/).filter(Boolean));
    }
  }
  getAttribute(name) {return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null;}
  addEventListener(name, callback) {this[name] = callback;}
  focus() {this.focused = true;}
  select() {this.selected = true;}
}

async function boot(initial = base(), {storage, getContext} = {}) {
  const elements = new Map([...html.matchAll(/<([\w-]+)[^>]*\bid="([^"]+)"/g)]
    .map(([, tag, id]) => [id, new Element(tag)]));
  const handlers = {};
  const requests = [];
  const timers = new Map();
  let nextTimer = 0;
  const bridge = {
    ready: async () => ({}), getContext,
    apiGet: async endpoint => endpoint === "monitor/status" ? monitorPayload(null) : initial,
    apiPost: async (endpoint, body) => {requests.push({endpoint, body}); return initial;},
  };
  const context = vm.createContext({
    window: {
      AstrBotPluginView: bridge, addEventListener: (name, cb) => {handlers[name] = cb;},
      get localStorage() {
        if (!storage) throw new Error("sandbox storage unavailable");
        return storage;
      },
    },
    document: {body: new Element("body"), execCommand: () => false,
      documentElement: new Element("html"), getElementById: id => {
      assert.ok(elements.has(id), `Missing element ${id}`);
      return elements.get(id);
    }, createElement: tag => new Element(tag), createElementNS: (_ns, tag) => new Element(tag),
    createTextNode: text => ({tag: "#text", textContent: String(text), children: [], attributes: {}}),
    hidden: false,
    addEventListener: (name, cb) => {handlers[name] = cb;}},
    Option: class {constructor(name, value) {this.text = name; this.value = value;}},
    navigator: {clipboard: {writeText: async () => {throw new Error("iframe clipboard unavailable");}}},
    setTimeout: (callback, delay) => {
      timers.set(++nextTimer, {callback, delay});
      return nextTimer;
    },
    clearTimeout: id => timers.delete(id),
  });
  const api = await vm.runInContext(`(async () => {${source}\n; return {render, renderMonitor, action, poll};})()`, context);
  return {api, bridge, requests, handlers, timers, win: context.window, navigator: context.navigator,
    doc: context.document, get: id => elements.get(id)};
}

test("theme cycles auto → light → dark → auto with sandbox storage blocked", async () => {
  const ui = await boot();
  const button = ui.get("theme-toggle");
  assert.equal(button.textContent, "主题：自动");
  for (const [theme, label] of [["light", "浅色"], ["dark", "深色"], [undefined, "自动"]]) {
    button.onclick();
    assert.equal(ui.doc.documentElement.dataset.theme, theme);
    assert.equal(button.textContent, `主题：${label}`);
  }
});

test("theme inherits host context and restores a stored manual choice", async () => {
  const values = new Map();
  const options = {
    storage: {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value)},
    getContext: () => ({isDark: true}),
  };
  const ui = await boot(base(), options);
  assert.equal(ui.doc.documentElement.dataset.theme, "dark");
  ui.get("theme-toggle").onclick();
  const reopened = await boot(base(), options);
  assert.equal(reopened.doc.documentElement.dataset.theme, "light");
  reopened.get("theme-toggle").onclick();
  reopened.get("theme-toggle").onclick();
  assert.equal(reopened.get("theme-toggle").textContent, "主题：自动");
  assert.equal(reopened.doc.documentElement.dataset.theme, "dark");
});

test("native bridge initialization populates real model and effort options", async () => {
  const ui = await boot();
  assert.equal(ui.get("model").value, "gemini-test");
  assert.equal(ui.get("apply").disabled, false);
  assert.deepEqual(ui.get("effort").children.map(option => option.value), ["default", "low", "high"]);
  assert.equal(ui.get("use-default").checked, false);
});

test("missing CLI enables installation guidance and prevents login/save", async () => {
  const data = base(); data.report = {status: "blocked", error_code: "NOT_INSTALLED"};
  const ui = await boot(data);
  assert.equal(ui.get("install-details").open, true);
  assert.equal(ui.get("login").disabled, true);
  assert.equal(ui.get("apply").disabled, true);
  assert.equal(ui.get("check-badge").textContent, "未找到 CLI");
});

test("startup diagnostics show search scope, stage and zero exit code", async () => {
  const data = base();
  data.report = {status: "blocked", stage: "initialize", error_code: "STARTUP_EXIT", exit_code: 0};
  data.environment.discovery_hint = "当前默认搜索范围：PATH、~/.local/bin/agy。";
  const ui = await boot(data);
  assert.equal(ui.get("check-badge").textContent, "握手前退出");
  assert.match(ui.get("discovery-hint").textContent, /PATH/);
  assert.ok(ui.get("facts").children.some(row => row.textContent === "CLI 退出码：0"));
  assert.ok(ui.get("facts").children.some(row => row.textContent === "失败阶段：ACP 握手"));
  assert.equal(ui.get("install-details").open, true);
});

test("unchanged status preserves input selections, list nodes, and a manually closed drawer", async () => {
  const data = base();
  data.report.error_code = "STARTUP_EXIT";
  data.report.customizations_mode = "minimal";
  data.environment.install_command = "mock-install";
  data.login = {state: "waiting", owned: true, url: "https://example.test/mock"};
  const ui = await boot(data);
  const facts = ui.get("facts").children;
  const diag = ui.get("env-diag-list").children;
  for (const id of ["install-command", "install-url", "auth-url"]) {
    const input = ui.get(id);
    const selected = input.value;
    Object.defineProperty(input, "value", {
      get: () => selected, set: () => assert.fail(`rewrote unchanged ${id}`),
    });
  }
  ui.get("install-details").open = false;
  ui.api.render(structuredClone(data));
  assert.equal(ui.get("facts").children, facts);
  assert.equal(ui.get("env-diag-list").children, diag);
  assert.equal(ui.get("install-details").open, false);
  data.report.error_code = "CLI_WRAPPER";
  ui.api.render(data);
  assert.equal(ui.get("install-details").open, true);
});

test("partial status and empty model catalog give consistent placeholders", async () => {
  const ui = await boot({});
  for (const [data, message] of [[{}, "先完成环境检测"],
    [{report: {status: "passed", models: []}}, "环境已就绪，但未读取到模型目录"]]) {
    ui.api.render(data);
    assert.equal(ui.get("model").children[0].text, message);
    assert.equal(ui.get("model-badge").textContent, message);
  }
});

test("configured path and wrapper failures get distinct labels", async () => {
  for (const [code, label] of [["COMMAND_NOT_FOUND", "配置路径无效"], ["CLI_WRAPPER", "启动脚本不兼容"]]) {
    const data = base(); data.report = {status: "blocked", error_code: code};
    const ui = await boot(data);
    assert.equal(ui.get("check-badge").textContent, label);
    assert.equal(ui.get("apply").disabled, true);
  }
});

test("duplicate actions are blocked and nonce is sent", async () => {
  const ui = await boot();
  let release;
  ui.bridge.apiPost = async (endpoint, body) => {
    ui.requests.push({endpoint, body});
    return new Promise(resolve => {release = resolve;});
  };
  const first = ui.api.action("check");
  await ui.api.action("login");
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].body.csrf, "test-nonce");
  assert.equal(ui.get("apply").disabled, true);
  release(base()); await first;
  assert.equal(ui.get("apply").disabled, false);
});

test("late status response cannot overwrite a newer save", async () => {
  const ui = await boot();
  let release;
  ui.bridge.apiGet = () => new Promise(resolve => {release = resolve;});
  const poll = ui.api.poll();
  const saved = base(); saved.result = {provider_id: "saved-id", default_selected: false};
  saved.providers = [{id: "saved-id", model: "gemini-test", enabled: true}];
  ui.bridge.apiPost = async () => saved;
  await ui.api.action("apply");
  release(base()); await poll;
  assert.equal(ui.get("finish-badge").textContent, "已保存");
});

test("status retries at 5/10/20/30 seconds, recovers, and preserves action errors", async () => {
  const ui = await boot();
  const scheduled = () => [...ui.timers.values()].filter(t => t.callback === ui.api.poll);
  ui.bridge.apiGet = async () => { throw new Error("status offline"); };
  for (const delay of [5000, 10000, 20000, 30000, 30000]) {
    await ui.api.poll();
    assert.deepEqual(scheduled().map(t => t.delay), [delay]);
    assert.equal(ui.get("error").textContent, "status offline");
  }
  ui.bridge.apiGet = async () => base();
  await ui.api.poll();
  assert.equal(ui.get("error").hidden, true);
  ui.bridge.apiPost = async () => { throw new Error("save failed"); };
  await ui.api.action("apply");
  ui.bridge.apiGet = async () => { throw new Error("status offline"); };
  await ui.api.poll();
  assert.deepEqual(scheduled().map(t => t.delay), [5000]);
  ui.bridge.apiGet = async () => base();
  await ui.api.poll();
  assert.equal(ui.get("error").textContent, "save failed");
});

test("login and action errors retain their raw code after translation", async () => {
  const ui = await boot();
  for (const [code, label] of [["LOGIN_EXPIRED", "登录已过期"], ["FUTURE_CODE", "操作失败"]]) {
    ui.api.render({login: {state: "failed", error_code: code}, action_error: code});
    assert.ok(ui.get("login-hint").textContent.includes(`${label}（${code}）`));
    assert.equal(ui.get("error").textContent, `${label}（${code}）`);
  }
});

test("authorization code clears immediately and URL clears while verifying", async () => {
  const data = base(); data.login = {state: "waiting", busy: true, owned: true, ticket: "fake-ticket", url: "https://accounts.google.com/fake", expires_in: 100};
  const ui = await boot(data);
  ui.get("auth-code").value = "fake-code";
  const next = base(); next.login = {...data.login, state: "verifying", url: ""};
  ui.bridge.apiPost = async (endpoint, body) => {
    assert.equal(endpoint, "setup/login-code"); assert.equal(body.code, "fake-code");
    assert.equal(body.ticket, "fake-ticket");
    assert.equal(ui.get("auth-code").value, ""); return next;
  };
  await ui.get("code-form").onsubmit({preventDefault() {}});
  assert.equal(ui.get("auth-url").value, "");
  assert.equal(ui.get("authorization").hidden, true);
});

test("copy tries Clipboard first and runs execCommand during the click", async () => {
  const ui = await boot();
  const button = ui.get("copy-install");
  button.textContent = "复制文档地址";
  const calls = [];
  ui.navigator.clipboard.writeText = async text => {
    calls.push(["clipboard", text]);
    throw new Error("denied");
  };
  ui.doc.execCommand = command => {
    const field = ui.doc.body.children.at(-1);
    assert.equal(field.tag, "textarea");
    assert.equal(field.selected, true);
    calls.push([command, field.value]);
    return true;
  };
  const copying = button.onclick();
  const text = ui.get("install-url").value;
  assert.deepEqual(calls, [["clipboard", text], ["copy", text]]);
  assert.equal(ui.doc.body.children.length, 0);
  await copying;
  assert.equal(button.textContent, "已复制 ✓");
  assert.equal(ui.get("copy-status").textContent, "已复制");
  assert.equal(ui.get("copy-status").dataset.state, "ok");
  await button.onclick();
  const feedback = [...ui.timers.values()].filter(t => t.delay <= 3000);
  assert.deepEqual(feedback.map(t => t.delay), [2000, 3000]);
  feedback.forEach(t => t.callback());
  assert.equal(button.textContent, "复制文档地址");
  assert.equal(ui.get("copy-status").textContent, "");
});

test("all copy controls work without Clipboard API and manual selection is the last fallback", async () => {
  const data = base();
  data.environment.install_command = "install-example";
  data.login = {state: "waiting", owned: true, url: "https://example.test/mock"};
  data.providers = [{id: "instance", model: "test", enabled: true}];
  const ui = await boot(data);
  delete ui.navigator.clipboard;
  ui.doc.execCommand = () => true;
  const instanceButton = collectNodes(ui.get("configured")).find(n => n.tag === "button");
  await instanceButton.click();
  assert.equal(instanceButton.textContent, "已复制 ✓");
  for (const id of ["copy-command", "copy-auth", "copy-install"]) {
    await ui.get(id).onclick();
    assert.equal(ui.get(id).textContent, "已复制 ✓");
  }
  ui.doc.execCommand = () => false;
  await ui.get("copy-auth").onclick();
  assert.equal(ui.get("auth-url").focused, true);
  assert.equal(ui.get("auth-url").selected, true);
  assert.equal(ui.get("copy-auth").textContent, "请手动复制");
  assert.equal(ui.get("copy-status").dataset.state, "warn");
  assert.match(ui.get("copy-status").textContent, /Ctrl\+C/);
});

test("Clipboard success survives execCommand failure and empty fields do nothing", async () => {
  const ui = await boot();
  let writes = 0;
  ui.navigator.clipboard.writeText = async () => { writes++; };
  let commands = 0;
  ui.doc.execCommand = () => { commands++; throw new Error("blocked"); };
  await ui.get("copy-auth").onclick();
  assert.equal(ui.get("copy-status").textContent, "");
  await ui.get("copy-install").onclick();
  assert.deepEqual([writes, commands], [1, 1]);
  assert.equal(ui.get("copy-install").textContent, "已复制 ✓");
  assert.equal(ui.doc.body.children.length, 0);
});

test("blocked authorization popup offers the copy link for null and thrown results", async () => {
  const ui = await boot();
  ui.get("auth-url").value = "https://example.test/mock";
  for (const throws of [false, true]) {
    const calls = [];
    ui.win.open = (...args) => {
      calls.push(args);
      if (throws) throw new Error("sandbox");
      return null;
    };
    ui.get("open-auth").onclick();
    assert.deepEqual(calls, [[ui.get("auth-url").value, "_blank", "noopener"]]);
    assert.equal(ui.get("copy-status").textContent, "浏览器拦截了新窗口，请用复制链接");
  }
});

test("page exit clears sensitive fields and stops polling", async () => {
  const ui = await boot();
  ui.get("auth-code").value = "fake-code";
  ui.get("auth-url").value = "fake-url";
  ui.handlers.pagehide({persisted: false});
  assert.equal(ui.get("auth-code").value, "");
  assert.equal(ui.get("auth-url").value, "");
  ui.bridge.apiGet = () => {throw new Error("disposed page must not poll");};
  await ui.api.poll();
});

test("visibility and bfcache suspend reads and refresh both snapshots on return", async () => {
  const data = base();
  data.login = {state: "waiting", owned: true, url: "https://example.test/mock"};
  const ui = await boot(data);
  const reads = [];
  ui.bridge.apiGet = async endpoint => {
    reads.push(endpoint);
    return endpoint === "setup/status" ? data : monitorPayload(null);
  };
  ui.doc.hidden = true;
  ui.handlers.visibilitychange();
  await ui.api.poll();
  assert.deepEqual(reads, []);
  ui.doc.hidden = false;
  ui.handlers.visibilitychange();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(reads.sort(), ["monitor/status", "setup/status"]);
  reads.length = 0;
  ui.get("auth-code").value = "mock-code";
  ui.handlers.pagehide({persisted: true});
  assert.equal(ui.get("auth-url").value, data.login.url);
  assert.equal(ui.get("auth-code").value, "mock-code");
  await ui.api.poll();
  assert.deepEqual(reads, []);
  ui.handlers.pageshow({persisted: true});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(reads.sort(), ["monitor/status", "setup/status"]);
  assert.equal(ui.get("auth-code").value, "mock-code");
});

test("late action success or failure leaves a disposed page cleared", async () => {
  for (const fail of [false, true]) {
    const ui = await boot();
    let finish;
    const data = base();
    data.login = {owned: true, url: "https://example.test/mock"};
    ui.bridge.apiPost = () => new Promise((resolve, reject) => {
      finish = () => fail ? reject(new Error("late error")) : resolve(data);
    });
    const started = ui.api.action("login");
    ui.handlers.pagehide({persisted: false});
    finish();
    await started;
    assert.equal(ui.get("auth-url").value, "");
    assert.equal(ui.get("error").textContent, "");
  }
});

test("model change resets thought effort and requests fresh catalog", async () => {
  const ui = await boot();
  ui.get("effort").value = "high";
  ui.get("model").onchange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests[0].endpoint, "setup/check");
  assert.equal(ui.requests[0].body.model, "gemini-test");
  assert.equal(ui.get("effort").value, "default");
});

test("model save sends selected tools_mode with chat default", async () => {
  const ui = await boot();
  assert.equal(ui.get("tools-mode").value, "chat");
  ui.get("model-form").onsubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests[0].endpoint, "setup/apply");
  assert.equal(ui.requests[0].body.tools_mode, "chat");

  ui.get("tools-mode").value = "tools";
  ui.get("model-form").onsubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests[1].body.tools_mode, "tools");
});

function collectNodes(root) {
  const nodes = [root];
  for (const child of root.children || []) nodes.push(...collectNodes(child));
  return nodes;
}

function monitorPayload(latestContext) {
  return {
    schema_version: 1,
    sampled_at: "2026-11-17T00:00:00+00:00",
    instances: [{
      provider_id: "agy-main",
      model: "gemini-test",
      enabled: true,
      tools_mode: "chat",
      effective_mode: "chat",
      tools_enabled: false,
      runtime_state: "active",
      pool: {active_sessions: 1, max_sessions: 8, live_processes: 1, active_tool_turns: 0},
      summary: {
        total_turns: 0, request_sent_turns: 0, success_turns: 0, failed_turns: 0,
        reuse_rate: null, cache_token_ratio: null, unavailable_usage_turns: 0,
        reported_usage_turns: 0, missing_cache_field_turns: 0,
        total_input_other: null, total_input_cached: null, total_output: null,
        rebuild_reasons: [],
      },
      latest_context: latestContext,
      recent_turns: [],
    }],
  };
}

test("monitor renders null context_window_size without showing 0%", async () => {
  const ui = await boot();
  ui.api.renderMonitor(monitorPayload({
    context_used: null, context_window_size: null, observed_at: null,
    source: "not_reported", scope: "latest_live_session", model: "gemini-test",
  }));
  const nodes = collectNodes(ui.get("monitor-instances"));
  const texts = nodes.map(n => n.textContent).filter(Boolean);
  assert.ok(texts.includes("上游未报告"));
  assert.equal(nodes.some(n => n.tag === "progress"), false);
  for (const text of texts) {
    assert.doesNotMatch(text, /(?:^|[^0-9.])0(?:\.0)?%/);
  }
});

test("monitor renders real context usage sample with percentage and observed_at", async () => {
  const ui = await boot();
  ui.api.renderMonitor(monitorPayload({
    context_used: 50000, context_window_size: 200000,
    observed_at: "2026-11-17T00:00:00+00:00",
    source: "acp_usage_update", scope: "latest_live_session", model: "gemini-test",
  }));
  const nodes = collectNodes(ui.get("monitor-instances"));
  const texts = nodes.map(n => n.textContent).filter(Boolean);
  const progress = nodes.find(n => n.tag === "progress");
  assert.ok(progress);
  assert.equal(progress.max, 200000);
  assert.equal(progress.value, 50000);
  assert.ok(texts.some(t => t.includes("50,000 / 200,000 Token · 25.0%")));
  assert.ok(texts.some(t => t.includes("采样于") && !t.includes("未报告")));
});

test("passed environment streamlines main hints and moves technical facts to advanced diagnostics", async () => {
  const data = base();
  data.report = {
    ...data.report,
    model_count: 3,
    customizations_mode: "minimal",
    cli_flags: {include_default_customizations: true, disable_telemetry: true},
    minimal_tools_status: "flag_available_unverified",
    tool_bridge_transport: "unix_socket",
    process_supervision: "posix_process_group",
    proxy_mode: "system",
    cli_update_status: "not_checked",
  };
  data.environment.discovery_hint = "当前默认搜索范围：PATH。";
  const ui = await boot(data);
  assert.deepEqual(ui.get("facts").children.map(n => n.textContent), ["CLI 1.5.2", "3 个可用模型"]);
  assert.equal(ui.get("check-hint").textContent, "");
  assert.equal(ui.get("login-badge").textContent, "已连接");
  assert.equal(ui.get("login-hint").textContent, "");
  assert.equal(ui.get("env-diagnostics").open, false);
  const diagTexts = collectNodes(ui.get("env-diag-list")).map(n => n.textContent).filter(Boolean);
  assert.ok(diagTexts.some(t => t.includes("Unix Socket")));
  assert.ok(diagTexts.some(t => t.includes("独立进程组")));
  assert.ok(diagTexts.some(t => t.includes("组件模式")));
  assert.ok(diagTexts.some(t => t.includes("CLI 更新策略")));
});

test("save feedback avoids redundant placeholder text and help popovers toggle in fallback mode", async () => {
  const saved = base();
  saved.providers = [{id: "inst-1", model: "gemini-test", tools_mode: "chat", enabled: true}];
  saved.result = {provider_id: "inst-1", default_selected: false};
  const ui = await boot(saved);
  assert.equal(ui.get("finish-badge").textContent, "已保存");
  assert.equal(ui.get("finish-hint").textContent, "");

  const withDefault = {...saved, result: {provider_id: "inst-1", default_selected: true}};
  ui.get("use-default").checked = true;
  ui.api.render(withDefault);
  assert.equal(ui.get("use-default").checked, false);
  ui.get("use-default").checked = true;
  ui.api.render(structuredClone(withDefault));
  assert.equal(ui.get("use-default").checked, true);
  assert.match(ui.get("finish-hint").textContent, /全局默认/);

  assert.equal(ui.get("monitor-help").hidden, true);
  ui.get("monitor-help-btn").click();
  assert.equal(ui.get("monitor-help").hidden, false);
  assert.equal(ui.get("monitor-help-btn").getAttribute("aria-expanded"), "true");
  ui.handlers.keydown({key: "Escape"});
  assert.equal(ui.get("monitor-help").hidden, true);
  assert.equal(ui.get("monitor-help-btn").getAttribute("aria-expanded"), "false");
});

test("monitor renders 3-layer hierarchy, SVG charts, 429 candidates, and keeps overview free of internal field names", async () => {
  const ui = await boot();
  const payload = monitorPayload({
    context_used: 80000, context_window_size: 200000,
    observed_at: "2026-11-17T00:05:00+00:00",
    source: "acp_usage_update", scope: "latest_live_session", model: "gemini-test",
  });
  payload.instances[0].summary = {
    total_turns: 10, request_sent_turns: 8, success_turns: 7, failed_turns: 3,
    reuse_rate: 0.75, cache_token_ratio: 0.6, unavailable_usage_turns: 1,
    reported_usage_turns: 7, missing_cache_field_turns: 1,
    total_input_other: 4000, total_input_cached: 6000, total_output: 2000,
    rebuild_reasons: [["cold_start", 2], ["system_changed", 1]],
  };
  payload.instances[0].recent_turns = [
    {
      recorded_at: "2026-11-17T00:04:00+00:00", model: "gemini-test", mode: "chat",
      status: "error", error_code: "RATE_LIMIT", elapsed_ms: 1450,
      input_other: null, input_cached: null, output: null, usage_status: "unavailable",
      cache_fields_present: {cachedReadTokens: false, cachedWriteTokens: false},
      context_used: null, context_window_size: null, observed_at: null,
      reused: false, rebuild_reasons: ["cold_start"],
      prompt_bytes: 512, replay_bytes: 0, omitted_messages: 0,
      content_429_candidate: {system_hash8: "a1b2c3d4", system_bytes: 2048, has_output: false, attempt: 2},
    },
    {
      recorded_at: "2026-11-17T00:02:00+00:00", model: "gemini-test", mode: "chat",
      status: "ok", error_code: null, elapsed_ms: 620,
      input_other: 1000, input_cached: 3000, output: 500, usage_status: "reported",
      cache_fields_present: {cachedReadTokens: true, cachedWriteTokens: false},
      context_used: 80000, context_window_size: 200000, observed_at: "2026-11-17T00:02:00+00:00",
      reused: true, rebuild_reasons: [],
      prompt_bytes: 256, replay_bytes: 0, omitted_messages: 0,
      content_429_candidate: null,
    },
  ];
  ui.api.renderMonitor(payload);

  const card = ui.get("monitor-instances").children[0];
  const overview = card.children.find(n => n.className === "monitor-overview");
  const layer2 = card.children.find(n => n.classList.contains("monitor-details"));
  const layer3 = card.children.find(n => n.classList.contains("monitor-diag"));
  assert.ok(overview);
  assert.ok(layer2 && layer2.open === false);
  assert.ok(layer3 && layer3.open === false);

  const overviewTexts = collectNodes(overview).map(n => n.textContent).filter(Boolean);
  assert.ok(overviewTexts.some(t => t === "实时会话池"));
  assert.ok(overviewTexts.some(t => t.includes("近 24h")));
  assert.ok(overviewTexts.some(t => t === "最近采样"));
  assert.ok(overviewTexts.some(t => t === "7 / 3"));
  assert.ok(overviewTexts.some(t => t.includes("RATE_LIMIT")));
  for (const forbidden of ["tools_mode", "effective_mode", "active_sessions", "total_input_other", "cachedReadTokens", "content_429_candidate"]) {
    assert.equal(overviewTexts.some(t => t.includes(forbidden)), false, `Overview exposed internal field ${forbidden}`);
  }

  const layer2Nodes = collectNodes(layer2);
  const layer2Texts = layer2Nodes.map(n => n.textContent).filter(Boolean);
  assert.ok(layer2Nodes.some(n => n.tag === "svg" && n.classList.contains("token-stack-svg")));
  assert.ok(layer2Nodes.some(n => n.tag === "svg" && n.classList.contains("latency-svg")));
  assert.equal(layer2Nodes.filter(n => n.tag === "svg" && n.classList.contains("gauge-svg")).length, 2);
  assert.ok(layer2Texts.includes("75.0%"));
  assert.ok(layer2Texts.includes("60.0%"));
  assert.ok(layer2Texts.some(t => t.includes("冷启动 × 2")));

  const layer3Texts = collectNodes(layer3).map(n => n.textContent).filter(Boolean);
  assert.ok(layer3Texts.some(t => t.includes("a1b2c3d4") && t.includes("2,048B")));
  assert.ok(layer3Texts.some(t => t.includes("reported=7")));

  const metric = collectNodes(overview).find(n => n.className === "monitor-metric");
  assert.deepEqual(metric.children.map(n => n.tag), ["dt", "dd"]);
  assert.ok(metric.children[1].children.some(n => n.className === "monitor-metric-sub"));

  // Identical cards keep their DOM even when the snapshot timestamp advances.
  layer2.open = true;
  layer2.toggle();
  ui.api.renderMonitor({...payload, sampled_at: "2026-11-17T00:07:00+00:00"});
  assert.equal(ui.get("monitor-instances").children[0], card);
  // A changed snapshot rebuilds cards while preserving only this instance's sections.
  payload.instances.push(structuredClone(payload.instances[0]));
  payload.instances[0].recent_turns[0].prompt_bytes = null;
  ui.api.renderMonitor(payload);
  const [updated, duplicate] = ui.get("monitor-instances").children;
  assert.notEqual(updated, card);
  assert.equal(duplicate.children.find(n => n.classList.contains("monitor-details")).open, false);
  assert.ok(collectNodes(updated).some(n => n.textContent.includes("prompt=未知 replay=0B")));
  const refreshedLayer2 = ui.get("monitor-instances").children[0].children.find(n => n.classList.contains("monitor-details"));
  assert.equal(refreshedLayer2.open, true);
});

test("monitor distinguishes genuine 0.0% reuse rate from missing/null data and renders inactive instances cleanly", async () => {
  const ui = await boot();
  const payload = monitorPayload({
    context_used: null, context_window_size: null, observed_at: null,
    source: "not_reported", scope: "latest_live_session", model: "gemini-test",
  });
  payload.instances[0].summary = {
    total_turns: 3, request_sent_turns: 3, success_turns: 3, failed_turns: 0,
    reuse_rate: 0, cache_token_ratio: null, unavailable_usage_turns: 0,
    reported_usage_turns: 2, missing_cache_field_turns: 2,
    total_input_other: 1200, total_input_cached: null, total_output: 300,
    rebuild_reasons: [],
  };
  payload.instances.push({
    provider_id: "agy-idle", model: "gemini-idle", enabled: false,
    tools_mode: "inherit", effective_mode: "chat", tools_enabled: false,
    runtime_state: "configured", pool: null, summary: null,
    latest_context: {context_used: null, context_window_size: null, observed_at: null, source: "not_reported", scope: "latest_live_session", model: "gemini-idle"},
    recent_turns: [],
  });
  ui.api.renderMonitor(payload);

  const [activeCard, idleCard] = ui.get("monitor-instances").children;
  const activeLayer2Texts = collectNodes(activeCard.children.find(n => n.classList.contains("monitor-details"))).map(n => n.textContent).filter(Boolean);
  assert.ok(activeLayer2Texts.includes("0.0%"));
  assert.ok(activeLayer2Texts.includes("未报告缓存字段"));
  assert.ok(activeLayer2Texts.some(t => t.includes("缓存读取 上游未报告")));

  const idleTexts = collectNodes(idleCard).map(n => n.textContent).filter(Boolean);
  assert.ok(idleTexts.some(t => t.includes("已配置未初始化")));
});

test("stylesheet and HTML enforce mobile overflow safety and system dark theme support", () => {
  const css = readFileSync(new URL("../views/setup/style.css", import.meta.url), "utf8");
  assert.match(html, /<meta\s+name="color-scheme"\s+content="light dark">/);
  assert.match(css, /@media\s*\(prefers-color-scheme:\s*dark\)/);
  assert.match(css, /minmax\(min\(100%,\s*360px\),\s*1fr\)/);
  assert.match(css, /overflow-wrap:\s*anywhere/);
});
