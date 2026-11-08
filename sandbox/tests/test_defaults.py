"""Defaults: set_defaults / show_defaults, argument filling, output options.

Layers: unit (tables, filling, store), formatting (run_command / edit_file / grep), a drift guard that ties the
tables to the tools really registered, and end-to-end over a real stdio MCP client against the fake docker.
"""
import gc
import json
import os

import pytest

from conftest import make_cfg
from sbx import defaults as D
from sbx import shellcore as sc
from sbx.errors import ConfigError, SbxError
from sbx.guest.fscore import FsCore
from sbx.server import build_server
from test_e2e import Session, write_config, anyio_backend  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.anyio


# --------------------------------------------------------------------------------------- validation

@pytest.mark.parametrize("key,given,expected", [
    ("details", True, True), ("details", "false", False), ("regex", " TRUE ", True),
    ("timeout_seconds", 30, 30), ("timeout_seconds", "45", 45), ("timeout_seconds", 60.0, 60),
    ("max_depth", 10, 10), ("session", "dev_1", "dev_1"), ("sandbox", "work", "work"),
])
def test_check_input_accepts_and_normalises(key, given, expected):
    assert D.check_input(key, given) == expected


@pytest.mark.parametrize("key,given,why", [
    ("details", "yes", "true or false"), ("details", 1, "true or false"),
    ("timeout_seconds", 0, "at least 1"), ("timeout_seconds", True, "whole number"),
    ("timeout_seconds", 1.5, "whole number"), ("max_depth", 11, "between 1 and 10"),
    ("session", "has space", "1-32 chars"), ("session", "x" * 33, "1-32 chars"),
    ("sandbox", "Bad Name", "invalid sandbox name"), ("sandbox", 5, "string"),
    ("sandbox", "host", "disabled"),
])
def test_check_input_rejects(key, given, why):
    with pytest.raises(SbxError, match=why):
        D.check_input(key, given)


def test_host_default_needs_host_enabled():
    assert D.check_input("sandbox", "host", host_enabled=True) == "host"


@pytest.mark.parametrize("key", ["path", "paths", "command", "content", "overwrite", "recursive", "network",
                                 "ports", "mounts", "env", "cwd", "name", "stdin", "approve"])
def test_things_that_must_stay_explicit_are_not_defaultable(key):
    """What to act on, and anything that widens what an action may do, is chosen per call."""
    with pytest.raises(SbxError, match="unknown input key"):
        D.check_input(key, "x")


def test_check_output():
    assert D.check_output("status", False) is False
    assert D.check_output("json", "Compact") == "compact"
    assert D.check_output("diff", "summary") == "summary"
    with pytest.raises(SbxError, match="one of: pretty, compact"):
        D.check_output("json", "xml")
    with pytest.raises(SbxError, match="unknown output key"):
        D.check_output("nope", 1)


# ------------------------------------------------------------------------------------------ filling

PARAMS = {"sandbox": {}, "source_sandbox": {}, "destination_sandbox": {}, "name": {}, "session": {}, "max_lines": {}}


def test_fill_only_omitted_arguments_and_explicit_wins():
    sd = D.SessionDefaults()
    sd.apply({"sandbox": "work"})
    args = {"path": "a"}
    assert sd.fill("read_file", {"sandbox": {}}, args) == ["sandbox"]
    assert args == {"path": "a", "sandbox": "work"}
    explicit = {"sandbox": "other"}
    sd.fill("read_file", {"sandbox": {}}, explicit)
    assert explicit["sandbox"] == "other"                      # explicit always wins


def test_explicit_null_sandbox_counts_as_omitted_but_null_max_lines_does_not():
    sd = D.SessionDefaults()
    sd.apply({"sandbox": "work", "max_lines": 50})
    a = {"sandbox": None, "max_lines": None}
    sd.fill("read_file", PARAMS, a)
    assert a["sandbox"] == "work"
    assert a["max_lines"] is None                              # null there means "no line limit": kept
    b = {}
    sd.fill("read_file", PARAMS, b)
    assert b["max_lines"] == 50


