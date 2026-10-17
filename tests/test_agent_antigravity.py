"""The Antigravity adapter: what it writes where, that it never overwrites what is not its own,
that uninstall gives the home back byte for byte, and the safety hook run as a real process
with Antigravity's own stdin JSON. HOME is a temporary folder everywhere."""
import json
import shlex
import shutil
import subprocess
import sys

import pytest

import agents
import hook
import kit_setup
import registry
from conftest import ACCOUNT, KIT, TOOLS
from test_agents_core import ARGS, ATTACKS, dirs, run_script, run_sh, tree, without_claude_leftovers
from test_hook import FREE, GATED
from test_installer import env_for, run_install

antigravity = agents.get("antigravity")

APP_SKILL = ".gemini/config/skills/bertie/SKILL.md"
CLI_SKILL = ".gemini/antigravity-cli/skills/bertie/SKILL.md"
SETTINGS = ".gemini/antigravity-cli/settings.json"
HOOKS = ".gemini/config/hooks.json"


@pytest.fixture
def ctx(home, tmp_path):
    return kit_setup.Ctx(home, tmp_path / "inst")


def put(home, rel, text):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def read(home, rel):
    return json.loads((home / rel).read_text())


def backups(home):
    return sorted(p.name for p in home.rglob("*.bertie-backup-*"))


# ---------------------------------------------------------------- the module

def test_the_adapter_has_the_whole_interface():
    assert (antigravity.KEY, antigravity.LABEL, antigravity.HOOK_STYLE) == ("antigravity", "Antigravity", "ask")
    for name in ("detect", "install", "uninstall", "hook_input", "hook_answer", "hook_failure"):
        assert callable(getattr(antigravity, name)), name
    assert not hasattr(antigravity, "rules_cover")                    # ask-style: the hook can ask itself


def test_detection_looks_only_at_antigravitys_own_folders_under_gemini(home):
    assert antigravity.detect(home) is False
    (home / ".claude").mkdir()
    (home / ".cursor").mkdir()
    assert antigravity.detect(home) is False
    (home / ".gemini").mkdir()                                        # shared with Google's other tools: not enough
    assert antigravity.detect(home) is False
    (home / ".gemini" / "config").mkdir()
    assert antigravity.detect(home) is True
    found = agents.detect_present(home)                               # adapters not yet in this copy are skipped
    assert "claude" in found and "antigravity" in found and found.index("claude") < found.index("antigravity")


def test_a_gemini_file_is_not_the_agent(home):
    (home / ".gemini").write_text("x")
    assert antigravity.detect(home) is False


def test_the_ask_rules_name_every_gated_action_word_by_word():
    rules = antigravity.ask_rules()
    assert "command(bertie-mail send)" in rules and "command(bertie-chat dm)" in rules
    assert len(rules) == len(registry.ask_rules()) == len(set(rules))
    assert all(r.startswith("command(") and r.endswith(")") and ":" not in r and "*" not in r for r in rules)


# ---------------------------------------------------------------- install

def test_files_land_where_antigravity_reads_them(ctx, home):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    msgs = antigravity.install(ctx, KIT)
    # both skills, neutral and ours
    for rel in (APP_SKILL, CLI_SKILL):
        text = (home / rel).read_text()
        assert text.startswith("---\nname: bertie\ndescription: ") and "claude" not in text.lower(), rel
        assert text == agents.neutral_skill_text(agents.kit_skill_text(KIT))
    # ask rules in the CLI's settings
    assert read(home, SETTINGS) == {"permissions": {"ask": antigravity.ask_rules()}}
    # the hook, under our own key, with the real path of hook.sh
    cmd = "sh " + shlex.quote(str(ctx.install_dir / "hook.sh")) + " --agent antigravity"
    assert read(home, HOOKS) == {"bertie": {"PreToolUse": [{
        "matcher": "run_command|write_to_file|replace_file_content|multi_replace_file_content|manage_task",
        "hooks": [{"type": "command", "command": cmd, "timeout": 10}]}]}}
    assert all(m.startswith("Antigravity: ") for m in msgs)
    assert "approval rules added to ~/" + SETTINGS in " ".join(msgs)
    assert "Touch ID" in msgs[-1]


def test_the_hook_command_runs_hook_sh_with_the_agent_flag(ctx, home):
    antigravity.install(ctx, KIT)
    cmd = read(home, HOOKS)["bertie"]["PreToolUse"][0]["hooks"][0]["command"]
    assert shlex.split(cmd) == ["sh", str(ctx.install_dir / "hook.sh"), "--agent", "antigravity"]


