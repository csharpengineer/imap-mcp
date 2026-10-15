"""MIME parsing: decoded headers, a plain-text body, attachment metadata."""

from __future__ import annotations

import base64
import binascii
import email
import email.policy
import html
import re
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

_WS = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n\s*\n+")


def parse_bytes(raw: bytes) -> Message:
    # compat32 is the most forgiving policy for malformed real-world mail.
    return email.message_from_bytes(raw, policy=email.policy.compat32)


def decode_mime_header(value: object) -> str:
    """Decode RFC 2047 encoded-words; never raises."""
    if value is None:
        return ""
    text = str(value)
    try:
        decoded = str(make_header(decode_header(text)))
    except Exception:
        decoded = text
    return " ".join(decoded.split())


def format_addresses(value: object) -> str:
    """'Name <a@b>, c@d' with encoded display names decoded."""
    if not value:
        return ""
    parts = []
    for name, addr in getaddresses([str(value)]):
        name = decode_mime_header(name)
        if name and addr:
            parts.append(f"{name} <{addr}>")
        else:
            parts.append(addr or name)
    return ", ".join(p for p in parts if p)


def format_date(value: object) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(str(value)).isoformat()
    except Exception:
        return decode_mime_header(value)


class _HTMLToText(HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "pre", "table", "hr"}
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(markup: str) -> str:
    parser = _HTMLToText()
    try:
        parser.feed(markup)
        parser.close()
        text = "".join(parser.out)
    except Exception:
        text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    return normalize_text(text)


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [_WS.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _payload_bytes(part: Message) -> bytes | None:
    cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
    if cte == "base64":
        # A partial fetch can cut base64 mid-quantum; compat32 then returns the
        # undecoded text. Decode the complete quanta ourselves.
        encoded = re.sub(rb"[^A-Za-z0-9+/]", b"", part.get_payload(decode=False).encode("ascii", "ignore"))
        try:
            return base64.b64decode(encoded[: len(encoded) // 4 * 4])
        except binascii.Error:
            return None
    return part.get_payload(decode=True)


def _decode_part(part: Message) -> str:
    payload = _payload_bytes(part)
    if payload is None:
        return ""
    declared = part.get_content_charset()
    if declared:
        try:
            # "replace" keeps a body readable when a partial fetch cut a
            # multi-byte character in half.
            return payload.decode(declared, "replace")
        except LookupError:
            pass  # unknown charset label; guess below
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("cp1252", "replace")


def _is_attachment(part: Message) -> bool:
    disposition = (part.get("Content-Disposition") or "").split(";")[0].strip().lower()
    if disposition == "attachment" or part.get_filename():
        return True
    # Unnamed non-text leaves (e.g. inline images) are still listed so the
    # caller knows they exist; their bytes are never returned.
    return part.get_content_maintype() != "text"


def extract(msg: Message) -> tuple[str, str, list[dict]]:
    """Return (body_text, body_source, attachments).

    body_source is "text/plain", "text/html" or "" (no text part). Prefers
    text/plain; falls back to HTML converted to text.
    """
    plain: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if _is_attachment(part):
            payload = _payload_bytes(part)
            attachments.append(
                {
                    "filename": decode_mime_header(part.get_filename()) or "(unnamed)",
                    "content_type": part.get_content_type(),
                    "size": len(payload) if payload is not None else 0,
                }
            )
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain.append(_decode_part(part))
        elif ctype == "text/html":
            html_parts.append(_decode_part(part))
    if any(p.strip() for p in plain):
        return normalize_text("\n\n".join(plain)), "text/plain", attachments
    if html_parts:
        return html_to_text("\n".join(html_parts)), "text/html", attachments
    return "", "", attachments


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + " …", True


def snippet(msg: Message, length: int = 160) -> str:
    body, _, _ = extract(msg)
    flat = " ".join(body.split())
    return truncate(flat, length)[0]
