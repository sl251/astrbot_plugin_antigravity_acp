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
  append(...children) {
    this.children.push(...children.map(c => (typeof c === "string" || typeof c === "number")
      ? {tag: "#text", textContent: String(c), children: [], attributes: {}}
      : c));
  }
  appendChild(child) {child.parent = this; this.children.push(child); return child;}
  remove() {if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this);}
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") {
      this.className = String(value);
      this._classes = new Set(this.className.split(/\s+/).filter(Boolean));
    }
  }
  getAttribute(name) {return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null;}
  removeAttribute(name) {delete this.attributes[name];}
  addEventListener(name, callback) {this[name] = callback;}
  focus() {this.focused = true;}
  select() {this.selected = true;}
}

async function boot(initial = base(), hooks = {}) {
  const elements = new Map([...html.matchAll(/<([\w-]+)([^>]*)\bid="([^"]+)"([^>]*)>/g)]
    .map(([, tag, pre, id, post]) => {
      const el = new Element(tag);
      if (/\bhidden\b/.test(`${pre} ${post}`)) el.hidden = true;
      return [id, el];
    }));
  const handlers = {};
  const requests = [];
  const getRequests = [];
  const bridge = {
    ready: async () => ({}),
    apiGet: async (endpoint) => {
      getRequests.push(endpoint);
      if (hooks.apiGet) return hooks.apiGet(endpoint);
      return initial;
    },
    apiPost: async (endpoint, body) => {
      requests.push({endpoint, body});
      if (hooks.apiPost) return hooks.apiPost(endpoint, body);
      return initial;
    },
  };
  const context = vm.createContext({
    window: {AstrBotPluginView: bridge, addEventListener: (name, cb) => {handlers[name] = cb;}},
    document: {body: new Element("body"), execCommand: () => false, documentElement: new Element("html"), getElementById: id => {
      assert.ok(elements.has(id), `Missing element ${id}`);
      return elements.get(id);
    }, createElement: tag => new Element(tag), createElementNS: (_ns, tag) => new Element(tag),
    createTextNode: text => ({tag: "#text", textContent: String(text), children: [], attributes: {}}),
    hidden: false,
    addEventListener: (name, cb) => {handlers[name] = cb;}},
    Option: class {constructor(name, value) {this.text = name; this.value = value;}},
    navigator: {clipboard: {writeText: async () => {throw new Error("iframe clipboard unavailable");}}},
    setTimeout: () => 1, clearTimeout: () => {},
  });
  const api = await vm.runInContext(`(async () => {${source}\n; return {render, renderMonitor, renderQuota, renderDaily, renderTurnHistory, toggleTurnHistory, loadTurnHistory, refreshMonitor, refreshQuotaCache, refreshQuotaManual, loadDailyUsage, saveToolPolicy, parseBridgeError, isAuthDeniedError, formatPanelError, action, poll};})()`, context);
  return {api, bridge, requests, getRequests, handlers, doc: context.document, get: id => elements.get(id)};
}

test("theme toggle switches light and dark theme on documentElement", async () => {
  const ui = await boot();
  ui.get("theme-toggle").onclick();
  assert.equal(ui.doc.documentElement.dataset.themeMode, "dark");
  assert.equal(ui.get("theme-toggle").textContent, "主题：深色");
  ui.get("theme-toggle").onclick();
  assert.equal(ui.get("theme-toggle").textContent, "主题：自动");
  ui.get("theme-toggle").onclick();
  assert.equal(ui.doc.documentElement.dataset.themeMode, "light");
  assert.equal(ui.get("theme-toggle").textContent, "主题：浅色");
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
  ui.bridge.apiGet = async endpoint => endpoint === "setup/status"
    ? new Promise(resolve => {release = resolve;})
    : {schema_version: 2, instances: []};
  const poll = ui.api.poll();
  const saved = base(); saved.result = {provider_id: "saved-id", default_selected: false};
  saved.providers = [{id: "saved-id", model: "gemini-test", enabled: true}];
  ui.bridge.apiPost = async () => saved;
  await ui.api.action("apply");
  release(base()); await poll;
  assert.equal(ui.get("finish-badge").textContent, "已保存");
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

test("copy failure selects text and offers keyboard fallback", async () => {
  const ui = await boot();
  await ui.get("copy-install").onclick();
  assert.equal(ui.get("install-url").selected, true);
  assert.match(ui.get("copy-status").textContent, /Ctrl\+C/);
});

test("page exit clears sensitive fields and stops polling", async () => {
  const ui = await boot();
  ui.get("auth-code").value = "fake-code";
  ui.get("auth-url").value = "fake-url";
  ui.handlers.pagehide();
  assert.equal(ui.get("auth-code").value, "");
  assert.equal(ui.get("auth-url").value, "");
  ui.bridge.apiGet = () => {throw new Error("disposed page must not poll");};
  await ui.api.poll();
});

test("model change resets thought effort from cache without immediate probe and refresh checks catalog", async () => {
  const data = base();
  data.report.models = [{id: "gemini-test", name: "Gemini Test"}, {id: "gemini-other", name: "Gemini Other"}];
  data.efforts_by_model = {"gemini-test": ["low", "high"], "gemini-other": ["medium"]};
  const ui = await boot(data);
  ui.get("open-add-model").onclick();
  ui.get("effort").value = "high";
  ui.get("model").value = "gemini-other";
  ui.get("model").onchange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests.length, 0);
  assert.equal(ui.get("effort").value, "default");
  assert.deepEqual(ui.get("effort").children.map(option => option.value), ["default", "medium"]);

  ui.get("refresh-catalog").onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests[0].endpoint, "setup/check");
});

test("model save sends selected tools_mode with chat default and create_only flag", async () => {
  const ui = await boot();
  assert.equal(ui.get("tools-mode").value, "chat");
  ui.get("model-form").onsubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests[0].endpoint, "setup/apply");
  assert.equal(ui.requests[0].body.tools_mode, "chat");
  assert.equal(ui.requests[0].body.create_only, true);

  ui.get("tools-mode").value = "tools";
  ui.get("model-form").onsubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests[1].body.tools_mode, "tools");
  assert.equal(ui.requests[1].body.create_only, true);
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

test("monitor removes redundant context usage, detailed records, diagnostics, and persistent failures panels", async () => {
  assert.doesNotMatch(html, /persistent-failures|failure-storage|持久失败诊断/);
  const ui = await boot();
  ui.api.renderMonitor(monitorPayload({
    context_used: 50000, context_window_size: 200000,
    observed_at: "2026-11-17T00:00:00+00:00",
    source: "acp_usage_update", scope: "latest_live_session", model: "gemini-test",
  }));
  const card = ui.get("monitor-instances").children[0];
  const nodes = collectNodes(card);
  const texts = nodes.map(n => n.textContent).filter(Boolean);
  assert.equal(nodes.some(n => n.classList.contains("monitor-context")), false);
  assert.equal(nodes.some(n => n.classList.contains("monitor-details")), false);
  assert.equal(nodes.some(n => n.classList.contains("monitor-diag")), false);
  assert.equal(texts.some(t => t.includes("上下文占用") || t.includes("详细记录") || t === "诊断"), false);
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
  assert.equal(diagTexts.some(t => t.includes("CLI 更新策略")), false);
});

test("save feedback avoids redundant placeholder text and the page has no help popovers", async () => {
  const saved = base();
  saved.providers = [{id: "inst-1", model: "gemini-test", tools_mode: "chat", enabled: true}];
  saved.result = {provider_id: "inst-1", default_selected: false};
  const ui = await boot(saved);
  assert.equal(ui.get("finish-badge").textContent, "已保存");
  assert.equal(ui.get("finish-hint").textContent, "");

  const withDefault = {...saved, result: {provider_id: "inst-1", default_selected: true}};
  ui.api.render(withDefault);
  assert.match(ui.get("finish-hint").textContent, /全局默认/);

  assert.doesNotMatch(html, /help-popover|popovertarget|scope-pill/);
});