def test_everything_is_recorded_in_the_manifest(ctx, home):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    antigravity.install(ctx, KIT)
    sec = kit_setup.load_manifest(ctx)["agents"]["antigravity"]
    assert sorted(sec["files"]) == sorted([APP_SKILL, CLI_SKILL])
    assert sorted(sec["json"]) == sorted([SETTINGS, HOOKS])
    assert sec["json"][HOOKS]["set"][0]["path"] == ["bertie"]


def test_without_the_cli_folder_there_are_no_ask_rules_and_it_says_so(ctx, home):
    msgs = antigravity.install(ctx, KIT)
    assert not (home / SETTINGS).exists()
    assert (home / APP_SKILL).is_file() and (home / CLI_SKILL).is_file() and (home / HOOKS).is_file()
    assert any("no approval rules were added" in m for m in msgs)
    assert "approval rules in the CLI" not in msgs[-1]


def test_the_cli_folder_made_by_the_skill_copy_does_not_start_ask_rules_on_a_second_run(ctx, home):
    antigravity.install(ctx, KIT)
    before = tree(home)
    antigravity.install(ctx, KIT)
    assert tree(home) == before and not (home / SETTINGS).exists()


def test_a_cli_folder_that_was_there_gets_rules_on_every_run_and_stays_afterwards(ctx, home):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    antigravity.install(ctx, KIT)
    antigravity.install(ctx, KIT)
    assert read(home, SETTINGS)["permissions"]["ask"] == antigravity.ask_rules()
    antigravity.uninstall(ctx)
    assert (home / ".gemini" / "antigravity-cli").is_dir()
    assert not (home / SETTINGS).exists()


def test_existing_settings_and_hooks_survive_and_uninstall_restores_the_home_byte_for_byte(ctx, home):
    # written the way json.dump(indent=4) writes it: the installer re-writes JSON in the file's own indent
    settings = json.dumps({"theme": "dark", "permissions": {"ask": ["command(rm -rf)"], "allow": ["command(ls)"]}},
                          indent=4) + "\n"
    hooks = json.dumps({"mine": {"PreToolUse": [{"matcher": "run_command", "hooks": [
        {"type": "command", "command": "sh /x/mine.sh"}]}]}, "other": 5}, indent=2)
    put(home, SETTINGS, settings)
    put(home, HOOKS, hooks)
    (home / ".gemini" / "config" / "notes.txt").write_text("keep me")
    before, before_dirs = tree(home), dirs(home)
    antigravity.install(ctx, KIT)
    s, h = read(home, SETTINGS), read(home, HOOKS)
    assert s["theme"] == "dark" and s["permissions"]["allow"] == ["command(ls)"]
    assert (home / SETTINGS).read_text().startswith('{\n    "theme"')                         # indent kept
    assert s["permissions"]["ask"] == ["command(rm -rf)"] + antigravity.ask_rules()
    assert h["mine"] == json.loads(hooks)["mine"] and h["other"] == 5 and "bertie" in h
    assert len(backups(home)) == 2                                    # one for each file edited
    antigravity.uninstall(ctx)
    assert tree(home) == before and dirs(home) == before_dirs
    assert (home / SETTINGS).read_text() == settings and (home / HOOKS).read_text() == hooks


def test_a_rule_the_user_already_had_is_theirs_and_stays(ctx, home):
    mine = "command(bertie-mail send)"
    put(home, SETTINGS, json.dumps({"permissions": {"ask": [mine]}}))
    antigravity.install(ctx, KIT)
    antigravity.uninstall(ctx)
    assert read(home, SETTINGS) == {"permissions": {"ask": [mine]}}


def test_uninstall_of_a_fresh_install_leaves_no_trace_at_all(ctx, home):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    before, before_dirs = tree(home), dirs(home)
    antigravity.install(ctx, KIT)
    msgs = antigravity.uninstall(ctx)
    assert tree(home) == before and dirs(home) == before_dirs
    assert "antigravity" not in kit_setup.load_manifest(ctx).get("agents", {})
    assert any("removed the safety hook" in m for m in msgs) and any("removed the skill" in m for m in msgs)


def test_uninstall_of_an_install_that_made_the_gemini_folder_removes_it_again(ctx, home):
    antigravity.install(ctx, KIT)
    assert (home / ".gemini").is_dir()
    antigravity.uninstall(ctx)
    assert not (home / ".gemini").exists()


