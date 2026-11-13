# Handing work over between instances

Read this at the start of any multi-step task, and whenever you pick up work somebody else started. Everything here uses plain files in a gitignored folder inside the project's mount, plus the shell you already have.

Why files in the project and not somewhere else: they survive the sandbox, they need no extra tool, and they follow the project's permissions: if the user removes the project's mount, its notes go with it.

## The `.collab/` folder

`<project>/.collab/` at the root of the project you were asked to work on (for example `/workspace/myproject/.collab/`). One per project; don't write into other mappings. Without a writable mapping for the project there is nothing that survives, so say so to the user instead of pretending to hand off.

| File | What | Written by | Lifetime |
|---|---|---|---|
| `plan.md` | steps with a status each | you | until the work is done |
| `scratchpad.md` | shared notes and knowledge | you | until the work is done |
| `setup-sandbox.sh` | rebuilds the sandbox environment | you | kept: it describes the sandbox, not one task |
| `calls.log` | what was actually done, one line per call | the sandbox server, if enabled | rotates by itself |

**Making the folder.** If the administrator enabled the sandbox's `[collab] log`, `.collab/` (with a `.gitignore` containing `*`) already exists when the sandbox starts, owned by the host user, and you do nothing. Otherwise create it yourself, once:

```
mkdir -p .collab && printf '*\n' > .collab/.gitignore && chown -R "$(stat -c %u:%g .)" .collab
```

The `*` makes git ignore the whole folder, that file included, so the project's own `.gitignore` stays untouched and nothing here can end up in a commit. The `chown` gives the folder to the host owner (the sandbox runs as root); repeat it for files you add when you finish, since `edit_file` and `write_file` replace files as root.

Never put tokens or other secrets in a `.collab/` file: it is plain text on the host's disk.

## Picking up (start of a session)

`cd` into the project once, then read the state:

```
ls -l --time-style=+%FT%TZ .collab; date -u +%FT%TZ
cat .collab/plan.md .collab/scratchpad.md
tail -n 30 .collab/calls.log
GIT_OPTIONAL_LOCKS=0 git status --short; git log --oneline -5
```

That is a starting point, not a recipe. Read as much as the situation needs: `tail -n 5` if you only want the last thing that happened, `tail -n 100` after a long run. When you look for something specific, search instead of reading everything: `grep -n -i sqlite .collab/scratchpad.md`, `grep -n -E 'WIP|blocked' .collab/plan.md`, `grep -E ' ERR | exit [1-9]' .collab/calls.log | tail -n 10` for the latest failures, `grep 'router.py' .collab/calls.log | tail` for what touched a file. Never `cat` the whole call log: it can be megabytes and output is cut at about 20,000 characters.

The modification times are the "last updated" stamps: a step marked `WIP` in a plan nobody has touched for hours, with a quiet log, was abandoned.

Then decide:

- **No `.collab/` or no `plan.md`:** fresh start, write the plan.
- **Plan with every step `done`:** leftovers of finished work. Reset (below), then start the new request.
- **Plan with open steps that fit the user's request:** resume. A step marked `WIP` was cut off and may be half applied: check `git status`, `git diff`, the scratchpad and the end of the log, finish or redo it, and only then mark it `done`.
- **Plan with open steps, but the user asks for something unrelated:** ask the user whether to continue the old plan or drop it. Don't delete an unfinished plan on your own.
- Notes can be out of date: re-check the cheap facts (`git log`, does the file still exist) before building on them.
- A killed instance can leave `.git/index.lock` behind. If git complains about it and `pgrep git` shows nothing running, remove that one lock file.
- The sandbox itself may be stopped or gone; see "Rebuilding the sandbox" below.

## plan.md

Write it before the first change, only if none exists. Steps must be small: one action, a few minutes, easy to verify ("add the `--log` flag in `router_cli.py`", "run the router tests"), not "implement logging".

```
# Plan: <goal in one line>
Request: <what the user asked, close to their words>

| # | Step | Status | Notes |
|---|------|--------|-------|
| 1 | Read the call path in router.py | done | see scratchpad |
| 2 | Add the log helper | WIP | |
| 3 | Run the router tests | not started | |
| 4 | Commit "router: log calls" | not started | |
| 5 | Reset .collab | not started | |
```

- Statuses: `not started`, `WIP`, `done`, `blocked` (say why in Notes).
- Set `WIP` before you start a step and `done` right after it checks out, each as one small `edit_file`; change two rows in one call when one step ends as the next begins. Do it immediately: you can be stopped at any moment. One `WIP` at a time.
- Steps you discover go into the table at once.
- Put the `commit` steps and a final reset step into the plan.
- Several unrelated requests (say two features): one `##` block each in the same plan, each with its own commit and reset step.
- There is no `Updated:` line on purpose: the file's modification time says it, and it can't be guessed wrong.

## scratchpad.md

What a new instance needs to continue without asking anyone. Append as soon as you learn something, as short bullets:

```
# Scratchpad
## Goal
## Decisions (and why)
## Findings (paths, behaviour, commands that work, gotchas)
## Files touched (stage exactly these)
## Dead ends (tried, did not work)
## Open questions (asked the user? answer?)
```