test("streamlined monitor keeps core KPIs and SVG charts, hides internal fields, and auto-clears fault on recovery", async () => {
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
  assert.ok(overview);
  assert.equal(card.children.some(n => n.classList.contains("monitor-details")), false);
  assert.equal(card.children.some(n => n.classList.contains("monitor-diag")), false);

  const overviewTexts = collectNodes(overview).map(n => n.textContent).filter(Boolean);
  assert.ok(overviewTexts.some(t => t.includes("近 24h")));
  assert.ok(overviewTexts.some(t => t === "7 / 3"));
  assert.ok(overviewTexts.some(t => t.includes("当前异常") && t.includes("请求受限")));
  for (const forbidden of ["tools_mode", "effective_mode", "active_sessions", "total_input_other", "cachedReadTokens", "content_429_candidate", "a1b2c3d4"]) {
    assert.equal(overviewTexts.some(t => t.includes(forbidden)), false, `Overview exposed internal field ${forbidden}`);
  }

  const overviewNodes = collectNodes(overview);
  assert.ok(overviewNodes.some(n => n.tag === "svg" && n.classList.contains("token-stack-svg")));
  assert.ok(overviewNodes.some(n => n.tag === "svg" && n.classList.contains("latency-svg")));
  assert.ok(overviewNodes.some(n => n.tag === "svg" && n.classList.contains("donut-svg")));
  assert.ok(overviewNodes.some(n => n.tag === "svg" && n.classList.contains("seg-svg")));
  assert.equal(overviewNodes.filter(n => n.tag === "svg" && n.classList.contains("gauge-svg")).length, 2);
  assert.ok(overviewTexts.includes("75.0%"));
  assert.ok(overviewTexts.includes("60.0%"));

  const reloaded = structuredClone(payload);
  reloaded.instances[0].history_storage = "memory_only";
  reloaded.instances[0].recent_turns.forEach(turn => { turn.historical = true; });
  ui.api.renderMonitor(reloaded);
  const reloadedNodes = collectNodes(ui.get("monitor-instances").children[0]);
  assert.ok(reloadedNodes.some(n => n.classList?.contains("latency-svg")));
  assert.ok(reloadedNodes.some(n => n.textContent === "仅内存统计"));
  assert.ok(!reloadedNodes.some(n => n.className === "fault-banner"));

  // 新一轮请求成功后，当前异常提示自动消失（历史错误不常驻）
  const recovered = structuredClone(payload);
  recovered.instances[0].recent_turns.unshift({
    recorded_at: "2026-11-17T00:06:00+00:00", model: "gemini-test", mode: "chat",
    status: "ok", error_code: null, elapsed_ms: 480,
    input_other: 800, input_cached: 2000, output: 400, usage_status: "reported",
  });
  ui.api.renderMonitor(recovered);
  const recoveredNodes = collectNodes(ui.get("monitor-instances").children[0]);
  assert.equal(recoveredNodes.some(n => n.className === "fault-banner"), false);
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
  const activeOverviewTexts = collectNodes(activeCard.children.find(n => n.className === "monitor-overview")).map(n => n.textContent).filter(Boolean);
  assert.ok(activeOverviewTexts.includes("0.0%"));
  assert.ok(activeOverviewTexts.includes("未报告缓存字段"));
  assert.ok(activeOverviewTexts.some(t => t.includes("缓存读取 上游未报告")));
  // 无重建原因且无历史记录时不渲染空的详细记录层
  assert.equal(activeCard.children.some(n => n.classList.contains("monitor-details")), false);

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

test("setup progress stepper reflects environment, login, model and enabled instance", async () => {
  const none = await boot(base());
  const items = () => none.get("setup-progress").children;
  assert.deepEqual(items().map(li => li.className), ["is-done", "is-done", "is-current", ""]);
  assert.equal(items()[0].children[0].textContent, "✓");
  assert.equal(items()[2].children[0].textContent, "3");

  const done = base();
  done.providers = [{id: "inst-1", model: "gemini-test", tools_mode: "chat", enabled: true}];
  none.api.render(done);
  assert.deepEqual(items().map(li => li.className), ["is-done", "is-done", "is-done", "is-done"]);

  const blocked = base();
  blocked.report = {status: "blocked", error_code: "NOT_INSTALLED"};
  none.api.render(blocked);
  assert.deepEqual(items().map(li => li.className), ["is-current", "", "", ""]);
});

test("app.js only references CSS variables defined in style.css", () => {
  const css = readFileSync(new URL("../views/setup/style.css", import.meta.url), "utf8");
  const defined = new Set([...css.matchAll(/(--[\w-]+)\s*:/g)].map(([, name]) => name));
  const referenced = new Set([...source.matchAll(/var\((--[\w-]+)/g)].map(([, name]) => name));
  const missing = [...referenced].filter(name => !defined.has(name));
  assert.deepEqual(missing, []);
});

test("connection indicator tracks ok, busy and error states and cancelled turns use warn tone", async () => {
  const ui = await boot(base());
  assert.equal(ui.get("connection").dataset.state, "ok");

  const busy = base();
  busy.busy = true;
  ui.api.render(busy);
  assert.equal(ui.get("connection").dataset.state, "busy");

  ui.bridge.apiGet = async () => {throw new Error("network down");};
  await ui.api.poll();
  assert.equal(ui.get("connection").dataset.state, "error");

  const payload = monitorPayload({
    context_used: null, context_window_size: null, observed_at: null,
    source: "not_reported", scope: "latest_live_session", model: "gemini-test",
  });
  payload.instances[0].recent_turns = [{
    recorded_at: "2026-11-17T00:04:00+00:00", model: "gemini-test", mode: "chat",
    status: "cancelled", error_code: "CANCELLED", elapsed_ms: 350,
    input_other: null, input_cached: null, output: null, usage_status: "unavailable",
    cache_fields_present: {cachedReadTokens: false, cachedWriteTokens: false},
    context_used: null, context_window_size: null, observed_at: null,
    reused: true, rebuild_reasons: [],
    prompt_bytes: 120, replay_bytes: 0, omitted_messages: 0,
    content_429_candidate: null,
  }];
  ui.api.renderMonitor(payload);
  const nodes = collectNodes(ui.get("monitor-instances"));
  assert.equal(nodes.some(n => n.className === "fault-banner"), false);
  assert.ok(nodes.some(n => n.tag === "rect" && n.attributes.fill === "var(--warn)"));
});


test("monitor v2 keeps routing summary while omitting removed persistent diagnostics UI", async () => {
  const ui = await boot();
  const payload = monitorPayload({});
  payload.schema_version = 2;
  payload.routing = {configured_primary_id: "primary", resolved_primary_id: "resolved", last_observed_provider_id: "observed", explicit_fallback_ids: []};
  payload.failure_events = [{error_code: "TIMEOUT", provider_tag: "tag", elapsed_ms: 50, recorded_at: "2026-11-24T00:00:00Z"}];
  payload.failure_retention = {storage_state: "available"};
  ui.api.renderMonitor(payload);
  assert.equal(ui.get("routing-summary").children[0].children[1].textContent, "primary");
  assert.equal(ui.get("routing-summary").children[1].children[1].textContent, "resolved");
  assert.equal(ui.get("persistent-failures"), undefined);
});
test("HTTP sandbox can copy through synchronous fallback", async () => {
  const ui = await boot();
  let copied = false;
  ui.doc.execCommand = name => {copied = name === "copy"; return copied;};
  await ui.get("copy-install").onclick();
  assert.equal(copied, true);
  assert.equal(ui.get("copy-status").textContent, "已复制");
});

test("monitor is the default tab and tab navigation switches panels and active button state", async () => {
  const ui = await boot();
  assert.equal(ui.get("tab-monitor").hidden, false);
  assert.equal(ui.get("tab-models").hidden, true);
  assert.equal(ui.get("tab-setup").hidden, true);
  assert.equal(ui.get("tab-btn-monitor").classList.contains("is-active"), true);

  ui.get("goto-models").onclick();
  assert.equal(ui.get("tab-monitor").hidden, true);
  assert.equal(ui.get("tab-models").hidden, false);
  assert.equal(ui.get("tab-btn-models").classList.contains("is-active"), true);

  ui.get("tab-btn-setup").onclick();
  assert.equal(ui.get("tab-models").hidden, true);
  assert.equal(ui.get("tab-setup").hidden, false);
  assert.equal(ui.get("tab-btn-setup").classList.contains("is-active"), true);
});

test("route-alert, drift-alert, routing-strip, and global effort label reflect live routing state", async () => {
  const data = base();
  data.global_effort = "high";
  data.providers = [{
    id: "acp-1", model: "gemini-test", thought_level: "default",
    tools_mode: "inherit", enabled: true, is_default: false, config_drifted: true,
  }];
  data.routing = {
    configured_primary_id: "acp-gone",
    resolved_primary_id: "acp-1",
    resolution_reason: "implicit_first_provider",
    explicit_fallback_ids: ["fb-1"],
    non_default_profile_overrides: 2,
  };
  const ui = await boot(data);
  assert.equal(ui.get("route-alert").hidden, false);
  assert.match(ui.get("route-alert").children[0].textContent, /acp-gone/);
  assert.equal(ui.get("drift-alert").hidden, false);
  assert.match(ui.get("routing-strip-meta").textContent, /备用 1 项/);
  assert.match(ui.get("routing-strip-meta").textContent, /2 个独立 Profile 覆盖/);
  assert.match(ui.get("global-policy-summary").textContent, /思考 高/);

  const cardText = collectNodes(ui.get("configured")).map(n => n.textContent).filter(Boolean);
  assert.ok(cardText.some(t => t.includes("跟随全局（高）")));
  assert.ok(cardText.includes("需同步"));

  // 点击顶部异常恢复按钮应将 acp-1 设为默认；待请求完成后点击漂移同步按钮触发批量修复
  ui.get("route-alert").children[1].click();
  await new Promise(resolve => setImmediate(resolve));
  ui.get("drift-alert").children[1].click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests[0].endpoint, "setup/provider-default");
  assert.deepEqual({...ui.requests[0].body}, {provider_id: "acp-1", confirm: true, csrf: "test-nonce"});
  assert.equal(ui.requests[1].endpoint, "setup/provider-repair");
  assert.deepEqual({...ui.requests[1].body}, {all: true, csrf: "test-nonce"});
});

test("add modal blocks duplicate config, protects fields from background polls, and caches catalog during busy probe", async () => {
  const data = base();
  data.report.models = [{id: "gemini-test", name: "Gemini Test"}, {id: "gemini-other", name: "Gemini Other"}];
  data.efforts_by_model = {"gemini-test": ["low", "high"], "gemini-other": ["medium"]};
  data.providers = [{
    id: "acp-1", model: "gemini-test", thought_level: "default",
    tools_mode: "chat", enabled: true, is_default: true, config_drifted: false,
  }];
  const ui = await boot(data);

  // 打开添加弹窗时默认参数恰好与 acp-1 重复，提交应被禁用
  ui.get("open-add-model").onclick();
  assert.equal(ui.get("modal-form").hidden, false);
  assert.equal(ui.get("form-dup-warn").hidden, false);
  assert.equal(ui.get("apply").disabled, true);

  // 修改参数并在弹窗开启期间模拟后台轮询写入其他状态，当前编辑项不应被覆盖
  ui.get("model").value = "gemini-other";
  ui.get("model").onchange();
  ui.get("effort").value = "medium";
  ui.get("effort").onchange();
  ui.get("tools-mode").value = "tools";
  ui.get("tools-mode").onchange();
  assert.equal(ui.get("form-dup-warn").hidden, true);
  assert.equal(ui.get("apply").disabled, false);

  const busyPoll = base();
  busyPoll.busy = true;
  busyPoll.report = {status: "checking", stage: "model_catalog", models: []};
  busyPoll.cached_models = data.report.models;
  ui.api.render(busyPoll);
  assert.equal(ui.get("model").value, "gemini-other");
  assert.equal(ui.get("effort").value, "medium");
  assert.equal(ui.get("tools-mode").value, "tools");
});

test("edit modal preserves provider_id, warns on model swap, and submits setup/provider-update", async () => {
  const data = base();
  data.report.models = [{id: "gemini-test", name: "Gemini Test"}, {id: "gemini-other", name: "Gemini Other"}];
  data.efforts_by_model = {"gemini-test": ["low", "high"], "gemini-other": ["medium"]};
  data.providers = [{
    id: "acp-1", model: "gemini-test", thought_level: "low",
    tools_mode: "tools", enabled: true, is_default: true, config_drifted: false,
  }];
  const ui = await boot(data);
  const card = ui.get("configured").children[0];
  const editBtn = collectNodes(card).find(n => n.tag === "button" && n.textContent === "编辑");
  editBtn.click();

  assert.equal(ui.get("modal-form").hidden, false);
  assert.equal(ui.get("form-id-row").hidden, false);
  assert.equal(ui.get("form-id-val").textContent, "acp-1");
  assert.equal(ui.get("use-default-row").hidden, true);
  assert.equal(ui.get("model").value, "gemini-test");
  assert.equal(ui.get("effort").value, "low");
  assert.equal(ui.get("tools-mode").value, "tools");
  assert.equal(ui.get("form-model-warn").hidden, true);

  ui.get("model").value = "gemini-other";
  ui.get("model").onchange();
  assert.equal(ui.get("form-model-warn").hidden, false);

  ui.get("model-form").onsubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.get("modal-form").hidden, true);
  assert.equal(ui.requests[0].endpoint, "setup/provider-update");
  assert.deepEqual({...ui.requests[0].body}, {
    provider_id: "acp-1",
    model: "gemini-other",
    effort: "default",
    tools_mode: "tools",
    csrf: "test-nonce",
  });
});

test("controlled offboarding fetches provider-impact and sends migrate_default_to with force_confirm", async () => {
  const data = base();
  data.providers = [
    {id: "acp-1", model: "gemini-test", thought_level: "default", tools_mode: "chat", enabled: true, is_default: true, config_drifted: false},
    {id: "acp-2", model: "gemini-flash", thought_level: "low", tools_mode: "chat", enabled: true, is_default: false, config_drifted: false},
  ];
  const ui = await boot(data);
  ui.bridge.apiPost = async (endpoint, body) => {
    ui.requests.push({endpoint, body});
    if (endpoint === "setup/provider-impact") {
      return {
        impact: {
          references: {primary: 1, fallback: 0, auxiliary: 1, observed_session_overrides: 2},
          requires_migration: true,
          replacement_candidates: [{id: "acp-2", model: "gemini-flash", thought_level: "low", is_acp: true}],
        },
      };
    }
    return data;
  };

  const firstCard = ui.get("configured").children[0];
  const delBtn = collectNodes(firstCard).find(n => n.tag === "button" && n.textContent === "删除");
  delBtn.click();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(ui.requests[0].endpoint, "setup/provider-impact");
  assert.equal(ui.get("modal-impact").hidden, false);
  assert.equal(ui.get("impact-mig-wrap").hidden, false);
  assert.deepEqual(ui.get("impact-mig-select").children.map(o => o.value), ["acp-2", "__NONE__"]);

  ui.get("impact-confirm").onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.get("modal-impact").hidden, true);
  assert.equal(ui.requests[1].endpoint, "setup/provider-delete");
  assert.deepEqual({...ui.requests[1].body}, {
    provider_id: "acp-1",
    confirm: true,
    force_confirm: true,
    migrate_default_to: "acp-2",
    csrf: "test-nonce",
  });

  // 停用并选择置空全局默认时，migrate_default_to 应传空字符串
  const currentCard = ui.get("configured").children[0];
  const disableBtn = collectNodes(currentCard).find(n => n.tag === "button" && n.textContent === "停用");
  disableBtn.click();
  await new Promise(resolve => setImmediate(resolve));
  ui.get("impact-mig-select").value = "__NONE__";
  ui.get("impact-confirm").onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.requests[3].endpoint, "setup/provider-toggle");
  assert.deepEqual({...ui.requests[3].body}, {
    provider_id: "acp-1",
    enabled: false,
    confirm: true,
    force_confirm: true,
    migrate_default_to: "",
    csrf: "test-nonce",
  });
});