def test_uninstall_with_nothing_installed_changes_nothing(ctx, home):
    put(home, HOOKS, '{"bertie": {"mine": true}}')                     # not recorded, so not ours to remove
    before = tree(home)
    assert antigravity.uninstall(ctx) == ["Antigravity: nothing to remove"]
    assert tree(home) == before


def test_a_reinstall_changes_nothing_and_makes_no_second_backup(ctx, home):
    put(home, HOOKS, '{"mine": 1}\n')
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    put(home, SETTINGS, '{"a": 1}\n')
    antigravity.install(ctx, KIT)
    before, made = tree(home), backups(home)
    manifest = (ctx.install_dir / "install-manifest.json").read_bytes()
    msgs = antigravity.install(ctx, KIT)
    assert tree(home) == before and backups(home) == made
    assert (ctx.install_dir / "install-manifest.json").read_bytes() == manifest
    assert "already in" in " ".join(msgs) and " added to " not in " ".join(msgs)


def test_a_reinstall_with_a_moved_install_folder_updates_our_hook_in_place(home, tmp_path):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    antigravity.install(kit_setup.Ctx(home, tmp_path / "one"), KIT)
    two = kit_setup.Ctx(home, tmp_path / "two")
    # the same manifest is found through the install folder, so carry it over
    shutil.copytree(tmp_path / "one", tmp_path / "two")
    antigravity.install(two, KIT)
    cmd = read(home, HOOKS)["bertie"]["PreToolUse"][0]["hooks"][0]["command"]
    assert str(tmp_path / "two") in cmd and str(tmp_path / "one") not in cmd


# ---------------------------------------------------------------- refusals

def test_a_bertie_key_that_is_not_ours_stops_the_install_and_nothing_is_touched(ctx, home):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    put(home, SETTINGS, '{"a": 1}')
    put(home, HOOKS, '{"bertie": {"PreToolUse": []}}')
    before = tree(home)
    with pytest.raises(agents.SetupError, match="'bertie' already exists and was not added by this installer"):
        antigravity.install(ctx, KIT)
    assert tree(home) == before and backups(home) == []
    assert not (home / APP_SKILL).exists() and not (home / CLI_SKILL).exists()   # not even the skills
    assert "agents" not in kit_setup.load_manifest(ctx)


@pytest.mark.parametrize("rel", [APP_SKILL, CLI_SKILL])
@pytest.mark.parametrize("text", ["---\nname: another-tool\n---\nhi\n", "just notes\n", ""])
def test_a_skill_that_is_not_ours_is_never_overwritten(ctx, home, rel, text):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    put(home, rel, text)
    before = tree(home)
    with pytest.raises(agents.SetupError, match="was not made by this installer"):
        antigravity.install(ctx, KIT)
    assert tree(home) == before and backups(home) == []
    assert not (home / HOOKS).exists()                                 # refused before anything was written


def test_a_symlinked_skill_is_not_ours(ctx, home, tmp_path):
    target = tmp_path / "elsewhere.md"
    target.write_text("---\nname: bertie\n---\n")
    link = home / APP_SKILL
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    with pytest.raises(agents.SetupError):
        antigravity.install(ctx, KIT)
    assert target.read_text() == "---\nname: bertie\n---\n"


@pytest.mark.parametrize("rel", [SETTINGS, HOOKS])
@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '"s"'])
def test_json_that_cannot_be_read_is_refused_untouched(ctx, home, rel, text):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    put(home, rel, text)
    before = tree(home)
    with pytest.raises(agents.SetupError):
        antigravity.install(ctx, KIT)
    assert tree(home) == before and backups(home) == [] and not (home / APP_SKILL).exists()


def test_a_permissions_entry_of_the_wrong_kind_is_refused_untouched(ctx, home):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    put(home, SETTINGS, '{"permissions": {"ask": "all"}}')
    before = tree(home)
    with pytest.raises(agents.SetupError, match="not a list"):
        antigravity.install(ctx, KIT)
    assert tree(home) == before and not (home / HOOKS).exists()


def test_our_own_hooks_key_from_an_earlier_install_is_not_a_conflict(ctx, home):
    antigravity.install(ctx, KIT)
    antigravity.install(ctx, KIT)                                      # the "bertie" key is there, and recorded
    assert "bertie" in read(home, HOOKS)


def test_uninstall_leaves_a_skill_the_user_swapped_in_since(ctx, home):
    antigravity.install(ctx, KIT)
    (home / APP_SKILL).write_text("---\nname: mine\n---\n")
    antigravity.uninstall(ctx)
    assert (home / APP_SKILL).read_text() == "---\nname: mine\n---\n"
    assert not (home / CLI_SKILL).exists()


