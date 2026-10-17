"""The agent core: the shared helpers in tools/agents, Claude Code's adapter, `hook.py --agent`,
hook.sh's fallbacks and install.sh's `--agent` option.

Nothing here depends on an adapter other than Claude Code's. The four others are stood in for
by fakes built from one source text (FAKE_SRC), either put into sys.modules or written into a
copy of the kit. HOME is a temporary folder everywhere; no test touches the real one."""
import ast
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import types

import pytest

import agents
import google_cli
import hook
import kit_setup
import registry
from conftest import ACCOUNT, KIT, TOOLS
from test_hook import (BYPASS, COMPOUND, FREE, GATED, HELPER_TAMPERING, KEYCHAIN, PATTERNS, RUN_TIME_NAMES,
                       UNREADABLE, WINDOW_DRIVING)
from test_installer import env_for, run_install

ATTACKS = (GATED + COMPOUND + UNREADABLE + BYPASS + KEYCHAIN + PATTERNS + RUN_TIME_NAMES
           + WINDOW_DRIVING + HELPER_TAMPERING)

# ---------------------------------------------------------------- stand-in adapters

# key: (label, hook style, config folder, uses the shared ~/.agents skill)
FAKES = {
    "antigravity": ("Antigravity", "ask", ".gemini", False),
    "cursor": ("Cursor", "ask", ".cursor", True),
    "codex": ("Codex", "block", ".codex", True),
    "kimi": ("Kimi Code", "block", ".kimi-code", True),
}

FAKE_SRC = r'''
from __future__ import annotations

import json
import sys
from pathlib import Path

import agents
import kit_setup

KEY = "__KEY__"
LABEL = "__LABEL__"
HOOK_STYLE = "__STYLE__"
FOLDER = "__FOLDER__"
SHARED = __SHARED__
MODE = None                      # a test sets this to make the fake misbehave
_MARK = "# bertie fake adapter __KEY__"
_NOTE = FOLDER + "/bertie.note"
_HOOKS = FOLDER + "/hooks.json"


def detect(home):
    return (Path(home) / FOLDER).is_dir()


def install(ctx, kit_dir):
    agents.write_owned_file(ctx, KEY, _NOTE, _MARK + "\n" + agents.hook_command(ctx, KEY) + "\n",
                            agents.marker_first_line(_MARK))
    group = {"matcher": "Bash", "hooks": [{"type": "command", "command": agents.hook_command(ctx, KEY)}]}
    agents.json_merge(ctx, KEY, _HOOKS, [agents.Append(["hooks", "PreToolUse"], [group])])
    if SHARED:
        agents.acquire_shared_skill(ctx, KEY, kit_dir)
    m = kit_setup.load_manifest(ctx)
    agents.section(m, KEY)["sends"] = agents.option(ctx, "codex_sends", "prompt")
    kit_setup.save_manifest(ctx, m)
    return [LABEL + ": set up (fake)", LABEL + ": protected by the fake"]


def uninstall(ctx):
    out = []
    if agents.json_unmerge(ctx, KEY, _HOOKS):
        out.append(LABEL + ": took the hook out")
    if agents.remove_owned_file(ctx, KEY, _NOTE, agents.marker_first_line(_MARK)):
        out.append(LABEL + ": removed the note")
    if SHARED:
        agents.release_shared_skill(ctx, KEY)
    agents.drop_section(ctx, KEY)
    return out or [LABEL + ": nothing to remove"]


def hook_input(data):
    if "boom" in data:
        raise ValueError("unreadable")
    if "probe" in data:
        import hook
        return "ask", "same:" + str(hook is sys.modules["__main__"])
    for kind in ("command", "file", "ask"):
        if kind in data:
            return kind, data[kind]
    return "none", ""


def hook_answer(reason):
    if MODE == "code2":
        return "out\n", 2
    if MODE == "raise":
        raise RuntimeError("bug")
    if HOOK_STYLE == "block":
        return ("" if not reason else agents.pretooluse_answer("deny", reason)), 0
    return json.dumps({"fake": "allow"} if not reason else {"fake": "ask", "reason": reason}) + "\n", 0


def hook_failure(reason):
    if MODE == "raise":
        raise RuntimeError("bug")
    if HOOK_STYLE == "block":
        return agents.pretooluse_answer("deny", reason), 0
    return json.dumps({"fake": "ask", "reason": reason}) + "\n", 0


def rules_cover(command):
    import registry
    if "/." in command or "\\" in command:
        return False
    if MODE == "truthy":
        return 1
    for tool, acts in registry.gated_actions().items():
        for act in acts:
            p = tool + " " + act
            if command == p or command.startswith(p + " "):
                return True
    return False
'''


def fake_source(key):
    label, style, folder, shared = FAKES[key]
    return (FAKE_SRC.replace("__KEY__", key).replace("__LABEL__", label).replace("__STYLE__", style)
            .replace("__FOLDER__", folder).replace("__SHARED__", str(shared)))


def fake_module(key):
    mod = types.ModuleType("agents." + key)
    exec(compile(fake_source(key), f"<fake {key}>", "exec"), mod.__dict__)
    return mod


@pytest.fixture
def fakes(monkeypatch):
    """Fake modules for the four other agents, in place of whatever agents/<key>.py holds."""
    mods = {}
    for key in FAKES:
        mods[key] = fake_module(key)
        monkeypatch.setitem(sys.modules, "agents." + key, mods[key])
    return mods


@pytest.fixture
def ctx(home, tmp_path):
    return kit_setup.Ctx(home, tmp_path / "inst")


@pytest.fixture
def fake_kit(tmp_path):
    """A copy of the kit whose four other adapters are the fakes (a copy, so nothing real changes)."""
    k = tmp_path / "fakekit"
    shutil.copytree(KIT, k, ignore=shutil.ignore_patterns("tests", ".pytest_cache", "__pycache__", ".git"))
    for key in FAKES:
        (k / "tools" / "agents" / f"{key}.py").write_text(fake_source(key))
    return k


