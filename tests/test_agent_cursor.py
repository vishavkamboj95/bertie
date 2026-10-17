"""The Cursor adapter: the shared skill, ~/.cursor/hooks.json, the safety hook in Cursor's own
answer format, hook.sh's fallback and install.sh --agent cursor.

HOME is a temporary folder everywhere; no test touches the real one. The attack lists are the
ones tests/test_hook.py holds, so a new way round the tools added there is tested here too."""
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys

import pytest

import agents
import google_cli
import hook
import kit_setup
import registry
from agents import cursor
from conftest import ACCOUNT, KIT, TOOLS
from test_agents_core import (ATTACKS, ARGS, backups, dirs, fake_python, run_script, run_sh, tree,
                              without_claude_leftovers)
from test_hook import FREE
from test_installer import run_install

HOOKS = ".cursor/hooks.json"
SKILL = ".agents/skills/bertie/SKILL.md"


@pytest.fixture
def ctx(home, tmp_path):
    return kit_setup.Ctx(home, tmp_path / "inst")


def hook_cmd(install_dir):
    return "sh " + shlex.quote(str(install_dir / "hook.sh")) + " --agent cursor"


def read_hooks(home):
    return json.loads((home / HOOKS).read_text())


def put_hooks(home, data, indent=2):
    p = home / HOOKS
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=indent) + "\n")
    return p


def manifest(ctx):
    return kit_setup.load_manifest(ctx)


def ask_cursor(payload=None, raw=None):
    return hook.agent_answer("cursor", io.StringIO(raw if raw is not None else json.dumps(payload)))


def shell(command):
    return {"command": command, "cwd": "/work/project"}


def write(content, tool="Write"):
    return {"tool_name": tool, "tool_input": {"path": "/work/notes.md", "contents": content}}


def parse(out):
    d = json.loads(out)
    assert out.endswith("\n") and set(d) <= {"permission", "user_message", "agent_message"}
    return d


@pytest.fixture(autouse=True)
def _no_shape_carries_over_between_tests():
    cursor._SHAPE = None
    yield
    cursor._SHAPE = None


# ================================================================ the interface

def test_the_adapter_has_the_whole_interface():
    assert agents.get("cursor") is cursor
    assert (cursor.KEY, cursor.LABEL, cursor.HOOK_STYLE) == ("cursor", "Cursor", "ask")
    for name in ("detect", "install", "uninstall", "hook_input", "hook_answer", "hook_failure"):
        assert callable(getattr(cursor, name)), name


def test_cursor_is_detected_by_its_config_folder_only(home):
    assert cursor.detect(home) is False
    (home / ".cursor").write_text("a file, not a folder")
    assert cursor.detect(home) is False
    (home / ".cursor").unlink()
    (home / ".cursor").mkdir()
    assert cursor.detect(home) is True
    assert "cursor" in agents.detect_present(home) and "cursor" in agents.default_selection(home)


# ================================================================ install

def test_install_writes_the_shared_skill_and_hooks_json_and_nothing_else(ctx, home):
    lines = cursor.install(ctx, KIT)
    assert (home / SKILL).read_text() == agents.neutral_skill_text((KIT / "skill" / "bertie" / "SKILL.md").read_text())
    assert "claude" not in (home / SKILL).read_text().lower()
    assert stat.S_IMODE((home / HOOKS).stat().st_mode) == 0o600
    cmd = hook_cmd(ctx.install_dir)
    assert read_hooks(home) == {"version": 1, "hooks": {
        "beforeShellExecution": [{"command": cmd, "timeout": 10, "failClosed": True}],
        "preToolUse": [{"command": cmd, "matcher": "Write|Delete", "timeout": 10}],
    }}
    assert shlex.split(cmd) == ["sh", str(ctx.install_dir / "hook.sh"), "--agent", "cursor"]
    assert sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file()) == [SKILL, HOOKS]
    assert not (home / ".claude").exists() and not (home / ".codex").exists() and not (home / ".kimi-code").exists()
    assert len(lines) == 3 and all(l.startswith("Cursor: ") for l in lines)
    assert "Restart Cursor" in lines[-1] and "Touch ID" in lines[-1]


def test_everything_written_is_recorded_in_the_manifest(ctx, home):
    cursor.install(ctx, KIT)
    m = manifest(ctx)
    assert m["shared_skill"]["users"] == ["cursor"]
    rec = m["agents"]["cursor"]["json"][HOOKS]
    assert rec["created_file"] is True and rec["created_dirs"] == [".cursor"]
    assert rec["set"] == [{"path": ["version"], "value": 1}]
    assert [e["path"] for e in rec["added"]] == [["hooks", "beforeShellExecution"], ["hooks", "preToolUse"]]


def test_the_shell_entry_is_fail_closed_and_the_write_entry_is_not(ctx, home):
    cursor.install(ctx, KIT)
    hooks = read_hooks(home)["hooks"]
    assert hooks["beforeShellExecution"][0]["failClosed"] is True
    assert "failClosed" not in hooks["preToolUse"][0] and hooks["preToolUse"][0]["matcher"] == "Write|Delete"