test("monitor cards display provider metadata badges when state.providers matches", async () => {
  const data = base();
  data.global_effort = "medium";
  data.providers = [{
    id: "agy-main", model: "gemini-test", thought_level: "default",
    tools_mode: "chat", enabled: true, is_default: true, config_drifted: true,
  }];
  const ui = await boot(data);
  ui.api.renderMonitor(monitorPayload({
    context_used: null, context_window_size: null, observed_at: null,
    source: "not_reported", scope: "latest_live_session", model: "gemini-test",
  }));
  const card = ui.get("monitor-instances").children[0];
  const texts = collectNodes(card).map(n => n.textContent).filter(Boolean);
  assert.ok(texts.includes("默认"));
  assert.ok(texts.includes("跟随全局（中）"));
  assert.ok(texts.includes("需同步"));
});

test("command_scope_warning shows warning alert when non-empty and hides when null or empty", async () => {
  const data = base();
  data.command_scope_warning = "当前默认配置排除了本插件，聊天指令 /agy 不会生效，请检查插件启用范围。";
  const ui = await boot(data);
  assert.equal(ui.get("command-scope-alert").hidden, false);
  assert.match(ui.get("command-scope-alert").textContent, /默认配置排除了本插件/);

  ui.api.render({...data, command_scope_warning: null});
  assert.equal(ui.get("command-scope-alert").hidden, true);

  ui.api.render({...data, command_scope_warning: "   "});
  assert.equal(ui.get("command-scope-alert").hidden, true);
});

test("tool policy renders dynamic backend tools, hides switches for unverified/unsupported tools, preserves draft on poll and save error, and sends csrf on save", async () => {
  const initialPolicy = {
    enable_tools: true,
    reload_required: false,
    tools: [
      {id: "astrbot_mcp", source: "mcp", purpose: "宿主提供的工具", risk: "host", capability: "supported", choices: [], policy: "allow_once", effective: true},
      {id: "read_file", source: "builtin", purpose: "读取隔离工作区的普通文本文件", risk: "high", capability: "supported", choices: ["deny", "allow_once"], policy: "deny", effective: false},
      {id: "search_web", source: "builtin", purpose: "联网搜索", risk: "network", capability: "unsupported", choices: ["deny"], policy: "deny", effective: false},
      {id: "custom_future_tool", source: "builtin", purpose: "未来实验工具", risk: "high", capability: "unverified", choices: ["deny", "allow_once"], policy: "deny", effective: false},
    ],
  };
  const data = base();
  data.tool_policy = initialPolicy;
  const ui = await boot(data);

  // 切换到工具权限页签
  ui.get("tab-btn-tools").onclick();
  assert.equal(ui.get("tab-tools").hidden, false);
  assert.equal(ui.get("tool-policy-master-badge").textContent, "总闸已开启");
  assert.equal(ui.get("tool-policy-master-alert").hidden, true);
  assert.equal(ui.get("tool-policy-reload-alert").hidden, true);

  const listNodes = collectNodes(ui.get("tool-policy-list"));
  const rows = listNodes.filter(n => n.className === "tool-row");
  assert.equal(rows.length, 4);

  // 校验动态渲染后端工具名且 capability=unverified / unsupported / choices=[] 不显示可切换按钮
  const findRow = (id) => rows.find(r => collectNodes(r).some(n => n.tag === "code" && n.textContent === id));
  const mcpRow = findRow("astrbot_mcp");
  const readRow = findRow("read_file");
  const searchRow = findRow("search_web");
  const futureRow = findRow("custom_future_tool");
  assert.ok(mcpRow && readRow && searchRow && futureRow);

  assert.equal(collectNodes(mcpRow).some(n => n.tag === "button"), false);
  assert.equal(collectNodes(searchRow).some(n => n.tag === "button"), false);
  assert.equal(collectNodes(futureRow).some(n => n.tag === "button"), false);
  assert.ok(collectNodes(futureRow).some(n => n.textContent === "未验证"));
  assert.ok(collectNodes(searchRow).some(n => n.textContent === "暂不支持"));

  // read_file 有可切换的 拒绝 / 单次允许 按钮及高风险提示
  const readBtns = collectNodes(readRow).filter(n => n.tag === "button");
  assert.deepEqual(readBtns.map(b => b.textContent), ["拒绝", "单次允许"]);
  assert.ok(collectNodes(readRow).some(n => n.className === "tool-risk-note"));

  // 切换为单次允许，标记未保存且后台轮询不会覆盖草稿
  readBtns[1].click();
  assert.equal(ui.get("tool-policy-dirty-badge").hidden, false);
  assert.equal(ui.get("tool-policy-save").disabled, false);

  ui.api.render({...data, csrf: "fresh-nonce", tool_policy: initialPolicy});
  assert.equal(ui.get("tool-policy-dirty-badge").hidden, false);
  const refreshedReadBtns = collectNodes(
    collectNodes(ui.get("tool-policy-list")).find(r => r.className === "tool-row" && collectNodes(r).some(n => n.textContent === "read_file")),
  ).filter(n => n.tag === "button");
  assert.equal(refreshedReadBtns[1].getAttribute("aria-pressed"), "true");

  // 模拟保存失败：保留编辑内容并给出错误提示
  ui.bridge.apiPost = async (endpoint, body) => {
    ui.requests.push({endpoint, body});
    const err = new Error("持久化写入失败");
    err.status = 500;
    throw err;
  };
  await ui.api.saveToolPolicy();
  assert.equal(ui.requests[0].endpoint, "setup/tool-policy");
  assert.deepEqual({...ui.requests[0].body}, {csrf: "fresh-nonce", read_file: "allow_once"});
  assert.equal(ui.get("tool-policy-feedback").hidden, false);
  assert.match(ui.get("tool-policy-feedback").textContent, /持久化写入失败/);
  assert.equal(ui.get("tool-policy-dirty-badge").hidden, false);
  assert.equal(ui.get("tool-policy-save").disabled, false);

  // 模拟保存成功：直接返回同结构 tool_policy（无外层同名字段）并展示 reload_required 提示
  const savedPolicy = {
    enable_tools: true,
    reload_required: true,
    tools: initialPolicy.tools.map(t => t.id === "read_file" ? {...t, policy: "allow_once", effective: false} : t),
  };
  ui.bridge.apiPost = async (endpoint, body) => {
    ui.requests.push({endpoint, body});
    return savedPolicy;
  };
  await ui.api.saveToolPolicy();
  assert.equal(ui.get("tool-policy-dirty-badge").hidden, true);
  assert.equal(ui.get("tool-policy-reload-alert").hidden, false);
  assert.match(ui.get("tool-policy-reload-alert").textContent, /重载插件/);

  // 总闸关闭时明确提示不生效
  ui.api.render({...data, tool_policy: {...savedPolicy, enable_tools: false}});
  assert.equal(ui.get("tool-policy-master-badge").textContent, "总闸已关闭");
  assert.equal(ui.get("tool-policy-master-alert").hidden, false);
  const allTexts = collectNodes(ui.get("tool-policy-list")).map(n => n.textContent).filter(Boolean);
  assert.ok(allTexts.includes("未生效（总闸关闭）"));
});