def tree(root, skip=(".venv", "__pycache__")):
    """Every file under root (not the private Python environment): relative path -> bytes."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not any(part in skip for part in p.relative_to(root).parts) \
                and ".bertie-backup-" not in p.name:
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def dirs(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir())


# Claude Code's uninstall has always left these empty folders (and a settings backup) behind.
CLAUDE_LEFTOVERS = {".claude", ".claude/skills", ".config", ".local", ".local/bin"}


def without_claude_leftovers(folders):
    return [d for d in folders if d not in CLAUDE_LEFTOVERS]


def backups(directory):
    return sorted(p.name for p in directory.glob("*.bertie-backup-*"))


# ================================================================ finding adapters

def test_the_adapter_list_is_the_five_agents_in_order():
    assert agents.ADAPTERS == ("claude", "antigravity", "cursor", "codex", "kimi")


def test_get_gives_the_claude_adapter_and_refuses_unknown_keys():
    assert agents.get("claude").KEY == "claude"
    for bad in ("nope", "", "Claude", "../x", "claude.py"):
        with pytest.raises(kit_setup.SetupError, match="unknown agent"):
            agents.get(bad)


def test_an_adapter_whose_file_is_missing_is_a_clear_setup_error(monkeypatch):
    monkeypatch.setattr(agents, "ADAPTERS", agents.ADAPTERS + ("ghost",))
    with pytest.raises(kit_setup.SetupError, match="ghost adapter is missing"):
        agents.get("ghost")


def test_a_real_import_error_inside_an_adapter_is_not_hidden(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg" / "agents"
    shutil.copytree(TOOLS / "agents", pkg, ignore=shutil.ignore_patterns("__pycache__"))
    (pkg / "kimi.py").write_text("import a_module_that_does_not_exist\n")
    # the package under test is the one already imported; point its search path at the copy
    monkeypatch.setattr(agents, "__path__", [str(pkg)])
    monkeypatch.delitem(sys.modules, "agents.kimi", raising=False)
    with pytest.raises(ModuleNotFoundError, match="a_module_that_does_not_exist"):
        agents.get("kimi")


def test_detect_present_looks_only_at_the_config_folders(home, fakes):
    assert agents.detect_present(home) == []
    for folder in (".kimi-code", ".claude", ".cursor"):
        (home / folder).mkdir()
    assert agents.detect_present(home) == ["claude", "cursor", "kimi"]


def test_detect_present_skips_an_adapter_that_is_not_in_this_copy(home, fakes, monkeypatch):
    (home / ".codex").mkdir()
    (home / ".cursor").mkdir()
    monkeypatch.setitem(sys.modules, "agents.codex", None)       # as if codex.py were not shipped
    assert agents.detect_present(home) == ["cursor"]


def test_default_selection_is_claude_always_plus_what_is_found(home, fakes):
    assert agents.default_selection(home) == ["claude"]            # an empty home: exactly as in v0.1
    (home / ".kimi-code").mkdir()
    (home / ".gemini").mkdir()
    assert agents.default_selection(home) == ["claude", "antigravity", "kimi"]


def test_parse_agents():
    assert agents.parse_agents("all") == list(agents.ADAPTERS)
    assert agents.parse_agents("kimi,claude") == ["claude", "kimi"]
    assert agents.parse_agents("codex, codex ,cursor") == ["cursor", "codex"]
    for bad in ("", "nope", "claude,", ",claude", "claude,,kimi", "Claude", "claude kimi"):
        with pytest.raises(kit_setup.SetupError, match="unknown agent"):
            agents.parse_agents(bad)


def test_labels_and_the_joined_names(fakes):
    assert agents.labels(["claude", "kimi"]) == ["Claude Code", "Kimi Code"]
    assert agents.join_labels(["claude"]) == "Claude Code"
    assert agents.join_labels(["claude", "kimi"]) == "Claude Code or Kimi Code"
    assert agents.join_labels(["claude", "cursor", "codex"]) == "Claude Code, Cursor or Codex"


def test_the_installers_shell_list_matches_the_adapter_list():
    sh = (KIT / "install.sh").read_text()
    line = next(l for l in sh.splitlines() if l.strip().startswith("all|claude|"))
    assert line.split(")")[0].strip().split("|") == ["all"] + list(agents.ADAPTERS)


# ================================================================ installer choices, hook command, answers

def test_options_default_and_override(ctx):
    assert agents.option(ctx, "codex_sends", "prompt") == "prompt"
    agents.set_options(ctx, codex_sends="allow")
    assert agents.option(ctx, "codex_sends", "prompt") == "allow"
    agents.set_options(ctx, other="x")
    assert agents.option(ctx, "codex_sends", "prompt") == "allow" and agents.option(ctx, "other", "") == "x"


def test_hook_command_is_sh_then_the_quoted_real_path_then_the_agent(home):
    ctx = kit_setup.Ctx(home)
    cmd = agents.hook_command(ctx, "codex")
    assert cmd == f"sh {home}/.config/bertie/hook.sh --agent codex"
    assert shlex.split(cmd) == ["sh", str(home / ".config" / "bertie" / "hook.sh"), "--agent", "codex"]
    assert "$HOME" not in cmd and "~" not in cmd


def test_hook_command_quotes_a_path_with_spaces_and_quotes(tmp_path, home):
    odd = tmp_path / "my dir" / "it's here"
    ctx = kit_setup.Ctx(home, odd)
    cmd = agents.hook_command(ctx, "kimi")
    assert shlex.split(cmd) == ["sh", str(odd / "hook.sh"), "--agent", "kimi"]


@pytest.mark.parametrize("key", ["", "Kimi", "a b", "x;y", "-k", "k$(x)"])
def test_hook_command_refuses_a_key_that_is_not_a_plain_key(ctx, key):
    with pytest.raises(kit_setup.SetupError):
        agents.hook_command(ctx, key)


def test_claude_keeps_its_own_hook_command():
    assert kit_setup.HOOK_COMMAND == 'sh "$HOME/.config/bertie/hook.sh"'


def test_pretooluse_answer_is_the_json_hook_py_has_always_printed():
    assert agents.pretooluse_answer("ask", "why") == json.dumps(hook.ask("why")) + "\n"
    out = json.loads(agents.pretooluse_answer("deny", "no"))["hookSpecificOutput"]
    assert out == {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "no"}


# ================================================================ the manifest

def test_section_creates_and_peek_does_not():
    m = {}
    assert agents.peek(m, "codex") == {} and m == {}
    agents.section(m, "codex")["x"] = 1
    assert m == {"agents": {"codex": {"x": 1}}} and agents.peek(m, "codex") == {"x": 1}


def test_a_damaged_manifest_entry_is_read_as_nothing_recorded():
    for bad in ("oops", 5, None, ["a"]):
        m = {"agents": {"codex": bad}}
        assert agents.peek(m, "codex") == {}
        assert agents.section(m, "codex") == {}
    assert agents.section({"agents": "oops"}, "kimi") == {}
    assert agents.recorded_keys({"agents": "oops"}) == [] and agents.recorded_keys({}) == []


def test_recorded_keys_lists_known_agents_in_order_then_strangers():
    m = {"agents": {"kimi": {}, "zzz": {}, "cursor": {}}}
    assert agents.recorded_keys(m) == ["cursor", "kimi", "zzz"]


def test_drop_section_removes_the_entry_and_then_the_empty_container(ctx):
    m = {"agents": {"codex": {"a": 1}, "kimi": {}}, "commands": ["x"]}
    kit_setup.save_manifest(ctx, m)
    agents.drop_section(ctx, "codex")
    assert kit_setup.load_manifest(ctx) == {"agents": {"kimi": {}}, "commands": ["x"]}
    agents.drop_section(ctx, "kimi")
    assert kit_setup.load_manifest(ctx) == {"commands": ["x"]}
    agents.drop_section(ctx, "kimi")                                  # already gone: nothing happens
    assert kit_setup.load_manifest(ctx) == {"commands": ["x"]}


def test_backup_file_copies_beside_and_records_the_name(ctx, home):
    f = home / "conf.toml"
    f.write_text("a = 1\n")
    b = agents.backup_file(ctx, "kimi", f)
    assert b.parent == home and b.name.startswith("conf.toml.bertie-backup-") and b.read_text() == "a = 1\n"
    assert agents.peek(kit_setup.load_manifest(ctx), "kimi")["backups"] == [b.name]


# ================================================================ JSON merge and unmerge

EXISTING = {
    "model": "x",
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "bye"}]}],
    },
}


def group(command="sh /x/hook.sh --agent codex", timeout=30):
    return {"matcher": "Bash", "hooks": [{"type": "command", "command": command, "timeout": timeout}]}


def put(home, rel, data, indent=2, newline=True):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=indent) + ("\n" if newline else ""))
    return p


def hooks_edit(g=None):
    return [agents.Append(["hooks", "PreToolUse"], [g or group()])]


def test_merge_keeps_everything_adds_the_entry_and_unmerge_restores_the_bytes(ctx, home):
    p = put(home, ".codex/hooks.json", EXISTING)
    original = p.read_bytes()
    assert agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit()) is True
    got = json.loads(p.read_text())
    assert got["model"] == "x" and got["hooks"]["Stop"] == EXISTING["hooks"]["Stop"]
    assert got["hooks"]["PreToolUse"][0] == EXISTING["hooks"]["PreToolUse"][0]
    assert got["hooks"]["PreToolUse"][1:] == [group()]
    (b,) = backups(home / ".codex")
    assert (home / ".codex" / b).read_bytes() == original
    assert agents.json_unmerge(ctx, "codex", ".codex/hooks.json") is True
    assert p.read_bytes() == original


@pytest.mark.parametrize("indent,newline", [(2, True), (4, True), ("\t", True), (2, False)])
def test_formatting_and_mode_survive_a_round_trip(ctx, home, indent, newline):
    p = put(home, ".codex/hooks.json", EXISTING, indent=indent, newline=newline)
    p.chmod(0o640)
    original = p.read_bytes()
    agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit())
    assert p.read_text().endswith("\n") == newline and stat.S_IMODE(p.stat().st_mode) == 0o640
    agents.json_unmerge(ctx, "codex", ".codex/hooks.json")
    assert p.read_bytes() == original and stat.S_IMODE(p.stat().st_mode) == 0o640


def test_a_file_and_folders_bertie_made_are_taken_away_again(ctx, home):
    assert not (home / ".codex").exists()
    assert agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit()) is True
    p = home / ".codex" / "hooks.json"
    assert json.loads(p.read_text()) == {"hooks": {"PreToolUse": [group()]}}
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and backups(home / ".codex") == []
    assert agents.json_unmerge(ctx, "codex", ".codex/hooks.json") is True
    assert list(home.rglob("*")) == []                               # the file and the folder, nothing left


def test_a_folder_that_was_already_there_stays(ctx, home):
    (home / ".codex").mkdir()
    agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit())
    agents.json_unmerge(ctx, "codex", ".codex/hooks.json")
    assert dirs(home) == [".codex"] and list((home / ".codex").iterdir()) == []


def test_containers_are_removed_only_if_bertie_made_them(ctx, home):
    p = put(home, ".codex/hooks.json", {"model": "x", "hooks": {}})
    original = p.read_bytes()
    agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit())
    assert json.loads(p.read_text())["hooks"] == {"PreToolUse": [group()]}
    agents.json_unmerge(ctx, "codex", ".codex/hooks.json")
    assert p.read_bytes() == original                                # "hooks" was theirs and stays; PreToolUse was ours
    p = put(home, ".codex/hooks.json", {"model": "x"})
    original = p.read_bytes()
    agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit())
    agents.json_unmerge(ctx, "codex", ".codex/hooks.json")
    assert p.read_bytes() == original


def test_an_empty_file_comes_back_empty(ctx, home):
    p = home / ".codex" / "hooks.json"
    p.parent.mkdir()
    p.write_text("")
    agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit())
    assert json.loads(p.read_text())["hooks"]["PreToolUse"] == [group()]
    agents.json_unmerge(ctx, "codex", ".codex/hooks.json")
    assert p.read_bytes() == b""


def test_merging_twice_changes_nothing_and_makes_no_second_backup(ctx, home):
    p = put(home, ".codex/hooks.json", EXISTING)
    agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit())
    first = p.read_bytes()
    assert agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit()) is False
    assert agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit()) is False
    assert p.read_bytes() == first and len(backups(home / ".codex")) == 1
    rec = agents.peek(kit_setup.load_manifest(ctx), "codex")["json"][".codex/hooks.json"]
    assert rec["added"] == [{"path": ["hooks", "PreToolUse"], "items": [group()]}]
    agents.json_unmerge(ctx, "codex", ".codex/hooks.json")
    assert json.loads(p.read_text()) == EXISTING


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]", '"text"', '{"hooks": []}', '{"hooks": null}',
                                 '{"hooks": {"PreToolUse": {}}}', '{"hooks": {"PreToolUse": null}}'])
def test_unsafe_files_are_refused_untouched_with_no_backup_and_no_record(ctx, home, bad):
    p = home / ".codex" / "hooks.json"
    p.parent.mkdir()
    p.write_text(bad)
    with pytest.raises(kit_setup.SetupError):
        agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit())
    assert p.read_text() == bad and backups(p.parent) == []
    assert not ctx.manifest_path.exists()


def test_a_later_edit_failing_leaves_the_file_untouched(ctx, home):
    p = put(home, ".codex/hooks.json", {"version": 2})
    original = p.read_bytes()
    with pytest.raises(kit_setup.SetupError, match="version"):
        agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit() + [agents.Require(["version"], 1)])
    assert p.read_bytes() == original and backups(p.parent) == []


def test_an_item_the_user_already_had_is_theirs_and_survives_uninstall(ctx, home):
    mine = group()
    p = put(home, ".codex/hooks.json", {"hooks": {"PreToolUse": [mine]}})
    original = p.read_bytes()
    assert agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit(mine)) is False
    agents.json_unmerge(ctx, "codex", ".codex/hooks.json")
    assert p.read_bytes() == original


def test_an_entry_of_ours_from_an_earlier_install_is_updated_in_place(ctx, home):
    p = put(home, ".codex/hooks.json", EXISTING)
    agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit(group(timeout=10)))
    agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit(group(timeout=30)))
    pre = json.loads(p.read_text())["hooks"]["PreToolUse"]
    assert pre[1:] == [group(timeout=30)]                            # replaced, not doubled
    agents.json_unmerge(ctx, "codex", ".codex/hooks.json")
    assert json.loads(p.read_text()) == EXISTING


def test_flat_hook_entries_work_the_same_way(ctx, home):
    entry = {"command": "sh /x/hook.sh --agent cursor", "timeout": 10, "failClosed": True}
    other = {"command": "theirs"}
    p = put(home, ".cursor/hooks.json", {"version": 1, "hooks": {"beforeShellExecution": [other]}})
    original = p.read_bytes()
    edits = [agents.Require(["version"], 1), agents.Append(["hooks", "beforeShellExecution"], [entry]),
             agents.Append(["hooks", "preToolUse"], [dict(entry, matcher="Write")])]
    agents.json_merge(ctx, "cursor", ".cursor/hooks.json", edits)
    got = json.loads(p.read_text())
    assert got["hooks"]["beforeShellExecution"] == [other, entry] and len(got["hooks"]["preToolUse"]) == 1
    agents.json_unmerge(ctx, "cursor", ".cursor/hooks.json")
    assert p.read_bytes() == original


def test_changes_made_after_install_are_kept_and_only_our_hook_leaves_a_shared_group(ctx, home):
    p = put(home, ".codex/hooks.json", EXISTING)
    agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit())
    data = json.loads(p.read_text())
    data["model"] = "later"
    data["hooks"]["PreToolUse"][1]["hooks"].append({"type": "command", "command": "added-by-the-user"})
    p.write_text(json.dumps(data, indent=2) + "\n")
    agents.json_unmerge(ctx, "codex", ".codex/hooks.json")
    got = json.loads(p.read_text())
    assert got["model"] == "later"
    assert got["hooks"]["PreToolUse"][1:] == [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "added-by-the-user"}]}]


def test_nothing_recorded_means_the_file_is_not_touched(ctx, home):
    p = put(home, ".codex/hooks.json", {"hooks": {"PreToolUse": [group()]}})
    original = p.read_bytes()
    assert agents.json_unmerge(ctx, "codex", ".codex/hooks.json") is False
    assert p.read_bytes() == original and backups(p.parent) == []
    kit_setup.save_manifest(ctx, {"agents": {"codex": "oops"}})
    assert agents.json_unmerge(ctx, "codex", ".codex/hooks.json") is False
    assert p.read_bytes() == original


def test_a_file_that_vanished_just_drops_its_record(ctx, home):
    agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit())
    (home / ".codex" / "hooks.json").unlink()
    assert agents.json_unmerge(ctx, "codex", ".codex/hooks.json") is False
    assert ".codex/hooks.json" not in agents.peek(kit_setup.load_manifest(ctx), "codex").get("json", {})


def test_a_file_made_unreadable_since_is_an_error_and_the_record_stays(ctx, home):
    p = put(home, ".codex/hooks.json", EXISTING)
    agents.json_merge(ctx, "codex", ".codex/hooks.json", hooks_edit())
    p.write_text("{broken")
    with pytest.raises(kit_setup.SetupError):
        agents.json_unmerge(ctx, "codex", ".codex/hooks.json")
    assert p.read_text() == "{broken"
    assert ".codex/hooks.json" in agents.peek(kit_setup.load_manifest(ctx), "codex")["json"]


def test_setkey_owns_a_key_and_refuses_one_it_did_not_add(ctx, home):
    p = put(home, "g/hooks.json", {"other": {"a": 1}})
    original = p.read_bytes()
    value = {"PreToolUse": [group()]}
    assert agents.json_merge(ctx, "antigravity", "g/hooks.json", [agents.SetKey(["bertie"], value)]) is True
    assert json.loads(p.read_text()) == {"other": {"a": 1}, "bertie": value}
    newer = {"PreToolUse": [group(timeout=5)]}
    assert agents.json_merge(ctx, "antigravity", "g/hooks.json", [agents.SetKey(["bertie"], newer)]) is True
    assert json.loads(p.read_text())["bertie"] == newer              # ours, so it may be updated
    agents.json_unmerge(ctx, "antigravity", "g/hooks.json")
    assert p.read_bytes() == original
    # a "bertie" key somebody else wrote is never overwritten
    p.write_text(json.dumps({"bertie": {"theirs": True}}))
    with pytest.raises(kit_setup.SetupError, match="'bertie' already exists"):
        agents.json_merge(ctx, "antigravity", "g/hooks.json", [agents.SetKey(["bertie"], value)])
    assert json.loads(p.read_text()) == {"bertie": {"theirs": True}}


def test_require_adds_a_missing_key_leaves_an_equal_one_and_refuses_a_different_one(ctx, home):
    p = put(home, ".cursor/hooks.json", {"hooks": {}})
    original = p.read_bytes()
    agents.json_merge(ctx, "cursor", ".cursor/hooks.json", [agents.Require(["version"], 1)])
    assert json.loads(p.read_text())["version"] == 1
    agents.json_unmerge(ctx, "cursor", ".cursor/hooks.json")
    assert p.read_bytes() == original                                # the version key was ours
    p = put(home, ".cursor/hooks.json", {"version": 1})
    original = p.read_bytes()
    assert agents.json_merge(ctx, "cursor", ".cursor/hooks.json", [agents.Require(["version"], 1)]) is False
    agents.json_unmerge(ctx, "cursor", ".cursor/hooks.json")
    assert p.read_bytes() == original                                # theirs, so it stays
    for other in (2, "1", True, None):
        p = put(home, ".cursor/hooks.json", {"version": other})
        with pytest.raises(kit_setup.SetupError, match="version"):
            agents.json_merge(ctx, "cursor", ".cursor/hooks.json", [agents.Require(["version"], 1)])
        assert json.loads(p.read_text()) == {"version": other}


def test_json_paths_must_stay_inside_the_home_folder(ctx, home):
    for rel in ("../evil.json", "/tmp/evil.json", "", "a/../../evil.json"):
        with pytest.raises(kit_setup.SetupError, match="outside the home folder"):
            agents.json_merge(ctx, "codex", rel, hooks_edit())


# ================================================================ files wholly Bertie's

MARK = "# bertie rules file"
OURS = agents.marker_first_line(MARK)


def test_marker_first_line_and_skill_is_ours():
    assert OURS(MARK + "\nrest\n") and OURS(MARK + "\n") and OURS(MARK)
    assert not OURS("") and not OURS("\n" + MARK) and not OURS("x " + MARK) and not OURS("# other\n" + MARK)
    assert agents.skill_is_ours("---\nname: bertie\ndescription: x\n---\nbody")
    assert agents.skill_is_ours("---\ndescription: x\nname: bertie\n---\n")
    for text in ("", "name: bertie\n", "---\nname: other\n---\nname: bertie\n", "---\nname: bertie2\n---\n",
                 "---\nname: bertie-x\n---\n"):
        assert not agents.skill_is_ours(text)


def test_write_owned_file_makes_the_folders_records_them_and_removes_them_again(ctx, home):
    rel = ".codex/rules/bertie.rules"
    (home / ".codex").mkdir()
    assert agents.write_owned_file(ctx, "codex", rel, MARK + "\nrule\n", OURS) is True
    p = home / rel
    assert p.read_text() == MARK + "\nrule\n" and stat.S_IMODE(p.stat().st_mode) == 0o644
    rec = agents.peek(kit_setup.load_manifest(ctx), "codex")["files"][rel]
    assert rec["created_dirs"] == [".codex/rules"]
    assert agents.write_owned_file(ctx, "codex", rel, MARK + "\nrule\n", OURS) is False     # same text: no change
    assert agents.write_owned_file(ctx, "codex", rel, MARK + "\nnew\n", OURS) is True       # ours: updated
    assert p.read_text() == MARK + "\nnew\n"
    assert agents.remove_owned_file(ctx, "codex", rel, OURS) is True
    assert not p.exists() and dirs(home) == [".codex"]                # .codex was there before; .codex/rules was ours


def test_a_file_that_is_not_ours_is_never_overwritten(ctx, home):
    rel = ".codex/rules/bertie.rules"
    p = home / rel
    p.parent.mkdir(parents=True)
    p.write_text("# somebody else's rules\n")
    with pytest.raises(kit_setup.SetupError, match="was not made by this installer"):
        agents.write_owned_file(ctx, "codex", rel, MARK + "\nrule\n", OURS)
    assert p.read_text() == "# somebody else's rules\n" and not ctx.manifest_path.exists()
    p.unlink()
    target = home / "elsewhere"
    target.write_text(MARK + "\n")
    p.symlink_to(target)                                              # even a link to a file with our marker
    with pytest.raises(kit_setup.SetupError):
        agents.write_owned_file(ctx, "codex", rel, MARK + "\nrule\n", OURS)
    assert p.is_symlink() and target.read_text() == MARK + "\n"


def test_remove_owned_file_needs_a_record_and_still_checks_ownership(ctx, home):
    rel = ".codex/rules/bertie.rules"
    p = home / rel
    p.parent.mkdir(parents=True)
    p.write_text(MARK + "\nlooks like ours\n")
    assert agents.remove_owned_file(ctx, "codex", rel, OURS) is False   # not recorded: not ours to delete
    assert p.exists()
    agents.write_owned_file(ctx, "codex", rel, MARK + "\nrule\n", OURS)
    p.write_text("# the user replaced it\n")
    assert agents.remove_owned_file(ctx, "codex", rel, OURS) is False   # recorded, but no longer ours
    assert p.read_text() == "# the user replaced it\n"
    assert rel not in agents.peek(kit_setup.load_manifest(ctx), "codex").get("files", {})


def test_a_folder_with_something_else_in_it_is_not_removed(ctx, home):
    rel = ".codex/rules/bertie.rules"
    agents.write_owned_file(ctx, "codex", rel, MARK + "\n", OURS)
    (home / ".codex" / "rules" / "mine.rules").write_text("x")
    agents.remove_owned_file(ctx, "codex", rel, OURS)
    assert (home / ".codex" / "rules" / "mine.rules").read_text() == "x"


def test_owned_file_paths_must_stay_inside_the_home_folder(ctx, home):
    for rel in ("../x", "/tmp/x", "", "a/../../x"):
        with pytest.raises(kit_setup.SetupError, match="outside the home folder"):
            agents.write_owned_file(ctx, "codex", rel, MARK + "\n", OURS)


def test_a_tampered_manifest_cannot_make_uninstall_delete_folders_elsewhere(ctx, home, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    rel = ".codex/bertie.rules"
    agents.write_owned_file(ctx, "codex", rel, MARK + "\n", OURS)
    m = kit_setup.load_manifest(ctx)
    m["agents"]["codex"]["files"][rel]["created_dirs"] = ["../victim", str(victim), 5, None]
    kit_setup.save_manifest(ctx, m)
    agents.remove_owned_file(ctx, "codex", rel, OURS)
    assert victim.is_dir()


# ================================================================ SKILL.md for other agents

def kit_skill():
    return (KIT / "skill" / "bertie" / "SKILL.md").read_text()


def test_the_neutral_copy_of_the_real_skill_names_no_claude():
    text = agents.neutral_skill_text(kit_skill())
    assert "claude" not in text.lower()
    assert text.startswith("---\nname: bertie\n") and agents.skill_is_ours(text)


def test_the_neutral_copy_reads_naturally():
    text = agents.neutral_skill_text(kit_skill())
    assert "your coding agent's approval\nprompt, a Mac pop-up" in text
    assert "That triggers the approval prompt in your coding agent, then the Mac pop-up" in text
    assert "**The agent must never answer that window.**" in text
    assert "not give your agent's app the Accessibility permission" in text


def test_neutral_text_changes_nothing_but_those_words():
    original = kit_skill()
    neutral = agents.neutral_skill_text(original)
    assert len(neutral.splitlines()) in (len(original.splitlines()), len(original.splitlines()) - 1)
    assert agents.neutral_skill_text("No mention at all.\n") == "No mention at all.\n"


@pytest.mark.parametrize("text", ["Use Claude here.", "claude", "Ask CLAUDE Code", "the Claude desktop"])
def test_a_mention_nobody_reworded_is_an_error_not_a_leak(text):
    with pytest.raises(kit_setup.SetupError, match="names Claude"):
        agents.neutral_skill_text(text)


def test_claude_codes_own_copy_is_still_the_kits_file_byte_for_byte(home, ctx):
    agents.get("claude").install(ctx, KIT)
    assert (home / ".claude" / "skills" / "bertie" / "SKILL.md").read_bytes() == \
        (KIT / "skill" / "bertie" / "SKILL.md").read_bytes()


def test_a_skill_copy_of_one_agent_is_neutral_owned_and_recorded(ctx, home):
    rel = ".gemini/config/skills/bertie/SKILL.md"
    assert agents.install_skill_copy(ctx, "antigravity", rel, KIT) is True
    p = home / rel
    assert p.read_text() == agents.neutral_skill_text(kit_skill())
    assert agents.install_skill_copy(ctx, "antigravity", rel, KIT) is False
    assert agents.remove_skill_copy(ctx, "antigravity", rel) is True
    assert list(home.rglob("*")) == []


def test_a_skill_that_is_not_bertie_s_is_never_overwritten(ctx, home):
    rel = ".gemini/config/skills/bertie/SKILL.md"
    p = home / rel
    p.parent.mkdir(parents=True)
    p.write_text("---\nname: something-else\n---\nmine\n")
    with pytest.raises(kit_setup.SetupError):
        agents.install_skill_copy(ctx, "antigravity", rel, KIT)
    assert p.read_text() == "---\nname: something-else\n---\nmine\n"


def test_a_missing_kit_skill_is_a_clear_error(ctx, tmp_path):
    with pytest.raises(kit_setup.SetupError, match="missing"):
        agents.install_skill_copy(ctx, "antigravity", ".x/SKILL.md", tmp_path / "empty")


# ================================================================ the shared ~/.agents skill

SHARED = ".agents/skills/bertie/SKILL.md"


def test_the_shared_skill_is_written_once_and_removed_by_the_last_agent(ctx, home):
    assert agents.acquire_shared_skill(ctx, "cursor", KIT) is True
    p = home / SHARED
    assert p.read_text() == agents.neutral_skill_text(kit_skill())
    assert agents.acquire_shared_skill(ctx, "codex", KIT) is False        # already there: not rewritten
    assert agents.acquire_shared_skill(ctx, "codex", KIT) is False        # and not counted twice
    assert kit_setup.load_manifest(ctx)["shared_skill"]["users"] == ["cursor", "codex"]
    assert agents.release_shared_skill(ctx, "cursor") is False            # codex still uses it
    assert p.is_file()
    assert agents.release_shared_skill(ctx, "codex") is True
    assert not p.exists() and "shared_skill" not in kit_setup.load_manifest(ctx)
    assert list(home.rglob("*")) == []                                    # ~/.agents and below: gone with it


def test_releasing_an_agent_that_never_used_it_changes_nothing(ctx, home):
    agents.acquire_shared_skill(ctx, "kimi", KIT)
    assert agents.release_shared_skill(ctx, "cursor") is False
    assert (home / SHARED).is_file() and kit_setup.load_manifest(ctx)["shared_skill"]["users"] == ["kimi"]
    assert agents.release_shared_skill(ctx, "x") is False
    assert agents.release_shared_skill(kit_setup.Ctx(home, ctx.install_dir / "none"), "kimi") is False


def test_remove_shared_skill_ignores_the_count(ctx, home):
    agents.acquire_shared_skill(ctx, "cursor", KIT)
    agents.acquire_shared_skill(ctx, "kimi", KIT)
    assert agents.remove_shared_skill(ctx) is True
    assert list(home.rglob("*")) == [] and agents.remove_shared_skill(ctx) is False


def test_a_foreign_shared_skill_is_never_overwritten(ctx, home):
    p = home / SHARED
    p.parent.mkdir(parents=True)
    p.write_text("---\nname: another-tool\n---\n")
    with pytest.raises(kit_setup.SetupError):
        agents.acquire_shared_skill(ctx, "codex", KIT)
    assert p.read_text() == "---\nname: another-tool\n---\n" and "shared_skill" not in kit_setup.load_manifest(ctx)
    p.write_text("no front matter at all\n")
    with pytest.raises(kit_setup.SetupError):
        agents.acquire_shared_skill(ctx, "codex", KIT)
    assert p.read_text() == "no front matter at all\n"


def test_a_shared_skill_the_user_replaced_is_left_when_the_count_reaches_zero(ctx, home):
    agents.acquire_shared_skill(ctx, "codex", KIT)
    (home / SHARED).write_text("---\nname: mine-now\n---\n")
    assert agents.release_shared_skill(ctx, "codex") is False
    assert (home / SHARED).read_text() == "---\nname: mine-now\n---\n"


def test_an_existing_agents_folder_is_kept(ctx, home):
    (home / ".agents" / "skills").mkdir(parents=True)
    (home / ".agents" / "keep.txt").write_text("k")
    agents.acquire_shared_skill(ctx, "codex", KIT)
    agents.release_shared_skill(ctx, "codex")
    assert dirs(home) == [".agents", ".agents/skills"] and (home / ".agents" / "keep.txt").read_text() == "k"


# ================================================================ Claude Code's adapter

def test_the_claude_adapter_has_the_whole_interface():
    c = agents.get("claude")
    assert (c.KEY, c.LABEL, c.HOOK_STYLE) == ("claude", "Claude Code", "ask")
    for name in ("detect", "install", "uninstall", "hook_input", "hook_answer", "hook_failure"):
        assert callable(getattr(c, name)), name


def test_claude_is_detected_by_its_config_folder_only(home):
    c = agents.get("claude")
    assert c.detect(home) is False
    (home / ".claude").mkdir()
    assert c.detect(home) is True


def test_a_default_install_and_an_explicit_claude_install_are_identical(tmp_path):
    homes = []
    for name, keys in (("a", None), ("b", ["claude"])):
        h = tmp_path / name
        h.mkdir()
        c = kit_setup.Ctx(h)
        msgs = kit_setup.install(c, KIT, True, keys)
        homes.append((h, msgs))
    (ha, ma), (hb, mb) = homes
    assert ma == mb == ["installed the skill 'bertie'",
                        "approval rules and safety hook added to ~/.claude/settings.json",
                        "added ~/.local/bin to PATH in ~/.zshrc"]
    assert tree(ha) == tree(hb) and tree(ha)
    manifest = json.loads((ha / ".config" / "bertie" / "install-manifest.json").read_text())
    assert set(manifest) == {"commands", "settings", "path_line"}      # the keys v0.1 wrote, and no more


def test_installing_again_says_already_in(ctx):
    kit_setup.install(ctx, KIT, False)
    assert kit_setup.install(ctx, KIT, False) == ["installed the skill 'bertie'",
                                                  "approval rules and safety hook already in ~/.claude/settings.json"]


def test_a_claude_only_uninstall_says_what_it_always_said_in_the_same_order(ctx, home):
    kit_setup.install(ctx, KIT, True)
    assert kit_setup.uninstall_local(ctx) == [
        "removed the approval rules and safety hook from ~/.claude/settings.json",
        "removed the PATH line from ~/.zshrc",
        "removed the commands: bertie-mail, bertie-calendar, bertie-chat, bertie-drive, bertie",
        "removed the skill 'bertie'"]
    assert [p for p in home.rglob("*") if p.is_file() and ".bertie-backup-" not in p.name] == []


def test_an_uninstall_without_a_manifest_still_cleans_claude_code_as_before(ctx, home):
    kit_setup.install(ctx, KIT, True)
    ctx.manifest_path.unlink()
    done = kit_setup.uninstall_local(ctx)
    assert "removed the skill 'bertie'" in done and not (home / ".claude" / "skills" / "bertie").exists()
    assert kit_setup.HOOK_COMMAND not in (home / ".claude" / "settings.json").read_text() \
        if (home / ".claude" / "settings.json").exists() else True


def test_the_claude_hook_input_reads_the_tools_hook_py_has_always_read():
    c = agents.get("claude")
    assert c.hook_input({"tool_name": "Bash", "tool_input": {"command": "ls"}}) == ("command", "ls")
    assert c.hook_input({"tool_name": "mcp__terminal__run_in_terminal", "tool_input": {"cmd": "a", "b": ["c"]}}) \
        == ("command", "a\nc")
    assert c.hook_input({"tool_name": "Write", "tool_input": {"file_path": "/x", "content": "hi"}}) == ("file", "/x\nhi")
    assert c.hook_input({"tool_name": "Read", "tool_input": {"command": "bertie-mail send r1"}}) == ("none", "")
    assert c.hook_input({}) == ("none", "")
    kind, reason = c.hook_input({"tool_name": "mcp__computer-use__screenshot", "tool_input": {}})
    assert kind == "ask" and "screen-control tool" in reason
    for bad in ({"tool_name": "Bash", "tool_input": "ls"}, {"tool_name": "Bash", "tool_input": {}},
                {"tool_name": "Bash", "tool_input": {"command": 5}}, {"tool_name": "Bash"}):
        with pytest.raises(ValueError):
            c.hook_input(bad)


def test_the_claude_answers():
    c = agents.get("claude")
    assert c.hook_answer(None) == ("", 0) and c.hook_answer("") == ("", 0)
    assert c.hook_answer("why") == (json.dumps(hook.ask("why")) + "\n", 0)
    assert c.hook_failure("oops") == (json.dumps(hook.ask("oops")) + "\n", 0)


# ================================================================ kit_setup.install and uninstall_local with other agents

def test_other_agents_are_set_up_after_claude_code_and_the_path_line(ctx, home, fakes):
    msgs = kit_setup.install(ctx, KIT, True, ["claude", "kimi", "codex"])
    assert msgs == ["installed the skill 'bertie'",
                    "approval rules and safety hook added to ~/.claude/settings.json",
                    "added ~/.local/bin to PATH in ~/.zshrc",
                    "Kimi Code: set up (fake)", "Kimi Code: protected by the fake",
                    "Codex: set up (fake)", "Codex: protected by the fake"]


def test_the_order_of_keys_asked_for_is_kept_for_the_other_agents(ctx, fakes):
    msgs = kit_setup.install(ctx, KIT, False, ["kimi", "claude", "codex"])
    assert [m.split(":")[0] for m in msgs[2:]] == ["Kimi Code", "Kimi Code", "Codex", "Codex"]
    assert msgs[:2] == ["installed the skill 'bertie'", "approval rules and safety hook added to ~/.claude/settings.json"]


def test_options_reach_the_adapters(ctx, fakes):
    kit_setup.install(ctx, KIT, False, ["codex"], {"codex_sends": "allow"})
    assert agents.peek(kit_setup.load_manifest(ctx), "codex")["sends"] == "allow"


def test_without_options_the_default_choice_is_used(ctx, fakes):
    kit_setup.install(ctx, KIT, False, ["codex"])
    assert agents.peek(kit_setup.load_manifest(ctx), "codex")["sends"] == "prompt"


def test_an_unknown_or_missing_adapter_stops_the_install_before_anything_is_written(ctx, home, fakes, monkeypatch):
    for keys in (["claude", "nope"], ["claude", "kimi", "kimi2"]):
        with pytest.raises(kit_setup.SetupError, match="unknown agent"):
            kit_setup.install(ctx, KIT, True, keys)
    monkeypatch.setitem(sys.modules, "agents.cursor", None)
    with pytest.raises(kit_setup.SetupError, match="cursor adapter is missing"):
        kit_setup.install(ctx, KIT, True, ["claude", "cursor"])
    assert list(home.rglob("*")) == [] and not ctx.manifest_path.exists()


def test_a_repeated_key_is_set_up_once(ctx, fakes):
    msgs = kit_setup.install(ctx, KIT, False, ["codex", "codex"])
    assert msgs == ["Codex: set up (fake)", "Codex: protected by the fake"]


def test_install_and_uninstall_of_several_agents_leave_the_home_as_it_was(ctx, home, fakes):
    (home / ".kimi-code").mkdir()
    (home / ".zshrc").write_text("alias ll='ls -l'\n")
    before = tree(home)
    before_dirs = dirs(home)
    kit_setup.install(ctx, KIT, True, list(agents.ADAPTERS))
    assert (home / ".agents" / "skills" / "bertie" / "SKILL.md").is_file()
    assert (home / ".codex" / "hooks.json").is_file() and (home / ".gemini" / "bertie.note").is_file()
    done = kit_setup.uninstall_local(ctx)
    assert tree(home) == before and without_claude_leftovers(dirs(home)) == before_dirs
    assert "Codex: took the hook out" in done and "Cursor: removed the note" in done


def test_the_backups_line_appears_only_when_a_backup_was_really_left(ctx, home, fakes):
    kit_setup.install(ctx, KIT, False, ["codex"])                           # the hooks file is made by Bertie
    assert not any("backups named" in m for m in kit_setup.uninstall_local(ctx))
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex" / "hooks.json").write_text(json.dumps({"model": "x"}) + "\n")
    kit_setup.install(ctx, KIT, False, ["codex"])                           # the user's file is edited: backed up
    done = kit_setup.uninstall_local(ctx)
    assert done[-1] == "backups named *.bertie-backup-* were left beside the files they belong to"
    assert json.loads((home / ".codex" / "hooks.json").read_text()) == {"model": "x"}


def test_the_shared_skill_goes_with_the_last_agent_that_used_it(ctx, home, fakes):
    kit_setup.install(ctx, KIT, False, ["cursor", "codex"])
    assert (home / SHARED).is_file()
    agents.get("cursor").uninstall(ctx)
    assert (home / SHARED).is_file()
    agents.get("codex").uninstall(ctx)
    assert not (home / ".agents").exists()


def test_a_codex_only_install_leaves_claude_codes_files_alone_on_uninstall(ctx, home, fakes):
    skill = home / ".claude" / "skills" / "bertie" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: bertie\n---\nmy own copy\n")
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"permissions": {"ask": ["Bash(bertie-mail send:*)"]}}))
    before = tree(home)
    kit_setup.install(ctx, KIT, True, ["codex"])
    done = kit_setup.uninstall_local(ctx)
    assert tree(home) == before
    assert not any("settings.json" in m or "the skill" in m for m in done)


def test_garbage_and_strangers_in_the_manifest_never_cause_a_deletion(ctx, home, fakes):
    innocent = home / "keep.txt"
    innocent.write_text("keep")
    note = home / ".codex" / "bertie.note"
    note.parent.mkdir()
    note.write_text("# bertie fake adapter codex\nnot recorded\n")
    kit_setup.save_manifest(ctx, {"agents": {"codex": "oops", "mystery": {"files": {"keep.txt": {}}},
                                             "kimi": {"files": {"../x": {"created_dirs": ["../.."]}}}},
                                  "shared_skill": "junk"})
    done = kit_setup.uninstall_local(ctx)
    assert innocent.read_text() == "keep" and note.exists()
    assert any("'mystery'" in m and "left" in m for m in done)


def test_a_recorded_agent_whose_adapter_is_missing_is_left_alone_and_said_so(ctx, home, fakes, monkeypatch):
    kit_setup.install(ctx, KIT, False, ["cursor", "codex"])
    monkeypatch.setitem(sys.modules, "agents.cursor", None)
    done = kit_setup.uninstall_local(ctx)
    assert any("'cursor'" in m and "left" in m for m in done)
    assert (home / ".cursor" / "bertie.note").is_file()                    # untouched
    assert not (home / ".codex").exists()                                  # the other agent was removed


def test_an_adapter_that_cannot_finish_does_not_stop_the_others(ctx, home, fakes):
    kit_setup.install(ctx, KIT, False, ["cursor", "codex"])
    (home / ".cursor" / "hooks.json").write_text("{broken")
    done = kit_setup.uninstall_local(ctx)
    assert any(m.startswith("Cursor: could not remove everything") for m in done)
    assert (home / ".cursor" / "hooks.json").read_text() == "{broken"
    assert not (home / ".codex").exists()


def test_the_shared_skill_is_removed_even_if_its_agents_left_no_record(ctx, home):
    agents.acquire_shared_skill(ctx, "codex", KIT)
    done = kit_setup.uninstall_local(ctx)
    assert not (home / ".agents").exists()
    assert any("shared skill" in m for m in done)


# ================================================================ kit_setup's command line

def test_resolve_prints_the_agents_an_install_would_set_up(home, fakes, capsys):
    kit_setup.main(["resolve"])
    assert capsys.readouterr().out == "claude\n"                           # an empty home: Claude Code alone
    (home / ".codex").mkdir()
    kit_setup.main(["resolve"])
    assert capsys.readouterr().out == "claude,codex\n"
    kit_setup.main(["resolve", "--agents", "kimi,claude"])
    assert capsys.readouterr().out == "claude,kimi\n"
    kit_setup.main(["resolve", "--agents", "all", "--names"])
    assert capsys.readouterr().out == "Claude Code, Antigravity, Cursor, Codex or Kimi Code\n"
    kit_setup.main(["resolve", "--agents", "claude", "--names"])
    assert capsys.readouterr().out == "Claude Code\n"


@pytest.mark.parametrize("args", [["--agents", "bogus"], ["--agents", ""], ["--agents", "claude,"],
                                  ["--codex-sends", "maybe"]])
def test_a_bad_agent_list_or_choice_exits_2_and_touches_nothing(home, args):
    with pytest.raises(SystemExit) as e:
        kit_setup.main(["install", "--kit-dir", str(KIT)] + args)
    assert e.value.code == 2
    assert list(home.rglob("*")) == [] and not (TOOLS / kit_setup.MANIFEST_NAME).exists()


def test_run_as_a_script_a_setup_error_is_a_clean_exit_not_a_traceback(home, tmp_path):
    """kit_setup.py is `__main__` when run; the adapters must raise the very SetupError it catches."""
    d = tmp_path / "scriptkit"
    shutil.copytree(TOOLS, d, ignore=shutil.ignore_patterns("__pycache__"))
    for key in FAKES:
        (d / "agents" / f"{key}.py").write_text(fake_source(key))
    (home / ".codex").mkdir()
    (home / ".codex" / "bertie.note").write_text("# not ours\n")
    r = subprocess.run([sys.executable, "-I", str(d / "kit_setup.py"), "install", "--kit-dir", str(KIT),
                        "--agents", "claude,codex"], env={"HOME": str(home), "PATH": os.environ["PATH"]},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "Traceback" not in r.stderr
    assert "already exists and was not made by this installer" in r.stderr
    assert (home / ".codex" / "bertie.note").read_text() == "# not ours\n"


# ================================================================ hook.py --agent

def answer(key, payload, raw=None):
    return hook.agent_answer(key, io.StringIO(raw if raw is not None else json.dumps(payload)))


def decision(out):
    return json.loads(out)


def test_agent_from_argv():
    f = hook.agent_from_argv
    assert f([]) is None and f(["x"]) is None and f(["--other", "codex"]) is None
    assert f(["--agent", "codex"]) == "codex" and f(["--agent=kimi"]) == "kimi"
    assert f(["--agent"]) == "" and f(["--agent="]) == ""
    assert f(["a", "--agent", "cursor", "b"]) == "cursor"


def test_an_ask_style_agent_is_asked_in_its_own_format(fakes):
    out, code = answer("cursor", {"command": "echo hi; bertie-mail send r1"})
    assert code == 0 and decision(out)["fake"] == "ask"
    assert "bertie-mail send" in decision(out)["reason"] and out.endswith("\n")
    assert decision(answer("cursor", {"command": "ls -la"})[0]) == {"fake": "allow"}
    assert decision(answer("cursor", {})[0]) == {"fake": "allow"}                      # kind none
    assert decision(answer("cursor", {"ask": "screen"})[0]) == {"fake": "ask", "reason": "screen"}


def test_an_ask_style_agent_reads_files_for_the_bypass_phrases(fakes):
    out, _ = answer("cursor", {"file": "cp x ~/.config/bertie/common.py"})
    assert decision(out)["fake"] == "ask" and "written to a file" in decision(out)["reason"]
    assert decision(answer("cursor", {"file": "print('hello')"})[0]) == {"fake": "allow"}
    assert decision(answer("cursor", {"file": ""})[0]) == {"fake": "allow"}


@pytest.mark.parametrize("cmd", ATTACKS)
def test_every_attack_string_is_asked_under_an_ask_style_agent(fakes, cmd):
    out, code = answer("cursor", {"command": cmd})
    assert code == 0 and decision(out)["fake"] == "ask", cmd


@pytest.mark.parametrize("cmd", [c for c in FREE if c.strip()])
def test_harmless_commands_are_let_through_under_an_ask_style_agent(fakes, cmd):
    assert decision(answer("cursor", {"command": cmd})[0]) == {"fake": "allow"}, cmd


def test_unreadable_input_fails_to_the_safe_answer_of_each_style(fakes):
    for raw in ("not json", "[]", '"s"', "5", '{"boom": 1}', '{"command": 5}',
                '{"command": null}', '{"file": 7}'):
        out, code = answer("cursor", None, raw)
        assert code == 0 and decision(out)["fake"] == "ask" and "could not read" in decision(out)["reason"], raw
        out, code = answer("codex", None, raw)
        h = decision(out)["hookSpecificOutput"]
        assert code == 0 and h["permissionDecision"] == "deny" and "could not read" in h["permissionDecisionReason"], raw
        assert "blocking to be safe" in h["permissionDecisionReason"]


def test_a_blank_command_is_nothing_to_check_exactly_as_for_claude_code(fakes):
    assert decision(answer("cursor", {"command": "   "})[0]) == {"fake": "allow"}
    assert answer("codex", {"command": ""}) == ("", 0)


def test_empty_input_is_no_opinion(fakes):
    assert answer("cursor", None, "") == (json.dumps({"fake": "allow"}) + "\n", 0)
    assert answer("codex", None, "") == ("", 0)


def test_input_that_cannot_even_be_decoded_fails_safe(fakes):
    class Bad:
        def read(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    out, code = hook.agent_answer("kimi", Bad())
    assert code == 0 and decision(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_an_adapter_that_misbehaves_is_a_failure_not_an_allow(fakes):
    fakes["cursor"].MODE = "code2"
    out, code = answer("cursor", {"command": "ls"})
    assert code == 0 and decision(out)["fake"] == "ask" and "ValueError" in decision(out)["reason"]
    fakes["cursor"].MODE = "raise"                                         # even hook_failure breaks: the fallback speaks
    out, code = answer("cursor", {"command": "ls"})
    assert code == 0 and decision(out) == {"permission": "deny", "user_message": decision(out)["user_message"],
                                           "agent_message": decision(out)["agent_message"]}
    fakes["codex"].MODE = "raise"
    h = decision(answer("codex", {"command": "ls"})[0])["hookSpecificOutput"]
    assert h["permissionDecision"] == "deny"


def test_an_adapter_with_an_unknown_hook_style_fails_safe(fakes):
    fakes["cursor"].HOOK_STYLE = "maybe"
    out, _ = answer("cursor", {"command": "ls"})
    assert decision(out)["fake"] == "ask" and "ValueError" in decision(out)["reason"]


# ---- block-only agents

def blocked(out):
    h = decision(out)["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse" and h["permissionDecision"] == "deny"
    return h["permissionDecisionReason"]


def test_a_plain_gated_command_is_left_to_the_agents_own_rule_when_the_rule_covers_it(fakes):
    for cmd in ("bertie-mail send r1", "bertie-chat dm a@test.invalid hi", "bertie logout",
                "bertie-drive share F a@test.invalid --role reader", "bertie-mail send"):
        assert answer("codex", {"command": cmd}) == ("", 0), cmd


def test_a_gated_command_the_rule_would_miss_is_blocked_with_the_instruction(fakes):
    for cmd in ("echo hi; bertie-mail send r1", "bertie-mail --json send r1", "BERTIE-MAIL send r1",
                "cd /tmp && bertie-mail send r1", 'bertie-mail "se""nd" r1', "bertie-mail send r1/.x"):
        reason = blocked(answer("codex", {"command": cmd})[0])
        assert reason.endswith("Run the Bertie command on its own, starting the line, e.g. "
                               "`bertie-mail send ...` (use --body-file for text with special characters), "
                               "so Codex asks you first."), cmd
        assert reason.startswith("Bertie safety check: ")


def test_the_reason_names_the_agent_it_is_for(fakes):
    assert blocked(answer("kimi", {"command": "x; bertie-mail send r1"})[0]).endswith("so Kimi Code asks you first.")


def test_a_gated_command_with_anything_else_wrong_is_blocked_even_when_the_rule_covers_its_start(fakes):
    for cmd in ("bertie-mail send r1 && ~/.config/bertie/.venv/bin/python x",
                "bertie-mail send r1; security find-generic-password -s bertie -w",
                "bertie-chat send spaces/A hi & osascript -e beep",
                "bertie-mail send r1 && rm -rf ~/.config/bertie"):
        assert "Run the Bertie command on its own" in blocked(answer("codex", {"command": cmd})[0]), cmd


def test_anything_that_is_not_a_gated_action_is_blocked(fakes):
    for cmd in BYPASS[:5] + KEYCHAIN[:3] + WINDOW_DRIVING[:3] + PATTERNS[:3] + UNREADABLE:
        reason = blocked(answer("kimi", {"command": cmd})[0])
        if hook.decide_parts(cmd)[0]:                         # a bertie command whose action cannot be read
            assert "Run the Bertie command on its own" in reason, cmd
        else:                                                 # no send among the reasons (round 5, H2)
            assert reason.endswith("This touches Bertie's safety settings; ask the user to do it by hand if it is needed."), cmd
            assert "Run the Bertie command on its own" not in reason, cmd


def test_harmless_commands_and_other_tools_have_no_opinion_under_a_block_only_agent(fakes):
    for cmd in [c for c in FREE if c.strip()]:
        assert answer("codex", {"command": cmd}) == ("", 0), cmd
    assert answer("codex", {}) == ("", 0)


def test_file_writes_that_name_the_bypass_phrases_are_blocked_without_the_command_instruction(fakes):
    reason = blocked(answer("codex", {"file": "cp x ~/.config/bertie/common.py"})[0])
    assert "written to a file" in reason and "Run the Bertie command" not in reason
    assert answer("codex", {"file": "print('hi')"}) == ("", 0)
    assert blocked(answer("codex", {"ask": "screen control"})[0]) == "screen control"


@pytest.mark.parametrize("cmd", ATTACKS)
def test_no_attack_string_gets_past_a_block_only_agent(fakes, cmd):
    """Blocked, unless it is a gated action on its own that the agent's rule surely covers."""
    out, code = answer("codex", {"command": cmd})
    assert code == 0
    if out:
        blocked(out)
        return
    gated, other = hook.decide_parts(cmd)
    assert gated and not other and fakes["codex"].rules_cover(cmd) is True, cmd


