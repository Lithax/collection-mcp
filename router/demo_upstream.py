"""Tiny upstream MCP server used to try the router and its CLI.

    python demo_upstream.py             # stdio
    python demo_upstream.py http 9000   # streamable HTTP at :9000/mcp
    python demo_upstream.py sse 9001    # legacy SSE at :9001/sse

Then, in another terminal:  python router.py add 127.0.0.1:9000
"""
import asyncio
import struct
import sys
import zlib

from fastmcp import Context, FastMCP
from fastmcp.utilities.types import Image

demo = FastMCP("demo", instructions="Tiny demo server for testing the router.")

@demo.tool(annotations={"readOnlyHint": True})
def add(a: int, b: int) -> int:
    """Add two integers.

    Longer explanation that should only show up in describe_tool."""
    return a + b

@demo.tool
def shout(text: str) -> str:
    """Uppercase some text."""
    return text.upper()

@demo.tool
def delete_everything() -> str:
    """Pretend-dangerous tool, used to test deny_tools."""
    return "boom"

@demo.tool
def fail() -> str:
    """Always raises, to test error passthrough."""
    raise RuntimeError("intentional failure")

def _png() -> bytes:
    """A valid 1x1 red PNG, built by hand so the demo needs no imaging library."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))

PIXEL_PNG = _png()

@demo.tool
def pixel() -> Image:
    """Return a 1x1 PNG, used to test that images pass through the router."""
    return Image(data=PIXEL_PNG, format="png")

_counts: dict[str, int] = {}

@demo.tool
def bump(ctx: Context) -> int:
    """Count calls per session, used to test that each router client gets its own upstream session."""
    key = ctx.session_id
    _counts[key] = _counts.get(key, 0) + 1
    return _counts[key]

@demo.tool
async def ask(ctx: Context) -> str:
    """Ask the client a yes/no question (elicitation), used to test that the router relays it."""
    res = await ctx.elicit("Proceed?", response_type=bool)
    return f"{res.action}:{getattr(res, 'data', None)}"

@demo.tool
async def nap(seconds: float, timeout_seconds: float = 0) -> str:
    """Sleep a while, used to test per-call timeouts and keepalives."""
    await asyncio.sleep(seconds)
    return f"slept {seconds}"

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if mode == "stdio":
        demo.run(transport="stdio")
    else:
        demo.run(transport=mode, host="127.0.0.1", port=int(sys.argv[2]))
