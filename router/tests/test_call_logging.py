"""Tests for the router's call log end to end: a real router driven in-memory, demo_upstream.py over stdio.

Run from the router directory:   python -m pytest tests/test_call_logging.py -q
(tests/conftest.py switches the log off for every other test; these point router.CALL_LOG at a temp file)
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import pytest
from fastmcp import Client

ROUTER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROUTER_DIR))

import calllog  # noqa: E402
import router  # noqa: E402


def demo_cfg(**extra):
    return {"command": sys.executable, "args": [str(ROUTER_DIR / "demo_upstream.py")], **extra}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "servers.json"
    monkeypatch.setattr(router, "CONFIG_PATH", path)

    def write(**servers):
        path.write_text(json.dumps({"mcpServers": servers}))

    yield write
    for name in list(router._forwarded):
        router._remove_registered(name)
    router._forwarded.clear()
    router._dynamic.clear()
    asyncio.run(router.pool.shutdown())


@pytest.fixture
def logfile(tmp_path, monkeypatch):
    path = tmp_path / "logs" / "calls.log"
    monkeypatch.setattr(router, "CALL_LOG", calllog.CallLog(path))
    return path


def fields(path):
    """The log as a list of [time, chat, target, args, outcome, duration] rows."""
    return [line.split(" | ") for line in path.read_text().splitlines()]


LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \| (-|[0-9a-f]{6}) \| \S+ \| .* \| .+ \| \d+ms$")


def test_call_tool_is_logged_with_server_tool_and_arguments(cfg, logfile):
    cfg(demo=demo_cfg())

    async def go():
        async with Client(router.mcp) as c:
            await c.call_tool("call_tool", {"server": "demo", "tool": "add", "arguments": {"a": 2, "b": 3}})
            await c.call_tool(
                "call_tool", {"server": "demo", "tool": "shout", "arguments": {"text": "very private words"}}
            )

    asyncio.run(go())
    rows = fields(logfile)
    assert len(rows) == 2
    assert all(LINE.match(line) for line in logfile.read_text().splitlines())
    assert rows[0][2:5] == ["demo.add", "a=2 b=3", "ok"]
    assert rows[1][2:5] == ["demo.shout", "text=<18 chars>", "ok"]
    text = logfile.read_text()
    assert "very private words" not in text  # bodies are reduced to a size
    assert "VERY PRIVATE WORDS" not in text  # and the upstream's result (shout upper-cases) is never logged


def test_forwarded_tool_is_logged_under_its_upstream_name(cfg, logfile):
    cfg(demo=demo_cfg(expose=["add"], prefix="d_"))

    async def go():
        await router._sync_forwarded()
        async with Client(router.mcp) as c:
            await c.call_tool("d_add", {"a": 4, "b": 5})

    asyncio.run(go())
    assert fields(logfile)[0][2:5] == ["demo.add", "a=4 b=5", "ok"]


def test_upstream_errors_are_logged_as_errors(cfg, logfile):
    cfg(demo=demo_cfg(expose=["fail"]))

    async def go():
        await router._sync_forwarded()
        async with Client(router.mcp) as c:
            await c.call_tool("call_tool", {"server": "demo", "tool": "fail"})  # comes back as a result
            await c.call_tool("fail", {}, raise_on_error=False)  # forwarded: flagged is_error
            await c.call_tool("call_tool", {"server": "nope", "tool": "x"}, raise_on_error=False)  # router error

    asyncio.run(go())
    rows = fields(logfile)
    assert len(rows) == 3
    assert rows[0][2] == "demo.fail" and rows[0][4].startswith("ERR ")
    assert rows[1][2] == "demo.fail" and rows[1][4].startswith("ERR ")
    assert rows[2][2] == "nope.x" and rows[2][4].startswith("ERR ") and "unknown server" in rows[2][4]


def test_the_routers_own_tools_are_logged_too(cfg, logfile):
    cfg(demo=demo_cfg())

    async def go():
        async with Client(router.mcp) as c:
            await c.call_tool("list_servers", {})
            await c.call_tool("list_tools", {"server": "demo"})

    asyncio.run(go())
    rows = fields(logfile)
    assert [r[2] for r in rows] == ["router.list_servers", "router.list_tools"]
    assert rows[0][3] == "-" and rows[1][3] == "server=demo"


def test_an_off_log_writes_nothing_and_calls_still_work(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(router, "CALL_LOG", calllog.CallLog(None))
    cfg(demo=demo_cfg())

    async def go():
        async with Client(router.mcp) as c:
            res = await c.call_tool("call_tool", {"server": "demo", "tool": "add", "arguments": {"a": 1, "b": 1}})
            return res.content[0].text.strip()

    assert asyncio.run(go()) == "2"
    assert list(tmp_path.glob("**/*.log")) == []


def test_an_unwritable_log_never_fails_a_call(cfg, tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    monkeypatch.setattr(router, "CALL_LOG", calllog.CallLog(blocker / "calls.log"))
    cfg(demo=demo_cfg())

    async def go():
        async with Client(router.mcp) as c:
            res = await c.call_tool("call_tool", {"server": "demo", "tool": "add", "arguments": {"a": 2, "b": 2}})
            return res.content[0].text.strip()

    assert asyncio.run(go()) == "4"
