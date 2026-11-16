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

# AppContainer tail appended by AstrBot v4.30.0-alpha.1 on Windows only
# (computer_use.py commit 6a782b7, _build_local_mode_prompt return tail).
# Extracted mechanically from upstream source; do not retype by hand.
_WINDOWS_APPCONTAINER_TAIL = (
    ' Restricted Windows execution uses AppContainer. '
    'PowerShell starts at Workspace:\\, a PSDrive mapped to the real workspace. '
    'Use relative paths or Workspace:\\ paths with cmdlets; native programs need relative or actual filesystem paths, not PSDrive paths. '
    'An InitializeDefaultDrives warning can occur because ancestor directories are inaccessible; it does not imply the workspace command failed. '
    'Access-denied errors on absolute paths can be retried with a workspace-relative path or astrbot_execute_python. '
    'Python/runtime files are read-only; use the workspace for temporary files and caches. '
    'Network capabilities do not guarantee localhost access, including local proxy ports. '
    'Host proxy environment variables are not inherited. '
    'Console interrupts are unavailable; use session terminate when needed. '
    'Job limits cover CPU time, process count and committed memory; per-file size and open-handle limits are unavailable. '
    'Missing startup authorization requires restarting AstrBot; tool calls never request administrator access or retry without isolation.'
)

# Exact copies of _build_local_mode_prompt() in AstrBot v4.29.4:
# https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/astr_main_agent.py
# and its v4.30.0-alpha.1 continuation (Windows gains the AppContainer tail):
# https://github.com/AstrBotDevs/AstrBot/blob/6a782b7be6591aa36b3e30f9e836c7c061c9ae50/astrbot/core/astr_agent_runtime/main_agent/computer_use.py
# This is deliberately version-coupled: new wording/OS values stay unchanged.
# These host-local shell/session claims do not describe AGY's session MCP tools,
# so they are removed in both chat and tools mode. No tool permissions change.
_LOCAL_TOOL_BLOCKS = frozenset(
    _LOCAL_TOOLS_START
    + f"Current operating system: {system}. {hint} "
    + _LOCAL_TOOLS_END
    + tail
    for system, hint, tail in (
        ("Linux", _UNIX_HINT, ""),
        ("Darwin", _UNIX_HINT, ""),
        ("Unknown", _UNIX_HINT, ""),
        (
            "Windows",
            "The runtime shell is PowerShell 7 (pwsh.exe). "
            "Use PowerShell 7-compatible syntax and cmdlets, and do not "
            "assume a full Unix userland or GNU utilities are available.",
            "",
        ),
        (
            "Windows",
            "The runtime shell is Windows PowerShell 5.1 (powershell.exe). "
            "Use Windows PowerShell 5.1-compatible syntax and cmdlets; do not use "
            "PowerShell 7-only syntax or assume Unix commands like cat/ls/grep are available.",
            "",
        ),
        (
            "Windows",
            "The runtime shell is PowerShell 7 (pwsh.exe). "
            "Use PowerShell 7-compatible syntax and cmdlets, and do not "
            "assume a full Unix userland or GNU utilities are available.",
            _WINDOWS_APPCONTAINER_TAIL,
        ),
        (
            "Windows",
            "The runtime shell is Windows PowerShell 5.1 (powershell.exe). "
            "Use Windows PowerShell 5.1-compatible syntax and cmdlets; do not use "
            "PowerShell 7-only syntax or assume Unix commands like cat/ls/grep are available.",
            _WINDOWS_APPCONTAINER_TAIL,
        ),
    )
)

# Structured host marker from AstrBot v4.30.0-alpha.1 (commit 6a782b7):
# req.system_prompt += (
#     f'\n<execution_environment runtime="{config.computer_use_runtime}">\n'
#     f"{runtime_prompt.strip()}\n"
#     "\n</execution_environment>\n"
# )
# The tag occupies its own line; runtime_prompt bundles the host local-tool
# sections below. When no matching local tools exist in this session the whole
# block is not forwarded (fail-closed for known host shapes).
_EXEC_ENV_OPEN = '<execution_environment runtime="local">'
_EXEC_ENV_CLOSE = "</execution_environment>"
_EXEC_ENV_TAG_PREFIX = "<execution_environment"
# Host section headers that must appear inside a genuine host execution
# environment block; blocks without any of them are left untouched so a
# persona using the same tag name is never misclassified.
_HOST_SECTION_MARKERS = (
    "## Shell and sessions",
    "## Workspace",
    "## Execution policy",
)


