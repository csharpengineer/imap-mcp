"""Spawn the real server over stdio and talk MCP to it."""

import json
import os
import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from fake_imap import PASSWORD, USER


def _params(port: int) -> StdioServerParameters:
    env = {k: v for k, v in os.environ.items() if not k.startswith("IMAP_")}
    env.update(
        IMAP_HOST="127.0.0.1",
        IMAP_PORT=str(port),
        IMAP_USER=USER,
        IMAP_PASS=PASSWORD,
        IMAP_SSL="false",
        IMAP_TIMEOUT="5",
    )
    return StdioServerParameters(command=sys.executable, args=["-m", "imap_mcp"], env=env)


def _payload(result) -> dict:
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def test_stdio_lists_tools_and_answers(fake_server):
    async def scenario():
        async with stdio_client(_params(fake_server.port)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                by_name = {t.name: t for t in tools}
                assert sorted(by_name) == ["get_message", "get_thread", "list_folders", "search_mail"]
                assert all(t.annotations.read_only_hint for t in tools)

                found = _payload(await session.call_tool("search_mail", {"from_address": "alice"}))
                assert [m["uid"] for m in found["messages"]] == [1]

                msg = _payload(await session.call_tool("get_message", {"folder": "INBOX", "uid": 1}))
                assert msg["body"] == "Numbers are up.\nSee you Monday."

                bad = await session.call_tool("search_mail", {"since": "yesterday"})
                assert bad.is_error and "YYYY-MM-DD" in bad.content[0].text
                return sorted(by_name)

    print("tools over stdio:", anyio.run(scenario))
    assert fake_server.command_names.count("LOGIN") == 3


def test_stdio_missing_config_is_a_tool_error(fake_server):
    params = _params(fake_server.port)
    params.env.pop("IMAP_PASS")

    async def scenario():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool("list_folders", {})
                assert res.is_error
                assert "IMAP_PASS" in res.content[0].text

    anyio.run(scenario)