def test_fill_skips_parameters_a_tool_does_not_have():
    sd = D.SessionDefaults()
    sd.apply({"sandbox": "work", "session": "dev"})
    a = {}
    sd.fill("check_command", {"sandbox": {}}, a)               # no `session` parameter on this tool
    assert a == {"sandbox": "work"}


def test_transfer_files_both_ends_get_the_default():
    sd = D.SessionDefaults()
    sd.apply({"sandbox": "work"})
    a = {"source_sandbox": "host"}
    sd.fill("transfer_files", PARAMS, a)
    assert a["source_sandbox"] == "host" and a["destination_sandbox"] == "work"


def test_lifecycle_name_is_filled_but_never_for_create_or_destroy():
    sd = D.SessionDefaults()
    sd.apply({"sandbox": "work"})
    for tool in ("sandbox_info", "sandbox_start", "sandbox_stop", "sandbox_extend"):
        a = {}
        sd.fill(tool, {"name": {}}, a)
        assert a == {"name": "work"}, tool
    for tool in ("sandbox_create", "sandbox_destroy"):          # create names a NEW sandbox; destroy stays explicit
        a = {}
        sd.fill(tool, {"name": {}}, a)
        assert a == {}, tool


def test_host_default_is_not_used_as_a_lifecycle_name():
    sd = D.SessionDefaults()
    sd.apply({"sandbox": "host"}, host_enabled=True)
    a = {}
    sd.fill("sandbox_info", {"name": {}}, a)
    assert a == {}


def test_session_default_does_not_change_reset_shell_session():
    """There an omitted `session` means "all sessions", which a default must not silently narrow."""
    sd = D.SessionDefaults()
    sd.apply({"session": "dev"})
    a = {}
    sd.fill("reset_shell_session", {"session": {}, "sandbox": {}}, a)
    assert "session" not in a


# ---------------------------------------------------------------------------- layers and atomicity

def test_layers_admin_then_session_and_null_clears():
    sd = D.SessionDefaults({"session": "cfg", "timeout_seconds": 10}, {"status": False})
    assert sd.effective_inputs() == {"session": "cfg", "timeout_seconds": 10}
    assert sd.out().status is False
    sd.apply({"session": "mine"}, {"status": True, "json": "compact"})
    assert sd.effective_inputs()["session"] == "mine" and sd.out().status is True and sd.out().json == "compact"
    sd.apply({"session": None}, {"status": None})                # clearing falls back to the admin layer
    assert sd.effective_inputs()["session"] == "cfg" and sd.out().status is False
    assert sd.out().json == "compact"


def test_apply_is_atomic():
    sd = D.SessionDefaults()
    sd.apply({"sandbox": "work"})
    with pytest.raises(SbxError) as e:
        sd.apply({"session": "ok", "max_depth": 99, "bogus": 1}, {"json": "pretty", "status": "maybe"})
    msg = str(e.value)
    assert "nothing was changed" in msg and "max_depth" in msg and "bogus" in msg and "status" in msg
    assert sd.inputs == {"sandbox": "work"} and sd.outputs == {}   # not even the valid parts landed


def test_apply_rejects_non_objects_and_unknown_null_keys():
    sd = D.SessionDefaults()
    with pytest.raises(SbxError, match="expected an object"):
        sd.apply(["sandbox"])
    with pytest.raises(SbxError, match="unknown key"):
        sd.apply({"typo": None})


def test_reset_clears_only_what_the_model_set():
    sd = D.SessionDefaults({"session": "cfg"})
    sd.apply({"sandbox": "work"}, {"status": False})
    sd.apply(reset=True)
    assert sd.effective_inputs() == {"session": "cfg"} and sd.out() == D.Out()


