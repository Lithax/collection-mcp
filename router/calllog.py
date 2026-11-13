"""A one-line-per-call log of what the router was asked to do.

Pure helpers with no fastmcp import (the router's middleware hands them plain values), so they can
be tested on their own. Off unless ROUTER_CALL_LOG turns it on, see README.md.

    2026-11-11T20:31:07Z | 3fa9c1 | sandbox.run_command | command="pytest -q" sandbox=work | ok | 1204ms

Fields: UTC time | chat (6 hex of the downstream client's id, '-' if unknown) | server.tool |
arguments | outcome (ok, exit N, ERR message, cancelled) | duration.

Results are never logged, only whether the call worked. Arguments are shortened, big bodies
(file contents, stdin) are reduced to a size and secrets are masked, but the masking is
best effort: don't send secrets in arguments and count on this to hide them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 5_000_000  # past this the log moves to <file>.1 and starts again
ARG_LIMIT = 100  # characters kept of one argument value
ERR_LIMIT = 140  # characters kept of an error message
LINE_LIMIT = 500  # characters per line; the arguments are cut to fit
_SCAN = 2000  # characters of a value looked at for secrets (keeps the patterns fast)

_OFF = {"off", "0", "false", "no", "none"}
_ON = {"on", "1", "true", "yes"}
_BODY_KEYS = frozenset({"content", "file_text", "stdin", "text", "old_text", "new_text", "data", "body"})
_COUNT_KEYS = frozenset({"env", "headers"})  # how many entries, never which or what
_SECRET_NAME = r"(?:token|secret|passw(?:or)?d|api[_-]?key|authorization|credential|private[_-]?key|cookie)"
_SECRET_KEY = re.compile(_SECRET_NAME, re.I)
_SCRUBS = [
    (re.compile(rf"([\w.-]*{_SECRET_NAME}[\w.-]*)(\s*[=:]\s*)(?:bearer\s+)?(\"[^\"]*\"|'[^']*'|\S+)", re.I), r"\1\2***"),
    (re.compile(r"\bbearer\s+[\w.~+/=-]{8,}", re.I), "Bearer ***"),
    (re.compile(r"\b(?:gh[pousr]_|github_pat_|sk-|xox[abprs]-)[\w-]{16,}"), "***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),  # user:password@host
]
_BARE = re.compile(r"[\w./:@+,~-]+")  # printed without quotes
_EXIT = re.compile(r"exit code:\s*(-?\d+)")


def scrub(text: str) -> str:
    """Mask things that look like secrets (best effort)."""
    for pattern, repl in _SCRUBS:
        text = pattern.sub(repl, text)
    return text


def _shorten(text: str, limit: int) -> str:
    n = len(text)
    text = scrub(text[:_SCAN])
    if n <= limit and len(text) <= limit:
        return text
    return f"{text[:limit]}…(+{max(n - limit, 1)})"


def _redact(value: Any) -> Any:
    """A copy of a nested value that is safe to print: secrets masked, bodies reduced to a size."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _SECRET_KEY.search(str(k)):
                out[k] = "***"
            elif str(k) in _BODY_KEYS and isinstance(v, str):
                out[k] = f"<{len(v)} chars>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return scrub(value[:_SCAN])
    return value


def _fmt(key: str, value: Any) -> str:
    if _SECRET_KEY.search(key):
        return "***"
    if key in _COUNT_KEYS and isinstance(value, dict):
        return f"<{len(value)} entries>"
    if isinstance(value, str):
        if key in _BODY_KEYS:
            return f"<{len(value)} chars>"
        text = _shorten(value, ARG_LIMIT)
        return text if _BARE.fullmatch(text) else json.dumps(text, ensure_ascii=False)
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    try:
        blob = json.dumps(_redact(value), ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        blob = repr(value)
    if len(blob) <= ARG_LIMIT:
        return blob
    unit = "keys" if isinstance(value, dict) else "items"
    size = len(value) if hasattr(value, "__len__") else 0
    return f"<{size} {unit}>"


def summarize_args(arguments: Any) -> str:
    """The arguments of a call on one line: key=value pairs, long values cut, bodies as <N chars>."""
    if not isinstance(arguments, dict) or not arguments:
        return "-"
    return " ".join(f"{k}={_fmt(str(k), v)}" for k, v in arguments.items())


def error(message: str) -> str:
    return "ERR " + _shorten(" ".join(message.split()) or "error", ERR_LIMIT)


def outcome(is_error: bool, text: str | None) -> str:
    """ok, 'exit N' (the sandbox's 'exit code: N' line, N != 0) or 'ERR ...', from a call's first text."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    # call_tool hands upstream errors back as an ordinary result starting '[error from server.tool]'
    if is_error or (lines and lines[0].startswith("[error from ")):
        return error(" ".join(lines[:2]))
    m = _EXIT.match(lines[0]) if lines else None
    if m and int(m.group(1)) != 0:
        return f"exit {m.group(1)}"
    return "ok"


def chat_tag(scope: str | None) -> str:
    """A short stable label for the downstream client (chat), so interleaved chats can be told apart."""
    return "-" if not scope else hashlib.sha1(str(scope).encode()).hexdigest()[:6]


def format_line(
    target: str, args: str, result: str, ms: int, chat: str = "-", now: dt.datetime | None = None
) -> str:
    """One log line, always a single line, at most about LINE_LIMIT characters."""
    ts = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    target = re.sub(r"[^\w.:-]", "?", target)[:80]
    result = " ".join(result.split())
    head, tail = f"{ts} | {chat} | {target} | ", f" | {result} | {ms}ms"
    room = max(LINE_LIMIT - len(head) - len(tail), 40)
    args = " ".join(args.split())
    if len(args) > room:
        args = args[: room - 1] + "…"
    return head + args + tail


def resolve_path(value: str | None, default: Path) -> Path | None:
    """ROUTER_CALL_LOG: unset, empty or off/0/false/no/none = no log; on/1/true/yes = the default path;
    anything else = a file path."""
    value = (value or "").strip()
    if not value or value.lower() in _OFF:
        return None
    if value.lower() in _ON:
        return default
    return Path(os.path.expanduser(value)).absolute()


class CallLog:
    """Appends lines to a file. Opens it per write (so deleting or truncating it from outside is fine),
    keeps it private (0600) and never raises: a log that can't be written must not break a call."""

    def __init__(self, path: Path | str | None, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path = Path(path) if path else None
        self.max_bytes = max_bytes
        self._warned = False

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def write(self, line: str) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.path.stat().st_size >= self.max_bytes:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            except FileNotFoundError:
                pass
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            if not self._warned:  # say it once, not on every call
                self._warned = True
                print(f"[router] call log {self.path}: {e}", file=sys.stderr, flush=True)