def test_a_cursor_folder_that_was_there_stays_and_unknown_files_in_it_are_not_touched(ctx, home):
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text('{"mcpServers": {}}\n')
    cursor.install(ctx, KIT)
    cursor.uninstall(ctx)
    assert (home / ".cursor" / "mcp.json").read_text() == '{"mcpServers": {}}\n'
    assert not (home / HOOKS).exists() and not (home / ".agents").exists()


USER_HOOKS = {
    "version": 1,
    "hooks": {
        "beforeShellExecution": [{"command": "./audit.sh", "timeout": 5}],
        "preToolUse": [{"command": "./lint.sh", "matcher": "Read"}],
        "stop": [{"command": "./bye.sh"}],
    },
    "other": {"keep": [1, 2]},
}


def test_the_users_own_hooks_survive_and_uninstall_gives_back_the_same_bytes(ctx, home):
    p = put_hooks(home, USER_HOOKS)
    (home / ".cursor" / "rules").mkdir()
    (home / ".cursor" / "rules" / "style.mdc").write_text("be brief\n")
    before, before_dirs = tree(home), dirs(home)
    original = p.read_bytes()
    cursor.install(ctx, KIT)
    got = read_hooks(home)
    assert got["other"] == USER_HOOKS["other"] and got["hooks"]["stop"] == USER_HOOKS["hooks"]["stop"]
    assert got["hooks"]["beforeShellExecution"][0] == USER_HOOKS["hooks"]["beforeShellExecution"][0]
    assert got["hooks"]["beforeShellExecution"][1]["failClosed"] is True
    assert got["hooks"]["preToolUse"][0] == USER_HOOKS["hooks"]["preToolUse"][0]
    assert got["hooks"]["preToolUse"][1]["matcher"] == "Write|Delete"
    (b,) = backups(home / ".cursor")
    assert (home / ".cursor" / b).read_bytes() == original
    lines = cursor.uninstall(ctx)
    assert p.read_bytes() == original and tree(home) == before and dirs(home) == before_dirs
    assert any("removed the safety hook" in l for l in lines) and any("removed the skill" in l for l in lines)
    assert manifest(ctx) == {}


@pytest.mark.parametrize("data", [{}, {"version": 1}, {"hooks": {}}, {"hooks": {"stop": []}}, {"x": 1}])
def test_a_hooks_file_with_little_in_it_comes_back_as_it_was(ctx, home, data):
    p = put_hooks(home, data)
    original = p.read_bytes()
    cursor.install(ctx, KIT)
    cursor.uninstall(ctx)
    assert p.read_bytes() == original


def test_an_empty_hooks_file_comes_back_empty(ctx, home):
    (home / ".cursor").mkdir()
    (home / HOOKS).write_text("")
    cursor.install(ctx, KIT)
    assert read_hooks(home)["version"] == 1
    cursor.uninstall(ctx)
    assert (home / HOOKS).read_text() == ""


def test_the_same_hook_the_user_already_had_is_theirs_and_is_not_removed(ctx, home):
    mine = {"command": hook_cmd(ctx.install_dir), "timeout": 10, "failClosed": True}
    put_hooks(home, {"version": 1, "hooks": {"beforeShellExecution": [mine]}})
    cursor.install(ctx, KIT)
    cursor.uninstall(ctx)
    assert read_hooks(home) == {"version": 1, "hooks": {"beforeShellExecution": [mine]}}


# ---- what it refuses to touch

def test_a_skill_that_is_not_ours_is_never_overwritten_and_nothing_else_is_written(ctx, home):
    foreign = home / SKILL
    foreign.parent.mkdir(parents=True)
    foreign.write_text("---\nname: someone-elses\n---\nTheir text.\n")
    put_hooks(home, USER_HOOKS)
    before = tree(home)
    with pytest.raises(kit_setup.SetupError, match="was not made by this installer"):
        cursor.install(ctx, KIT)
    assert tree(home) == before and manifest(ctx) == {} and backups(home / ".cursor") == []


def test_a_skill_file_of_the_wrong_kind_is_refused_too(ctx, home):
    (home / SKILL).mkdir(parents=True)
    with pytest.raises(kit_setup.SetupError, match="was not made by this installer"):
        cursor.install(ctx, KIT)
    assert not (home / HOOKS).exists() and manifest(ctx) == {}


@pytest.mark.parametrize("raw", ["{not json", "[]", '"text"', "null", "5", '{"hooks": '])
def test_a_hooks_file_that_is_not_a_json_object_is_refused_untouched(ctx, home, raw):
    (home / ".cursor").mkdir()
    (home / HOOKS).write_text(raw)
    before = tree(home)
    with pytest.raises(kit_setup.SetupError, match="hooks.json"):
        cursor.install(ctx, KIT)
    assert tree(home) == before and manifest(ctx) == {} and backups(home / ".cursor") == []
    assert not (home / ".agents").exists()


