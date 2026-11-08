"""Defaults: say it once instead of on every call.

Two kinds, both set with the `set_defaults` tool (an administrator can pre-set them under [defaults]):

  input   argument values used when a call omits them          {"sandbox": "work", "session": "dev"}
  output  what results contain                                  {"status": False, "json": "compact"}

Resolution, highest first:

    explicit argument  >  what the model set this session  >  [defaults.*] in the config  >  built-in

The model can only default the *option-like* arguments listed in INPUT below. Arguments that say WHAT to act
on (path, command, content, ...) and ones that widen what an action may do (overwrite, recursive, network,
ports, mounts, ...) are deliberately not defaultable: each of those should be a conscious choice per call.

Scope: one MCP session. stdio = one session for the process. HTTP = one per client session. Behind the
mcp-router that is the router's single upstream connection, so everything behind that router shares it.
Defaults live in memory: a server restart (or a new session) starts from the [defaults] config again.
"""
from __future__ import annotations

import contextlib
import contextvars
import re
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from .errors import SbxError
from .util import validate_name


class DefaultsError(SbxError):
    """A rejected default. Shown to the model as-is."""


# -------------------------------------------------------------------------------------- input keys

@dataclass(frozen=True)
class InputKey:
    kind: str                                   # sandbox | session | bool | int
    doc: str                                    # shown in the set_defaults description; "" when the key is named
                                                # exactly like the argument it fills (the model already knows it)
    targets: tuple[tuple[str, str], ...]        # (tool, parameter) pairs it fills; tool "*" = any tool with that parameter
    lo: int | None = None
    hi: int | None = None
    none_is_unset: bool = True                  # an explicit null argument counts as "omitted"


INPUT: dict[str, InputKey] = {
    # `sandbox` also feeds the lifecycle tools' `name` (but never sandbox_create's name, and never
    # sandbox_destroy: destroying something should always be spelled out).
    "sandbox": InputKey("sandbox", "name or 'host'",
                        (("*", "sandbox"), ("*", "source_sandbox"), ("*", "destination_sandbox"),
                         ("sandbox_info", "name"), ("sandbox_start", "name"),
                         ("sandbox_stop", "name"), ("sandbox_extend", "name"))),
    # not reset_shell_session: there an omitted `session` means "all of them".
    "session": InputKey("session", "", (("run_command", "session"),)),
    "timeout_seconds": InputKey("int", "", (("run_command", "timeout_seconds"),), lo=1),
    "details": InputKey("bool", "", (("list_directory", "details"),)),
    "max_depth": InputKey("int", "", (("directory_tree", "max_depth"),), lo=1, hi=10),
    # for read_file an explicit null means "no line limit", so it must not be overridden.
    "max_lines": InputKey("int", "", (("read_file", "max_lines"),), lo=1, none_is_unset=False),
    "max_results": InputKey("int", "", (("search_files", "max_results"),), lo=1),
    "max_matches": InputKey("int", "", (("grep", "max_matches"),), lo=1),
    "regex": InputKey("bool", "", (("grep", "regex"),)),
    "case_sensitive": InputKey("bool", "", (("grep", "case_sensitive"),)),
    "dry_run": InputKey("bool", "", (("edit_file", "dry_run"),)),
}

# -------------------------------------------------------------------------------------- output keys


@dataclass(frozen=True)
class OutputKey:
    kind: str                                   # bool | enum
    default: Any
    doc: str
    choices: tuple[str, ...] = ()


OUTPUT: dict[str, OutputKey] = {
    "status": OutputKey("bool", True, "run_command exit-code line; false = only on failure, timeout or kill"),
    "duration": OutputKey("bool", True, "the (1.23s) in it"),
    "labels": OutputKey("bool", True, "run_command '--- stdout ---' marker; false drops it when stdout is all there is"),
    "diff": OutputKey("enum", "full", "edit_file: full diff, or just +/- counts (dry_run always diffs)", ("full", "summary")),
    "stats": OutputKey("bool", True, "grep scanned/skipped footer; false keeps only why it stopped early"),
    "json": OutputKey("enum", "pretty", "JSON results",
                      ("pretty", "compact")),
}


@dataclass(frozen=True)
class Out:
    """Resolved output options for one call. Field names and defaults mirror OUTPUT (a test enforces it)."""
    status: bool = True
    duration: bool = True
    labels: bool = True
    diff: str = "full"
    stats: bool = True
    json: str = "pretty"


# ------------------------------------------------------------------------------------- validation

SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    raise DefaultsError("expected true or false")