def test_adopt_only_when_nothing_is_default_yet():
    sd = D.SessionDefaults()
    assert sd.adopt("a") and sd.inputs["sandbox"] == "a"
    assert not sd.adopt("b") and sd.inputs["sandbox"] == "a"
    assert not D.SessionDefaults().adopt("c", server_default="configured")


def test_store_isolates_sessions_and_releases_closed_ones():
    store = D.DefaultsStore({"session": "cfg"}, {"status": False})

    class Key:                                                    # stands in for an MCP ServerSession
        pass
    a, b = Key(), Key()
    store.for_session(a).apply({"sandbox": "one"})
    assert "sandbox" not in store.for_session(b).effective_inputs()
    assert store.for_session(a) is store.for_session(a)
    assert store.for_session(b).effective_inputs() == {"session": "cfg"}     # every session starts from [defaults]
    assert len(store._sessions) == 2
    del a
    gc.collect()
    assert len(store._sessions) == 1


def test_forget_sandbox_clears_matching_defaults_everywhere():
    store = D.DefaultsStore()

    class Key:
        pass
    a, b = Key(), Key()
    store.for_session(a).apply({"sandbox": "gone"})
    store.for_session(b).apply({"sandbox": "kept"})
    store.forget_sandbox("gone")
    assert "sandbox" not in store.for_session(a).effective_inputs()
    assert store.for_session(b).effective_inputs()["sandbox"] == "kept"


# ----------------------------------------------------------------------------------- config layer

def raw(tmp_path, **defaults):
    return {"server": {"state_dir": str(tmp_path / "s")}, "defaults": defaults}


def test_config_defaults_are_validated_and_normalised(tmp_path):
    from sbx import config as C
    cfg = C.from_dict(raw(tmp_path, input={"session": "dev", "timeout_seconds": "90"},
                          output={"status": False, "json": "Compact"}, adopt_created=True))
    assert cfg.defaults.input == {"session": "dev", "timeout_seconds": 90}
    assert cfg.defaults.output == {"status": False, "json": "compact"} and cfg.defaults.adopt_created


@pytest.mark.parametrize("bad,match", [
    ({"input": {"sandbox": "a"}}, r"\[tools\] default_sandbox"),
    ({"input": {"nope": 1}}, "unknown input key"),
    ({"input": {"max_depth": 99}}, r"\[defaults.input\] max_depth"),
    ({"output": {"json": "xml"}}, r"\[defaults.output\] json"),
    ({"output": {"stats": "maybe"}}, r"\[defaults.output\] stats"),
])
def test_config_rejects_bad_defaults(tmp_path, bad, match):
    from sbx import config as C
    with pytest.raises(ConfigError, match=match):
        C.from_dict(raw(tmp_path, **bad))


def test_sample_config_documents_defaults_and_still_loads(tmp_path):
    import tomllib
    from sbx import config as C
    assert "[defaults]" in C.SAMPLE_CONFIG and "set_defaults" in C.SAMPLE_CONFIG
    parsed = tomllib.loads(C.SAMPLE_CONFIG)
    parsed.setdefault("server", {})["state_dir"] = str(tmp_path / "s")
    C.from_dict(parsed)


# ---------------------------------------------------------------------- tables vs the real tools

def _props(schema):
    return schema.get("properties", {})


def _bounds(prop):
    """(minimum, maximum) from a pydantic JSON-schema property, looking through Optional's anyOf."""
    for p in [prop, *prop.get("anyOf", [])]:
        if p.get("type") in ("integer", "number"):
            return p.get("minimum"), p.get("maximum")
    return None, None


