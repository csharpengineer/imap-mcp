"""stdio MCP server exposing read-only IMAP tools for one or more accounts."""

# No `from __future__ import annotations` here: tool signatures are rewritten
# at runtime (_with_account_param) and pydantic needs real annotation objects.
import atexit
import inspect
import json
import sys
from typing import Annotated, Any, Callable

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import client
from .accounts import AccountPool
from .client import Mailbox, MailError
from .config import ConfigError, load_accounts
from .guard import ReadOnlyViolation

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

Folder = Annotated[str, Field(description="Folder name as returned by list_folders, e.g. INBOX.")]
Uid = Annotated[int, Field(description="Message uid from search_mail or get_thread (uids are per folder).")]
Date = Annotated[str | None, Field(description="Date as YYYY-MM-DD.")]


def _dump(result: Any) -> str:
    # Compact JSON: consumers include small local models with tight context.
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _with_account_param(fn: Callable, names: list[str]) -> Callable:
    """Give fn an `account` parameter whose schema lists the configured names.

    The account set is only known at startup, so the schema is set here:
    required (no default) with several accounts, defaulted with exactly one.
    Validation stays a plain str so unknown names get AccountPool's error,
    which lists the valid names.
    """
    sig = inspect.signature(fn)
    annotation = Annotated[
        str,
        Field(
            description="Account to use. One of: " + ", ".join(names),
            json_schema_extra={"enum": names},
        ),
    ]
    account = sig.parameters["account"].replace(
        annotation=annotation,
        default=names[0] if len(names) == 1 else inspect.Parameter.empty,
    )
    others = [p for name, p in sig.parameters.items() if name != "account"]
    fn.__signature__ = sig.replace(parameters=[account, *others])
    return fn


def create_server(pool: AccountPool) -> MCPServer:
    names = pool.names
    mcp = MCPServer(
        name="imap-mcp",
        instructions=(
            "Read-only access to IMAP mailboxes. Accounts: " + ", ".join(names) + ". "
            "Use list_folders to find folder names, search_mail to find messages "
            "(returns uids), then get_message or get_thread with the same account, "
            "folder and uid. Nothing here can modify mail."
        ),
    )

    async def run(account: str | None, op: Callable[[Mailbox], Any]) -> str:
        def work() -> Any:
            try:
                return pool.run(account, op)
            except (MailError, ReadOnlyViolation) as exc:
                raise ToolError(str(exc)) from None

        # imaplib is blocking; keep the stdio event loop responsive.
        return _dump(await anyio.to_thread.run_sync(work))

    def tool(fn: Callable) -> Callable:
        return mcp.tool(annotations=READ_ONLY, structured_output=False)(_with_account_param(fn, names))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def list_accounts() -> str:
        """List configured accounts: name, host, port, user, ssl, and whether a password is set."""
        return _dump({"accounts": pool.describe()})

    @tool
    async def list_folders(*, account: str) -> str:
        r"""List mailbox folders. Special folders carry flags like \All, \Sent, \Trash."""
        return await run(account, lambda box: {"account": box.account.name, "folders": box.list_folders()})

    @tool
    async def search_mail(
        *,
        account: str,
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
        return await run(
            account,
            lambda box: {
                "account": box.account.name,
                **box.search(
                    folder,
                    limit,
                    since=since,
                    before=before,
                    from_=from_address,
                    to=to_address,
                    subject=subject,
                    text=text,
                    unseen_only=unseen_only,
                ),
            },
        )

    @tool
    async def get_message(
        *,
        account: str,
        folder: Folder,
        uid: Uid,
        max_chars: Annotated[int, Field(description="Body character cap, 200-20000.")] = client.BODY_CHARS_DEFAULT,
    ) -> str:
        """Read one message: headers, plain-text body (HTML converted), attachment names and sizes.

        Attachment contents are never returned. Reading does not mark the message as read.
        """
        return await run(account, lambda box: {"account": box.account.name, **box.get_message(folder, uid, max_chars)})

    @tool
    async def get_thread(
        *,
        account: str,
        folder: Folder,
        uid: Uid,
        limit: Annotated[int, Field(description="Max messages, 1-50; keeps the newest.")] = 20,
    ) -> str:
        """List the conversation a message belongs to, oldest first.

        The returned uids belong to the returned "folder" (on Gmail that is
        All Mail, so your sent replies are included).
        """
        return await run(account, lambda box: {"account": box.account.name, **box.get_thread(folder, uid, limit)})

    return mcp


def main() -> None:
    try:
        accounts = load_accounts()
    except ConfigError as exc:
        print(f"imap-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    pool = AccountPool(accounts)
    missing = [a.name for a in accounts.values() if a.password() is None]
    if missing:
        # Not fatal: other accounts keep working. Names only, never values.
        print(f"imap-mcp: warning: no password env var set for account(s): {', '.join(missing)}", file=sys.stderr)
    atexit.register(pool.close_all)
    create_server(pool).run("stdio")