def _remove_execution_env_blocks(lines, reasons):
    """Strip complete <execution_environment runtime="local"> blocks.

    Only lines that exactly match the known host open/close tags enter the
    state machine. Partial (unclosed), nested, or content-unverified blocks
    are kept verbatim and reported via ``reasons``.
    """
    output = []
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]
        content = line.removesuffix("\r")
        if content == _EXEC_ENV_OPEN:
            close_index = None
            restart = None
            nested = False
            j = i + 1
            while j < total:
                inner = lines[j].removesuffix("\r")
                if inner == _EXEC_ENV_CLOSE:
                    close_index = j
                    break
                if inner == _EXEC_ENV_OPEN:
                    # A new exact open tag before this block closes: treat the
                    # earlier fragment as partial (kept) and restart collection
                    # from the new tag, so a stray persona tag never swallows a
                    # genuine trailing host block.
                    restart = j
                    break
                if inner.startswith(_EXEC_ENV_TAG_PREFIX):
                    nested = True
                j += 1
            if restart is not None:
                reasons.add("tag_partial")
                output.extend(lines[i:restart])
                i = restart
                continue
            if close_index is None:
                reasons.add("tag_partial")
                output.extend(lines[i:])
                return output
            if nested:
                reasons.add("tag_nested")
                output.extend(lines[i : close_index + 1])
                i = close_index + 1
                continue
            block_body = [
                lines[k].removesuffix("\r") for k in range(i + 1, close_index)
            ]
            if any(
                body.rstrip() == marker
                for marker in _HOST_SECTION_MARKERS
                for body in block_body
            ):
                # Known host shape: drop the entire block, tags included.
                i = close_index + 1
                continue
            reasons.add("tag_unverified")
            output.extend(lines[i : close_index + 1])
            i = close_index + 1
            continue
        if content == _EXEC_ENV_CLOSE:
            # Orphan close tag outside any tracked block: keep verbatim.
            reasons.add("tag_orphan_close")
            output.append(line)
            i += 1
            continue
        if content.startswith(_EXEC_ENV_TAG_PREFIX) and content != _EXEC_ENV_OPEN:
            # <execution_environment with an unknown runtime/attributes: keep.
            reasons.add("tag_variant")
            output.append(line)
            i += 1
            continue
        output.append(line)
        i += 1
    return output


def filter_host_capabilities(system: str) -> str:
    """Strip known host local-tool declarations, preserving everything else.

    Exact-match host sentences (AstrBot 4.29.4 / 4.30 wording) and complete
    ``<execution_environment runtime="local">`` blocks carrying host section
    headers are removed. Unknown or partial shapes fail open to the original
    text with a debug reason; known host shapes fail closed.
    """
    if (
        _LOCAL_TOOLS_START.rstrip() not in system
        and _EXEC_ENV_TAG_PREFIX not in system
    ):
        return system
    reasons = set()
    lines = _remove_execution_env_blocks(system.split("\n"), reasons)
    # Exact-match pass runs on lines already emitted by the tag pass: a 4.29.4
    # block that survived inside an unverified/partial tag still loses its own
    # exact line, matching v0.6.1 behaviour.
    output = []
    for line in lines:
        content = line.removesuffix("\r")
        if content in _LOCAL_TOOL_BLOCKS:
            output.append(line[len(content) :])
        else:
            output.append(line)
            if _LOCAL_TOOLS_START.rstrip() in content:
                reasons.add("text_unmatched")
    if reasons:
        # Never include system/persona contents in diagnostics.
        logger.debug(
            "AGY host capability declaration handling: " + ", ".join(sorted(reasons))
        )
    return "\n".join(output)
