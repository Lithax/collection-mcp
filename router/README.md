# mcp-router

One MCP server that fronts many. Add it as your single custom connector; it reaches every
server listed in `servers.json`.

## Run
    python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
    python router.py                      # http://127.0.0.1:8000/mcp
    ROUTER_TRANSPORT=stdio python router.py   # for local stdio clients

Env vars: `ROUTER_CONFIG`, `ROUTER_HOST`, `ROUTER_PORT`, and these limits (defaults in brackets):

| variable | meaning |
|---|---|
| `ROUTER_TIMEOUT` (60) | seconds to wait for one upstream call; a call that passes `timeout_seconds` waits longer (see Timeouts) |
| `ROUTER_MAX_TIMEOUT` (900) | ceiling for the above |
| `ROUTER_KEEPALIVE` (15) | seconds between progress pings while a call is slow, 0 = off |
| `ROUTER_MAX_CHARS` (20000) | text per result; the rest is cut |
| `ROUTER_MAX_IMAGE_CHARS` (7000000) | base64 characters per image/audio block (about 5 MB); bigger ones become a text note |
| `ROUTER_SESSION_IDLE` (14400) | seconds of silence after which a chat's upstream session is closed |
| `ROUTER_MAX_SESSIONS` (16) | chat sessions kept open per server (least recently used closed first) |
| `ROUTER_CALL_LOG` (off) | turns the call log on: `on` writes `<repo>/.collab/calls.log`, a path writes that file; `off` (also `0`, `false`, `no`, `none`) or unset leaves it off (see Call log) |
| `ROUTER_CALL_LOG_MAX` (5000000) | bytes after which the log is moved to `<file>.1` and started again |

## Add servers from the command line
Point the CLI at something that's already running; it works out the rest.

    python router.py add 127.0.0.1:9000                  # finds /mcp or /sse, detects http vs sse
    python router.py add https://host/some/path -y       # -y: never prompt, take defaults
    python router.py add --stdio -- npx -y some-pkg /dir # launches it to scan it
    python router.py list
    python router.py remove NAME

What `add` does: probes the endpoint, detects streamable HTTP vs legacy SSE, detects whether
auth is needed, connects, lists the tools, flags destructive-looking ones (using the server's
own `readOnlyHint`/`destructiveHint` annotations plus name heuristics like `delete_*`, `exec`,
`write_*`) and offers to put them in `deny_tools`. It asks only for what it can't infer: a
credential if the server returns 401, a name, a description. Then it shows the entry and asks
before saving (atomic write, previous file kept as `servers.json.bak`).

Useful flags: `--name`, `--description`, `--header 'Name: value'`, `--env KEY=VALUE` (stdio),
`--allow GLOB` / `--deny GLOB`, `--allow-all`, `--disabled`, `--force`, `--dry-run`, `--config PATH`.

**Secrets:** tokens are stored as `${ENV_VAR}` references, never plaintext (unless
`--inline-secret`), and the CLI tells you what to `export` where the router runs. Prefer the
interactive hidden prompt or an already-exported variable
(`--header 'Authorization: Bearer ${MY_TOKEN}'`) over typing a token in a command line, since
that lands in your shell history. Servers that only support OAuth (no static token) aren't
supported yet.

To try it: `python demo_upstream.py http 9000` in one terminal, then
`python router.py add 127.0.0.1:9000` in another.

## Tools
| tool | purpose |
|---|---|
| `list_servers` | configured upstreams |
| `list_tools(server)` | names + one-line descriptions |
| `describe_tool(server, tool)` | full JSON schema |
| `call_tool(server, tool, arguments)` | run it (text and images come back) |
| `restart_server(server)` | drop every cached connection to it / kill a stdio process |
| `forward(server, tools, enable)` | expose upstream tools directly as router tools (see below) |

## Forwarding: call upstream tools directly
By default every upstream call is `call_tool(server, tool, arguments)`, preceded by a
`describe_tool` round trip to learn the schema. *Forwarding* registers chosen upstream tools on
the router under their real names and schemas, so a client calls e.g. `run_command(command=...)`
directly. Only what you forward costs context; the router itself stays at its 6 meta-tools.

**Static**, in `servers.json` (applied when the router starts, and on `forward()` with no arguments):

    "sandbox": {
      "url": "http://127.0.0.1:8767/mcp",
      "expose": ["run_command", "read_file", "write_file", "edit_file", "sandbox_create"],
      "prefix": "sbx_",
      "deny_tools": ["image_*", "sandbox_info", "sandbox_extend"]
    }

`expose` is a list of globs. `prefix` is optional (default none). If a name would clash with a
router tool or another server's tool, it falls back to `<server>_<tool>`. `deny_tools` /
`allow_tools` still win: a denied tool is never forwarded.

**Dynamic**: `forward(server="sandbox", tools=["run_command", "read_*"])` adds them,
`enable=false` removes them again, and `forward()` with no arguments re-syncs from
`servers.json` and lists what is forwarded. Dynamic choices live in memory only (lost on router
restart); tools set by `expose` can't be switched off with `forward`, edit `servers.json`.

**Client support:** the router sends `tools/list_changed` after `forward()` changes anything, but
whether a client picks that up mid-conversation is up to the client. If the new tools don't show up,
reconnect the connector, or use static `expose`, which works with any client. Forwarded tools use
the upstream's description and input schema; upstream output schemas are not forwarded. Results come
back as content blocks, like `call_tool`: text (capped at `ROUTER_MAX_CHARS`) and images/audio
untouched. Tested with fastmcp 4.1.x.

