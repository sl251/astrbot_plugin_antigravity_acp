"""First-run orchestration using AstrBot's authenticated plugin views."""

import asyncio
import hashlib
import os
import platform
import secrets
from collections import Counter
from copy import deepcopy
from pathlib import Path

from astrbot.api import logger

from .builtin_tools import supports_read, tool_policy
from .quota import QuotaService
from .turn_history import HISTORY_ID, valid_history_id
from .errors import AgyError
from .web_errors import WEB_HEADERS, web_error
from .login import LoginFlow
from .paths import plugin_data_root
from .probe import probe
from .routing import (
    OverrideObservations,
    compute_acp_modalities,
    dashboard_admin,
    delete_acp_provider,
    disable_provider,
    enable_provider,
    inspect_acp_provider,
    provider_impact,
    repair_acp_providers,
    routing_snapshot,
    select_default,
    update_acp_provider,
)

PLUGIN_NAME = "astrbot_plugin_antigravity_acp"
HINTS = {
    "NOT_INSTALLED": "当前 AstrBot 运行环境的默认路径中未找到 CLI，不代表其他目录未安装。已有 CLI 时请在插件配置填写 command 绝对路径；Docker 使用容器内路径，保存并重载后重新检测。",
    "COMMAND_NOT_FOUND": "配置的 command 路径不存在或不是文件。请核对 AstrBot 的运行用户及容器内路径；显式配置不会自动回退到其他 CLI。修改后保存并重载插件。",
    "STARTUP_EXIT": "CLI 已启动，但在完成 ACP 握手前退出。请检查原生可执行文件、脚本依赖和运行用户；旧包装脚本可能不接受升级后的隔离目录。不要仅凭此错误重新安装或登录。",
    "CLI_WRAPPER": "启动脚本拒绝了当前隔离目录或认证链接。请把 command 改为官方原生可执行文件，或更新旧脚本的目录限制；插件升级后数据目录可能已变化。保存并重载后重新检测。",
    "TRANSPORT": "CLI 的 ACP 输出管道已关闭。请检查进程状态、启动脚本与 ACP 兼容性；仅此错误不能确定是网络或认证问题。",
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
    "ROUTE_MISMATCH": "全局 runner 未确认选择目标实例，请查看路由诊断并显式选择实例。",
    "REFERENCE_BLOCKED": "请查看影响预览。存在引用或扫描不完整时，插件阻止删除和停用。",
    "LOCAL": "本地操作未完成，请检查数据目录权限、宿主版本和运行环境，再重新检测。",
}


def next_step(report):
    if report.get("status") == "passed":
        if report.get("minimal_tools_status") == "unsupported":
            return "基础检查通过，但当前 CLI 不支持组件精简参数。请恢复 agy_customizations_mode=inherit。"
        return "环境与模型目录检查通过。选择模型并保存即可配置；此检查没有发送对话。"
    return HINTS.get(report.get("error_code"), "请重新检测；仍失败时用 /agy doctor 获取脱敏诊断。")


def environment(settings=None):
    container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    return {
        "system": platform.system(), "python": platform.python_version(),
        "container": container, "proxy_mode_note": "登录与对话共用插件代理设置",
        "discovery_hint": (
            "当前使用显式 command：仅检查配置的可执行文件，不自动回退到默认搜索。"
            if settings is not None and settings.command else
            "当前默认搜索范围：AstrBot 进程的 PATH、%LOCALAPPDATA%/agy/bin/agy.exe、用户 .local/bin/agy.exe。"
            if os.name == "nt" else
            "当前默认搜索范围：AstrBot 进程的 PATH、~/.local/bin/agy。"
        ),
        "install_url": "https://antigravity.google/docs/cli/tutorial",
        "install_command": "irm https://antigravity.google/cli/install.ps1 | iex" if os.name == "nt" else "curl -fsSL https://antigravity.google/cli/install.sh | bash",
        "install_hint": (
            "请在容器内部、以 AstrBot 的运行用户安装并登录 CLI；持久保存官方认证目录。宿主机安装不等于容器可用。"
            if container else
            "Windows：安装官方原生 agy.exe。自动搜索 PATH、%LOCALAPPDATA%\\agy\\bin 和用户 .local\\bin。"
            if os.name == "nt" else
            "Linux / macOS：按官方 CLI 安装指引操作。自动搜索 PATH 和 ~/.local/bin/agy；系统服务须使用同一运行用户。"
        ) + " 更新前请停止活跃对话、备份原 CLI，再按官方指引手动更新；失败时恢复旧文件并重载插件。插件不检查远程最新版、不覆盖全局 CLI。",
    }


