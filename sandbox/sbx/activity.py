"""A per-project hand-off log: one line per file/shell call, written into the project's own `.collab/` folder.

Agents take over from each other (a chat ends, a sandbox is recreated). The plan and notes they leave live in
`<project>/.collab/`; this adds what they can't be relied on to write themselves: a record of what was actually
done. Off unless `[collab] log = true`.

A call is attributed to every writable mapping it touches: the ones containing its path (file tools) or the
directory a shell command runs in and the absolute paths its text names. Each gets a line in
`<that mapping's host folder>/.collab/calls.log`. `<parent>/build-<project>`, the scratch copy agents develop in
because the mapping itself is the user's real files, counts as `<project>`. Calls inside `.collab` itself (the
agents' own notes) leave no line. A call that touches no writable mapping at all leaves no line either, unless
`[collab] fallback` names a writable mapping: then it goes to that one. The server writes on the host as its own
user, so the files belong to the host user, and the model never learns a host path.

    2026-11-12T19:50:03Z | 3fa9c1 | run_command@work | cwd=router command="pytest -q" | exit 1 | 21004ms

Fields: UTC time | chat (6 hex per live MCP session, best effort) | tool@sandbox | arguments | outcome | duration.
Paths are shown relative to the mapping, or as absolute sandbox paths when they lie outside it (a build copy, /tmp).
Results are never logged. Commands go through the server's output
redactor; the file tools already pass sizes instead of file bodies.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import posixpath
import re
import secrets
import sys
import weakref
from pathlib import Path
from typing import Any, Callable, Iterable

COLLAB_DIR = ".collab"
LOG_NAME = "calls.log"
BUILD_PREFIX = "build-"  # <parent>/build-<project> is the scratch copy of the mapping <parent>/<project>
ARG_LIMIT = 100    # characters kept of one argument value
ERR_LIMIT = 140    # characters kept of an error message
LINE_LIMIT = 500   # characters per line; the arguments are cut to fit
_SCAN = 2000       # characters of a value handed to the redactor
_PATH_KEYS = ("path", "paths", "source", "destination", "source_path", "destination_path")
_BARE = re.compile(r"[\w./:@+,~-]+")  # printed without quotes
_CMD_SCAN = 20000  # characters of a shell command searched for paths
_CMD_PATHS = 40    # paths taken from one shell command
_ABS = re.compile(r"(?<![\w./~:-])/[\w.+@%,=~:-]*(?:/[\w.+@%,=~:-]*)*")  # an absolute path inside a command line
_CD = re.compile(r"""^\s*cd\s+(?:--\s+)?(?:"([^"$`\\]+)"|'([^']+)'|([^\s;&|<>()$`\\"'*?\[\]{}~]+))\s*(?:&&|;|$)""")


# ------------------------------------------------------------------------------------------ which project
def guest_path(workdir: str, path: str) -> str:
    """An absolute, normalised guest path (relative ones resolve against the sandbox workdir)."""
    return posixpath.normpath(path if path.startswith("/") else posixpath.join(workdir, path))


def _roots(mounts: Iterable[Any]) -> list[tuple[str, Any, bool]]:
    """(guest path, mapping, is_build_copy) for every place that belongs to a mapping: the mapping itself and its
    `<parent>/build-<folder name>` / `<parent>/build-<mapping name>` scratch copy."""
    out: list[tuple[str, Any, bool]] = []
    for m in mounts:
        g = posixpath.normpath(m.guest)
        out.append((g, m, False))
        for n in sorted({posixpath.basename(g), getattr(m, "name", "")} - {""}):
            out.append((posixpath.join(posixpath.dirname(g), BUILD_PREFIX + n), m, True))
    return out


def classify(mounts: Iterable[Any], workdir: str, path: str):
    """Where a sandbox path belongs: `("mount", mapping, path relative to its root)` inside a writable mapping or its
    build copy; `("notes",)` inside a .collab folder (the agents' own notes); `("outside",)` anywhere else, read-only
    mappings included. The innermost root wins (a real mapping beats a build copy of the same depth), so a read-only
    mapping nested in a writable one is respected."""
    p = guest_path(workdir, path)
    best = None
    for root, m, alias in _roots(mounts):
        if p == root or p.startswith(root.rstrip("/") + "/"):
            if best is None or (len(root), not alias) > (len(best[0]), not best[2]):
                best = (root, m, alias)
    if best is None or not best[1].rw:
        return ("outside",)
    rel = posixpath.relpath(p, best[0])
    if rel == COLLAB_DIR or rel.startswith(COLLAB_DIR + "/"):
        return ("notes",)
    return ("mount", best[1], rel)


