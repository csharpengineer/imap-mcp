"""stdio MCP server exposing read-only IMAP tools."""

from __future__ import annotations

import json
from typing import Annotated, Any, Callable

import anyio
from pydantic import Field
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import client
from .client import Mailbox, MailError
from .config import Config, ConfigError
from .guard import ReadOnlyViolation

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

mcp = MCPServer(
    name="imap-mcp",
    instructions=(
        "Read-only access to one IMAP mailbox. Use list_folders to find folder "
        "names, search_mail to find messages (returns uids), then get_message or "
        "get_thread with that folder and uid. Nothing here can modify mail."
    ),
)


async def _run(op: Callable[[Mailbox], Any]) -> str:
    def work() -> Any:
        try:
            config = Config.from_env()
            with Mailbox(config) as box:
                return op(box)
        except (MailError, ConfigError, ReadOnlyViolation) as exc:
            raise ToolError(str(exc)) from None

    # imaplib is blocking; keep the stdio event loop responsive.
    result = await anyio.to_thread.run_sync(work)
    # Compact JSON: consumers include small local models with tight context.
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


Folder = Annotated[str, Field(description="Folder name as returned by list_folders, e.g. INBOX.")]
Uid = Annotated[int, Field(description="Message uid from search_mail or get_thread (uids are per folder).")]
Date = Annotated[str | None, Field(description="Date as YYYY-MM-DD.")]


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def list_folders() -> str:
    r"""List mailbox folders. Special folders carry flags like \All, \Sent, \Trash."""
    return await _run(lambda box: {"folders": box.list_folders()})


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def search_mail(
    folder: Folder = "INBOX",
    since: Date = None,
    before: Date = None,
    from_address: Annotated[str | None, Field(description="Substring of the From header.")] = None,
    to_address: Annotated[str | None, Field(description="Substring of the To header.")] = None,
    subject: Annotated[str | None, Field(description="Substring of the subject.")] = None,
    text: Annotated[str | None, Field(description="Substring anywhere in headers or body.")] = None,
    unseen_only: Annotated[bool, Field(description="Only unread messages.")] = False,
    limit: Annotated[int, Field(description="Max results, 1-50.")] = 10,
) -> str:
    """Search one folder for messages; newest first. since is inclusive, before is exclusive.

    Returns uid, date, from, subject, unread and a short snippet per message.
    """
    return await _run(
        lambda box: box.search(
            folder,
            limit,
            since=since,
            before=before,
            from_=from_address,
            to=to_address,
            subject=subject,
            text=text,
            unseen_only=unseen_only,
        )
    )


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def get_message(
    folder: Folder,
    uid: Uid,
    max_chars: Annotated[int, Field(description="Body character cap, 200-20000.")] = client.BODY_CHARS_DEFAULT,
) -> str:
    """Read one message: headers, plain-text body (HTML converted), attachment names and sizes.

    Attachment contents are never returned. Reading does not mark the message as read.
    """
    return await _run(lambda box: box.get_message(folder, uid, max_chars))


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def get_thread(
    folder: Folder,
    uid: Uid,
    limit: Annotated[int, Field(description="Max messages, 1-50; keeps the newest.")] = 20,
) -> str:
    """List the conversation a message belongs to, oldest first.

    The returned uids belong to the returned "folder" (on Gmail that is
    All Mail, so your sent replies are included).
    """
    return await _run(lambda box: box.get_thread(folder, uid, limit))


def main() -> None:
    mcp.run("stdio")
