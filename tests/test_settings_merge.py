"""Merging into ~/.claude/settings.json and taking exactly that back out."""
import copy
import json
import stat

import pytest

import kit_setup
import registry

EXISTING = {
    "model": "opus",
    "permissions": {
        "allow": ["Bash(ls:*)", "Bash(git status:*)"],
        "ask": ["Bash(rm:*)", "Bash(bertie-mail send:*)"],     # one of ours already present
        "deny": ["Bash(sudo:*)"],
    },
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 /x/other.py"}]},
            {"matcher": "Read", "hooks": [{"type": "command", "command": "python3 /x/read.py"}]},
        ],
        "SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}],
    },
    "env": {"A": "1"},
}


def write(home, data, indent=2, newline=True):
    p = home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=indent) + ("\n" if newline else ""))
    return p


def load(home):
    return json.loads((home / ".claude" / "settings.json").read_text())


def backups(home):
    return sorted(p.name for p in (home / ".claude").glob("settings.json.bertie-backup-*"))


@pytest.fixture
def ctx(home, tmp_path):
    return kit_setup.Ctx(home, tmp_path / "inst")


def test_merge_keeps_everything_and_adds_rules_and_hook(ctx, home):
    write(home, EXISTING)
    assert kit_setup.merge_settings(ctx) is True
    got = load(home)
    # every original key, rule and hook survives
    assert got["model"] == "opus" and got["env"] == {"A": "1"}
    assert got["permissions"]["allow"] == EXISTING["permissions"]["allow"]
    assert got["permissions"]["deny"] == EXISTING["permissions"]["deny"]
    assert got["permissions"]["ask"][:2] == EXISTING["permissions"]["ask"]
    assert got["hooks"]["SessionStart"] == EXISTING["hooks"]["SessionStart"]
    assert got["hooks"]["PreToolUse"][:2] == EXISTING["hooks"]["PreToolUse"]
    # ours are added
    for rule in registry.ask_rules():
        assert rule in got["permissions"]["ask"]
    assert got["permissions"]["ask"].count("Bash(bertie-mail send:*)") == 1
    ours = got["hooks"]["PreToolUse"][2:]
    assert ours == [{"matcher": m, "hooks": [{"type": "command", "command": kit_setup.HOOK_COMMAND}]}
                    for m in kit_setup.HOOK_MATCHERS]
    assert kit_setup.HOOK_MATCHERS == ("Bash|mcp__terminal__run_in_terminal", "Write|Edit", "mcp__computer-use__.*")


def test_the_expected_rules_are_present(ctx, home):
    kit_setup.merge_settings(ctx)
    ask = load(home)["permissions"]["ask"]
    for r in ["Bash(bertie-mail send:*)", "Bash(bertie-mail trash:*)",
              "Bash(bertie-calendar create:*)", "Bash(bertie-calendar move:*)",
              "Bash(bertie-calendar cancel:*)", "Bash(bertie-chat send:*)",
              "Bash(bertie-chat dm:*)", "Bash(bertie-drive upload:*)",
              "Bash(bertie-drive update:*)", "Bash(bertie-drive share:*)"]:
        assert r in ask


def test_merge_is_idempotent_and_makes_no_extra_backup(ctx, home):
    write(home, EXISTING)
    kit_setup.merge_settings(ctx)
    first = (home / ".claude" / "settings.json").read_bytes()
    assert len(backups(home)) == 1
    assert kit_setup.merge_settings(ctx) is False
    assert kit_setup.merge_settings(ctx) is False
    assert (home / ".claude" / "settings.json").read_bytes() == first
    assert len(backups(home)) == 1


def test_backup_holds_the_original_bytes(ctx, home):
    p = write(home, EXISTING)
    original = p.read_bytes()
    kit_setup.merge_settings(ctx)
    (b,) = backups(home)
    assert (home / ".claude" / b).read_bytes() == original


@pytest.mark.parametrize("indent,newline", [(2, True), (4, True), ("\t", True), (2, False)])
def test_formatting_style_is_preserved(ctx, home, indent, newline):
    p = write(home, EXISTING, indent=indent, newline=newline)
    kit_setup.merge_settings(ctx)
    text = p.read_text()
    assert text.endswith("\n") == newline
    first_indented = [l for l in text.splitlines() if l[:1] in (" ", "\t")][0]
    lead = first_indented[: len(first_indented) - len(first_indented.lstrip())]
    assert lead == ("\t" if indent == "\t" else " " * indent)
    # key order of the original is kept
    assert list(json.loads(text))[: len(EXISTING)] == list(EXISTING)


def test_missing_file_is_created(ctx, home):
    assert not (home / ".claude" / "settings.json").exists()
    assert kit_setup.merge_settings(ctx) is True
    got = load(home)
    assert set(got) == {"permissions", "hooks"}
    assert stat.S_IMODE((home / ".claude" / "settings.json").stat().st_mode) == 0o600
    assert backups(home) == []                      # nothing existed to back up


def test_empty_file_is_treated_as_empty_settings(ctx, home):
    p = home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text("")
    kit_setup.merge_settings(ctx)
    assert "permissions" in load(home)


def test_existing_file_mode_is_kept(ctx, home):
    p = write(home, EXISTING)
    p.chmod(0o640)
    kit_setup.merge_settings(ctx)
    assert stat.S_IMODE(p.stat().st_mode) == 0o640


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]", '{"permissions": []}',
                                 '{"permissions": {"ask": "x"}}', '{"hooks": []}',
                                 '{"hooks": {"PreToolUse": {}}}'])
def test_unsafe_files_are_refused_and_left_alone(ctx, home, bad):
    p = home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text(bad)
    with pytest.raises(kit_setup.SetupError):
        kit_setup.merge_settings(ctx)
    assert p.read_text() == bad
    assert backups(home) == []