def test_only_a_strict_true_from_rules_cover_counts(fakes):
    fakes["codex"].MODE = "truthy"
    assert "Run the Bertie command" in blocked(answer("codex", {"command": "bertie-mail send r1"})[0])


def test_a_rules_cover_that_breaks_blocks(fakes, monkeypatch):
    def broken(command):
        raise RuntimeError("bug")
    monkeypatch.setattr(fakes["kimi"], "rules_cover", broken)
    h = decision(answer("kimi", {"command": "bertie-mail send r1"})[0])["hookSpecificOutput"]
    assert h["permissionDecision"] == "deny" and "RuntimeError" in h["permissionDecisionReason"]


@pytest.mark.parametrize("cmd", ATTACKS + [c for c in FREE if c] + ["$'\\x62'ertie logout", "echo " + "a" * 30000, ""])
def test_decide_parts_is_decide_taken_apart(cmd):
    gated, other = hook.decide_parts(cmd)
    assert hook._finish(gated + other) == hook.decide(cmd), cmd


def test_decide_parts_separates_gated_from_the_rest():
    assert hook.decide_parts("bertie-mail send r1")[1] == [] and len(hook.decide_parts("bertie-mail send r1")[0]) == 1
    g, o = hook.decide_parts("bertie-mail send r1; cat ~/.config/bertie/account")
    assert len(g) == 1 and len(o) == 1
    assert hook.decide_parts("ls") == ([], [])
    g, o = hook.decide_parts("bertie-mail $ACTION r1")
    assert len(g) == 1 and o == []                                          # "unreadable subcommand" comes from gated_hits


