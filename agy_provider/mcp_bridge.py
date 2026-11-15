"""Private stdio MCP relay, launched by AGY. Only stdlib; never imports host tools."""

import asyncio
import json
import os
import sys
import threading
from concurrent.futures import TimeoutError as FutureTimeout

if __package__:
    from .bridge_transport import TOKEN_ENV, connect, read_frame
else:
    from bridge_transport import TOKEN_ENV, connect, read_frame

LIMIT = 1048576


async def main(path):
    loop = asyncio.get_running_loop()
    token = os.environ.pop(TOKEN_ENV, "")
    transport = None
    stopped = threading.Event()
    if os.name == "nt":
        # Windows inherited stdio handles need not support overlapped reads.
        # A dedicated daemon reader avoids both Proactor pipe assumptions and
        # shutdown_default_executor hanging on a cancelled blocking readline.
        queue = asyncio.Queue(maxsize=16)

        def pump():
            while not stopped.is_set():
                try:
                    line = sys.stdin.buffer.readline(LIMIT + 1)
                except OSError:
                    line = b""
                if stopped.is_set():
                    return
                try:
                    future = asyncio.run_coroutine_threadsafe(queue.put(line), loop)
                except RuntimeError:
                    return
                while not stopped.is_set():
                    try:
                        future.result(timeout=0.25)
                        break
                    except FutureTimeout:
                        continue
                    except BaseException:
                        return
                if stopped.is_set():
                    future.cancel()
                    return
                if not line or len(line) > LIMIT:
                    return

        threading.Thread(target=pump, name="mcp:stdin", daemon=True).start()
        readline = queue.get
    else:
        reader = asyncio.StreamReader(limit=LIMIT)
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        readline = reader.readline
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
                    "serverInfo": {"name": "astrbot", "version": "0.6.0"},
                }
            elif method == "ping":
                result = {}
            elif method in {"tools/list", "tools/call"}:
                async with asyncio.timeout(5):
                    source, writer = await connect(path, request, token)
                # Covers maximum configured host approval + execution budgets.
                async with asyncio.timeout(2110):
                    reply = await read_frame(source, 16 * LIMIT)
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
        except (OSError, ValueError, TimeoutError, RecursionError):
            send(
                {
                    "id": ident,
                    "error": {"code": -32000, "message": "Bridge unavailable"},
                }
            )
        finally:
            if writer:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 2)
                except (OSError, TimeoutError):
                    pass
            pending.pop(ident, None)

    try:
        while line := await readline():
            if len(line) > LIMIT or not line.endswith(b"\n"):
                raise ValueError("MCP frame exceeds budget")
            try:
                request = json.loads(line)
            except (ValueError, RecursionError):
                send({"id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                continue
            ident = request.get("id")
            if ident is None:
                if request.get("method") == "notifications/cancelled":
                    params = request.get("params")
                    target = params.get("requestId") if isinstance(params, dict) else None
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
        stopped.set()
        if transport is not None:
            transport.close()
        tasks = list(pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main(sys.argv[1]))
    except (OSError, ValueError, IndexError, RecursionError):
        raise SystemExit(2) from None
