import base64

import pytest

from imap_mcp import imap_utf7, mime
from imap_mcp.client import normalize_subject, parse_list
from imap_mcp.config import Config, ConfigError


@pytest.mark.parametrize(
    "plain,encoded",
    [("INBOX", "INBOX"), ("Entwürfe", "Entw&APw-rfe"), ("A&B", "A&-B"), ("日本語", "&ZeVnLIqe-")],
)
def test_modified_utf7_roundtrip(plain, encoded):
    assert imap_utf7.encode(plain) == encoded
    assert imap_utf7.decode(encoded) == plain


def test_parse_list_quoted_literal_and_utf7():
    data = [
        b'(\HasNoChildren) "/" "INBOX"',
        b'(\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"',
        b'(\HasNoChildren) "/" Entw&APw-rfe',
        (b'(\HasNoChildren \Trash) "/" {9}', b'Odd"Name'),
    ]
    assert parse_list(data) == [
        {"name": "INBOX"},
        {"name": "[Gmail]/Sent Mail", "flags": ["\Sent"]},
        {"name": "Entwürfe"},
        {"name": 'Odd"Name', "flags": ["\Trash"]},
    ]


def test_normalize_subject():
    assert normalize_subject("Re: FW: re[2]: Lunch?") == "Lunch?"
    assert normalize_subject("Lunch: today") == "Lunch: today"


def test_partial_fetch_with_cut_base64_and_utf8_still_parses():
    body = "Grüße ".encode() * 50
    raw = (
        b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + base64.encodebytes(body)
    )
    cut = mime.parse_bytes(raw[: len(raw) - 37])
    text = mime.snippet(cut, 40)
    assert text.startswith("Grüße Grüße")


def test_header_decoding_never_raises():
    assert mime.decode_mime_header("=?bogus-charset?Q?abc?=") != ""
    assert mime.format_addresses("=?utf-8?Q?Zo=C3=AB?= <zoe@example.com>") == "Zoë <zoe@example.com>"
    assert mime.format_date("not a date") == "not a date"


def test_config_from_env_defaults_and_secret_hidden():
    cfg = Config.from_env({"IMAP_HOST": "imap.example.com", "IMAP_USER": "u", "IMAP_PASS": "s3cret"})
    assert (cfg.port, cfg.ssl, cfg.timeout) == (993, True, 30.0)
    assert "s3cret" not in repr(cfg)
    plain = Config.from_env({"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASS": "p", "IMAP_SSL": "false"})
    assert plain.port == 143
    with pytest.raises(ConfigError, match="IMAP_HOST"):
        Config.from_env({"IMAP_USER": "u", "IMAP_PASS": "p"})
    with pytest.raises(ConfigError, match="IMAP_SSL"):
        Config.from_env({"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASS": "p", "IMAP_SSL": "maybe"})