# ---------------------------------------------------------------- hook_input and the answers

def call(name, **args):
    return {"toolCall": {"name": name, "args": args}, "hookEventName": "PreToolUse"}


def test_a_run_command_is_a_command_and_the_file_tools_are_files():
    # round 3 (F6): Cwd is a folder, read as one (hook_scan_text, only where it can matter), not a line of text
    assert antigravity.hook_input(call("run_command", CommandLine="ls -la", Cwd="/tmp")) == ("command", "ls -la")
    kind, text = antigravity.hook_input(call("write_to_file", TargetFile="/x.py", CodeContent="print(1)"))
    assert kind == "file" and "print(1)" in text and "/x.py" in text
    kind, text = antigravity.hook_input(call("replace_file_content", TargetFile="/x", TargetContent="a",
                                             ReplacementContent="b"))
    assert kind == "file" and "a" in text and "b" in text
    kind, text = antigravity.hook_input(call("multi_replace_file_content", TargetFile="/x", ReplacementChunks=[
        {"TargetContent": "old", "ReplacementContent": "cp x ~/.config/bertie/common.py"}]))
    assert kind == "file" and "cp x ~/.config/bertie/common.py" in text


def test_other_tools_are_not_ours_to_watch():
    assert antigravity.hook_input(call("view_file", AbsolutePath="/x")) == ("none", "")
    assert antigravity.hook_input(call("list_dir")) == ("none", "")


def test_a_blank_command_is_nothing_to_check_not_an_error():
    assert antigravity.hook_input(call("run_command", CommandLine="")) == ("command", "")


@pytest.mark.parametrize("data", [
    {}, {"toolCall": None}, {"toolCall": "run_command"}, {"toolCall": {}}, {"toolCall": {"name": 5}},
    {"toolCall": {"name": "run_command"}}, {"toolCall": {"name": "run_command", "args": "ls"}},
    {"toolCall": {"name": "run_command", "args": {}}}, {"toolCall": {"name": "run_command", "args": {"CommandLine": 5}}},
    {"toolCall": {"name": "run_command", "args": {"Cwd": "/tmp"}}},
])
def test_anything_unreadable_raises_so_the_hook_asks(data):
    with pytest.raises(Exception):
        antigravity.hook_input(data)


def test_a_command_hidden_outside_the_command_line_field_is_still_read():
    kind, text = antigravity.hook_input(call("run_command", CommandLine="ls", Extra="bertie-mail send r1"))
    assert kind == "command" and "bertie-mail send r1" in text


def test_the_answers():
    assert antigravity.hook_answer(None) == ('{"decision": "allow"}\n', 0)
    out, code = antigravity.hook_answer("because")
    assert code == 0 and json.loads(out) == {"decision": "force_ask", "reason": "because"} and out.endswith("\n")
    out, code = antigravity.hook_failure("broken")
    assert code == 0 and json.loads(out) == {"decision": "force_ask", "reason": "broken"}


# ---------------------------------------------------------------- hook.py --agent antigravity, as a process

def run_hook(payload=None, raw=None, script=None):
    p = run_script(["--agent", "antigravity"], raw if raw is not None else json.dumps(payload), script=script)
    assert p.returncode == 0 and p.stderr == "", p.stderr
    return json.loads(p.stdout)


def force_asked(out):
    return out.get("decision") == "force_ask" and bool(out.get("reason"))


def test_a_gated_send_is_force_asked_with_bertie_s_reason():
    out = run_hook(call("run_command", CommandLine="bertie-mail send r123"))
    assert force_asked(out) and "Bertie safety check" in out["reason"] and "bertie-mail send" in out["reason"]
    assert set(out) == {"decision", "reason"}


def test_a_harmless_command_is_allowed():
    assert run_hook(call("run_command", CommandLine="ls -la", Cwd="/tmp/project")) == {"decision": "allow"}


def test_an_unwatched_tool_is_allowed():
    assert run_hook(call("view_file", AbsolutePath="/etc/hosts")) == {"decision": "allow"}


@pytest.mark.parametrize("cmd", ATTACKS)
def test_every_attack_string_is_force_asked(cmd):
    assert force_asked(run_hook(call("run_command", CommandLine=cmd))), cmd


@pytest.mark.parametrize("cmd", [c for c in FREE if c.strip()])
def test_harmless_commands_are_allowed_too(cmd):
    assert run_hook(call("run_command", CommandLine=cmd)) == {"decision": "allow"}, cmd


