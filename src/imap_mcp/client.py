"""Read-only IMAP operations behind the MCP tools.

One connection per tool call: connect, LOGIN, EXAMINE (never SELECT), fetch
with BODY.PEEK, LOGOUT. That avoids stale-connection handling at the cost of
a login round-trip per call, which is fine at agent call rates.
"""

from __future__ import annotations

import datetime as dt
import re
import ssl
from typing import Any

from . import imap_utf7, mime
from .config import Config
from .guard import ReadOnlyIMAP4, ReadOnlyIMAP4_SSL

SEARCH_LIMIT_MAX = 50
SNIPPET_CHARS = 120
SNIPPET_FETCH_BYTES = 4096
BODY_CHARS_DEFAULT = 4000
BODY_CHARS_MAX = 20000
THREAD_LIMIT_MAX = 50
# Messages bigger than this are fetched partially: body text is still
# available, but attachments past the cut-off are missing from the list.
MESSAGE_FETCH_BYTES = 10 * 1024 * 1024
THREAD_REFERENCE_IDS_MAX = 10

SUMMARY_HEADERS = "DATE FROM SUBJECT CONTENT-TYPE CONTENT-TRANSFER-ENCODING"
SPECIAL_USE = {"\\All", "\\Archive", "\\Drafts", "\\Flagged", "\\Important",
               "\\Junk", "\\Sent", "\\Trash", "\\Noselect"}

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FETCH_START = re.compile(rb"^\s*\d+ \(")
_LITERAL_KEY = re.compile(rb"((?:BODY|BINARY)\[[^\]]*\](?:<\d+>)?|RFC822(?:\.\w+)?)\s*\{\d+\}\s*$", re.I)
_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>NIL|"(?:[^"\\]|\\.)*") ?(?P<name>.*)$', re.S)
_SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg|sv|vs)\s*(\[\d+\])?\s*:\s*)+", re.I)


class MailError(Exception):
    """User-facing error; message is safe to return to the model."""


# --------------------------------------------------------------------- helpers

def quote(value: str) -> str:
    if any(c in value for c in "\r\n\0"):
        raise MailError("CR, LF and NUL are not allowed in search terms or folder names")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_date(value: str, field: str) -> str:
    try:
        d = dt.date.fromisoformat(value.strip())
    except ValueError:
        raise MailError(f"{field} must be YYYY-MM-DD, got {value!r}") from None
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def build_search(
    since: str | None = None,
    before: str | None = None,
    from_: str | None = None,
    to: str | None = None,
    subject: str | None = None,
    text: str | None = None,
    unseen_only: bool = False,
) -> tuple[list[str], bytes | None]:
    """Return (criteria tokens, optional UTF-8 literal for the last token).

    imaplib supports a single literal per command, so at most one search term
    may contain non-ASCII characters; it is sent last with CHARSET UTF-8.
    """
    tokens: list[str] = []
    if since:
        tokens += ["SINCE", imap_date(since, "since")]
    if before:
        tokens += ["BEFORE", imap_date(before, "before")]
    if unseen_only:
        tokens.append("UNSEEN")
    non_ascii: list[tuple[str, str]] = []
    for key, value in (("FROM", from_), ("TO", to), ("SUBJECT", subject), ("TEXT", text)):
        if not value:
            continue
        if value.isascii():
            tokens += [key, quote(value)]
        else:
            non_ascii.append((key, value))
    if len(non_ascii) > 1:
        raise MailError("only one search term may contain non-ASCII characters")
    if non_ascii:
        key, value = non_ascii[0]
        quote(value)  # reject CR/LF/NUL
        return tokens + [key], value.encode("utf-8")
    return tokens or ["ALL"], None


def parse_fetch(data: list[Any]) -> list[dict]:
    """Group imaplib FETCH output into per-message dicts.

    Each dict has "meta" (all non-literal text) and "lits" mapping
    "header" / "text" / "full" to literal bytes.
    """
    messages: list[dict] = []
    cur: dict | None = None
    for item in data or []:
        if item is None:
            continue
        meta = item[0] if isinstance(item, tuple) else item
        if _FETCH_START.match(meta):
            cur = {"meta": b"", "lits": {}}
            messages.append(cur)
        if cur is None:
            continue
        cur["meta"] += meta
        if isinstance(item, tuple):
            m = _LITERAL_KEY.search(meta)
            key = m.group(1).upper() if m else b""
            if key.startswith(b"BODY[HEADER"):
                cur["lits"]["header"] = item[1]
            elif key.startswith(b"BODY[TEXT"):
                cur["lits"]["text"] = item[1]
            elif key.startswith(b"BODY[]"):
                cur["lits"]["full"] = item[1]
    out = []
    for msg in messages:
        meta = msg["meta"]
        uid = re.search(rb"\bUID (\d+)", meta)
        if not uid:
            continue  # unsolicited FETCH (e.g. flag update)
        flags = re.search(rb"\bFLAGS \(([^)]*)\)", meta)
        size = re.search(rb"\bRFC822\.SIZE (\d+)", meta)
        thrid = re.search(rb"\bX-GM-THRID (\d+)", meta)
        out.append(
            {
                "uid": int(uid.group(1)),
                "flags": flags.group(1).decode("ascii", "replace").split() if flags else [],
                "size": int(size.group(1)) if size else None,
                "thrid": thrid.group(1).decode() if thrid else None,
                "lits": msg["lits"],
            }
        )
    return out