# ------------------------------------------------------------------ uninstall of settings

def test_unmerge_restores_the_original_content(ctx, home):
    write(home, EXISTING)
    kit_setup.merge_settings(ctx)
    assert kit_setup.unmerge_settings(ctx) is True
    assert load(home) == EXISTING                   # incl. the rule that was there before us


def test_unmerge_does_not_remove_a_rule_the_user_had_first(ctx, home):
    write(home, EXISTING)
    kit_setup.merge_settings(ctx)
    kit_setup.unmerge_settings(ctx)
    assert "Bash(bertie-mail send:*)" in load(home)["permissions"]["ask"]


def test_unmerge_keeps_changes_made_after_install(ctx, home):
    write(home, EXISTING)
    kit_setup.merge_settings(ctx)
    data = load(home)
    data["permissions"]["allow"].append("Bash(npm test:*)")
    data["hooks"]["PreToolUse"].append({"matcher": "Edit", "hooks": [{"type": "command", "command": "later"}]})
    write(home, data)
    kit_setup.unmerge_settings(ctx)
    got = load(home)
    assert "Bash(npm test:*)" in got["permissions"]["allow"]
    assert got["hooks"]["PreToolUse"][-1]["matcher"] == "Edit"
    assert not any(kit_setup.HOOK_COMMAND in json.dumps(g) for g in got["hooks"]["PreToolUse"])


def test_unmerge_of_a_file_we_created_removes_the_file(ctx, home):
    kit_setup.merge_settings(ctx)
    assert kit_setup.unmerge_settings(ctx) is True
    assert not (home / ".claude" / "settings.json").exists()


def test_unmerge_removes_containers_only_if_we_created_them(ctx, home):
    write(home, {"permissions": {"allow": ["x"]}, "hooks": {}})
    kit_setup.merge_settings(ctx)
    kit_setup.unmerge_settings(ctx)
    # 'ask' and 'PreToolUse' did not exist, so they are gone again; 'hooks' did, so it stays
    assert load(home) == {"permissions": {"allow": ["x"]}, "hooks": {}}


def test_hook_added_to_a_shared_group_leaves_the_other_handlers(ctx, home):
    data = copy.deepcopy(EXISTING)
    write(home, data)
    kit_setup.merge_settings(ctx)
    got = load(home)
    # a user later merges our handler into their own Bash group by hand
    got["hooks"]["PreToolUse"][0]["hooks"].append({"type": "command", "command": kit_setup.HOOK_COMMAND})
    write(home, got)
    kit_setup.unmerge_settings(ctx)
    after = load(home)
    assert after["hooks"]["PreToolUse"][0]["hooks"] == EXISTING["hooks"]["PreToolUse"][0]["hooks"]


def test_unmerge_without_a_manifest_still_removes_our_hook(ctx, home):
    write(home, EXISTING)
    kit_setup.merge_settings(ctx)
    ctx.manifest_path.unlink()
    kit_setup.unmerge_settings(ctx)
    got = load(home)
    assert kit_setup.HOOK_COMMAND not in json.dumps(got)
    assert got["permissions"]["allow"] == EXISTING["permissions"]["allow"]


def test_rerun_after_adding_a_service_adds_only_the_new_rule(ctx, home, monkeypatch):
    kit_setup.merge_settings(ctx)
    new = registry.Service(key="tasks", command="bertie-tasks", module="svc_tasks", label="Tasks",
                           api_name="Google Tasks API", scopes=("https://www.googleapis.com/auth/tasks",),
                           gated={"add": "adds a task"})
    monkeypatch.setattr(registry, "SERVICES", registry.SERVICES + (new,))
    assert kit_setup.merge_settings(ctx) is True
    assert "Bash(bertie-tasks add:*)" in load(home)["permissions"]["ask"]
    kit_setup.unmerge_settings(ctx)
    assert not (home / ".claude" / "settings.json").exists()      # both runs' rules were recorded


def test_an_older_install_with_the_narrow_bash_matcher_is_widened(ctx, home):
    old = copy.deepcopy(EXISTING)
    old["hooks"]["PreToolUse"].append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": kit_setup.HOOK_COMMAND}]})
    write(home, old)
    assert kit_setup.merge_settings(ctx) is True
    groups = load(home)["hooks"]["PreToolUse"]
    ours = [g for g in groups if any(h.get("command") == kit_setup.HOOK_COMMAND for h in g["hooks"])]
    assert [g["matcher"] for g in ours] == list(kit_setup.HOOK_MATCHERS)
    assert groups[:2] == EXISTING["hooks"]["PreToolUse"]                # other people's hooks untouched
    assert kit_setup.merge_settings(ctx) is False                       # and it settles


def test_an_install_from_before_the_screen_control_matcher_gains_it_once(ctx, home):
    old = copy.deepcopy(EXISTING)
    for m in kit_setup.HOOK_MATCHERS[:2]:
        old["hooks"]["PreToolUse"].append(
            {"matcher": m, "hooks": [{"type": "command", "command": kit_setup.HOOK_COMMAND}]})
    write(home, old)
    assert kit_setup.merge_settings(ctx) is True
    ours = [g["matcher"] for g in load(home)["hooks"]["PreToolUse"]
            if any(h.get("command") == kit_setup.HOOK_COMMAND for h in g["hooks"])]
    assert ours == list(kit_setup.HOOK_MATCHERS)
    assert kit_setup.merge_settings(ctx) is False


def test_unmerge_removes_every_hook_group(ctx, home):
    write(home, EXISTING)
    kit_setup.merge_settings(ctx)
    kit_setup.unmerge_settings(ctx)
    assert load(home) == EXISTING