# ---- the adapter cannot be used

def test_an_unknown_agent_gets_the_claude_style_ask():
    for key in ("nope", "", "Claude", "../x"):
        out, code = answer(key, {"command": "ls"})
        h = decision(out)["hookSpecificOutput"]
        assert code == 0 and h["permissionDecision"] == "ask" and "could not load" in h["permissionDecisionReason"]


@pytest.mark.parametrize("key,check", [
    ("codex", lambda d: d["hookSpecificOutput"]["permissionDecision"] == "deny"),
    ("kimi", lambda d: d["hookSpecificOutput"]["permissionDecision"] == "deny"),
    ("cursor", lambda d: d["permission"] == "deny" and d["user_message"] == d["agent_message"]),
    ("antigravity", lambda d: d["decision"] == "force_ask" and d["reason"]),
])
def test_an_adapter_that_is_not_there_is_answered_in_that_agents_own_safe_format(monkeypatch, key, check):
    monkeypatch.setitem(sys.modules, "agents." + key, None)
    out, code = answer(key, {"command": "bertie-mail send r1"})
    assert code == 0 and out.endswith("\n") and check(decision(out)), out


# ---- Claude through the same path

CLAUDE_PAYLOADS = (
    [{"tool_name": "Bash", "tool_input": {"command": c}} for c in ATTACKS + FREE]
    + [{"tool_name": "mcp__terminal__run_in_terminal", "tool_input": {"cmd": c}} for c in GATED[:4] + FREE[:4]]
    + [{"tool_name": t, "tool_input": p} for t, p in (
        ("Write", {"file_path": "/tmp/a.sh", "content": "cp x ~/.config/bertie/bertie-approve"}),
        ("Edit", {"file_path": "/tmp/a", "old_string": "a", "new_string": "approve.sha256"}),
        ("Write", {"file_path": "/tmp/a.py", "content": "print(1)"}),
        ("MultiEdit", {"edits": [{"new_string": "importlib.import_module('svc_'+'mail')"}]}),
        ("mcp__computer-use__screenshot", {}), ("mcp__computer-use__left_click", {"coordinate": [1, 2]}),
        ("Read", {"command": "bertie-mail send r1"}), ("mcp__other__tool", {"x": 1}), ("Bash", {}),
        ("Bash", "ls"), ("Bash", {"command": 5}), ("Bash", {"command": ""}), (None, None), (5, {"a": "b"}))]
    + [{}, {"tool_name": "Bash"}, {"tool_input": {"command": "bertie-mail send r1"}}]
)


