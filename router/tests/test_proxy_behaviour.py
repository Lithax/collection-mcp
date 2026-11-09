"""Tests for what the router carries between its clients and the upstream servers:
images, per-client upstream sessions, questions the upstream asks (elicitation), per-call timeouts.

Run from the router directory:   python -m pytest tests -q
Upstream is demo_upstream.py, over stdio or (for session tests) streamable HTTP; the router is driven
in-memory. Downstream clients that need their own session use mode="legacy" (a stateful connection).
"""
import asyncio
import base64
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

ROUTER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROUTER_DIR))

import demo_upstream  # noqa: E402
import router  # noqa: E402

mcp_types = router.mcp_types


def demo_cfg(**extra):
    return {"command": sys.executable, "args": [str(ROUTER_DIR / "demo_upstream.py")], **extra}


def run(coro):
    return asyncio.run(coro)


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
def http_upstream():
    """demo_upstream.py serving streamable HTTP on a free port; yields its URL. List this fixture
    BEFORE `cfg` so the router's pool is shut down while the server is still alive."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, str(ROUTER_DIR / "demo_upstream.py"), "http", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if time.time() > deadline:
                    raise RuntimeError("demo upstream did not start")
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def block(**kw):
    return mcp_types.TextContent(**kw) if kw["type"] == "text" else {
        "image": mcp_types.ImageContent,
        "resource_link": mcp_types.ResourceLink,
        "resource": mcp_types.EmbeddedResource,
    }[kw["type"]].model_validate(kw)


def result(*blocks, structured=None):
    return SimpleNamespace(content=list(blocks), structured_content=structured, is_error=False)


def image(data="QUJD"):
    return block(type="image", data=data, mimeType="image/png")


def mime_of(b):
    return getattr(b, "mime_type", None) or getattr(b, "mimeType", None)


# ---- converting results ----------------------------------------------------------------------
def test_blocks_keep_images_in_order():
    img = image()
    out = router._blocks(result(block(type="text", text="a"), img, block(type="text", text="b")))
    assert [b.type for b in out] == ["text", "image", "text"]
    assert out[1] is img and [out[0].text, out[2].text] == ["a", "b"]


def test_blocks_join_neighbouring_text_like_before():
    out = router._blocks(result(block(type="text", text="a"), block(type="text", text="b")))
    assert [b.text for b in out] == ["a\nb"]


def test_blocks_cap_text_across_blocks(monkeypatch):
    monkeypatch.setattr(router, "MAX_CHARS", 10)
    out = router._blocks(result(block(type="text", text="12345678"), image(), block(type="text", text="abcdefgh")))
    assert [b.type for b in out] == ["text", "image", "text"]
    assert out[0].text == "12345678"
    assert out[2].text == "ab\n…[truncated, 6 more chars]"


def test_blocks_oversized_image_becomes_text(monkeypatch):
    monkeypatch.setattr(router, "MAX_IMAGE_CHARS", 2)
    out = router._blocks(result(image("QUJD")))
    assert [b.type for b in out] == ["text"]
    assert "omitted" in out[0].text and "image/png" in out[0].text


def test_blocks_nothing_and_structured():
    assert router._blocks(result())[0].text == "(no output)"
    assert json.loads(router._blocks(result(structured={"a": 1}))[0].text) == {"a": 1}


def test_blocks_resources_become_text():
    link = block(type="resource_link", name="n", uri="file:///tmp/x")
    emb = block(type="resource", resource={"uri": "file:///tmp/y", "text": "hello"})
    text = router._blocks(result(link, emb))[0].text
    assert "[resource link: file:///tmp/x" in text and "hello" in text


def test_with_prefix():
    out = router._with_prefix([block(type="text", text="x"), image()], "[err]")
    assert [b.type for b in out] == ["text", "image"] and out[0].text == "[err]\nx"
    out = router._with_prefix([image()], "[err]")
    assert [b.type for b in out] == ["text", "image"] and out[0].text == "[err]"


def test_call_timeout(monkeypatch):
    monkeypatch.setattr(router, "CALL_TIMEOUT", 60.0)
    monkeypatch.setattr(router, "MAX_CALL_TIMEOUT", 900.0)
    monkeypatch.setattr(router, "TIMEOUT_GRACE", 10.0)
    t = router._call_timeout
    assert t(None) == 60 and t({}) == 60
    assert t({"timeout_seconds": 5}) == 60  # never shorter than the default
    assert t({"timeout_seconds": 300}) == 310
    assert t({"timeout_seconds": 5000}) == 900  # capped
    assert t({"timeout_seconds": True}) == 60 and t({"timeout_seconds": "300"}) == 60 and t({"timeout_seconds": -1}) == 60


# ---- images, end to end ----------------------------------------------------------------------
def test_images_pass_through_forwarded_and_call_tool(cfg):
    cfg(demo=demo_cfg(expose=["pixel"]))
    want = base64.b64encode(demo_upstream.PIXEL_PNG).decode()

    async def go():
        await router._sync_forwarded()
        async with Client(router.mcp) as c:
            direct = await c.call_tool("pixel", {})
            generic = await c.call_tool("call_tool", {"server": "demo", "tool": "pixel"})
            for res in (direct, generic):
                imgs = [b for b in res.content if b.type == "image"]
                assert len(imgs) == 1, res.content
                assert imgs[0].data == want and mime_of(imgs[0]) == "image/png"

    run(go())


def test_oversized_image_falls_back_to_text(cfg, monkeypatch):
    cfg(demo=demo_cfg())

    async def go():
        async with Client(router.mcp) as c:
            monkeypatch.setattr(router, "MAX_IMAGE_CHARS", 10)
            res = await c.call_tool("call_tool", {"server": "demo", "tool": "pixel"})
            assert [b.type for b in res.content] == ["text"] and "omitted" in res.content[0].text

    run(go())


# ---- one upstream session per downstream client ----------------------------------------------
def scoped(server="demo"):
    return [k for k in router.pool._clients if k[0] == server and k[1] is not None]


async def bump(c):
    return int((await c.call_tool("bump", {})).content[0].text)


def listed(res):
    data = res.data if res.data is not None else res.structured_content
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    return {s["name"]: s for s in data}


def test_each_client_gets_its_own_upstream_session(http_upstream, cfg):
    cfg(demo={"url": http_upstream, "mode": "legacy", "expose": ["bump"]})

    async def go():
        await router._sync_forwarded()
        async with Client(router.mcp, mode="legacy") as a, Client(router.mcp, mode="legacy") as b:
            assert [await bump(a), await bump(a)] == [1, 2]
            assert await bump(b) == 1  # not 3: b has its own upstream session
            assert await bump(a) == 3
            assert len(scoped()) == 2
            assert listed(await a.call_tool("list_servers", {}))["demo"]["sessions"] == "per_client"

    run(go())


def test_sessions_shared_switches_it_off(http_upstream, cfg):
    cfg(demo={"url": http_upstream, "mode": "legacy", "sessions": "shared", "expose": ["bump"]})

    async def go():
        await router._sync_forwarded()
        async with Client(router.mcp, mode="legacy") as a, Client(router.mcp, mode="legacy") as b:
            assert [await bump(a), await bump(a), await bump(b)] == [1, 2, 3]
            assert scoped() == []
            assert listed(await a.call_tool("list_servers", {}))["demo"]["sessions"] == "shared"

    run(go())


def test_stdio_servers_stay_shared(cfg):
    cfg(demo=demo_cfg())

    async def go():
        async with Client(router.mcp, mode="legacy") as c:
            assert listed(await c.call_tool("list_servers", {}))["demo"]["sessions"] == "shared"

    run(go())


def test_idle_sessions_are_closed(http_upstream, cfg, monkeypatch):
    monkeypatch.setattr(router, "SESSION_IDLE", 0.3)
    cfg(demo={"url": http_upstream, "mode": "legacy", "expose": ["bump"]})

    async def go():
        await router._sync_forwarded()
        async with Client(router.mcp, mode="legacy") as a:
            await bump(a)
            assert len(scoped()) == 1
            await asyncio.sleep(0.6)
            async with Client(router.mcp, mode="legacy") as b:
                await bump(b)  # connecting b closes a's idle session
                assert len(scoped()) == 1

    run(go())


def test_surplus_sessions_are_closed_least_recently_used_first(http_upstream, cfg, monkeypatch):
    monkeypatch.setattr(router, "MAX_SESSIONS", 2)
    cfg(demo={"url": http_upstream, "mode": "legacy", "expose": ["bump"]})

    async def go():
        await router._sync_forwarded()
        async with Client(router.mcp, mode="legacy") as a, Client(router.mcp, mode="legacy") as b, Client(
            router.mcp, mode="legacy"
        ) as c:
            await bump(a)
            await bump(b)
            first = set(scoped())
            oldest = min(first, key=lambda k: router.pool._clients[k].last_used)
            await bump(c)
            assert len(scoped()) == 2 and set(scoped()) & first == first - {oldest}  # the oldest one went
            assert await bump(c) == 2  # the newest session is intact

    run(go())


def test_restart_server_drops_every_session(http_upstream, cfg):
    cfg(demo={"url": http_upstream, "mode": "legacy", "expose": ["bump"]})

    async def go():
        await router._sync_forwarded()
        async with Client(router.mcp, mode="legacy") as a, Client(router.mcp, mode="legacy") as b:
            await bump(a)
            await bump(b)
            assert router.pool.connected("demo") and len(scoped()) == 2
            await a.call_tool("restart_server", {"server": "demo"})
            assert not router.pool.connected("demo")
            assert await bump(a) == 1  # a fresh session, so the count starts over

    run(go())


# ---- questions the upstream asks (elicitation) -----------------------------------------------
def test_upstream_questions_reach_the_client(http_upstream, cfg):
    cfg(demo={"url": http_upstream, "mode": "legacy", "expose": ["ask"]})
    asked = []

    async def yes(message, response_type, params, context):
        asked.append(message)
        return ElicitResult(action="accept", content={"value": True})

    async def no(message, response_type, params, context):
        return ElicitResult(action="decline")

    async def go():
        await router._sync_forwarded()
        async with Client(router.mcp, mode="legacy", elicitation_handler=yes) as c:
            assert (await c.call_tool("ask", {})).content[0].text == "accept:True"
        assert asked == ["Proceed?"]
        async with Client(router.mcp, mode="legacy", elicitation_handler=no) as c:
            assert (await c.call_tool("ask", {})).content[0].text.startswith("decline")

    run(go())


def test_client_that_cannot_answer_gets_a_clean_error(http_upstream, cfg):
    cfg(demo={"url": http_upstream, "mode": "legacy", "expose": ["ask"]})

    async def go():
        await router._sync_forwarded()
        async with Client(router.mcp, mode="legacy") as c:  # no elicitation handler
            res = await asyncio.wait_for(c.call_tool("ask", {}, raise_on_error=False), timeout=20)
            assert res.is_error and "elicit" in res.content[0].text.lower()

    run(go())


# ---- timeouts ----------------------------------------------------------------------------------
def test_a_calls_own_timeout_is_honoured(cfg, monkeypatch):
    cfg(demo=demo_cfg(expose=["nap"]))

    async def go():
        await router._sync_forwarded()  # connect first: starting the process takes longer than 1s
        monkeypatch.setattr(router, "CALL_TIMEOUT", 1.0)
        monkeypatch.setattr(router, "TIMEOUT_GRACE", 1.0)
        async with Client(router.mcp) as c:
            ok = await c.call_tool("nap", {"seconds": 2.0, "timeout_seconds": 5})
            assert ok.content[0].text == "slept 2.0"  # asked for longer than the 1s default, got it
            slow = await c.call_tool("nap", {"seconds": 3.0}, raise_on_error=False)
            assert slow.is_error and "no answer within 1s" in slow.content[0].text

    run(go())


def test_slow_calls_send_progress(cfg, monkeypatch):
    cfg(demo=demo_cfg(expose=["nap"]))
    seen = []

    async def on_progress(progress, total, message):
        seen.append(progress)

    async def go():
        await router._sync_forwarded()
        monkeypatch.setattr(router, "KEEPALIVE", 0.2)
        async with Client(router.mcp) as c:
            res = await c.call_tool("nap", {"seconds": 1.3, "timeout_seconds": 5}, progress_handler=on_progress)
            assert res.content[0].text == "slept 1.3"
        assert len(seen) >= 3 and seen == sorted(seen)

    run(go())