test("tool policy page stays concise without redundant per-row descriptions or duplicate readonly labels", async () => {
  const data = base();
  data.tool_policy = {
    enable_tools: true,
    reload_required: false,
    tools: [
      {id: "astrbot_mcp", source: "mcp", purpose: "宿主提供的工具", risk: "host", capability: "supported", choices: [], policy: "allow_once", effective: true, reload_required: false},
      {id: "read_file", source: "builtin", purpose: "读取隔离工作区顶层文本文件", risk: "high", capability: "unverified", choices: ["deny"], policy: "deny", effective: false, reload_required: false},
      {id: "run_command", source: "builtin", purpose: "执行终端命令", risk: "high", capability: "unsupported", choices: ["deny"], policy: "deny", effective: false, reload_required: false},
    ],
  };
  const ui = await boot(data);
  ui.get("tab-btn-tools").onclick();

  assert.doesNotMatch(html, /单个模型是否启用工具在/);
  const listNodes = collectNodes(ui.get("tool-policy-list"));
  assert.equal(listNodes.some(n => n.tag === "p"), false);
  const allTexts = listNodes.map(n => n.textContent).filter(Boolean);
  assert.ok(allTexts.includes("宿主管控"));
  assert.ok(allTexts.includes("已拒绝"));
  for (const noisy of ["拒绝（只读）", "单次允许（宿主管控）", "未生效", "高风险", "当前版本或安全边界不支持此工具"]) {
    assert.equal(allTexts.some(t => t.includes(noisy)), false, `Unexpected noisy text: ${noisy}`);
  }
});

test("quota panel handles unqueried, loading, populated, null reset/fraction, stale, unsupported, and all 7 sanitized error codes", async () => {
  const unqueried = {
    schema_version: 1, source: "official_cli", login_env: "env-a", cli_version: "1.5.17",
    capability: "supported", fetched_at: null, stale: false, groups: [], error: null,
  };
  const ui = await boot(base(), {
    apiGet: async (ep) => ep === "monitor/quota" ? {...unqueried, fetched_at: "2026-11-26T05:00:00Z"} : base(),
  });
  await new Promise(resolve => setImmediate(resolve));
  ui.api.renderQuota(unqueried);
  assert.equal(ui.get("quota-badge").textContent, "未查询");
  assert.match(collectNodes(ui.get("quota-body")).map(n => n.textContent).join(" "), /尚未查询/);

  // 手动刷新：加载态与防重复请求 + 携带 csrf
  let releaseQuota;
  ui.bridge.apiPost = async (endpoint, body) => {
    ui.requests.push({endpoint, body});
    return new Promise(resolve => {releaseQuota = resolve;});
  };
  const firstRefresh = ui.api.refreshQuotaManual();
  await ui.api.refreshQuotaManual();
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.requests[0].endpoint, "setup/quota-refresh");
  assert.deepEqual({...ui.requests[0].body}, {csrf: "test-nonce"});
  assert.equal(ui.get("quota-refresh").disabled, true);
  assert.equal(ui.get("quota-badge").textContent, "查询中");

  const populated = {
    schema_version: 1, source: "official_cli", login_env: "env-a", cli_version: "1.5.17",
    capability: "supported", fetched_at: "2026-11-26T05:00:00+00:00", stale: false,
    groups: [
      {
        name: "Gemini 组",
        models_shared: ["Gemini 4 Neon", "Gemini 4 Carbon"],
        buckets: [
          {id: "g-week", name: "周额度", window: "weekly", remaining_fraction: 0.471, reset_time: "2099-11-30T20:11:31Z"},
          {id: "g-5h", name: "5小时额度", window: "5h", remaining_fraction: 0.85, reset_time: "1970-01-01T00:00:00Z"},
        ],
      },
      {
        name: "未知模型组",
        models_shared: [],
        buckets: [
          {id: "u-custom", name: "自定义池", window: "custom_window", remaining_fraction: null, reset_time: null},
        ],
      },
    ],
    error: null,
  };
  releaseQuota(populated);
  await firstRefresh;

  assert.equal(ui.get("quota-badge").textContent, "已更新");
  assert.equal(ui.get("quota-card").dataset.stale, "false");
  const qTexts = collectNodes(ui.get("quota-body")).map(n => n.textContent).filter(Boolean);
  assert.ok(qTexts.includes("Gemini 组"));
  assert.ok(qTexts.includes("Gemini 4 Neon"));
  assert.ok(qTexts.includes("47.1%"));
  assert.ok(qTexts.includes("85%"));
  assert.ok(qTexts.includes("周窗口"));
  assert.ok(qTexts.includes("5 小时窗口"));
  assert.ok(qTexts.includes("custom_window"));
  assert.ok(qTexts.includes("未知"));
  assert.equal(qTexts.filter(t => t === "重置时间：不适用").length, 2);
  assert.ok(qTexts.some(t => t.includes("后重置")));
  // 绝不把 login_env 或未上报分数显示成 0%
  assert.equal(ui.get("quota-meta").textContent.includes("env-a"), false);
  assert.equal(qTexts.includes("0%"), false);

  // stale=true 且带错误时保留旧组数据并标记过期
  ui.api.renderQuota({...populated, stale: true, error: {code: "TIMEOUT", message: "raw stderr secret"}});
  assert.equal(ui.get("quota-card").dataset.stale, "true");
  assert.equal(ui.get("quota-badge").textContent, "数据过期");
  assert.match(ui.get("quota-alert").textContent, /超时/);
  assert.equal(ui.get("quota-alert").textContent.includes("raw stderr secret"), false);
  assert.ok(collectNodes(ui.get("quota-body")).some(n => n.textContent === "Gemini 组"));

  // 所有 7 个契约 error.code 均有固定脱敏文案（不回显原始 message）
  for (const [code, expected] of [
    ["AUTH", /尚未登录|登录态/],
    ["TIMEOUT", /超时/],
    ["NOT_INSTALLED", /未检测到官方/],
    ["UNSUPPORTED", /暂不支持/],
    ["QUOTA_FORMAT", /未能解析/],
    ["QUOTA_FAILED", /查询失败/],
    ["ENV_CHANGED", /登录环境已变更/],
  ]) {
    ui.api.renderQuota({
      schema_version: 1, source: "official_cli", login_env: "env-b", cli_version: null,
      capability: code === "UNSUPPORTED" ? "unsupported" : "unknown",
      fetched_at: null, stale: false, groups: [],
      error: {code, message: `LEAKED_${code}_SECRET`},
    });
    assert.match(ui.get("quota-alert").textContent, expected, `Expected sanitized copy for ${code}`);
    assert.equal(ui.get("quota-alert").textContent.includes(`LEAKED_${code}_SECRET`), false);
  }

  // capability=unsupported 且 error=null
  ui.api.renderQuota({
    schema_version: 1, source: "official_cli", login_env: "env-b", cli_version: "1.2.0",
    capability: "unsupported", fetched_at: null, stale: false, groups: [], error: null,
  });
  assert.equal(ui.get("quota-badge").textContent, "暂不支持");

  // 登录进行中时清空无法确认归属的旧额度。
  ui.api.renderQuota(populated);
  ui.bridge.apiPost = async () => {
    const err = new Error("login in progress");
    err.status = 409;
    throw err;
  };
  await ui.api.refreshQuotaManual();
  assert.match(ui.get("quota-alert").textContent, /登录正在进行中/);
  assert.equal(ui.get("quota-badge").textContent, "查询失败");
  assert.ok(!collectNodes(ui.get("quota-body")).some(n => n.textContent === "Gemini 组"));
});

const quotaSample = (name = "Gemini", overrides = {}) => ({
  schema_version: 1, login_env: "env-a", capability: "supported",
  fetched_at: "2026-11-26T05:00:00Z", stale: false, error: null,
  groups: [{name, buckets: []}], ...overrides,
});
const flushRequests = () => new Promise(resolve => setImmediate(resolve));
const quotaReads = ui => ui.getRequests.filter(endpoint => endpoint === "monitor/quota").length;
const quotaHasGroup = (ui, name) => collectNodes(ui.get("quota-body")).some(n => n.textContent === name);

test("quota checks cache on entry and restoration, never on ordinary polling", async () => {
  let cache = quotaSample("", {groups: [], fetched_at: null});
  let release;
  const ui = await boot(base(), {
    apiGet: async endpoint => endpoint === "monitor/quota" ? cache : base(),
    apiPost: () => new Promise(resolve => { release = resolve; }),
  });
  assert.equal(quotaReads(ui), 1);
  assert.equal(ui.requests.length, 1);
  assert.deepEqual({...ui.requests[0].body}, {csrf: "test-nonce", force: false});
  assert.equal(ui.get("quota-badge").textContent, "查询中");
  await ui.api.refreshQuotaManual();
  assert.equal(ui.requests.length, 1);
  cache = quotaSample("首次采样");
  release(cache);
  await flushRequests();
  assert.ok(quotaHasGroup(ui, "首次采样"));

  await ui.api.poll();
  await ui.api.refreshMonitor();
  assert.equal(quotaReads(ui), 1);
  ui.get("tab-btn-setup").onclick();
  ui.get("tab-btn-monitor").onclick();
  await flushRequests();
  assert.equal(quotaReads(ui), 2);
  assert.equal(ui.requests.length, 1);

  cache = {...cache, stale: true};
  ui.doc.hidden = true;
  ui.handlers.visibilitychange();
  ui.doc.hidden = false;
  ui.handlers.visibilitychange();
  await flushRequests();
  assert.equal(quotaReads(ui), 3);
  assert.equal(ui.requests.length, 2);
  assert.equal(ui.get("quota-badge").textContent, "更新中");
  assert.ok(quotaHasGroup(ui, "首次采样"));
  release({...cache, error: {code: "TIMEOUT"}});
  await flushRequests();
  assert.equal(ui.get("quota-badge").textContent, "数据过期");
  assert.ok(quotaHasGroup(ui, "首次采样"));

  cache = quotaSample("恢复后的缓存");
  ui.handlers.pagehide({persisted: true});
  ui.handlers.pageshow({persisted: true});
  ui.handlers.visibilitychange();
  await flushRequests();
  assert.equal(quotaReads(ui), 4);
  assert.equal(ui.requests.length, 2);
  assert.ok(quotaHasGroup(ui, "恢复后的缓存"));
});