@pytest.mark.parametrize("cmd", GATED[:3])
def test_the_hook_in_process_agrees_with_the_one_in_the_subprocess(cmd):
    import io
    payload = json.dumps(call("run_command", CommandLine=cmd))
    text, code = hook.agent_answer("antigravity", io.StringIO(payload))
    assert code == 0 and json.loads(text) == run_hook(raw=payload)


def test_file_writes_naming_the_bypass_phrases_are_force_asked():
    for name, args in (
        ("write_to_file", {"TargetFile": "/tmp/x.sh", "CodeContent": "cp x ~/.config/bertie/common.py"}),
        ("replace_file_content", {"TargetFile": "/tmp/x", "TargetContent": "a",
                                  "ReplacementContent": "python ~/.config/bertie/svc_mail.py send r1"}),
        ("multi_replace_file_content", {"TargetFile": "/tmp/x", "ReplacementChunks": [
            {"TargetContent": "a", "ReplacementContent": "security find-generic-password -s bertie -w"}]}),
    ):
        out = run_hook(call(name, **args))
        assert force_asked(out) and "written to a file" in out["reason"], name


def test_ordinary_file_writes_are_allowed():
    assert run_hook(call("write_to_file", TargetFile="/tmp/a.py", CodeContent="print('hello')")) == {"decision": "allow"}
    assert run_hook(call("multi_replace_file_content", TargetFile="/tmp/a.py", ReplacementChunks=[
        {"TargetContent": "a", "ReplacementContent": "b"}])) == {"decision": "allow"}


def test_a_screen_style_tool_name_is_not_special_here():
    assert run_hook(call("mcp__computer-use__screenshot")) == {"decision": "allow"}


@pytest.mark.parametrize("raw", ["not json", "{", "[]", '"s"', "5", "null", "{}", '{"toolCall": {}}',
                                 '{"toolCall": {"name": "run_command"}}',
                                 '{"toolCall": {"name": "run_command", "args": {"CommandLine": null}}}'])
def test_bad_or_unreadable_input_is_force_asked(raw):
    out = run_hook(raw=raw)
    assert force_asked(out), raw


def test_empty_input_is_force_asked_not_let_through():
    assert force_asked(run_hook(raw=""))


def test_input_that_is_not_utf8_is_force_asked():
    p = subprocess.run([sys.executable, "-I", str(TOOLS / "hook.py"), "--agent", "antigravity"],
                       input=b"\xff\xfe{", capture_output=True, timeout=30)
    assert p.returncode == 0 and force_asked(json.loads(p.stdout))


def test_a_broken_adapter_file_still_gets_the_antigravity_format(tmp_path):
    d = tmp_path / "installed"
    shutil.copytree(TOOLS, d, ignore=shutil.ignore_patterns("__pycache__"))
    (d / "agents" / "antigravity.py").write_text("this is not python (\n")
    out = run_hook(call("run_command", CommandLine="ls"), script=d / "hook.py")
    assert force_asked(out) and "asking to be safe" in out["reason"]


def test_a_missing_adapter_file_still_gets_the_antigravity_format(tmp_path):
    d = tmp_path / "installed"
    shutil.copytree(TOOLS, d, ignore=shutil.ignore_patterns("__pycache__"))
    (d / "agents" / "antigravity.py").unlink()
    out = run_hook(call("run_command", CommandLine="ls"), script=d / "hook.py")
    assert force_asked(out)


# ---------------------------------------------------------------- hook.sh

@pytest.fixture
def lonely(tmp_path):
    """An install folder with hook.sh and nothing else: no hook.py, so no Python can run it."""
    d = tmp_path / "lonely"
    d.mkdir()
    shutil.copy(TOOLS / "hook.sh", d / "hook.sh")
    return d


def test_hook_sh_without_python_makes_antigravity_ask(lonely, home, tmp_path):
    r = run_sh(lonely, ["--agent", "antigravity"], json.dumps(call("run_command", CommandLine="ls")), home, tmp_path)
    out = json.loads(r.stdout)
    assert r.returncode == 0 and r.stderr == "" and list(out) == ["decision", "reason"]
    assert out["decision"] == "force_ask" and "no working Python" in out["reason"]


def test_the_fallback_matches_what_hook_py_says_when_the_adapter_is_gone():
    py = json.loads(hook.fallback_answer("antigravity", "x"))
    assert py == {"decision": "force_ask", "reason": "x"}