@pytest.mark.parametrize("version", [2, 0, "1", 1.5, True, None, [1], {"v": 1}])
def test_a_version_other_than_1_is_refused_untouched(ctx, home, version):
    p = put_hooks(home, {"version": version, "hooks": {"stop": [{"command": "x"}]}})
    original = p.read_bytes()
    with pytest.raises(kit_setup.SetupError, match="'version'"):
        cursor.install(ctx, KIT)
    assert p.read_bytes() == original and manifest(ctx) == {} and backups(home / ".cursor") == []
    assert not (home / ".agents").exists()


@pytest.mark.parametrize("data", [{"hooks": []}, {"hooks": "x"}, {"hooks": {"preToolUse": {}}},
                                  {"hooks": {"beforeShellExecution": "no"}}])
def test_a_hooks_file_of_an_unexpected_shape_is_refused_untouched(ctx, home, data):
    p = put_hooks(home, data)
    original = p.read_bytes()
    with pytest.raises(kit_setup.SetupError, match="hooks"):
        cursor.install(ctx, KIT)
    assert p.read_bytes() == original and manifest(ctx) == {} and not (home / ".agents").exists()


def test_a_missing_kit_skill_is_a_clear_error_and_writes_nothing(ctx, home, tmp_path):
    empty = tmp_path / "emptykit"
    empty.mkdir()
    with pytest.raises(kit_setup.SetupError, match="missing"):
        cursor.install(ctx, empty)
    assert not (home / ".cursor").exists() and not (home / ".agents").exists()


# ---- the shared skill

def test_the_shared_skill_stays_while_another_agent_still_records_it(ctx, home):
    assert agents.acquire_shared_skill(ctx, "codex", KIT) is True          # codex got there first
    cursor.install(ctx, KIT)
    assert manifest(ctx)["shared_skill"]["users"] == ["codex", "cursor"]
    lines = cursor.uninstall(ctx)
    assert (home / SKILL).is_file() and manifest(ctx)["shared_skill"]["users"] == ["codex"]
    assert not any("removed the skill" in l for l in lines)
    assert not (home / HOOKS).exists()
    assert agents.release_shared_skill(ctx, "codex") is True
    assert not (home / ".agents").exists()


def test_the_shared_skill_cursor_wrote_goes_when_cursor_is_the_last(ctx, home):
    cursor.install(ctx, KIT)
    cursor.uninstall(ctx)
    assert not (home / ".agents").exists() and "shared_skill" not in manifest(ctx)


def test_installing_after_another_agent_does_not_rewrite_the_skill(ctx, home):
    agents.acquire_shared_skill(ctx, "kimi", KIT)
    mtime = (home / SKILL).stat().st_mtime_ns
    lines = cursor.install(ctx, KIT)
    assert (home / SKILL).stat().st_mtime_ns == mtime and "already had the skill" in lines[0]


# ---- idempotent

def test_installing_twice_changes_nothing_and_makes_no_backup(ctx, home):
    put_hooks(home, USER_HOOKS)
    cursor.install(ctx, KIT)
    after_first = tree(home)
    first_backups = backups(home / ".cursor")
    manifest_first = manifest(ctx)
    lines = cursor.install(ctx, KIT)
    assert tree(home) == after_first and backups(home / ".cursor") == first_backups
    assert manifest(ctx) == manifest_first
    assert "already had" in lines[0] and "already in" in lines[1]
    cursor.uninstall(ctx)
    assert read_hooks(home) == USER_HOOKS


def test_an_entry_of_ours_that_was_edited_since_is_given_back_in_full_and_all_of_it_leaves(ctx, home):
    cursor.install(ctx, KIT)
    data = read_hooks(home)
    del data["hooks"]["beforeShellExecution"][0]["failClosed"]              # someone turned fail-closed off
    put_hooks(home, data)
    cursor.install(ctx, KIT)
    assert any(e.get("failClosed") is True for e in read_hooks(home)["hooks"]["beforeShellExecution"])
    cursor.uninstall(ctx)
    assert not (home / HOOKS).exists()


def test_uninstall_without_an_install_does_nothing(ctx, home):
    put_hooks(home, USER_HOOKS)
    before = tree(home)
    assert cursor.uninstall(ctx) == ["Cursor: nothing to remove"]
    assert tree(home) == before


# ================================================================ the hook: input and answers

def test_hook_input_reads_both_shapes():
    assert cursor.hook_input(shell("git status")) == ("command", "git status")
    assert cursor.hook_input({"command": "   "}) == ("command", "   ")
    assert cursor.hook_input(write("hello")) == ("file", "/work/notes.md\nhello")
    assert cursor.hook_input({"tool_name": "Write", "tool_input": {"edits": [{"new": "a"}, {"new": "b"}]}}) == ("file", "a\nb")
    assert cursor.hook_input({"tool_name": "Write"}) == ("file", "")
    assert cursor.hook_input({"hook_event_name": "stop"}) == ("none", "")
    assert cursor.hook_input({}) == ("none", "")