test("manual quota refresh supersedes an older GET and unload discards later POST", async () => {
  let releaseRead, releasePost;
  const ui = await boot(base(), {
    apiGet: async endpoint => endpoint === "monitor/quota"
      ? new Promise(resolve => { releaseRead = resolve; }) : base(),
    apiPost: () => new Promise(resolve => { releasePost = resolve; }),
  });
  const manual = ui.api.refreshQuotaManual();
  releasePost(quotaSample("手动结果"));
  await manual;
  releaseRead(quotaSample("迟到的缓存", {stale: true}));
  await flushRequests();
  assert.deepEqual({...ui.requests[0].body}, {csrf: "test-nonce"});
  assert.equal(ui.requests.length, 1);
  assert.ok(quotaHasGroup(ui, "手动结果"));

  const late = ui.api.refreshQuotaManual();
  const status = ui.get("quota-badge").textContent;
  ui.handlers.pagehide();
  releasePost(quotaSample("迟到的查询"));
  await late;
  assert.equal(ui.get("quota-badge").textContent, status);
  assert.ok(!quotaHasGroup(ui, "迟到的查询"));
});

test("quota waits for login and a readable cache, then ignores a pre-login POST", async () => {
  let status = {...base(), login: {state: "waiting", busy: true}};
  let failGet = true, releasePost;
  const ui = await boot(status, {
    apiGet: async endpoint => {
      if (endpoint !== "monitor/quota") return status;
      if (failGet) throw new Error("cache unavailable");
      return quotaSample("", {groups: [], fetched_at: null});
    },
    apiPost: () => new Promise(resolve => { releasePost = resolve; }),
  });
  assert.equal(quotaReads(ui), 0);
  status = base();
  await ui.api.poll();
  await flushRequests();
  assert.equal(quotaReads(ui), 1);
  assert.equal(ui.requests.length, 0);
  assert.equal(ui.get("quota-badge").textContent, "查询失败");
  await ui.api.poll();
  assert.equal(quotaReads(ui), 1);

  failGet = false;
  ui.get("tab-btn-setup").onclick();
  ui.get("tab-btn-monitor").onclick();
  await flushRequests();
  assert.equal(ui.requests.length, 1);
  status = {...base(), login: {state: "waiting", busy: true}};
  await ui.api.poll();
  releasePost(quotaSample("旧账号"));
  await flushRequests();
  assert.ok(!quotaHasGroup(ui, "旧账号"));
  assert.equal(ui.get("quota-refresh").disabled, true);

  status = base();
  await ui.api.poll();
  await flushRequests();
  assert.equal(ui.requests.length, 2);
  releasePost(quotaSample("新账号"));
  await flushRequests();
  assert.ok(quotaHasGroup(ui, "新账号"));
});

test("daily heatmap distinguishes empty days, zero usage, and unknown tokens, supports local metric/model filters and range switching", async () => {
  const sample90 = {
    schema_version: 1,
    timezone: "host_local",
    range: {days: 90, start: "2026-11-20", end: "2026-11-26"},
    models: ["gemini-4-neon", "gemini-3.8-flash"],
    days: [
      // 11-21: 已知零消耗（例如全部本地短路/未发送）
      {
        date: "2026-11-21", requests: 1, success: 0, failed: 1,
        input_tokens: 0, cached_tokens: 0, output_tokens: 0, total_tokens: 0, usage_unknown_turns: 0,
        by_model: {
          "gemini-4-neon": {requests: 1, success: 0, failed: 1, input_tokens: 0, cached_tokens: 0, output_tokens: 0, total_tokens: 0, usage_unknown_turns: 0},
        },
      },
      // 11-22: 全部轮次 usage 未知（各列为 null，绝不可当成 0）
      {
        date: "2026-11-22", requests: 2, success: 0, failed: 2,
        input_tokens: null, cached_tokens: null, output_tokens: null, total_tokens: null, usage_unknown_turns: 2,
        by_model: {
          "gemini-3.8-flash": {requests: 2, success: 0, failed: 2, input_tokens: null, cached_tokens: null, output_tokens: null, total_tokens: null, usage_unknown_turns: 2},
        },
      },
      // 11-26 (今日 = range.end): 正常有 Token 消耗
      {
        date: "2026-11-26", requests: 4, success: 3, failed: 1,
        input_tokens: 1200, cached_tokens: 400, output_tokens: 300, total_tokens: 1500, usage_unknown_turns: 1,
        by_model: {
          "gemini-4-neon": {requests: 4, success: 3, failed: 1, input_tokens: 1200, cached_tokens: 400, output_tokens: 300, total_tokens: 1500, usage_unknown_turns: 1},
        },
      },
    ],
  };

  const ui = await boot(base(), {
    apiGet: async (ep) => {
      if (ep === "monitor/daily/90") return sample90;
      if (ep === "monitor/daily/30") return {...sample90, range: {days: 30, start: "2026-11-20", end: "2026-11-26"}};
      if (ep === "monitor/daily/365") {
        const err = new Error("storage unavailable");
        err.status = 503;
        throw err;
      }
      return base();
    },
  });
  await new Promise(resolve => setImmediate(resolve));

  // 默认 90 天 + 请求数
  assert.ok(ui.getRequests.includes("monitor/daily/90"));
  assert.match(html, /id="daily-footnote"[^>]*>[^<]*插件本地/);

  const cells = () => collectNodes(ui.get("daily-heatmap")).filter(n => n.classList.contains("hm-cell"));
  assert.equal(cells().length, 7);
  const byDate = (d) => cells().find(c => c.dataset.date === d);

  // 11-20 无记录 → empty；11-26 有 4 次请求 → 活跃等级
  assert.equal(byDate("2026-11-20").dataset.level, "empty");
  assert.ok(["1", "2", "3", "4"].includes(byDate("2026-11-26").dataset.level));

  // 切换到 Token 指标（本地切换，不发新网络请求）
  const beforeCount = ui.getRequests.length;
  ui.get("daily-metric-tokens").onclick();
  assert.equal(ui.getRequests.length, beforeCount);
  assert.equal(byDate("2026-11-20").dataset.level, "empty");    // 无记录
  assert.equal(byDate("2026-11-21").dataset.level, "zero");     // 已知 0 Token
  assert.equal(byDate("2026-11-22").dataset.level, "unknown");  // 全未知 null（区别于 0 和无记录）
  assert.ok(["1", "2", "3", "4"].includes(byDate("2026-11-26").dataset.level));

  // 点击无记录日期与未知日期，详情栏准确区分
  byDate("2026-11-20").click();
  assert.equal(ui.get("daily-detail").textContent, "2026-11-20：无记录");
  byDate("2026-11-22").click();
  assert.match(ui.get("daily-detail").textContent, /输入 未知.*输出 未知.*合计 未知 Token.*2 轮用量未知/);

  // 仅按 gemini-3.8-flash 筛选：该模型仅 11-22 有记录且全未知 → 汇总显示未知而非 0，今日无记录
  ui.get("daily-model-filter").value = "gemini-3.8-flash";
  ui.get("daily-model-filter").onchange();
  assert.equal(ui.getRequests.length, beforeCount);
  const summaryTexts = collectNodes(ui.get("daily-summary")).map(n => n.textContent).filter(Boolean);
  assert.ok(summaryTexts.includes("无记录"));
  assert.ok(summaryTexts.includes("未知"));
  assert.ok(summaryTexts.includes("1 / 90 天"));

  // 切换 30 天发新请求；切换 365 天触发 503 错误提示
  ui.get("daily-range-30").onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(ui.getRequests.includes("monitor/daily/30"));

  ui.get("daily-range-365").onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.get("daily-alert").hidden, false);
  assert.match(ui.get("daily-alert").children[0].textContent, /503/);
  assert.equal(ui.get("daily-alert").children[1].textContent, "重试");
});

test("admin command help modal opens on demand, lists all 9 contract commands, and supports copy", async () => {
  const ui = await boot();
  assert.equal(ui.get("modal-commands").hidden, true);
  ui.get("open-commands").onclick();
  assert.equal(ui.get("modal-commands").hidden, false);

  const cmdNodes = collectNodes(ui.get("commands-list"));
  const codes = cmdNodes.filter(n => n.tag === "code").map(n => n.textContent);
  for (const expected of ["/agy help", "/agy setup", "/agy use <provider_id>", "/agy cost", "/agy doctor", "/agy quota", "/agy status", "/agy models", "/agy usage [30|90|365]"]) {
    assert.ok(codes.includes(expected), `Missing command ${expected}`);
  }

  let copied = false;
  ui.doc.execCommand = name => {copied = name === "copy"; return copied;};
  const copyBtn = cmdNodes.find(n => n.tag === "button" && n.textContent === "复制");
  await copyBtn.click();
  assert.equal(copied, true);

  ui.get("commands-close").onclick();
  assert.equal(ui.get("modal-commands").hidden, true);
});

