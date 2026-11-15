"""Private local MCP transport; also importable by the standalone stdio relay."""

import asyncio
import hashlib
import hmac
import json
import secrets

VERSION = 1
FRAME_LIMIT = 1048576
TOKEN_ENV = "ASTRBOT_MCP_BRIDGE_TOKEN"
HANDSHAKE_TIMEOUT = 5


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True, allow_nan=False).encode("utf-8")


def _reject_constant(_):
    raise ValueError("Invalid JSON number")


async def read_frame(reader, limit=FRAME_LIMIT):
    line = await reader.readline()
    if not line.endswith(b"\n") or len(line) > limit:
        raise ValueError("Invalid bridge frame")
    value = json.loads(line, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Invalid bridge object")
    return value


def proof(token: str, nonce: str, request: dict) -> str:
    return hmac.new(token.encode("ascii"), nonce.encode("ascii") + b"\n" + encode(request),
                    hashlib.sha256).hexdigest()


class BridgeTransport:
    """One session key and a fresh challenge for every TCP connection.

    The secret only travels via the child's MCP environment. The socket carries
    a challenge-bound request MAC, preventing recorded requests from replaying
    on a new connection. POSIX retains its mode-0600 Unix socket protocol.
    """

    def __init__(self, tcp: bool = False):
        self.tcp = tcp
        self.token = secrets.token_hex(32) if tcp else ""

    async def receive(self, reader, writer):
        if not self.tcp:
            return await read_frame(reader)
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            hello = await read_frame(reader, 1024)
            client_nonce = hello.get("nonce")
            if (
                type(hello.get("version")) is not int or hello["version"] != VERSION
                or not isinstance(client_nonce, str) or len(client_nonce) != 64
                or any(c not in "0123456789abcdef" for c in client_nonce)
            ):
                raise ValueError("Invalid bridge hello")
            nonce = secrets.token_hex(32)
            challenge = {"version": VERSION, "nonce": nonce, "max_frame": FRAME_LIMIT}
            writer.write(encode({**challenge, "mac": proof(self.token, client_nonce, challenge)}) + b"\n")
            await writer.drain()
            envelope = await read_frame(reader)
            request, signature = envelope.get("request"), envelope.get("mac")
            if (
                type(envelope.get("version")) is not int or envelope["version"] != VERSION
                or not isinstance(request, dict) or not isinstance(signature, str)
                or len(signature) != 64 or not signature.isascii()
                or not hmac.compare_digest(signature, proof(self.token, nonce, request))
            ):
                raise ValueError("Bridge authentication failed")
            return request


async def connect(endpoint: str, request: dict, token: str):
    """Connect only to literal loopback or an explicit private Unix path."""
    if not endpoint.startswith("tcp:"):
        reader, writer = await asyncio.open_unix_connection(endpoint, limit=16 * FRAME_LIMIT)
        try:
            data = encode(request) + b"\n"
            if len(data) > FRAME_LIMIT:
                raise ValueError("Bridge frame too large")
            writer.write(data)
            await writer.drain()
            return reader, writer
        except BaseException:
            writer.close()
            raise
    port = int(endpoint[4:])
    if not 1 <= port <= 65535 or len(token) != 64 or not token.isascii():
        raise ValueError("Invalid bridge endpoint")
    reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=16 * FRAME_LIMIT)
    try:
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            client_nonce = secrets.token_hex(32)
            writer.write(encode({"version": VERSION, "nonce": client_nonce}) + b"\n")
            await writer.drain()
            challenge = await read_frame(reader, 1024)
            nonce = challenge.get("nonce")
            signature = challenge.pop("mac", None)
            if (
                type(challenge.get("version")) is not int or challenge["version"] != VERSION
                or challenge.get("max_frame") != FRAME_LIMIT
                or not isinstance(nonce, str) or len(nonce) != 64
                or any(c not in "0123456789abcdef" for c in nonce)
                or not isinstance(signature, str) or len(signature) != 64
                or not signature.isascii()
                or not hmac.compare_digest(signature, proof(token, client_nonce, challenge))
            ):
                raise ValueError("Invalid bridge challenge")
            data = encode({"version": VERSION, "request": request,
                           "mac": proof(token, nonce, request)}) + b"\n"
            if len(data) > FRAME_LIMIT:
                raise ValueError("Bridge frame too large")
            writer.write(data)
            await writer.drain()
        return reader, writer
    except BaseException:
        writer.close()
        raise