def test_hook_sh_with_a_real_python_gives_the_full_answer(home, tmp_path):
    d = tmp_path / "installed"
    shutil.copytree(TOOLS, d, ignore=shutil.ignore_patterns("__pycache__"))
    py = d / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    r = run_sh(d, ["--agent", "antigravity"], json.dumps(call("run_command", CommandLine="true && bertie-mail send r1")),
               home, tmp_path)
    assert r.returncode == 0 and force_asked(json.loads(r.stdout)) and "bertie-mail send" in r.stdout
    r = run_sh(d, ["--agent", "antigravity"], json.dumps(call("run_command", CommandLine="ls")), home, tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout) == {"decision": "allow"}


# ---------------------------------------------------------------- install.sh --agent antigravity

def installed_dir(home):
    return home / ".config" / "bertie"


def test_install_sh_sets_up_antigravity_end_to_end_and_the_installed_hook_works(home, tmp_path):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    r = run_install(home, tmp_path, *ARGS, "--agent", "antigravity")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "  Antigravity: installed the skill 'bertie'" in r.stdout
    assert "  Antigravity: approval rules added to ~/.gemini/antigravity-cli/settings.json" in r.stdout
    assert "  Antigravity: safety hook added to ~/.gemini/config/hooks.json" in r.stdout
    assert "start Antigravity, and say:" in r.stdout
    assert (home / APP_SKILL).is_file() and (home / CLI_SKILL).is_file()
    assert read(home, SETTINGS)["permissions"]["ask"] == antigravity.ask_rules()
    assert not (home / ".claude").exists() and not (home / ".agents").exists()          # claude not selected
    # the files the installer copied include the adapter, and the hook command it wrote runs
    assert (installed_dir(home) / "agents" / "antigravity.py").is_file()
    cmd = read(home, HOOKS)["bertie"]["PreToolUse"][0]["hooks"][0]["command"]
    p = subprocess.run(cmd, shell=True, env=env_for(home, tmp_path), capture_output=True, text=True, timeout=60,
                       input=json.dumps(call("run_command", CommandLine="x; bertie-mail send r1")))
    assert p.returncode == 0 and force_asked(json.loads(p.stdout)), p.stdout + p.stderr
    p = subprocess.run(cmd, shell=True, env=env_for(home, tmp_path), capture_output=True, text=True, timeout=60,
                       input=json.dumps(call("run_command", CommandLine="ls")))
    assert json.loads(p.stdout) == {"decision": "allow"}


def test_install_sh_without_the_cli_folder_adds_no_rules(home, tmp_path):
    r = run_install(home, tmp_path, *ARGS, "--agent", "antigravity")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (home / SETTINGS).exists() and (home / HOOKS).is_file()
    assert "no approval rules were added" in r.stdout


def test_install_sh_finds_antigravity_by_its_folder_when_told_nothing(home, tmp_path):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    r = run_install(home, tmp_path, *ARGS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Done. Open a NEW terminal, start Claude Code or Antigravity, and say:" in r.stdout
    assert (home / ".claude" / "settings.json").is_file() and (home / HOOKS).is_file()


def test_install_sh_refuses_a_foreign_hooks_key_and_leaves_the_file_alone(home, tmp_path):
    put(home, HOOKS, '{"bertie": {"mine": true}}')
    before = tree(home)
    r = run_install(home, tmp_path, *ARGS, "--agent", "antigravity")
    assert r.returncode == 1 and "'bertie' already exists and was not added by this installer" in r.stderr
    assert (home / HOOKS).read_text() == '{"bertie": {"mine": true}}'
    assert not (home / APP_SKILL).exists()
    assert before == {k: v for k, v in tree(home).items() if k in before}


def test_install_sh_rerun_changes_nothing_and_uninstall_restores_the_home(home, tmp_path, keychain, monkeypatch, dialog):
    import auth
    import common
    import google_cli
    put(home, HOOKS, json.dumps({"mine": {"x": 1}}, indent=2) + "\n")
    put(home, SETTINGS, json.dumps({"theme": "dark"}, indent=2) + "\n")
    (home / ".zshrc").write_text("alias ll='ls -l'\n")
    before, before_dirs = tree(home), dirs(home)
    args = (*ARGS, "--agent", "antigravity")
    assert run_install(home, tmp_path, *args).returncode == 0
    after = tree(home)
    assert after != before
    assert run_install(home, tmp_path, *args).returncode == 0
    assert tree(home) == after
    monkeypatch.setattr(common, "HOME", installed_dir(home))
    monkeypatch.setattr(auth, "_post", lambda url, data: (200, "{}"))
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps({"refresh_token": "R", "scopes": []}))
    google_cli.main(["uninstall", "--yes"])
    assert tree(home) == before and without_claude_leftovers(dirs(home)) == before_dirs
    assert "what the installer set up for Antigravity" in dialog.shown[-1]["message"]


