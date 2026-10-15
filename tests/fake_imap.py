"""A small in-process IMAP server for tests.

It speaks enough IMAP4rev1 over a real TCP socket for imaplib, records every
command line it receives, and mutates state the way a real server would
(e.g. a non-PEEK BODY[] fetch sets \\Seen), so tests can prove the client
never asks for a change.
"""

from __future__ import annotations

import email
import email.policy
import re
import socket
import socketserver
import threading
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

USER = "tester@example.com"
PASSWORD = "not-a-real-password"


@dataclass
class FakeMessage:
    uid: int
    raw: bytes
    flags: set[str] = field(default_factory=set)
    thrid: int | None = None

    @property
    def parsed(self):
        return email.message_from_bytes(self.raw, policy=email.policy.default)


@dataclass
class FakeFolder:
    name: str
    flags: list[str] = field(default_factory=lambda: ["\\HasNoChildren"])
    messages: list[FakeMessage] = field(default_factory=list)


class FakeIMAPServer:
    def __init__(self, folders: list[FakeFolder], capabilities: list[str] | None = None):
        self.folders = {f.name: f for f in folders}
        self.capabilities = capabilities or ["IMAP4rev1", "AUTH=PLAIN"]
        self.commands: list[str] = []  # every command line, password redacted
        self.logins = 0
        self._sockets: list[socket.socket] = []
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def drop_connections(self) -> None:
        """Simulate the server (or network) killing every open session."""
        for sock in self._sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._sockets.clear()

    @property
    def command_names(self) -> list[str]:
        names = []
        for line in self.commands:
            parts = line.split()
            name = parts[1].upper() if len(parts) > 1 else ""
            if name == "UID" and len(parts) > 2:
                name = "UID " + parts[2].upper()
            names.append(name)
        return names

    def _handler(self):
        server = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                server._sockets.append(self.request)
                try:
                    Session(server, self.rfile, self.wfile).run()
                except OSError:
                    pass  # connection dropped by drop_connections()

        return Handler


