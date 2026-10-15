"""End-to-end client behaviour against the fake IMAP server."""

import re

import pytest

from imap_mcp import client
from imap_mcp.client import Mailbox, MailError, build_search
from fake_imap import PASSWORD

READ_ONLY_WIRE = {"CAPABILITY", "LOGIN", "LOGOUT", "LIST", "EXAMINE", "UID SEARCH", "UID FETCH"}


def assert_wire_read_only(server):
    names = set(server.command_names)
    assert names <= READ_ONLY_WIRE, names - READ_ONLY_WIRE
    for line in server.commands:
        if " FETCH " in line.upper():
            assert not re.search(r"(?<![.\w])BODY\[|\bRFC822\b(?!\.)", line, re.I), line


def flags_snapshot(server):
    return {
        (folder.name, m.uid): frozenset(m.flags)
        for folder in server.folders.values()
        for m in folder.messages
    }


def test_list_folders(config, fake_server):
    with Mailbox(config, PASSWORD) as box:
        assert box.list_folders() == [{"name": "INBOX"}, {"name": "Sent"}]


def test_search_defaults_newest_first_with_snippets(config):
    with Mailbox(config, PASSWORD) as box:
        res = box.search("INBOX", limit=3)
    assert res["total"] == 8
    assert [m["uid"] for m in res["messages"]] == [8, 7, 6]
    assert res["messages"][0] == {
        "uid": 8,
        "date": "2026-09-12T12:00:00+00:00",
        "from": "Eve <eve@example.com>",
        "subject": "Fwd: Lunch?",
        "unread": True,
        "snippet": "Forwarding Bob's lunch invite.",
    }
    assert res["messages"][1]["snippet"].startswith("lorem ipsum")
    assert len(res["messages"][1]["snippet"]) <= client.SNIPPET_CHARS + 2


@pytest.mark.parametrize(
    "criteria,expected",
    [
        ({"from_": "alice"}, [1]),
        ({"to": "alice@example.com"}, [6]),
        ({"subject": "quarterly"}, [6, 1]),
        ({"text": "tacos"}, [2]),
        ({"unseen_only": True}, [8, 7, 4, 2]),
        ({"since": "2026-09-15"}, [7, 5, 4, 3]),
        ({"before": "2026-09-10"}, [6, 1]),
        ({"since": "2026-09-10", "before": "2026-09-16", "unseen_only": True}, [8, 2]),
        ({"subject": "Grüße"}, [4]),  # non-ASCII -> CHARSET UTF-8 literal
        ({"from_": "bob", "subject": "Lunch"}, [2]),
        ({"subject": "nothing matches this"}, []),
    ],
)
def test_search_filters(config, criteria, expected):
    with Mailbox(config, PASSWORD) as box:
        res = box.search("INBOX", limit=50, **criteria)
    assert [m["uid"] for m in res["messages"]] == expected
    assert res["total"] == len(expected)


def test_search_limit_is_capped(config):
    with Mailbox(config, PASSWORD) as box:
        assert box.search("INBOX", limit=0)["returned"] == 1
        assert box.search("INBOX", limit=10_000)["returned"] == 8


def test_search_rejects_bad_input():
    with pytest.raises(MailError, match="YYYY-MM-DD"):
        build_search(since="last tuesday")
    with pytest.raises(MailError, match="one search term"):
        build_search(subject="Grüße", from_="Jürgen")
    with pytest.raises(MailError, match="CR, LF"):
        build_search(subject="x\r\nA1 STORE 1 +FLAGS (\\Deleted)")
    assert build_search(subject='say "hi"')[0] == ["SUBJECT", '"say \\"hi\\""']


def test_get_message_multipart_alternative_prefers_plain(config):
    with Mailbox(config, PASSWORD) as box:
        msg = box.get_message("INBOX", 2, 4000)
    assert msg["body"] == "Plain version: tacos at noon?"
    assert msg["body_format"] == "text/plain"
    assert msg["attachments"] == []
    assert msg["unread"] is True


def test_get_message_html_only(config):
    with Mailbox(config, PASSWORD) as box:
        msg = box.get_message("INBOX", 3, 4000)
    assert msg["subject"] == "Café menu ☕"
    assert msg["from"] == "Carol Élise <carol@example.com>"
    assert msg["body_format"] == "text/html"
    assert "alert" not in msg["body"] and "color" not in msg["body"]
    assert msg["body"] == "Today only:\n\nCrème brûlée\n\nEspresso"