def parse_list(data: list[Any]) -> list[dict]:
    folders = []
    for item in data or []:
        if item is None:
            continue
        if isinstance(item, tuple):  # name sent as a literal
            line, literal = item
            m = _LIST_LINE.match(re.sub(rb"\s*\{\d+\}$", b"", line) + b" ")
            raw_name = literal
        else:
            m = _LIST_LINE.match(item)
            raw_name = m.group("name").strip() if m else b""
            if raw_name.startswith(b'"') and raw_name.endswith(b'"'):
                raw_name = re.sub(rb"\\(.)", rb"\1", raw_name[1:-1])
        if not m:
            continue
        name = imap_utf7.decode(raw_name.decode("utf-8", "replace"))
        flags = m.group("flags").decode("ascii", "replace").split()
        entry: dict = {"name": name}
        special = [f for f in flags if f.capitalize() in SPECIAL_USE or f in SPECIAL_USE]
        if special:
            entry["flags"] = special
        folders.append(entry)
    return folders


def normalize_subject(subject: str) -> str:
    return _SUBJECT_PREFIX.sub("", subject).strip()


def _uids(data: list[Any]) -> list[int]:
    raw = b" ".join(d for d in data or [] if isinstance(d, bytes))
    return sorted({int(x) for x in raw.split() if x.isdigit()})


# ---------------------------------------------------------------------- client