@pytest.mark.parametrize("data", [{"command": 5}, {"command": None}, {"command": ["a"]}, {"tool_name": 5},
                                  {"tool_name": None, "tool_input": {}}, {"command": {"a": "b"}}])
def test_hook_input_raises_on_what_it_cannot_read(data):
    with pytest.raises(ValueError):
        cursor.hook_input(data)


def test_a_call_with_both_a_tool_and_a_command_has_both_checked_as_a_command():
    assert cursor.hook_input({"tool_name": "Write", "command": "ls", "tool_input": {"a": "b"}}) == ("command", "b\nls")
    out = parse(ask_cursor({"tool_name": "Write", "command": "bertie-mail send r1", "tool_input": {"a": "b"}})[0])
    assert out["permission"] == "deny" and "bertie-mail send" in out["user_message"]      # it cannot ask, so deny
    assert ask_cursor({"tool_name": "Write", "command": "ls", "tool_input": {"a": "b"}}) == ('{"permission": "allow"}\n', 0)


def test_the_answers_are_json_in_cursors_own_format():
    cursor.hook_input(shell("ls"))
    assert cursor.hook_answer(None) == ('{"permission": "allow"}\n', 0)
    cursor.hook_input(shell("ls"))
    text, code = cursor.hook_answer("why")
    assert code == 0 and json.loads(text) == {"permission": "ask", "user_message": "why", "agent_message": "why"}
    cursor.hook_input(write("x"))
    text, code = cursor.hook_answer("why")
    assert code == 0 and json.loads(text) == {"permission": "deny", "user_message": "why", "agent_message": "why"}


def test_a_failure_asks_for_a_shell_command_and_denies_everything_else():
    cursor.hook_input(shell("ls"))
    assert json.loads(cursor.hook_failure("r")[0])["permission"] == "ask"
    cursor.hook_input(write("x"))
    assert json.loads(cursor.hook_failure("r")[0])["permission"] == "deny"
    assert json.loads(cursor.hook_failure("r")[0])["permission"] == "deny"       # nothing read: deny
    assert cursor.hook_failure("r")[1] == 0


def test_one_calls_shape_is_never_read_by_the_next():
    cursor.hook_input(shell("ls"))
    cursor.hook_answer(None)
    assert json.loads(cursor.hook_failure("r")[0])["permission"] == "deny"
    cursor.hook_input(write("x"))
    with pytest.raises(ValueError):
        cursor.hook_input({"command": 5})                                         # a failed read forgets the old shape
    assert json.loads(cursor.hook_failure("r")[0])["permission"] == "deny"


# ================================================================ the hook through hook.py (in this process)

def test_a_gated_send_is_asked_in_cursors_format():
    out, code = ask_cursor(shell("bertie-mail send r1"))
    d = parse(out)
    assert code == 0 and d["permission"] == "ask" and d["user_message"] == d["agent_message"]
    assert "bertie-mail send" in d["user_message"] and d["user_message"].startswith("Bertie safety check: ")


@pytest.mark.parametrize("cmd", ATTACKS)
def test_every_attack_string_is_asked_through_beforeshellexecution(cmd):
    out, code = ask_cursor(shell(cmd))
    assert code == 0 and parse(out)["permission"] == "ask", cmd


@pytest.mark.parametrize("cmd", [c for c in FREE if c.strip()])
def test_harmless_commands_are_allowed(cmd):
    assert ask_cursor(shell(cmd)) == ('{"permission": "allow"}\n', 0), cmd


def test_a_blank_command_and_events_that_are_not_ours_are_allowed():
    allow = ('{"permission": "allow"}\n', 0)
    assert ask_cursor(shell("   ")) == allow and ask_cursor(shell("")) == allow
    assert ask_cursor({}) == allow and ask_cursor({"hook_event_name": "stop"}) == allow


@pytest.mark.parametrize("content", [
    "#!/bin/sh\n$HOME/.config/bertie/.venv/bin/python x.py\n",
    "import svc_mail\nsvc_mail.main(['send', 'r1'])\n",
    "import keyring\nkeyring.get_password('bertie', 'x')",
    "security find-generic-password -s bertie -w",
    "m = __import__('sv' + 'c_mail')",
    "cp x ~/.config/bertie/bertie-approve",
])
def test_writing_bypass_text_is_denied_by_pretooluse_write(content):
    d = parse(ask_cursor(write(content))[0])
    assert d["permission"] == "deny" and "written to a file" in d["user_message"]


def test_the_file_text_in_any_field_is_read():
    payload = {"tool_name": "Write", "tool_input": {"file_path": "/x", "streamContent": [{"line": "import svc_mail"}]}}
    assert parse(ask_cursor(payload)[0])["permission"] == "deny"


@pytest.mark.parametrize("content", ["meeting notes\nship the report", "print('hello')", "bertie-mail search x", ""])
def test_ordinary_file_writes_are_allowed(content):
    assert ask_cursor(write(content)) == ('{"permission": "allow"}\n', 0)


