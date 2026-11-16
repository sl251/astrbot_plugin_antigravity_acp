"""Remove only known host capability declarations that do not apply to AGY."""

from astrbot.api import logger


_LOCAL_TOOLS_START = (
    "Local computer tools are available subject to your role's file and execution permissions. "
)
_LOCAL_TOOLS_END = (
    "Local shell commands automatically return a managed session when they "
    "outlive the initial wait. Use `astrbot_shell_session` to list, poll, "
    "write raw text or complete lines to, interrupt, or terminate those sessions. "
    "Use its `write_line` action for line-oriented programs so the session receives "
    "a real line feed. Do not add `&`, `nohup`, or another detachment wrapper for "
    "ordinary long-running commands."
)
_UNIX_HINT = "The runtime shell is Unix-like. Use POSIX-compatible shell commands."

# Exact copies of _build_local_mode_prompt() in AstrBot v4.29.4:
# https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/astr_main_agent.py
# This is deliberately version-coupled: new wording/OS values stay unchanged.
# These host-local shell/session claims do not describe AGY's session MCP tools,
# so they are removed in both chat and tools mode. No tool permissions change.
_LOCAL_TOOL_BLOCKS = frozenset(
    _LOCAL_TOOLS_START
    + f"Current operating system: {system}. {hint} "
    + _LOCAL_TOOLS_END
    for system, hint in (
        ("Linux", _UNIX_HINT),
        ("Darwin", _UNIX_HINT),
        ("Unknown", _UNIX_HINT),
        (
            "Windows",
            "The runtime shell is PowerShell 7 (pwsh.exe). "
            "Use PowerShell 7-compatible syntax and cmdlets, and do not "
            "assume a full Unix userland or GNU utilities are available.",
        ),
        (
            "Windows",
            "The runtime shell is Windows PowerShell 5.1 (powershell.exe). "
            "Use Windows PowerShell 5.1-compatible syntax and cmdlets; do not use "
            "PowerShell 7-only syntax or assume Unix commands like cat/ls/grep are available.",
        ),
    )
)


def filter_host_capabilities(system: str) -> str:
    """Strip complete known standalone lines, preserving all surrounding text."""
    if _LOCAL_TOOLS_START.rstrip() not in system:
        return system
    output = []
    unmatched = False
    # Split on LF only: other Unicode separators can belong to persona text.
    for line in system.split("\n"):
        content = line.removesuffix("\r")
        if content in _LOCAL_TOOL_BLOCKS:
            output.append(line[len(content):])
        else:
            output.append(line)
            unmatched |= _LOCAL_TOOLS_START.rstrip() in content
    if unmatched:
        # Never include system/persona contents in diagnostics.
        logger.debug("AGY 本地工具能力声明未匹配 AstrBot 4.29.4 已知完整文案，保持原样。")
    return "\n".join(output)