def test_input_table_matches_registered_tools(tmp_path, fake_docker):
    """Every (tool, parameter) the table feeds must exist, and its limits must agree with the tool's schema,
    so a default can never be accepted here and then rejected when a tool uses it."""
    mcp, _ = build_server(make_cfg(tmp_path))
    tools = {t.name: _props(t.parameters) for t in mcp._tool_manager.list_tools()}
    for key, spec in D.INPUT.items():
        for tool, param in spec.targets:
            owners = [n for n, p in tools.items() if param in p] if tool == "*" else [tool]
            assert owners, f"{key}: no tool has parameter {param!r}"
            for n in owners:
                assert n in tools and param in tools[n], f"{key}: {n}.{param} does not exist"
                if spec.kind == "int":
                    lo, hi = _bounds(tools[n][param])
                    assert (lo is None or spec.lo is None or spec.lo >= lo) and (hi is None or (spec.hi is not None and spec.hi <= hi)), \
                        f"{key}: limits disagree with {n}.{param} ({lo}..{hi})"
                if spec.kind == "bool":
                    assert tools[n][param].get("type") == "boolean", f"{key}: {n}.{param} is not a boolean"


def test_nothing_that_must_stay_explicit_is_reachable_through_a_default(tmp_path, fake_docker):
    mcp, _ = build_server(make_cfg(tmp_path))
    fed = {p for spec in D.INPUT.values() for _, p in spec.targets}
    assert not fed & {"path", "paths", "source", "destination", "source_path", "destination_path", "command",
                      "content", "edits", "overwrite", "recursive", "network", "ports", "mounts", "env", "cwd",
                      "stdin", "pattern", "approve"}
    # `name` is fed only for the four lifecycle tools that act on an existing sandbox
    assert {t for spec in D.INPUT.values() for t, p in spec.targets if p == "name"} == \
        {"sandbox_info", "sandbox_start", "sandbox_stop", "sandbox_extend"}


def test_out_fields_mirror_the_output_table():
    import dataclasses
    fields = {f.name: f.default for f in dataclasses.fields(D.Out)}
    assert fields == {k: v.default for k, v in D.OUTPUT.items()}


def test_tool_description_lists_every_key():
    text = D.tool_description()
    for k in [*D.INPUT, *D.OUTPUT]:
        assert k in text


# ---------------------------------------------------------------------------- run_command output

def make_result(**kw):
    base = dict(exit_code=0, stdout="", stderr="", truncated=False, timed_out=False, flooded=False,
                duration=1.234, signal=None)
    base.update(kw)
    return sc.ExecResult(**base)


@pytest.fixture
def fmt(tmp_path, fake_docker):
    cfg = make_cfg(tmp_path)
    redact = sc.Redactor(cfg.shell_guest)

    def f(**kw):
        opts = {k: kw.pop(k) for k in ("status", "duration", "labels") if k in kw}
        return sc.format_result(make_result(**kw), 20, cfg.shell_guest, redact, **opts)
    return f


def test_default_format_is_unchanged(fmt):
    assert fmt(stdout="hi\n") == "exit code: 0  (1.23s)\n--- stdout ---\nhi"
    assert fmt() == "exit code: 0  (1.23s)\n(no output)"
    assert fmt(exit_code=2, stdout="a\n", stderr="b\n") == \
        "exit code: 2  (1.23s)\n--- stdout ---\na\n--- stderr ---\nb"


def test_status_off_returns_just_the_output_on_success(fmt):
    assert fmt(stdout="hi\n", status=False, labels=False) == "hi"
    assert fmt(stdout="hi\n", status=False) == "--- stdout ---\nhi"            # labels are a separate switch
    assert fmt(status=False) == "(no output)"                                    # still says it succeeded
    assert fmt(stdout="\n", status=False, labels=False) == "(no output)"         # never an empty result


def test_status_off_never_hides_failures(fmt):
    # failing: the status line is back even though status=False; labels=False still applies to the stdout marker
    assert fmt(exit_code=1, status=False, stdout="x\n", labels=False) == "exit code: 1  (1.23s)\nx"
    assert fmt(timed_out=True, exit_code=None, status=False).startswith("TIMED OUT after 20s")
    assert fmt(flooded=True, exit_code=None, status=False).startswith("KILLED")
    assert fmt(signal=9, exit_code=None, status=False).startswith("terminated by signal 9 (SIGKILL)")


