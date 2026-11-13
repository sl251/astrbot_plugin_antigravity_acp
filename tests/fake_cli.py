"""Offline ACP fixture. Never connects to a service or accesses credentials."""

import json
import sys
import time

scenario = sys.argv[1]
model = "gemini-test"
effort = "low"


def config():
    return {
        "configOptions": [
            {
                "id": "model",
                "currentValue": model,
                "options": [
                    {"value": "gemini-test", "name": "Gemini Test"},
                    {"value": "opaque-id", "name": "Gemini Other"},
                    {"value": "other", "name": "Not supported"},
                ],
            },
            {
                "id": "thought_level",
                "currentValue": effort,
                "options": [
                    {"value": value, "name": value}
                    for value in (
                        ("low", "high") if model == "gemini-test" else ("medium",)
                    )
                ],
            },
        ]
    }


def send(obj):
    print(json.dumps({"jsonrpc": "2.0", **obj}), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    result = {}
    if method == "initialize":
        if scenario in {"startup-exit", "startup-zero", "wrapper-exit", "network-exit"}:
            message = {
                "startup-exit": "Deployment script stopped FAKE-PRIVATE /private/test",
                "startup-zero": "Deployment script stopped FAKE-PRIVATE",
                "wrapper-exit": "Refusing auth bridge outside AGY plugin session directory FAKE-PRIVATE",
                "network-exit": "DNS resolution failed FAKE-PRIVATE",
            }[scenario]
            for char in message:
                sys.stderr.write(char)
                sys.stderr.flush()
            raise SystemExit(0 if scenario == "startup-zero" else 23)
        if scenario in {"wrapper-warning", "auth-with-wrapper-warning"}:
            print(
                "Refusing auth bridge outside AGY plugin session directory FAKE-PRIVATE",
                file=sys.stderr,
                flush=True,
            )
        if scenario == "hang":
            time.sleep(60)
        result = {
            "protocolVersion": 1,
            "agentCapabilities": {
                "promptCapabilities": {"image": True, "private": "must-not-publish"}
            },
        }
    elif method == "session/new":
        if scenario == "post-init-exit":
            raise SystemExit(23)
        if scenario in {"auth", "auth-with-wrapper-warning"}:
            send(
                {
                    "id": request["id"],
                    "error": {
                        "code": -32000,
                        "message": "Authentication required FAKE-PRIVATE",
                    },
                }
            )
            continue
        result = {"sessionId": "fake-session", **config()}
        if scenario == "no-models":
            result = {"sessionId": "fake-session"}
    elif method == "session/set_config_option":
        if request["params"]["configId"] == "model":
            model = request["params"]["value"]
        else:
            effort = request["params"]["value"]
        result = config()
    elif method == "session/prompt":
        if scenario == "probe":
            send(
                {
                    "id": request["id"],
                    "error": {
                        "code": -32000,
                        "message": "Probe sent a forbidden prompt",
                    },
                }
            )
            continue
        if scenario in {"retry-quota-only", "retry-eof-only", "retry-then-answer"}:
            chunks = {
                "retry-quota-only": (
                    "⚠ API error (attempt 1): UNAVAILABLE ",
                    "(code 503): No capacity available for model gemini-2.5-pro on the server\n",
                    "⚠️ API error (attempt 2): UNAVAILABLE (code 503): No capacity available for model gemini-2.5-pro on the server",
                ),
                "retry-eof-only": (
                    "⚠ API error (attempt 1):",
                    " stream error: unexpected EOF\n",
                ),
                "retry-then-answer": (
                    "⚠️ API error (attempt 1): unexpected EOF\n",
                    "真实回复内容",
                ),
            }[scenario]
            for text in chunks:
                send(
                    {
                        "method": "session/update",
                        "params": {
                            "sessionId": "fake-session",
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": text},
                            },
                        },
                    }
                )
            send({"id": request["id"], "result": {"stopReason": "end_turn"}})
            continue
        for kind, text in (
            ("agent_thought_chunk", "fake reasoning"),
            ("agent_message_chunk", "fake answer"),
        ):
            send(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "fake-session",
                        "update": {
                            "sessionUpdate": kind,
                            "content": {"type": "text", "text": text},
                        },
                    },
                }
            )
        result = {"stopReason": "end_turn"}
    else:
        send(
            {
                "id": request["id"],
                "error": {"code": -32601, "message": "method not found"},
            }
        )
        continue
    send({"id": request["id"], "result": result})