test("experimental search_web renders independent off-by-default switch, honest post-observation copy, and saves only changed keys", async () => {
  const searchTool = {
    id: "search_web",
    source: "builtin",
    purpose: "联网搜索",
    risk: "network",
    capability: "experimental",
    choices: ["deny", "allow"],
    policy: "deny",
    effective: false,
    reload_required: false,
    pre_execution_approval: false,
    enforcement: "post_observation",
    limitation: "CLI 自动执行搜索，无法在执行前审批。关闭时由模型护栏和事后中止限制，发现时搜索请求可能已发出。开启后按通知计入预算，超预算时中止本轮。",
  };
  const readTool = {
    id: "read_file",
    source: "builtin",
    purpose: "读取隔离工作区顶层文本文件",
    risk: "high",
    capability: "supported",
    choices: ["deny", "allow_once"],
    policy: "deny",
    effective: false,
    reload_required: false,
  };
  const data = base();
  data.tool_policy = {
    enable_tools: true,
    reload_required: false,
    tools: [
      {id: "astrbot_mcp", source: "mcp", purpose: "宿主提供的工具", risk: "host", capability: "supported", choices: [], policy: "allow_once", effective: true, reload_required: false},
      readTool,
      searchTool,
      {id: "run_command", source: "builtin", purpose: "执行终端命令", risk: "high", capability: "unsupported", choices: ["deny"], policy: "deny", effective: false, reload_required: false},
    ],
  };
  const ui = await boot(data);
  ui.get("tab-btn-tools").onclick();

  const findRow = (id) => collectNodes(ui.get("tool-policy-list")).find(
    r => r.className === "tool-row" && collectNodes(r).some(n => n.tag === "code" && n.textContent === id),
  );
  const searchRow = findRow("search_web");
  assert.ok(searchRow);
  const searchNodes = collectNodes(searchRow);
  const searchTexts = searchNodes.map(n => n.textContent).filter(Boolean);

  // 实验性徽标 + 默认关闭的独立开关（role="switch"）
  assert.ok(searchTexts.includes("实验性"));
  assert.ok(searchTexts.includes("联网风险"));
  assert.ok(searchTexts.includes("未生效"));
  const sw = searchNodes.find(n => n.tag === "button" && n.getAttribute("role") === "switch");
  assert.ok(sw);
  assert.equal(sw.getAttribute("aria-checked"), "false");

  // 诚实文案：必须说明事后拦截、请求可能已发出、不能保证百分百拦住、计入工具调用次数，且不得包含虚假承诺或技术术语
  const noteOff = searchNodes.find(n => n.className === "tool-risk-note")?.textContent || "";
  assert.match(noteOff, /事后拦截/);
  assert.match(noteOff, /请求可能已经发出/);
  assert.match(noteOff, /不能保证百分之百/);
  assert.match(noteOff, /工具调用次数/);
  for (const forbidden of ["已阻止", "执行前拒绝", "绝对安全", "post_observation", "tool_max_calls"]) {
    assert.equal(noteOff.includes(forbidden), false, `Search OFF copy must not contain "${forbidden}"`);
  }

  // 开启开关 → 文案切换仍诚实、不承诺硬上限；保存只提交 search_web
  sw.click();
  assert.equal(ui.get("tool-policy-dirty-badge").hidden, false);
  const searchRowOn = findRow("search_web");
  const swOn = collectNodes(searchRowOn).find(n => n.tag === "button" && n.getAttribute("role") === "switch");
  assert.equal(swOn.getAttribute("aria-checked"), "true");
  const noteOn = collectNodes(searchRowOn).find(n => n.className === "tool-risk-note")?.textContent || "";
  assert.match(noteOn, /事后拦截/);
  assert.match(noteOn, /工具调用次数/);
  assert.match(noteOn, /尽量中止/);
  assert.match(noteOn, /无法撤回/);
  for (const forbidden of ["已阻止", "执行前拒绝", "绝对安全", "post_observation", "tool_max_calls"]) {
    assert.equal(noteOn.includes(forbidden), false, `Search ON copy must not contain "${forbidden}"`);
  }

  const savedPolicy = {
    enable_tools: true,
    reload_required: true,
    tools: data.tool_policy.tools.map(t => t.id === "search_web" ? {...t, policy: "allow", effective: false, reload_required: true} : t),
  };
  ui.bridge.apiPost = async (endpoint, body) => {
    ui.requests.push({endpoint, body});
    return savedPolicy;
  };
  await ui.api.saveToolPolicy();
  assert.equal(ui.requests[0].endpoint, "setup/tool-policy");
  assert.deepEqual({...ui.requests[0].body}, {csrf: "test-nonce", search_web: "allow"});

  // 保存后待重载徽标明确提示「当前未生效 · 重载后开启」
  const reloadedRow = findRow("search_web");
  assert.equal(reloadedRow.dataset.reloadPending, "true");
  const reloadedBadge = collectNodes(reloadedRow).find(n => n.className === "badge" && n.textContent.includes("重载后"));
  assert.ok(reloadedBadge);
  assert.equal(reloadedBadge.textContent, "当前未生效 · 重载后开启");
  assert.equal(reloadedBadge.dataset.state, "warn");
});

test("pending reload badges clearly distinguish on->off downgrade and off->on upgrade per row", async () => {
  const data = base();
  data.tool_policy = {
    enable_tools: true,
    reload_required: true,
    tools: [
      {id: "astrbot_mcp", source: "mcp", purpose: "宿主提供的工具", risk: "host", capability: "supported", choices: [], policy: "allow_once", effective: true, reload_required: false},
      {id: "read_file", source: "builtin", purpose: "读取隔离工作区顶层文本文件", risk: "high", capability: "supported", choices: ["deny", "allow_once"], policy: "deny", effective: true, reload_required: true},
      {id: "search_web", source: "builtin", purpose: "联网搜索", risk: "network", capability: "experimental", choices: ["deny", "allow"], policy: "allow", effective: false, reload_required: true, pre_execution_approval: false, enforcement: "post_observation"},
    ],
  };
  const ui = await boot(data);
  const findRow = (id) => collectNodes(ui.get("tool-policy-list")).find(
    r => r.className === "tool-row" && collectNodes(r).some(n => n.tag === "code" && n.textContent === id),
  );

  // read_file: allow_once -> deny 待重载时绝不能只显示绿色「当前生效」
  const readRow = findRow("read_file");
  const readBadges = collectNodes(readRow).filter(n => n.className === "badge");
  assert.equal(readRow.dataset.reloadPending, "true");
  assert.equal(readBadges.some(b => b.textContent === "当前生效" && b.dataset.state === "ready"), false);
  const readPendingBadge = readBadges.find(b => b.textContent === "当前生效 · 重载后关闭");
  assert.ok(readPendingBadge);
  assert.equal(readPendingBadge.dataset.state, "warn");

  // search_web: deny -> allow 待重载显示「当前未生效 · 重载后开启」
  const searchRow = findRow("search_web");
  const searchBadges = collectNodes(searchRow).filter(n => n.className === "badge");
  assert.equal(searchRow.dataset.reloadPending, "true");
  assert.ok(searchBadges.some(b => b.textContent === "当前未生效 · 重载后开启" && b.dataset.state === "warn"));

  // astrbot_mcp: 未变化的行仍正常显示绿色「当前生效」
  const mcpRow = findRow("astrbot_mcp");
  const mcpBadges = collectNodes(mcpRow).filter(n => n.className === "badge");
  assert.equal(mcpRow.dataset.reloadPending, undefined);
  assert.ok(mcpBadges.some(b => b.textContent === "当前生效" && b.dataset.state === "ready"));

  // search_web: allow -> deny 与 read_file: deny -> allow_once 反向场景
  ui.api.render({
    ...data,
    tool_policy: {
      enable_tools: true,
      reload_required: true,
      tools: [
        {id: "read_file", source: "builtin", purpose: "读取隔离工作区顶层文本文件", risk: "high", capability: "supported", choices: ["deny", "allow_once"], policy: "allow_once", effective: false, reload_required: true},
        {id: "search_web", source: "builtin", purpose: "联网搜索", risk: "network", capability: "experimental", choices: ["deny", "allow"], policy: "deny", effective: true, reload_required: true, pre_execution_approval: false, enforcement: "post_observation"},
      ],
    },
  });
  assert.ok(collectNodes(findRow("read_file")).some(b => b.className === "badge" && b.textContent === "当前未生效 · 重载后单次允许" && b.dataset.state === "warn"));
  assert.ok(collectNodes(findRow("search_web")).some(b => b.className === "badge" && b.textContent === "当前生效 · 重载后关闭" && b.dataset.state === "warn"));
});

