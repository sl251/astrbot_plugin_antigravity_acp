"""First-run orchestration using AstrBot's authenticated plugin views."""

import asyncio
import hashlib
import os
import platform
import secrets
from copy import deepcopy
from pathlib import Path

from .errors import AgyError
from .login import LoginFlow
from .paths import plugin_data_root
from .probe import probe


PLUGIN_NAME = "astrbot_plugin_antigravity_acp"
HINTS = {
    "NOT_INSTALLED": "在运行 AstrBot 的环境中安装官方 CLI，随后重新检测；已安装时可在插件配置填写 agy 的绝对路径。",
    "AUTH": "点击「开始官方登录」。终端兜底：用运行 AstrBot 的同一用户执行 agy acp --login，再重新检测。",
    "AUTH_BRIDGE": "检查官方认证文件及父目录权限。Windows 文件桥需要符号链接权限；若使用系统钥匙串，可在插件配置关闭 auth_bridge 后重载并检测。",
    "CONFIG": "检查插件配置中的 CLI 路径、代理模式和代理地址；保存后重载插件。",
    "PERMISSION": "当前 AstrBot 用户无权执行 CLI 或访问所需目录，请检查安装用户和文件权限。",
    "LAUNCH": "检查 CLI 是否为当前系统的原生可执行文件及 Python 子进程支持。Windows 使用 agy.exe。",
    "UNSUPPORTED": "检查官方 CLI 是否支持 ACP、隔离启动及当前模型档位。升级 CLI 后重新检测，或把思考档位改为 default。",
    "PROXY_AUTH": "代理认证失败，请检查插件代理配置或 AstrBot 服务的代理环境变量。",
    "PROXY": "检查代理是否启动、地址是否可达。Docker 中的 127.0.0.1 是容器自身。",
    "NETWORK": "检查 AstrBot 所在环境的 DNS、网络出口和代理，再重新检测。",
    "TLS": "检查系统时间、证书链和代理证书。",
    "TIMEOUT": "操作超时，请检查网络和 CLI 状态；需要时调整插件超时配置后重载。",
    "NO_MODELS": "当前账号没有返回可用 Gemini 模型，请检查登录账号和模型权限。",
    "MODEL": "模型目录可能已变化，请重新检测并选择当前返回的模型。",
    "RATE_LIMIT": "上游请求受限，请稍后再试。",
    "QUOTA": "上游额度不足，请在官方客户端检查额度。",
    "LOGIN_URL": "未取得授权链接，请在同一运行环境执行 agy acp --login，然后重新检测。",
    "HOST": "提供商配置未完成，请检查 AstrBot 模型页面；已创建的实例会保留，重试不会重复创建。",
    "LOCAL": "本地操作未完成，请检查数据目录权限、宿主版本和运行环境，再重新检测。",
}


def next_step(report):
    if report.get("status") == "passed":
        return "环境与模型目录检查通过。选择模型并保存即可配置；此检查没有发送对话。"
    return HINTS.get(report.get("error_code"), "请重新检测；仍失败时用 /agy doctor 获取脱敏诊断。")


def environment():
    container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    return {
        "system": platform.system(), "python": platform.python_version(),
        "container": container, "proxy_mode_note": "登录与对话共用插件代理设置",
        "install_url": "https://antigravity.google/docs/getting-started",
        "install_command": "irm https://antigravity.google/cli/install.ps1 | iex" if os.name == "nt" else "curl -fsSL https://antigravity.google/cli/install.sh | bash",
        "install_hint": (
            "请在容器内部、以 AstrBot 的运行用户安装并登录 CLI；持久保存官方认证目录。宿主机安装不等于容器可用。"
            if container else
            "Windows：安装官方原生 agy.exe。自动搜索 PATH、%LOCALAPPDATA%\\agy\\bin 和用户 .local\\bin。"
            if os.name == "nt" else
            "Linux / macOS：按官方 CLI 安装指引操作。自动搜索 PATH 和 ~/.local/bin/agy；系统服务须使用同一运行用户。"
        ),
    }