def legacy(monkeypatch, raw):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    monkeypatch.setattr(sys, "stdout", out)
    hook.main()
    return out.getvalue()


def test_hook_agent_claude_answers_exactly_like_hook_py_without_arguments(monkeypatch):
    raws = [json.dumps(p) for p in CLAUDE_PAYLOADS] + ["", "not json", "[]", "5", '"x"', "null"]
    for raw in raws:
        expected = legacy(monkeypatch, raw)
        assert hook.agent_answer("claude", io.StringIO(raw)) == (expected, 0), raw


def run_script(args, stdin, cwd=None, script=None):
    return subprocess.run([sys.executable, "-I", str(script or TOOLS / "hook.py")] + args, input=stdin,
                          capture_output=True, text=True, timeout=30, cwd=cwd)


@pytest.mark.parametrize("payload", [
    {"tool_name": "Bash", "tool_input": {"command": "bertie-mail send r1"}},
    {"tool_name": "Bash", "tool_input": {"command": "ls"}},
    {"tool_name": "Write", "tool_input": {"file_path": "/x", "content": "cp x ~/.config/bertie/common.py"}},
    {"tool_name": "mcp__computer-use__screenshot", "tool_input": {}},
])
def test_run_as_a_script_the_claude_agent_flag_changes_nothing(payload):
    plain = run_script([], json.dumps(payload))
    flagged = run_script(["--agent", "claude"], json.dumps(payload))
    assert (flagged.returncode, flagged.stdout, flagged.stderr) == (plain.returncode, plain.stdout, plain.stderr)
    assert plain.returncode == 0 and plain.stderr == ""


