import imaplib

import pytest

from fake_imap import PASSWORD, USER
from imap_mcp.guard import ReadOnlyIMAP4, ReadOnlyViolation, check_command


@pytest.mark.parametrize(
    "name,args",
    [
        ("SELECT", ('"INBOX"',)),
        ("STORE", ("1", "+FLAGS", "(\\Seen)")),
        ("COPY", ("1", "Trash")),
        ("MOVE", ("1", "Trash")),
        ("EXPUNGE", ()),
        ("CLOSE", ()),
        ("APPEND", ("INBOX", None, None)),
        ("CREATE", ("x",)),
        ("DELETE", ("x",)),
        ("RENAME", ("x", "y")),
        ("SUBSCRIBE", ("x",)),
        ("UNSUBSCRIBE", ("x",)),
        ("SETACL", ("x", "u", "r")),
        ("ENABLE", ("UTF8=ACCEPT",)),
        ("IDLE", ()),
        ("UID", ("STORE", "1", "+FLAGS", "(\\Deleted)")),
        ("UID", ("COPY", "1", "Trash")),
        ("UID", ("MOVE", "1", "Trash")),
        ("UID", ("EXPUNGE", "1")),
        ("UID", (b"STORE", b"1", b"+FLAGS", b"(\\Seen)")),
        ("UID", ()),
        ("UID", ("FETCH", "1", "(BODY[])")),
        ("UID", ("FETCH", "1", "(UID BODY[TEXT])")),
        ("UID", ("FETCH", "1", "(BODY[1.2]<0.100>)")),
        ("UID", ("FETCH", "1", "(BINARY[1])")),
        ("UID", ("FETCH", "1", "(RFC822)")),
        ("UID", ("FETCH", "1", "RFC822.TEXT")),
        ("FETCH", ("1", "(BODY[HEADER])")),
        ("FETCH", ("1", "body[]")),
    ],
)
def test_state_changing_commands_are_blocked(name, args):
    with pytest.raises(ReadOnlyViolation):
        check_command(name, args)


@pytest.mark.parametrize(
    "name,args",
    [
        ("CAPABILITY", ()),
        ("LOGIN", ("u", '"p"')),
        ("LIST", ('""', "*")),
        ("EXAMINE", ('"INBOX"',)),
        ("LOGOUT", ()),
        ("UID", ("SEARCH", "ALL")),
        ("UID", (b"SEARCH", "CHARSET", "UTF-8", "FROM")),
        ("UID", ("FETCH", "1:3", "(UID FLAGS BODY.PEEK[HEADER.FIELDS (DATE FROM)])")),
        ("UID", ("FETCH", "1", "(BODY.PEEK[]<0.100> RFC822.SIZE RFC822.HEADER X-GM-THRID)")),
        ("FETCH", ("1", "(BODY.PEEK[TEXT]<0.10>)")),
    ],
)
def test_read_only_commands_are_allowed(name, args):
    check_command(name, args)


def test_blocked_before_reaching_the_wire(fake_server):
    """Even direct imaplib calls on the guarded class never hit the server."""
    conn = ReadOnlyIMAP4("127.0.0.1", fake_server.port, timeout=5)
    conn.login(USER, PASSWORD)
    with pytest.raises(ReadOnlyViolation):
        conn.select("INBOX")  # readonly=False -> SELECT
    conn.select('"INBOX"', readonly=True)
    attempts = [
        lambda: conn.store("1", "+FLAGS", "(\\Seen)"),
        lambda: conn.uid("STORE", "1", "+FLAGS", "(\\Deleted)"),
        lambda: conn.uid("COPY", "1", "Sent"),
        lambda: conn.uid("FETCH", "1", "(BODY[])"),
        lambda: conn.fetch("1", "(RFC822)"),
        lambda: conn.copy("1", "Sent"),
        lambda: conn.expunge(),
        lambda: conn.close(),
        lambda: conn.append("INBOX", None, None, b"Subject: x\r\n\r\nx"),
        lambda: conn.create("New"),
        lambda: conn.delete("Sent"),
        lambda: conn.rename("Sent", "Old"),
    ]
    for attempt in attempts:
        with pytest.raises(ReadOnlyViolation):
            attempt()
    conn.logout()
    # 3.14+ imaplib re-sends CAPABILITY after LOGIN; ignore it.
    sent = [c for c in fake_server.command_names if c != "CAPABILITY"]
    assert sent == ["LOGIN", "EXAMINE", "LOGOUT"]
    inbox = fake_server.folders["INBOX"].messages
    assert [sorted(m.flags) for m in inbox][:2] == [["\\Seen"], []]


def test_violation_is_an_imaplib_error():
    assert issubclass(ReadOnlyViolation, imaplib.IMAP4.error)