class Onboarding:
    def __init__(self, context, settings):
        self.context, self.settings = context, settings
        self.root = plugin_data_root()
        self.csrf = secrets.token_urlsafe(32)
        self.closed = False
        self.job = None
        self.operation = ""
        self.report = {"status": "pending", "stage": "discovery"}
        self.result = None
        self.action_error = ""
        self.login = LoginFlow(settings, self.root, self._check)
        self.routes = []

    @property
    def busy(self):
        return self.job is not None and not self.job.done()

    def _available(self):
        if self.closed:
            raise AgyError("CLOSED", "插件已停用，请启用或刷新页面。")
        if self.busy or self.login.busy:
            raise AgyError("BUSY", "检测、登录或保存正在进行，请等待完成。")

    def providers(self):
        from .provider import PROVIDER_TYPE

        manager = self.context.provider_manager
        rows = []
        for config in list(manager.providers_config):
            merged = manager.get_merged_provider_config(config)
            if merged.get("type") == PROVIDER_TYPE:
                rows.append({
                    "id": config.get("id"),
                    "model": merged.get("model") or (merged.get("model_config") or {}).get("model", ""),
                    "enabled": bool(config.get("enable")),
                })
        return rows

    def snapshot(self, owner=""):
        return {
            "csrf": self.csrf, "busy": self.busy, "operation": self.operation,
            "report": deepcopy(self.report), "hint": next_step(self.report),
            "environment": environment(), "login": self.login.snapshot(owner),
            "providers": self.providers(), "result": self.result,
            "action_error": self.action_error,
            "action_hint": HINTS.get(self.action_error, "操作未完成，请重新检测或查看模型配置。") if self.action_error else "",
            "global_effort": self.settings.thought_level,
        }

    async def _check(self, model="", effort="default"):
        self.report = {"status": "checking", "stage": "discovery"}

        def progress(report):
            report["status"] = "checking"
            self.report = report

        self.report = await probe(self.settings, details=True, model=model, effort=effort, progress=progress)
        return self.report

    def start_check(self, model=""):
        self._available()
        self._launch("check", lambda: self._check(model))

    def _launch(self, operation, work):
        self.operation, self.action_error, self.result = operation, "", None

        async def run():
            try:
                await work()
            except asyncio.CancelledError:
                raise
            except AgyError as exc:
                self.action_error = exc.code
            except Exception:
                self.action_error = "HOST" if operation == "apply" else "LOCAL"
            finally:
                self.operation = ""

        self.job = asyncio.create_task(run(), name="agy:setup-" + operation)

    def start_apply(self, model, effort, use_default):
        self._available()
        if not isinstance(effort, str) or effort not in {"default", "low", "medium", "high"} or type(use_default) is not bool:
            raise AgyError("CONFIG", "模型档位或切换选项无效。")
        if model not in {row["id"] for row in self.report.get("models", [])}:
            raise AgyError("MODEL", "请先检测并选择当前目录中的模型。")
        self._launch("apply", lambda: self._apply(model, effort, use_default))

    async def _apply(self, model, effort, use_default):
        # Always revalidate model and effort before changing host configuration.
        actual_effort = self.settings.thought_level if effort == "default" else effort
        report = await self._check(model, actual_effort)
        if report.get("status") != "passed":
            raise AgyError(report.get("error_code", "LOCAL"), "配置前检查未通过。")
        # Host create/update persist configuration around awaited reloads.
        # Finish that critical section before plugin shutdown removes adapters.
        commit = asyncio.create_task(self._save_provider(model, effort, use_default), name="agy:save-provider")
        cancelled = None
        while True:
            try:
                await asyncio.shield(commit)
                break
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
                if commit.done():
                    break
        commit.result()
        if cancelled:
            raise cancelled

    async def _save_provider(self, model, effort, use_default):
        from astrbot.core.provider.entities import ProviderType
        from .provider import DEFAULT_CONFIG, PROVIDER_TYPE

        manager = self.context.provider_manager
        existing = None
        for candidate in list(manager.providers_config):
            merged = manager.get_merged_provider_config(candidate)
            candidate_model = merged.get("model") or (merged.get("model_config") or {}).get("model", "")
            candidate_effort = (merged.get("custom_extra_body") or {}).get("agy_thought_level", "default")
            if merged.get("type") == PROVIDER_TYPE and candidate_model == model and candidate_effort == effort:
                existing = candidate
                break
        if existing is not None:
            provider_id = existing["id"]
            # A failed host save can leave a loaded, enabled instance only in
            # memory. Re-persist on explicit save, including retries/reloads,
            # before reporting success. Preserve all existing custom fields.
            config = deepcopy(existing)
            config["enable"] = True
            await manager.update_provider(provider_id, config)
        else:
            suffix = hashlib.sha256((model + "\0" + effort).encode()).hexdigest()[:12]
            base_id = "agy_setup_" + suffix
            provider_id = base_id
            occupied = {row.get("id") for row in manager.providers_config}
            index = 2
            while provider_id in occupied:
                provider_id = f"{base_id}_{index}"
                index += 1
            config = deepcopy(DEFAULT_CONFIG)
            config.update(id=provider_id, model=model, enable=True)
            config["custom_extra_body"]["agy_thought_level"] = effort
            if self.settings.enable_tools:
                config["modalities"].append("tool_use")
            if self.settings.enable_images:
                config["modalities"].append("image")
            await manager.create_provider(config)
        # Host methods persist the provider and selected default; no second
        # plugin state file can get out of sync after a restart.
        self.result = {"provider_id": provider_id, "model": model, "default_selected": False}
        if use_default:
            await manager.set_provider(provider_id, ProviderType.CHAT_COMPLETION)
            self.result["default_selected"] = True

    def register(self):
        for action in ("status", "check", "login", "login-code", "login-cancel", "apply"):
            async def handler(action=action):
                return await self.handle(action)

            route = f"/{PLUGIN_NAME}/setup/{action}"
            self.context.register_web_api(route, handler, ["GET" if action == "status" else "POST"], "AGY 首次设置")
            self.routes.append((route, handler))

    async def handle(self, action):
        from astrbot.api.web import error_response, json_response, request

        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        # Dashboard validates its session before calling a registered handler.
        # Check identity here too, including direct handler invocations.
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401, headers=headers)
        try:
            if self.closed:
                raise AgyError("CLOSED", "插件已停用。")
            if action != "status":
                if (request.content_type or "").split(";", 1)[0].strip().lower() != "application/json":
                    raise AgyError("CONFIG", "此操作需要 JSON 请求。")
                raw = await request.body()
                if len(raw) > 16384:
                    raise AgyError("CONFIG", "请求过长。")
                data = await request.json(default={})
                if (
                    not isinstance(data, dict)
                    or not isinstance(data.get("csrf"), str)
                    or not data["csrf"].isascii()
                    or not secrets.compare_digest(data["csrf"], self.csrf)
                ):
                    return error_response("页面已过期，请刷新后重试。", status_code=403, headers=headers)
                model = data.get("model", "")
                if not isinstance(model, str) or len(model) > 1024:
                    raise AgyError("CONFIG", "模型 ID 无效。")
                if action == "check":
                    self.start_check(model)
                elif action == "login":
                    self._available()
                    self.result, self.action_error = None, ""
                    self.report = {"status": "pending", "stage": "authentication"}
                    self.login.start(request.username)
                elif action == "login-code":
                    await self.login.submit(request.username, data.get("ticket", ""), data.get("code"))
                elif action == "login-cancel":
                    await self.login.cancel(request.username, data.get("ticket", ""))
                elif action == "apply":
                    self.start_apply(model, data.get("effort", "default"), data.get("use_default", False))
            return json_response(self.snapshot(request.username), headers=headers)
        except AgyError as exc:
            return error_response(str(exc), status_code=409 if exc.code == "BUSY" else 400, headers=headers)
        except Exception:
            return error_response("AGY 本地操作失败，请重新检测或查看插件配置。", status_code=500, headers=headers)

    async def close(self):
        self.closed = True
        registered = self.context.registered_web_apis
        registered[:] = [row for row in registered if not any(row[0] == route and row[1] is handler for route, handler in self.routes)]
        if self.busy:
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        await self.login.close()
