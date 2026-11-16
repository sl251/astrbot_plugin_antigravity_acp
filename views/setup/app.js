const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView;
const SVG_NS = "http://www.w3.org/2000/svg";
let state, pending = false, disposed = false, timer, revision = 0, cachedModels = [], modelSignature = "", effortSignature = "", providerSignature = "";
let themeMode = "auto", nextManualTheme = "dark";
let monitorTimer, monitorPending = false, monitorReady = false, monitorSnapshot, reportDiagSignature = "";
const expandedMonitorSections = new Set();
const fallbackPopovers = [];

const activeLogin = (login) => login.busy;
const stages = {
  discovery: "查找 CLI", isolation: "检查隔离目录", authentication: "检查认证",
  initialize: "ACP 握手", "session/new": "验证登录", model_catalog: "读取模型目录",
  model_selection: "检查模型档位", complete: "检查通过",
};
const loginStates = {
  idle: "等待登录", starting: "正在启动", waiting: "等待授权",
  verifying: "正在验证", complete: "登录已验证", failed: "登录未完成", cancelled: "已取消",
};
const rebuildReasonLabels = {
  no_identity: "无会话标识", abort: "请求中止", previous_error: "上轮异常",
  upstream_compaction_detected: "上游疑似压缩", process_dead: "进程退出",
  idle_expired: "空闲超时", pool_evicted: "池淘汰", runtime_restart: "运行时重启",
  cold_start: "冷启动", system_changed: "系统提示变更", model_changed: "模型变更",
  effort_changed: "思考强度变更", tool_hash_changed: "工具变更", tool_ledger_limit: "工具账本上限",
  history_rewrite: "历史重写", history_host_trim: "历史裁剪", history_append_gap: "历史断层",
  unknown: "未知原因",
};
const toolsModeLabels = {chat: "纯聊天", inherit: "跟随全局", tools: "工具模式", invalid: "无效配置"};
const turnStatusLabels = {ok: "成功", error: "失败", cancelled: "取消", short_circuited: "预检/熔断拦截", unknown: "未知"};
const transportLabels = {loopback_hmac: "本机认证连接", unix_socket: "Unix Socket", unsupported: "当前平台不支持"};
const supervisionLabels = {windows_job: "Windows Job Object", windows_taskkill_fallback: "taskkill 回退", posix_process_group: "独立进程组"};
const minimalToolsLabels = {inactive: "未启用精简", flag_available_unverified: "支持参数（待验收）", unsupported: "CLI 不支持"};
const faultLabels = {
  RATE_LIMIT: "请求受限", QUOTA: "额度不足", AUTH: "认证失败", AUTH_BRIDGE: "认证桥接失败",
  TIMEOUT: "请求超时", NETWORK: "网络异常", PROXY: "代理异常", PROXY_AUTH: "代理认证失败",
  TLS: "证书/TLS 异常", TRANSPORT: "传输中断", BACKPRESSURE: "并发过载", BUSY: "实例忙碌",
  CANCELLED: "已取消", EMPTY: "空响应", UPSTREAM: "上游异常", PROTOCOL: "协议异常",
};

function formatFaultBrief(code) {
  const label = faultLabels[code];
  return label ? `${label} (${code})` : code;
}

function applyTheme() {
  const root = document.documentElement;
  if (!root) return;
  if (themeMode === "light" || themeMode === "dark") {
    root.dataset.theme = themeMode;
    nextManualTheme = themeMode === "dark" ? "light" : "dark";
  } else if (root.dataset) {
    delete root.dataset.theme;
  }
  const labels = {auto: "主题：自动", light: "主题：浅色", dark: "主题：深色"};
  const btn = $("theme-toggle");
  if (btn) btn.textContent = labels[themeMode];
}

function cycleTheme() {
  themeMode = themeMode === "auto" ? nextManualTheme : "auto";
  applyTheme();
}

function wireHelpPopover(btnId, popId) {
  const btn = $(btnId), pop = $(popId);
  if (!btn || !pop) return;
  const hasNative = typeof HTMLElement !== "undefined" && Object.hasOwn(HTMLElement.prototype, "popover");
  if (hasNative) {
    pop.addEventListener("toggle", event => {
      btn.setAttribute("aria-expanded", String(event.newState === "open"));
    });
  } else {
    pop.hidden = true;
    fallbackPopovers.push({btn, pop});
    btn.addEventListener("click", () => {
      const open = pop.hidden;
      for (const item of fallbackPopovers) {
        item.pop.hidden = true;
        item.pop.classList?.remove("is-open");
        item.btn.setAttribute("aria-expanded", "false");
      }
      pop.hidden = !open;
      pop.classList?.toggle("is-open", open);
      btn.setAttribute("aria-expanded", String(open));
    });
  }
}

