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
        if scenario == "hang":
            time.sleep(60)
        result = {
            "protocolVersion": 1,
            "agentCapabilities": {
                "promptCapabilities": {"image": True, "private": "must-not-publish"}
            },
        }
    elif method == "session/new":
        if scenario == "auth":
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
