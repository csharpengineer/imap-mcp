"""Spawn the real server over stdio and talk MCP to it."""

import json
import os
import subprocess
import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from fake_imap import PASSWORD, USER

MAIL_TOOLS = ["get_message", "get_thread", "list_folders", "search_mail"]


def _base_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("IMAP_") and not k.endswith("_IMAP_PASS")}


def _legacy_params(port: int) -> StdioServerParameters:
    env = _base_env()
    env.update(
        IMAP_HOST="127.0.0.1",
        IMAP_PORT=str(port),
        IMAP_USER=USER,
        IMAP_PASS=PASSWORD,
        IMAP_SSL="false",
        IMAP_TIMEOUT="5",
    )
    return StdioServerParameters(command=sys.executable, args=["-m", "imap_mcp"], env=env)


def _multi_params(tmp_path, work_port: int, gmail_port: int, **extra_env) -> StdioServerParameters:
    def account(port, password_env):
        return {"host": "127.0.0.1", "port": port, "user": USER, "ssl": False, "timeout": 5,
                "password_env": password_env}

    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"accounts": {
        "work": account(work_port, "WORK_IMAP_PASS"),
        "gmail": account(gmail_port, "GMAIL_IMAP_PASS"),
    }}), encoding="utf-8")
    env = _base_env()
    env.update(IMAP_MCP_CONFIG=str(path), **extra_env)
    return StdioServerParameters(command=sys.executable, args=["-m", "imap_mcp"], env=env)


def _payload(result) -> dict:
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def _run(params, scenario):
    async def main():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await scenario(session)

    return anyio.run(main)


def test_stdio_single_account_legacy_env(fake_server):
    async def scenario(session):
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert sorted(tools) == sorted(["list_accounts", *MAIL_TOOLS])
        assert all(t.annotations.read_only_hint for t in tools.values())
        # Single account: `account` is optional and defaults to "default".
        schema = tools["search_mail"].input_schema
        assert "account" not in schema.get("required", [])
        assert schema["properties"]["account"]["enum"] == ["default"]

        found = _payload(await session.call_tool("search_mail", {"from_address": "alice"}))
        assert found["account"] == "default"
        assert [m["uid"] for m in found["messages"]] == [1]
        msg = _payload(await session.call_tool("get_message", {"folder": "INBOX", "uid": 1}))
        assert msg["body"] == "Numbers are up.\nSee you Monday."
        bad = await session.call_tool("search_mail", {"since": "yesterday"})
        assert bad.is_error and "YYYY-MM-DD" in bad.content[0].text
        return sorted(tools)

    print("tools over stdio:", _run(_legacy_params(fake_server.port), scenario))
    assert fake_server.logins == 1  # one session reused across calls


def test_stdio_two_accounts(tmp_path, fake_server, gmail_server):
    params = _multi_params(tmp_path, fake_server.port, gmail_server.port,
                           WORK_IMAP_PASS=PASSWORD, GMAIL_IMAP_PASS=PASSWORD)

    async def scenario(session):
        tools = {t.name: t for t in (await session.list_tools()).tools}
        for name in MAIL_TOOLS:
            schema = tools[name].input_schema
            assert "account" in schema["required"], name
            assert schema["properties"]["account"]["enum"] == ["gmail", "work"]

        accounts = await session.call_tool("list_accounts", {})
        assert PASSWORD not in accounts.content[0].text
        assert _payload(accounts)["accounts"] == [
            {"name": "gmail", "host": "127.0.0.1", "port": gmail_server.port, "user": USER, "ssl": False,
             "password_env": "GMAIL_IMAP_PASS", "password_set": True},
            {"name": "work", "host": "127.0.0.1", "port": fake_server.port, "user": USER, "ssl": False,
             "password_env": "WORK_IMAP_PASS", "password_set": True},
        ]

        gmail = _payload(await session.call_tool("list_folders", {"account": "gmail"}))
        work = _payload(await session.call_tool("list_folders", {"account": "work"}))
        assert "[Gmail]/All Mail" in [f["name"] for f in gmail["folders"]]
        assert [f["name"] for f in work["folders"]] == ["INBOX", "Sent"]

        thread = _payload(await session.call_tool("get_thread", {"account": "gmail", "folder": "INBOX", "uid": 2}))
        assert thread["method"] == "gmail-thrid"

        unknown = await session.call_tool("list_folders", {"account": "personal"})
        assert unknown.is_error
        assert "unknown account 'personal'; valid accounts: gmail, work" in unknown.content[0].text

        missing = await session.call_tool("list_folders", {})
        assert missing.is_error

    _run(params, scenario)
    assert fake_server.logins == 1 and gmail_server.logins == 1


def test_stdio_missing_password_for_one_account(tmp_path, fake_server, gmail_server):
    params = _multi_params(tmp_path, fake_server.port, gmail_server.port, GMAIL_IMAP_PASS=PASSWORD)

    async def scenario(session):
        accounts = _payload(await session.call_tool("list_accounts", {}))["accounts"]
        assert {a["name"]: a["password_set"] for a in accounts} == {"gmail": True, "work": False}
        res = await session.call_tool("list_folders", {"account": "work"})
        assert res.is_error and "password env var WORK_IMAP_PASS is not set" in res.content[0].text
        ok = _payload(await session.call_tool("list_folders", {"account": "gmail"}))
        assert ok["account"] == "gmail"

    _run(params, scenario)
    assert fake_server.commands == []


def test_stdio_invalid_config_fails_fast_without_echoing_secrets(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"accounts": {"gmail": {
        "host": "imap.gmail.com", "user": "u", "password_env": "G", "password": "hunter2-secret"}}}),
        encoding="utf-8")
    env = _base_env()
    env["IMAP_MCP_CONFIG"] = str(path)
    proc = subprocess.run([sys.executable, "-m", "imap_mcp"], env=env, input="", capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 2
    assert "configuration error" in proc.stderr and "remove password" in proc.stderr
    assert "hunter2" not in proc.stderr + proc.stdout