function badge(id, text, kind = "") {
  const el = $(id);
  el.textContent = text;
  el.dataset.state = kind;
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

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svgNode(tag, attrs = {}, text) {
  const node = typeof document.createElementNS === "function"
    ? document.createElementNS(SVG_NS, tag)
    : document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== undefined && value !== null) node.setAttribute(key, String(value));
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

const monitorCount = value => Number.isFinite(value) ? value.toLocaleString() : "未知";
const monitorPercent = value => Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "未知";

function monitorTime(value) {
  const date = new Date(value);
  return value && Number.isFinite(date.getTime()) ? date.toLocaleString() : "未报告";
}

function monitorShortTime(value) {
  const date = new Date(value);
  if (!value || !Number.isFinite(date.getTime())) return "未报告";
  try {
    return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
  } catch {
    return date.toLocaleTimeString();
  }
}

function monitorAgo(value) {
  const timestamp = value ? Date.parse(value) : NaN;
  if (!Number.isFinite(timestamp)) return "时间未报告";
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  return `${Math.floor(seconds / 3600)} 小时前`;
}

function formatDuration(ms) {
  if (!Number.isFinite(ms)) return "未知";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms >= 10000 ? 1 : 2)} s`;
}

function kvItem(label, value) {
  const item = el("div", "kv-item");
  item.append(el("dt", "", label), el("dd", "", value));
  return item;
}

function overviewMetric(label, scope, value, sub, valueTone = "") {
  const item = el("div", "monitor-metric");
  const dt = el("dt", "");
  dt.append(el("span", "", label), el("span", "scope-pill", scope));
  const dd = el("dd", valueTone ? `tone-${valueTone}` : "", value);
  item.append(dt, dd);
  if (sub) item.append(el("p", "monitor-metric-sub", sub));
  return item;
}

function createLayerDetails(storeKey, kindClass, title, badgeText) {
  const details = el("details", `monitor-layer ${kindClass}`);
  if (expandedMonitorSections.has(storeKey)) details.open = true;
  details.addEventListener("toggle", () => {
    if (details.open) expandedMonitorSections.add(storeKey);
    else expandedMonitorSections.delete(storeKey);
  });
  const summary = el("summary", "");
  summary.append(el("span", "", title));
  if (badgeText) summary.append(el("span", "layer-badge", badgeText));
  details.append(summary);
  return details;
}

function renderGaugeTile(label, rate, subText, emptyLabel, strokeColor) {
  const tile = el("div", "monitor-gauge");
  const valid = Number.isFinite(rate);
  const svg = svgNode("svg", {class: "gauge-svg", viewBox: "0 0 40 40", "aria-hidden": "true"});
  const trackAttrs = {cx: 20, cy: 20, r: 15, fill: "none", stroke: "var(--chart-track)", "stroke-width": 4.5};
  if (!valid) trackAttrs["stroke-dasharray"] = "3 3";
  svg.append(svgNode("circle", trackAttrs));
  if (valid) {
    const circumference = 2 * Math.PI * 15;
    const clamped = Math.max(0, Math.min(1, rate));
    svg.append(svgNode("circle", {
      cx: 20, cy: 20, r: 15, fill: "none",
      stroke: strokeColor, "stroke-width": 4.5, "stroke-linecap": "round",
      "stroke-dasharray": `${(clamped * circumference).toFixed(2)} ${circumference.toFixed(2)}`,
      transform: "rotate(-90 20 20)",
    }));
  }
  const info = el("div", "gauge-info");
  info.append(
    el("span", "gauge-label", label),
    el("span", "gauge-value", valid ? monitorPercent(rate) : emptyLabel),
  );
  if (subText) info.append(el("span", "gauge-sub", subText));
  tile.append(svg, info);
  return tile;
}

function renderTokenChart(summary) {
  const block = el("div", "chart-block");
  const reported = summary?.reported_usage_turns || 0;
  const unavailable = summary?.unavailable_usage_turns || 0;
  const missingCache = summary?.missing_cache_field_turns || 0;
  const head = el("div", "chart-head");
  head.append(
    el("span", "chart-title", "24h Token 构成"),
    el("span", "chart-meta", reported > 0 ? `已上报 ${reported} 轮` : "近 24h"),
  );
  block.append(head);

  if (reported <= 0) {
    const note = unavailable > 0 ? `暂无已上报用量（${unavailable} 轮上游未报告）` : "暂无已上报 Token 用量";
    block.append(el("p", "chart-empty", note));
    return block;
  }

  const other = Number.isFinite(summary.total_input_other) ? summary.total_input_other : 0;
  const hasCached = reported > missingCache && Number.isFinite(summary.total_input_cached);
  const cached = hasCached ? summary.total_input_cached : 0;
  const output = Number.isFinite(summary.total_output) ? summary.total_output : 0;
  const total = other + cached + output;

  if (total <= 0) {
    block.append(el("p", "chart-empty", "已上报轮次用量为 0"));
    return block;
  }

  const width = 300;
  const parts = [
    {key: "input", label: "非缓存输入", value: other, color: "var(--chart-input)"},
    ...(hasCached ? [{key: "cached", label: "缓存读取", value: cached, color: "var(--chart-cached)"}] : []),
    {key: "output", label: "输出", value: output, color: "var(--chart-output)"},
  ];
  const svg = svgNode("svg", {
    class: "token-stack-svg", role: "img", viewBox: `0 0 ${width} 14`, preserveAspectRatio: "none",
    "aria-label": `24h Token 构成：${parts.map(p => `${p.label} ${monitorCount(p.value)}`).join("，")}`,
  });
  svg.append(svgNode("title", {}, parts.map(p => `${p.label} ${monitorCount(p.value)}`).join(" · ")));

  let cursor = 0;
  const active = parts.filter(p => p.value > 0);
  active.forEach((part, index) => {
    const remaining = width - cursor;
    const w = index === active.length - 1
      ? Math.max(0, remaining)
      : Math.min(remaining, Math.max(2, Math.round((part.value / total) * width)));
    if (w > 0) {
      svg.append(svgNode("rect", {x: cursor, y: 0, width: w, height: 14, fill: part.color}));
      cursor += w;
    }
  });
  block.append(svg);

  const legend = el("div", "chart-legend");
  const addLegend = (dotClass, text) => {
    const item = el("span", "legend-item");
    item.append(el("span", `legend-dot ${dotClass}`), el("span", "", text));
    legend.append(item);
  };
  addLegend("input", `非缓存输入 ${monitorCount(other)} (${((other / total) * 100).toFixed(1)}%)`);
  addLegend("cached", hasCached ? `缓存读取 ${monitorCount(cached)} (${((cached / total) * 100).toFixed(1)}%)` : "缓存读取 上游未报告");
  addLegend("output", `输出 ${monitorCount(output)} (${((output / total) * 100).toFixed(1)}%)`);
  block.append(legend);
  return block;
}

function renderLatencyChart(recent) {
  const block = el("div", "chart-block");
  const timed = recent.slice().reverse().filter(turn => Number.isFinite(turn.elapsed_ms));
  const head = el("div", "chart-head");
  head.append(el("span", "chart-title", `最近请求耗时${timed.length ? `（近 ${timed.length} 轮）` : ""}`));

  if (!timed.length) {
    block.append(head, el("p", "chart-empty", "暂无耗时采样"));
    return block;
  }

  const maxMs = Math.max(...timed.map(t => t.elapsed_ms), 1);
  const avgMs = Math.round(timed.reduce((sum, t) => sum + t.elapsed_ms, 0) / timed.length);
  const latestMs = timed[timed.length - 1].elapsed_ms;
  head.append(el("span", "chart-meta", `最新 ${formatDuration(latestMs)} · 均值 ${formatDuration(avgMs)}`));
  block.append(head);

  const width = 300;
  const svg = svgNode("svg", {
    class: "latency-svg", role: "img", viewBox: `0 0 ${width} 60`, preserveAspectRatio: "none",
    "aria-label": `最近 ${timed.length} 轮请求耗时分布，峰值 ${formatDuration(maxMs)}，均值 ${formatDuration(avgMs)}`,
  });
  svg.append(
    svgNode("line", {x1: 0, y1: 29, x2: width, y2: 29, stroke: "var(--line)", "stroke-dasharray": "2 3", "stroke-width": 1}),
    svgNode("line", {x1: 0, y1: 56, x2: width, y2: 56, stroke: "var(--line)", "stroke-width": 1}),
  );

  const slot = width / Math.max(timed.length, 6);
  const bw = Math.min(18, Math.max(4, slot * 0.68));
  timed.forEach((turn, idx) => {
    const barH = Math.max(4, Math.round((turn.elapsed_ms / maxMs) * 50));
    const x = (idx * slot + (slot - bw) / 2).toFixed(1);
    const y = (56 - barH).toFixed(1);
    const color = turn.status === "ok" ? "var(--chart-input)" : "var(--error)";
    const rect = svgNode("rect", {x, y, width: bw.toFixed(1), height: barH, rx: 2, fill: color});
    const statusText = turnStatusLabels[turn.status] || "未知";
    rect.append(svgNode("title", {}, `${formatDuration(turn.elapsed_ms)} · ${statusText}${turn.error_code ? ` (${turn.error_code})` : ""} · ${monitorAgo(turn.recorded_at)}`));
    svg.append(rect);
  });
  block.append(svg);

  const axis = el("div", "chart-axis");
  axis.append(el("span", "", "较早"), el("span", "", `峰值 ${formatDuration(maxMs)}`), el("span", "", "最新"));
  block.append(axis);
  return block;
}

function renderMonitor(data) {
  if (data.schema_version !== 1 || !Array.isArray(data.instances)) throw new Error("monitor schema");
  const cards = data.instances.map(row => {
    const idKey = row.provider_id || "unknown";
    const card = el("article", "monitor-instance");

    const head = el("div", "monitor-instance-head");
    const idGroup = el("div", "monitor-id-group");
    idGroup.append(
      el("h3", "", row.provider_id),
      el("p", "monitor-model", row.model || "未选择模型"),
    );
    const badges = el("div", "monitor-badges");
    const enabledBadge = el("span", "badge", row.enabled ? "已启用" : "已停用");
    if (row.enabled && row.runtime_state === "active") enabledBadge.dataset.state = "ready";
    const modeLabel = row.effective_mode === "tools" ? "工具模式" : row.effective_mode === "chat" ? "纯聊天" : "模式未知";
    badges.append(enabledBadge, el("span", "badge", modeLabel));
    head.append(idGroup, badges);
    card.append(head);

    const recent = Array.isArray(row.recent_turns) ? row.recent_turns : [];
    const pool = row.pool;
    const summary = row.summary;
    const context = row.latest_context || {};

    if (row.runtime_state !== "active") {
      card.append(el("p", "monitor-empty", "已配置未初始化，暂无运行统计。"));
    } else {
      // 第一层：运行概览（默认展开，紧凑双列布局）
      const overview = el("div", "monitor-overview");
      const scopeBar = el("div", "monitor-scope-bar");
      const scopePills = el("div", "scope-pills");
      scopePills.append(
        el("span", "scope-pill", "实时会话池"),
        el("span", "scope-pill", "近 24h (≤100轮)"),
        el("span", "scope-pill", "最近采样"),
      );
      scopeBar.append(scopePills);
      if (recent.length) {
        scopeBar.append(el("span", "", `最近一轮 ${monitorAgo(recent[0].recorded_at)}`));
      }
      overview.append(scopeBar);

      const faults = recent.filter(turn => turn.error_code || turn.status === "error");
      const grid = el("dl", "monitor-overview-grid");
      const activeSub = pool ? `存活进程 ${monitorCount(pool.live_processes)} · 续轮 ${monitorCount(pool.active_tool_turns)}` : "";
      const sentSub = summary && summary.request_sent_turns !== summary.total_turns ? `实际发送 ${monitorCount(summary.request_sent_turns)}` : "";
      const failCount = summary?.failed_turns || 0;
      const latestFaultText = faults.length
        ? (faults[0].error_code ? formatFaultBrief(faults[0].error_code) : "请求异常")
        : recent.length ? "近期无异常" : "暂无记录";
      const latestFaultSub = faults.length
        ? `${monitorAgo(faults[0].recorded_at)}${faults.length > 1 ? ` · 共 ${faults.length} 次` : ""}`
        : "";

      grid.append(
        overviewMetric("活动会话", "实时", pool ? `${monitorCount(pool.active_sessions)} / ${monitorCount(pool.max_sessions)}` : "未知", activeSub),
        overviewMetric("请求轮数", "近 24h", summary ? monitorCount(summary.total_turns) : "未知", sentSub),
        overviewMetric("成功 / 失败", "近 24h", summary ? `${monitorCount(summary.success_turns)} / ${monitorCount(summary.failed_turns)}` : "未知", "", failCount > 0 ? "warn" : ""),
        overviewMetric("最近异常", "近 20 轮", latestFaultText, latestFaultSub, faults.length ? "error" : ""),
      );
      overview.append(grid);

      // 上下文占用（最近单次存活会话有效采样）
      const contextBox = el("div", "monitor-context");
      const ctxHead = el("div", "monitor-context-head");
      const ctxTitle = el("div", "monitor-context-title");
      ctxTitle.append(el("span", "", "上下文占用"), el("span", "scope-pill", "最近采样"));
      const validContext = Number.isFinite(context.context_used) && Number.isFinite(context.context_window_size)
        && context.context_window_size > 0 && context.context_used >= 0 && Boolean(context.observed_at);

      if (validContext) {
        const ratio = context.context_used / context.context_window_size;
        ctxHead.append(ctxTitle, el("span", "monitor-context-val", `${monitorCount(context.context_used)} / ${monitorCount(context.context_window_size)} Token · ${monitorPercent(ratio)}`));
        const progress = el("progress", "monitor-progress");
        progress.max = context.context_window_size;
        progress.value = context.context_used;
        if (ratio >= 0.9) progress.dataset.level = "high";
        else if (ratio >= 0.7) progress.dataset.level = "warn";
        progress.setAttribute("aria-label", "最近采样的存活会话上下文占用");
        contextBox.append(ctxHead, progress, el("p", "small", `${context.model || row.model} · 采样于 ${monitorTime(context.observed_at)}`));
      } else {
        ctxHead.append(ctxTitle, el("span", "monitor-context-val", "上游未报告"));
        contextBox.append(ctxHead);
      }
      overview.append(contextBox);
      card.append(overview);

      // 第二层：详细统计（按需展开）
      const detailsBadge = summary && summary.total_turns > 0 ? `${monitorCount(summary.total_turns)} 轮` : "暂无样本";
      const layer2 = createLayerDetails(`${idKey}::details`, "monitor-details", "详细统计与图表", detailsBadge);
      const layer2Body = el("div", "monitor-layer-body");

      layer2Body.append(renderTokenChart(summary));

      const gauges = el("div", "monitor-gauges");
      const sentTurns = summary?.request_sent_turns || 0;
      const reuseEmpty = sentTurns === 0 ? "暂无样本" : "未知";
      const missingCache = summary?.missing_cache_field_turns || 0;
      const reportedTurns = summary?.reported_usage_turns || 0;
      const cacheEmpty = reportedTurns > 0 && reportedTurns === missingCache ? "未报告缓存字段" : "暂无样本";
      gauges.append(
        renderGaugeTile(
          "会话复用率 (24h)",
          summary?.reuse_rate,
          sentTurns > 0 ? `基于 ${monitorCount(sentTurns)} 次实际发送` : "",
          reuseEmpty,
          "var(--chart-input)",
        ),
        renderGaugeTile(
          "缓存读取占比 (24h)",
          summary?.cache_token_ratio,
          missingCache > 0 ? `${monitorCount(missingCache)} 轮缺缓存字段` : "",
          cacheEmpty,
          "var(--chart-cached)",
        ),
      );
      layer2Body.append(gauges);
      layer2Body.append(renderLatencyChart(recent));

      // 会话重建次数与原因
      const rebuildBlock = el("div", "chart-block");
      const reasons = Array.isArray(summary?.rebuild_reasons) ? summary.rebuild_reasons : [];
      const rebuildHead = el("div", "chart-head");
      rebuildHead.append(el("span", "chart-title", "会话重建原因 (24h)"));
      if (reasons.length) {
        const reasonTotal = reasons.reduce((sum, [, count]) => sum + (Number.isFinite(count) ? count : 0), 0);
        rebuildHead.append(el("span", "chart-meta", `合计 ${monitorCount(reasonTotal)} 次`));
        const chips = el("div", "reason-chips");
        reasons.slice(0, 8).forEach(([reason, count]) => {
          chips.append(el("span", "reason-chip", `${rebuildReasonLabels[reason] || reason} × ${monitorCount(count)}`));
        });
        rebuildBlock.append(rebuildHead, chips);
      } else {
        rebuildBlock.append(rebuildHead, el("p", "chart-empty", "暂无会话重建记录"));
      }
      layer2Body.append(rebuildBlock);

      // 历史请求记录（最近 20 轮）
      const historyBlock = el("div", "chart-block");
      const historyHead = el("div", "chart-head");
      historyHead.append(
        el("span", "chart-title", "历史请求记录"),
        el("span", "chart-meta", recent.length ? `近 ${recent.length} 轮` : "最近 20 轮"),
      );
      if (recent.length) {
        const list = el("ul", "turn-list");
        recent.forEach(turn => {
          const item = el("li", "turn-row");
          const top = el("div", "turn-row-top");
          const statusLabel = turnStatusLabels[turn.status] || "未知";
          const statusClass = turn.status === "ok" ? "tone-ok" : "tone-error";
          const left = el("span", statusClass, `${statusLabel}${turn.error_code ? ` · ${turn.error_code}` : ""}`);
          const right = el("span", "small", `${turn.reused ? "复用" : "新建"} · ${formatDuration(turn.elapsed_ms)} · ${monitorAgo(turn.recorded_at)}`);
          top.append(left, right);

          const meta = el("div", "turn-row-meta");
          const usageText = turn.usage_status === "reported"
            ? `入 ${monitorCount(turn.input_other)} · 缓存 ${turn.cache_fields_present?.cachedReadTokens ? monitorCount(turn.input_cached) : "未报告"} · 出 ${monitorCount(turn.output)}`
            : turn.usage_status === "not_sent" ? "未发送请求" : "用量未报告";
          meta.append(el("span", "", turn.model || row.model || "未知模型"), el("span", "", usageText));
          item.append(top, meta);
          list.append(item);
        });
        historyBlock.append(historyHead, list);
      } else {
        historyBlock.append(historyHead, el("p", "chart-empty", "暂无历史请求记录"));
      }
      layer2Body.append(historyBlock);

      layer2.append(layer2Body);
      card.append(layer2);
    }

    // 第三层：高级诊断（默认折叠）
    const diagLayer = createLayerDetails(`${idKey}::diag`, "monitor-diag", "高级诊断", "原始字段与 429");
    const diagBody = el("div", "monitor-layer-body");

    // 1. CLI 与 Provider 状态
    const providerGroup = el("div", "diag-group");
    providerGroup.append(el("h4", "", "CLI 与 Provider 状态"));
    const pKv = el("dl", "kv-list");
    pKv.append(
      kvItem("provider_id", row.provider_id || "未知"),
      kvItem("runtime_state", `${row.runtime_state || "未知"} (${row.enabled ? "enabled" : "disabled"})`),
      kvItem("tools_mode → effective", `${row.tools_mode || "inherit"} → ${row.effective_mode || "未知"} (tools_enabled=${String(row.tools_enabled)})`),
      kvItem("pool", pool ? `active=${pool.active_sessions}/${pool.max_sessions}, live=${pool.live_processes}, tool_turns=${pool.active_tool_turns}` : "未初始化（无运行时）"),
    );
    if (state?.report) {
      const rep = state.report;
      pKv.append(
        kvItem("CLI 状态与版本", `${rep.status || "未知"} (${stages[rep.stage] || rep.stage || "未知"}) · ${rep.cli_version || "未探测"}`),
        kvItem("进程托管 / 工具桥", `${supervisionLabels[rep.process_supervision] || rep.process_supervision || "未知"} · ${transportLabels[rep.tool_bridge_transport] || rep.tool_bridge_transport || "未知"}`),
      );
    }
    providerGroup.append(pKv);
    diagBody.append(providerGroup);

    // 2. 429 诊断候选信息
    const c429Turns = recent.filter(turn => turn.content_429_candidate);
    const c429Group = el("div", "diag-group");
    c429Group.append(el("h4", "", `429 诊断候选信息 (${c429Turns.length})`));
    if (c429Turns.length) {
      const list = el("ul", "monitor-faults");
      c429Turns.forEach(turn => {
        const c = turn.content_429_candidate;
        const item = el("li", "");
        item.append(
          el("code", "", `${turn.error_code || "429"} · hash=${c.system_hash8 || "未知"} · sys=${monitorCount(c.system_bytes)}B · attempt=${c.attempt ?? "未知"} · output=${c.has_output ? "是" : "否"}`),
          el("span", "small", monitorTime(turn.recorded_at)),
        );
        list.append(item);
      });
      c429Group.append(list);
    } else {
      c429Group.append(el("p", "small", "近 20 轮无 429 候选记录"));
    }
    diagBody.append(c429Group);

    // 3. 详细异常与 Telemetry 原始字段
    const faultTurns = recent.filter(turn => turn.error_code || turn.status !== "ok");
    const faultGroup = el("div", "diag-group");
    faultGroup.append(el("h4", "", `异常与调试记录 (${faultTurns.length})`));
    if (faultTurns.length) {
      const list = el("ul", "monitor-faults");
      faultTurns.forEach(turn => {
        const item = el("li", "");
        const reasonsText = Array.isArray(turn.rebuild_reasons) && turn.rebuild_reasons.length ? turn.rebuild_reasons.join(",") : "none";
        item.append(
          el("code", "", `${turn.status}${turn.error_code ? `/${turn.error_code}` : ""} · ${formatDuration(turn.elapsed_ms)} · prompt=${monitorCount(turn.prompt_bytes)}B replay=${monitorCount(turn.replay_bytes)}B omit=${monitorCount(turn.omitted_messages)} · reasons=${reasonsText}`),
          el("span", "small", monitorTime(turn.recorded_at)),
        );
        list.append(item);
      });
      faultGroup.append(list);
    } else {
      faultGroup.append(el("p", "small", "近 20 轮无异常或中断记录"));
    }
    diagBody.append(faultGroup);

    const teleGroup = el("div", "diag-group");
    teleGroup.append(el("h4", "", "Telemetry 详细字段"));
    const tKv = el("dl", "kv-list");
    tKv.append(
      kvItem("usage 覆盖 (24h)", summary ? `reported=${summary.reported_usage_turns}, unavailable=${summary.unavailable_usage_turns}, missing_cache=${summary.missing_cache_field_turns}` : "未初始化"),
      kvItem("Token 汇总 (24h)", summary ? `other=${summary.total_input_other ?? "null"}, cached=${summary.total_input_cached ?? "null"}, output=${summary.total_output ?? "null"}` : "未初始化"),
      kvItem("比率字段 (24h)", summary ? `reuse_rate=${monitorPercent(summary.reuse_rate)}, cache_token_ratio=${monitorPercent(summary.cache_token_ratio)}` : "未初始化"),
      kvItem("latest_context", `source=${context.source || "not_reported"}, scope=${context.scope || "latest_live_session"}, used=${context.context_used ?? "null"}/${context.context_window_size ?? "null"}`),
    );
    teleGroup.append(tKv);
    teleGroup.append(el("p", "small", "注：接口仅下发近 24h（≤100 轮）窗口统计与脱敏错误码（error_code），不含启动以来持久化累计及原始 CLI stderr/RPC 报文；深度排障请执行 /agy doctor。"));
    diagBody.append(teleGroup);

    diagLayer.append(diagBody);
    card.append(diagLayer);

    return card;
  });

  $("monitor-instances").replaceChildren(...cards);
  $("monitor").dataset.stale = "false";
  badge("monitor-badge", "本地快照", cards.length ? "ready" : "");
  $("monitor-message").textContent = cards.length ? "" : "尚未配置 AGY 实例，请在下方完成模型配置。";
  $("monitor-updated").textContent = `快照 ${monitorShortTime(data.sampled_at)} · 10s 自动刷新`;
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
      : monitorSnapshot ? "监控读取失败，以下为过期快照。" : "暂时无法读取监控数据，请稍后刷新。";
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

  $("connection").textContent = busy ? "正在同步…" : "已连接 AstrBot";
  $("environment").textContent = `${data.environment.system} · Python ${data.environment.python}${data.environment.container ? " · 容器环境" : ""}`;
  $("discovery-hint").textContent = data.environment.discovery_hint || "";
  $("install-hint").textContent = data.environment.install_hint;
  $("install-url").value = data.environment.install_url;
  $("install-command").value = data.environment.install_command;

  // 默认仅展示 CLI 版本、可用模型数量，以及失败时的退出码与阶段
  $("facts").replaceChildren(...[
    r.cli_version ? `CLI ${r.cli_version}` : null,
    r.model_count != null ? `${r.model_count} 个可用模型` : null,
    Number.isInteger(r.exit_code) ? `CLI 退出码：${r.exit_code}` : null,
    r.error_code && stages[r.stage] ? `失败阶段：${stages[r.stage]}` : null,
  ].filter(Boolean).map(text => el("span", "fact", text)));

  // 高级诊断明细移入 env-diag-list
  const caps = r.advertised_capabilities;
  const capSummary = caps
    ? `MCP(http=${Boolean(caps.mcpCapabilities?.http)}, sse=${Boolean(caps.mcpCapabilities?.sse)}) · 模态(img=${Boolean(caps.promptCapabilities?.image)}, audio=${Boolean(caps.promptCapabilities?.audio)})`
    : null;
  $("env-diag-list").replaceChildren(...[
    r.customizations_mode ? kvItem("组件模式", r.customizations_mode) : null,
    r.cli_flags ? kvItem("精简参数", `${r.cli_flags.include_default_customizations ? "支持" : "未探测到"} · 禁用遥测=${r.cli_flags.disable_telemetry ? "支持" : "不支持"}`) : null,
    r.minimal_tools_status ? kvItem("精简状态", minimalToolsLabels[r.minimal_tools_status] || r.minimal_tools_status) : null,
    r.tool_bridge_transport ? kvItem("工具桥 / Socket", transportLabels[r.tool_bridge_transport] || "未知") : null,
    r.process_supervision ? kvItem("进程托管", supervisionLabels[r.process_supervision] || "未知") : null,
    r.proxy_mode ? kvItem("代理配置", `${r.proxy_mode}${data.environment.proxy_mode_note ? `（${data.environment.proxy_mode_note}）` : ""}`) : null,
    r.cli_update_status ? kvItem("CLI 更新策略", "手动管理，不检查或覆盖全局 CLI") : null,
    r.auth_bridge !== undefined ? kvItem("认证桥接", r.auth_bridge ? "开启" : "关闭") : null,
    capSummary ? kvItem("ACP 能力声明", capSummary) : null,
  ].filter(Boolean));

  const errorLabels = {NOT_INSTALLED: "未找到 CLI", COMMAND_NOT_FOUND: "配置路径无效", STARTUP_EXIT: "握手前退出", CLI_WRAPPER: "启动脚本不兼容"};
  badge("check-badge", data.busy ? stages[r.stage] || "正在检测" : passed ? "环境就绪" : errorLabels[r.error_code] || r.error_code || "等待检测", passed ? "ready" : r.error_code ? "error" : "");
  $("check-hint").textContent = data.busy
    ? `正在${stages[r.stage] || "检查环境"}…`
    : passed
      ? (r.minimal_tools_status === "unsupported" ? data.hint : "")
      : data.hint;
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
    : passed ? ""
    : login.state === "failed" ? `登录未完成（${login.error_code}），请重试或使用终端登录。`
    : "请启动官方登录并在浏览器完成授权。";
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
    $("effort").replaceChildren(new Option(`跟随全局（${data.global_effort}）`, "default"), ...efforts.map(e => new Option({low: "低", medium: "中", high: "高"}[e], e)));
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
  $("finish-hint").textContent = data.result?.default_selected
    ? "已同步设为全局默认模型。"
    : configured.length ? "" : "保存模型后将在此生成会话切换命令。";

  const providerKey = JSON.stringify(configured);
  if (providerSignature !== providerKey) {
    $("configured").replaceChildren(...configured.map(row => {
      const wrapper = el("div", "provider");
      const pHead = el("div", "provider-head");
      const title = el("strong", "", row.model);
      const meta = el("span", "badge", `${toolsModeLabels[row.tools_mode] || "跟随全局"} · ${row.enabled ? "已启用" : "已停用"}`);
      if (row.enabled) meta.dataset.state = "ready";
      pHead.append(title, meta);

      const line = el("div", "copy-row");
      const input = document.createElement("input");
      input.readOnly = true;
      input.value = `/agy use ${row.id}`;
      input.setAttribute("aria-label", "切换模型命令");
      const button = el("button", "btn btn-sm", "复制命令");
      button.type = "button";
      button.addEventListener("click", () => copy(input));
      line.append(input, button);
      wrapper.append(pHead, line);
      return wrapper;
    }));
    providerSignature = providerKey;
  }
  if (data.action_error) showError(`${data.action_error}：${data.action_hint}`);
  const nextDiagSig = JSON.stringify([r.status, r.stage, r.cli_version, r.process_supervision, r.tool_bridge_transport]);
  if (monitorSnapshot && nextDiagSig !== reportDiagSignature) {
    reportDiagSignature = nextDiagSig;
    renderMonitor(monitorSnapshot);
  } else {
    reportDiagSignature = nextDiagSig;
  }
}

async function action(name, body = {}) {
  if (!state || pending) return;
  revision++;
  pending = true; showError(); render(state);
  try {
    const data = await bridge.apiPost(`setup/${name}`, {...body, csrf: state.csrf});
    pending = false; render(data);
  } catch (error) {
    pending = false; render(state); showError(error.message || "请求失败，请刷新页面。");
  }
}

wireHelpPopover("monitor-help-btn", "monitor-help");
wireHelpPopover("model-help-btn", "model-help");
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
  const code = $("auth-code").value; $("auth-code").value = "";
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

if (fallbackPopovers.length) {
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    for (const item of fallbackPopovers) {
      item.pop.hidden = true;
      item.pop.classList?.remove("is-open");
      item.btn.setAttribute("aria-expanded", "false");
    }
  });
}

window.addEventListener("pagehide", () => {
  disposed = true;
  clearTimeout(timer);
  clearTimeout(monitorTimer);
  monitorSnapshot = undefined;
  $("auth-code").value = "";
  $("auth-url").value = "";
  state = null;
});

if (!bridge) {
  showError("请从 AstrBot WebUI 侧边栏或插件详情打开 Antigravity ACP 面板。");
  badge("monitor-badge", "不可用");
  $("monitor-message").textContent = "请从 AstrBot 管理面板打开运行监控。";
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