def test_without_arguments_hook_py_needs_neither_the_agents_package_nor_kit_setup(tmp_path):
    only = tmp_path / "only"
    only.mkdir()
    for name in ("hook.py", "registry.py"):
        shutil.copy(TOOLS / name, only / name)
    p = run_script([], json.dumps({"tool_name": "Bash", "tool_input": {"command": "bertie-mail send r1"}}),
                   script=only / "hook.py")
    assert p.returncode == 0 and json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert not (only / "agents").exists()


# ---- the same, as separate processes through a copy of the kit

@pytest.fixture
def kit_copy(tmp_path):
    """tools/ as it is installed, with the four other adapters replaced by the fakes."""
    d = tmp_path / "installed"
    shutil.copytree(TOOLS, d, ignore=shutil.ignore_patterns("__pycache__"))
    for key in FAKES:
        (d / "agents" / f"{key}.py").write_text(fake_source(key))
    return d


def test_hook_py_as_a_process_speaks_each_agents_format_and_always_exits_zero(kit_copy):
    p = run_script(["--agent", "cursor"], json.dumps({"command": "x; bertie-mail send r1"}), script=kit_copy / "hook.py")
    assert p.returncode == 0 and json.loads(p.stdout)["fake"] == "ask" and p.stderr == ""
    p = run_script(["--agent=codex"], json.dumps({"command": "x; bertie-mail send r1"}), script=kit_copy / "hook.py")
    assert p.returncode == 0 and blocked(p.stdout).endswith("so Codex asks you first.")
    p = run_script(["--agent", "codex"], json.dumps({"command": "bertie-mail send r1"}), script=kit_copy / "hook.py")
    assert p.returncode == 0 and p.stdout == ""
    p = run_script(["--agent", "kimi"], "garbage", script=kit_copy / "hook.py")
    assert p.returncode == 0 and decision(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_adapter_and_hook_py_share_one_module_when_run_as_a_script(kit_copy):
    p = run_script(["--agent", "cursor"], json.dumps({"probe": 1}), script=kit_copy / "hook.py")
    assert json.loads(p.stdout) == {"fake": "ask", "reason": "same:True"}


def test_a_broken_adapter_file_is_answered_in_the_agents_safe_format(kit_copy):
    (kit_copy / "agents" / "cursor.py").write_text("this is not python (\n")
    p = run_script(["--agent", "cursor"], json.dumps({"command": "ls"}), script=kit_copy / "hook.py")
    d = json.loads(p.stdout)
    assert p.returncode == 0 and d["permission"] == "deny"
    (kit_copy / "agents" / "kimi.py").unlink()
    p = run_script(["--agent", "kimi"], json.dumps({"command": "ls"}), script=kit_copy / "hook.py")
    assert p.returncode == 0 and decision(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_stray_argument_or_a_bad_agent_flag(kit_copy):
    stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "bertie-mail send r1"}})
    p = run_script(["--whatever"], stdin, script=kit_copy / "hook.py")        # ignored, as it always was
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    p = run_script(["--agent"], stdin, script=kit_copy / "hook.py")
    assert p.returncode == 0 and json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    p = run_script(["--agent", "nope"], stdin, script=kit_copy / "hook.py")
    assert p.returncode == 0 and json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


