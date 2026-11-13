"""Private stdio MCP relay, launched by AGY. Only stdlib; never imports host tools."""

import asyncio
import json
import sys

LIMIT = 1048576


async def main(path):
    reader = asyncio.StreamReader(limit=LIMIT)
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    pending = {}
    seen = set()

    def send(obj):
        obj["jsonrpc"] = "2.0"
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    async def handle(request):
        ident = request["id"]
        writer = None
        try:
            method = request.get("method")
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "astrbot", "version": "0.5.1"},
                }
            elif method == "ping":
                result = {}
            elif method in {"tools/list", "tools/call"}:
                source, writer = await asyncio.open_unix_connection(
                    path, limit=16 * LIMIT
                )
                writer.write((json.dumps(request) + "\n").encode())
                await writer.drain()
                reply = json.loads(await source.readline())
                send({"id": ident, **reply})
                return
            else:
                send(
                    {
                        "id": ident,
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
                return
            send({"id": ident, "result": result})
        except (OSError, ValueError):
            send(
                {
                    "id": ident,
                    "error": {"code": -32000, "message": "Bridge unavailable"},
                }
            )
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
            pending.pop(ident, None)

    try:
        while line := await reader.readline():
            try:
                request = json.loads(line)
            except ValueError:
                send({"id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                continue
            ident = request.get("id")
            if ident is None:
                if request.get("method") == "notifications/cancelled":
                    target = (request.get("params") or {}).get("requestId")
                    if type(target) in (str, int) and target in pending:
                        pending[target].cancel()
                continue  # initialized / roots/list_changed
            if type(ident) not in (str, int):
                continue
            if ident in seen or len(seen) >= 4096 or len(pending) >= 16:
                send(
                    {
                        "id": ident,
                        "error": {
                            "code": -32600,
                            "message": "Duplicate ID or request limit",
                        },
                    }
                )
                continue
            seen.add(ident)
            pending[ident] = asyncio.create_task(handle(request))
    finally:
        transport.close()
        tasks = list(pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1]))
    except (OSError, ValueError, IndexError):
        raise SystemExit(2) from None
