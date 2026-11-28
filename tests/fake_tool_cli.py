"""Offline ACP peer driving the real stdio MCP relay and host tool turn."""

import hashlib
import json
import os
import subprocess
import sys


def send(value):
    sys.stdout.buffer.write((json.dumps({"jsonrpc": "2.0", **value}) + "\n").encode())
    sys.stdout.buffer.flush()


def update(value):
    send({"method": "session/update", "params": {"sessionId": "tool-session", "update": value}})


def call_tool(relay, index):
    request = {"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {
        "name": "echo", "arguments": {"value": "中文 🦊"}, "_meta": {
            "antigravity.google/conversation_id": "tool-session",
            "antigravity.google/root_conversation_id": "tool-session"}}}
    # Real AGY asks ACP permission BEFORE issuing tools/call. Built-in
    # requests above stay denied, but the observed host MCP call must be
    # allowed to reach the relay (host approval still happens later).
    tool_call = {
        "sessionUpdate": "tool_call", "toolCallId": f"echo-call-{index}",
        "name": "mcp_astrbot_echo", "status": "pending",
        "rawInput": {"toolSummary": "Echo test", "value": "中文 🦊"},
    }
    update(tool_call)
    send({"id": f"mcp-permission-{index}", "method": "session/request_permission", "params": {
        "sessionId": "tool-session", "toolCall": tool_call,
        "options": [{"optionId": "allow-this-call", "kind": "allow_once"},
                    {"optionId": "deny", "kind": "reject_once"}],
    }})
    permission = json.loads(sys.stdin.buffer.readline())
    if permission.get("result", {}).get("outcome") != {
        "outcome": "selected", "optionId": "allow-this-call"
    }:
        raise SystemExit(10)
    relay.stdin.write((json.dumps(request) + "\n").encode())
    relay.stdin.flush()
    return json.loads(relay.stdout.readline())

relay = None
image_capability = os.environ.get("AGY_TEST_TOOL_IMAGE")
tool_rounds = os.environ.get("AGY_TEST_TOOL_ROUNDS")
log_path = os.environ.get("AGY_TEST_LOG")
prompt_count = 0
try:
    for line in sys.stdin.buffer:
        obj = json.loads(line)
        method = obj.get("method")
        if "id" not in obj:
            continue
        if method == "initialize":
            capabilities = {}
            if image_capability is not None:
                capabilities["promptCapabilities"] = {"image": image_capability == "1"}
            result = {"protocolVersion": 1, "agentCapabilities": capabilities}
        elif method == "session/new":
            server = obj["params"]["mcpServers"][0]
            relay = subprocess.Popen([server["command"], *server["args"]],
                                     env={**os.environ, **server["env"]}, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            result = {"sessionId": "tool-session", "configOptions": [{
                "id": "model", "currentValue": "gemini-test", "options": [{"value": "gemini-test", "name": "Gemini Test"}]}]}
        elif method == "session/prompt":
            prompt_count += 1
            if tool_rounds is not None and prompt_count > 1:
                update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "follow-up"}})
                send({"id": obj["id"], "result": {"stopReason": "end_turn"}})
                continue
            rounds = int(tool_rounds or 1)
            # Force a built-in permission request: it must always be rejected.
            send({"id": "permission-1", "method": "session/request_permission", "params": {
                "sessionId": "tool-session", "toolCall": {
                    "name": "run_command", "toolCallId": "native-call-1", "status": "pending"
                }, "options": [{"optionId": "allow", "kind": "allow_once"}],
            }})
            permission = json.loads(sys.stdin.buffer.readline())
            if permission["result"]["outcome"]["outcome"] != "cancelled":
                raise SystemExit(9)
            for used in (100000, 20000):
                update({"sessionUpdate": "usage_update", "used": used, "size": 200000})
            for index in range(1, rounds + 1):
                reply = call_tool(relay, index)
                if reply.get("error", {}).get("message") == "TOOL_TIMEOUT":
                    result = {"stopReason": "cancelled"}
                    break
                content = reply["result"]["content"]
                if log_path and tool_rounds is not None:
                    with open(log_path, "a", encoding="utf-8") as log:
                        log.write(json.dumps({
                            "event": "tool_result",
                            "types": [part["type"] for part in content],
                            "images": [part["mimeType"] for part in content if part["type"] == "image"],
                            "digests": [
                                hashlib.sha256(part["data"].encode("ascii")).hexdigest()
                                for part in content if part["type"] == "image"
                            ],
                        }) + "\n")
                answer = content[0]["text"]
            else:
                update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": answer}})
                result = {"stopReason": "end_turn", "usage": {"inputTokens": 100, "outputTokens": 5}}
        else:
            result = {}
        send({"id": obj["id"], "result": result})
finally:
    if relay is not None:
        relay.kill()
        relay.wait()
