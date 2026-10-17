"""The shared parts of the review-fix round: checks before anything is written, uninstall that
keeps what an agent still needs, links refused, original bytes given back, and the hook's
handling of other agents' hook files and failures. Each adapter's own tests cover its side.

HOME is a temporary folder everywhere (the autouse fixture in conftest.py)."""
import io
import json
import os
import subprocess
import sys

import pytest

import agents
import google_cli
import hook
import kit_setup
import registry
from conftest import ACCOUNT, KIT, TOOLS
from test_agents_core import ctx, dirs, run_script, tree, without_claude_leftovers  # noqa: F401
from test_installer import env_for, run_install

ARGS = (ACCOUNT, "--skip-deps", "--skip-login")
HOOKS = ".cursor/hooks.json"
needs_user = pytest.mark.skipif(os.geteuid() == 0, reason="root can write through any permission")


def put(home, rel, text):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def sign_in(keychain):
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps({"refresh_token": "R", "scopes": []}))


def uninstall(home, keychain, monkeypatch):
    import auth
    import common
    monkeypatch.setattr(common, "HOME", home / ".config" / "bertie")
    monkeypatch.setattr(auth, "_post", lambda url, data: (200, "{}"))
    sign_in(keychain)
    google_cli.main(["uninstall", "--yes"])


# ================================================================ A8: folders made, removed deepest first

def test_a8_folders_are_removed_deepest_first_whatever_order_they_were_written_down(home, ctx):
    (home / "a" / "b" / "c").mkdir(parents=True)
    (home / "keep").mkdir()
    agents.prune_dirs(ctx, ["a/b/c", "a/b", "a", "keep/none"])           # deepest first: the wrong way round
    assert not (home / "a").exists() and (home / "keep").is_dir()


def test_a8_a_folder_with_something_in_it_stays_and_so_do_its_parents(home, ctx):
    (home / "a" / "b").mkdir(parents=True)
    (home / "a" / "b" / "mine.txt").write_text("x")
    agents.prune_dirs(ctx, ["a/b", "a"])
    assert (home / "a" / "b" / "mine.txt").is_file() and (home / "a").is_dir()


