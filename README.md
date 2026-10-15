# imap-mcp

A small, **read-only** [MCP](https://modelcontextprotocol.io) server that lets an AI agent search and read mail over IMAP. It works with any IMAP provider and has extra support for Gmail threads.

It only ever opens mailboxes with `EXAMINE`, which is read-only. It reads with `BODY.PEEK`, so reading a message never marks it as read. It exposes no tool that deletes, moves, flags, sends, or marks mail.

## Install

Requires Python 3.10+ (tested on 3.12 and 3.14). The only runtime dependency is the official `mcp` SDK, pinned to `2.3.0`.

```sh
# run straight from GitHub with uv (no clone needed)
uvx --from git+https://github.com/csharpengineer/imap-mcp imap-mcp

# or from a clone
git clone https://github.com/csharpengineer/imap-mcp
cd imap-mcp
uv venv && uv pip install -e .        # or: python -m venv .venv && pip install -e .
imap-mcp                              # or: python -m imap_mcp
```

The server talks MCP over stdio. Start it from an MCP client, not by hand.

## Configuration

You can configure one account or several. The caller picks the account on every call. Configuration has two sources, and you can use both at once:

1. **An account file** (recommended when you have more than one account). Point `IMAP_MCP_CONFIG` at a JSON file.
2. **Single-account env vars** (`IMAP_HOST`, `IMAP_USER`, `IMAP_PASS`, ...). These become an account named `default`.

**Passwords never go in the config file.** Each account names an env var with `password_env`, and the server reads the password from that env var when it connects. If you put a `password` key in the file, the server refuses to start.

### Multiple accounts

Start from [`accounts.example.json`](accounts.example.json). Copy it to `accounts.json`, which git ignores:

```json
{
  "accounts": {
    "gmail": {
      "host": "imap.gmail.com",
      "user": "you@gmail.com",
      "password_env": "GMAIL_IMAP_PASS"
    },
    "work": {
      "host": "mail.example.com",
      "port": 993,
      "user": "you@example.com",
      "ssl": true,
      "timeout": 30,
      "password_env": "WORK_IMAP_PASS"
    }
  }
}
```

Then provide the environment:

```sh
IMAP_MCP_CONFIG=/path/to/accounts.json
GMAIL_IMAP_PASS=<gmail app password>      # one secret per account
WORK_IMAP_PASS=<work password>
```

| Key | Required | Default | Notes |
|---|---|---|---|
| `host` | yes | | IMAP server hostname |
| `user` | yes | | login name, usually the email address |
| `password_env` | yes | | **name** of the env var holding the password, e.g. `WORK_IMAP_PASS` |
| `port` | no | `993` (`143` when `ssl` is false) | |
| `ssl` | no | `true` | implicit TLS. With `false`, STARTTLS is used whenever the server advertises it |
| `timeout` | no | `30` | socket timeout in seconds |

Account names may use letters, digits, `_`, `.` and `-`. Unknown keys are rejected.

### Single account (env vars only)

| Variable | Required | Default |
|---|---|---|
| `IMAP_HOST` | yes | |
| `IMAP_USER` | yes | |
| `IMAP_PASS` | yes | never logged or returned |
| `IMAP_PORT` | no | `993` (`143` when `IMAP_SSL=false`) |
| `IMAP_SSL` | no | `true` |
| `IMAP_TIMEOUT` | no | `30` |

See `.env.example`.

### Startup and failure behaviour

- **Invalid configuration stops the server at startup.** It exits with code 2 and prints a message on stderr. Examples are a missing file, invalid JSON, a missing key, a bad port, a password in the file, or no accounts at all. Error messages never echo values from the file.
- **A missing password env var is not fatal.** The server starts, logs a warning that names the account (never a value), and only that account's calls fail. `list_accounts` reports `password_set: false` for it.
- **Connections are per account.** Each account connects lazily on first use and the connection is reused between calls. If the connection drops (idle timeout, network), the server reconnects and retries once. Every operation is read-only, so the retry is safe. A failure on one account never affects the others.

## Tools

All tools return compact JSON and carry the MCP `readOnlyHint` annotation. A UID belongs to one account and one folder, so always pass the same `account` and `folder` you got it from.

Every mail tool takes `account`. Its schema lists the configured names as an enum. It is **required** when several accounts are configured and **optional** when there is exactly one. An unknown name returns an error that lists the valid names.

| Tool | Arguments | Returns |
|---|---|---|
| `list_accounts` | none | `{accounts: [{name, host, port, user, ssl, password_env, password_set}]}`. This never includes passwords |
| `list_folders` | `account` | `{account, folders: [{name, flags?}]}`. `flags` shows only special-use flags such as `\All`, `\Sent`, `\Trash`, `\Noselect` |
| `search_mail` | `account`, `folder`="INBOX", `since`, `before` (YYYY-MM-DD; since is inclusive, before is exclusive), `from_address`, `to_address`, `subject`, `text`, `unseen_only`, `limit`=10 (max 50) | `{account, folder, total, returned, messages: [{uid, date, from, subject, unread, snippet}]}`, newest first |
| `get_message` | `account`, `folder`, `uid`, `max_chars`=4000 (200–20000) | `{account, uid, folder, date, from, to, cc?, subject, unread, message_id, body, body_format, body_truncated, attachments: [{filename, content_type, size}], attachments_incomplete?}` |
| `get_thread` | `account`, `folder`, `uid`, `limit`=20 (max 50) | `{account, method, folder, total, returned, messages: [...]}`, oldest first |

How the tools behave:

- **Bodies.** `get_message` prefers `text/plain`. For HTML-only mail it converts the HTML to text and drops scripts and styles. It decodes RFC 2047 headers and any charset Python knows. An unknown charset label falls back to UTF-8, then cp1252. Bodies longer than `max_chars` end with `…` and set `body_truncated: true`.
- **Attachments.** You get names, types, and decoded sizes. Attachment contents are never returned. Messages larger than 10 MB are fetched partially, and the result carries `attachments_incomplete: true`.
- **Search terms.** Matching is substring matching done by the server. At most one search term may contain non-ASCII characters. It is sent as a UTF-8 literal, a limit that comes from imaplib.
- **Threads.** If the server advertises `X-GM-EXT-1` (Gmail), `get_thread` uses `X-GM-THRID` and searches the `\All` folder, so your sent replies are included. The returned `folder` is then `[Gmail]/All Mail`, and the returned UIDs belong to that folder. On other servers it follows `Message-ID` / `References` / `In-Reply-To`. If those find nothing, it falls back to matching the normalized subject (with `Re:`/`Fwd:` stripped), and `method` reports which approach was used.

## How read-only is enforced

Two layers enforce it, both in this code:

1. **The tools only issue read commands:** `EXAMINE`, `UID SEARCH`, and `UID FETCH` with `BODY.PEEK[...]`.
2. **A wire-level allowlist** (`src/imap_mcp/guard.py`). This wraps imaplib's single command choke point. Any command outside `CAPABILITY NOOP LOGIN AUTHENTICATE STARTTLS LOGOUT LIST LSUB STATUS EXAMINE SEARCH FETCH UID(SEARCH|FETCH)` raises an error before anything is sent. So do `FETCH`es of `BODY[...]`, `BINARY[...]`, `RFC822`, or `RFC822.TEXT`, since those set `\Seen`. `SELECT`, `STORE`, `COPY`, `MOVE`, `EXPUNGE`, `CLOSE`, `APPEND`, `CREATE`, `DELETE`, `RENAME`, and the rest are all blocked.

The tests run every tool against a fake IMAP server that records each command it receives. They then assert that only read commands arrived and that no message flags changed.

## Gmail

- Host `imap.gmail.com`, port `993`, SSL on (all defaults). See the `gmail` entry in `accounts.example.json`.
- Turn on 2-Step Verification for the Google account. Then create an **app password** (Google Account → Security → App passwords) and put it in the env var named by that account's `password_env` (or `IMAP_PASS` in single-account mode). Your normal Google password will not work.
- Use the full address as `user` (or `IMAP_USER`).
- Gmail folders look like `[Gmail]/All Mail`, `[Gmail]/Sent Mail`, and so on. Call `list_folders` to see them.

> **Security warning.** A Gmail app password grants **full read/write access to the whole mailbox** over IMAP (and SMTP). Google has no read-only app password. Read-only access is enforced **by this code, not by Google**. Anyone or anything else that gets the app password can delete or send mail. Store it only in a secret store or masked credential field, never in a repo, a config file you share, or a chat transcript. Revoke it in your Google Account when you no longer need it.

## Register with Claude Code

Several accounts:

```sh
claude mcp add imap --scope user \
  -e IMAP_MCP_CONFIG=/path/to/accounts.json \
  -- uvx --from git+https://github.com/csharpengineer/imap-mcp imap-mcp
```

A single account:

```sh
claude mcp add imap --scope user \
  -e IMAP_HOST=imap.gmail.com \
  -e IMAP_USER=you@gmail.com \
  -- uvx --from git+https://github.com/csharpengineer/imap-mcp imap-mcp
```

Passwords (`GMAIL_IMAP_PASS`, `WORK_IMAP_PASS`, `IMAP_PASS`) are deliberately left out. Anything passed with `-e` is stored in plain text in `~/.claude.json`. Supply each password as an env var in the environment of the process that launches Claude Code, for example a secret manager or a masked credential store that injects env vars. Use one secret per account. The server inherits that environment.

To check the setup, call `list_accounts`. It shows each account's `password_set` without revealing anything. If a password variable is missing, that account's tools return `account 'work': password env var WORK_IMAP_PASS is not set`, and the other accounts keep working.

## Development

```sh
uv venv && uv pip install -e ".[test]"
.venv/Scripts/python -m pytest -q     # Windows
.venv/bin/python -m pytest -q         # macOS/Linux
```

The tests never touch a real mailbox. `tests/fake_imap.py` is an in-process IMAP server on `127.0.0.1` that speaks real IMAP over a socket. `tests/test_stdio.py` launches the actual server over stdio and calls its tools through the MCP client SDK.
