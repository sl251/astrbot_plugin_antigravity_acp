"""聊天管理员指令鉴权与人类可读输出格式化。"""

from datetime import date, datetime, timedelta
from functools import wraps

HELP = """AGY 管理员指令
/agy status - 运行状态
/agy quota - 账号额度
/agy usage [30|90|365] - 每日用量
/agy cost - 成本与复用
/agy models - 已配置模型
/agy use <ID> - 切换会话模型
/agy doctor - 环境与连接自检
/agy setup - 面板入口与实例
/agy help - 显示此帮助"""

_WINDOW_LABELS = {
    "weekly": "周额度",
    "5h": "5h额度",
    "daily": "日额度",
    "monthly": "月额度",
}

_MODE_LABELS = {
    "chat": "纯聊天",
    "tools": "工具模式",
    "inherit": "跟随总闸",
}

_STAGE_LABELS = {
    "discovery": "查找 CLI",
    "isolation": "检查隔离目录",
    "authentication": "检查认证",
    "initialize": "ACP 握手",
    "session/new": "验证登录",
    "model_catalog": "读取模型目录",
    "model_selection": "检查模型档位",
    "complete": "检查通过",
}

_PROXY_LABELS = {
    "inherit": "继承环境",
    "custom": "自定义代理",
    "direct": "直连",
}


def mode_label(mode):
    """将工具模式代码转为简洁中文标签。"""
    return _MODE_LABELS.get(mode, mode or "未知")


def admin_command(handler):
    """在命令匹配后、读取任何插件状态前校验 AstrBot 管理员权限。"""

    @wraps(handler)
    async def checked(plugin, event, *args, **kwargs):
        # 4.29.4 的 ProcessStage 仅在 not event.call_llm 时分发，此处需先放行。
        event.should_call_llm(True)
        if not event.is_admin():
            try:
                await event.send(event.plain_result("AGY [FORBIDDEN] 仅管理员可使用此指令。"))
            finally:
                event.stop_event()
            return
        async for result in handler(plugin, event, *args, **kwargs):
            yield result

    return checked


def _parse_local(iso_text):
    """解析带时区的时间戳并转换为宿主本地时间。"""
    if not isinstance(iso_text, str) or not iso_text.strip():
        return None
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo else parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)


def _format_local_stamp(local_dt, ref_dt):
    """同一天显示 HH:MM，跨日显示 MM/DD HH:MM（避免连字符触发客户端日期链接）。"""
    if local_dt.date() == ref_dt.date():
        return local_dt.strftime("%H:%M")
    return local_dt.strftime("%m/%d %H:%M")


def _format_reset(reset_time, ref_dt):
    """将重置时间格式化为本地倒计时加简明时刻。"""
    if not reset_time:
        return "不适用"
    local_dt = _parse_local(reset_time)
    if local_dt is None:
        return str(reset_time)
    stamp = _format_local_stamp(local_dt, ref_dt)
    delta_secs = (local_dt - ref_dt).total_seconds()
    if delta_secs <= 0:
        return f"已到重置 ({stamp})"
    total_mins = max(1, round(delta_secs / 60))
    days, rem = divmod(total_mins, 1440)
    hours, mins = divmod(rem, 60)
    if days > 0:
        countdown = f"{days}天{hours}小时" if hours > 0 else f"{days}天"
    elif hours > 0:
        countdown = f"{hours}小时{mins}分" if mins > 0 else f"{hours}小时"
    else:
        countdown = f"{mins}分钟"
    return f"{countdown}后 ({stamp})"


def _format_pct(fraction):
    """格式化剩余比例，整数比例省略无效小数零。"""
    if not isinstance(fraction, (int, float)):
        return "未知"
    pct = max(0.0, min(1.0, float(fraction))) * 100
    rounded = round(pct)
    return f"{rounded}%" if abs(pct - rounded) < 0.05 else f"{pct:.1f}%"


def _format_count(value):
    """格式化整数统计值，未知保留「未知」。"""
    return f"{value:,}" if isinstance(value, int) else "未知"


