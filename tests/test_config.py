import json

import pytest

from imap_mcp.config import AccountConfig, ConfigError, load_accounts

LEGACY = {"IMAP_HOST": "imap.example.com", "IMAP_USER": "u@example.com", "IMAP_PASS": "s3cret-legacy"}


def write_config(tmp_path, accounts) -> str:
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"accounts": accounts}), encoding="utf-8")
    return str(path)


GOOD = {
    "gmail": {"host": "imap.gmail.com", "user": "me@gmail.com", "password_env": "GMAIL_IMAP_PASS"},
    "work": {
        "host": "mail.example.com",
        "port": 1993,
        "user": "me",
        "ssl": True,
        "timeout": 10,
        "password_env": "WORK_IMAP_PASS",
    },
}


def test_legacy_env_is_default_account():
    accounts = load_accounts(LEGACY)
    assert list(accounts) == ["default"]
    a = accounts["default"]
    assert (a.host, a.port, a.ssl, a.timeout, a.password_env) == ("imap.example.com", 993, True, 30.0, "IMAP_PASS")
    assert a.password(LEGACY) == "s3cret-legacy"
    assert load_accounts({**LEGACY, "IMAP_SSL": "false"})["default"].port == 143


def test_config_file_accounts(tmp_path):
    accounts = load_accounts({"IMAP_MCP_CONFIG": write_config(tmp_path, GOOD)})
    assert sorted(accounts) == ["gmail", "work"]
    assert accounts["gmail"] == AccountConfig("gmail", "imap.gmail.com", "me@gmail.com", "GMAIL_IMAP_PASS")
    assert (accounts["work"].port, accounts["work"].timeout) == (1993, 10.0)


def test_config_file_plus_legacy(tmp_path):
    accounts = load_accounts({**LEGACY, "IMAP_MCP_CONFIG": write_config(tmp_path, GOOD)})
    assert sorted(accounts) == ["default", "gmail", "work"]


def test_missing_password_env_is_not_a_config_error(tmp_path):
    # Startup must succeed; the account just reports password_set false.
    accounts = load_accounts({"IMAP_MCP_CONFIG": write_config(tmp_path, GOOD), "GMAIL_IMAP_PASS": "x"})
    assert accounts["work"].describe({"GMAIL_IMAP_PASS": "x"})["password_set"] is False
    assert accounts["gmail"].describe({"GMAIL_IMAP_PASS": "x"})["password_set"] is True


@pytest.mark.parametrize(
    "accounts,message",
    [
        ({"gmail": {**GOOD["gmail"], "password": "hunter2-in-file"}}, "remove password"),
        ({"gmail": {**GOOD["gmail"], "app_password": "hunter2-in-file"}}, "remove app_password"),
        ({"gmail": {**GOOD["gmail"], "hostname": "x"}}, "unknown keys hostname"),
        ({"gmail": {"host": "h", "user": "u"}}, "password_env is required"),
        ({"gmail": {**GOOD["gmail"], "password_env": "hunter2 not a name"}}, "password_env must be an env var name"),
        ({"gmail": {**GOOD["gmail"], "port": 70000}}, "between 1 and 65535"),
        ({"gmail": {**GOOD["gmail"], "port": "abc"}}, "must be an integer"),
        ({"gmail": {**GOOD["gmail"], "ssl": "yes"}}, "ssl must be true or false"),
        ({"gmail": {**GOOD["gmail"], "timeout": 0}}, "must be positive"),
        ({"bad name!": GOOD["gmail"]}, "names may use"),
        ({}, "must be an object like"),
    ],
)
def test_config_file_validation(tmp_path, accounts, message):
    with pytest.raises(ConfigError, match=message) as err:
        load_accounts({"IMAP_MCP_CONFIG": write_config(tmp_path, accounts)})
    assert "hunter2" not in str(err.value)


def test_invalid_json_does_not_echo_contents(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text('{"accounts": {"g": {"host": "h", "user": "u", "password_env": hunter2-secret}}}', encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON") as err:
        load_accounts({"IMAP_MCP_CONFIG": str(path)})
    assert "hunter2" not in str(err.value)


def test_no_accounts_and_partial_legacy(tmp_path):
    with pytest.raises(ConfigError, match="no IMAP accounts configured"):
        load_accounts({})
    with pytest.raises(ConfigError, match="missing IMAP_USER"):
        load_accounts({"IMAP_HOST": "h", "IMAP_PASS": "p"})
    with pytest.raises(ConfigError, match="file not found"):
        load_accounts({"IMAP_MCP_CONFIG": str(tmp_path / "nope.json")})
    with pytest.raises(ConfigError, match="IMAP_SSL"):
        load_accounts({**LEGACY, "IMAP_SSL": "maybe"})
    with pytest.raises(ConfigError, match="defined both"):
        load_accounts({**LEGACY, "IMAP_MCP_CONFIG": write_config(tmp_path, {"default": GOOD["gmail"]})})


def test_example_config_in_repo_is_valid():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "accounts.example.json"
    accounts = load_accounts({"IMAP_MCP_CONFIG": str(example)})
    assert sorted(accounts) == ["gmail", "work"]
