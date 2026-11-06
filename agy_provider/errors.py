"""Public errors deliberately exclude CLI output, prompts and credentials."""

import re


class AgyError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"AGY [{code}] {message}")


def remote_error(error: object) -> AgyError:
    # Inspect transiently for classification; never propagate upstream text.
    text = str(error).lower()
    auth_required = isinstance(error, dict) and error.get("code") == -32000
    if auth_required or re.search(
        r"authentication.required|unauth|not.logged.in|sign.in|login|credential|\b401\b",
        text,
    ):
        return AgyError(
            "AUTH",
            "请用运行 AstrBot 的同一系统用户执行官方 agy 登录。"
            "若普通 CLI 已登录而隔离 session/new 仍失败，请检查隔离目录对应的"
            "官方认证后端/搜索路径；不要复制其他用户凭据或关闭配置隔离。",
        )
    if re.search(r"quota|exhaust|capacity|usage.limit", text):
        return AgyError("QUOTA", "AGY 额度不足或容量不可用，请检查官方 CLI。")
    if re.search(r"rate.limit|too.many|\b429\b", text):
        return AgyError("RATE_LIMIT", "AGY 请求受限，请稍后再试。")
    if re.search(r"timeout|timed.out|deadline", text):
        return AgyError("TIMEOUT", "AGY 上游请求超时。")
    if re.search(r"-32601|method.not.found|not.supported", text):
        return AgyError("UNSUPPORTED", "此 AGY 版本不支持所需 ACP 方法，请升级 CLI。")
    return AgyError(
        "UPSTREAM", "AGY 返回错误；请在本机 CLI 检查登录、网络与模型可用性。"
    )