def mount_for(mounts: Iterable[Any], workdir: str, path: str):
    """(mapping, path relative to its root) if `path` is in a writable mapping (or its build copy) and not inside its
    .collab folder; otherwise None."""
    c = classify(mounts, workdir, path)
    return (c[1], c[2]) if c[0] == "mount" else None


def paths_of(args: dict[str, Any]) -> list[str]:
    """The guest paths a file-tool call names, in the order they should be tried."""
    out: list[str] = []
    for k in _PATH_KEYS:
        v = args.get(k)
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, (list, tuple)):
            out.extend(x for x in v if isinstance(x, str))
    return out


def shell_paths(cwd: str | None, command: str) -> list[str]:
    """The paths a shell call touches, best effort: where it effectively runs (the directory of a leading `cd`, else the
    directory it started in), then every absolute path its text names. Relative paths after a later `cd`, variables
    and globs are not followed."""
    text = command[:_CMD_SCAN]
    run_in = cwd
    m = _CD.match(text)
    if m:
        run_in = posixpath.normpath(posixpath.join(cwd or ".", next(g for g in m.groups() if g)))
    out = [run_in] if run_in else []
    for tok in _ABS.findall(text):
        out.append(posixpath.normpath(tok))
    seen: set[str] = set()
    return [p for p in out if not (p in seen or seen.add(p))][:_CMD_PATHS]