class Mailbox:
    """Context manager wrapping one read-only IMAP session."""

    def __init__(self, config: Config):
        self.config = config
        self.conn: Any = None
        self.capabilities: set[str] = set()

    def __enter__(self) -> "Mailbox":
        cfg = self.config
        try:
            if cfg.ssl:
                self.conn = ReadOnlyIMAP4_SSL(
                    cfg.host, cfg.port, ssl_context=ssl.create_default_context(), timeout=cfg.timeout
                )
            else:
                self.conn = ReadOnlyIMAP4(cfg.host, cfg.port, timeout=cfg.timeout)
                if "STARTTLS" in self.conn.capabilities:
                    self.conn.starttls(ssl.create_default_context())
            self.conn.login(cfg.user, cfg.password)
        except MailError:
            raise
        except Exception as exc:
            self._close()
            # Server/OS messages never contain the password; the config repr hides it.
            raise MailError(f"IMAP connection/login to {cfg.host}:{cfg.port} failed: {exc}") from None
        self.capabilities = {str(c).upper() for c in self.conn.capabilities}
        return self

    def __exit__(self, *exc: object) -> None:
        self._close()

    def _close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    # -- primitives

    def _check(self, typ: str, data: Any, what: str) -> Any:
        if typ != "OK":
            detail = b" ".join(d for d in data or [] if isinstance(d, bytes)).decode("utf-8", "replace")
            raise MailError(f"{what} failed: {detail or typ}")
        return data

    def examine(self, folder: str) -> int:
        """Open folder read-only; return its message count."""
        typ, data = self.conn.select(quote(imap_utf7.encode(folder)), readonly=True)
        self._check(typ, data, f"opening folder {folder!r}")
        try:
            return int(data[0])
        except (TypeError, ValueError, IndexError):
            return 0

    def uid_search(self, tokens: list[str], literal: bytes | None = None) -> list[int]:
        if literal is not None:
            self.conn.literal = literal
            typ, data = self.conn.uid("SEARCH", "CHARSET", "UTF-8", *tokens)
        else:
            typ, data = self.conn.uid("SEARCH", *tokens)
        return _uids(self._check(typ, data, "search"))

    def uid_fetch(self, uids: list[int], items: str) -> list[dict]:
        if not uids:
            return []
        typ, data = self.conn.uid("FETCH", ",".join(map(str, uids)), items)
        return parse_fetch(self._check(typ, data, "fetch"))

    def summaries(self, uids: list[int], with_snippet: bool = True) -> list[dict]:
        items = f"(UID FLAGS BODY.PEEK[HEADER.FIELDS ({SUMMARY_HEADERS})]"
        items += f" BODY.PEEK[TEXT]<0.{SNIPPET_FETCH_BYTES}>)" if with_snippet else ")"
        by_uid = {m["uid"]: m for m in self.uid_fetch(uids, items)}
        out = []
        for uid in uids:
            m = by_uid.get(uid)
            if m is None:
                continue
            raw = m["lits"].get("header", b"") + m["lits"].get("text", b"")
            msg = mime.parse_bytes(raw)
            entry = {
                "uid": uid,
                "date": mime.format_date(msg.get("Date")),
                "from": mime.format_addresses(msg.get("From")),
                "subject": mime.decode_mime_header(msg.get("Subject")),
                "unread": "\\Seen" not in m["flags"],
            }
            if with_snippet:
                entry["snippet"] = mime.snippet(msg, SNIPPET_CHARS)
            out.append(entry)
        return out

    # -- tools

    def list_folders(self) -> list[dict]:
        typ, data = self.conn.list()
        return parse_list(self._check(typ, data, "LIST"))

    def search(self, folder: str, limit: int, **criteria: Any) -> dict:
        limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
        tokens, literal = build_search(**criteria)
        self.examine(folder)
        uids = self.uid_search(tokens, literal)
        newest = list(reversed(uids[-limit:]))
        return {
            "folder": folder,
            "total": len(uids),
            "returned": len(newest),
            "messages": self.summaries(newest),
        }

    def get_message(self, folder: str, uid: int, max_chars: int) -> dict:
        max_chars = max(200, min(int(max_chars), BODY_CHARS_MAX))
        self.examine(folder)
        fetched = self.uid_fetch(
            [int(uid)], f"(UID FLAGS RFC822.SIZE BODY.PEEK[]<0.{MESSAGE_FETCH_BYTES}>)"
        )
        if not fetched:
            raise MailError(f"no message with uid {uid} in {folder!r}")
        m = fetched[0]
        msg = mime.parse_bytes(m["lits"].get("full", b""))
        body, source, attachments = mime.extract(msg)
        body, truncated = mime.truncate(body, max_chars)
        result: dict = {
            "uid": m["uid"],
            "folder": folder,
            "date": mime.format_date(msg.get("Date")),
            "from": mime.format_addresses(msg.get("From")),
            "to": mime.format_addresses(msg.get("To")),
            "subject": mime.decode_mime_header(msg.get("Subject")),
            "unread": "\\Seen" not in m["flags"],
            "message_id": (msg.get("Message-ID") or "").strip(),
            "body": body,
            "body_format": source or "none",
            "body_truncated": truncated,
            "attachments": attachments,
        }
        cc = mime.format_addresses(msg.get("Cc"))
        if cc:
            result["cc"] = cc
        if m["size"] and m["size"] > MESSAGE_FETCH_BYTES:
            result["attachments_incomplete"] = True
        return result

    def get_thread(self, folder: str, uid: int, limit: int) -> dict:
        limit = max(1, min(int(limit), THREAD_LIMIT_MAX))
        self.examine(folder)
        if "X-GM-EXT-1" in self.capabilities:
            found = self._gmail_thread(folder, int(uid))
            if found:
                return self._thread_result(*found, limit=limit)
        target = self.uid_fetch(
            [int(uid)],
            "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES IN-REPLY-TO SUBJECT)])",
        )
        if not target:
            raise MailError(f"no message with uid {uid} in {folder!r}")
        hdr = mime.parse_bytes(target[0]["lits"].get("header", b""))
        ids = re.findall(r"<[^<>\s]+>", " ".join(
            str(hdr.get(h) or "") for h in ("References", "In-Reply-To", "Message-ID")))
        ids = list(dict.fromkeys(ids))[-THREAD_REFERENCE_IDS_MAX:]
        uids: list[int] = []
        if ids:
            uids = self.uid_search(_or_all(
                [["HEADER", "Message-ID", quote(i)] for i in ids]
                + [["HEADER", "References", quote(i)] for i in ids]
            ))
        if len(set(uids) - {int(uid)}) > 0:
            return self._thread_result("references", folder, uids, limit=limit)
        subject = normalize_subject(mime.decode_mime_header(hdr.get("Subject")))
        if subject:
            tokens, literal = build_search(subject=subject)
            uids = self.uid_search(tokens, literal)
            return self._thread_result("subject", folder, uids, limit=limit)
        return self._thread_result("none", folder, [int(uid)], limit=limit)

    def _gmail_thread(self, folder: str, uid: int) -> tuple[str, str, list[int]] | None:
        fetched = self.uid_fetch([uid], "(UID X-GM-THRID)")
        if not fetched or not fetched[0]["thrid"]:
            return None
        thrid = fetched[0]["thrid"]
        # Search All Mail so replies filed elsewhere (Sent) are included.
        all_mail = next(
            (f["name"] for f in self.list_folders() if "\\All" in f.get("flags", [])), folder
        )
        if all_mail != folder:
            self.examine(all_mail)
        return "gmail-thrid", all_mail, self.uid_search(["X-GM-THRID", thrid])

    def _thread_result(self, method: str, folder: str, uids: list[int], limit: int) -> dict:
        # Oldest first; UID order is arrival order within a folder.
        messages = self.summaries(sorted(uids)[-limit:])
        return {
            "method": method,
            "folder": folder,
            "total": len(uids),
            "returned": len(messages),
            "messages": messages,
        }


def _or_all(groups: list[list[str]]) -> list[str]:
    """Combine criteria groups with IMAP's prefix OR: OR a (OR b c)."""
    if len(groups) == 1:
        return groups[0]
    return ["OR", _paren(groups[0]), _paren(_or_all(groups[1:]))]


def _paren(tokens: list[str]) -> str:
    return "(" + " ".join(tokens) + ")"