def _int(v: Any, lo: int | None, hi: int | None) -> int:
    if isinstance(v, bool):
        raise DefaultsError("expected a whole number")
    if isinstance(v, str) and re.fullmatch(r"-?\d{1,12}", v.strip()):
        v = int(v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if not isinstance(v, int):
        raise DefaultsError("expected a whole number")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise DefaultsError("must be " + (f"between {lo} and {hi}" if lo is not None and hi is not None
                                           else f"at least {lo}" if lo is not None else f"at most {hi}"))
    return v


def check_input(key: str, value: Any, *, host_enabled: bool = False) -> Any:
    """Validate and normalise one input default. Raises DefaultsError."""
    spec = INPUT.get(key)
    if spec is None:
        raise DefaultsError(f"unknown input key (valid: {', '.join(INPUT)})")
    if spec.kind == "bool":
        return _bool(value)
    if spec.kind == "int":
        return _int(value, spec.lo, spec.hi)
    if not isinstance(value, str):
        raise DefaultsError("expected a string")
    if spec.kind == "session":
        if not SESSION_RE.fullmatch(value):
            raise DefaultsError("use 1-32 chars of letters, digits, '_' and '-'")
        return value
    if value == "host":                                   # kind == "sandbox"
        if not host_enabled:
            raise DefaultsError("the host target is disabled; use a sandbox")
        return value
    return validate_name(value)


def check_output(key: str, value: Any) -> Any:
    """Validate and normalise one output default. Raises DefaultsError."""
    spec = OUTPUT.get(key)
    if spec is None:
        raise DefaultsError(f"unknown output key (valid: {', '.join(OUTPUT)})")
    if spec.kind == "bool":
        return _bool(value)
    if isinstance(value, str) and value.strip().lower() in spec.choices:
        return value.strip().lower()
    raise DefaultsError("must be one of: " + ", ".join(spec.choices))


def validate_admin(inputs: Mapping[str, Any], outputs: Mapping[str, Any], *, host_enabled: bool) -> tuple[dict, dict]:
    """Check [defaults.input] / [defaults.output] at startup. Returns normalised copies; raises DefaultsError."""
    out_in: dict[str, Any] = {}
    for k, v in inputs.items():
        if k == "sandbox":
            raise DefaultsError("[defaults.input] sandbox: use [tools] default_sandbox for the server-wide default "
                                "sandbox (the model can still change its own with set_defaults)")
        try:
            out_in[k] = check_input(k, v, host_enabled=host_enabled)
        except DefaultsError as e:
            raise DefaultsError(f"[defaults.input] {k}: {e}") from None
    out_out: dict[str, Any] = {}
    for k, v in outputs.items():
        try:
            out_out[k] = check_output(k, v)
        except DefaultsError as e:
            raise DefaultsError(f"[defaults.output] {k}: {e}") from None
    return out_in, out_out


# -------------------------------------------------------------------------------------- the store

_MISSING = object()
_CLEAR = object()


class SessionDefaults:
    """What one MCP session has set, layered over the administrator's [defaults]."""

    def __init__(self, admin_input: Mapping[str, Any] | None = None, admin_output: Mapping[str, Any] | None = None):
        self._admin_in = dict(admin_input or {})
        self._admin_out = dict(admin_output or {})
        self.inputs: dict[str, Any] = {}          # set by the model
        self.outputs: dict[str, Any] = {}
        self._lock = threading.Lock()

    # -- reading ---------------------------------------------------------------------------------
    def effective_inputs(self) -> dict[str, Any]:
        with self._lock:
            return {**self._admin_in, **self.inputs}

    def effective_outputs(self) -> dict[str, Any]:
        with self._lock:
            vals = {k: s.default for k, s in OUTPUT.items()}
            vals.update(self._admin_out)
            vals.update(self.outputs)
            return vals

    def out(self) -> Out:
        return Out(**self.effective_outputs())

    def mine(self) -> dict[str, dict[str, Any]]:
        """What the model itself has set (a consistent copy)."""
        with self._lock:
            return {"input": dict(self.inputs), "output": dict(self.outputs)}

    def from_config(self) -> dict[str, dict[str, Any]]:
        return {"input": dict(self._admin_in), "output": dict(self._admin_out)}

    # -- argument filling ------------------------------------------------------------------------
    def fill(self, tool: str, params: Mapping[str, Any], args: dict[str, Any]) -> list[str]:
        """Put defaults into `args` for every parameter this tool has and the caller left out.
        `args` is the raw dict the model sent, so "omitted" and "passed the default value" stay distinct.
        Returns the parameter names that were filled."""
        filled: list[str] = []
        for key, value in self.effective_inputs().items():
            spec = INPUT[key]
            for t, param in spec.targets:
                if t not in ("*", tool) or param not in params:
                    continue
                if param == "name" and value == "host":     # lifecycle tools address real sandboxes only
                    continue
                cur = args.get(param, _MISSING)
                if cur is _MISSING or (cur is None and spec.none_is_unset):
                    args[param] = value
                    filled.append(param)
        return filled

    # -- writing ---------------------------------------------------------------------------------
    def apply(self, inputs: Any = None, outputs: Any = None, *, reset: bool = False,
              host_enabled: bool = False) -> None:
        """Set/clear defaults atomically: if anything is invalid, nothing changes. A null value clears a key."""
        problems: list[str] = []
        staged: dict[str, dict[str, Any]] = {"input": {}, "output": {}}
        for scope, given, check in (("input", inputs, lambda k, v: check_input(k, v, host_enabled=host_enabled)),
                                    ("output", outputs, check_output)):
            if given is None:
                continue
            if not isinstance(given, dict):
                problems.append(f"{scope}: expected an object like {{\"key\": value}}")
                continue
            for k, v in given.items():
                if v is None:
                    if k not in (INPUT if scope == "input" else OUTPUT):
                        problems.append(f"{scope}.{k}: unknown key (valid: "
                                        f"{', '.join(INPUT if scope == 'input' else OUTPUT)})")
                    else:
                        staged[scope][k] = _CLEAR
                    continue
                try:
                    staged[scope][k] = check(k, v)
                except DefaultsError as e:
                    problems.append(f"{scope}.{k}: {e}")
        if problems:
            raise DefaultsError("nothing was changed. " + "; ".join(problems))
        with self._lock:
            if reset:
                self.inputs.clear()
                self.outputs.clear()
            for store, scope in ((self.inputs, "input"), (self.outputs, "output")):
                for k, v in staged[scope].items():
                    if v is _CLEAR:
                        store.pop(k, None)
                    else:
                        store[k] = v

    def adopt(self, sandbox: str, *, server_default: str = "") -> bool:
        """Make a just-created sandbox the default if nothing else already is. True if it was adopted."""
        with self._lock:
            if "sandbox" in self.inputs or server_default:
                return False
            self.inputs["sandbox"] = sandbox
            return True

    def forget_sandbox(self, name: str) -> None:
        with self._lock:
            if self.inputs.get("sandbox") == name:
                del self.inputs["sandbox"]


class DefaultsStore:
    """One SessionDefaults per MCP session (weakly held, so closed sessions disappear on their own)."""

    def __init__(self, admin_input: Mapping[str, Any] | None = None, admin_output: Mapping[str, Any] | None = None,
                 *, adopt_created: bool = False):
        self._admin = (dict(admin_input or {}), dict(admin_output or {}))
        self.adopt_created = adopt_created
        self._sessions: "weakref.WeakKeyDictionary[Any, SessionDefaults]" = weakref.WeakKeyDictionary()
        self._global = SessionDefaults(*self._admin)      # calls that arrive without a session (tests, tools run directly)
        self._lock = threading.Lock()

    def for_session(self, key: Any) -> SessionDefaults:
        if key is None:
            return self._global
        with self._lock:
            sd = self._sessions.get(key)
            if sd is None:
                sd = self._sessions[key] = SessionDefaults(*self._admin)
            return sd

    def current(self) -> SessionDefaults:
        return _ACTIVE.get() or self._global

    def forget_sandbox(self, name: str) -> None:
        with self._lock:
            everyone = [self._global, *self._sessions.values()]
        for sd in everyone:
            sd.forget_sandbox(name)


# The SessionDefaults of the call being served. Set by the server's call_tool override; copied into the
# worker threads that run the synchronous tools (anyio propagates contextvars).
_ACTIVE: contextvars.ContextVar[SessionDefaults | None] = contextvars.ContextVar("sbx_session_defaults", default=None)


@contextlib.contextmanager
def activate(sd: SessionDefaults) -> Iterator[SessionDefaults]:
    token = _ACTIVE.set(sd)
    try:
        yield sd
    finally:
        _ACTIVE.reset(token)


# ------------------------------------------------------------------------------------ documentation

def tool_description() -> str:
    """Text of the set_defaults tool. Generated from the tables above so it cannot drift from the behaviour."""
    ins = ", ".join(f"{k} ({v.doc})" if v.doc else k for k, v in INPUT.items())
    outs = "\n".join(
        f"  {k} [{'|'.join(v.choices) if v.choices else 'true|false'}, default {json_literal(v.default)}]: {v.doc}"
        for k, v in OUTPUT.items())
    return ("Set defaults for this session so you need not repeat them. Explicit arguments always win; "
            "null removes a key; reset=true clears all; invalid input changes nothing.\n"
            f"input = arguments filled in when you omit them: {ins}.\n"
            f"output = what results contain:\n{outs}\n"
            "Example: set_defaults(input={\"sandbox\": \"work\"}, output={\"status\": false, \"json\": \"compact\"})")


def json_literal(v: Any) -> str:
    return "true" if v is True else "false" if v is False else str(v)


def describe(sd: SessionDefaults) -> dict[str, Any]:
    """What is in effect (for show_defaults)."""
    d: dict[str, Any] = {"input": sd.effective_inputs(), "output": sd.effective_outputs()}
    cfg = {k: v for k, v in sd.from_config().items() if v}
    if cfg:
        d["server_config"] = cfg
    return d