# ------------------------------------------------------------------------------------------ the line
def _val(v: Any, scrub: Callable[[str], str]) -> str:
    if v is None or isinstance(v, (bool, int, float)):
        return json.dumps(v)
    if isinstance(v, str):
        n = len(v)
        s = scrub(v[:_SCAN])
        if n > ARG_LIMIT or len(s) > ARG_LIMIT:
            s = f"{s[:ARG_LIMIT]}…(+{max(n - ARG_LIMIT, 1)})"
        return s if _BARE.fullmatch(s) else json.dumps(s, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        blob = json.dumps([scrub(x[:_SCAN]) if isinstance(x, str) else x for x in v],
                          ensure_ascii=False, separators=(",", ":"), default=str)
        return blob if len(blob) <= ARG_LIMIT else f"<{len(v)} items>"
    return f"<{type(v).__name__}>"


def summarize(args: dict[str, Any], rel: Callable[[str], str], scrub: Callable[[str], str]) -> str:
    """key=value pairs on one line: paths made relative to the project, text redacted and cut."""
    parts = []
    for k, v in args.items():
        if k in _PATH_KEYS or k == "cwd":
            v = [rel(x) if isinstance(x, str) else x for x in v] if isinstance(v, (list, tuple)) \
                else rel(v) if isinstance(v, str) else v
        parts.append(f"{k}={_val(v, scrub)}")
    return " ".join(parts) or "-"


def error_outcome(message: str | None) -> str:
    return "ok" if message is None else "ERR " + (" ".join(message.split())[:ERR_LIMIT] or "error")


def shell_outcome(outcome: str, exit_code: int | None, timed_out: bool | None) -> str:
    """The audit's outcome word plus the exit status, as one short phrase."""
    if outcome in ("ran", "ran_after_approval"):
        if timed_out:
            return "timeout"
        return "ok" if exit_code == 0 else "ERR no exit status" if exit_code is None else f"exit {exit_code}"
    return "ERR " + outcome.replace("_", " ")


def format_line(chat: str, tool: str, sandbox: str, args: str, outcome: str, ms: float,
                now: dt.datetime | None = None) -> str:
    """One log line, always a single line, at most about LINE_LIMIT characters."""
    ts = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    target = re.sub(r"[^\w.@:-]", "?", f"{tool}@{sandbox}")[:80]
    head, tail = f"{ts} | {chat} | {target} | ", f" | {' '.join(outcome.split())} | {int(ms)}ms"
    room = max(LINE_LIMIT - len(head) - len(tail), 40)
    args = " ".join(args.split())
    if len(args) > room:
        args = args[: room - 1] + "…"
    return head + args + tail


# ------------------------------------------------------------------------------------------ the writer
class ActivityLog:
    """Appends hand-off lines to the right project. Never raises: a log that can't be written must not break a
    call (a failure is reported on stderr once per folder)."""

    def __init__(self, cfg: Any, scrub: Callable[[str], str] = lambda s: s) -> None:
        self.enabled = bool(cfg.log)
        self.max_bytes = int(cfg.max_bytes)
        self.fallback = str(getattr(cfg, "fallback", "") or "").strip()   # name of the [mounts.*] that gets the rest
        self.scrub = scrub
        self._tags: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()
        self._warned: set[str] = set()

    def tag(self, session: Any) -> str:
        """A short stable label for an MCP session (one per chat), '-' when there is none to label."""
        if session is None:
            return "-"
        try:
            return self._tags.setdefault(session, secrets.token_hex(3))
        except TypeError:  # not weak-referenceable
            return "-"

    def note(self, mounts: Iterable[Any], workdir: str, *, tool: str, sandbox: str, args: dict[str, Any],
             paths: list[str], outcome: str, ms: float, session: Any = None,
             now: dt.datetime | None = None) -> bool:
        """Append a line for the call to every writable mapping it touched. A call that touched none, and nothing in a
        .collab folder, goes to the fallback mapping if one is configured. Returns whether a line was written."""
        if not self.enabled:
            return False
        try:
            mounts = list(mounts)
            targets: dict[str, Any] = {}      # guest path -> mapping, in the order first touched
            notes = outside = False
            for p in paths:
                if not p:
                    continue
                c = classify(mounts, workdir, p)
                if c[0] == "mount":
                    targets.setdefault(posixpath.normpath(c[1].guest), c[1])
                elif c[0] == "notes":
                    notes = True
                else:
                    outside = True
            if not targets and outside and not notes and self.fallback:
                fb = next((m for m in mounts if m.name == self.fallback and m.rw), None)
                if fb is not None:
                    targets[posixpath.normpath(fb.guest)] = fb
            wrote = False
            for base, mount in targets.items():
                def rel(p: str, base: str = base) -> str:
                    gp = guest_path(workdir, p)
                    if gp == base:
                        return "."
                    return posixpath.relpath(gp, base) if gp.startswith(base.rstrip("/") + "/") else gp

                line = format_line(self.tag(session), tool, sandbox, summarize(args, rel, self.scrub), outcome, ms, now)
                wrote = self._append(Path(mount.host), line) or wrote
            return wrote
        except Exception as e:  # noqa: BLE001
            self._warn("activity", e)
            return False

    def prepare(self, mounts: Iterable[Any]) -> None:
        """Create each writable mapping's .collab folder up front, as the server's own user. If the first thing an
        agent does is create it from inside the sandbox it is owned by root, and the server could not write its log
        there. Called when a sandbox is created or started."""
        if not self.enabled:
            return
        for m in mounts:
            if getattr(m, "rw", False):
                try:
                    self._ensure_dir(Path(m.host) / COLLAB_DIR)
                except OSError as e:
                    self._warn(str(Path(m.host) / COLLAB_DIR), e)

    def _ensure_dir(self, d: Path) -> None:
        created = not d.exists()
        d.mkdir(exist_ok=True)
        if created:
            (d / ".gitignore").write_text("*\n")  # git ignores this whole folder, this file included

    def _append(self, root: Path, line: str) -> bool:
        d = root / COLLAB_DIR
        try:
            self._ensure_dir(d)
            path = d / LOG_NAME
            try:
                if path.stat().st_size >= self.max_bytes:
                    os.replace(path, d / (LOG_NAME + ".1"))
            except FileNotFoundError:
                pass
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return True
        except OSError as e:
            self._warn(str(d), e)
            return False

    def _warn(self, where: str, e: BaseException) -> None:
        if where not in self._warned:
            self._warned.add(where)
            print(f"[sandbox] collab log {where}: {e}", file=sys.stderr, flush=True)