def quota_text(data, *, now=None):
    """将官方 CLI 额度快照格式化为移动端可读的中文文本。"""
    ref_dt = now if now is not None else datetime.now().astimezone()
    fetched_dt = _parse_local(data.get("fetched_at"))
    fetched_str = _format_local_stamp(fetched_dt, ref_dt) if fetched_dt else ""
    stale = bool(data.get("stale"))

    if stale and fetched_str:
        header = f"AGY 账号额度（缓存已过期 · {fetched_str}）"
    elif fetched_str:
        header = f"AGY 账号额度（{fetched_str} 更新）"
    elif stale:
        header = "AGY 账号额度（缓存已过期）"
    else:
        header = "AGY 账号额度"

    lines = [header]
    if data.get("error"):
        lines.append(f"提示：{data['error']['message']}")
    groups = data.get("groups") or []
    for group in groups:
        lines.append("")
        lines.append(group["name"])
        for bucket in group["buckets"]:
            win = _WINDOW_LABELS.get(bucket["window"], bucket["window"])
            pct = _format_pct(bucket.get("remaining_fraction"))
            reset = _format_reset(bucket.get("reset_time"), ref_dt)
            lines.append(f"• {win} {pct} · 重置 {reset}")
    if not groups and not data.get("error"):
        lines.append("暂无可用额度数据。")
    return "\n".join(lines)


def usage_text(data):
    """将本地每日用量快照格式化为精简可读的统计与 28 天热力图。"""
    days = data["days"]
    if not days:
        return "所选区间没有本地用量记录。"

    def known(key):
        counts = [day[key] for day in days if day[key] is not None]
        return sum(counts) if counts else None

    requests_total = known("requests")
    success_total = known("success")
    failed_total = known("failed")
    unknown_turns = known("usage_unknown_turns")
    lines = [
        f"AGY 本地用量 · 近 {data['range']['days']} 天",
        f"{len(days)} 天有记录 · {_format_count(requests_total)} 轮"
        f"（成功 {_format_count(success_total)} / 失败 {_format_count(failed_total)}）",
        f"Token：输入 {_format_count(known('input_tokens'))} · "
        f"输出 {_format_count(known('output_tokens'))} · "
        f"合计 {_format_count(known('total_tokens'))}",
    ]
    if isinstance(unknown_turns, int) and unknown_turns > 0:
        lines.append(f"另有 {unknown_turns} 轮用量未知")

    end = date.fromisoformat(data["range"]["end"])
    indexed = {row["date"]: row for row in days}
    cells = []
    for offset in range(27, -1, -1):
        key = (end - timedelta(days=offset)).isoformat()
        row = indexed.get(key)
        if row is None:
            cell = "·"
        elif row["total_tokens"] is None or (row["usage_unknown_turns"] and row["total_tokens"] == 0):
            cell = "?"
        elif row["total_tokens"] == 0:
            cell = "0"
        elif row["total_tokens"] < 1000:
            cell = "░"
        elif row["total_tokens"] < 10000:
            cell = "▒"
        else:
            cell = "▓"
        cells.append(cell)

    lines.extend([
        "近 28 天热力图（左早右晚）：",
        "".join(cells),
        "·无 ?未知 0零 ░<1千 ▒<1万 ▓≥1万",
        "近期记录：",
    ])
    for day in days[-7:]:
        parsed_day = date.fromisoformat(day["date"])
        day_label = parsed_day.strftime("%m/%d") if parsed_day.year == end.year else parsed_day.strftime("%Y/%m/%d")
        count = day["total_tokens"]
        detail = f"{count:,} Token" if isinstance(count, int) else "Token 未知"
        if day["usage_unknown_turns"] and count is not None:
            detail += "（部分未知）"
        lines.append(f"{day_label} · {day['requests']} 轮 · {detail}")
    return "\n".join(lines)