# ================================================================ hook.sh

def fake_python(directory, log=None, prints="", code=0):
    """A .venv/bin/python that logs each call and answers `prints` with exit `code`."""
    py = directory / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    body = "#!/bin/sh\n"
    if log:
        body += f'echo "$@" >> "{log}"\ncat > /dev/null\n'
    if prints:
        body += f"printf '%s' '{prints}'\n"
    body += f"exit {code}\n"
    py.write_text(body)
    py.chmod(0o755)
    return py


def run_sh(directory, args, stdin, home, tmp_path, path=None):
    env = env_for(home, tmp_path)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(["sh", str(directory / "hook.sh")] + args, input=stdin, capture_output=True, text=True,
                          timeout=60, env=env)


@pytest.fixture
def lonely(tmp_path):
    """An install folder with hook.sh and nothing else: no hook.py, so no Python can run it."""
    d = tmp_path / "lonely"
    d.mkdir()
    shutil.copy(TOOLS / "hook.sh", d / "hook.sh")
    return d


ASK_JSON = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                   "permissionDecisionReason":
                                       "Safety check could not start (no working Python); asking to be safe"}}


@pytest.mark.parametrize("args", [[], ["--agent", "claude"], ["--agent=claude"], ["--agent", "nope"], ["--agent"],
                                  ["--other"]])
def test_hook_sh_without_python_asks_the_claude_way_for_claude_and_for_anything_unknown(lonely, home, tmp_path, args):
    r = run_sh(lonely, args, json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}), home, tmp_path)
    assert r.returncode == 0 and r.stderr == "" and json.loads(r.stdout) == ASK_JSON


@pytest.mark.parametrize("args", [["--agent", "codex"], ["--agent=codex"], ["--agent", "kimi"], ["--agent=kimi"]])
def test_hook_sh_without_python_blocks_codex_and_kimi_with_a_message_and_exit_2(lonely, home, tmp_path, args):
    r = run_sh(lonely, args, "{}", home, tmp_path)
    assert r.returncode == 2 and r.stdout == ""
    assert r.stderr.strip() == "Safety check could not start (no working Python); blocking to be safe"


@pytest.mark.parametrize("args", [["--agent", "cursor"], ["--agent=cursor"]])
def test_hook_sh_without_python_denies_cursor(lonely, home, tmp_path, args):
    r = run_sh(lonely, args, "{}", home, tmp_path)
    d = json.loads(r.stdout)
    assert r.returncode == 0 and r.stderr == "" and set(d) == {"permission", "user_message", "agent_message"}
    assert d["permission"] == "deny"
    assert "no working Python" in d["user_message"] and d["user_message"] == d["agent_message"]


@pytest.mark.parametrize("args", [["--agent", "antigravity"], ["--agent=antigravity"]])
def test_hook_sh_without_python_makes_antigravity_ask(lonely, home, tmp_path, args):
    r = run_sh(lonely, args, "{}", home, tmp_path)
    d = json.loads(r.stdout)
    assert r.returncode == 0 and r.stderr == "" and set(d) == {"decision", "reason"}
    assert d["decision"] == "force_ask" and "no working Python" in d["reason"]


def test_the_fallbacks_match_what_hook_py_says_when_it_cannot_load_an_adapter(lonely, home, tmp_path):
    for key in ("codex", "kimi", "cursor", "antigravity", "claude"):
        sh = run_sh(lonely, ["--agent", key], "{}", home, tmp_path)
        py = hook.fallback_answer(key, "x")
        if key in ("codex", "kimi"):
            assert sh.returncode == 2 and json.loads(py)["hookSpecificOutput"]["permissionDecision"] == "deny"
        else:
            assert set(json.loads(sh.stdout)) == set(json.loads(py)), key
            assert list(json.loads(sh.stdout)) == list(json.loads(py)), key


def test_hook_sh_hands_its_arguments_to_hook_py_and_passes_the_answer_through_once(tmp_path, home):
    d = tmp_path / "inst"
    d.mkdir()
    shutil.copy(TOOLS / "hook.sh", d / "hook.sh")
    (d / "hook.py").write_text("")
    log = tmp_path / "calls.log"
    deny = '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"r"}}'
    fake_python(d, log=log, prints=deny)
    r = run_sh(d, ["--agent", "codex"], '{"tool_name": "Bash"}', home, tmp_path)
    assert r.returncode == 0 and r.stdout == deny + "\n" and r.stderr == ""
    calls = log.read_text().splitlines()
    assert calls == [f"-I {d}/hook.py --agent codex"]                        # once, with the flag and its value


def test_hook_sh_with_no_arguments_still_calls_hook_py_with_none(tmp_path, home):
    d = tmp_path / "inst"
    d.mkdir()
    shutil.copy(TOOLS / "hook.sh", d / "hook.sh")
    log = tmp_path / "calls.log"
    fake_python(d, log=log)
    r = run_sh(d, [], "{}", home, tmp_path)
    assert r.returncode == 0 and r.stdout == "" and log.read_text().splitlines() == [f"-I {d}/hook.py"]


def test_hook_sh_still_tries_the_next_python_when_one_exits_nonzero(tmp_path, home):
    d = tmp_path / "inst"
    d.mkdir()
    shutil.copy(TOOLS / "hook.sh", d / "hook.sh")
    log = tmp_path / "calls.log"
    fake_python(d, log=log, code=3)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").write_text(f'#!/bin/sh\ncat > /dev/null\necho "second $@" >> "{log}"\nprintf \'%s\' \'{{"ok":1}}\'\n')
    (bin_dir / "python3").chmod(0o755)
    r = run_sh(d, ["--agent", "cursor"], "{}", home, tmp_path, path=str(bin_dir) + os.pathsep + "/usr/bin:/bin")
    assert r.returncode == 0 and r.stdout == '{"ok":1}\n'
    assert log.read_text().splitlines() == [f"-I {d}/hook.py --agent cursor", f"second -I {d}/hook.py --agent cursor"]


def test_hook_sh_end_to_end_with_a_real_python(kit_copy, home, tmp_path):
    """hook.sh from the install folder, a real interpreter, the fake adapters: the whole chain."""
    shutil.copy(TOOLS / "hook.sh", kit_copy / "hook.sh")
    py = kit_copy / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    r = run_sh(kit_copy, ["--agent", "codex"], json.dumps({"command": "x; bertie-mail send r1"}), home, tmp_path)
    assert r.returncode == 0 and "so Codex asks you first." in blocked(r.stdout)
    r = run_sh(kit_copy, ["--agent", "cursor"], json.dumps({"command": "ls"}), home, tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout) == {"fake": "allow"}
    r = run_sh(kit_copy, [], json.dumps({"tool_name": "Bash", "tool_input": {"command": "bertie-mail send r1"}}),
               home, tmp_path)
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


# ================================================================ install.sh --agent

ARGS = (ACCOUNT, "--skip-deps", "--skip-login")


def nothing_touched(home):
    return not (home / ".config").exists() and not (home / ".claude").exists() and not (home / ".local").exists()


@pytest.mark.parametrize("extra", [
    ["--agent", "bogus"], ["--agent=bogus"], ["--agent", "claude,bogus"], ["--agent", "Claude"],
    ["--agent", "claude,,kimi"], ["--agent", "claude,"], ["--agent", ",claude"], ["--agent"], ["--agent="],
    ["--agent", ""], ["--agent", "claude kimi"], ["--agent", "*"], ["--agent", "../x"],
    ["--codex-sends"], ["--codex-sends", "maybe"], ["--codex-sends=maybe"], ["--codex-sends="],
])
def test_a_bad_agent_option_exits_2_and_touches_nothing(home, tmp_path, extra):
    r = run_install(home, tmp_path, *ARGS, *extra)
    assert r.returncode == 2, r.stdout + r.stderr
    assert nothing_touched(home)
    assert "usage" in r.stdout


def test_an_unknown_agent_is_named_in_the_message(home, tmp_path):
    r = run_install(home, tmp_path, *ARGS, "--agent", "claude,emacs")
    assert r.returncode == 2 and "unknown agent: 'emacs'" in r.stdout and "claude, antigravity, cursor, codex, kimi" in r.stdout