@pytest.mark.parametrize("raw", ["not json", "[]", '"s"', "5", "null", '{"command": 5}', '{"command": null}',
                                 '{"tool_name": 7}', '{"tool_name": "Write", "tool_input": '])
def test_input_that_cannot_be_read_is_denied(raw):
    out, code = ask_cursor(None, raw)
    d = parse(out)
    assert code == 0 and d["permission"] == "deny" and "could not read" in d["user_message"], raw


def test_a_bug_after_a_shell_command_was_read_asks_rather_than_denies(monkeypatch):
    def broken(text):
        raise RuntimeError("bug")
    monkeypatch.setattr(hook, "decide", broken)
    d = parse(ask_cursor(shell("ls"))[0])
    assert d["permission"] == "ask" and "RuntimeError" in d["user_message"]
    monkeypatch.setattr(hook, "decide_file_text", broken)
    assert parse(ask_cursor(write("x"))[0])["permission"] == "deny"


# ================================================================ the hook as a process

CURSOR_SHELL = shell("true && bertie-mail send r1")


def test_hook_py_as_a_process_speaks_cursors_format_and_exits_zero():
    p = run_script(["--agent", "cursor"], json.dumps(CURSOR_SHELL))
    assert p.returncode == 0 and p.stderr == "" and parse(p.stdout)["permission"] == "ask"
    p = run_script(["--agent=cursor"], json.dumps(shell("ls -la")))
    assert p.returncode == 0 and p.stdout == '{"permission": "allow"}\n' and p.stderr == ""
    p = run_script(["--agent", "cursor"], json.dumps(write("import svc_mail")))
    assert p.returncode == 0 and parse(p.stdout)["permission"] == "deny"
    p = run_script(["--agent", "cursor"], json.dumps(write("hello")))
    assert p.returncode == 0 and p.stdout == '{"permission": "allow"}\n'
    p = run_script(["--agent", "cursor"], "this is not json")
    assert p.returncode == 0 and parse(p.stdout)["permission"] == "deny" and p.stderr == ""
    p = run_script(["--agent", "cursor"], "")
    assert p.returncode == 0 and parse(p.stdout)["permission"] == "deny"             # no input at all is a failure


@pytest.mark.parametrize("cmd", ATTACKS)
def test_as_a_process_every_attack_string_is_asked(cmd):
    p = run_script(["--agent", "cursor"], json.dumps(shell(cmd)))
    assert p.returncode == 0 and parse(p.stdout)["permission"] == "ask", cmd


def test_a_broken_adapter_file_is_denied_in_cursors_format(tmp_path):
    d = tmp_path / "installed"
    shutil.copytree(TOOLS, d, ignore=shutil.ignore_patterns("__pycache__"))
    (d / "agents" / "cursor.py").write_text("this is not python (\n")
    p = run_script(["--agent", "cursor"], json.dumps(shell("ls")), script=d / "hook.py")
    assert p.returncode == 0 and parse(p.stdout)["permission"] == "deny"


# ================================================================ hook.sh

def test_hook_sh_without_python_denies_in_cursors_format(tmp_path, home):
    d = tmp_path / "lonely"
    d.mkdir()
    shutil.copy(TOOLS / "hook.sh", d / "hook.sh")
    for args in (["--agent", "cursor"], ["--agent=cursor"]):
        r = run_sh(d, args, json.dumps(shell("bertie-mail send r1")), home, tmp_path)
        out = json.loads(r.stdout)
        assert r.returncode == 0 and r.stderr == "" and r.stdout.endswith("\n")
        assert out["permission"] == "deny" and out["user_message"] == out["agent_message"]
        assert "no working Python" in out["user_message"]


def test_hook_sh_fallback_is_what_hook_py_says_when_it_cannot_load_its_adapter(tmp_path, home):
    d = tmp_path / "lonely"
    d.mkdir()
    shutil.copy(TOOLS / "hook.sh", d / "hook.sh")
    sh = json.loads(run_sh(d, ["--agent", "cursor"], "{}", home, tmp_path).stdout)
    py = json.loads(hook.fallback_answer("cursor", "x"))
    assert list(sh) == list(py) == ["permission", "user_message", "agent_message"]
    assert sh["permission"] == py["permission"] == "deny"
    assert sh["user_message"] == sh["agent_message"] and py["user_message"] == py["agent_message"] == "x"


def test_hook_sh_passes_the_flag_on_and_a_real_answer_through_once(tmp_path, home):
    d = tmp_path / "inst"
    shutil.copytree(TOOLS, d, ignore=shutil.ignore_patterns("__pycache__"))
    log = tmp_path / "calls.log"
    reply = '{"permission": "ask", "user_message": "r", "agent_message": "r"}'
    fake_python(d, log=log, prints=reply)
    r = run_sh(d, ["--agent", "cursor"], json.dumps(CURSOR_SHELL), home, tmp_path)
    assert r.returncode == 0 and r.stdout == reply + "\n"
    assert log.read_text().splitlines() == [f"-I {d}/hook.py --agent cursor"]


