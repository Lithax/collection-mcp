# Sandbox via router: usage

You have a disposable Linux sandbox behind an MCP router. The everyday tools are in your tool list: call them by name (if your client lists them as deferred, load them with `tool_search` first). Don't run `list_servers`, `list_tools` or `describe_tool` for anything documented here, and ignore `forward`.

## Start of every session

```
sandbox_list()
sandbox_create(name="work", image="python:3.12-slim")
set_defaults(input={"sandbox":"work","regex":true}, output={"status":false,"labels":false,"json":"compact","diff":"summary","stats":false})
```

1. `sandbox_list()` first. If a `running` sandbox fits (check `mounts`, and `expires_in_seconds` if it is not null), reuse it and skip the create. Servers allow only a few sandboxes at once (`max_sandboxes`, often 2): destroy what you created when you are done, and anything the user asks you to.
2. Create only if nothing fits. Names: a-z, 0-9, `-`. The image needs `python3`, because the file tools run on it: use `python:3.12-slim`. `debian:*` slim images have none, and `sandbox_create` then returns a `warning` saying so. If `expires_in_seconds` comes back as a number, the server has a default lifetime and an expired sandbox is destroyed with its files: pass `ttl_seconds=14400`. If it is `null`, there is no expiry.
3. Don't pass `mounts`: the administrator attaches the project folders to every new sandbox (they show up in `mounts`, usually several, all `rw`), and you cannot request others. Leave `network`, `ports`, `cpus` and `memory_mb` at the defaults unless you must: limits belong to the server (`call_tool(server="sandbox", tool="sandbox_backends")` shows `max_cpus`, `max_memory_mb`, `max_sandboxes` and `defaults.network`).
4. `set_defaults` once per chat. After that omit `sandbox`: `run_command(command="pytest -q")`. An explicit argument always wins. Defaults belong to your chat (the router gives each chat its own upstream session), so `no sandbox specified` means they were reset: run `set_defaults` again. If `show_defaults` lists a sandbox you didn't set, you are on an older router where chats share defaults: pass `sandbox="..."` and `session="..."` explicitly on every call.

What the defaults do: `status:false` hides the routine `exit code: 0` line (failures, timeouts and "(no output)" are still shown); `labels:false` drops `--- stdout ---`; `diff:"summary"` makes `edit_file` return `+N -M lines` (use `dry_run=true` to preview a diff); `stats:false` drops grep's footer; `regex:true` makes `grep` patterns regexes (the tool's own default is plain text); `json:"compact"` shrinks JSON results.

## Direct tools (call by name)

| Tool | Notes |
|---|---|
| `run_command(command, cwd?, timeout_seconds?, stdin?, session?)` | bash. `cd` and exported variables persist between calls (per `session`, default `"default"`); functions, aliases, unexported variables and background jobs do not. Default timeout 60 s; see "Limits" for longer. |
| `read_file(path, start_line?, max_lines?)` | Page big files with `start_line` / `max_lines`. |
| `read_multiple_files(paths)` | Up to 20 small files in one call. |
| `write_file(path, content, overwrite=false)` | Creates parent dirs. Refuses to replace an existing file unless `overwrite=true`. For a big file: `write_file` the first chunk, then `append_file`. |
| `append_file(path, content)` | |
| `edit_file(path, edits=[{old_text, new_text, replace_all?}], dry_run?)` | `old_text` must match exactly once unless `replace_all`. Prefer this to rewriting a file. |
| `grep(pattern, path=".", file_glob="*", regex?, case_sensitive?, max_matches=100)` | Content search; skips binaries and protected paths. Escape literals like `(` while `regex` is on. |
| `search_files(pattern, path=".", max_results=100)` | Find by name: a glob (`*.py`) or a substring. |
| `list_directory(path=".", details?)` / `directory_tree(path=".", max_depth=3)` | |
| `view_image(path)` | Returns the image itself (png, jpeg, gif, webp). If you only get text like `[image: image/png, ~N base64 chars omitted]`, the router is an old one without image passthrough: tell the user to update and restart it. |
| `transfer_files(source_path, destination_path, source_sandbox?, destination_sandbox?, overwrite?)` | Copy between sandboxes, or host to sandbox when the host target is enabled (`"host"`). `destination_path` is the full target path; the source name is not appended. Both sandbox args default to your default sandbox. |
| `sandbox_list()`, `sandbox_create(...)`, `set_defaults(...)`, `show_defaults()` | See above. |

Relative paths resolve against the sandbox workdir (`/workspace` by default). File tools and `cwd=` only reach the workdir, `/tmp` and mapped folders.

## Everything else: `call_tool`

```
call_tool(server="sandbox", tool="sandbox_extend", arguments={"seconds": 3600})
```

Your default fills `name` / `sandbox` here too. If a call fails with `Unknown tool: X`, X is not direct: use `call_tool`.