def test_the_help_text_describes_the_new_options(home, tmp_path):
    r = run_install(home, tmp_path, "--help")
    assert r.returncode == 0 and "--agent" in r.stdout and "--codex-sends" in r.stdout and nothing_touched(home)


def test_with_no_agent_folders_the_default_is_claude_code_alone_exactly_as_before(home, tmp_path):
    r = run_install(home, tmp_path, *ARGS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'Done. Open a NEW terminal, start Claude Code, and say:' in r.stdout
    assert "  installed the skill 'bertie'" in r.stdout and "  approval rules and safety hook added to" in r.stdout
    assert (home / ".claude" / "settings.json").is_file() and not (home / ".agents").exists()
    for folder in (".codex", ".cursor", ".gemini", ".kimi-code"):
        assert not (home / folder).exists(), folder
    manifest = json.loads((home / ".config" / "bertie" / "install-manifest.json").read_text())
    assert set(manifest) == {"commands", "settings", "path_line"}


def test_agent_claude_is_byte_identical_to_the_default_install(tmp_path):
    results = []
    for name, extra in (("default", []), ("explicit", ["--agent", "claude"]), ("equals", ["--agent=claude"])):
        h = tmp_path / name
        h.mkdir()
        r = run_install(h, tmp_path, *ARGS, *extra)
        assert r.returncode == 0, r.stdout + r.stderr
        results.append((tree(h), r.stdout.replace(str(h), "HOME")))
    assert results[0][0] == results[1][0] == results[2][0]
    assert results[0][1] == results[1][1] == results[2][1]            # even what it printed
    assert any(k.endswith("install-manifest.json") for k in results[0][0])


def test_the_agents_folder_is_copied_read_only_and_a_rerun_replaces_it(home, tmp_path):
    assert run_install(home, tmp_path, *ARGS).returncode == 0
    d = home / ".config" / "bertie"
    names = sorted(p.name for p in (d / "agents").glob("*.py"))
    assert names == sorted(p.name for p in (TOOLS / "agents").glob("*.py")) and "__init__.py" in names
    for f in (d / "agents").glob("*.py"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o400, f
    (d / "agents" / "stale.py").write_text("old")
    assert run_install(home, tmp_path, *ARGS).returncode == 0
    assert not (d / "agents" / "stale.py").exists()
    assert (d / "agents" / "claude.py").read_bytes() == (TOOLS / "agents" / "claude.py").read_bytes()


def test_the_installed_hook_runs_an_agent_flag_from_the_install_folder(home, tmp_path):
    assert run_install(home, tmp_path, *ARGS).returncode == 0
    d = home / ".config" / "bertie"
    r = subprocess.run(["sh", str(d / "hook.sh"), "--agent", "claude"], env=env_for(home, tmp_path),
                       capture_output=True, text=True, timeout=60,
                       input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "true && bertie-mail send r1"}}))
    assert r.returncode == 0 and json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def installed_with_fakes(home, tmp_path, fake_kit, *extra):
    return run_install(home, tmp_path, *ARGS, *extra, kit=fake_kit)


def test_agents_found_in_the_home_folder_are_set_up_with_claude_code(home, tmp_path, fake_kit):
    (home / ".kimi-code").mkdir()
    r = installed_with_fakes(home, tmp_path, fake_kit)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "  Kimi Code: set up (fake)" in r.stdout and "  Kimi Code: protected by the fake" in r.stdout
    assert "Done. Open a NEW terminal, start Claude Code or Kimi Code, and say:" in r.stdout
    assert (home / ".claude" / "settings.json").is_file() and (home / ".kimi-code" / "bertie.note").is_file()
    assert (home / ".agents" / "skills" / "bertie" / "SKILL.md").is_file()
    assert not (home / ".codex").exists() and not (home / ".cursor").exists()
    hooks = json.loads((home / ".kimi-code" / "hooks.json").read_text())
    cmd = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert shlex.split(cmd) == ["sh", str(home / ".config" / "bertie" / "hook.sh"), "--agent", "kimi"]


def test_an_agent_that_is_not_in_the_home_folder_is_not_set_up_by_default(home, tmp_path, fake_kit):
    r = installed_with_fakes(home, tmp_path, fake_kit)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "start Claude Code, and say:" in r.stdout and "(fake)" not in r.stdout
    assert not (home / ".agents").exists()


def test_agent_all_sets_up_all_five(home, tmp_path, fake_kit):
    r = installed_with_fakes(home, tmp_path, fake_kit, "--agent", "all")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "start Claude Code, Antigravity, Cursor, Codex or Kimi Code, and say:" in r.stdout
    for folder in (".gemini", ".cursor", ".codex", ".kimi-code"):
        assert (home / folder / "bertie.note").is_file(), folder
    manifest = json.loads((home / ".config" / "bertie" / "install-manifest.json").read_text())
    assert sorted(manifest["agents"]) == ["antigravity", "codex", "cursor", "kimi"]
    assert manifest["shared_skill"]["users"] == ["cursor", "codex", "kimi"]


def test_an_explicit_list_overrides_what_is_found(home, tmp_path, fake_kit):
    (home / ".kimi-code").mkdir()
    r = installed_with_fakes(home, tmp_path, fake_kit, "--agent", "claude,codex")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (home / ".codex" / "bertie.note").is_file() and not (home / ".kimi-code" / "bertie.note").exists()


def test_without_claude_in_the_list_claude_code_is_left_alone(home, tmp_path, fake_kit):
    skill = home / ".claude" / "skills" / "bertie" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: another-tool\n---\n")                  # would stop a Claude Code install
    r = installed_with_fakes(home, tmp_path, fake_kit, "--agent", "codex")
    assert r.returncode == 0, r.stdout + r.stderr
    assert skill.read_text() == "---\nname: another-tool\n---\n" and not (home / ".claude" / "settings.json").exists()
    assert "start Codex, and say:" in r.stdout and "installed the skill" not in r.stdout
    manifest = json.loads((home / ".config" / "bertie" / "install-manifest.json").read_text())
    assert "settings" not in manifest and "commands" in manifest
    assert (home / ".local" / "bin" / "bertie-mail").is_file()


def test_claude_in_the_list_still_refuses_someone_elses_skill(home, tmp_path, fake_kit):
    skill = home / ".claude" / "skills" / "bertie" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: another-tool\n---\n")
    for extra in ([], ["--agent", "claude,codex"], ["--agent", "all"]):
        r = installed_with_fakes(home, tmp_path, fake_kit, *extra)
        assert r.returncode == 1 and "is not this tool's skill" in r.stdout, extra
        assert not (home / ".config").exists()


def test_codex_sends_reaches_the_codex_adapter(home, tmp_path, fake_kit):
    h2 = tmp_path / "home2"
    h2.mkdir()
    assert installed_with_fakes(home, tmp_path, fake_kit, "--agent", "codex").returncode == 0
    assert installed_with_fakes(h2, tmp_path, fake_kit, "--agent", "codex", "--codex-sends", "allow").returncode == 0
    read = lambda h: json.loads((h / ".config" / "bertie" / "install-manifest.json").read_text())["agents"]["codex"]["sends"]
    assert read(home) == "prompt" and read(h2) == "allow"
    h3 = tmp_path / "home3"
    h3.mkdir()
    assert installed_with_fakes(h3, tmp_path, fake_kit, "--agent", "codex", "--codex-sends=prompt").returncode == 0
    assert read(h3) == "prompt"


@pytest.mark.parametrize("extra", [["--agent", "claude,codex"], ["--agent=codex"], ["--agent", "all"]])
def test_an_adapter_missing_from_the_kit_stops_the_install_before_anything_is_touched(home, tmp_path, fake_kit, extra):
    (fake_kit / "tools" / "agents" / "codex.py").unlink()
    r = installed_with_fakes(home, tmp_path, fake_kit, *extra)
    assert r.returncode == 1 and "The codex adapter is missing from this copy" in r.stdout
    assert nothing_touched(home)


def test_a_file_that_is_not_ours_stops_the_install_with_a_clear_message(home, tmp_path, fake_kit):
    note = home / ".codex" / "bertie.note"
    note.parent.mkdir()
    note.write_text("# not ours\n")
    r = installed_with_fakes(home, tmp_path, fake_kit, "--agent", "claude,codex")
    assert r.returncode == 1 and "already exists and was not made by this installer" in r.stderr
    assert note.read_text() == "# not ours\n"


def test_a_rerun_with_other_agents_changes_nothing(home, tmp_path, fake_kit):
    assert installed_with_fakes(home, tmp_path, fake_kit, "--agent", "all").returncode == 0
    before = tree(home)
    assert installed_with_fakes(home, tmp_path, fake_kit, "--agent", "all").returncode == 0
    assert tree(home) == before


def test_uninstall_after_a_multi_agent_install_leaves_the_home_as_it_was(home, tmp_path, fake_kit, keychain,
                                                                        monkeypatch, dialog, fakes):
    import auth
    import common
    (home / ".zshrc").write_text("alias ll='ls -l'\n")
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mine.json").write_text('{"theirs": true}\n')
    before, before_dirs = tree(home), dirs(home)
    assert installed_with_fakes(home, tmp_path, fake_kit, "--agent", "all").returncode == 0
    assert tree(home) != before
    monkeypatch.setattr(common, "HOME", home / ".config" / "bertie")
    monkeypatch.setattr(auth, "_post", lambda url, data: (200, "{}"))
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps({"refresh_token": "R", "scopes": []}))
    google_cli.main(["uninstall", "--yes"])
    assert tree(home) == before and without_claude_leftovers(dirs(home)) == before_dirs
    asked = dialog.shown[-1]["message"]
    assert "what the installer set up for Antigravity, Cursor, Codex, Kimi Code" in asked


def test_the_uninstall_window_says_nothing_new_for_a_claude_only_install(home, tmp_path, keychain, monkeypatch,
                                                                         dialog):
    import auth
    import common
    assert run_install(home, tmp_path, *ARGS).returncode == 0
    monkeypatch.setattr(common, "HOME", home / ".config" / "bertie")
    monkeypatch.setattr(auth, "_post", lambda url, data: (200, "{}"))
    google_cli.main(["uninstall", "--yes"])
    assert "installer set up for" not in dialog.shown[-1]["message"]


# ================================================================ the code itself

@pytest.mark.parametrize("path", sorted((TOOLS / "agents").glob("*.py")), ids=lambda p: p.name)
def test_agent_code_parses_as_python_3_9_and_uses_only_the_standard_library(path):
    tree_ = ast.parse(path.read_text(), feature_version=(3, 9))
    stdlib = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else None
    ours = {"agents", "kit_setup", "registry", "hook"}
    for node in ast.walk(tree_):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else [])
        for n in names:
            top = n.split(".")[0]
            if stdlib is not None:
                assert top in stdlib or top in ours, (path.name, n)
            else:
                assert top not in {"google", "googleapiclient", "keyring", "pytest", "requests"}, (path.name, n)