test("string-only bridge decodes [AGY:CODE] across all panel branches, prevents 409 LOGIN_BUSY auth misclassification, and keeps fallback without code", async () => {
  // 真实 AstrBot 4.29.4 plugin_page_bridge.js 只用 new Error(stringMessage) reject，绝不带 .status
  const bridgeErr = (msg) => new Error(msg);
  const data = base();
  data.tool_policy = {
    enable_tools: true,
    reload_required: false,
    tools: [
      {id: "read_file", source: "builtin", purpose: "读取文件", risk: "high", capability: "supported", choices: ["deny", "allow_once"], policy: "deny", effective: false, reload_required: false},
    ],
  };

  const ui = await boot(data, {
    apiGet: async (ep) => {
      if (ep === "setup/status") return data;
      if (ep === "monitor/status") return {schema_version: 2, sampled_at: "2026-11-26T00:00:00Z", instances: []};
      if (ep === "monitor/quota") throw bridgeErr("[AGY:CLOSED] 插件已停用。");
      if (ep.startsWith("monitor/daily/")) throw bridgeErr("[AGY:STORAGE_UNAVAILABLE] 每日统计存储不可用。");
      return data;
    },
  });
  await new Promise(resolve => setImmediate(resolve));

  // 1. monitor/quota [AGY:CLOSED]（无 .status）显示停用提示且剥离前缀
  assert.match(ui.get("quota-alert").textContent, /插件已停用/);
  assert.equal(ui.get("quota-alert").textContent.includes("[AGY:"), false);

  // 2. monitor/daily [AGY:STORAGE_UNAVAILABLE]（无 .status）显示本地统计暂不可用且剥离前缀
  assert.match(ui.get("daily-alert").children[0].textContent, /本地统计暂不可用/);
  assert.equal(ui.get("daily-alert").children[0].textContent.includes("[AGY:"), false);

  // 3. setup/quota-refresh [AGY:LOGIN_BUSY]（文案含「登录」且无 .status）绝不被误判为鉴权失败
  assert.equal(ui.api.isAuthDeniedError(bridgeErr("[AGY:LOGIN_BUSY] 登录中，请稍后刷新。")), false);
  assert.equal(ui.api.isAuthDeniedError(bridgeErr("[AGY:BUSY] 检测、登录或保存正在进行，请等待完成。")), false);
  assert.equal(ui.api.isAuthDeniedError(bridgeErr("[AGY:CSRF_EXPIRED] 页面已过期，请刷新后重试。")), false);
  assert.equal(ui.api.isAuthDeniedError(bridgeErr("[AGY:UNAUTHENTICATED] 请先登录 AstrBot 管理面板。")), true);
  assert.equal(ui.api.isAuthDeniedError(bridgeErr("[AGY:FORBIDDEN] 仅 AstrBot 面板管理员可使用。")), true);

  ui.bridge.apiPost = async () => {throw bridgeErr("[AGY:LOGIN_BUSY] 登录中，请稍后刷新。");};
  await ui.api.refreshQuotaManual();
  assert.match(ui.get("quota-alert").textContent, /官方登录正在进行中/);
  assert.equal(ui.get("quota-alert").textContent.includes("无权"), false);
  assert.equal(ui.get("quota-alert").textContent.includes("[AGY:"), false);

  // 4. quota-refresh 覆盖 QUOTA_UNAVAILABLE / CSRF_EXPIRED / FORBIDDEN / UNAUTHENTICATED / BUSY
  for (const [raw, expected] of [
    ["[AGY:QUOTA_UNAVAILABLE] 额度服务尚未就绪。", /额度服务暂不可用/],
    ["[AGY:CSRF_EXPIRED] 页面已过期，请刷新后重试。", /页面已过期，请刷新/],
    ["[AGY:FORBIDDEN] 仅 AstrBot 面板管理员可使用设置页面。", /无权刷新账号额度/],
    ["[AGY:UNAUTHENTICATED] 请先登录 AstrBot 管理面板。", /无权刷新账号额度/],
    ["[AGY:BUSY] 操作进行中。", /其他操作正在进行中/],
  ]) {
    ui.bridge.apiPost = async () => {throw bridgeErr(raw);};
    await ui.api.refreshQuotaManual();
    assert.match(ui.get("quota-alert").textContent, expected);
    assert.equal(ui.get("quota-alert").textContent.includes("[AGY:"), false);
  }

  // 5. setup/tool-policy 覆盖 CSRF_EXPIRED / CLOSED / FORBIDDEN / UNSUPPORTED / INTERNAL（均保留草稿并剥离前缀）
  const readBtns = collectNodes(ui.get("tool-policy-list")).filter(n => n.tag === "button");
  readBtns[1].click();
  for (const [raw, expected] of [
    ["[AGY:CSRF_EXPIRED] 页面已过期，请刷新后重试。", /页面已过期，请刷新.*已保留当前编辑内容/],
    ["[AGY:CLOSED] 插件已停用。", /插件已停用.*已保留当前编辑内容/],
    ["[AGY:FORBIDDEN] 仅面板管理员。", /无权修改工具策略.*已保留当前编辑内容/],
    ["[AGY:UNSUPPORTED] 请先完成受支持 CLI 的版本检测。", /请先完成受支持 CLI 的版本检测.*已保留当前编辑内容/],
    ["[AGY:INTERNAL] AGY 本地操作失败。", /AGY 本地操作失败.*已保留当前编辑内容/],
  ]) {
    ui.bridge.apiPost = async () => {throw bridgeErr(raw);};
    await ui.api.saveToolPolicy();
    assert.match(ui.get("tool-policy-feedback").textContent, expected);
    assert.equal(ui.get("tool-policy-feedback").textContent.includes("[AGY:"), false);
    assert.equal(ui.get("tool-policy-dirty-badge").hidden, false);
  }

  // 6. 首次设置 action() 与 poll() 同样剥离 [AGY:CODE] 并输出对应提示
  ui.bridge.apiPost = async () => {throw bridgeErr("[AGY:CSRF_EXPIRED] 页面已过期，请刷新后重试。");};
  await ui.api.action("check");
  assert.match(ui.get("error").textContent, /页面已过期，请刷新/);
  assert.equal(ui.get("error").textContent.includes("[AGY:"), false);

  ui.bridge.apiPost = async () => {throw bridgeErr("[AGY:BUSY] 检测、登录或保存正在进行，请等待完成。");};
  await ui.api.action("login");
  assert.match(ui.get("error").textContent, /正在进行中/);
  assert.equal(ui.get("error").textContent.includes("[AGY:"), false);

  ui.bridge.apiGet = async () => {throw bridgeErr("[AGY:CLOSED] 插件已停用。");};
  await ui.api.poll();
  assert.equal(ui.get("error").textContent.includes("[AGY:"), false);

  // 7. 无稳定码时的回退行为：直接请求的 HTTP status 仍兼容；纯字符串宿主鉴权错误仍命中鉴权提示
  const direct409 = new Error("登录中，请稍后刷新。");
  direct409.status = 409;
  assert.equal(ui.api.isAuthDeniedError(direct409), false);
  ui.bridge.apiPost = async () => {throw direct409;};
  await ui.api.refreshQuotaManual();
  assert.match(ui.get("quota-alert").textContent, /官方登录正在进行中/);

  const hostAuthErr = bridgeErr("Unauthorized");
  assert.equal(ui.api.isAuthDeniedError(hostAuthErr), true);
  ui.bridge.apiPost = async () => {throw hostAuthErr;};
  await ui.api.refreshQuotaManual();
  assert.match(ui.get("quota-alert").textContent, /无权刷新账号额度/);

  const unknownErr = bridgeErr("some network glitch");
  assert.equal(ui.api.isAuthDeniedError(unknownErr), false);
  ui.bridge.apiPost = async () => {throw unknownErr;};
  await ui.api.refreshQuotaManual();
  assert.equal(ui.get("quota-alert").textContent, "额度刷新请求失败，请稍后重试。");
});

test("setup-guide-alert guides blocked unauthenticated users to setup tab and hides once ready", async () => {
  const blocked = {
    ...base(),
    report: {status: "blocked", stage: "cli", cli_version: "", selected_model: "", models: [], efforts: []},
    login: {state: "idle", busy: false, owned: false, url: "", ticket: ""},
    providers: [],
  };
  const ui = await boot(blocked);
  const guideAlert = ui.get("setup-guide-alert");
  assert.equal(guideAlert.hidden, false);
  const guideNodes = collectNodes(guideAlert);
  assert.match(guideNodes.map(n => n.textContent).join(" "), /环境与认证/);
  const jumpBtn = guideNodes.find(n => n.tag === "button" && n.textContent.includes("前往配置"));
  assert.ok(jumpBtn);
  jumpBtn.click();
  assert.equal(ui.get("tab-setup").hidden, false);
  assert.equal(ui.get("tab-monitor").hidden, true);

  ui.api.render({...blocked, report: base().report});
  assert.equal(guideAlert.hidden, true);

  ui.api.render({...blocked, providers: [{provider_id: "agy-main", model: "gemini-test", enabled: true}]});
  assert.equal(guideAlert.hidden, true);
});

test("active 0-turn monitor card renders compact single-line status while non-empty turns keep KPI grid", async () => {
  const ui = await boot();
  const hasClass = (node, cls) => String(node.className || "").split(/\s+/).includes(cls);
  const emptyMon = monitorPayload();
  ui.api.renderMonitor(emptyMon);
  const cardsEmpty = collectNodes(ui.get("monitor-instances")).filter(n => n.tag === "article");
  assert.equal(cardsEmpty.length, 1);
  const emptyNodes = collectNodes(cardsEmpty[0]);
  assert.ok(emptyNodes.some(n => hasClass(n, "monitor-empty") && /已就绪，近 24h 暂无请求样本/.test(n.textContent)));
  assert.equal(emptyNodes.some(n => hasClass(n, "kpi-grid")), false);

  const activeMon = monitorPayload();
  activeMon.instances[0].summary.total_turns = 2;
  activeMon.instances[0].summary.request_sent_turns = 2;
  activeMon.instances[0].summary.success_turns = 2;
  ui.api.renderMonitor(activeMon);
  const activeNodes = collectNodes(ui.get("monitor-instances"));
  assert.ok(activeNodes.some(n => hasClass(n, "kpi-grid")));
  assert.equal(activeNodes.some(n => hasClass(n, "monitor-empty")), false);
});

test("quota panel localizes English bucket names and collapses model lists longer than 3", async () => {
  const ui = await boot();
  const hasClass = (node, cls) => String(node.className || "").split(/\s+/).includes(cls);
  ui.api.renderQuota({
    schema_version: 1,
    status: "ok",
    capability: "supported",
    fetched_at: "2099-11-26T10:00:00Z",
    stale: false,
    error: null,
    groups: [
      {
        name: "Gemini",
        models_shared: ["M1", "M2", "M3", "M4"],
        buckets: [
          {id: "w", name: "Weekly Limit Remaining", window: "weekly", remaining_fraction: 0.9, reset_time: "2099-12-03T08:00:00Z"},
          {id: "5h", name: "Five Hour Limit Remaining", window: "five_hour", remaining_fraction: 0.8, reset_time: "2099-11-26T13:00:00Z"},
        ],
      },
      {
        name: "Compact",
        models_shared: ["C1", "C2"],
        buckets: [
          {id: "d", name: "Daily Limit Remaining", window: "daily", remaining_fraction: 1.0, reset_time: null},
          {id: "m", name: "Monthly Limit Remaining", window: "monthly", remaining_fraction: 1.0, reset_time: null},
        ],
      },
    ],
  });
  const nodes = collectNodes(ui.get("quota-body"));
  const text = nodes.map(n => n.textContent).join(" ");
  assert.match(text, /每周剩余/);
  assert.match(text, /5 小时剩余/);
  assert.match(text, /每日剩余/);
  assert.match(text, /每月剩余/);
  assert.doesNotMatch(text, /Weekly Limit Remaining|Five Hour Limit Remaining/);
  const drawers = nodes.filter(n => n.tag === "details" && hasClass(n, "quota-models-drawer"));
  assert.equal(drawers.length, 1);
  assert.match(collectNodes(drawers[0]).find(n => n.tag === "summary").textContent, /适用 4 个模型/);
});