def test_duration_off(fmt):
    assert fmt(stdout="hi\n", duration=False) == "exit code: 0\n--- stdout ---\nhi"
    assert fmt(exit_code=3, status=False, duration=False) == "exit code: 3\n(no output)"


def test_labels_off_keeps_stderr_distinguishable(fmt):
    assert fmt(stdout="out\n", stderr="err\n", status=False, labels=False) == \
        "--- stdout ---\nout\n--- stderr ---\nerr"                                # both present: both labelled
    assert fmt(stderr="err\n", status=False, labels=False) == "--- stderr ---\nerr"


def test_truncation_notice_is_never_dropped(fmt):
    out = fmt(stdout="x\n", truncated=True, status=False, labels=False)
    assert out.splitlines()[0] == "x" and "output truncated" in out


# ------------------------------------------------------------------------- guest engine options

@pytest.fixture
def engine(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return FsCore({"roots": [str(root)], "read_only": False, "allow_delete": True}), root


def test_grep_footer_is_optional_but_stop_reasons_are_kept(engine):
    c, root = engine
    (root / "a.txt").write_text("needle\nhay\nneedle\n")
    full = c.call("grep", {"pattern": "needle"})["text"]
    assert full.splitlines()[-1].startswith("[2 matches; scanned 1 files")
    short = c.call("grep", {"pattern": "needle", "show_stats": False})["text"]
    assert short.splitlines() == [l for l in full.splitlines() if not l.startswith("[")] and "scanned" not in short
    assert c.call("grep", {"pattern": "zzz", "show_stats": False})["text"] == "no matches"
    capped = c.call("grep", {"pattern": "needle", "max_matches": 1, "show_stats": False})["text"]
    assert capped.splitlines()[-1] == "[stopped: match limit (1) reached]"        # why it stopped survives


def test_edit_file_reports_exact_line_counts(engine):
    c, root = engine
    f = root / "f.txt"
    f.write_text("one\ntwo\nthree\n")
    res = c.call("edit_file", {"path": "f.txt", "edits": [{"old_text": "two", "new_text": "2\n2b"}]})
    assert (res["added"], res["removed"]) == (2, 1) and res["path"] == str(f)
    assert "+2b" in res["text"] and f.read_text() == "one\n2\n2b\nthree\n"
    # a removed line that itself starts with "--" must not be mistaken for a diff header
    f.write_text("-- comment\nkeep\n")
    res = c.call("edit_file", {"path": "f.txt", "edits": [{"old_text": "-- comment\n", "new_text": ""}]})
    assert (res["added"], res["removed"]) == (0, 1)
    # dry runs and no-ops stay plain text: they carry no counts, so a summary never replaces a preview
    dry = c.call("edit_file", {"path": "f.txt", "dry_run": True, "edits": [{"old_text": "keep", "new_text": "x"}]})
    assert "added" not in dry and dry["text"].startswith("[dry run")


# --------------------------------------------------------------------------------------- end to end

async def test_defaults_end_to_end(tmp_path, fake_docker):
    cfgfile, ws, host = write_config(tmp_path)
    async with Session(cfgfile) as c:
        names = {t.name for t in (await c.s.list_tools()).tools}
        assert {"set_defaults", "show_defaults"} <= names
        assert not (await c.call("sandbox_create", name="dev"))[0]

        # nothing set yet: the old behaviour (and its pointer to the fix)
        err, out = await c.call("run_command", command="echo x")
        assert err and "no sandbox specified" in out and "set_defaults" in out

        # one call, then no `sandbox` anywhere
        err, out = await c.call("set_defaults", input={"sandbox": "dev"})
        assert not err and out == 'ok {"input":{"sandbox":"dev"}}', out
        err, out = await c.call("run_command", command="echo hello")
        assert not err and "exit code: 0" in out and "hello" in out
        assert not (await c.call("write_file", path="a.txt", content="one\ntwo\n"))[0]
        assert (ws / "a.txt").exists()
        assert (await c.call("read_file", path="a.txt"))[1] == "one\ntwo\n"
        err, out = await c.call("sandbox_info")                       # `name` falls back to the default too
        assert not err and json.loads(out)["name"] == "dev"
        # explicit arguments always win
        err, out = await c.call("run_command", command="echo x", sandbox="nosuch")
        assert err and "no such sandbox" in out
        # but destroy never takes the default
        err, out = await c.call("sandbox_destroy")
        assert err and "name" in out

        # output options: trim what comes back
        err, out = await c.call("set_defaults", output={"status": False, "labels": False})
        assert not err
        assert (await c.call("run_command", command="echo hello"))[1] == "hello"
        err, out = await c.call("run_command", command="false")
        assert out.startswith("exit code: 1")                          # failures are never hidden
        assert (await c.call("run_command", command="echo oops >&2"))[1] == "--- stderr ---\noops"
        assert (await c.call("run_command", command="true"))[1] == "(no output)"
        await c.call("set_defaults", output={"duration": False, "status": True})
        assert (await c.call("run_command", command="echo hello"))[1] == "exit code: 0\nhello"

        err, out = await c.call("edit_file", path="a.txt", edits=[{"old_text": "two", "new_text": "2"}])
        assert "+2" in out and "@@" in out                            # diff=full by default
        await c.call("set_defaults", output={"diff": "summary", "stats": False})
        err, out = await c.call("edit_file", path="a.txt", edits=[{"old_text": "2", "new_text": "two"}])
        assert out == f"edited {ws / 'a.txt'}: 1 edit(s), +1 -1 lines"
        err, out = await c.call("edit_file", path="a.txt", dry_run=True, edits=[{"old_text": "two", "new_text": "x"}])
        assert out.startswith("[dry run") and "@@" in out              # previews always show the diff
        err, out = await c.call("grep", pattern="one")
        assert out.strip() == f"{ws / 'a.txt'}:1: one"

        await c.call("set_defaults", output={"json": "compact"})
        err, out = await c.call("sandbox_list")
        assert "\n" not in out and json.loads(out)[0]["name"] == "dev"
        err, out = await c.call("list_allowed_directories")
        assert "\n" not in out and json.loads(out)["target"] == "dev"

        # what is in effect
        err, out = await c.call("show_defaults")
        shown = json.loads(out)
        assert shown["input"] == {"sandbox": "dev"}
        assert shown["output"] == {"status": True, "duration": False, "labels": False, "diff": "summary",
                                   "stats": False, "json": "compact"}
        assert "server_config" not in shown

        # bad input changes nothing and says what is valid
        err, out = await c.call("set_defaults", input={"max_depth": 99, "path": "/x"}, output={"json": "pretty"})
        assert err and "nothing was changed" in out and "max_depth" in out and "path" in out
        assert json.loads((await c.call("show_defaults"))[1])["output"]["json"] == "compact"

        # a null clears one key; reset clears everything
        await c.call("set_defaults", output={"json": None})
        assert "\n" in (await c.call("sandbox_list"))[1]
        assert not (await c.call("set_defaults", reset=True))[0]
        err, out = await c.call("run_command", command="echo x")
        assert err and "no sandbox specified" in out                  # the sandbox default went with it
        err, out = await c.call("run_command", command="echo hi", sandbox="dev")
        assert out.startswith("exit code: 0  (") and out.endswith("hi")   # and the output options are back to normal

        # destroying the default sandbox clears the default
        await c.call("set_defaults", input={"sandbox": "dev"})
        assert not (await c.call("sandbox_destroy", name="dev"))[0]
        assert json.loads((await c.call("show_defaults"))[1])["input"] == {}

    from sbx.shellcore import Audit
    ok, msg = Audit.verify(str(tmp_path / "audit.jsonl"))
    assert ok, msg
    recs = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    sets = [r for r in recs if r.get("tool") == "set_defaults"]
    assert any(r["ok"] and r["input"] == {"sandbox": "dev"} for r in sets)        # what was set is on record
    assert any(not r["ok"] and "nothing was changed" in r["error"] for r in sets)  # and so is a refusal
    runs = [r for r in recs if r.get("tool") == "run_command" and r.get("command") == "echo hello"]
    assert runs and all(r["sandbox"] == "dev" for r in runs)           # tool entries carry the RESOLVED sandbox


async def test_set_defaults_notes_a_sandbox_that_does_not_exist_yet(tmp_path, fake_docker):
    cfgfile, ws, host = write_config(tmp_path)
    async with Session(cfgfile) as c:
        err, out = await c.call("set_defaults", input={"sandbox": "later"})
        assert not err and "no sandbox named 'later' exists yet" in out
        err, out = await c.call("set_defaults", input={"sandbox": "host"})     # [host] is enabled in this config
        assert not err and "exists yet" not in out


async def test_host_default_is_refused_when_the_host_target_is_off(tmp_path, fake_docker):
    cfgfile, ws, host = write_config(tmp_path)
    text = open(cfgfile).read().replace("[host]\nenabled = true", "[host]\nenabled = false").replace(
        f'roots = ["{host}"]', "")
    open(cfgfile, "w").write(text)
    async with Session(cfgfile) as c:
        err, out = await c.call("set_defaults", input={"sandbox": "host"})
        assert err and "host target is disabled" in out


async def test_server_config_sets_where_every_session_starts(tmp_path, fake_docker):
    cfgfile, ws, host = write_config(tmp_path, extra='[defaults.input]\nsession = "ci"\n'
                                                     '[defaults.output]\nstatus = false\nlabels = false')
    async with Session(cfgfile) as c:
        assert not (await c.call("sandbox_create", name="dev"))[0]
        assert not (await c.call("set_defaults", input={"sandbox": "dev"}))[0]
        assert (await c.call("run_command", command="export FOO=1"))[1] == "(no output)"
        assert (await c.call("run_command", command="echo $FOO"))[1] == "1"        # session "ci" came from the config
        err, out = await c.call("shell_info")
        assert '"name": "ci"' in out
        shown = json.loads((await c.call("show_defaults"))[1])
        assert shown["input"] == {"session": "ci", "sandbox": "dev"}
        assert shown["server_config"] == {"input": {"session": "ci"}, "output": {"status": False, "labels": False}}
        # the model overrides the server's choice for itself, and can fall back to it
        await c.call("set_defaults", output={"status": True})
        assert (await c.call("run_command", command="echo a"))[1].startswith("exit code: 0")
        await c.call("set_defaults", output={"status": None})          # back to the server's choice
        assert (await c.call("run_command", command="echo a"))[1] == "a"


async def test_adopt_created_is_opt_in(tmp_path, fake_docker):
    cfgfile, ws, host = write_config(tmp_path)
    async with Session(cfgfile) as c:                                # off by default: creating does not choose
        err, out = await c.call("sandbox_create", name="one")
        assert "note" not in json.loads(out)
        err, out = await c.call("run_command", command="echo x")
        assert err and "no sandbox specified" in out
    (tmp_path / "second").mkdir()
    cfgfile2, *_ = write_config(tmp_path / "second", extra="[defaults]\nadopt_created = true")
    async with Session(cfgfile2) as c:
        err, out = await c.call("sandbox_create", name="one")
        assert "now your default sandbox" in json.loads(out)["note"]
        assert "exit code: 0" in (await c.call("run_command", command="echo x"))[1]
        err, out = await c.call("sandbox_create", name="two")        # only the first is adopted
        assert "note" not in json.loads(out)
        assert json.loads((await c.call("show_defaults"))[1])["input"] == {"sandbox": "one"}


async def test_server_default_sandbox_is_not_overridden_by_adoption(tmp_path, fake_docker):
    cfgfile, ws, host = write_config(tmp_path, extra="[defaults]\nadopt_created = true")
    async with Session(cfgfile, "--default-sandbox", "main") as c:
        err, out = await c.call("sandbox_create", name="other")
        assert "note" not in json.loads(out)


def test_instructions_describe_defaults_and_sessions(tmp_path, fake_docker):
    from sbx.server import instructions
    text = instructions(make_cfg(tmp_path))
    assert "set_defaults" in text and "show_defaults" in text
    assert "stateless" not in text and "cd once" in text              # sessions exist, so don't tell the model to pass cwd
    assert "becomes your default" not in text                         # adoption is opt-in
    off = instructions(make_cfg(tmp_path, shell={"session": {"enabled": False}}))
    assert "stateless" in off and "pass cwd" in off                   # ...but when they are disabled, do
    on = instructions(make_cfg(tmp_path, defaults={"adopt_created": True}))
    assert "becomes your default" in on


async def test_sessions_do_not_share_defaults(tmp_path, fake_docker):
    """Two clients of one server process (as with streamable-http) each get their own defaults."""
    from mcp.shared.memory import create_connected_server_and_client_session as connect
    cfgfile, ws, host = write_config(tmp_path)
    from sbx import config as C
    import argparse
    ap = argparse.ArgumentParser()
    C.add_cli_args(ap)
    cfg = C.build_config(ap.parse_args(["--config", cfgfile]))
    mcp, _ = build_server(cfg)

    async def call(cs, tool, **args):
        r = await cs.call_tool(tool, args)
        return r.isError, "\n".join(x.text for x in r.content if x.type == "text")

    async with connect(mcp._mcp_server) as one, connect(mcp._mcp_server) as two:
        assert not (await call(one, "sandbox_create", name="dev"))[0]
        assert not (await call(one, "set_defaults", input={"sandbox": "dev"}, output={"status": False}))[0]
        assert (await call(one, "run_command", command="echo a"))[1] == "--- stdout ---\na"
        err, out = await call(two, "run_command", command="echo a")        # the other session set nothing
        assert err and "no sandbox specified" in out
        assert json.loads((await call(two, "show_defaults"))[1])["input"] == {}
        err, out = await call(two, "run_command", command="echo b", sandbox="dev")   # explicit still works
        assert not err and out.startswith("exit code: 0")                             # and sees ITS (default) format


def test_defaults_tools_are_annotated_so_clients_do_not_prompt(tmp_path, fake_docker):
    mcp, _ = build_server(make_cfg(tmp_path))
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    for n in ("set_defaults", "show_defaults"):
        assert tools[n].annotations.readOnlyHint is True
    assert "sandbox" in tools["set_defaults"].description and "status" in tools["set_defaults"].description
    # the lifecycle tools that now accept a default no longer REQUIRE a name in their schema
    for n in ("sandbox_info", "sandbox_start", "sandbox_stop", "sandbox_extend"):
        assert "name" not in tools[n].parameters.get("required", []), n
    assert "name" in tools["sandbox_destroy"].parameters["required"]


async def test_lifecycle_tools_explain_themselves_when_no_sandbox_can_be_chosen(tmp_path, fake_docker):
    cfgfile, ws, host = write_config(tmp_path)
    async with Session(cfgfile) as c:
        err, out = await c.call("sandbox_info")
        assert err and "no sandbox specified" in out and "set_defaults" in out
        err, out = await c.call("sandbox_info", name="host")           # named, but not a sandbox: say that instead
        assert err and "not a sandbox" in out and "no sandbox specified" not in out
        await c.call("set_defaults", input={"sandbox": "host"})          # a host default never becomes a lifecycle name
        err, out = await c.call("sandbox_stop")
        assert err and "no sandbox specified" in out
