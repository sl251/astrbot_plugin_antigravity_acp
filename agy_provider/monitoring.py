"""Dashboard-admin snapshots; GET never starts a CLI or queries remote quota."""

import sqlite3
from datetime import datetime, timezone

from .onboarding import PLUGIN_NAME
from .routing import dashboard_admin, routing_snapshot
from .telemetry import MAX_HISTORY_TURNS, daily_snapshot, failure_snapshot
from .web_errors import WEB_HEADERS, web_error


class Monitoring:
    def __init__(self, context, quota=None):
        self.context = context
        self.quota = quota
        self.closed = False
        self.routes = []

    def register(self):
        for resource in ("status", "turns", "quota", "daily/30", "daily/90", "daily/365"):
            async def handler(resource=resource):
                return await self.handle(resource)

            route = f"/{PLUGIN_NAME}/monitor/{resource}"
            self.context.register_web_api(route, handler, ["GET"], "AGY 运行概览")
            self.routes.append((route, handler))

    async def handle(self, resource="status"):
        from astrbot.api.web import json_response, request
        from .provider import instance_monitor_snapshots, instance_turn_history_snapshots

        # AstrBot authenticates the dashboard session before dispatch. Its
        # dashboard has one configured admin username, not a chat-platform ID.
        # Check that identity again; unknown host auth configuration fails closed.
        username = request.username
        if not username:
            return web_error("UNAUTHENTICATED", "请先登录 AstrBot 管理面板。", 401)
        manager = self.context.provider_manager
        if not dashboard_admin(manager, username):
            return web_error("FORBIDDEN", "仅 AstrBot 面板管理员可查看运行概览。", 403)
        if self.closed:
            return web_error("CLOSED", "插件已停用。", 503)
        try:
            if resource == "quota":
                if self.quota is None:
                    return web_error("QUOTA_UNAVAILABLE", "额度服务尚未就绪。", 503)
                if self.quota.closed:
                    return web_error("CLOSED", "插件已停用。", 503)
                return json_response(self.quota.snapshot(), headers=WEB_HEADERS)
            if resource == "turns":
                # 只读内存中的历史窗口快照（含重启后恢复的近 24 小时明细），不触发任何 CLI 或 I/O。
                payload = {
                    "schema_version": 1,
                    "sampled_at": datetime.now(timezone.utc).isoformat(),
                    "window": {"hours": 24, "max_turns_per_instance": MAX_HISTORY_TURNS},
                    "instances": instance_turn_history_snapshots(manager),
                }
                return json_response(payload, headers=WEB_HEADERS)
            if resource.startswith("daily/"):
                try:
                    payload = daily_snapshot(int(resource.split("/")[1]))
                except (OSError, sqlite3.Error):
                    return web_error("STORAGE_UNAVAILABLE", "每日统计存储不可用。", 503)
                return json_response(payload, headers=WEB_HEADERS)
            # No await within collection: all runtime/telemetry mutations run
            # on the same event loop, so these fields share one consistent view.
            payload = {
                "schema_version": 2,
                "routing": routing_snapshot(manager),
                **failure_snapshot(),
                "sampled_at": datetime.now(timezone.utc).isoformat(),
                "window": {"hours": 24, "max_turns_per_instance": 100},
                "instances": instance_monitor_snapshots(manager),
            }
            return json_response(payload, headers=WEB_HEADERS)
        except Exception:
            return web_error("INTERNAL", "暂时无法读取监控数据。", 500)

    async def close(self):
        self.closed = True
        registered = self.context.registered_web_apis
        registered[:] = [row for row in registered if not any(
            row[0] == route and row[1] is handler for route, handler in self.routes
        )]
        self.routes.clear()