Tests: `python -m pytest tests -q` (in-memory, uses `demo_upstream.py`), and
`python tests/smoke_http.py` (starts the real router over HTTP). The `CancelledError`
traceback the router prints on SIGTERM is not from forwarding; the original code does it too.

## Sessions: one upstream session per chat
Some servers keep state per MCP session (the sandbox's `set_defaults` does). If every chat shared
one upstream session, one chat's defaults would leak into the next. So for a server reached by URL
the router opens a separate upstream session for each downstream client (chat), and closes it after
`ROUTER_SESSION_IDLE` seconds of silence, or when more than `ROUTER_MAX_SESSIONS` are open (least
recently used first). In `servers.json`:

    "sandbox": { "url": "http://127.0.0.1:8767/mcp", "sessions": "auto" }   // auto | per_client | shared

`auto` (the default) and `per_client` isolate chats; `shared` restores the old behavior, one session
for everybody. stdio servers are always shared, since another connection would mean another
process. `list_servers` shows each server's mode.

This needs a client the router can recognize from one request to the next: a stateful connection
(initialize handshake). Over the newer stateless protocol (MCP 2026-07-28) an anonymous HTTP request
carries no identity, so those calls fall back to one shared session and nothing can be isolated.
A server entry can also set `"mode": "legacy"` to make the router itself use the classic handshake
toward that server, which is what servers with per-session state want.

## Images and audio
Results from an upstream tool pass through as MCP content blocks: text (capped, see above), images
and audio untouched, so tools like the sandbox's `view_image` work. A block bigger than
`ROUTER_MAX_IMAGE_CHARS` is replaced by a short text note. Resources and resource links are shown as text.

## Questions from upstream (approvals)
An upstream server can ask its client a question in the middle of a call (MCP elicitation; the
sandbox does this to ask "allow network access?" or "run this risky command?"). The router passes
such a question to the client that made the call, and the answer back, if that client declared
elicitation support and can be asked back. Otherwise the upstream sees "elicitation unsupported"
and refuses, as before. The call waits while the question is open, so answer within its timeout.

## Timeouts
The router waits `ROUTER_TIMEOUT` seconds (60) for an upstream call. A call that passes
`timeout_seconds` (like the sandbox's `run_command`) is waited for that long plus ten seconds, up to
`ROUTER_MAX_TIMEOUT` (900). While a call is slow the router sends the client a progress notification
every `ROUTER_KEEPALIVE` seconds (15), if the client asked for progress; that keeps proxies and clients
with idle timeouts from giving up. Your client or tunnel may still have a limit of its own.

## Call log
Optional, off by default. With `ROUTER_CALL_LOG` set, every tool call the router handles, its own tools and
forwarded ones alike, is appended to one file as one line, so a person can `tail` or `grep` what went
through the gateway. `ROUTER_CALL_LOG=on` writes `.collab/calls.log` in this repo (gitignored); a path
writes that file.

It is a gateway-wide stream, not a project log: the router is deliberately opaque and can't tell which
sandbox or project a call touches, so every chat and every server lands in the same file. For a hand-off
log per project, use the sandbox server's own `[collab] log` (see `sandbox/README.md`).

    2026-11-12T07:31:07Z | 3fa9c1 | sandbox.run_command | command="pytest -q" sandbox=work | ok | 1204ms

- Fields: UTC time, chat, `server.tool`, arguments, outcome, duration. The chat is 6 hex characters made
  from the downstream client's session id, so interleaved chats can be told apart (`-` when the client
  can't be told from another). The log is shared by every chat and server.
- Outcome: `ok`, `exit N` (the sandbox's non-zero `exit code: N`), `ERR <message>` (upstream errors that
  `call_tool` hands back as an ordinary result count too) or `cancelled`.
- Results are never logged. Arguments are cut to about 100 characters per value, file bodies and stdin
  become a size (`content=<1656 chars>`), `env` and `headers` a count, and values under keys like `token`,
  `password` or `api_key`, `KEY=value` secrets in command text, bearer tokens, common token formats and
  `user:password@` in URLs are masked. That is best effort: don't rely on it to hide a secret.
- Past `ROUTER_CALL_LOG_MAX` bytes the file moves to `<file>.1` (one older file is kept). It is created
  private (0600) and opened for every write, so deleting or truncating it while the router runs is fine.
  If it can't be written the router says so once on stderr and carries on; a call never fails because of it.
- The tests switch it off (`tests/conftest.py`) so a test run doesn't write to the real log.

## Notes
- Entries can set `"transport": "sse"` for legacy SSE servers (the CLI does this for you).
- `list_tools` follows pagination, so servers with many tools aren't truncated.
- `servers.json` is re-read on every call: add, edit or disable servers live. Changed entries reconnect automatically.
- `allow_tools` / `deny_tools` (globs) restrict what each upstream exposes. Deny wins.
- `${VAR}` in `env`, `headers`, `args`, `url` expands from the router's environment.
- **Auth:** this router has none. Bind to 127.0.0.1 and put your tunnel's access control in front of it.
  Anyone who can reach it can use every upstream server you configure.
- Upstream output is untrusted data; the router's instructions tell the model to treat it that way.