# ================================================================ the review round (A1, A3, A5, A6, A11, C4, A2)

@pytest.mark.parametrize("sub", ["config", "antigravity-cli", "antigravity"])
def test_each_of_antigravitys_own_folders_is_enough_to_detect_it(home, sub):
    (home / ".gemini" / sub).mkdir(parents=True)
    assert antigravity.detect(home) is True


def test_a_file_named_like_one_of_its_folders_is_not_the_agent(home):
    (home / ".gemini").mkdir()
    (home / ".gemini" / "config").write_text("x")
    assert antigravity.detect(home) is False


def test_manage_task_is_hooked(ctx, home):
    antigravity.install(ctx, KIT)
    matcher = read(home, HOOKS)["bertie"]["PreToolUse"][0]["matcher"]
    assert "manage_task" in matcher.split("|") and antigravity.MATCHER == matcher


def test_text_typed_into_a_running_terminal_is_checked_like_a_command():
    task = call("manage_task", Action="send_input", TaskId="t1", Input="bertie-mail send r1\n")
    assert antigravity.hook_input(task) == ("command", "bertie-mail send r1\n")
    assert force_asked(run_hook(task)) and "bertie-mail send" in run_hook(task)["reason"]
    hidden = call("manage_task", Action="send_input", TaskId="t1", Input="b''ertie-mail se\"\"nd r1\n")
    assert force_asked(run_hook(hidden))


def test_other_task_actions_and_harmless_input_are_let_through():
    assert run_hook(call("manage_task", Action="list")) == {"decision": "allow"}
    assert run_hook(call("manage_task", Action="kill", TaskId="t1")) == {"decision": "allow"}
    assert run_hook(call("manage_task", Action="send_input", TaskId="t1", Input="y\n")) == {"decision": "allow"}


@pytest.mark.parametrize("args", [{}, {"Action": 5}, {"Action": "send_input"}, {"Action": "send_input", "Input": 5},
                                  {"Action": "send_input", "Input": None}, {"Action": "send_input", "Input": ["x"]}])
def test_a_task_call_that_cannot_be_read_is_force_asked(args):
    with pytest.raises(ValueError):
        antigravity.hook_input(call("manage_task", **args))
    assert force_asked(run_hook(call("manage_task", **args)))


def test_a_manage_task_call_without_args_is_force_asked():
    assert force_asked(run_hook({"toolCall": {"name": "manage_task"}}))


@pytest.mark.parametrize("name", ["write_to_file", "replace_file_content", "multi_replace_file_content"])
@pytest.mark.parametrize("args", [{}, {"TargetFile": None}])
def test_a_file_tool_with_nothing_to_read_is_force_asked(name, args):
    with pytest.raises(ValueError):
        antigravity.hook_input(call(name, **args))
    assert force_asked(run_hook(call(name, **args)))
    assert force_asked(run_hook({"toolCall": {"name": name}}))


def test_a_cli_installed_after_ours_gets_its_rules_on_the_next_install(ctx, home):
    msgs = antigravity.install(ctx, KIT)
    assert not (home / SETTINGS).exists()
    assert any("no CLI settings file" in m and "run the installer again" in m for m in msgs)
    assert kit_setup.load_manifest(ctx)["agents"]["antigravity"]["cli_before"] is False
    put(home, SETTINGS, '{"theme": "dark"}\n')                       # the CLI starts, and makes its settings file
    msgs = antigravity.install(ctx, KIT)
    assert read(home, SETTINGS) == {"theme": "dark", "permissions": {"ask": antigravity.ask_rules()}}
    assert any("approval rules added to" in m for m in msgs)
    antigravity.uninstall(ctx)
    assert read(home, SETTINGS) == {"theme": "dark"}


def test_the_folder_our_own_skill_copy_made_is_not_the_clis(ctx, home):
    antigravity.install(ctx, KIT)
    assert (home / ".gemini" / "antigravity-cli").is_dir()           # made for the CLI skill
    antigravity.install(ctx, KIT)
    assert not (home / SETTINGS).exists()                            # still no rules: no CLI settings file


def test_a_cli_folder_that_was_there_first_is_remembered(ctx, home):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    antigravity.install(ctx, KIT)
    assert kit_setup.load_manifest(ctx)["agents"]["antigravity"]["cli_before"] is True
    antigravity.install(ctx, KIT)
    assert read(home, SETTINGS) == {"permissions": {"ask": antigravity.ask_rules()}}