def _provider_defaults():
    """读取适配器默认模板与类型标识；离线测试无宿主 provider 模块时回退到本地常量。"""
    try:
        from .provider import DEFAULT_CONFIG, PROVIDER_TYPE
    except ImportError:
        from .routing import DEFAULT_CONFIG, PROVIDER_TYPE
    return DEFAULT_CONFIG, PROVIDER_TYPE


async def ensure_provider_source(manager):
    """Persist the source relationship and per-instance history identities.

    ProviderManager has no source-creation method. Use its configuration and
    resource lock, as its create/update methods do. Existing model IDs and
    per-model settings remain unchanged, so session selections remain valid.
    Old hosts lacking resource_lock or provider_sources structure skip binding
    with a warning so wizard saves can proceed.
    """
    _, PROVIDER_TYPE = _provider_defaults()

    lock = getattr(manager, "resource_lock", None)
    acm = getattr(manager, "acm", None)
    config = getattr(acm, "default_conf", None)
    if (
        lock is None
        or not hasattr(lock, "__aenter__")
        or not hasattr(lock, "__aexit__")
        or config is None
        or not hasattr(config, "get")
        or not hasattr(config, "__getitem__")
        or not callable(getattr(config, "save_config", None))
    ):
        logger.warning(
            "当前 AstrBot 宿主缺少 provider_sources 或 resource_lock 接口，已跳过模型来源绑定。"
        )
        return None

    async with lock:
        providers = config.get("provider")
        sources = config.get("provider_sources")
        if not isinstance(providers, list) or not isinstance(sources, list):
            logger.warning(
                "当前 AstrBot 宿主配置缺少 provider/provider_sources 结构，已跳过模型来源绑定。"
            )
            return None
        source = next(
            (
                row
                for row in sources
                if isinstance(row, dict)
                and row.get("id")
                and row.get("type") == PROVIDER_TYPE
                and row.get("provider_type") == "chat_completion"
            ),
            None,
        )
        created = source is None
        if created:
            occupied = {row.get("id") for row in sources if isinstance(row, dict)}
            source_id = PROVIDER_TYPE
            index = 2
            while source_id in occupied:
                source_id = f"{PROVIDER_TYPE}_{index}"
                index += 1
            source = {
                "id": source_id, "type": PROVIDER_TYPE,
                "provider": PROVIDER_TYPE, "provider_type": "chat_completion",
                "enable": True, "key": [""],
            }
            sources.append(source)
        source_types = {
            row["id"]: row.get("type") for row in sources if isinstance(row, dict) and row.get("id")
        }
        provider_ids = Counter(
            row["id"] for row in providers if isinstance(row, dict) and isinstance(row.get("id"), str)
        )
        changed = []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            updates = {}
            provider_source = provider.get("provider_source_id")
            if provider.get("type") == PROVIDER_TYPE and provider_source not in source_types:
                updates["provider_source_id"] = source["id"]
            provider_id = provider.get("id")
            if (isinstance(provider_id, str) and provider_id and provider_ids[provider_id] == 1
                    and provider.get("type", source_types.get(provider_source)) == PROVIDER_TYPE
                    and not valid_history_id(provider.get(HISTORY_ID))):
                updates[HISTORY_ID] = secrets.token_hex(16)
            for key, value in updates.items():
                changed.append((provider, key, key in provider, provider.get(key)))
                provider[key] = value
        try:
            # Re-persist on explicit save even after a previous write failed.
            config.save_config()
        except BaseException:
            for provider, key, present, previous in changed:
                if present:
                    provider[key] = previous
                else:
                    provider.pop(key, None)
            if created:
                sources.remove(source)
            raise
        manager.provider_sources_config = sources
        manager.providers_config = providers
        return source["id"]


