"""Offline ACP fixture. Never connects to a service or accesses credentials."""

import json
import os
import sys
import time

scenario = sys.argv[1]
model = "gemini-test"
effort = "low"
prompt_seq = 0
_test_log = os.environ.get("AGY_TEST_LOG")


def record(event, **fields):
    if not _test_log:
        return
    with open(_test_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": event, **fields}) + "\n")


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


def active_scenario():
    path = os.environ.get("AGY_SCENARIO_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
        if text:
            return text
    return scenario


def send(obj):
    print(json.dumps({"jsonrpc": "2.0", **obj}), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    result = {}
    if method == "initialize":
        record("init", argv=sys.argv[1:])
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
        record("new")
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
        prompt_seq += 1
        blocks = request.get("params", {}).get("prompt", [])
        record(
            "prompt",
            seq=prompt_seq,
            texts=[b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"],
            block_types=[b.get("type") for b in blocks if isinstance(b, dict)],
        )
        cur = active_scenario()
        if cur == "probe":
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
        if cur == "error-with-usage":
            send(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "fake-session",
                        "update": {
                            "sessionUpdate": "usage_update",
                            "usage": {
                                "inputTokens": 650,
                                "cachedReadTokens": 100,
                                "outputTokens": 5,
                            },
                        },
                    },
                }
            )
            send(
                {
                    "id": request["id"],
                    "error": {
                        "code": -32000,
                        "message": "Upstream service unavailable",
                        "data": {
                            "usage": {
                                "inputTokens": 650,
                                "cachedReadTokens": 100,
                                "outputTokens": 5,
                            }
                        },
                    },
                }
            )
            continue
        if cur == "quota-error":
            send(
                {
                    "id": request["id"],
                    "error": {
                        "code": -32000,
                        "message": "Resource has been exhausted (quota). Retry after 12s.",
                        "data": {
                            "retryAfter": 12,
                            "usage": {"inputTokens": 200, "outputTokens": 0},
                        },
                    },
                }
            )
            continue
        if cur == "empty":
            send(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "fake-session",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "   \n  "},
                        },
                    },
                }
            )
            result = {
                "stopReason": "end_turn",
                "usage": {"inputTokens": 500, "outputTokens": 0},
            }
        elif cur == "usage":
            send(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "fake-session",
                        "update": {
                            "sessionUpdate": "usage_update",
                            "usage": {
                                "inputTokens": 1000,
                                "cachedReadTokens": 400,
                                "outputTokens": 20,
                            },
                        },
                    },
                }
            )
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
            result = {
                "stopReason": "end_turn",
                "usage": {
                    "inputTokens": 1200,
                    "cachedReadTokens": 800,
                    "outputTokens": 42,
                },
            }
        elif cur == "no-cache-usage":
            send(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "fake-session",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "fake answer"},
                        },
                    },
                }
            )
            result = {
                "stopReason": "end_turn",
                "usage": {"inputTokens": 300, "outputTokens": 15},
            }
        else:
            answer = f"reply:{prompt_seq}" if cur == "echo" else "fake answer"
            for kind, text in (
                ("agent_thought_chunk", "fake reasoning"),
                ("agent_message_chunk", answer),
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
