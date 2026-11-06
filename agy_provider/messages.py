"""Normalize AstrBot text messages and replay bounded history only on rebuild."""

import hashlib
import json
from dataclasses import dataclass

from .errors import AgyError


def text_content(value: object, *, assistant: bool = False) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise AgyError("UNSUPPORTED", "消息内容必须为文本或文本块列表。")
    parts = []
    for part in value:
        if hasattr(part, "model_dump"):
            part = part.model_dump()
        # AstrBot persists reasoning as ThinkPart alongside assistant text.
        # It is neither visible answer text nor replayable user input.
        if assistant and isinstance(part, dict) and part.get("type") == "think":
            continue
        if (
            not isinstance(part, dict)
            or part.get("type") != "text"
            or not isinstance(part.get("text"), str)
        ):
            raise AgyError(
                "UNSUPPORTED", "首版仅支持文本；图片、音频和其他内容块尚未接入。"
            )
        parts.append(part["text"])
    return "".join(parts)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


@dataclass
class ConversationInput:
    system: str
    messages: list[dict[str, str]]

    @property
    def hashes(self) -> list[str]:
        return [digest(message) for message in self.messages]

    @property
    def system_hash(self) -> str:
        return digest(self.system)


def normalize(
    contexts: list[dict] | None,
    prompt: str | None,
    system_prompt: str | None,
    extra_parts: list | None,
    limit: int,
) -> ConversationInput:
    messages = []
    systems = []
    if system_prompt:
        systems.append(system_prompt)
    for raw in contexts or []:
        role = raw.get("role")
        if raw.get("tool_calls") or role == "tool":
            raise AgyError(
                "TOOLS_DISABLED",
                "此会话包含工具调用记录；首版不支持工具桥，请新建纯文本会话。",
            )
        if role not in {"system", "developer", "user", "assistant"}:
            raise AgyError("UNSUPPORTED", "消息包含不支持的角色。")
        content = text_content(raw.get("content"), assistant=role == "assistant")
        if role in {"system", "developer"}:
            if content and content not in systems:
                systems.append(content)
        else:
            messages.append({"role": role, "content": content})
    if prompt is not None:
        if not isinstance(prompt, str):
            raise AgyError("INPUT", "prompt 必须为字符串。")
        extra = text_content(extra_parts) if extra_parts else ""
        messages.append(
            {"role": "user", "content": prompt + ("\n" + extra if extra else "")}
        )
    elif not messages and extra_parts:
        messages.append({"role": "user", "content": text_content(extra_parts)})
    # In contexts-only mode the AstrBot runner already assembled extra parts.
    # Do not append them again, even if the runner passes the same argument.
    if not messages or messages[-1]["role"] != "user":
        raise AgyError("INPUT", "需要一条最新的用户文本消息。")
    if not messages[-1]["content"].strip():
        raise AgyError("INPUT", "用户文本为空。")
    system = "\n\n".join(systems)
    if len(system.encode()) > limit:
        raise AgyError("INPUT_LIMIT", "系统提示超过插件字节预算，请缩短人格/系统提示。")
    return ConversationInput(system, messages)


def replay(messages: list[dict[str, str]], budget: int) -> tuple[str, int]:
    latest = messages[-1]["content"]
    if len(latest.encode()) > budget:
        raise AgyError(
            "INPUT_LIMIT", "最新输入超过字节预算，请拆分输入；插件不会截断当前问题。"
        )
    if len(messages) == 1:
        return latest, 0

    def encode(history, omitted):
        body = json.dumps(
            {"omitted_messages": omitted, "messages": history}, ensure_ascii=False
        )
        # Keep delimiter-like user text inside JSON rather than the wrapper.
        body = body.replace("<", "\\u003c").replace(">", "\\u003e")
        return f"<astrbot_history>\n{body}\n</astrbot_history>\n\n{latest}"

    # Keep a contiguous tail of complete turns. The current input is never cut.
    history = messages[:-1]
    omitted = 0
    while history:
        packed = encode(history, omitted)
        if len(packed.encode()) <= budget:
            return packed, omitted
        history = history[1:]
        omitted += 1
        while history and history[0]["role"] == "assistant":
            history = history[1:]
            omitted += 1
    packed = encode([], omitted)
    if len(packed.encode()) > budget:
        raise AgyError(
            "INPUT_LIMIT", "当前输入已占满预算，无法附加历史截断标记，请缩短输入。"
        )
    return packed, omitted
