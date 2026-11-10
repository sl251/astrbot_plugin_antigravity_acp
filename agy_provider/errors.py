"""Public errors deliberately exclude CLI output, prompts and credentials."""

import re


class AgyError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"AGY [{code}] {message}")


_MESSAGES = {
    "PROXY_AUTH": "AGY 代理认证失败，请检查代理账号或代理服务配置。",
    "PROXY": "AGY 无法通过代理连接，请检查代理协议、地址与服务状态。",
    "NETWORK": "AGY 网络连接失败，请检查当前运行环境的网络、DNS 和代理。",
    "TLS": "AGY TLS 验证失败，请检查证书链、系统时间与代理证书设置。",
    "AUTH": (
        "请用运行 AstrBot 的同一系统用户执行官方 agy 登录。"
        "若普通 CLI 已登录而隔离 session/new 仍失败，请检查认证桥与官方认证后端；"
        "不要复制其他用户凭据或关闭配置隔离。"
    ),
    "QUOTA": "AGY 额度不足或容量不可用，请检查官方 CLI。",
    "RATE_LIMIT": "AGY 请求受限，请稍后再试。",
    "TIMEOUT": "AGY 上游请求超时，请检查当前网络与 CLI 状态。",
    "UNSUPPORTED": "此 AGY 版本不支持所需 ACP 方法，请升级 CLI。",
    "UPSTREAM": "AGY 返回错误；请在本机 CLI 检查登录、网络与模型可用性。",
}


def classified_error(code: str, *, stage: str = "") -> AgyError:
    """Rebuild an error from an allowlisted code, without upstream text."""
    if code not in _MESSAGES:
        code = "UPSTREAM"
    prefix = (
        f"阶段 {stage}；"
        if stage
        in {
            "initialize",
            "session/new",
            "session/set_config_option",
            "session/prompt",
            "rpc",
        }
        else ""
    )
    return AgyError(code, prefix + _MESSAGES[code])


def remote_error(error: object) -> AgyError:
    # Inspect transiently for classification; never propagate upstream text.
    # In particular, -32000 is the generic JSON-RPC server-error range, not AUTH.
    text = str(error).lower()
    patterns = (
        (
            "PROXY_AUTH",
            r"\b407\b|\bproxy[\s:_-]+(?:requires?[\s:_-]+)?"
            r"(?:authentication|authorization|auth|credentials?)\b|"
            r"\b(?:authentication|authorization|auth|credentials?)[\s:_-]+"
            r"(?:for[\s:_-]+|to[\s:_-]+)proxy\b|err_proxy_auth",
        ),
        (
            "PROXY",
            r"\bproxy[\s:_-]+(?:error|unavailable|refused|failed)\b|"
            r"\bproxy[\s:_-]+(?:connection|connect|tunnel)[\s:_-]+"
            r"(?:failed|failure|error|refused|reset)\b|err_proxy_connection|"
            r"(?:failed|unable).to.connect.to.(?:the.)?proxy|"
            r"\bsocks(?:5h?)?[\s:_-]+(?:handshake[\s:_-]+)?"
            r"(?:failed|failure|error|refused)\b|"
            r"\bconnect[\s:_-]+(?:tunnel[\s:_-]+)?(?:failed|failure|error)\b|"
            r"\btunnel[\s:_-]+(?:failed|failure|error)\b",
        ),
        (
            "TLS",
            r"\b(?:tls|ssl)[\s:_-]+(?:(?:handshake|connection|verification|verify)"
            r"[\s:_-]+)?(?:failed|failure|error|alert)\b|"
            r"certificate[\s:_-]+(?:(?:verify|verification|validation)[\s:_-]+)?"
            r"(?:failed|failure|error|expired|invalid|untrusted)\b|"
            r"\bcert_(?:has_expired|untrusted|authority_invalid)|"
            r"self[\s_-]signed[\s_-]cert|"
            r"unable[\s_-]to[\s_-](?:verify|get[\s_-]issuer)[\s_-]+"
            r"(?:the[\s_-]first[\s_-])?cert|err_(?:tls|ssl)_[a-z_]+",
        ),
        (
            "NETWORK",
            r"\bdns[\s:_-]+(?:resolution[\s:_-]+)?(?:failed|failure|error)\b|"
            r"enotfound|eai_again|econnrefused|econnreset|enetunreach|ehostunreach|"
            r"connection.(?:refused|reset)|network.(?:unreachable|error)|"
            r"getaddrinfo[\s:_-]+(?:failed|failure|error)|"
            r"name.or.service.not.known|failed.to.fetch|fetch.failed",
        ),
        (
            "AUTH",
            r"authentication.required|unauth|not.logged.in|sign.in|login|credential|\b401\b",
        ),
        ("QUOTA", r"quota|exhaust|capacity|usage.limit"),
        ("RATE_LIMIT", r"rate.limit|too.many|\b429\b"),
        ("TIMEOUT", r"timeout|timed.out|deadline"),
        ("UNSUPPORTED", r"-32601|method.not.found|not.supported"),
    )
    for code, pattern in patterns:
        if re.search(pattern, text):
            return classified_error(code)
    return classified_error("UPSTREAM")
