"""Register only while the plugin is enabled; own no global CLI credentials."""

import asyncio
import json
from copy import deepcopy

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider import register as registry

from .agy_provider.config import Settings
from .agy_provider.errors import AgyError
from .agy_provider.host_compat import install_host_compat, uninstall_host_compat
from .agy_provider.onboarding import Onboarding, ensure_provider_source, next_step
from .agy_provider.provider import (
    DEFAULT_CONFIG,
    OWNER,
    PROVIDER_TYPE,
    AgyProvider,
    configure,
    instance_cost_snapshots,
    terminate_all,
)
from .agy_provider.telemetry import format_cost_report

RESTORE_IDS = "_sl251_agy_provider_restore_ids"


def unregister_owned_adapter() -> None:
    meta = registry.provider_cls_map.get(PROVIDER_TYPE)
    if meta is None:
        return
    if getattr(meta.cls_type, "agy_plugin_owner", None) != OWNER:
        raise RuntimeError("agy_acp 已被其他插件注册，请先解决 provider 名称冲突。")
    registry.provider_cls_map.pop(PROVIDER_TYPE)
    registry.provider_registry[:] = [
        item for item in registry.provider_registry if item is not meta
    ]


@register(
    "astrbot_plugin_antigravity_acp",
    "sl251",
    "通过本机 AGY ACP 使用 Gemini 模型",
    "0.5.2",
    "https://github.com/sl251/astrbot_plugin_antigravity_acp",
)
class AgyProviderPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.registered = False
        self.doctor_lock = asyncio.Lock()
        self.doctor_task = None
        self.stopping = False
        self.setup_service = None

    @filter.command_group("agy")
    async def agy(self, event: AstrMessageEvent):
        pass

    @agy.command("setup")
    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    async def setup(self, event: AstrMessageEvent):
        if not event.is_admin():
            return
        if self.stopping or self.setup_service is None:
            yield event.plain_result("AGY 插件尚未就绪，请启用后重试。")
            return
        service = self.setup_service
        busy = service.busy or service.login.busy
        message = "AGY 首次设置\n打开 AstrBot WebUI → 插件 → Antigravity ACP → 首次设置。\n"
        message += "检测或登录正在进行。" if busy else next_step(service.report)
        if rows := service.providers():
            message += "\n已配置模型：\n" + "\n".join(
                f"{row['id']} · {row['model']} · mode={row.get('tools_mode', 'inherit')}"
                for row in rows
            )
            message += "\n在本会话使用：/agy use <提供商 ID>（建议纯聊天优先用 chat 实例，工具会话用 tools 实例；统计：/agy cost）"
        yield event.plain_result(message)

    @agy.command("use")
    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    async def use(self, event: AstrMessageEvent, provider_id: str = ""):
        if not event.is_admin():
            return
        service = self.setup_service
        if self.stopping or service is None:
            yield event.plain_result("AGY 插件尚未就绪。")
            return
        try:
            service._available()
            allowed = {row["id"] for row in service.providers() if row["enabled"]}
            if provider_id not in allowed:
                raise AgyError("CONFIG", "请先在首次设置页保存模型，再使用 /agy use <提供商 ID>。")
            from astrbot.core.provider.entities import ProviderType

            await self.context.provider_manager.set_provider(
                provider_id, ProviderType.CHAT_COMPLETION, event.unified_msg_origin
            )
            chosen = next(
                (row for row in service.providers() if row["id"] == provider_id), {}
            )
            mode = chosen.get("tools_mode", "inherit")
            yield event.plain_result(
                f"当前会话已切换到 {provider_id}（agy_tools_mode={mode}），可以开始聊天。"
            )
        except AgyError as exc:
            yield event.plain_result(str(exc))
        except Exception:
            yield event.plain_result("AGY 模型切换失败，请检查模型是否已启用，或重新运行首次设置。")

    @agy.command("cost")
    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    async def cost(self, event: AstrMessageEvent):
        if not event.is_admin():
            return
        if self.stopping:
            yield event.plain_result("AGY [CLOSED] 插件正在停用，无法读取成本统计。")
            return
        settings = (
            self.setup_service.settings
            if self.setup_service is not None
            else Settings.from_dict(self.config)
        )
        settings_summary = {
            "session_reuse_mode": settings.session_reuse_mode,
            "history_bytes": settings.history_bytes,
            "enable_tools": settings.enable_tools,
            "cost_log_enabled": settings.cost_log_enabled,
            "empty_dedupe_ttl": settings.empty_dedupe_ttl,
            "quota_cooldown": settings.quota_cooldown,
        }
        yield event.plain_result(
            format_cost_report(instance_cost_snapshots(), settings_summary)
        )

    @agy.command("doctor")
    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    async def doctor(self, event: AstrMessageEvent):
        # Also fail closed for direct handler invocation outside the dispatcher.
        if not event.is_admin():
            return
        if self.stopping:
            yield event.plain_result("AGY [CLOSED] 插件正在停用，请启用后再自检。")
            return
        if self.doctor_lock.locked():
            yield event.plain_result("AGY [BUSY] 自检正在运行，请稍后再试。")
            return
        async with self.doctor_lock:
            try:
                if self.setup_service is None:
                    raise AgyError("CLOSED", "首次设置服务尚未就绪。")
                self.setup_service.start_check()
            except AgyError as exc:
                yield event.plain_result(str(exc))
                return
            self.doctor_task = self.setup_service.job
            try:
                await asyncio.shield(self.doctor_task)
                report = {
                    key: value for key, value in self.setup_service.report.items()
                    if key not in {"models", "efforts", "selected_model"}
                }
            finally:
                self.doctor_task = None
        yield event.plain_result(
            "AGY 无生成自检（不会发送模型提示）：\n"
            + json.dumps(report, ensure_ascii=False, indent=2)
            + "\n" + next_step(report)
        )

    async def initialize(self):
        repairs = {}
        settings = Settings.from_dict(self.config, on_repair=repairs.__setitem__)
        if repairs:
            self.config.update(repairs)
            for key, value in repairs.items():
                logger.warning("AGY 配置 %s 无效或与其他预算冲突，已恢复为 %s。", key, value)
            try:
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception:
                logger.warning("AGY 修正配置写盘失败；本次使用修正值，请在插件配置页重新保存。")
        configure(settings)
        unregister_owned_adapter()
        template = deepcopy(DEFAULT_CONFIG)
        if settings.enable_tools:
            template["modalities"].append("tool_use")
        if settings.enable_images:
            template["modalities"].append("image")
        registry.register_provider_adapter(
            PROVIDER_TYPE,
            "本机官方 AGY CLI / ACP Gemini 模型（支持 custom_extra_body.agy_tools_mode=inherit|chat|tools）",
            default_config_tmpl=template,
            provider_display_name="AGY Gemini (ACP)",
        )(AgyProvider)
        self.registered = True
        try:
            install_host_compat(owner=OWNER, logger=logger)
        except Exception:
            unregister_owned_adapter()
            self.registered = False
            raise
        manager = self.context.provider_manager
        if any(config.get("type") == PROVIDER_TYPE for config in manager.providers_config):
            try:
                await ensure_provider_source(manager)
            except Exception:
                logger.warning("AGY 模型来源关联保存失败；已有模型仍可使用，请在插件面板重新保存模型。")
        # Startup: ProviderManager.initialize follows plugin initialization.
        # Hot reload / re-enable: restore only instances this plugin removed.
        restore_ids = getattr(manager, RESTORE_IDS, set())
        for config in list(manager.providers_config):
            if config.get("id") in restore_ids and config.get("enable"):
                merged = manager.get_merged_provider_config(config)
                if merged.get("type") == PROVIDER_TYPE:
                    try:
                        await manager.reload(config)
                    except Exception:
                        logger.error("AGY provider 重载失败，请在模型设置重新启用。")
        setattr(manager, RESTORE_IDS, set())
        logger.info(
            "已注册 agy_acp。打开插件详情 → 首次设置，或发送 /agy setup 开始引导。"
        )
        self.stopping = False
        self.setup_service = Onboarding(self.context, settings)
        self.setup_service.register()
        if settings.setup_auto_check:
            self.setup_service.start_check()

    async def terminate(self):
        self.stopping = True
        if self.setup_service:
            await self.setup_service.close()
        if self.doctor_task:
            self.doctor_task.cancel()
            await asyncio.gather(self.doctor_task, return_exceptions=True)
        if not self.registered:
            return
        try:
            manager = self.context.provider_manager
            restore_ids = set(getattr(manager, RESTORE_IDS, set()))
            async with manager.reload_lock:
                for provider_id, provider in list(manager.inst_map.items()):
                    if getattr(provider, "agy_plugin_owner", None) == OWNER:
                        restore_ids.add(provider_id)
                        await manager.terminate_provider(provider_id)
            setattr(manager, RESTORE_IDS, restore_ids)
            await terminate_all()
        finally:
            uninstall_host_compat()
            unregister_owned_adapter()
            self.registered = False
