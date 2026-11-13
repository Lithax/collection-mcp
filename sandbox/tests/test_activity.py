"""The per-project hand-off log (sbx/activity.py): which project a call belongs to, what a line looks like,
where it is written, and that it never gets in the way of a call."""
import datetime as dt
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sbx import activity as act  # noqa: E402
from sbx import config as C  # noqa: E402
from sbx.backends.base import Mount  # noqa: E402
from test_e2e import Session, write_config, anyio_backend  # noqa: E402,F401  (fixture re-export)

WORKDIR = "/workspace"
NOW = dt.datetime(2026, 11, 12, 19, 50, 3, tzinfo=dt.timezone.utc)


def cfg(**kw):
    return C.CollabCfg(log=kw.pop("log", True), **kw)


class Session_:  # stands in for the per-MCP-session defaults object (weak-referenceable, like the real one)
    pass


# ---- which project -------------------------------------------------------------------------------
def test_the_innermost_mapping_wins_and_read_only_ones_are_respected():
    ms = [Mount("/h/proj", "/workspace/proj", "rw", "proj"), Mount("/h/vendor", "/workspace/proj/vendor", "ro", "v")]
    assert act.mount_for(ms, WORKDIR, "/workspace/proj/src/x.py") == (ms[0], "src/x.py")
    assert act.mount_for(ms, WORKDIR, "/workspace/proj/vendor/x.py") is None          # nested read-only mapping
    assert act.mount_for(ms, WORKDIR, "/workspace/proj") == (ms[0], ".")


def test_relative_paths_resolve_against_the_workdir_and_traps_are_not_matched():
    ms = [Mount("/h/proj", "/workspace/proj", "rw", "proj")]
    assert act.mount_for(ms, "/workspace/proj", "src/x.py") == (ms[0], "src/x.py")
    assert act.mount_for(ms, WORKDIR, "proj/x.py") == (ms[0], "x.py")
    assert act.mount_for(ms, WORKDIR, "/workspace/proj-other/x.py") is None           # shares a prefix, not a parent
    assert act.mount_for(ms, WORKDIR, "/workspace/proj/../elsewhere/x.py") is None    # .. is resolved first
    assert act.mount_for(ms, WORKDIR, "/tmp/x") is None


def test_the_collab_folder_itself_is_never_logged():
    ms = [Mount("/h/proj", "/workspace/proj", "rw", "proj")]
    assert act.mount_for(ms, WORKDIR, "/workspace/proj/.collab") is None
    assert act.mount_for(ms, WORKDIR, "/workspace/proj/.collab/plan.md") is None
    assert act.mount_for(ms, WORKDIR, "/workspace/proj/.collabs/x") == (ms[0], ".collabs/x")


def test_paths_of_collects_every_path_argument():
    args = {"path": "a", "paths": ["b", 3, "c"], "source": "d", "destination": "e", "pattern": "nope"}
    assert act.paths_of(args) == ["a", "b", "c", "d", "e"]
    assert act.paths_of({"command": "ls"}) == []


# ---- the line --------------------------------------------------------------------------------------
def test_summarize_shows_paths_relative_to_the_project_and_redacts_text():
    rel = lambda p: p.replace("/workspace/proj/", "")  # noqa: E731
    scrub = lambda s: s.replace("hunter2", "[REDACTED]")  # noqa: E731
    out = act.summarize({"cwd": "/workspace/proj/router", "command": "pytest -q"}, lambda p: "router", scrub)
    assert out == 'cwd=router command="pytest -q"'
    assert act.summarize({"path": "/workspace/proj/a.py", "content_bytes": 12}, rel, scrub) == "path=a.py content_bytes=12"
    assert "hunter2" not in act.summarize({"command": "login --password hunter2"}, rel, scrub)
    assert act.summarize({}, rel, scrub) == "-"


def test_summarize_cuts_long_values_and_counts_long_lists():
    rel = lambda p: p  # noqa: E731
    out = act.summarize({"command": "a" * 300}, rel, lambda s: s)
    assert "…(+200)" in out and len(out) < 150
    assert act.summarize({"paths": ["x" * 30] * 10}, rel, lambda s: s) == "paths=<10 items>"
    assert act.summarize({"command": "echo a\necho b"}, rel, lambda s: s).count("\n") == 0


def test_outcomes():
    assert act.error_outcome(None) == "ok"
    assert act.error_outcome("boom\nbad") == "ERR boom bad"
    assert act.shell_outcome("ran", 0, False) == "ok"
    assert act.shell_outcome("ran_after_approval", 0, False) == "ok"
    assert act.shell_outcome("ran", 2, False) == "exit 2"
    assert act.shell_outcome("ran", 124, True) == "timeout"
    assert act.shell_outcome("ran", None, False) == "ERR no exit status"
    assert act.shell_outcome("blocked", None, None) == "ERR blocked"
    assert act.shell_outcome("needs_confirmation_denied", None, None) == "ERR needs confirmation denied"


