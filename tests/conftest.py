from __future__ import annotations

import base64

import pytest

from fake_imap import PASSWORD, USER, FakeFolder, FakeIMAPServer, FakeMessage
from imap_mcp.config import AccountConfig


def crlf(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("latin-1")


def raw(headers: str, body: bytes | str = b"") -> bytes:
    if isinstance(body, str):
        body = crlf(body)
    return crlf(headers.strip("\n") + "\n\n") + body


PLAIN = raw(
    """
From: Alice Example <alice@example.com>
To: tester@example.com
Subject: Quarterly report
Date: Tue, 01 Sep 2026 09:00:00 +0000
Message-ID: <q1@example.com>
Content-Type: text/plain; charset=us-ascii
""",
    "Numbers are up.\nSee you Monday.\n",
)

ALTERNATIVE = raw(
    """
From: Bob <bob@example.com>
To: tester@example.com
Subject: Lunch?
Date: Thu, 10 Sep 2026 12:00:00 +0000
Message-ID: <lunch@example.com>
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="ALT"
""",
    """--ALT
Content-Type: text/plain; charset=utf-8

Plain version: tacos at noon?
--ALT
Content-Type: text/html; charset=utf-8

<p>HTML version: <b>tacos</b> at noon?</p>
--ALT--
""",
)

HTML_ONLY = raw(
    """
From: =?utf-8?B?Q2Fyb2wgw4lsaXNl?= <carol@example.com>
To: tester@example.com
Subject: =?utf-8?B?Q2Fmw6kgbWVudSDimJU=?=
Date: Tue, 15 Sep 2026 08:30:00 +0000
Message-ID: <menu@example.com>
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8
Content-Transfer-Encoding: base64
""",
    base64.encodebytes(
        "<html><head><style>p{color:red}</style><script>alert(1)</script></head>"
        "<body><p>Today&nbsp;only:</p><ul><li>Crème brûlée</li><li>Espresso</li></ul></body></html>".encode()
    ).replace(b"\n", b"\r\n"),
)

LATIN1 = raw(
    """
From: =?iso-8859-1?Q?J=FCrgen_M=FCller?= <juergen@example.de>
To: tester@example.com
Subject: =?iso-8859-1?Q?Gr=FC=DFe_aus_K=F6ln?=
Date: Sun, 20 Sep 2026 18:00:00 +0200
Message-ID: <koeln@example.de>
MIME-Version: 1.0
Content-Type: text/plain; charset=iso-8859-1
Content-Transfer-Encoding: quoted-printable
""",
    "Sch=F6ne Gr=FC=DFe und bis bald.\n",
)

CP1252_UNKNOWN_LABEL = raw(
    """
From: Dana <dana@example.com>
To: tester@example.com
Subject: Smart quotes
Date: Mon, 21 Sep 2026 10:00:00 +0000
Message-ID: <quotes@example.com>
MIME-Version: 1.0
Content-Type: text/plain; charset=x-no-such-charset
Content-Transfer-Encoding: 8bit
""",
    b"\x93Quoted\x94 \x96 dash\r\n",
)

REPLY_WITH_ATTACHMENT = raw(
    """
From: Tester <tester@example.com>
To: Alice Example <alice@example.com>
Subject: Re: Quarterly report
Date: Wed, 02 Sep 2026 09:00:00 +0000
Message-ID: <q2@example.com>
In-Reply-To: <q1@example.com>
References: <q1@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="MIX"
""",
    """--MIX
Content-Type: text/plain; charset=utf-8

Thanks, attached the slides.
--MIX
Content-Type: application/pdf; name="slides.pdf"
Content-Disposition: attachment; filename="=?utf-8?Q?Pr=C3=A4sentation.pdf?="
Content-Transfer-Encoding: base64

"""
    + base64.encodebytes(b"%PDF-1.4 " + b"x" * 3000).decode()
    + "--MIX--\n",
)

LONG = raw(
    """
From: Newsletter <news@example.com>
To: tester@example.com
Subject: Weekly digest
Date: Fri, 25 Sep 2026 07:00:00 +0000
Message-ID: <digest@example.com>
Content-Type: text/plain; charset=utf-8
""",
    ("lorem ipsum dolor sit amet " * 2000) + "\nTHE END\n",
)

UNRELATED_SAME_SUBJECT = raw(
    """
From: Eve <eve@example.com>
To: tester@example.com
Subject: Fwd: Lunch?
Date: Sat, 12 Sep 2026 12:00:00 +0000
Message-ID: <fwd-lunch@example.com>
Content-Type: text/plain; charset=utf-8
""",
    "Forwarding Bob's lunch invite.\n",
)


def inbox_messages() -> list[FakeMessage]:
    return [
        FakeMessage(1, PLAIN, {"\\Seen"}, thrid=100),
        FakeMessage(2, ALTERNATIVE, set(), thrid=200),
        FakeMessage(3, HTML_ONLY, {"\\Seen"}, thrid=300),
        FakeMessage(4, LATIN1, set(), thrid=400),
        FakeMessage(5, CP1252_UNKNOWN_LABEL, {"\\Seen"}, thrid=500),
        FakeMessage(6, REPLY_WITH_ATTACHMENT, {"\\Seen"}, thrid=100),
        FakeMessage(7, LONG, set(), thrid=700),
        FakeMessage(8, UNRELATED_SAME_SUBJECT, set(), thrid=800),
    ]


@pytest.fixture
def fake_server():
    with FakeIMAPServer([FakeFolder("INBOX", messages=inbox_messages()), FakeFolder("Sent")]) as srv:
        yield srv


@pytest.fixture
def gmail_server():
    # Gmail-like: thread member only in Sent, findable via All Mail + X-GM-THRID.
    sent_reply = FakeMessage(
        3,
        raw(
            """
From: Tester <tester@example.com>
To: Bob <bob@example.com>
Subject: Re: Lunch?
Date: Thu, 10 Sep 2026 12:30:00 +0000
Message-ID: <lunch-reply@example.com>
Content-Type: text/plain; charset=utf-8
""",
            "Yes!\n",
        ),
        {"\\Seen"},
        thrid=200,
    )
    inbox = inbox_messages()
    all_mail = [FakeMessage(m.uid, m.raw, set(m.flags), m.thrid) for m in inbox] + [
        FakeMessage(9, sent_reply.raw, {"\\Seen"}, thrid=200)
    ]
    folders = [
        FakeFolder("INBOX", messages=inbox),
        FakeFolder("[Gmail]", flags=["\\HasChildren", "\\Noselect"]),
        FakeFolder("[Gmail]/All Mail", flags=["\\HasNoChildren", "\\All"], messages=all_mail),
        FakeFolder("[Gmail]/Sent Mail", flags=["\\HasNoChildren", "\\Sent"], messages=[sent_reply]),
    ]
    with FakeIMAPServer(folders, ["IMAP4rev1", "X-GM-EXT-1", "AUTH=PLAIN"]) as srv:
        yield srv


def config_for(server: FakeIMAPServer, name: str = "test", password_env: str = "TEST_IMAP_PASS") -> AccountConfig:
    return AccountConfig(
        name=name, host="127.0.0.1", port=server.port, user=USER, password_env=password_env, ssl=False, timeout=5
    )


@pytest.fixture
def config(fake_server) -> AccountConfig:
    return config_for(fake_server)


@pytest.fixture
def gmail_config(gmail_server) -> AccountConfig:
    return config_for(gmail_server, name="gmail", password_env="GMAIL_IMAP_PASS")