def test_a8_nothing_outside_home_or_not_a_list_is_touched(home, ctx, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    agents.prune_dirs(ctx, ["../outside", 5, None, str(outside)])
    agents.prune_dirs(ctx, "a")
    agents.prune_dirs(ctx, None)
    assert outside.is_dir()


# ================================================================ A6: the original bytes come back

EDITS = lambda: [agents.Require(["version"], 1), agents.Append(["hooks", "stop"], [{"command": "x"}])]  # noqa: E731


@pytest.mark.parametrize("text", [
    '{\n\t"version": 1,\n\t"zeta": [1,2],\n\t"alpha": {"b":1}\n}',                 # tabs, odd spacing, no final newline
    '{"version":1,"b":2,"a":1}\n',                                                # compact, keys not sorted
    '{\r\n  "version": 1\r\n}\r\n',                                                # CRLF
    "{}\n",
    "",                                                                           # an empty file
])
def test_a6_uninstall_gives_back_the_files_original_bytes(home, ctx, text):
    p = put(home, "cfg/hooks.json", text)
    original = p.read_bytes()
    assert agents.json_merge(ctx, "cursor", "cfg/hooks.json", EDITS()) is True
    assert p.read_bytes() != original
    assert agents.json_unmerge(ctx, "cursor", "cfg/hooks.json") is True
    assert p.read_bytes() == original


def test_a6_a_change_the_user_made_in_between_is_kept_not_overwritten(home, ctx):
    p = put(home, "cfg/hooks.json", '{\n\t"version": 1,\n\t"mine": true\n}')
    agents.json_merge(ctx, "cursor", "cfg/hooks.json", EDITS())
    data = json.loads(p.read_text())
    data["added_later"] = 7
    p.write_text(json.dumps(data, indent=4))
    agents.json_unmerge(ctx, "cursor", "cfg/hooks.json")
    assert json.loads(p.read_text()) == {"version": 1, "mine": True, "added_later": 7}


def test_a6_a_tampered_first_backup_is_not_trusted(home, ctx):
    p = put(home, "cfg/hooks.json", '{"version": 1,\n "mine": 1}\n')
    agents.json_merge(ctx, "cursor", "cfg/hooks.json", EDITS())
    rec = kit_setup.load_manifest(ctx)["agents"]["cursor"]["json"]["cfg/hooks.json"]
    (p.parent / rec["orig_backup"]).write_text('{"version": 1, "mine": 2}')           # no longer the recorded checksum
    agents.json_unmerge(ctx, "cursor", "cfg/hooks.json")
    assert json.loads(p.read_text()) == {"version": 1, "mine": 1}


# ================================================================ C6/A7: links are refused

def test_c6_a_json_config_that_is_a_link_is_refused_and_the_target_untouched(home, ctx):
    target = put(home, "dotfiles/hooks.json", '{"version": 1}\n')
    (home / "cfg").mkdir()
    (home / "cfg" / "hooks.json").symlink_to(target)
    before = tree(home)
    with pytest.raises(kit_setup.SetupError, match="link"):
        agents.json_merge(ctx, "cursor", "cfg/hooks.json", EDITS())
    assert tree(home) == before and (home / "cfg" / "hooks.json").is_symlink()
    assert not (home / ".config" / "bertie" / "install-manifest.json").exists()


def test_c6_a_dangling_link_is_refused_and_never_deleted(home, ctx):
    (home / "cfg").mkdir()
    link = home / "cfg" / "hooks.json"
    link.symlink_to(home / "nowhere.json")
    with pytest.raises(kit_setup.SetupError, match="link"):
        agents.json_merge(ctx, "cursor", "cfg/hooks.json", EDITS())
    assert link.is_symlink() and not (home / "nowhere.json").exists()
    # a record that says "Bertie made this file", and a link standing where it was: left alone
    m = kit_setup.load_manifest(ctx)
    agents.section(m, "cursor").setdefault("json", {})["cfg/hooks.json"] = {
        "created_file": True, "added": [], "created_dirs": []}
    kit_setup.save_manifest(ctx, m)
    # round 2 (R4): a link that leads nowhere has nothing of Bertie's behind it, so the uninstall no longer
    # stops on it: the record goes, the link is never touched
    assert agents.json_unmerge(ctx, "cursor", "cfg/hooks.json") is False
    assert link.is_symlink() and not (home / "nowhere.json").exists()
    assert "cfg/hooks.json" not in kit_setup.load_manifest(ctx)["agents"]["cursor"]["json"]


def test_c6_unmerge_refuses_a_file_that_became_a_link_and_keeps_the_record(home, ctx):
    # round 2 (R4): refused only while the file it leads to still holds Bertie's own hook command
    p = put(home, "cfg/hooks.json", '{"version": 1}\n')
    agents.json_merge(ctx, "cursor", "cfg/hooks.json", [
        agents.Require(["version"], 1), agents.Append(["hooks", "stop"], [{"command": agents.hook_command(ctx, "cursor")}])])
    moved = home / "elsewhere.json"
    p.rename(moved)
    p.symlink_to(moved)
    snapshot = moved.read_bytes()
    with pytest.raises(kit_setup.SetupError, match="link"):
        agents.json_unmerge(ctx, "cursor", "cfg/hooks.json")
    assert moved.read_bytes() == snapshot
    assert "cfg/hooks.json" in kit_setup.load_manifest(ctx)["agents"]["cursor"]["json"]


# ================================================================ C9/A9: a failing disk or odd text is a plain message

def test_c9_a_json_file_that_is_not_text_is_a_plain_error(home, ctx):
    p = home / "cfg" / "hooks.json"
    p.parent.mkdir()
    p.write_bytes(b"\xff\xfe\x00{}")
    with pytest.raises(kit_setup.SetupError, match="could not be read.*UTF-8"):
        agents.json_merge(ctx, "cursor", "cfg/hooks.json", EDITS())
    assert p.read_bytes() == b"\xff\xfe\x00{}"


@needs_user
def test_c9_a_folder_that_cannot_be_written_is_a_plain_error_and_the_file_is_as_it_was(home, ctx):
    p = put(home, "cfg/hooks.json", '{"version": 1}\n')
    p.parent.chmod(0o500)
    try:
        with pytest.raises(kit_setup.SetupError, match="could not write"):
            agents.json_merge(ctx, "cursor", "cfg/hooks.json", EDITS())
    finally:
        p.parent.chmod(0o700)
    assert p.read_text() == '{"version": 1}\n'


@needs_user
def test_c9_new_folders_made_before_a_failed_write_are_taken_away_again(home, ctx):
    (home / "blocked").mkdir()
    (home / "blocked").chmod(0o500)
    try:
        with pytest.raises(kit_setup.SetupError):
            agents.json_merge(ctx, "cursor", "blocked/sub/hooks.json", EDITS())
    finally:
        (home / "blocked").chmod(0o700)
    assert not (home / "blocked" / "sub").exists()


class _Failing:
    KEY, LABEL, HOOK_STYLE = "failing", "Failing", "ask"

    def __init__(self, error):
        self.error = error

    def install(self, ctx, kit_dir):
        raise self.error


@pytest.mark.parametrize("error", [PermissionError(13, "Permission denied"),
                                   UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad start byte")])
def test_c9_an_adapter_install_that_hits_the_disk_or_odd_text_is_a_plain_error(ctx, error):
    with pytest.raises(kit_setup.SetupError, match="Failing: could not finish the setup.*Run the installer again"):
        kit_setup._install_agent(_Failing(error), ctx, KIT)


def test_c9_claudes_install_errors_are_passed_through_as_they_always_were(ctx):
    class Claude(_Failing):
        KEY = "claude"
    with pytest.raises(PermissionError):
        kit_setup._install_agent(Claude(PermissionError(13, "no")), ctx, KIT)


# ================================================================ C5: look at everything before writing anything

def test_c5_a_refusal_by_the_second_agent_leaves_the_first_not_installed(home, ctx):
    target = put(home, "dotfiles/hooks.json", '{"version": 1}\n')
    (home / ".codex").mkdir()
    (home / ".codex" / "hooks.json").symlink_to(target)
    before = tree(home)
    with pytest.raises(kit_setup.SetupError, match=r"\.codex/hooks\.json is a link"):
        kit_setup.install(ctx, KIT, False, ["cursor", "codex"])
    assert tree(home) == before and not (home / ".cursor").exists() and not (home / ".agents").exists()
    assert not (home / ".local").exists() and not (home / ".config" / "bertie").exists()


def test_c5_the_check_command_changes_nothing_and_says_why_it_stops(home, tmp_path):
    put(home, HOOKS, "{broken")
    before = tree(home)
    r = subprocess.run([sys.executable, "-I", str(TOOLS / "kit_setup.py"), "check", "--kit-dir", str(KIT),
                        "--agents", "cursor"], capture_output=True, text=True, timeout=60, env=env_for(home, tmp_path))
    assert r.returncode == 1 and "not valid JSON" in r.stderr and r.stdout == ""
    assert tree(home) == before and not (home / ".agents").exists()


def test_c5_the_check_command_passes_quietly_when_all_is_well(home, tmp_path):
    put(home, HOOKS, '{"version": 1}\n')
    before = tree(home)
    r = subprocess.run([sys.executable, "-I", str(TOOLS / "kit_setup.py"), "check", "--kit-dir", str(KIT),
                        "--agents", "cursor,kimi"], capture_output=True, text=True, timeout=60,
                       env=env_for(home, tmp_path))
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert tree(home) == before


def test_c5_install_sh_stops_before_copying_anything(home, tmp_path):
    put(home, HOOKS, "{broken")
    r = run_install(home, tmp_path, *ARGS, "--agent", "cursor")
    assert r.returncode == 1 and "Nothing was installed or changed." in r.stdout
    assert not (home / ".config" / "bertie").exists() and not (home / ".local" / "bin").exists()
    assert (home / HOOKS).read_text() == "{broken"


def test_c5_an_install_that_passes_the_check_goes_through(home, tmp_path):
    put(home, HOOKS, '{"version": 1}\n')
    r = run_install(home, tmp_path, *ARGS, "--agent", "cursor")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "beforeShellExecution" in (home / HOOKS).read_text()


def test_c5_an_adapter_with_nothing_to_check_up_front_is_fine(ctx):
    class Plain:
        KEY, LABEL = "plain", "Plain"
    import agents as a
    orig = a.get
    try:
        a.get = lambda key: Plain() if key == "plain" else orig(key)
        kit_setup.preflight(ctx, KIT, ["plain"])
    finally:
        a.get = orig


def test_c5_a_claude_only_install_is_looked_over_and_unchanged(home, tmp_path):
    r = run_install(home, tmp_path, *ARGS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / ".claude" / "settings.json").is_file() and not (home / ".cursor").exists()


# ================================================================ C1/A2/K7/K9: uninstall keeps what an agent still needs

def test_c1_uninstall_stops_before_signing_out_when_an_agent_file_cannot_be_cleaned(home, tmp_path, keychain,
                                                                                    monkeypatch, dialog, capsys):
    assert run_install(home, tmp_path, *ARGS, "--agent", "cursor").returncode == 0
    (home / HOOKS).write_text("{broken")
    sign_in(keychain)
    import auth
    import common
    monkeypatch.setattr(common, "HOME", home / ".config" / "bertie")
    monkeypatch.setattr(auth, "_post", lambda url, data: pytest.fail("signed out although a file was not clean"))
    before = tree(home)
    with pytest.raises(SystemExit) as e:
        google_cli.main(["uninstall", "--yes"])
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "nothing was removed and you are still signed in" in err and ".cursor/hooks.json" in err
    assert tree(home) == before and (home / ".config" / "bertie" / "hook.sh").is_file()
    assert keychain.get_password(registry.KEYCHAIN_SERVICE, ACCOUNT) is not None
    assert dialog.shown == []                                          # the person was not even asked


def test_c1_uninstall_keeps_the_install_folder_while_a_file_still_names_the_hook(home, tmp_path, keychain,
                                                                                 monkeypatch, dialog, capsys):
    assert run_install(home, tmp_path, *ARGS, "--agent", "cursor").returncode == 0
    m = kit_setup.load_manifest(kit_setup.Ctx(home, home / ".config" / "bertie"))
    m["agents"].pop("cursor")                                          # the record is gone, the hook entry is not
    kit_setup.save_manifest(kit_setup.Ctx(home, home / ".config" / "bertie"), m)
    capsys.readouterr()
    uninstall(home, keychain, monkeypatch)
    out = capsys.readouterr().out
    assert (home / ".config" / "bertie" / "hook.sh").is_file()          # kept: Cursor would look for it there
    assert "was KEPT" in out and "Cursor: ~/.cursor/hooks.json" in out
    assert "rm -r ~/.config/bertie" in out and "removed the install folder" not in out
    assert "You are signed out" in out
    assert not (home / ".local" / "bin" / "bertie").exists()           # the commands are gone


def test_c1_when_nothing_is_left_the_folder_goes_as_before(home, tmp_path, keychain, monkeypatch, dialog, capsys):
    assert run_install(home, tmp_path, *ARGS, "--agent", "cursor").returncode == 0
    capsys.readouterr()
    uninstall(home, keychain, monkeypatch)
    out = capsys.readouterr().out
    assert "removed the install folder ~/.config/bertie" in out and "KEPT" not in out
    assert not (home / ".config" / "bertie").exists()


def test_c1_a_claude_only_uninstall_prints_what_it_always_printed(home, tmp_path, keychain, monkeypatch, dialog,
                                                                  capsys):
    assert run_install(home, tmp_path, *ARGS).returncode == 0
    capsys.readouterr()
    uninstall(home, keychain, monkeypatch)
    out = capsys.readouterr().out
    assert "removed the install folder ~/.config/bertie" in out
    assert "settings.json.bertie-backup-* were left in ~/.claude" in out
    assert "KEPT" not in out and "also left beside" not in out


def test_k9_the_summary_names_backups_left_beside_other_agents_files(home, tmp_path, keychain, monkeypatch, dialog,
                                                                     capsys):
    put(home, HOOKS, json.dumps({"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}) + "\n")
    put(home, ".kimi-code/config.toml", '[ui]\ntheme = "dark"\n')
    assert run_install(home, tmp_path, *ARGS, "--agent", "cursor,kimi").returncode == 0
    capsys.readouterr()
    uninstall(home, keychain, monkeypatch)
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if "also left beside the other agents' files" in l]
    assert line and "~/.cursor/hooks.json" in line[0] and "~/.kimi-code/config.toml" in line[0]
    assert any(".bertie-backup-" in p.name for p in (home / ".kimi-code").iterdir())


def test_uninstall_problems_lists_a_link_and_an_unreadable_file_and_is_empty_when_clean(home, tmp_path, ctx):
    assert run_install(home, tmp_path, *ARGS, "--agent", "cursor").returncode == 0
    c = kit_setup.Ctx(home, home / ".config" / "bertie")
    assert agents.uninstall_problems(c) == []
    p = home / HOOKS
    text = p.read_text()
    p.write_bytes(b"\xff\xfe")
    problems = agents.uninstall_problems(c)
    assert len(problems) == 1 and ".cursor/hooks.json" in problems[0]
    p.unlink()
    p.symlink_to(home / "x.json")
    assert agents.uninstall_problems(c) == []                  # round 2 (R4): a link that leads nowhere is not a problem
    p.unlink()
    (home / "x.json").write_text(text)                         # a link to a file that still holds the hook entry is
    p.symlink_to(home / "x.json")
    problems = agents.uninstall_problems(c)
    assert len(problems) == 1 and "link" in problems[0] and str(home / "x.json") in problems[0]
    p.unlink()
    p.write_text(text)
    assert agents.uninstall_problems(c) == []


def test_undo_goes_on_past_a_failed_step_and_keeps_the_record_until_nothing_is_left(home, ctx):
    m = kit_setup.load_manifest(ctx)
    agents.section(m, "fake")["note"] = 1
    kit_setup.save_manifest(ctx, m)
    u = agents.Undo(ctx, "fake", "Fake")

    def boom():
        raise PermissionError(13, "Permission denied")

    assert u.run("the thing", boom) is None
    assert u.run("the other", lambda: "ok") == "ok"
    assert u.left == ["the thing: [Errno 13] Permission denied"]
    lines = u.finish()
    assert "could not remove everything" in lines[-1] and "fake" in kit_setup.load_manifest(ctx)["agents"]
    ok = agents.Undo(ctx, "fake", "Fake")
    assert ok.finish() == ["Fake: nothing to remove"] and "fake" not in kit_setup.load_manifest(ctx).get("agents", {})


def test_the_leftovers_are_found_from_the_adapters_files_even_with_no_record(home, ctx):
    put(home, HOOKS, json.dumps({"version": 1, "hooks": {"beforeShellExecution": [
        {"command": agents.hook_command(ctx, "cursor")}]}}))
    left = agents.hook_leftovers(ctx)
    assert [(label, where) for label, where, _ in left] == [("Cursor", "~/.cursor/hooks.json")]
    assert left[0][2]                                                   # and says what to delete by hand


# ================================================================ C8: the neutral skill copy is marked

def test_c8_the_copy_carries_a_mark_and_only_marked_copies_are_ours(tmp_path):
    text = agents.neutral_skill_text(agents.kit_skill_text(KIT))
    assert agents.NEUTRAL_MARK in text and agents.neutral_skill_is_ours(text)
    foreign = "---\nname: bertie\ndescription: someone else's\n---\nbody\n"
    assert not agents.neutral_skill_is_ours(foreign) and not agents.neutral_skill_is_ours("")
    assert not agents.neutral_skill_is_ours(text.replace(agents.NEUTRAL_MARK, ""))


def test_c8_a_foreign_file_that_only_says_name_bertie_is_not_overwritten(home, ctx):
    p = put(home, agents.SHARED_SKILL_REL, "---\nname: bertie\ndescription: mine\n---\nmy own notes\n")
    with pytest.raises(kit_setup.SetupError):
        agents.preflight_skill(ctx, agents.SHARED_SKILL_REL, KIT)
    assert p.read_text().endswith("my own notes\n")


# ================================================================ A1: skill copies never go through links

def test_a1_a_skill_folder_that_is_a_link_into_claudes_skills_is_refused(home, ctx):
    (home / ".claude" / "skills" / "bertie").mkdir(parents=True)
    (home / ".agents" / "skills").mkdir(parents=True)
    (home / ".agents" / "skills" / "bertie").symlink_to(home / ".claude" / "skills" / "bertie")
    with pytest.raises(kit_setup.SetupError, match="link"):
        agents.preflight_skill(ctx, agents.SHARED_SKILL_REL, KIT)
    assert list((home / ".claude" / "skills" / "bertie").iterdir()) == []


def test_a1_a_linked_agents_folder_is_refused(home, ctx):
    (home / "dotfiles").mkdir()
    (home / ".agents").symlink_to(home / "dotfiles")
    with pytest.raises(kit_setup.SetupError, match="link"):
        agents.preflight_skill(ctx, agents.SHARED_SKILL_REL, KIT)
    assert list((home / "dotfiles").iterdir()) == []


def test_a1_an_ordinary_skill_folder_passes(home, ctx):
    agents.preflight_skill(ctx, agents.SHARED_SKILL_REL, KIT)
    assert not (home / ".agents").exists()                                # looking changes nothing


# ================================================================ C4/X4: another agent's hook files

@pytest.mark.parametrize("text", [
    "cat ~/.cursor/hooks.json", "sed -i x ~/.codex/hooks.json", "echo > ~/.codex/rules/bertie.rules",
    "cd ~/.codex && vi config.toml", "rm ~/.gemini/config/hooks.json", "cp x ~/.gemini/antigravity-cli/settings.json",
    "vi ~/.kimi-code/config.toml", "rm -r ~/.agents/skills/bertie", "cd ~/.CURSOR; cat Hooks.JSON",
])
def test_c4_an_agent_command_naming_a_hook_file_is_given_a_reason(text):
    reason = hook.decide_agent_command(text)
    assert reason and "safety hook is set up" in reason


@pytest.mark.parametrize("text", [
    "cat ~/.cursorrc", "cat ~/.cursor-backup/hooks.json", "vi ~/.codex/notes.txt", "ls ~/.agents/skills/other",
    "git commit -m hooks.json", "cat ~/.gemini/GEMINI.md", "vi my.codex/rulesfile",
])
def test_c4_other_files_are_not_swept_in(text):
    assert hook.agent_config_hits(hook.normalise(text)) == []


def test_c4_the_kimi_folder_from_the_environment_counts(monkeypatch):
    monkeypatch.setenv("KIMI_CODE_HOME", "/srv/kimi-data")
    assert hook.agent_config_hits(hook.normalise("vi /srv/kimi-data/config.toml"))
    assert not hook.agent_config_hits(hook.normalise("vi /srv/other/config.toml"))


def test_c4_text_going_into_a_file_naming_a_hook_file_is_denied_as_a_file_write():
    reason = hook.decide_agent_file("rm ~/.codex/hooks.json")
    assert reason and "(in text being written to a file)" in reason


def test_c4_claudes_hook_gives_the_answers_it_always_gave():
    for cmd in ("cat ~/.cursor/hooks.json", "sed -i x ~/.codex/hooks.json", "vi ~/.kimi-code/config.toml"):
        assert hook.decide(cmd) is None
        p = run_script([], json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}))
        assert (p.returncode, p.stdout, p.stderr) == (0, "", "")


# ================================================================ K2/X3 plumbing: an adapter without the new parts still works

class _Bare:
    """The interface as it was before this round: no hook_scan_text, ask_clause, failure_blocks or preflight."""
    KEY, LABEL, HOOK_STYLE = "bare", "Bare", "block"

    def hook_input(self, data):
        return "command", data["command"]

    def rules_cover(self, command):
        return command.startswith("bertie-")

    def hook_answer(self, reason):
        return ("" if not reason else "DENY " + reason), 0

    def hook_failure(self, reason):
        return "FAIL " + reason, 0


def test_an_adapter_without_the_new_optional_parts_gets_the_old_behaviour():
    a = _Bare()
    assert hook.agent_reason(a, {"command": "bertie-mail send r1"}) is None
    reason = hook.agent_reason(a, {"command": "echo hi; bertie-mail send r1"})
    assert reason.endswith("so Bare asks you first.")


def test_decide_block_judges_the_rule_on_the_command_alone_but_the_checks_on_the_text():
    a = _Bare()
    assert hook.decide_block(a, "bertie-mail send r1") is None
    assert hook.decide_block(a, "bertie-mail send r1", checked="cd /x && bertie-mail send r1") is None
    assert hook.decide_block(a, "bertie-mail send r1", checked="cd ~/.config/bertie && bertie-mail send r1") is not None
    assert hook.decide_block(a, "ls", checked="cd ~/.config/bertie && ls") is not None
    assert hook.decide_block(a, "ls", checked="cd /tmp && ls") is None


def test_an_asking_adapter_gets_the_folder_in_front_of_the_command():
    class Asker(_Bare):
        HOOK_STYLE = "ask"

        def hook_scan_text(self, data, command):
            return "cd " + data["cwd"] + " && " + command

    assert hook.agent_reason(Asker(), {"command": "ls", "cwd": "/tmp"}) is None
    assert hook.agent_reason(Asker(), {"command": "ln -sf a b", "cwd": "/h/.config/bertie"})


def test_a_scan_text_that_is_not_text_fails_closed():
    class Odd(_Bare):
        def hook_scan_text(self, data, command):
            return 5

    with pytest.raises(ValueError):
        hook.agent_reason(Odd(), {"command": "ls"})


# ================================================================ C3/C7: failures answer in the way they act

@pytest.mark.parametrize("key,word", [("antigravity", "asking"), ("codex", "blocking"), ("kimi", "blocking")])
def test_c7_the_failure_message_says_what_the_answer_does(key, word):
    out, code = hook.agent_answer(key, io.StringIO("not json"))
    assert code == 0 and f"{word} to be safe" in out


@pytest.mark.parametrize("key", ["cursor", "codex", "kimi"])
def test_c3_silence_is_a_failure_for_agents_that_always_send_something(key):
    for blank in ("", "  \n\t"):
        out, code = hook.agent_answer(key, io.StringIO(blank))
        assert code == 0 and "could not read this command" in out


def test_c3_antigravity_silence_still_asks_as_it_did():
    out, code = hook.agent_answer("antigravity", io.StringIO(""))
    assert code == 0 and '"decision": "force_ask"' in out


# ================================================================ X7: who asks

def test_x7_the_block_tail_names_the_asker():
    assert hook.block_tail("Codex").endswith("so Codex asks you first.")
    assert hook.block_tail("Codex", "the Mac window asks you").endswith("so the Mac window asks you first.")
