"""Multi-account routing, isolation, lazy connect and reconnect."""

import socket

import pytest

from conftest import config_for
from fake_imap import PASSWORD
from imap_mcp.accounts import AccountPool
from imap_mcp.client import MailError
from imap_mcp.config import AccountConfig

ENV = {"WORK_IMAP_PASS": PASSWORD, "GMAIL_IMAP_PASS": PASSWORD}


@pytest.fixture
def pool(fake_server, gmail_server):
    accounts = {
        "work": config_for(fake_server, name="work", password_env="WORK_IMAP_PASS"),
        "gmail": config_for(gmail_server, name="gmail", password_env="GMAIL_IMAP_PASS"),
    }
    p = AccountPool(accounts, env=ENV)
    yield p
    p.close_all()


def folders(box):
    return [f["name"] for f in box.list_folders()]


def test_two_accounts_are_routed_to_their_own_servers(pool, fake_server, gmail_server):
    assert pool.run("work", folders) == ["INBOX", "Sent"]
    assert gmail_server.commands == []  # lazy: gmail not contacted yet
    assert "[Gmail]/All Mail" in pool.run("gmail", folders)
    assert pool.run("gmail", lambda b: b.get_thread("INBOX", 2, 20))["method"] == "gmail-thrid"
    assert pool.run("work", lambda b: b.get_thread("INBOX", 2, 20))["method"] == "subject"
    assert "X-GM-THRID" not in " ".join(fake_server.commands)


def test_connection_is_reused_per_account(pool, fake_server):
    for _ in range(3):
        pool.run("work", folders)
    assert fake_server.logins == 1


def test_unknown_account_lists_valid_names(pool):
    with pytest.raises(MailError, match=r"unknown account 'personal'; valid accounts: gmail, work"):
        pool.run("personal", folders)


def test_account_required_when_several(pool):
    with pytest.raises(MailError, match="account is required; choose one of: gmail, work"):
        pool.run(None, folders)


def test_single_account_may_be_omitted(fake_server):
    pool = AccountPool({"only": config_for(fake_server, name="only")}, env={"TEST_IMAP_PASS": PASSWORD})
    assert pool.run(None, folders) == ["INBOX", "Sent"]
    pool.close_all()


def test_missing_password_breaks_only_that_account(fake_server, gmail_server):
    pool = AccountPool(
        {
            "work": config_for(fake_server, name="work", password_env="WORK_IMAP_PASS"),
            "gmail": config_for(gmail_server, name="gmail", password_env="GMAIL_IMAP_PASS"),
        },
        env={"GMAIL_IMAP_PASS": PASSWORD},
    )
    with pytest.raises(MailError, match="account 'work': password env var WORK_IMAP_PASS is not set"):
        pool.run("work", folders)
    assert fake_server.commands == []  # never even connected
    assert "INBOX" in pool.run("gmail", folders)
    pool.close_all()


def test_unreachable_account_does_not_affect_others(fake_server):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]  # closed again before use: connection refused
    dead = AccountConfig("dead", "127.0.0.1", "u", "DEAD_PASS", port=dead_port, ssl=False, timeout=2)
    pool = AccountPool(
        {"dead": dead, "work": config_for(fake_server, name="work", password_env="WORK_IMAP_PASS")},
        env={**ENV, "DEAD_PASS": "x"},
    )
    with pytest.raises(MailError, match="account 'dead': connection/login to 127.0.0.1"):
        pool.run("dead", folders)
    assert pool.run("work", folders) == ["INBOX", "Sent"]
    with pytest.raises(MailError, match="account 'dead'"):
        pool.run("dead", folders)  # retried lazily, still isolated
    pool.close_all()


def test_reconnects_after_server_drops_connection(pool, fake_server):
    assert pool.run("work", folders) == ["INBOX", "Sent"]
    fake_server.drop_connections()
    res = pool.run("work", lambda b: b.search("INBOX", limit=2))
    assert [m["uid"] for m in res["messages"]] == [8, 7]
    assert fake_server.logins == 2


def test_bad_password_is_not_cached_as_a_connection(fake_server):
    pool = AccountPool({"work": config_for(fake_server, name="work", password_env="P")}, env={"P": "wrong"})
    for _ in range(2):
        with pytest.raises(MailError, match="AUTHENTICATIONFAILED"):
            pool.run("work", folders)
    pool._env = {"P": PASSWORD}  # credential fixed (e.g. new session env)
    assert pool.run("work", folders) == ["INBOX", "Sent"]
    pool.close_all()


def test_describe_leaks_no_secrets(pool):
    described = pool.describe()
    assert [a["name"] for a in described] == ["gmail", "work"]
    assert set(described[0]) == {"name", "host", "port", "user", "ssl", "password_env", "password_set"}
    assert all(a["password_set"] is True for a in described)
    assert PASSWORD not in repr(described)


def test_every_account_is_read_only_on_the_wire(pool, fake_server, gmail_server):
    from test_tools import assert_wire_read_only, flags_snapshot

    before = (flags_snapshot(fake_server), flags_snapshot(gmail_server))
    for name in ("work", "gmail"):
        pool.run(name, folders)
        pool.run(name, lambda b: b.search("INBOX", limit=50, unseen_only=True))
        for uid in range(1, 9):
            pool.run(name, lambda b, u=uid: b.get_message("INBOX", u, 4000))
            pool.run(name, lambda b, u=uid: b.get_thread("INBOX", u, 20))
    assert_wire_read_only(fake_server)
    assert_wire_read_only(gmail_server)
    assert (flags_snapshot(fake_server), flags_snapshot(gmail_server)) == before