def test_hook_sh_with_a_real_python_gives_cursors_answers(tmp_path, home):
    d = tmp_path / "inst"
    shutil.copytree(TOOLS, d, ignore=shutil.ignore_patterns("__pycache__"))
    py = d / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    r = run_sh(d, ["--agent", "cursor"], json.dumps(CURSOR_SHELL), home, tmp_path)
    assert r.returncode == 0 and parse(r.stdout)["permission"] == "ask"
    r = run_sh(d, ["--agent", "cursor"], json.dumps(shell("ls")), home, tmp_path)
    assert r.stdout == '{"permission": "allow"}\n'
    r = run_sh(d, ["--agent", "cursor"], json.dumps(write("cp x ~/.config/bertie/common.py")), home, tmp_path)
    assert parse(r.stdout)["permission"] == "deny"


# ================================================================ install.sh --agent cursor

def test_install_sh_sets_up_cursor_alone_and_the_installed_hook_works(home, tmp_path):
    r = run_install(home, tmp_path, *ARGS, "--agent", "cursor")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "  Cursor: installed the skill 'bertie'" in r.stdout and "  Cursor: safety hook added to ~/.cursor/hooks.json" in r.stdout
    assert "start Cursor, and say:" in r.stdout
    assert not (home / ".claude").exists() and not (home / ".codex").exists() and not (home / ".gemini").exists()
    assert (home / SKILL).is_file() and (home / ".config" / "bertie" / "agents" / "cursor.py").is_file()
    hooks = read_hooks(home)
    assert hooks["hooks"]["beforeShellExecution"][0]["command"] == hook_cmd(home / ".config" / "bertie")
    assert hooks["hooks"]["preToolUse"][0]["command"] == hook_cmd(home / ".config" / "bertie")
    # the command Cursor will run, taken from the file and run as written
    for event, payload, want in (("beforeShellExecution", CURSOR_SHELL, "ask"),
                                 ("beforeShellExecution", shell("ls"), "allow"),
                                 ("preToolUse", write("import svc_mail"), "deny"),
                                 ("preToolUse", write("hello"), "allow")):
        command = hooks["hooks"][event][0]["command"]
        p = subprocess.run(command, shell=True, input=json.dumps(payload), capture_output=True, text=True, timeout=60,
                           env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
        assert p.returncode == 0 and parse(p.stdout)["permission"] == want, (event, p.stdout, p.stderr)
    m = json.loads((home / ".config" / "bertie" / "install-manifest.json").read_text())
    assert "settings" not in m and sorted(m["agents"]) == ["cursor"] and m["shared_skill"]["users"] == ["cursor"]


def test_a_cursor_folder_in_the_home_is_found_and_set_up_with_claude_code(home, tmp_path):
    (home / ".cursor").mkdir()
    r = run_install(home, tmp_path, *ARGS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "start Claude Code or Cursor, and say:" in r.stdout
    assert (home / ".claude" / "settings.json").is_file() and (home / HOOKS).is_file() and (home / SKILL).is_file()


def test_install_sh_leaves_everything_alone_when_the_hooks_file_is_refused(home, tmp_path):
    (home / ".cursor").mkdir()
    (home / HOOKS).write_text('{"version": 2}\n')
    r = run_install(home, tmp_path, *ARGS, "--agent", "cursor")
    assert r.returncode == 1 and "'version'" in r.stderr
    assert (home / HOOKS).read_text() == '{"version": 2}\n' and not (home / ".agents").exists()


def test_install_sh_twice_changes_nothing(home, tmp_path):
    assert run_install(home, tmp_path, *ARGS, "--agent", "cursor").returncode == 0
    before = tree(home)
    r = run_install(home, tmp_path, *ARGS, "--agent", "cursor")
    assert r.returncode == 0 and tree(home) == before
    assert "Cursor: already had the skill" in r.stdout and "Cursor: safety hook already in" in r.stdout


def test_uninstall_after_install_sh_leaves_the_home_as_it_was(home, tmp_path, keychain, monkeypatch, dialog):
    import auth
    import common
    (home / ".zshrc").write_text("alias ll='ls -l'\n")
    put_hooks(home, USER_HOOKS)
    before, before_dirs = tree(home), dirs(home)
    assert run_install(home, tmp_path, *ARGS, "--agent", "cursor").returncode == 0
    assert tree(home) != before
    monkeypatch.setattr(common, "HOME", home / ".config" / "bertie")
    monkeypatch.setattr(auth, "_post", lambda url, data: (200, "{}"))
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps({"refresh_token": "R", "scopes": []}))
    google_cli.main(["uninstall", "--yes"])
    assert tree(home) == before and without_claude_leftovers(dirs(home)) == before_dirs
    assert "what the installer set up for Cursor" in dialog.shown[-1]["message"]


# ================================================================ the review round (C1-C11)

def test_the_event_name_decides_which_shape_a_call_is():
    assert cursor.hook_input({"hook_event_name": "beforeShellExecution", "command": "ls"}) == ("command", "ls")
    assert cursor._SHAPE == "shell"
    assert cursor.hook_input({"hook_event_name": "preToolUse", "tool_name": "Write",
                              "tool_input": {"path": "/x"}}) == ("file", "/x")
    assert cursor._SHAPE == "tool"


@pytest.mark.parametrize("data", [
    {"hook_event_name": "beforeShellExecution"},
    {"hook_event_name": "beforeShellExecution", "cwd": "/x"},
    {"hook_event_name": "beforeShellExecution", "tool_name": "Write", "tool_input": {"a": "b"}},
    {"hook_event_name": "beforeShellExecution", "command": None},
    {"hook_event_name": "preToolUse", "command": "ls"},
    {"hook_event_name": "preToolUse"},
    {"hook_event_name": 5, "command": "ls"},
])
def test_an_event_that_lacks_what_it_always_carries_cannot_be_read(data):
    with pytest.raises(ValueError):
        cursor.hook_input(data)
    assert parse(ask_cursor(data)[0])["permission"] == "deny"


def test_a_shell_event_that_also_names_a_tool_is_a_shell_call_with_both_checked():
    data = {"hook_event_name": "beforeShellExecution", "command": "ls", "tool_name": "Write",
            "tool_input": {"a": "import svc_mail"}}
    kind, text = cursor.hook_input(data)
    assert kind == "command" and "svc_mail" in text and cursor._SHAPE == "shell"
    assert parse(ask_cursor(data)[0])["permission"] == "ask"


@pytest.mark.parametrize("raw", ["", "   ", "\n\n", "\t \n"])
def test_input_that_is_empty_or_blank_is_denied_not_let_through(raw):
    d = parse(ask_cursor(None, raw)[0])
    assert d["permission"] == "deny" and "no hook input" not in d["user_message"] and "could not read" in d["user_message"]


def test_an_empty_json_object_is_still_nothing_to_check():
    assert ask_cursor({}) == ('{"permission": "allow"}\n', 0)


def test_the_failure_wording_follows_the_answer_that_is_given(monkeypatch):
    def broken(text):
        raise RuntimeError("bug")
    monkeypatch.setattr(hook, "decide", broken)
    d = parse(ask_cursor(shell("ls"))[0])
    assert d["permission"] == "ask" and d["user_message"].endswith("asking to be safe")
    monkeypatch.setattr(hook, "decide_file_text", broken)
    d = parse(ask_cursor(write("x"))[0])
    assert d["permission"] == "deny" and d["user_message"].endswith("blocking to be safe")
    d = parse(ask_cursor(None, "not json")[0])
    assert d["permission"] == "deny" and d["user_message"].endswith("blocking to be safe")


def test_failure_blocks_says_what_hook_failure_will_do():
    assert cursor.failure_blocks() is True                            # nothing read yet: deny
    cursor.hook_input(shell("ls"))
    assert cursor.failure_blocks() is False
    cursor.hook_input(write("x"))
    assert cursor.failure_blocks() is True


def test_the_folder_a_command_runs_in_is_part_of_what_is_checked():
    data = {"command": "mv hook.sh h", "cwd": "/h/.config/bertie"}
    assert cursor.hook_scan_text(data, data["command"]) == "cd /h/.config/bertie && mv hook.sh h"
    assert cursor.hook_scan_text({"command": "ls"}, "ls") is None
    assert cursor.hook_scan_text({"command": "ls", "cwd": ""}, "ls") is None
    with pytest.raises(ValueError):
        cursor.hook_scan_text({"command": "ls", "cwd": 5}, "ls")
    d = parse(ask_cursor({"command": "ln -sf /usr/bin/true .venv/bin/python", "cwd": "/h/.config/bertie"})[0])
    assert d["permission"] == "ask"
    assert ask_cursor({"command": "ln -sf /usr/bin/true x", "cwd": "/work/project"}) == ('{"permission": "allow"}\n', 0)


@pytest.mark.parametrize("cmd", [
    "sed -i s/a/b/ ~/.cursor/hooks.json",
    "cd ~/.cursor && rm hooks.json",
    "echo '{}' > $HOME/.cursor/hooks.json",
    "mv ~/.codex/hooks.json /tmp/x",
    "printf x >> ~/.codex/config.toml",
    "rm -r ~/.codex/rules",
    "cp x ~/.gemini/config/hooks.json",
    "tee ~/.gemini/antigravity-cli/settings.json",
    "echo x >> ~/.kimi-code/config.toml",
    "rm -r ~/.agents/skills/bertie",
    "sed -i x ~/.cur''sor/hooks.json",
])
def test_a_command_naming_an_agents_hook_file_is_asked(cmd):
    d = parse(ask_cursor(shell(cmd))[0])
    assert d["permission"] == "ask" and "safety hook is set up" in d["user_message"], cmd


def test_a_command_naming_the_kimi_folder_from_the_environment_is_asked(monkeypatch):
    monkeypatch.setenv("KIMI_CODE_HOME", "/data/kimi")
    d = parse(ask_cursor(shell("sed -i x /data/kimi/config.toml"))[0])
    assert d["permission"] == "ask" and "Kimi Code's settings" in d["user_message"]
    assert ask_cursor(shell("ls /data/kimi")) == ('{"permission": "allow"}\n', 0)


@pytest.mark.parametrize("tool,args", [
    ("Write", {"path": "/h/.cursor/hooks.json", "contents": "{}"}),
    ("Delete", {"path": "/h/.cursor/hooks.json"}),
    ("Write", {"path": "/h/.codex/rules/bertie.rules", "contents": ""}),
    ("Delete", {"path": "/h/.agents/skills/bertie/SKILL.md"}),
])
def test_writing_or_deleting_an_agents_hook_file_is_denied(tool, args):
    d = parse(ask_cursor({"tool_name": tool, "tool_input": args})[0])
    assert d["permission"] == "deny" and "safety hook is set up" in d["user_message"]


def test_the_hook_runs_for_deletes_too(ctx, home):
    cursor.install(ctx, KIT)
    entry = read_hooks(home)["hooks"]["preToolUse"][0]
    assert entry["matcher"] == "Write|Delete" and cursor.WRITE_MATCHER == "Write|Delete"


def test_the_skill_copy_carries_a_mark_and_a_foreign_file_that_only_says_name_bertie_is_refused(ctx, home):
    cursor.install(ctx, KIT)
    text = (home / SKILL).read_text()
    assert agents.NEUTRAL_MARK in text and agents.neutral_skill_is_ours(text)
    cursor.uninstall(ctx)
    foreign = "---\nname: bertie\ndescription: someone else's\n---\nTheir own text.\n"
    (home / SKILL).parent.mkdir(parents=True)
    (home / SKILL).write_text(foreign)
    with pytest.raises(kit_setup.SetupError, match="not made by this installer"):
        cursor.install(ctx, KIT)
    assert (home / SKILL).read_text() == foreign and not (home / HOOKS).exists()


def test_a_hooks_file_that_is_a_link_is_refused_and_nothing_is_written(ctx, home, tmp_path):
    target = tmp_path / "dotfiles.json"
    target.write_text('{"version": 1}\n')
    (home / ".cursor").mkdir()
    (home / HOOKS).symlink_to(target)
    before = tree(home)
    with pytest.raises(kit_setup.SetupError, match="link"):
        cursor.install(ctx, KIT)
    assert tree(home) == before and target.read_text() == '{"version": 1}\n' and not (home / SKILL).exists()


def test_a_dangling_link_where_hooks_json_goes_is_refused_not_followed(ctx, home, tmp_path):
    (home / ".cursor").mkdir()
    (home / HOOKS).symlink_to(tmp_path / "nowhere.json")
    with pytest.raises(kit_setup.SetupError, match="link"):
        cursor.install(ctx, KIT)
    assert (home / HOOKS).is_symlink() and not (tmp_path / "nowhere.json").exists()


def test_the_shared_skill_through_a_linked_agents_folder_is_refused(ctx, home, tmp_path):
    real = tmp_path / "dotfiles" / "agents"
    real.mkdir(parents=True)
    (home / ".agents").symlink_to(real)
    with pytest.raises(kit_setup.SetupError, match="link"):
        cursor.install(ctx, KIT)
    assert list(real.rglob("*")) == [] and not (home / HOOKS).exists()


def test_uninstall_goes_on_when_hooks_json_cannot_be_read_and_keeps_the_record(ctx, home):
    cursor.install(ctx, KIT)
    (home / HOOKS).write_text("{broken")
    lines = cursor.uninstall(ctx)
    assert (home / HOOKS).read_text() == "{broken" and not (home / SKILL).exists()   # the skill still went
    assert any(l.startswith("Cursor: could not remove everything") and HOOKS in l for l in lines)
    assert "cursor" in manifest(ctx)["agents"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_uninstall_keeps_the_install_folder_while_hooks_json_still_names_the_hook(ctx, home):
    cursor.install(ctx, KIT)
    folder = home / ".cursor"
    folder.chmod(0o555)                                  # hooks.json cannot be rewritten
    try:
        lines = kit_setup.uninstall_local(ctx)
        left = agents.hook_leftovers(ctx)
        notes = kit_setup.leftover_notes(ctx)
    finally:
        folder.chmod(0o755)
    assert any(l.startswith("Cursor: could not remove everything") for l in lines)
    assert [(label, where) for label, where, _ in left] == [("Cursor", "~/" + HOOKS)]
    assert "hook.sh" in (home / HOOKS).read_text() and cursor.KEY in manifest(ctx)["agents"]
    assert notes[0].startswith("Not everything could be taken out") and "KEPT" in notes[0]
    assert any("~/.cursor/hooks.json" in n for n in notes) and "delete the folder" in notes[-1]
