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
  }
  replaceChildren(...children) {
    this.children = children;
    if (this.tag === "select") this.value = children[0]?.value || "";
  }
  append(...children) {this.children.push(...children);}
  setAttribute() {}
  addEventListener(name, callback) {this[name] = callback;}
  focus() {this.focused = true;}
  select() {this.selected = true;}
}

async function boot(initial = base()) {
  const elements = new Map([...html.matchAll(/<([\w-]+)[^>]*\bid="([^"]+)"/g)]
    .map(([, tag, id]) => [id, new Element(tag)]));
  const handlers = {};
  const requests = [];
  const bridge = {
    ready: async () => ({}), apiGet: async () => initial,
    apiPost: async (endpoint, body) => {requests.push({endpoint, body}); return initial;},
  };
  const context = vm.createContext({
    window: {AstrBotPluginView: bridge, addEventListener: (name, cb) => {handlers[name] = cb;}},
    document: {documentElement: new Element("html"), getElementById: id => {
      assert.ok(elements.has(id), `Missing element ${id}`);
      return elements.get(id);
    }, createElement: tag => new Element(tag), hidden: false,
    addEventListener: (name, cb) => {handlers[name] = cb;}},
    Option: class {constructor(name, value) {this.text = name; this.value = value;}},
    navigator: {clipboard: {writeText: async () => {throw new Error("iframe clipboard unavailable");}}},
    setTimeout: () => 1, clearTimeout: () => {},
  });
  const api = await vm.runInContext(`(async () => {${source}\n; return {render, renderMonitor, action, poll};})()`, context);
  return {api, bridge, requests, handlers, doc: context.document, get: id => elements.get(id)};
}

test("theme toggle switches light and dark theme on documentElement", async () => {
  const ui = await boot();
  ui.get("theme-toggle").onclick();
  assert.equal(ui.doc.documentElement.dataset.theme, "dark");
  assert.equal(ui.get("theme-toggle").textContent, "主题：深色");
  ui.get("theme-toggle").onclick();
  assert.equal(ui.get("theme-toggle").textContent, "主题：自动");
  ui.get("theme-toggle").onclick();
  assert.equal(ui.doc.documentElement.dataset.theme, "light");
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
  ui.bridge.apiGet = () => new Promise(resolve => {release = resolve;});
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