Keep it short (under about 100 lines; tidy it when it grows). "Files touched" matters: the user may have uncommitted changes of their own, so the next instance stages only what is listed there.

## calls.log

When the administrator enabled `[collab] log` on the sandbox server, every file or shell call that touches this project is appended to `.collab/calls.log` by the server, reads included. You never write to it. It answers "what did the previous instance actually do?":

```
2026-11-12T19:50:03Z | 3fa9c1 | run_command@work | cwd=router command="pytest -q" | exit 1 | 21004ms
2026-11-12T19:50:41Z | 3fa9c1 | edit_file@work | path=router/router.py edits=<2 items> | ok | 12ms
```

Fields: UTC time, chat tag, `tool@sandbox`, arguments (paths relative to the project), outcome (`ok`, `exit N`, `timeout`, `ERR message`), duration. Results and file contents are never logged. The chat tag is the same for one live session and differs between chats, so it separates two agents working in the same project; it is best effort and can change when the connection is re-established.

It lives in the project, so it only holds calls that touched this project; calls in other projects, outside any mapping, or inside `.collab/` itself are not in it. If the file doesn't exist, the log is not enabled: rely on the plan, the scratchpad and `git diff`, and mention it to the user only if you needed it.

## Commits

Commit whenever the tree is in a fully working state: it runs, the tests you can run pass, the change is complete in itself. Plan a `commit` step at those points and note the hash in its Notes cell.

- **Several unrelated requests:** one commit per finished request, never one commit mixing them.
- **One feature that only works when finished:** no commit in a broken middle state; commit when it works. If it has parts that work alone (a helper with its tests, say), commit those as they land.
- **Interrupted with uncommitted work:** the next instance decides from `git diff`, the plan and the scratchpad whether to finish and commit or to redo it. Don't commit a state nobody has checked.
- Everything under "Working in a mounted project" in `SKILL.md` still applies: explicit paths (the list in the scratchpad), author, `chown`, never `git add -A`. Push only if the user asked.

## Reset

Plan and scratchpad are working state, not documentation: don't leave them lying around. When a piece of work is **done, working, tested and committed** (or the user dropped it):

```
grep -E '\| (not started|WIP|blocked) \|' .collab/plan.md     # any output = something is still open
```

- **One request among several:** remove its block from `plan.md` and its notes from `scratchpad.md` and go on with the next one. Carry over only facts that are still true and useful for it.
- **The last or only work:** `rm -f .collab/plan.md .collab/scratchpad.md`, then `chown -R "$(stat -c %u:%g .)" .collab`. Keep `setup-sandbox.sh`. Leave `calls.log` alone: the server rotates it, and another chat may be working in the same project. Empty it (`: > .collab/calls.log`) only if you are sure nobody is.
- **Before deleting:** every step is `done` (no `WIP`, `not started` or unexplained `blocked`), `git status` shows none of your files uncommitted, and nothing in the notes is something the user still needs. If it is, put it in the commit message or the project's docs, not in `.collab/`.
- **Never reset because you ran out of time or context:** that is when the next instance needs the files most. Leave them and say so in your final message.

## Rebuilding the sandbox (`setup-sandbox.sh`)

A recreated sandbox has no git, no packages, no venv. `.collab/setup-sandbox.sh` brings a fresh sandbox back to the state the work needs, so nobody rediscovers and reinstalls by hand.

- Create it the first time you install or configure anything by hand, and add each further step in the same moment, so it never lags behind the sandbox.
- **Idempotent:** a re-run must be safe, so check before acting (`command -v git`, `[ -d /workspace/venv-<project> ]`). Print `==> step name` before each step; if a run times out, the next one continues where it stopped.
- Installs and config only: apt packages, git `safe.directory` and identity, a venv outside the mount, pip/npm installs. No secrets (tokens come from the token file at push time), no background jobs, nothing that touches other projects.
- Ends by touching `/workspace/.ready-<project>`: a marker on the sandbox's own disk that disappears with the sandbox. A new instance checks it and runs the script when the marker is missing or older than the script:

```
[ /workspace/.ready-<project> -nt .collab/setup-sandbox.sh ] || bash .collab/setup-sandbox.sh
```

- `write_file` the script first and run it in the next call (never both in one command). Use `timeout_seconds=300`.
- With network `none` the installs are blocked: don't work around it, note it in the scratchpad and tell the user.

Skeleton to adapt:

```bash
#!/usr/bin/env bash
# Rebuilds the sandbox environment for <project>. Idempotent: safe to run again.
set -euo pipefail
PROJECT=myproject   # the project's folder name under /workspace

echo "==> git"
command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq --no-install-recommends git; }
git config --global --get-all safe.directory | grep -qx "/workspace/$PROJECT" \
  || git config --global --add safe.directory "/workspace/$PROJECT"
git config --global user.name claude
git config --global user.email noreply@anthropic.com

echo "==> python venv"
[ -d "/workspace/venv-$PROJECT" ] || python3 -m venv "/workspace/venv-$PROJECT"
"/workspace/venv-$PROJECT/bin/pip" install -q -r "/workspace/$PROJECT/requirements.txt"   # example

touch "/workspace/.ready-$PROJECT"
echo "==> done"
```