def test_get_message_latin1_quoted_printable(config):
    with Mailbox(config, PASSWORD) as box:
        msg = box.get_message("INBOX", 4, 4000)
    assert msg["from"] == "Jürgen Müller <juergen@example.de>"
    assert msg["subject"] == "Grüße aus Köln"
    assert msg["body"] == "Schöne Grüße und bis bald."
    assert msg["date"] == "2026-09-20T18:00:00+02:00"


def test_get_message_unknown_charset_label_falls_back(config):
    with Mailbox(config, PASSWORD) as box:
        msg = box.get_message("INBOX", 5, 4000)
    assert msg["body"] == "“Quoted” – dash"


def test_get_message_attachment_metadata_only(config):
    with Mailbox(config, PASSWORD) as box:
        msg = box.get_message("INBOX", 6, 4000)
    assert msg["body"] == "Thanks, attached the slides."
    assert msg["attachments"] == [
        {"filename": "Präsentation.pdf", "content_type": "application/pdf", "size": 3009}
    ]
    assert "PDF-1.4" not in str(msg)


def test_get_message_truncates(config):
    with Mailbox(config, PASSWORD) as box:
        short = box.get_message("INBOX", 7, 500)
        default = box.get_message("INBOX", 7, client.BODY_CHARS_DEFAULT)
        huge = box.get_message("INBOX", 7, 10**9)
    assert short["body_truncated"] is True
    assert len(short["body"]) <= 502 and short["body"].endswith("…")
    assert len(default["body"]) <= client.BODY_CHARS_DEFAULT + 2
    assert len(huge["body"]) <= client.BODY_CHARS_MAX + 2  # hard cap
    with Mailbox(config, PASSWORD) as box:
        full = box.get_message("INBOX", 1, 4000)
    assert full["body_truncated"] is False


def test_get_message_missing_uid_and_folder(config):
    with Mailbox(config, PASSWORD) as box:
        with pytest.raises(MailError, match="no message with uid 999"):
            box.get_message("INBOX", 999, 4000)
        with pytest.raises(MailError, match="opening folder 'Nope'"):
            box.get_message("Nope", 1, 4000)


def test_thread_by_references(config):
    with Mailbox(config, PASSWORD) as box:
        res = box.get_thread("INBOX", 6, 20)
    assert res["method"] == "references"
    assert [m["uid"] for m in res["messages"]] == [1, 6]


def test_thread_falls_back_to_subject(config):
    with Mailbox(config, PASSWORD) as box:
        res = box.get_thread("INBOX", 2, 20)
    assert res["method"] == "subject"
    assert [m["uid"] for m in res["messages"]] == [2, 8]


def test_thread_gmail_thrid_uses_all_mail(gmail_config, gmail_server):
    with Mailbox(gmail_config, PASSWORD) as box:
        res = box.get_thread("INBOX", 2, 20)
    assert res["method"] == "gmail-thrid"
    assert res["folder"] == "[Gmail]/All Mail"
    assert [m["uid"] for m in res["messages"]] == [2, 9]
    assert "UID SEARCH" in gmail_server.command_names
    assert any("X-GM-THRID 200" in c for c in gmail_server.commands)


def test_gmail_folder_flags(gmail_config):
    with Mailbox(gmail_config, PASSWORD) as box:
        folders = box.list_folders()
    assert {"name": "[Gmail]/All Mail", "flags": ["\\All"]} in folders
    assert {"name": "[Gmail]", "flags": ["\\Noselect"]} in folders


def test_bad_password_is_reported_without_leaking_it(fake_server, config):
    box = Mailbox(config, "hunter2-secret")
    with pytest.raises(MailError) as err:
        box.connect()
    assert "AUTHENTICATIONFAILED" in str(err.value) and "'test'" in str(err.value)
    assert "hunter2" not in str(err.value)
    assert "hunter2" not in repr(vars(box))  # password not retained after connect


def test_every_tool_is_read_only_on_the_wire(config, fake_server, gmail_config, gmail_server):
    """Run every operation, then prove no mutating command was sent and no flags changed."""
    for server, cfg in ((fake_server, config), (gmail_server, gmail_config)):
        before = flags_snapshot(server)
        with Mailbox(cfg, PASSWORD) as box:
            box.list_folders()
            box.search("INBOX", limit=50)
            box.search("INBOX", limit=50, subject="Grüße", unseen_only=True)
            for uid in range(1, 9):
                box.get_message("INBOX", uid, 4000)
                box.get_thread("INBOX", uid, 20)
        assert_wire_read_only(server)
        assert flags_snapshot(server) == before
        assert "SELECT" not in server.command_names
        assert all("PEEK" in c.upper() or "X-GM-THRID" in c for c in server.commands if "UID FETCH" in c.upper())
