"""Read-only, dashboard-admin monitoring; never starts or probes a runtime."""

from datetime import datetime, timezone

from .onboarding import PLUGIN_NAME


class Monitoring:
    def __init__(self, context):
        self.context = context
        self.closed = False
        self.routes = []

    def register(self):
        route = f"/{PLUGIN_NAME}/monitor/status"
        handler = self.handle
        self.context.register_web_api(route, handler, ["GET"], "AGY 运行概览")
        self.routes.append((route, handler))

    async def handle(self):
        from astrbot.api.web import error_response, json_response, request
        from .provider import instance_monitor_snapshots

        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        # AstrBot authenticates the dashboard session before dispatch. Its
        # dashboard has one configured admin username, not a chat-platform ID.
        # Check that identity again; unknown host auth configuration fails closed.
        username = request.username
        if not username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401, headers=headers)
        manager = self.context.provider_manager
        try:
            admin = manager.acm.default_conf["dashboard"]["username"]
        except (AttributeError, KeyError, TypeError):
            admin = None
        if (not isinstance(username, str) or username.startswith("api_key:")
                or not isinstance(admin, str) or not admin or username != admin):
            return error_response("仅 AstrBot 面板管理员可查看运行概览。", status_code=403, headers=headers)
        if self.closed:
            return error_response("插件已停用。", status_code=503, headers=headers)
        try:
            # No await within collection: all runtime/telemetry mutations run
            # on the same event loop, so these fields share one consistent view.
            payload = {
                "schema_version": 1,
                "sampled_at": datetime.now(timezone.utc).isoformat(),
                "window": {"hours": 24, "max_turns_per_instance": 100},
                "instances": instance_monitor_snapshots(manager),
            }
            return json_response(payload, headers=headers)
        except Exception:
            return error_response("暂时无法读取监控数据。", status_code=500, headers=headers)

    async def close(self):
        self.closed = True
        registered = self.context.registered_web_apis
        registered[:] = [row for row in registered if not any(
            row[0] == route and row[1] is handler for route, handler in self.routes
        )]
        self.routes.clear()