class Session:
    def __init__(self, server: FakeIMAPServer, rfile, wfile):
        self.server = server
        self.rfile = rfile
        self.wfile = wfile
        self.folder: FakeFolder | None = None
        self.readonly = True

    def send(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode()
        self.wfile.write(data)
        self.wfile.flush()

    def read_command(self) -> tuple[str, list[bytes]] | None:
        """Read one command line plus any literals; return (line, literals)."""
        line = self.rfile.readline()
        if not line:
            return None
        text = b""
        literals: list[bytes] = []
        while True:
            m = re.search(rb"\{(\d+)\}\r\n$", line)
            if not m:
                text += line
                break
            text += line[: m.start()] + b"\x00LIT%d\x00" % len(literals)
            self.send("+ go ahead\r\n")
            literals.append(self.rfile.read(int(m.group(1))))
            line = self.rfile.readline()
        return text.rstrip(b"\r\n").decode("utf-8", "replace"), literals

    def run(self) -> None:
        self.send("* OK fake IMAP ready\r\n")
        while True:
            got = self.read_command()
            if got is None:
                return
            line, literals = got
            tag, _, rest = line.partition(" ")
            cmd, _, args = rest.partition(" ")
            cmd = cmd.upper()
            logged = line
            if cmd == "LOGIN":
                logged = f"{tag} LOGIN <redacted>"
            self.server.commands.append(logged)
            handler = getattr(self, "do_" + cmd.replace(" ", "_"), None)
            if handler is None:
                # Behave like a permissive real server: accept it. Tests
                # assert on self.server.commands, not on this response.
                self.send(f"{tag} OK {cmd} accepted\r\n")
                continue
            if handler(tag, args, literals) == "BYE":
                return

    # -- commands

    def do_CAPABILITY(self, tag, args, lits):
        self.send(f"* CAPABILITY {' '.join(self.server.capabilities)}\r\n{tag} OK done\r\n")

    def do_NOOP(self, tag, args, lits):
        self.send(f"{tag} OK done\r\n")

    def do_LOGIN(self, tag, args, lits):
        user, password = [_unquote(t) for t in tokenize(args, lits)[:2]]
        if (user, password) == (USER, PASSWORD):
            self.server.logins += 1
            self.send(f"{tag} OK logged in\r\n")
        else:
            self.send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials\r\n")

    def do_LOGOUT(self, tag, args, lits):
        self.send(f"* BYE bye\r\n{tag} OK done\r\n")
        return "BYE"

    def do_LIST(self, tag, args, lits):
        for f in self.server.folders.values():
            name = f.name.replace("\\", "\\\\").replace('"', '\\"')
            self.send(f'* LIST ({" ".join(f.flags)}) "/" "{name}"\r\n')
        self.send(f"{tag} OK done\r\n")

    def _open(self, tag, args, lits, readonly):
        name = _unquote(tokenize(args, lits)[0])
        folder = self.server.folders.get(name)
        if folder is None:
            self.send(f"{tag} NO [NONEXISTENT] Unknown Mailbox: {name}\r\n")
            return
        self.folder, self.readonly = folder, readonly
        mode = "READ-ONLY" if readonly else "READ-WRITE"
        self.send(f"* FLAGS (\\Seen \\Flagged)\r\n* {len(folder.messages)} EXISTS\r\n"
                  f"* OK [UIDVALIDITY 1] ok\r\n{tag} OK [{mode}] opened\r\n")

    def do_EXAMINE(self, tag, args, lits):
        self._open(tag, args, lits, True)

    def do_SELECT(self, tag, args, lits):
        self._open(tag, args, lits, False)

    def do_UID(self, tag, args, lits):
        sub, _, rest = args.partition(" ")
        sub = sub.upper()
        if self.folder is None:
            self.send(f"{tag} BAD no mailbox\r\n")
        elif sub == "SEARCH":
            tokens = tokenize(rest, lits)
            if tokens and str(tokens[0]).upper() == "CHARSET":
                tokens = tokens[2:]
            matched = [m.uid for m in self.folder.messages if evaluate(tokens, m)]
            self.send(f"* SEARCH {' '.join(map(str, matched))}\r\n".replace(" \r\n", "\r\n"))
            self.send(f"{tag} OK search done\r\n")
        elif sub == "FETCH":
            uid_set, _, items = rest.partition(" ")
            wanted = parse_uid_set(uid_set)
            for seq, msg in enumerate(self.folder.messages, 1):
                if msg.uid in wanted:
                    self.send(b"* %d FETCH (" % seq + self.fetch_items(msg, items) + b")\r\n")
            self.send(f"{tag} OK fetch done\r\n")
        else:
            self.send(f"{tag} OK UID {sub} accepted\r\n")

    def fetch_items(self, msg: FakeMessage, items: str) -> bytes:
        out: list[bytes] = [b"UID %d" % msg.uid]
        for m in re.finditer(r"(BODY(?:\.PEEK)?\[[^\]]*\](?:<\d+\.\d+>)?|UID|FLAGS|RFC822\.SIZE|X-GM-THRID|INTERNALDATE)",
                             items.strip("()"), re.I):
            item = m.group(1).upper()
            if item == "UID":
                continue
            if item == "FLAGS":
                out.append(f"FLAGS ({' '.join(sorted(msg.flags))})".encode())
            elif item == "RFC822.SIZE":
                out.append(b"RFC822.SIZE %d" % len(msg.raw))
            elif item == "X-GM-THRID":
                out.append(b"X-GM-THRID %d" % (msg.thrid or 0))
            elif item == "INTERNALDATE":
                out.append(b'INTERNALDATE "01-Jan-2026 00:00:00 +0000"')
            else:
                if ".PEEK" not in item and not self.readonly:
                    msg.flags.add("\\Seen")
                section = re.search(r"\[([^\]]*)\]", item).group(1)
                partial = re.search(r"<(\d+)\.(\d+)>", item)
                data = section_bytes(msg.raw, section)
                name = f"BODY[{section}]".encode()
                if partial:
                    start, length = int(partial.group(1)), int(partial.group(2))
                    data = data[start : start + length]
                    name += b"<%d>" % start
                out.append(name + b" {%d}\r\n" % len(data) + data)
        return b" ".join(out)


# ------------------------------------------------------------------ helpers

def section_bytes(raw: bytes, section: str) -> bytes:
    head, sep, body = raw.partition(b"\r\n\r\n")
    sec = section.upper()
    if sec == "":
        return raw
    if sec == "TEXT":
        return body
    if sec == "HEADER":
        return head + b"\r\n\r\n"
    m = re.match(r"HEADER\.FIELDS \(([^)]*)\)", sec)
    if m:
        wanted = set(m.group(1).split())
        lines, keep = [], False
        for line in head.split(b"\r\n"):
            if line[:1] in (b" ", b"\t"):
                if keep:
                    lines.append(line)
                continue
            keep = line.split(b":", 1)[0].decode().upper() in wanted
            if keep:
                lines.append(line)
        return b"\r\n".join(lines) + b"\r\n\r\n"
    raise ValueError(f"unsupported section {section}")


def parse_uid_set(text: str) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        if ":" in part:
            a, b = part.split(":")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def tokenize(text: str, literals: list[bytes]) -> list:
    """Split IMAP args into atoms, quoted strings and nested lists."""
    pos = 0

    def parse_list(end: str | None) -> list:
        nonlocal pos
        items: list = []
        while pos < len(text):
            ch = text[pos]
            if ch == " ":
                pos += 1
            elif ch == ")" and end == ")":
                pos += 1
                return items
            elif ch == "(":
                pos += 1
                items.append(parse_list(")"))
            elif ch == '"':
                pos += 1
                buf = []
                while text[pos] != '"':
                    if text[pos] == "\\":
                        pos += 1
                    buf.append(text[pos])
                    pos += 1
                pos += 1
                items.append(Quoted("".join(buf)))
            elif ch == "\x00":
                end_lit = text.index("\x00", pos + 1)
                idx = int(text[pos + 4 : end_lit])
                items.append(Quoted(literals[idx].decode("utf-8")))
                pos = end_lit + 1
            else:
                m = re.match(r"[^ ()]+", text[pos:])
                items.append(m.group(0))
                pos += len(m.group(0))
        return items

    return parse_list(None)


class Quoted(str):
    pass


def _unquote(token) -> str:
    return str(token)


def _header(msg: FakeMessage, name: str) -> str:
    return str(msg.parsed.get(name) or "")


def evaluate(tokens: list, msg: FakeMessage) -> bool:
    it = iter(tokens)

    def one(tok) -> bool:
        if isinstance(tok, list):
            return all_of(iter(tok))
        key = str(tok).upper()
        if key == "ALL":
            return True
        if key == "UNSEEN":
            return "\\Seen" not in msg.flags
        if key == "OR":
            return one(next(it)) | one(next(it))
        if key in ("SINCE", "BEFORE"):
            day = _imap_date(next(it))
            sent = parsedate_to_datetime(_header(msg, "Date")).date()
            return sent >= day if key == "SINCE" else sent < day
        if key in ("FROM", "TO", "SUBJECT"):
            return str(next(it)).lower() in _header(msg, key.capitalize()).lower()
        if key == "TEXT":
            needle = str(next(it)).lower()
            parsed = msg.parsed
            texts = [str(v) for v in parsed.values()]
            texts += [
                (p.get_payload(decode=True) or b"").decode("utf-8", "replace")
                for p in parsed.walk()
                if p.get_content_maintype() == "text"
            ]
            return needle in "\n".join(texts).lower()
        if key == "HEADER":
            name, value = str(next(it)), str(next(it))
            return value.lower() in _header(msg, name).lower()
        if key == "X-GM-THRID":
            return msg.thrid == int(next(it))
        raise ValueError(f"unsupported search key {key}")

    def all_of(items) -> bool:
        nonlocal it
        saved, it = it, items
        try:
            return all([one(t) for t in it])
        finally:
            it = saved

    return all_of(iter(tokens))


def _imap_date(text: str):
    import datetime as dt

    return dt.datetime.strptime(str(text), "%d-%b-%Y").date()
