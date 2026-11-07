"""Register only while the plugin is enabled; own no global CLI credentials."""

from copy import deepcopy

from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.core.provider import register as registry

from .agy_provider.config import Settings
from .agy_provider.host_compat import install_host_compat, uninstall_host_compat
from .agy_provider.provider import (
    DEFAULT_CONFIG,
    OWNER,
    PROVIDER_TYPE,
    AgyProvider,
    configure,
    terminate_all,
)

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
    "astrbot_plugin_agy_provider",
    "sl251",
    "通过本机 AGY ACP 使用 Gemini 模型",
    "0.1.0",
    "https://github.com/sl251/sl251-astrbot-plugin-agy-provider",
)
class AgyProviderPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.registered = False

    async def initialize(self):
        settings = Settings.from_dict(self.config)
        configure(settings)
        unregister_owned_adapter()
        template = deepcopy(DEFAULT_CONFIG)
        if settings.enable_tools:
            template["modalities"].append("tool_use")
        if settings.enable_images:
            template["modalities"].append("image")
        registry.register_provider_adapter(
            PROVIDER_TYPE,
            "本机官方 AGY CLI / ACP Gemini 模型（工具/图片按插件开关启用）",
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
        logger.info("AGY Gemini provider 已注册；首次获取模型/聊天时启动本机 CLI。")

    async def terminate(self):
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