| `tool` | `arguments` | Use |
|---|---|---|
| `sandbox_extend` | `seconds`, `name?` | Extend the lifetime before `expires_in_seconds` runs out (only if it is not null). |
| `sandbox_start` / `sandbox_stop` | `name?` | Start after "sandbox is stopped". Files are kept. |
| `sandbox_info` | `name?` | Live usage and IP. |
| `sandbox_destroy` | `name` (always explicit) | Only for a sandbox you created, when finished or asked. Mapped host folders are not deleted. |
| `sandbox_backends` | none | The server's limits and defaults (network, cpus, memory, max sandboxes). |
| `check_command` | `command`, `cwd?` | Dry-run: would it run, need approval, or be blocked? |
| `image_list`, `image_pull`, `image_remove` | `image` (pull/remove), `backend?` | `backend` defaults to the server's. On an older sandbox server it is required: pass `backend="docker"`. |
| `shell_info`, `reset_shell_session`, `list_allowed_directories` | `session?` / none | Shell policy and limits; clear a session's cwd/env; readable roots. |
| `file_info`, `move_path`, `copy_path`, `create_directory`, `delete_path` | `path` etc. | Usually simpler as `mv`, `cp`, `mkdir -p`, `rm` in `run_command`. |

Need the exact schema of one of these? `describe_tool(server="sandbox", tool="...")`. Call it before the first use of a tool you haven't used: arguments are not validated until the sandbox sees them (for example `image_list` once failed with a missing `backend`).

## Limits that will bite

- **Text results are cut at ~20,000 characters** by the router (`…[truncated, N more chars]`). Read files in pieces (`start_line`, `max_lines` about 200) and trim command output (`| head -50`, `| tail -30`, `grep`). Images are separate and pass through up to about 5 MB.
- **Time.** The router waits 60 s for a call. `run_command(timeout_seconds=N)` is waited for up to N+10 s (router ceiling: 900), but the user's client or tunnel can have a shorter limit of its own, so keep calls short. A call that times out may still be running in the sandbox: check before re-running it.
- **No detaching.** Background and detached jobs (`&`, `nohup`, `setsid`) need human approval. Through the router that only works when the client can answer questions; otherwise the call is `REFUSED: ... needs human approval but the client could not ask`. Do not retry or disguise it. Run things in the foreground, in steps that each finish within the time limit (for example install packages in two or three calls).
- **Don't write and run in one command.** A command line that creates a script and executes it is refused for the same reason (the code was never visible to analysis). `write_file` first, then run it in the next call.
- **Network is whatever the server defaults to** (`sandbox_backends` → `defaults.network`). With `outbound`, `pip`, `apt-get`, `git` and `curl` work. With `none` they are BLOCKED: don't retry or work around it, use what the image has, or tell the user. Network, ports and extra mounts requested at create time need a human's approval; that works only when the client can answer questions.
- **File tools need `python3` in the image.** On "no python3" errors, destroy and recreate the sandbox with `image="python:3.12-slim"`. `run_command` works regardless.
- **Commands are classified before they run.** `sudo` / `su` are blocked and risky ones are refused with a reason. Change the approach; never disguise the command.
- **A `rw` entry in `mounts` is the user's real files.** Don't delete or overwrite there without asking, and only touch the project the user named: the other mappings are other projects.

## Working in a mounted project

The sandbox runs as root, so anything it creates in an `rw` mount is **root-owned on the host**, and git run as root there changes ownership inside `.git`.

- **Develop and test in a copy inside the sandbox**, not in the mount: `mkdir -p /workspace/build && cd /workspace/<project> && tar --exclude=.git --exclude=.github -cf - . | tar -xf - -C /workspace/build`. Run tests with `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`. Then copy only the finished files into the mount and give them back to the host owner (`ls -ln` shows the uid:gid; `chown` the files you wrote, and `.git` after committing).
- **git is not in `python:3.12-slim`.** With outbound network: `apt-get update -qq`, then `apt-get install -y -qq --no-install-recommends git` (two calls, each under 60 s). Run `git config --global --add safe.directory /workspace/<project>`, and use `GIT_OPTIONAL_LOCKS=0` for read-only git (a plain `git status` rewrites `.git/index` as root).
- **Stage explicit paths, never `git add -A`.** The user may have uncommitted changes of their own, and gitignored local files (`USAGE.md`, tokens, `servers.json`, `config.toml`) that must not be force-added.
- **Credentials.** If the user points you at a token file, never print it, never put it in a remote URL or in `.git/config`. Let a one-off `GIT_ASKPASS` script read it for the single push.
- A venv outside the mount keeps installs out of the user's folder: `python3 -m venv /workspace/venv` (installing packages needs outbound network).
- Check for .github/ for the github repo ACL token, if not there, ask user
- author and commit as claude noreply@anthropic.com for github (depending on who did the work, you can also add me 173831442+Lithax@users.noreply.github.com as author/co author in the given case)

## Defaults and shell sessions

Defaults live on the router's upstream session for your chat. Shell sessions (cwd and env) belong to the sandbox: everyone using the same sandbox and session name shares them, so use your own `session="..."` name if another chat may be working in the same sandbox. If calls fail with connection errors, `restart_server(server="sandbox")` reconnects (it drops every chat's connection); then run `set_defaults` again.

## Efficiency

- Skip discovery. Chain steps in one call: `cd src && pytest -q 2>&1 | tail -30`. `cd` once; the session remembers.
- `read_multiple_files` over several `read_file`s; `grep(file_glob="*.py")` over reading trees; `edit_file` over rewriting.
- Pass only the arguments you need. Errors name what is valid: fix the argument rather than switching tools.