class Onboarding:
    def __init__(self, context, settings, config=None):
        self.context, self.settings = context, settings
        self.config = config
        self.quota = QuotaService(settings)
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
        self.overrides = OverrideObservations()
        self.override_hook = self.overrides.changed
        self.cached_models = []
        self.efforts_by_model = {}

    @property
    def busy(self):
        return self.job is not None and not self.job.done()

    def _available(self):
        if self.closed:
            raise AgyError("CLOSED", "插件已停用，请启用或刷新页面。")
        if self.busy or self.login.busy:
            raise AgyError("BUSY", "检测、登录或保存正在进行，请等待完成。")

    def _known_models(self):
        models = self.report.get("models")
        if isinstance(models, list) and models:
            return models
        return self.cached_models

    def providers(self):
        _, PROVIDER_TYPE = _provider_defaults()

        manager = self.context.provider_manager
        route = routing_snapshot(manager)
        acm = getattr(manager, "acm", None)
        default_conf = getattr(acm, "default_conf", None)
        sources = (
            default_conf.get("provider_sources")
            if isinstance(default_conf, dict)
            else getattr(manager, "provider_sources_config", None)
        )
        source_ids = (
            {row.get("id") for row in sources if isinstance(row, dict) and row.get("id")}
            if isinstance(sources, list) and sources
            else None
        )
        rows = []
        for config in list(manager.providers_config):
            merged = manager.get_merged_provider_config(config)
            if merged.get("type") == PROVIDER_TYPE:
                rows.append(
                    inspect_acp_provider(
                        manager,
                        config,
                        self.settings,
                        route=route,
                        source_ids=source_ids,
                    )
                )
        return rows

    def command_scope_warning(self):
        manager = getattr(self.context.provider_manager, "acm", None)
        conf = getattr(manager, "default_conf", {}) or {}
        scope = conf.get("plugin_set", ["*"])
        if isinstance(scope, list) and scope != ["*"] and PLUGIN_NAME not in scope:
            return "默认配置的可用插件未包含 Antigravity ACP。请在目标会话的配置 → 可用插件中勾选 astrbot_plugin_antigravity_acp 并保存，以接收 /agy 指令。"
        return None

    def _read_supported(self):
        return self.report.get("verified_read") is True and supports_read(self.report.get("cli_version"))

    def policy(self):
        config = self.config or {}
        return tool_policy(
            self.settings, config.get("builtin_read_file", self.settings.builtin_read_file),
            self._read_supported(), config.get("builtin_search_web", self.settings.builtin_search_web),
        )

    def save_policy(self, data):
        changes = data.keys() - {"csrf"}
        if ("csrf" not in data or not changes or changes - {"read_file", "search_web"}
                or ("read_file" in changes and data["read_file"] not in ("deny", "allow_once"))
                or ("search_web" in changes and data["search_web"] not in ("deny", "allow"))):
            raise AgyError("CONFIG", "工具策略参数无效。")
        if data.get("read_file") == "allow_once" and not self._read_supported():
            raise AgyError("UNSUPPORTED", "请先完成受支持 CLI 的版本检测。")
        config = self.config
        save = getattr(config, "save_config", None)
        if not callable(save):
            raise RuntimeError("无法保存插件配置。")
        updates = {f"builtin_{name}": data[name] for name in ("read_file", "search_web") if name in changes}
        previous = {key: config[key] for key in updates if key in config}
        try:
            config.update(updates)
            save()
        except Exception:
            for key in updates.keys() - previous.keys():
                config.pop(key, None)
            config.update(previous)
            raise
        return self.policy()

    def snapshot(self, owner=""):
        return {
            "tool_policy": self.policy(),
            "command_scope_warning": self.command_scope_warning(),
            "csrf": self.csrf, "busy": self.busy, "operation": self.operation,
            "report": deepcopy(self.report), "hint": next_step(self.report),
            "cached_models": deepcopy(self._known_models()),
            "efforts_by_model": deepcopy(self.efforts_by_model),
            "environment": environment(self.settings), "login": self.login.snapshot(owner),
            "providers": self.providers(), "result": self.result,
            "routing": routing_snapshot(self.context.provider_manager),
            "action_error": self.action_error,
            "action_hint": HINTS.get(self.action_error, "操作未完成，请重新检测或查看模型配置。") if self.action_error else "",
            "global_effort": self.settings.thought_level,
            "global_enable_tools": self.settings.enable_tools,
            "global_enable_images": self.settings.enable_images,
        }

    async def _check(self, model="", effort="default"):
        self.report = {"status": "checking", "stage": "discovery"}

        def progress(report):
            report["status"] = "checking"
            self.report = report

        self.report = await probe(self.settings, details=True, model=model, effort=effort, progress=progress)
        models = self.report.get("models")
        if isinstance(models, list) and models:
            self.cached_models = deepcopy(models)
        selected = self.report.get("selected_model")
        if isinstance(selected, str) and selected:
            self.efforts_by_model[selected] = list(self.report.get("efforts") or [])
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
                self.action_error = "HOST" if operation == "apply" or operation.startswith("provider-") else "LOCAL"
            finally:
                self.operation = ""

        self.job = asyncio.create_task(run(), name="agy:setup-" + operation)

    def start_apply(self, model, effort, use_default, tools_mode=None, create_only=False):
        self._available()
        if (
            not isinstance(effort, str)
            or effort not in {"default", "low", "medium", "high"}
            or type(use_default) is not bool
            or type(create_only) is not bool
        ):
            raise AgyError("CONFIG", "模型档位或切换选项无效。")
        if tools_mode is not None and (
            not isinstance(tools_mode, str)
            or tools_mode not in {"inherit", "chat", "tools"}
        ):
            raise AgyError("CONFIG", "工具模式应为 inherit/chat/tools。")
        if model not in {row["id"] for row in self._known_models()}:
            raise AgyError("MODEL", "请先检测并选择当前目录中的模型。")
        self._launch("apply", lambda: self._apply(model, effort, use_default, tools_mode, create_only))

    async def _run_shielded(self, coro, name):
        """在插件停用取消窗口内保护宿主持久化关键区完成。"""
        commit = asyncio.create_task(coro, name=name)
        cancelled = None
        while True:
            try:
                await asyncio.shield(commit)
                break
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
                if commit.done():
                    break
        result = commit.result()
        if cancelled:
            raise cancelled
        return result

    async def _apply(self, model, effort, use_default, tools_mode=None, create_only=False):
        # 修改宿主配置前始终复核模型与思考档位。
        actual_effort = self.settings.thought_level if effort == "default" else effort
        report = await self._check(model, actual_effort)
        if report.get("status") != "passed":
            raise AgyError(report.get("error_code", "LOCAL"), "配置前检查未通过。")
        await self._run_shielded(
            self._save_provider(model, effort, use_default, tools_mode, create_only=create_only),
            "agy:save-provider",
        )

    async def _save_provider(self, model, effort, use_default, tools_mode=None, create_only=False):
        DEFAULT_CONFIG, PROVIDER_TYPE = _provider_defaults()

        target_mode = "chat" if tools_mode is None else tools_mode
        match_any_mode = tools_mode is None
        manager = self.context.provider_manager
        source_id = await ensure_provider_source(manager)
        existing = None
        existing_mode = "inherit"
        for candidate in list(manager.providers_config):
            merged = manager.get_merged_provider_config(candidate)
            candidate_model = merged.get("model") or (merged.get("model_config") or {}).get("model", "")
            candidate_effort = (merged.get("custom_extra_body") or {}).get("agy_thought_level", "default")
            candidate_mode = (merged.get("custom_extra_body") or {}).get("agy_tools_mode", "inherit")
            if (
                merged.get("type") == PROVIDER_TYPE
                and candidate_model == model
                and candidate_effort == effort
                and (match_any_mode or candidate_mode == target_mode)
            ):
                if existing is not None:
                    raise AgyError("CONFIG", "多个 ACP 实例匹配，请在模型管理中显式选择实例 ID。")
                existing = candidate
                existing_mode = candidate_mode
        if existing is not None:
            if create_only:
                raise AgyError("CONFIG", "已存在相同模型、思考档位与模式的实例，请直接编辑现有配置。")
            provider_id = existing["id"]
            effective_mode = existing_mode
            await enable_provider(
                manager,
                provider_id,
                expected=deepcopy(existing),
                use_default=use_default,
                modalities=compute_acp_modalities(self.settings, effective_mode),
            )
        else:
            suffix_seed = (
                f"{model}\0{effort}"
                if target_mode == "chat"
                else f"{model}\0{effort}\0{target_mode}"
            )
            suffix = hashlib.sha256(suffix_seed.encode()).hexdigest()[:12]
            base_id = "agy_setup_" + suffix
            provider_id = base_id
            occupied = {row.get("id") for row in manager.providers_config}
            index = 2
            while provider_id in occupied:
                provider_id = f"{base_id}_{index}"
                index += 1
            config = deepcopy(DEFAULT_CONFIG)
            config.update(id=provider_id, model=model, enable=True)
            config[HISTORY_ID] = secrets.token_hex(16)
            if source_id:
                config["provider_source_id"] = source_id
            config["custom_extra_body"]["agy_thought_level"] = effort
            config["custom_extra_body"]["agy_tools_mode"] = target_mode
            config["modalities"] = compute_acp_modalities(self.settings, target_mode)
            await manager.create_provider(config)
            effective_mode = target_mode
        # 由宿主持久化实例与全局默认，不维护第二套易漂移的插件状态文件。
        self.result = {
            "provider_id": provider_id,
            "model": model,
            "tools_mode": effective_mode,
            "default_selected": False,
            "provider_state": "existing" if existing is not None else "created",
        }
        if use_default:
            if existing is None:
                await select_default(manager, provider_id)
            self.result["default_selected"] = True

    async def _update_provider(self, provider_id, model, effort, tools_mode):
        manager = self.context.provider_manager
        current = next((row for row in self.providers() if row["id"] == provider_id), None)
        if current is None:
            raise AgyError("CONFIG", "请选择现有的唯一 ACP 实例 ID。")
        known_ids = {row["id"] for row in self._known_models()}
        model_changed = current["model"] != model
        effort_changed = current["thought_level"] != effort
        cached_efforts = self.efforts_by_model.get(model, [])
        needs_probe = (
            (model_changed and model not in known_ids)
            or (
                effort != "default"
                and (model_changed or effort_changed)
                and effort not in cached_efforts
            )
        )
        if needs_probe:
            actual_effort = self.settings.thought_level if effort == "default" else effort
            report = await self._check(model, actual_effort)
            if report.get("status") != "passed":
                raise AgyError(report.get("error_code", "LOCAL"), "配置前检查未通过。")
        source_id = await ensure_provider_source(manager)
        changed_model = await self._run_shielded(
            update_acp_provider(
                manager,
                provider_id,
                model=model,
                effort=effort,
                tools_mode=tools_mode,
                settings=self.settings,
                source_id=source_id,
            ),
            "agy:update-provider",
        )
        self.result = {
            "provider_id": provider_id,
            "model": model,
            "thought_level": effort,
            "tools_mode": tools_mode,
            "model_changed": changed_model,
            "applied": True,
        }

    async def _repair(self, provider_id=None):
        manager = self.context.provider_manager
        source_id = await ensure_provider_source(manager)
        repaired_ids = await self._run_shielded(
            repair_acp_providers(manager, self.settings, provider_id=provider_id, source_id=source_id),
            "agy:repair-provider",
        )
        self.result = {
            "provider_id": provider_id,
            "repaired_ids": repaired_ids,
            "applied": True,
        }

    async def _manage(self, action, provider_id, enabled=None, migrate_default_to=None, force_confirm=False):
        manager = self.context.provider_manager
        impact = provider_impact(manager, provider_id, self.overrides)
        self.result = {"provider_id": provider_id, "impact": impact, "applied": False}
        if action == "provider-default":
            await select_default(manager, provider_id)
        elif action == "provider-toggle":
            if enabled:
                current = next((row for row in self.providers() if row["id"] == provider_id), {})
                cur_mode = current.get("tools_mode", "inherit")
                if cur_mode not in {"inherit", "chat", "tools"}:
                    cur_mode = "inherit"
                await enable_provider(
                    manager,
                    provider_id,
                    modalities=compute_acp_modalities(self.settings, cur_mode),
                )
            else:
                await disable_provider(
                    manager,
                    provider_id,
                    migrate_default_to=migrate_default_to,
                    force_confirm=force_confirm,
                    observations=self.overrides,
                )
        elif action == "provider-delete":
            await delete_acp_provider(
                manager,
                provider_id,
                migrate_default_to=migrate_default_to,
                force_confirm=force_confirm,
                observations=self.overrides,
            )
            self.result.update(applied=True, impact=None)
            return
        else:
            raise AgyError("CONFIG", "未知的实例操作。")
        self.result.update(applied=True, impact=provider_impact(manager, provider_id, self.overrides))

    def register(self):
        manager = self.context.provider_manager
        if callable(getattr(manager, "register_provider_change_hook", None)):
            manager.register_provider_change_hook(self.override_hook)
        for action in (
            "status", "check", "login", "login-code", "login-cancel", "apply",
            "provider-impact", "provider-default", "provider-delete",
            "provider-toggle", "provider-update", "provider-repair", "tool-policy", "quota-refresh",
        ):
            async def handler(action=action):
                return await self.handle(action)

            route = f"/{PLUGIN_NAME}/setup/{action}"
            self.context.register_web_api(route, handler, ["GET" if action == "status" else "POST"], "AGY 首次设置")
            self.routes.append((route, handler))

    async def handle(self, action):
        from astrbot.api.web import json_response, request

        # Dashboard 在分发前会校验会话；此处再次校验身份以防直接调用。
        if not request.username:
            return web_error("UNAUTHENTICATED", "请先登录 AstrBot 管理面板。", 401)
        if not dashboard_admin(self.context.provider_manager, request.username):
            return web_error("FORBIDDEN", "仅 AstrBot 面板管理员可使用设置页面。", 403)
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
                    return web_error("CSRF_EXPIRED", "页面已过期，请刷新后重试。", 403)
                model = data.get("model", "")
                if not isinstance(model, str) or len(model) > 1024:
                    raise AgyError("CONFIG", "模型 ID 无效。")
                if action == "tool-policy":
                    return json_response(self.save_policy(data), headers=WEB_HEADERS)
                if action == "quota-refresh":
                    force = data.get("force", True)
                    if data.keys() - {"csrf", "force"} or type(force) is not bool:
                        raise AgyError("CONFIG", "额度刷新参数无效。")
                    if self.quota.closed:
                        raise AgyError("CLOSED", "插件已停用。")
                    if self.login.busy:
                        return web_error("LOGIN_BUSY", "登录中，请稍后刷新。", 409)
                    return json_response(await self.quota.refresh(force=force), headers=WEB_HEADERS)
                if action == "provider-repair":
                    self._available()
                    if data.keys() == {"csrf", "all"} and data.get("all") is True:
                        self._launch(action, lambda: self._repair(None))
                    elif data.keys() == {"csrf", "provider_id"}:
                        provider_id = data.get("provider_id")
                        if not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > 1024:
                            raise AgyError("CONFIG", "实例 ID 无效。")
                        self._launch(action, lambda: self._repair(provider_id))
                    else:
                        raise AgyError("CONFIG", "实例修复参数无效。")
                elif action.startswith("provider-"):
                    self._available()
                    provider_id = data.get("provider_id")
                    if (not isinstance(provider_id, str) or not provider_id.strip()
                            or len(provider_id) > 1024):
                        raise AgyError("CONFIG", "实例 ID 无效。")
                    if action == "provider-impact":
                        if data.keys() != {"csrf", "provider_id"}:
                            raise AgyError("CONFIG", "实例操作参数无效。")
                        impact = provider_impact(self.context.provider_manager, provider_id, self.overrides)
                        return json_response({"impact": impact}, headers=WEB_HEADERS)
                    if action == "provider-update":
                        if data.keys() != {"csrf", "provider_id", "model", "effort", "tools_mode"}:
                            raise AgyError("CONFIG", "实例编辑参数无效。")
                        effort = data.get("effort")
                        tools_mode = data.get("tools_mode")
                        if (
                            not model.strip()
                            or effort not in {"default", "low", "medium", "high"}
                            or tools_mode not in {"inherit", "chat", "tools"}
                        ):
                            raise AgyError("CONFIG", "模型、思考强度或工具模式无效。")
                        self._launch(action, lambda: self._update_provider(provider_id, model, effort, tools_mode))
                    elif action == "provider-default":
                        if data.keys() != {"csrf", "provider_id", "confirm"} or data["confirm"] is not True:
                            raise AgyError("CONFIG", "请明确确认默认实例操作。")
                        self._launch(action, lambda: self._manage(action, provider_id))
                    elif action in {"provider-toggle", "provider-delete"}:
                        required = {"csrf", "provider_id", "confirm"}
                        if action == "provider-toggle":
                            required.add("enabled")
                        optional = {"migrate_default_to", "force_confirm"}
                        if not required.issubset(data.keys()) or not set(data.keys()).issubset(required | optional):
                            raise AgyError("CONFIG", "实例操作参数无效。")
                        if data["confirm"] is not True or (action == "provider-toggle" and type(data["enabled"]) is not bool):
                            raise AgyError("CONFIG", "请明确确认实例操作和目标状态。")
                        migrate_default_to = data.get("migrate_default_to")
                        if migrate_default_to is not None and (not isinstance(migrate_default_to, str) or len(migrate_default_to) > 1024):
                            raise AgyError("CONFIG", "替代默认实例 ID 无效。")
                        force_confirm = data.get("force_confirm", False)
                        if type(force_confirm) is not bool:
                            raise AgyError("CONFIG", "确认标记必须为布尔值。")
                        self._launch(
                            action,
                            lambda: self._manage(
                                action,
                                provider_id,
                                data.get("enabled"),
                                migrate_default_to=migrate_default_to,
                                force_confirm=force_confirm,
                            ),
                        )
                    else:
                        raise AgyError("CONFIG", "未知的实例操作。")
                elif action == "check":
                    self.start_check(model)
                elif action == "login":
                    self._available()
                    self.result, self.action_error = None, ""
                    self.report = {"status": "pending", "stage": "authentication"}
                    self.quota.invalidate()
                    self.login.start(request.username)
                elif action == "login-code":
                    await self.login.submit(request.username, data.get("ticket", ""), data.get("code"))
                elif action == "login-cancel":
                    await self.login.cancel(request.username, data.get("ticket", ""))
                elif action == "apply":
                    self.start_apply(
                        model,
                        data.get("effort", "default"),
                        data.get("use_default", False),
                        data.get("tools_mode"),
                        data.get("create_only", False),
                    )
            return json_response(self.snapshot(request.username), headers=WEB_HEADERS)
        except AgyError as exc:
            status_code = 400
            if exc.code == "BUSY":
                status_code = 409
            elif exc.code == "CLOSED" and action in ("tool-policy", "quota-refresh"):
                status_code = 503
            return web_error(exc.code, exc.public_message, status_code)
        except Exception:
            return web_error("INTERNAL", "AGY 本地操作失败，请重新检测或查看插件配置。", 500)

    async def close(self):
        self.closed = True
        registered = self.context.registered_web_apis
        registered[:] = [row for row in registered if not any(row[0] == route and row[1] is handler for route, handler in self.routes)]
        hooks = getattr(self.context.provider_manager, "_provider_change_hooks", [])
        if self.override_hook in hooks:
            hooks.remove(self.override_hook)
        if self.busy:
            if not self.operation.startswith("provider-"):
                self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        await self.login.close()
        await self.quota.close()
