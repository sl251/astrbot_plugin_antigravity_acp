"""Use real ACP model ids/config options, never a hard-coded model list."""

from .errors import AgyError
from .transport import AcpClient


def options(result: dict) -> list[dict]:
    return [opt for opt in (result.get("configOptions") or []) if isinstance(opt, dict)]


def find_option(config: list[dict], category: str) -> dict | None:
    return next(
        (
            opt
            for opt in config
            if opt.get("id") == category or opt.get("category") == category
        ),
        None,
    )


def choices(option: dict | None) -> dict[str, str]:
    found = {}
    if not option:
        return found
    for item in option.get("options") or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("value"), str):
            found[item["value"]] = str(item.get("name", item["value"]))
        else:
            found.update(choices(item))  # ACP grouped select options
    return found


def catalog_names(result: dict) -> dict[str, str]:
    found = choices(find_option(options(result), "model"))
    model_data = result.get("models") or {}
    for model in model_data.get("availableModels", []):
        if isinstance(model, dict) and isinstance(model.get("modelId"), str):
            found[model["modelId"]] = str(model.get("name", model["modelId"]))
    # Name as well as id is checked because upstream ids can be opaque.
    ids = {
        key: name
        for key, name in found.items()
        if "gemini" in key.lower() or "gemini" in name.lower()
    }
    if not ids:
        raise AgyError(
            "NO_MODELS",
            "当前 ACP 目录没有 Gemini 模型，请检查 CLI 登录与账号模型权限。",
        )
    return ids


def catalog(result: dict) -> list[str]:
    return list(catalog_names(result))


async def select(client: AcpClient, result: dict, model: str, effort: str) -> list[dict]:
    available = catalog(result)
    if model not in available:
        raise AgyError("MODEL", "所选模型不在当前 Gemini 目录中，请重新获取模型列表。")
    config = options(result)
    model_option = find_option(config, "model")
    current = (model_option or {}).get("currentValue") or (
        result.get("models") or {}
    ).get("currentModelId")
    if current != model:
        if model_option:
            changed = await client.set_option(model_option["id"], model)
            config = options(changed) or config
            actual = find_option(config, "model")
            if actual and actual.get("currentValue") not in {None, model}:
                raise AgyError("MODEL", "AGY 未切换到请求的模型。")
        elif (result.get("models") or {}).get("availableModels"):
            changed = await client.request(
                "session/set_model",
                {"sessionId": client.session_id, "modelId": model},
                client.settings.startup_timeout,
            )
            # Legacy set_model may not report updated config options. Do not
            # advertise or apply the previous model's thought levels in that case.
            config = options(changed)
        else:
            raise AgyError("UNSUPPORTED", "此 CLI 没有提供模型选择能力。")
    if effort == "default":
        return config
    level = find_option(config, "thought_level")
    allowed = choices(level)
    if not level or effort not in allowed:
        raise AgyError(
            "UNSUPPORTED",
            "当前模型不支持所选思考档位，请改为 default 或 CLI 实际支持的档位。",
        )
    if level.get("currentValue") != effort:
        changed = await client.set_option(level["id"], effort)
        actual = find_option(options(changed), "thought_level")
        if actual and actual.get("currentValue") not in {None, effort}:
            raise AgyError("UNSUPPORTED", "AGY 未应用所选思考档位。")
        config = options(changed) or config
    return config