@pytest.mark.parametrize("cmd", [
    "sed -i s/x/y/ ~/.gemini/config/hooks.json",
    "cd ~/.gemini/config && rm hooks.json",
    "echo {} > ~/.gemini/antigravity-cli/settings.json",
    "cat x | tee ~/.gem''ini/config/hooks.json",
])
def test_a_command_naming_the_hook_files_is_force_asked(cmd):
    out = run_hook(call("run_command", CommandLine=cmd))
    assert force_asked(out) and "safety hook is set up" in out["reason"], cmd


@pytest.mark.parametrize("tool,args", [
    ("write_to_file", {"TargetFile": "/h/.gemini/config/hooks.json", "CodeContent": "{}"}),
    ("replace_file_content", {"TargetFile": "/h/.gemini/antigravity-cli/settings.json", "TargetContent": "a"}),
])
def test_a_file_write_to_the_hook_files_is_force_asked(tool, args):
    out = run_hook(call(tool, **args))
    assert force_asked(out) and "safety hook is set up" in out["reason"]


def test_ordinary_commands_and_files_in_gemini_are_still_let_through():
    assert run_hook(call("run_command", CommandLine="ls ~/.gemini")) == {"decision": "allow"}
    assert run_hook(call("write_to_file", TargetFile="/h/.gemini/notes.md", CodeContent="hi")) == {"decision": "allow"}


def test_uninstall_goes_on_past_a_hooks_file_it_cannot_read_and_says_what_is_left(ctx, home):
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    antigravity.install(ctx, KIT)
    (home / HOOKS).write_text("{broken")
    msgs = antigravity.uninstall(ctx)
    assert (home / HOOKS).read_text() == "{broken"
    assert not (home / SETTINGS).exists()                                    # the rules were still taken out
    assert not (home / APP_SKILL).exists() and not (home / CLI_SKILL).exists()
    assert any(m.startswith("Antigravity: could not remove everything") and HOOKS in m for m in msgs)
    assert "antigravity" in kit_setup.load_manifest(ctx)["agents"]           # what is left stays on record
    assert agents.hook_leftovers(ctx) == []                                  # broken text names no hook
    (home / HOOKS).write_text(json.dumps({"bertie": {"PreToolUse": [{"matcher": "x", "hooks": [
        {"type": "command", "command": agents.hook_command(ctx, "antigravity")}]}]}}))
    left = agents.hook_leftovers(ctx)
    assert [(label, where) for label, where, _ in left] == [("Antigravity", "~/" + HOOKS)]


def test_a_hooks_file_that_is_a_link_is_refused_before_anything_is_written(ctx, home, tmp_path):
    real = tmp_path / "dotfiles" / "hooks.json"
    real.parent.mkdir()
    real.write_text("{}\n")
    (home / ".gemini" / "config").mkdir(parents=True)
    (home / HOOKS).symlink_to(real)
    before = tree(home)
    with pytest.raises(kit_setup.SetupError, match="link"):
        antigravity.install(ctx, KIT)
    assert tree(home) == before and real.read_text() == "{}\n" and not ctx.install_dir.exists()


def test_a_skill_folder_that_is_a_link_into_another_agents_skills_is_refused(ctx, home, tmp_path):
    other = home / ".claude" / "skills" / "bertie"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: bertie\n---\nthe real Claude Code copy\n")
    (home / ".gemini" / "config" / "skills").mkdir(parents=True)
    (home / ".gemini" / "config" / "skills" / "bertie").symlink_to(other)
    before = (other / "SKILL.md").read_bytes()
    with pytest.raises(kit_setup.SetupError, match="link"):
        antigravity.install(ctx, KIT)
    assert (other / "SKILL.md").read_bytes() == before and not (home / HOOKS).exists()


def test_the_hooks_file_comes_back_byte_for_byte_after_uninstall(ctx, home):
    original = '{\n\t"zeta": 1,\n\t"alpha": [1,2 ,3]\n}'              # tabs, no trailing newline, odd spacing
    put(home, HOOKS, original)
    antigravity.install(ctx, KIT)
    assert (home / HOOKS).read_text() != original
    antigravity.uninstall(ctx)
    assert (home / HOOKS).read_bytes() == original.encode()


def test_install_sh_check_stops_before_anything_is_copied(home, tmp_path):
    put(home, HOOKS, "{broken")
    r = run_install(home, tmp_path, *ARGS, "--agent", "antigravity")
    assert r.returncode == 1 and "Nothing was installed or changed." in r.stdout
    assert "not valid JSON" in r.stderr
    assert not (home / ".config" / "bertie").exists() and not (home / ".local" / "bin").exists()
