"""Deterministic ACP peer which launches the real stdio MCP relay.

No network, model, account or tool handler runs in this process. Host-provided
results must traverse the real MCP socket before the final answer is emitted.
"""

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path


def latest_text(blocks):
    text = "".join(block.get("text", "") for block in blocks)
    start = text.find("<astrbot_history>\n")
    end = text.find("\n</astrbot_history>")
    if start >= 0 and end >= 0:
        raw = text[start + len("<astrbot_history>\n") : end]
        history = json.loads(raw)["messages"]
        trailing = text[end + len("\n</astrbot_history>") :].strip()
        if trailing:
            return trailing
        latest = next(
            message for message in reversed(history) if message["role"] == "user"
        )
        content = latest["content"]
        return (
            content
            if isinstance(content, str)
            else "".join(part.get("text", "") for part in content)
        )
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    session = "fake-" + uuid.uuid4().hex
    mcp = None
    mcp_id = 0
    tool_name = None

    def record(kind, **values):
        with args.log.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps({"kind": kind, "session": session, **values}) + "\n"
            )

    def send(value):
        print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)

    def update(update_kind, **values):
        send(
            {
                "method": "session/update",
                "params": {
                    "sessionId": session,
                    "update": {"sessionUpdate": update_kind, **values},
                },
            }
        )

    def mcp_send(method, params=None):
        nonlocal mcp_id
        mcp_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": mcp_id,
            "method": method,
            "params": params or {},
        }
        mcp.stdin.write(json.dumps(request) + "\n")
        mcp.stdin.flush()
        return mcp_id

    def mcp_read():
        line = mcp.stdout.readline()
        if not line:
            raise RuntimeError("MCP relay EOF")
        return json.loads(line)

    def mcp_request(method, params=None):
        request_id = mcp_send(method, params)
        response = mcp_read()
        if response["id"] != request_id:
            raise RuntimeError("MCP ID mismatch")
        return response

    def start_call(name, arguments):
        update(
            "tool_call",
            toolCallId=f"call_{mcp_id + 1}",
            name="mcp_astrbot_" + name,
            status="in_progress",
            rawInput=arguments,
            args=arguments,
            kind="other",
            title="Run host tool",
        )
        record("mcp_call", name=name, arguments=arguments, id=mcp_id + 1)
        return mcp_send(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
                "_meta": {
                    "antigravity.google/conversation_id": session,
                    "antigravity.google/root_conversation_id": session,
                },
            },
        )

    def finish_call(response, name):
        record("mcp_result", result=response)
        if "error" in response:
            raise RuntimeError("MCP tool failed")
        update(
            "tool_call_update",
            toolCallId=f"call_{response['id']}",
            name="mcp_astrbot_" + name,
            status="completed",
            kind="other",
            title="Run host tool",
        )
        return response["result"]["content"][0]["text"]

    try:
        for raw in sys.stdin:
            request = json.loads(raw)
            method, ident = request.get("method"), request.get("id")
            if method == "initialize":
                send(
                    {
                        "id": ident,
                        "result": {
                            "protocolVersion": 1,
                            "agentCapabilities": {
                                "promptCapabilities": {"image": True}
                            },
                        },
                    }
                )
            elif method == "session/new":
                servers = request["params"]["mcpServers"]
                if servers:
                    descriptor = servers[0]
                    mcp = subprocess.Popen(
                        [descriptor["command"], *descriptor["args"]],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )
                    discovered = mcp_request("server/discover")
                    if discovered["error"]["code"] != -32601:
                        raise RuntimeError("MCP discover contract")
                    initialized = mcp_request(
                        "initialize",
                        {
                            "protocolVersion": "2025-11-25",
                            "clientInfo": {
                                "name": "antigravity-client",
                                "version": "v1.0.0",
                            },
                        },
                    )
                    if initialized["result"]["protocolVersion"] != "2025-11-25":
                        raise RuntimeError("MCP protocol contract")
                    mcp.stdin.write(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "notifications/roots/list_changed",
                            }
                        )
                        + "\n"
                    )
                    mcp.stdin.flush()
                    listing = mcp_request("tools/list")
                    tool_name = listing["result"]["tools"][0]["name"]
                record("session_new", mcp_loaded=bool(servers))
                send(
                    {
                        "id": ident,
                        "result": {
                            "sessionId": session,
                            "models": {
                                "currentModelId": "gemini-test",
                                "availableModels": [
                                    {"modelId": "gemini-test", "name": "Gemini Test"}
                                ],
                            },
                        },
                    }
                )
            elif method == "session/prompt":
                text = latest_text(request["params"]["prompt"])
                record("prompt_started", text=text)
                if text.startswith("prefix:"):
                    for chunk in ("Before ", "tool. "):
                        update(
                            "agent_message_chunk",
                            content={"type": "text", "text": chunk},
                        )
                if not mcp:
                    answer = "Text: " + text
                elif text.startswith("multistep:"):
                    start_call("lookup_fixture", {"message": "fixture-key"})
                    lookup_result = finish_call(mcp_read(), "lookup_fixture")
                    start_call(tool_name, {"message": lookup_result})
                    answer = "Answer: " + finish_call(mcp_read(), tool_name)
                elif text.startswith("parallel:"):
                    # Both MCP requests are outstanding before either result is
                    # read. Result IDs, not arrival order, determine the answer.
                    request_ids = [
                        start_call(tool_name, {"message": text + suffix})
                        for suffix in ("/a", "/b")
                    ]
                    results = {}
                    for _ in request_ids:
                        response = mcp_read()
                        results[response["id"]] = finish_call(response, tool_name)
                    answer = "Answer: " + " | ".join(
                        results[request_id] for request_id in request_ids
                    )
                else:
                    start_call(tool_name, {"message": text})
                    answer = "Answer: " + finish_call(mcp_read(), tool_name)
                update("agent_message_chunk", content={"type": "text", "text": answer})
                record("prompt_done", answer=answer)
                send({"id": ident, "result": {"stopReason": "end_turn"}})
            elif method == "session/cancel":
                record("cancelled")
            elif ident is not None:
                send({"id": ident, "result": {}})
    finally:
        if mcp:
            mcp.stdin.close()
            try:
                mcp.wait(timeout=2)
            except subprocess.TimeoutExpired:
                mcp.kill()
                mcp.wait()


if __name__ == "__main__":
    main()