def doctor_text(report, hint):
    """将无生成自检报告格式化为人类可读的中文摘要，不输出原始 JSON。"""
    passed = report.get("status") in {"passed", "ready"}
    stage = _STAGE_LABELS.get(report.get("stage"), report.get("stage") or "未知阶段")
    error_code = report.get("error_code")
    exit_code = report.get("exit_code")

    if passed:
        lines = ["AGY 自检通过（未发送对话）"]
    else:
        detail = f"{stage} · {error_code}" if error_code else stage
        if isinstance(exit_code, int):
            detail += f" · 退出码 {exit_code}"
        lines = [f"AGY 自检未通过（{detail}）"]

    cli_ver = report.get("cli_version") or "未检测到"
    model_count = report.get("model_count")
    env_line = f"CLI：{cli_ver}"
    if isinstance(model_count, int):
        env_line += f" · 可用模型 {model_count} 个"
    lines.append(env_line)

    auth_text = "开启" if report.get("auth_bridge") else "关闭"
    proxy_text = _PROXY_LABELS.get(report.get("proxy_mode"), report.get("proxy_mode") or "未知")
    lines.append(f"认证与代理：桥接{auth_text} · {proxy_text}")

    caps = report.get("advertised_capabilities") or {}
    if isinstance(caps, dict) and caps:
        img_ok = bool((caps.get("promptCapabilities") or {}).get("image"))
        mcp_caps = caps.get("mcpCapabilities") or {}
        mcp_list = [name for name in ("http", "sse") if mcp_caps.get(name)]
        mcp_text = "/".join(mcp_list) if mcp_list else "无"
        lines.append(f"能力支持：识图{'支持' if img_ok else '不支持'} · MCP({mcp_text})")

    read_text = "支持" if report.get("verified_read") else "未验证"
    custom_text = "精简" if report.get("customizations_mode") == "minimal" else "标准"
    lines.append(f"安全与组件：内置读文件{read_text} · 默认组件{custom_text}")

    routing = report.get("routing") or {}
    if isinstance(routing, dict) and routing:
        reason = routing.get("resolution_reason")
        primary = routing.get("configured_primary_id")
        resolved = routing.get("resolved_primary_id")
        if reason == "configured_primary":
            route_desc = f"{resolved}（生效中）"
        elif reason == "implicit_first_provider":
            route_desc = f"异常（{primary or '未设'} 未命中，回退至 {resolved or '无'}）"
        elif reason == "missing_primary":
            route_desc = "未设置默认模型"
        elif reason == "non_local_runner":
            route_desc = "非本地 runner"
        else:
            route_desc = "未知"
        fb_count = len(routing.get("explicit_fallback_ids") or [])
        if fb_count:
            route_desc += f" · 备用 {fb_count} 项"
        lines.append(f"默认路由：{route_desc}")

    if hint:
        lines.append(f"说明：{hint}")
    return "\n".join(lines)


def setup_text(busy, hint, rows):
    """格式化 /agy setup 的面板入口与模型实例引导信息。"""
    lines = [
        "AGY 配置入口",
        "WebUI：插件 → Antigravity ACP → 首次设置",
        f"状态：{'检测或登录正在进行，请稍候。' if busy else hint}",
    ]
    if rows:
        lines.append("")
        lines.append(f"已配置模型（{len(rows)} 个）：")
        for row in rows:
            tags = []
            if row.get("is_default"):
                tags.append("默认")
            tags.append("启用" if row.get("enabled") else "停用")
            tags.append(mode_label(row.get("tools_mode", "inherit")))
            lines.append(f"• {row.get('model') or row['id']} [{' · '.join(tags)}]")
            lines.append(f"  ID: {row['id']}")
        lines.append("切换本会话：/agy use <ID>")
    return "\n".join(lines)


def status_text(version, rows):
    """格式化 /agy status 的实例池与活跃会话概览。"""
    lines = [f"AGY v{version} · 已加载 {len(rows)} 个实例"]
    if not rows:
        lines.append("尚未启用模型，可用 /agy models 查看或前往面板配置。")
        return "\n".join(lines)
    for row in rows:
        pool = row.get("pool") or {}
        active = pool.get("active_sessions", 0)
        live = pool.get("live_processes", 0)
        mode = mode_label(row.get("effective_mode") or row.get("tools_mode", "inherit"))
        model = row.get("model") or row["provider_id"]
        lines.append(f"• {model} [{mode}]")
        lines.append(f"  ID: {row['provider_id']} · {active} 会话 / {live} 进程")
    return "\n".join(lines)


def models_text(rows):
    """格式化 /agy models 的已配置实例与切换命令。"""
    if not rows:
        return "尚未配置模型，请运行 /agy setup。"
    lines = [f"AGY 已配置模型（{len(rows)} 个）"]
    for row in rows:
        tags = []
        if row.get("is_default"):
            tags.append("默认")
        tags.append("启用" if row.get("enabled") else "停用")
        tags.append(mode_label(row.get("tools_mode", "inherit")))
        lines.append(f"• {row.get('model') or row['id']} [{' · '.join(tags)}]")
        lines.append(f"  切换：/agy use {row['id']}")
    return "\n".join(lines)


def use_text(provider_id, row):
    """格式化 /agy use 切换成功提示。"""
    model = row.get("model") or provider_id
    mode = mode_label(row.get("tools_mode", "inherit"))
    return f"当前会话已切换：{model}（{mode}）\n实例 ID：{provider_id}"