test("tool policy folds locked unsupported builtins into drawer only when group has primary items", async () => {
  const hasClass = (node, cls) => String(node.className || "").split(/\s+/).includes(cls);
  const ui = await boot({
    ...base(),
    tool_policy: {
      enable_tools: true,
      reload_required: false,
      tools: [
        {
          id: "read_file", source: "builtin", purpose: "读取文件", policy: "deny",
          choices: ["deny", "allow_once"], capability: "unverified", risk: "medium", effective: false,
        },
        {
          id: "write_file", source: "builtin", purpose: "写入文件", policy: "deny",
          choices: ["deny"], capability: "unsupported", risk: "high", effective: false,
        },
        {
          id: "run_command", source: "builtin", purpose: "执行命令", policy: "deny",
          choices: ["deny"], capability: "unsupported", risk: "high", effective: false,
        },
      ],
    },
  });
  const nodes = collectNodes(ui.get("tool-policy-list"));
  const drawers = nodes.filter(n => n.tag === "details" && hasClass(n, "tool-locked-drawer"));
  assert.equal(drawers.length, 1);
  assert.equal(collectNodes(drawers[0]).find(n => n.tag === "summary").textContent, "固定拒绝的内置工具（2 项）");
  const drawerRowIds = collectNodes(drawers[0]).filter(n => n.tag === "code").map(n => n.textContent);
  assert.deepEqual(drawerRowIds, ["write_file", "run_command"]);
});

test("heatmap fixes hover scale jitter, stabilizes footer layout, avoids month label overlap, and clears hover only on grid exit", async () => {
  const css = readFileSync(new URL("../views/setup/style.css", import.meta.url), "utf8");
  // 热力图格子悬浮禁止使用 transform: scale(...)，页脚采用纵向固定布局防高度跳变抽动
  const hmHoverMatch = css.match(/\.hm-cell:hover\s*\{([^}]*)\}/);
  assert.ok(hmHoverMatch);
  assert.doesNotMatch(hmHoverMatch[1], /transform\s*:\s*scale/);
  assert.match(css, /\.heatmap-footer\s*\{[^}]*flex-direction:\s*column/);
  assert.match(css, /\.heatmap-detail\s*\{[^}]*min-height:/);

  // 2026-10-30（周五）至 2026-11-28：第 0 列仅含 10 月末 2 天且第 1 列即进入 11 月，不应把 10月 与 11月 叠在相邻两列
  const ui = await boot(base(), {
    apiGet: async (ep) => {
      if (ep.startsWith("monitor/daily/")) {
        return {
          schema_version: 1,
          timezone: "host_local",
          range: {days: 30, start: "2026-10-30", end: "2026-11-28"},
          models: ["gemini-4-hydrogen"],
          days: [
            {date: "2026-11-27", requests: 10, success: 9, failed: 1, input_tokens: 1000, cached_tokens: 400, output_tokens: 200, total_tokens: 1200, usage_unknown_turns: 0, by_model: {}},
          ],
        };
      }
      return base();
    },
  });
  await new Promise(resolve => setImmediate(resolve));

  const monthLabels = collectNodes(ui.get("daily-heatmap"))
    .filter(n => n.className === "heatmap-month-label")
    .map(n => n.textContent)
    .filter(Boolean);
  assert.deepEqual(monthLabels, ["11月"]);

  // 鼠标悬停到无记录日期，再到另一个日期，中间经过格子间隙时不依赖单格 mouseleave 闪烁；仅离开整个网格才恢复选中日期
  const grid = collectNodes(ui.get("daily-heatmap")).find(n => n.className === "heatmap-grid");
  const cells = collectNodes(grid).filter(n => n.className === "hm-cell");
  const cellEmpty = cells.find(c => c.dataset.date === "2026-11-20");
  assert.equal(cellEmpty.mouseleave, undefined);
  cellEmpty.mouseenter();
  assert.equal(ui.get("daily-detail").textContent, "2026-11-20：无记录");
  grid.mouseleave();
  assert.match(ui.get("daily-detail").textContent, /^2026-11-27：10 次请求/);
});

test("collapsible turn history card defaults collapsed, fetches monitor/turns on expand, filters/paginates, and exposes sanitized per-turn metrics", async () => {
  const turns25 = Array.from({length: 25}, (_, idx) => ({
    recorded_at: `2026-11-28T10:${String(59 - idx).padStart(2, "0")}:00+00:00`,
    model: "gemini-4-hydrogen",
    negotiated_model: idx === 0 ? "gemini-4-hydrogen" : null,
    mode: "tools",
    status: idx === 1 ? "error" : idx === 2 ? "short_circuited" : "ok",
    error_code: idx === 1 ? "TIMEOUT" : idx === 2 ? "RATE_LIMIT" : null,
    usage_status: idx === 1 ? "unavailable" : idx === 2 ? "not_sent" : idx === 3 ? "reported" : "reported",
    elapsed_ms: idx === 1 ? 30500 : 1200 + idx * 100,
    input_other: idx === 1 ? null : idx === 2 ? 0 : 1000 + idx,
    input_cached: idx === 1 ? null : idx === 2 ? 0 : idx === 3 ? null : 4000,
    output: idx === 1 ? null : idx === 2 ? 0 : 250 + idx,
    cache_fields_present: {cachedReadTokens: idx !== 3 && idx !== 1, cachedWriteTokens: false},
    reused: idx % 2 === 0,
    rebuild_reasons: idx % 2 === 0 ? [] : ["history_host_trim"],
    prompt_bytes: 2048,
    replay_bytes: idx % 2 === 0 ? 0 : 1024,
    omitted_messages: idx === 0 ? 2 : 0,
    body_bytes: 512,
    thought_bytes: 128,
    context_used: 12000,
    context_window_size: 200000,
    historical: idx >= 20,
    turn_id: `abcdef012345${String(idx).padStart(4, "0")}`,
    content_429_candidate: {system_hash8: "a1b2c3d4", system_bytes: 2048, has_output: false, attempt: 1},
  }));

  const mon = monitorPayload(null);
  mon.schema_version = 2;
  mon.instances[0].model = "gemini-4-hydrogen";
  mon.instances[0].summary.total_turns = 25;
  mon.instances[0].recent_turns = turns25.slice(0, 20);

  let failTurnsEndpoint = false;
  const ui = await boot(base(), {
    apiGet: async (ep) => {
      if (ep === "monitor/status") return mon;
      if (ep === "monitor/turns") {
        if (failTurnsEndpoint) throw new Error("[AGY:INTERNAL] 暂时不可用");
        return {
          schema_version: 1,
          sampled_at: "2026-11-28T11:00:00+00:00",
          window: {hours: 24, max_turns_per_instance: 100},
          instances: [{
            provider_id: "agy-main",
            model: "gemini-4-hydrogen",
            enabled: true,
            effective_mode: "tools",
            runtime_state: "active",
            history_storage: "available",
            total_turns: 25,
            turns: turns25,
          }],
        };
      }
      return base();
    },
  });
  await new Promise(resolve => setImmediate(resolve));

  // 默认折叠且不主动发 monitor/turns 请求，但徽标和摘要预览直接展示
  assert.equal(ui.get("turn-history-panel").hidden, true);
  assert.equal(ui.get("turn-history-toggle").getAttribute("aria-expanded"), "false");
  assert.equal(ui.getRequests.includes("monitor/turns"), false);
  assert.equal(ui.get("turn-history-badge").textContent, "25 条");
  assert.match(ui.get("turn-history-preview").textContent, /最新 1\.20 s · 成功 · 缓存命中 80\.0%/);

  // 点击展开：拉取 monitor/turns（展示 25 条，第 1 页 20 条，支持翻页与详情展开）
  ui.get("turn-history-toggle").onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.get("turn-history-panel").hidden, false);
  assert.equal(ui.get("turn-history-toggle").getAttribute("aria-expanded"), "true");
  assert.equal(ui.getRequests.filter(e => e === "monitor/turns").length, 1);
  assert.equal(ui.get("turn-history-range-info").textContent, "第 1–20 / 25 条");

  const bodyTexts = collectNodes(ui.get("turn-history-body")).map(n => n.textContent).filter(Boolean);
  assert.ok(bodyTexts.includes("1.20 s"));
  assert.ok(bodyTexts.includes("30.5 s"));
  assert.ok(bodyTexts.includes("4,000"));
  assert.ok(bodyTexts.includes("命中 80.0%"));
  assert.ok(bodyTexts.includes("未报告")); // idx=3 缺缓存读取字段
  assert.ok(bodyTexts.includes("未知"));   // idx=1 usage unavailable
  assert.ok(bodyTexts.includes("失败 · 请求超时"));
  assert.ok(bodyTexts.includes("重建 · 宿主裁剪历史"));
  // 绝不泄露内部 hash 或原始 turn_id
  assert.equal(bodyTexts.some(t => t.includes("a1b2c3d4") || t.includes("abcdef012345")), false);

  // 展开首行详情，核验总计 Token 与发送/历史重放字节
  const firstDetailBtn = collectNodes(ui.get("turn-history-body"))
    .find(n => n.tag === "button" && n.classList.contains("turn-detail-btn"));
  firstDetailBtn.click();
  const detailTexts = collectNodes(ui.get("turn-history-body"))
    .filter(n => n.className === "turn-detail-row")
    .flatMap(r => collectNodes(r).map(n => n.textContent).filter(Boolean));
  assert.ok(detailTexts.includes("5,000")); // 1000 + 4000
  assert.ok(detailTexts.includes("5,250")); // 1000 + 4000 + 250
  assert.ok(detailTexts.some(t => t.includes("裁剪 2 条")));

  // 翻到第 2 页看到第 21-25 条（含重启前 historical 标记）
  ui.get("turn-history-next").onclick();
  assert.equal(ui.get("turn-history-range-info").textContent, "第 21–25 / 25 条");
  const page2Texts = collectNodes(ui.get("turn-history-body")).map(n => n.textContent).filter(Boolean);
  assert.ok(page2Texts.includes("重启前"));

  // 状态筛选：仅看异常（idx=1 error + idx=2 short_circuited = 2 条）
  ui.get("turn-history-status-abnormal").onclick();
  assert.equal(ui.get("turn-history-range-info").textContent, "第 1–2 / 2 条");

  // 切回全部，模拟 monitor/turns 拉取失败时保留降级提示与已有快照
  ui.get("turn-history-status-all").onclick();
  failTurnsEndpoint = true;
  await ui.api.loadTurnHistory();
  assert.equal(ui.get("turn-history-alert").hidden, false);
  assert.match(ui.get("turn-history-alert").children[0].textContent, /回退展示/);

  // 再次点击收起面板
  ui.get("turn-history-toggle").onclick();
  assert.equal(ui.get("turn-history-panel").hidden, true);
  assert.equal(ui.get("turn-history-toggle").getAttribute("aria-expanded"), "false");
});
