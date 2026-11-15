"""Offline ACP peer driving the real stdio MCP relay and host tool turn."""

import json
import os
import subprocess
import sys


def send(value):
    sys.stdout.buffer.write((json.dumps({"jsonrpc": "2.0", **value}) + "\n").encode())
    sys.stdout.buffer.flush()


def update(value):
    send({"method": "session/update", "params": {"sessionId": "tool-session", "update": value}})


relay = None
try:
    for line in sys.stdin.buffer:
        obj = json.loads(line)
        method = obj.get("method")
        if "id" not in obj:
            continue
        if method == "initialize":
            result = {"protocolVersion": 1, "agentCapabilities": {}}
        elif method == "session/new":
            server = obj["params"]["mcpServers"][0]
            relay = subprocess.Popen([server["command"], *server["args"]],
                                     env={**os.environ, **server["env"]}, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            result = {"sessionId": "tool-session", "configOptions": [{
                "id": "model", "currentValue": "gemini-test", "options": [{"value": "gemini-test", "name": "Gemini Test"}]}]}
        elif method == "session/prompt":
            # Force a built-in permission request: it must always be rejected.
            send({"id": "permission-1", "method": "session/request_permission", "params": {"sessionId": "tool-session"}})
            permission = json.loads(sys.stdin.buffer.readline())
            if permission["result"]["outcome"]["outcome"] != "cancelled":
                raise SystemExit(9)
            for used in (100000, 20000):
                update({"sessionUpdate": "usage_update", "used": used, "size": 200000})
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "echo", "arguments": {"value": "中文 🦊"}, "_meta": {
                    "antigravity.google/conversation_id": "tool-session",
                    "antigravity.google/root_conversation_id": "tool-session"}}}
            relay.stdin.write((json.dumps(request) + "\n").encode())
            relay.stdin.flush()
            reply = json.loads(relay.stdout.readline())
            result = reply["result"]
            answer = result["content"][0]["text"]
            update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": answer}})
            result = {"stopReason": "end_turn", "usage": {"inputTokens": 100, "outputTokens": 5}}
        else:
            result = {}
        send({"id": obj["id"], "result": result})
finally:
    if relay is not None:
        relay.kill()
        relay.wait()
