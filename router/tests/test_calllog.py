"""Tests for calllog.py: the pure helpers behind the router's call log (no router, no upstream).

Run from the router directory:   python -m pytest tests/test_calllog.py -q
"""
import datetime as dt
import os
import stat
import sys
from pathlib import Path

ROUTER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROUTER_DIR))

import calllog  # noqa: E402

summarize = calllog.summarize_args


# ---- arguments ----------------------------------------------------------------------------------
def test_plain_values_are_bare_and_text_with_spaces_is_quoted():
    assert summarize({"sandbox": "work", "path": "router/router.py"}) == "sandbox=work path=router/router.py"
    assert summarize({"command": "pytest -q"}) == 'command="pytest -q"'


def test_scalars_and_empty():
    assert summarize({"regex": True, "n": 3, "x": None}) == "regex=true n=3 x=null"
    assert summarize(None) == "-" and summarize({}) == "-" and summarize("nope") == "-"


def test_bodies_become_a_size():
    assert summarize({"path": "a.md", "content": "x" * 1656}) == "path=a.md content=<1656 chars>"
    assert summarize({"stdin": "secret input"}) == "stdin=<12 chars>"


def test_edits_show_sizes_not_text():
    out = summarize({"path": "f.py", "edits": [{"old_text": "foo", "new_text": "barbaz"}]})
    assert "foo" not in out and "barbaz" not in out
    assert '"old_text":"<3 chars>"' in out and '"new_text":"<6 chars>"' in out


def test_long_values_are_cut_and_long_collections_counted():
    out = summarize({"command": "a" * 300})
    assert "…(+200)" in out and len(out) < 150
    assert summarize({"paths": ["a" * 30] * 10}) == "paths=<10 items>"
    assert summarize({"cfg": {f"k{i}": "v" * 20 for i in range(10)}}) == "cfg=<10 keys>"


def test_a_value_is_always_one_line():
    out = summarize({"command": "echo a\necho b\r\necho c"})
    assert "\n" not in out and "\r" not in out
    assert "\\n" in out


def test_secret_keys_and_env_are_masked():
    assert summarize({"token": "abc", "GITHUB_TOKEN": "x", "password": "p"}) == "token=*** GITHUB_TOKEN=*** password=***"
    assert summarize({"env": {"A": "1", "B": "2"}}) == "env=<2 entries>"
    assert "hunter2" not in summarize({"opts": {"api_key": "hunter2", "n": 1}})


def test_secrets_inside_text_are_masked():
    for text, leaked in [
        ("GH_TOKEN=abc123 git push", "abc123"),
        ("export PASSWORD='s3cr3t value'", "s3cr3t"),
        ("curl -H 'Authorization: Bearer abcdefghijkl' x", "abcdefghijkl"),
        ("echo ghp_" + "a" * 30, "ghp_"),
        ("echo github_pat_" + "B" * 30, "github_pat_"),
        ("git clone https://user:pw1234@example.com/r.git", "pw1234"),
    ]:
        out = summarize({"command": text})
        assert leaked not in out, (text, out)
    assert "GH_TOKEN=***" in summarize({"command": "GH_TOKEN=abc123 git push"})


# ---- outcome ------------------------------------------------------------------------------------
def test_outcome():
    assert calllog.outcome(False, "hello") == "ok"
    assert calllog.outcome(False, None) == "ok"
    assert calllog.outcome(False, "exit code: 0  (0.1s)\nhi") == "ok"
    assert calllog.outcome(False, "exit code: 2  (0.1s)\nboom") == "exit 2"
    assert calllog.outcome(True, "boom\nsecond\nthird") == "ERR boom second"
    # call_tool returns upstream errors as an ordinary result with this prefix
    assert calllog.outcome(False, "[error from demo.fail]\nit broke") == "ERR [error from demo.fail] it broke"


def test_error_is_one_short_line():
    out = calllog.error("a\nb\n" + "x" * 500)
    assert "\n" not in out and out.startswith("ERR a b ") and len(out) < 200
    assert calllog.error("") == "ERR error"


# ---- the line -----------------------------------------------------------------------------------
NOW = dt.datetime(2026, 11, 11, 20, 31, 7, tzinfo=dt.timezone.utc)


def test_format_line():
    line = calllog.format_line("sandbox.run_command", 'command="pytest -q"', "ok", 1204, chat="3fa9c1", now=NOW)
    assert line == '2026-11-11T20:31:07Z | 3fa9c1 | sandbox.run_command | command="pytest -q" | ok | 1204ms'


def test_format_line_cannot_be_split_or_forged_and_is_bounded():
    line = calllog.format_line("a\nb|c", "x\n2026-01-01T00:00:00Z | forged", "ok\nERR", 1, now=NOW)
    assert "\n" not in line
    assert "a?b?c" in line
    long = calllog.format_line("s.t", "k=" + "v" * 5000, "ok", 1, now=NOW)
    assert len(long) <= calllog.LINE_LIMIT and long.endswith(" | ok | 1ms")


def test_chat_tag():
    assert calllog.chat_tag(None) == "-" and calllog.chat_tag("") == "-"
    tag = calllog.chat_tag("session-1")
    assert len(tag) == 6 and int(tag, 16) >= 0
    assert tag == calllog.chat_tag("session-1") != calllog.chat_tag("session-2")


# ---- ROUTER_CALL_LOG ----------------------------------------------------------------------------
def test_resolve_path():
    default = Path("/repo/.collab/calls.log")
    for off in (None, "", "  ", "off", "OFF", "0", "false", "No", "none"):  # off unless asked for
        assert calllog.resolve_path(off, default) is None
    for on in ("on", "ON", "1", "true", "Yes"):
        assert calllog.resolve_path(on, default) == default
    assert calllog.resolve_path("/tmp/x.log", default) == Path("/tmp/x.log")
    assert calllog.resolve_path("~/x.log", default) == Path(os.path.expanduser("~/x.log"))
    assert calllog.resolve_path("rel.log", default).is_absolute()


# ---- the file -----------------------------------------------------------------------------------
def test_write_creates_folders_keeps_the_file_private_and_appends(tmp_path):
    path = tmp_path / "deep" / "er" / "calls.log"
    log = calllog.CallLog(path)
    assert log.enabled
    log.write("one")
    log.write("two")
    assert path.read_text() == "one\ntwo\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_disabled_log_does_nothing():
    log = calllog.CallLog(None)
    assert not log.enabled
    log.write("x")  # must not raise


def test_deleting_the_file_from_outside_is_fine(tmp_path):
    path = tmp_path / "calls.log"
    log = calllog.CallLog(path)
    log.write("old")
    path.unlink()
    log.write("new")
    assert path.read_text() == "new\n"


def test_rotation_keeps_one_older_file(tmp_path):
    path = tmp_path / "calls.log"
    log = calllog.CallLog(path, max_bytes=50)
    for line in ("a" * 30, "b" * 30, "c" * 30):
        log.write(line)
    assert (tmp_path / "calls.log.1").read_text() == "a" * 30 + "\n" + "b" * 30 + "\n"
    assert path.read_text() == "c" * 30 + "\n"


def test_an_unwritable_log_never_raises_and_complains_once(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("i am a file, not a folder")
    log = calllog.CallLog(blocker / "calls.log")
    log.write("one")
    log.write("two")
    assert capsys.readouterr().err.count("call log") == 1