def test_format_line_is_one_bounded_line():
    line = act.format_line("3fa9c1", "run_command", "work", 'cwd=router command="pytest -q"', "exit 1", 21004.6, NOW)
    assert line == '2026-11-12T19:50:03Z | 3fa9c1 | run_command@work | cwd=router command="pytest -q" | exit 1 | 21004ms'
    hostile = act.format_line("-", "a\nb", "w|x", "x\n2026-01-01T00:00:00Z | forged", "ok\nERR", 1, NOW)
    assert "\n" not in hostile and "a?b@w?x" in hostile
    long = act.format_line("-", "t", "s", "k=" + "v" * 5000, "ok", 1, NOW)
    assert len(long) <= act.LINE_LIMIT and long.endswith(" | ok | 1ms")


# ---- the writer ------------------------------------------------------------------------------------
def two_projects(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    return a, b, [Mount(str(a), "/workspace/a", "rw", "a"), Mount(str(b), "/workspace/b", "rw", "b")]


def note(log, ms, path, **over):
    kw = dict(tool="write_file", sandbox="w", args={"path": path, "content_bytes": 5}, paths=[path], outcome="ok",
              ms=3.2, now=NOW)
    kw.update(over)
    return log.note(ms, WORKDIR, **kw)


def test_a_call_is_logged_in_the_project_it_touched_and_only_there(tmp_path):
    a, b, ms = two_projects(tmp_path)
    log = act.ActivityLog(cfg())
    assert note(log, ms, "/workspace/a/src/x.py")
    assert note(log, ms, "/workspace/a/y.py", tool="read_file")
    lines = (a / ".collab" / "calls.log").read_text().splitlines()
    assert lines[0] == "2026-11-12T19:50:03Z | - | write_file@w | path=src/x.py content_bytes=5 | ok | 3ms"
    assert " read_file@w " in lines[1] and len(lines) == 2
    assert not (b / ".collab").exists()                         # the other project is untouched


def test_the_collab_folder_ignores_itself_in_git_and_is_readable_by_others(tmp_path):
    a, _, ms = two_projects(tmp_path)
    note(act.ActivityLog(cfg()), ms, "/workspace/a/x")
    assert (a / ".collab" / ".gitignore").read_text() == "*\n"
    assert stat.S_IMODE((a / ".collab" / "calls.log").stat().st_mode) == 0o644


def test_nothing_is_written_for_unmapped_read_only_or_collab_paths(tmp_path):
    a, _, ms = two_projects(tmp_path)
    ro = [Mount(str(tmp_path / "ro"), "/workspace/ro", "ro", "ro")]
    (tmp_path / "ro").mkdir()
    log = act.ActivityLog(cfg())
    assert not note(log, ms, "/tmp/elsewhere")
    assert not note(log, ro, "/workspace/ro/x")
    assert not note(log, ms, "/workspace/a/.collab/plan.md")
    assert not (tmp_path / "ro" / ".collab").exists() and not (a / ".collab" / "calls.log").exists()


def test_a_disabled_log_does_nothing(tmp_path):
    a, _, ms = two_projects(tmp_path)
    assert not note(act.ActivityLog(cfg(log=False)), ms, "/workspace/a/x")
    assert not (a / ".collab").exists()


def test_prepare_creates_the_folder_up_front_for_writable_mappings_only(tmp_path):
    a, b, ms = two_projects(tmp_path)
    ro = tmp_path / "ro"
    ro.mkdir()
    ms.append(Mount(str(ro), "/workspace/ro", "ro", "ro"))
    act.ActivityLog(cfg()).prepare(ms)
    for p in (a, b):
        assert (p / ".collab" / ".gitignore").read_text() == "*\n" and not (p / ".collab" / "calls.log").exists()
    assert not (ro / ".collab").exists()                         # a read-only mapping is never touched
    (a / ".collab" / ".gitignore").write_text("mine\n")
    act.ActivityLog(cfg()).prepare(ms)                            # idempotent: leaves an existing folder alone
    assert (a / ".collab" / ".gitignore").read_text() == "mine\n"
    act.ActivityLog(cfg(log=False)).prepare([Mount(str(ro), "/workspace/x", "rw", "x")])
    assert not (ro / ".collab").exists()                         # disabled: nothing happens


def test_the_first_path_that_hits_a_project_decides(tmp_path):
    a, b, ms = two_projects(tmp_path)
    log = act.ActivityLog(cfg())
    note(log, ms, "/tmp/none", paths=["/tmp/none", "/workspace/b/src", "/workspace/a/x"],
         args={"source": "/workspace/b/src", "destination": "/workspace/a/x"}, tool="copy_path")
    assert (b / ".collab" / "calls.log").exists() and not (a / ".collab").exists()


def test_rotation_keeps_one_older_file(tmp_path):
    a, _, ms = two_projects(tmp_path)
    log = act.ActivityLog(cfg(max_bytes=120))
    for i in range(4):
        note(log, ms, f"/workspace/a/f{i}")
    d = a / ".collab"
    assert (d / "calls.log.1").exists() and (d / "calls.log").exists()
    assert (d / "calls.log").stat().st_size < 400


def test_redaction_is_applied_to_what_is_written(tmp_path):
    a, _, ms = two_projects(tmp_path)
    log = act.ActivityLog(cfg(), scrub=lambda s: s.replace("hunter2", "[REDACTED]"))
    note(log, ms, "/workspace/a", tool="run_command", args={"cwd": "/workspace/a", "command": "login hunter2"},
         paths=["/workspace/a"])
    text = (a / ".collab" / "calls.log").read_text()
    assert "hunter2" not in text and "[REDACTED]" in text and "cwd=. " in text


def test_a_log_that_cannot_be_written_never_raises_and_complains_once(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("a file where the project folder should be")
    ms = [Mount(str(blocker), "/workspace/a", "rw", "a")]
    log = act.ActivityLog(cfg())
    assert not note(log, ms, "/workspace/a/x")
    assert not note(log, ms, "/workspace/a/y")
    assert capsys.readouterr().err.count("collab log") == 1


def test_session_tags_are_stable_per_session_and_dash_when_unknown():
    log = act.ActivityLog(cfg())
    s1, s2 = Session_(), Session_()
    assert log.tag(s1) == log.tag(s1) != log.tag(s2) and len(log.tag(s1)) == 6
    assert log.tag(None) == "-" and log.tag(5) == "-"          # nothing to label / not weak-referenceable


# ---- end to end: a real server over stdio against the fake docker --------------------------------------
def e2e_config(tmp_path, collab: bool):
    proj = tmp_path / "proj"
    proj.mkdir()
    extra = f'[mounts.proj]\nhost = "{proj}"\nguest = "{proj}"\nmode = "rw"\n'   # fake guest fs == host fs
    if collab:
        extra += "[collab]\nlog = true\n"
    cfgfile, ws, _ = write_config(tmp_path, extra=extra)
    text = open(cfgfile).read().replace("[sandbox]\n", '[sandbox]\nmounts = ["proj"]\n', 1)
    open(cfgfile, "w").write(text)
    return cfgfile, ws, proj


@pytest.mark.anyio
async def test_e2e_calls_are_logged_in_the_project_they_touched(tmp_path, fake_docker):
    cfgfile, ws, proj = e2e_config(tmp_path, collab=True)
    async with Session(cfgfile) as c:
        err, out = await c.call("sandbox_create", name="w")
        assert not err, out
        assert (proj / ".collab" / ".gitignore").read_text() == "*\n"      # created up front, by the server
        assert not (proj / ".collab" / "calls.log").exists()
        for tool, args in [
            ("write_file", dict(path=f"{proj}/src/a.txt", content="hello")),
            ("read_file", dict(path=f"{proj}/src/a.txt")),
            ("run_command", dict(command="echo hi", cwd=f"{proj}/src")),
            ("run_command", dict(command="false", cwd=str(proj))),
            ("write_file", dict(path=f"{proj}/.collab/plan.md", content="my notes")),     # the agent's own notes
            ("list_directory", dict(path=str(ws))),                                        # workdir: no writable mapping
        ]:
            err, out = await c.call(tool, sandbox="w", **args)
            assert not err, (tool, out)
    lines = (proj / ".collab" / "calls.log").read_text().splitlines()
    assert [ln.split(" | ")[2] for ln in lines] == ["write_file@w", "read_file@w", "run_command@w", "run_command@w"]
    assert lines[0].split(" | ")[3] == "path=src/a.txt content_bytes=5" and lines[0].split(" | ")[4] == "ok"
    assert lines[2].split(" | ")[3] == 'cwd=src command="echo hi"' and lines[2].split(" | ")[4] == "ok"
    assert lines[3].split(" | ")[3] == "cwd=. command=false" and lines[3].split(" | ")[4] == "exit 1"
    assert (proj / ".collab" / ".gitignore").read_text() == "*\n"
    assert "my notes" not in (proj / ".collab" / "calls.log").read_text()                 # bodies are never logged


@pytest.mark.anyio
async def test_e2e_nothing_is_written_unless_switched_on(tmp_path, fake_docker):
    cfgfile, ws, proj = e2e_config(tmp_path, collab=False)
    async with Session(cfgfile) as c:
        err, out = await c.call("sandbox_create", name="w")
        assert not err, out
        err, out = await c.call("write_file", sandbox="w", path=f"{proj}/a.txt", content="x")
        assert not err, out
    assert not (proj / ".collab").exists()
