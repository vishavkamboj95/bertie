"""Kimi Code's adapter (tools/agents/kimi.py): the shared skill, the marked block in
~/.kimi-code/config.toml, the safety hook for a block-only agent, and rules_cover.

HOME is a temporary folder everywhere; nothing here reads or writes the real ~/.kimi-code.
Python 3.11 and later can read TOML (tomllib), and then the file is parsed with it. Earlier
Pythons check the same structure with a small reader of TOML basic strings and `[[tables]]`
written here, which does not use the code under test (and is checked against tomllib where
there is one)."""
import concurrent.futures
import copy
import io
import json
import os
import random
import re
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
from agents import kimi
from conftest import ACCOUNT, KIT, TOOLS
from test_agents_core import (ATTACKS, backups, ctx, dirs, lonely, run_script, run_sh, tree,  # noqa: F401
                              without_claude_leftovers)
from test_hook import FREE, GATED
from test_installer import env_for, run_install

try:
    import tomllib
except ImportError:                                  # Python before 3.11: the text is checked instead
    tomllib = None

needs_tomllib = pytest.mark.skipif(tomllib is None, reason="no tomllib before Python 3.11")

# written as numbers so no odd character sits in this file
E_ACUTE, SNOWMAN, BOM, LS, PS, NEL, EM_DASH = (chr(c) for c in (0xE9, 0x2603, 0xFEFF, 0x2028, 0x2029, 0x85, 0x2014))
BS = chr(92)

BEGIN_LINE = "# >>> bertie: added by the bertie installer; `bertie uninstall` removes it >>>"
END_LINE = "# <<< bertie: add your own settings above this block <<<"
OLD_END_LINE = "# <<< bertie <<<"          # what an early build wrote (round 2 moved the block and changed this line)
CONFIG = ".kimi-code/config.toml"
SKILL = ".agents/skills/bertie/SKILL.md"

OWN = ('[permission]\nmode = "ask"\n\n'
       '[[permission.rules]]\ndecision = "deny"\npattern = "Bash(rm -rf*)"\n\n'
       '[[hooks]]\nevent = "PreToolUse"\nmatcher = "^Bash$"\ncommand = "echo mine"\ntimeout = 5\n')

ORIGINALS = {
    "plain": '[ui]\ntheme = "dark"\n',
    "no newline at the end": '[ui]\ntheme = "dark"',
    "empty": "",
    "only a comment": "# nothing here yet\n",
    "blank lines at the end": '[ui]\ntheme = "dark"\n\n\n',
    "crlf": '[ui]\r\ntheme = "dark"\r\n',
    "crlf, no newline at the end": '[ui]\r\ntheme = "dark"',
    "non-ascii": '[ui]\ntitle = "caf' + E_ACUTE + ' ' + SNOWMAN + '"\n',
    "their own hooks and rules": OWN,
    "their own hooks and rules, no newline at the end": OWN.rstrip("\n"),
    "no table": 'theme = "dark"\n',
    "no table, no newline at the end": 'theme = "dark"',
    "no table, crlf, no newline at the end": 'theme = "dark"\r\nmodel = "k"',
    "comment lines stuck to the first table": '# my settings\nname = "x"\n\n# the ui\n# (shown at the top)\n[ui]\ntheme = "dark"\n',
    "brackets and hashes in values before the first table": (
        'colors = [\n  ["a", "b"],\n  ["c"],\n]\n'
        'text = """\n[not a table]\n# not a comment\n"""\n'
        "lit = \'\'\'\n[nor this]\n\'\'\'\n"
        'one = "[x] # y"\n\n[ui]\ntheme = "dark"\n'),
    "a table straight away": '[ui]\ntheme = "dark"\n',
    "array of tables first": '[[models]]\nname = "m"\n',
}

# Round 2: the block goes just before the first table, and at the end only when there is none. Where it
# belongs in each sample above, written out by hand: the text it goes in front of (None = the end).
BLOCK_BEFORE = {
    "plain": "[ui]", "no newline at the end": "[ui]", "blank lines at the end": "[ui]", "crlf": "[ui]",
    "crlf, no newline at the end": "[ui]", "non-ascii": "[ui]", "their own hooks and rules": "[permission]",
    "their own hooks and rules, no newline at the end": "[permission]",
    "empty": None, "only a comment": None, "no table": None, "no table, no newline at the end": None,
    "no table, crlf, no newline at the end": None,
    "comment lines stuck to the first table": "# the ui\n# (shown at the top)\n[ui]",
    "brackets and hashes in values before the first table": "[ui]",
    "a table straight away": "[ui]", "array of tables first": "[[models]]",
}
assert set(BLOCK_BEFORE) == set(ORIGINALS)


# ---------------------------------------------------------------- helpers

def config(home):
    return home / CONFIG


def put_config(home, text, mode=None):
    p = config(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    if mode is not None:
        p.chmod(mode)
    return p


def raw(home):
    return config(home).read_bytes().decode("utf-8")


def expected_lead(original):
    """The line break the installer adds so the file ends with one before a block that goes at the
    end (a file with no table). A block that goes in front of a table needs none."""
    if not original or original.endswith("\n"):
        return ""
    return "\r\n" if "\r\n" in original else "\n"


def expected_place(name):
    """(what comes before the block, what comes after it, the line break added) for the sample `name`."""
    original = ORIGINALS[name]
    marker = BLOCK_BEFORE[name]
    if marker is None:
        lead = expected_lead(original)
        return original + lead, "", lead
    at = original.index(marker)
    return original[:at], original[at:], ""


_UNESCAPE = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def unquote(literal):
    """Read a TOML basic string, strictly, without the code under test."""
    assert len(literal) >= 2 and literal[0] == '"' and literal[-1] == '"', literal
    body, out, i = literal[1:-1], [], 0
    while i < len(body):
        c = body[i]
        assert c != '"' and ord(c) >= 0x20 and ord(c) != 0x7F, ("a raw character in a basic string", literal)
        if c != "\\":
            out.append(c)
            i += 1
        elif body[i + 1] == "u":
            digits = body[i + 2:i + 6]
            assert re.fullmatch(r"[0-9A-Fa-f]{4}", digits), literal
            out.append(chr(int(digits, 16)))
            i += 6
        else:
            out.append(_UNESCAPE[body[i + 1]])
            i += 2
    return "".join(out)


def split_block(text):
    """(what is before our block, the block, what is after it); the block runs from its begin line
    to just after the line break that ends its end line."""
    assert text.count(BEGIN_LINE) == 1 and text.count(END_LINE) == 1, text
    i = text.index(BEGIN_LINE)
    j = text.index(END_LINE) + len(END_LINE)
    if text.startswith("\n", j):
        j += 1
    elif text.startswith("\r\n", j):
        j += 2
    return text[:i], text[i:j], text[j:]


def read_block(block):
    """The tables of a block as {"permission": {"rules": [...]}, "hooks": [...]}."""
    data, cur = {}, None
    for line in block.replace("\r\n", "\n").split("\n"):
        if not line or line.startswith("#"):
            continue
        if line.startswith("[["):
            assert line.endswith("]]"), line
            path = line[2:-2].split(".")
            node = data
            for k in path[:-1]:
                node = node.setdefault(k, {})
            cur = {}
            node.setdefault(path[-1], []).append(cur)
        else:
            key, eq, value = line.partition(" = ")
            assert eq and cur is not None, line
            cur[key] = unquote(value) if value.startswith('"') else int(value)
    return data


def expected_rules():
    out = []
    for tool, acts in registry.gated_actions().items():
        for act, what in acts.items():
            for pattern in (f"Bash({tool} {act}*)", f"Bash({tool} {act}*/**)"):
                out.append({"decision": "ask", "pattern": pattern, "reason": "Bertie: " + what})
    return out


def expected_hook(c):
    return {"event": "PreToolUse", "matcher": "^(Bash|Write|Edit)$",
            "command": "sh " + shlex.quote(str(c.install_dir / "hook.sh")) + " --agent kimi", "timeout": 30}


def check_block(text, c):
    """Our block is in the text exactly once and says what it should. Returns (before, after)."""
    before, block, after = split_block(text)
    data = read_block(block)
    assert set(data) == {"permission", "hooks"} and set(data["permission"]) == {"rules"}
    assert data["permission"]["rules"] == expected_rules()
    assert data["hooks"] == [expected_hook(c)]
    if tomllib:
        assert tomllib.loads(block) == data                       # the small reader here agrees with a real one
        full = tomllib.loads(text)
        for rule in expected_rules():
            assert full["permission"]["rules"].count(rule) == 1
        assert full["hooks"].count(expected_hook(c)) == 1
    return before, after


def without_ours(parsed, c):
    """A parsed config with our rules and our hook taken out."""
    d = copy.deepcopy(parsed)
    perm = d.get("permission")
    if isinstance(perm, dict) and "rules" in perm:
        perm["rules"] = [r for r in perm["rules"] if r not in expected_rules()]
        if not perm["rules"]:
            del perm["rules"]
        if not perm:
            del d["permission"]
    if "hooks" in d:
        d["hooks"] = [h for h in d["hooks"] if h != expected_hook(c)]
        if not d["hooks"]:
            del d["hooks"]
    return d


def manifest(c):
    return kit_setup.load_manifest(c)


def answer(payload=None, raw_text=None):
    return hook.agent_answer("kimi", io.StringIO(raw_text if raw_text is not None else json.dumps(payload)))


def bash(cmd):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}}


def file_tool(tool, **tool_input):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}


# a block with no send among its reasons ends with this instead of the send advice (round 5, H2)
NEUTRAL = "This touches Bertie's safety settings; ask the user to do it by hand if it is needed."


def denied(out):
    h = json.loads(out)["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse" and h["permissionDecision"] == "deny"
    return h["permissionDecisionReason"]


def kimi_hook(payload=None, raw_text=None):
    return run_script(["--agent", "kimi"], raw_text if raw_text is not None else json.dumps(payload))


# ================================================================ the adapter and its detection

def test_the_adapter_has_the_whole_interface():
    assert agents.get("kimi") is kimi
    assert (kimi.KEY, kimi.LABEL, kimi.HOOK_STYLE) == ("kimi", "Kimi Code", "block")
    for name in ("detect", "install", "uninstall", "hook_input", "hook_answer", "hook_failure", "rules_cover"):
        assert callable(getattr(kimi, name)), name


def test_kimi_is_detected_by_its_config_folder_only(home, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kimi").write_text("#!/bin/sh\n")
    (bin_dir / "kimi").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    assert kimi.detect(home) is False                       # a kimi program on PATH does not count
    (home / ".kimi-code").write_text("a file, not a folder")
    assert kimi.detect(home) is False
    (home / ".kimi-code").unlink()
    (home / ".kimi-code").mkdir()
    assert kimi.detect(home) is True                        # the folder alone is enough, with no config file in it
    assert "kimi" in agents.detect_present(home)
    assert agents.default_selection(home)[0] == "claude" and "kimi" in agents.default_selection(home)


# ================================================================ install: what lands where

def test_the_files_land_where_kimi_reads_them(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    assert sorted(tree(home)) == [SKILL, CONFIG]
    assert (home / SKILL).read_text() == agents.neutral_skill_text((KIT / "skill" / "bertie" / "SKILL.md").read_text())
    assert "claude" not in (home / SKILL).read_text().lower()
    assert stat.S_IMODE((home / SKILL).stat().st_mode) == 0o644
    assert not (home / ".kimi-code" / "skills").exists()   # one copy only: Kimi reads ~/.agents/skills too
    check_block(raw(home), ctx)


def test_the_hook_command_points_at_the_install_folder_with_the_agent_flag(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    _, block, _ = split_block(raw(home))
    command = read_block(block)["hooks"][0]["command"]
    assert shlex.split(command) == ["sh", str(ctx.install_dir / "hook.sh"), "--agent", "kimi"]
    assert "$HOME" not in command and "~" not in command


def test_the_block_for_one_action_is_exactly_this(home, ctx):
    put_config(home, "")
    kimi.install(ctx, KIT)
    text = raw(home)
    assert text.startswith(BEGIN_LINE + "\n[[permission.rules]]\n")
    assert ('[[permission.rules]]\n'
            'decision = "ask"\n'
            'pattern = "Bash(bertie-mail send*)"\n'
            'reason = "Bertie: SENDS an email from the signed-in account"\n'
            '\n'
            '[[permission.rules]]\n'
            'decision = "ask"\n'
            'pattern = "Bash(bertie-mail send*/**)"\n'
            'reason = "Bertie: SENDS an email from the signed-in account"\n'
            '\n') in text
    assert text.endswith('[[hooks]]\n'
                         'event = "PreToolUse"\n'
                         'matcher = "^(Bash|Write|Edit)$"\n'
                         f'command = "sh {ctx.install_dir}/hook.sh --agent kimi"\n'
                         'timeout = 30\n'
                         + END_LINE + "\n")


def test_every_gated_action_has_its_two_rules_and_nothing_else_is_gated(home, ctx):
    put_config(home, "")
    kimi.install(ctx, KIT)
    rules = read_block(split_block(raw(home))[1])["permission"]["rules"]
    pairs = [(t, a) for t, acts in registry.gated_actions().items() for a in acts]
    assert len(rules) == 2 * len(pairs) == 26
    for tool, act in pairs:
        assert [r["pattern"] for r in rules if r["pattern"].startswith(f"Bash({tool} {act}*")] == [
            f"Bash({tool} {act}*)", f"Bash({tool} {act}*/**)"]
    assert {r["decision"] for r in rules} == {"ask"} and all(r["reason"].startswith("Bertie: ") for r in rules)
    assert not [r for r in rules if "whoami" in r["pattern"] or "search" in r["pattern"]]


def test_every_command_rules_cover_accepts_has_both_rules_in_the_block(home, ctx):
    put_config(home, "")
    kimi.install(ctx, KIT)
    patterns = {r["pattern"] for r in read_block(split_block(raw(home))[1])["permission"]["rules"]}
    for cmd in GATED + ["bertie-mail send", "bertie logout", "bertie-chat dm"]:
        assert kimi.rules_cover(cmd), cmd
        tool, act = cmd.split()[:2]
        assert {f"Bash({tool} {act}*)", f"Bash({tool} {act}*/**)"} <= patterns, cmd


def test_the_report_says_what_was_done_and_what_protects_what(home, ctx):
    put_config(home, ORIGINALS["plain"])
    msgs = kimi.install(ctx, KIT)
    assert all(m.startswith("Kimi Code: ") for m in msgs)
    text = "\n".join(msgs)
    assert "installed the skill in ~/.agents/skills/bertie" in text
    assert "added to ~/.kimi-code/config.toml" in text
    # K1: Kimi 0.8.2 never applies config ask rules, so the report must not say Kimi asks because of them
    assert "does not yet apply ask rules from its settings" in text
    assert "Kimi itself does not ask before a send" in text and "in its default mode it asks before every command" in text
    assert "Mac confirmation window and Touch ID guard every send" in text
    assert "can only block, never ask" in text
    assert "--auto" in text and "approve for this session" in text and "asks nothing" in text
    assert "ask rules do the asking" not in text and "asks you first" not in text
    assert "Start a new Kimi Code session" in msgs[-1]
    assert EM_DASH not in text


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o640])
def test_the_files_mode_is_kept(home, ctx, mode):
    p = put_config(home, ORIGINALS["plain"], mode)
    kimi.install(ctx, KIT)
    assert stat.S_IMODE(p.stat().st_mode) == mode
    kimi.uninstall(ctx)
    assert stat.S_IMODE(p.stat().st_mode) == mode


def test_a_backup_of_the_file_is_made_first_and_recorded(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    (b,) = backups(home / ".kimi-code")
    assert b.startswith("config.toml.bertie-backup-")
    assert (home / ".kimi-code" / b).read_bytes() == ORIGINALS["plain"].encode()
    assert manifest(ctx)["agents"]["kimi"]["backups"] == [b]


def test_the_manifest_records_the_block_and_the_shared_skill(home, ctx):
    put_config(home, ORIGINALS["no table, no newline at the end"])      # a block at the end of the file needs the line break
    kimi.install(ctx, KIT)
    m = manifest(ctx)
    assert m["agents"]["kimi"]["toml"] == {CONFIG: {"lead": "\n"}}
    assert m["shared_skill"]["users"] == ["kimi"]
    assert "skipped" not in m["agents"]["kimi"]


# ================================================================ install and uninstall give the file back

@pytest.mark.parametrize("name", sorted(ORIGINALS))
def test_install_then_uninstall_gives_back_exactly_the_bytes_and_the_home(home, ctx, name):
    original = ORIGINALS[name]
    put_config(home, original)
    before_tree, before_dirs = tree(home), dirs(home)
    kimi.install(ctx, KIT)
    before, after = check_block(raw(home), ctx)
    want_before, want_after, want_lead = expected_place(name)
    assert (before, after) == (want_before, want_after)
    assert manifest(ctx)["agents"]["kimi"]["toml"][CONFIG]["lead"] == want_lead
    if tomllib:
        assert without_ours(tomllib.loads(raw(home)), ctx) == tomllib.loads(original)
    msgs = kimi.uninstall(ctx)
    assert raw(home) == original
    assert tree(home) == before_tree and dirs(home) == before_dirs
    assert any("removed the ask rules and safety hook from ~/.kimi-code/config.toml" in m for m in msgs)
    assert "agents" not in manifest(ctx) and "shared_skill" not in manifest(ctx)


def test_the_users_own_tables_survive_next_to_ours(home, ctx):
    original = ('[ui]\ntheme = "dark"\n\n' + OWN + '\n[model]\nname = "k"\n')
    put_config(home, original)
    kimi.install(ctx, KIT)
    text = raw(home)
    before, after = check_block(text, ctx)
    assert (before, after) == ("", original)                    # round 2: the block is in front of their first table
    if tomllib:
        full, mine = tomllib.loads(text), tomllib.loads(original)
        assert without_ours(full, ctx) == mine
        assert full["ui"] == {"theme": "dark"} and full["model"] == {"name": "k"} and full["permission"]["mode"] == "ask"
        assert full["permission"]["rules"][-1] == {"decision": "deny", "pattern": "Bash(rm -rf*)"}    # theirs now follow ours
        assert full["hooks"][-1]["command"] == "echo mine" and len(full["hooks"]) == 2
    kimi.uninstall(ctx)
    assert raw(home) == original


def test_a_block_with_text_after_it_does_not_glue_lines_together_on_uninstall(home, ctx):
    original = ORIGINALS["no table, no newline at the end"]      # a block at the end of the file
    put_config(home, original)
    kimi.install(ctx, KIT)
    config(home).write_bytes((raw(home) + 'b = 2\n').encode())      # the user wrote below our block
    kimi.uninstall(ctx)
    assert raw(home) == original + "\n" + "b = 2\n"


def test_text_before_and_after_the_block_is_kept_when_only_the_block_goes(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    before, block, after = split_block(raw(home))
    config(home).write_bytes(("[first]\na = 1\n" + before + block + after + "[last]\nz = 2\n").encode())
    kimi.uninstall(ctx)
    assert raw(home) == "[first]\na = 1\n" + ORIGINALS["plain"] + "[last]\nz = 2\n"


def test_uninstall_leaves_a_backup_beside_the_file_and_says_so(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    installed = raw(home)
    msgs = kimi.uninstall(ctx)
    names = backups(home / ".kimi-code")
    assert len(names) == 2
    assert any((home / ".kimi-code" / n).read_text() == installed for n in names)
    assert any(n in "\n".join(msgs) for n in names)


def test_a_symlinked_config_stays_a_link_and_the_target_is_edited(home, ctx, tmp_path):
    target = tmp_path / "dotfiles" / "kimi.toml"
    target.parent.mkdir()
    target.write_text(ORIGINALS["plain"])
    target.chmod(0o600)
    (home / ".kimi-code").mkdir()
    config(home).symlink_to(target)
    kimi.install(ctx, KIT)
    assert config(home).is_symlink() and os.readlink(str(config(home))) == str(target)
    check_block(target.read_text(), ctx)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    kimi.uninstall(ctx)
    assert config(home).is_symlink() and target.read_text() == ORIGINALS["plain"]


# ================================================================ install again

def test_installing_twice_changes_nothing_the_second_time(home, ctx):
    put_config(home, ORIGINALS["no newline at the end"])
    kimi.install(ctx, KIT)
    snapshot, manifest_before = tree(home), manifest(ctx)
    names = backups(home / ".kimi-code")
    msgs = kimi.install(ctx, KIT)
    assert tree(home) == snapshot and manifest(ctx) == manifest_before
    assert backups(home / ".kimi-code") == names and len(names) == 1       # no second backup
    assert "already in ~/.kimi-code/config.toml" in "\n".join(msgs) and "the skill is already in" in msgs[0]
    assert raw(home).count(BEGIN_LINE) == 1


def test_a_new_install_replaces_the_block_where_it_is_and_keeps_what_the_user_wrote_around_it(home, ctx, monkeypatch):
    original = ORIGINALS["no table, no newline at the end"]
    put_config(home, original)
    kimi.install(ctx, KIT)
    before, block, after = split_block(raw(home))
    config(home).write_bytes((before + block + "[last]\nz = 2\n").encode())
    monkeypatch.setattr(agents, "hook_command", lambda c, key: "sh /elsewhere/hook.sh --agent kimi")
    msgs = kimi.install(ctx, KIT)
    b2, block2, a2 = split_block(raw(home))
    assert (b2, a2) == (before, "[last]\nz = 2\n")                          # same place, same surroundings
    assert read_block(block2)["hooks"][0]["command"] == "sh /elsewhere/hook.sh --agent kimi"
    assert "updated in ~/.kimi-code/config.toml" in "\n".join(msgs)
    assert len(backups(home / ".kimi-code")) == 2
    # round 2: the user's table now follows the block, so the line break before the block is part of their file
    # and nothing more is recorded; the uninstall below still gives back the same bytes
    assert manifest(ctx)["agents"]["kimi"]["toml"][CONFIG]["lead"] == ""
    kimi.uninstall(ctx)
    assert raw(home) == original + "\n" + "[last]\nz = 2\n"


def test_a_block_found_after_the_manifest_was_lost_is_replaced_not_doubled_and_recorded_again(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    os.remove(str(ctx.manifest_path))                                       # the manifest was lost
    kimi.install(ctx, KIT)
    assert raw(home).count(BEGIN_LINE) == 1
    check_block(raw(home), ctx)
    assert manifest(ctx)["agents"]["kimi"]["toml"] == {CONFIG: {"lead": ""}}
    kimi.uninstall(ctx)
    assert raw(home) == ORIGINALS["plain"]


# ================================================================ no config file

@pytest.mark.parametrize("how", ["no folder", "folder without a config", "a folder where the config should be"])
def test_without_a_config_file_only_the_skill_is_installed_and_the_report_says_so(home, ctx, how):
    if how != "no folder":
        (home / ".kimi-code").mkdir()
    if how == "a folder where the config should be":
        config(home).mkdir()
    before_dirs = dirs(home)
    msgs = kimi.install(ctx, KIT)
    text = "\n".join(msgs)
    assert (home / SKILL).is_file()
    assert "~/.kimi-code/config.toml was not found" in text and "NOT added" in text
    assert "Until then only the Mac confirmation window and Touch ID guard each send" in text and "run the installer again" in text
    assert "will not ask" not in text
    assert "Mac confirmation window and Touch ID" in text
    assert all(m.startswith("Kimi Code: ") for m in msgs)
    assert (home / ".kimi-code").exists() == (how != "no folder")
    assert sorted(tree(home)) == [SKILL]
    assert manifest(ctx)["agents"]["kimi"]["skipped"]
    kimi.uninstall(ctx)
    assert tree(home) == {} and dirs(home) == before_dirs


def test_the_block_is_added_once_the_config_file_exists(home, ctx):
    kimi.install(ctx, KIT)                                                   # nothing to edit yet
    put_config(home, ORIGINALS["plain"])
    msgs = kimi.install(ctx, KIT)
    assert "added to ~/.kimi-code/config.toml" in "\n".join(msgs)
    check_block(raw(home), ctx)
    assert "skipped" not in manifest(ctx)["agents"]["kimi"]


# ================================================================ refusals: the file is left alone

CLASHES = [
    "hooks = []\n",
    '  hooks = [{ event = "PreToolUse" }]\n',
    '"hooks" = []\n',
    "'hooks' = []\n",
    "hooks.PreToolUse = []\n",
    "[hooks]\nPreToolUse = []\n",
    "[ hooks ]\n",
    "[hooks.PreToolUse]\n",
    "[[hooks.PreToolUse]]\n",
    '[permission]\nrules = [{ decision = "deny", pattern = "Bash(rm*)" }]\n',
    '[permission]\n  rules=[]\n',
    'permission = { mode = "ask" }\n',
    "permission.rules = []\n",
    "[permission.rules]\n",
    "[permission.rules.extra]\n",
    "[ permission . rules ]\n",
    '[ "permission" . "rules" ]\n',
    "[[permission.rules.extra]]\n",
    BOM + "hooks = []\n",
    '[ui]\ntheme = "dark"\n\nhooks = []\n',
]


@pytest.mark.parametrize("text", CLASHES)
def test_a_file_that_already_defines_hooks_or_permission_rules_the_wrong_way_is_refused(home, ctx, text):
    put_config(home, text)
    with pytest.raises(kit_setup.SetupError, match="already defines the hooks or the permission rules"):
        kimi.install(ctx, KIT)
    assert raw(home) == text
    assert backups(home / ".kimi-code") == []
    assert not (home / ".agents").exists() and manifest(ctx) == {}          # nothing else was done either


def test_the_refusal_names_the_line(home, ctx):
    put_config(home, '[ui]\ntheme = "dark"\n\nhooks = []\n')
    with pytest.raises(kit_setup.SetupError, match=r"line 4 \(hooks = \[\]\)"):
        kimi.install(ctx, KIT)


@pytest.mark.parametrize("text", [
    OWN,
    '# hooks = old\n[ui]\ntheme = "dark"\n',
    '[ui]\nhooks_dir = "x"\nrules_file = "y"\npermission_mode = "z"\n',
    "[hooks_extra]\nx = 1\n",
    '[permission]\nmode = "ask"\n',
    'permission.mode = "ask"\n',
    '[[hooks]]\nevent = "PreToolUse"\nmatcher = "^Bash$"\ncommand = "echo mine"\n',
    '[[permission.rules]]\ndecision = "allow"\npattern = "Bash(ls*)"\n',
])
def test_their_own_hook_and_rule_tables_are_not_a_clash(home, ctx, text):
    put_config(home, text)
    kimi.install(ctx, KIT)
    check_block(raw(home), ctx)
    if tomllib:
        assert without_ours(tomllib.loads(raw(home)), ctx) == tomllib.loads(text)


@needs_tomllib
@pytest.mark.parametrize("text", ['[ "per\\u006dission" . rules ]\n', '[ "hoo\\u006bs" ]\nx = 1\n'])
def test_a_clash_the_line_checks_cannot_see_is_caught_by_the_toml_reader(home, ctx, text):
    put_config(home, text)
    with pytest.raises(kit_setup.SetupError, match="could not be read as TOML"):
        kimi.install(ctx, KIT)
    assert raw(home) == text and backups(home / ".kimi-code") == []


def test_a_file_that_is_not_utf8_is_refused_untouched(home, ctx):
    p = config(home)
    p.parent.mkdir()
    p.write_bytes(b"\xff\xfe[ui]\n")
    with pytest.raises(kit_setup.SetupError, match="not UTF-8"):
        kimi.install(ctx, KIT)
    assert p.read_bytes() == b"\xff\xfe[ui]\n" and backups(home / ".kimi-code") == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_folder_that_cannot_be_written_is_a_clear_refusal_and_leaves_the_file_alone(home, ctx):
    put_config(home, ORIGINALS["plain"])
    folder = home / ".kimi-code"
    folder.chmod(0o500)
    try:
        with pytest.raises(kit_setup.SetupError, match="could not save a copy"):
            kimi.install(ctx, KIT)
    finally:
        folder.chmod(0o700)
    assert raw(home) == ORIGINALS["plain"] and [p.name for p in folder.iterdir()] == ["config.toml"]


def test_someone_elses_shared_skill_stops_the_install_before_the_config_is_touched(home, ctx):
    put_config(home, ORIGINALS["plain"])
    skill = home / SKILL
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: another-tool\n---\n")
    with pytest.raises(kit_setup.SetupError, match="already exists and was not made by this installer"):
        kimi.install(ctx, KIT)
    assert raw(home) == ORIGINALS["plain"] and backups(home / ".kimi-code") == []
    assert skill.read_text() == "---\nname: another-tool\n---\n"


def test_markers_gone_but_the_hook_still_there_is_refused_with_the_lines_to_remove(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    text = raw(home).replace(BEGIN_LINE + "\n", "").replace(END_LINE + "\n", "")
    config(home).write_bytes(text.encode())
    names = backups(home / ".kimi-code")
    with pytest.raises(kit_setup.SetupError) as e:
        kimi.install(ctx, KIT)
    lines = text.split("\n")
    hook_line = lines.index(next(l for l in lines if l.startswith("command = "))) + 1
    rule_lines = [i for i, l in enumerate(lines, 1) if l.startswith("pattern = ")]
    assert "has Bertie's safety hook but not the start and end lines" in str(e.value)
    assert f"the hook command at line {hook_line} (command = " in str(e.value)
    assert f"26 rule pattern lines at lines {rule_lines[0]} to {rule_lines[-1]}" in str(e.value)
    assert raw(home) == text and backups(home / ".kimi-code") == names


@pytest.mark.parametrize("damage", ["no end line", "no begin line", "two blocks", "end before begin"])
def test_markers_that_do_not_pair_up_are_refused(home, ctx, damage):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    _, block, _ = split_block(raw(home))
    text = {"no end line": raw(home).replace(END_LINE + "\n", ""),
            "no begin line": raw(home).replace(BEGIN_LINE + "\n", ""),
            "two blocks": raw(home) + block,
            "end before begin": END_LINE + "\n" + raw(home).replace(END_LINE + "\n", "")}[damage]
    config(home).write_bytes(text.encode())
    with pytest.raises(kit_setup.SetupError, match="only part of the start and end lines|but not the start and end"):
        kimi.install(ctx, KIT)
    assert raw(home) == text


def test_a_user_who_removed_everything_by_hand_gets_a_fresh_block(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    config(home).write_bytes(ORIGINALS["plain"].encode())
    kimi.install(ctx, KIT)
    check_block(raw(home), ctx)


def test_a_gated_name_with_pattern_characters_is_refused_not_written(home, ctx, monkeypatch):
    real = registry.gated_actions

    def odd():
        out = real()
        out["bertie-mail"] = dict(out["bertie-mail"], **{"se*d": "x"})
        return out

    monkeypatch.setattr(registry, "gated_actions", odd)
    put_config(home, ORIGINALS["plain"])
    with pytest.raises(kit_setup.SetupError, match="characters that mean something in Kimi's rule patterns"):
        kimi.install(ctx, KIT)
    assert raw(home) == ORIGINALS["plain"]
    assert kimi.rules_cover("bertie-mail se*d") is False                    # and no rule would cover it either


# ================================================================ uninstall: only what is recorded

def test_markers_gone_means_the_file_is_left_alone_and_the_lines_are_listed(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    text = raw(home).replace(BEGIN_LINE + "\n", "").replace(END_LINE + "\n", "")
    config(home).write_bytes(text.encode())
    msgs = kimi.uninstall(ctx)
    assert raw(home) == text
    joined = "\n".join(msgs)
    assert "no longer has the start and end lines Bertie put round its block" in joined
    assert "left exactly as it is" in joined and "delete the [[hooks]] table" in joined
    lines = text.split("\n")
    hook_line = lines.index(next(l for l in lines if l.startswith("command = "))) + 1
    rule_lines = [i for i, l in enumerate(lines, 1) if l.startswith("pattern = ")]
    assert f"the hook command at line {hook_line} (command = " in joined
    assert f"26 rule pattern lines at lines {rule_lines[0]} to {rule_lines[-1]}" in joined
    assert "agents" not in manifest(ctx) and not (home / SKILL).exists()   # the rest is still cleaned up


def test_a_damaged_block_is_left_alone_too(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    text = raw(home).replace(END_LINE + "\n", "")
    config(home).write_bytes(text.encode())
    msgs = kimi.uninstall(ctx)
    assert raw(home) == text and "only part of the start and end lines" in "\n".join(msgs)


def test_a_file_with_nothing_of_ours_left_is_not_touched(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    config(home).write_bytes(ORIGINALS["plain"].encode())
    names = backups(home / ".kimi-code")
    msgs = kimi.uninstall(ctx)
    assert raw(home) == ORIGINALS["plain"] and backups(home / ".kimi-code") == names
    assert not any("config.toml" in m for m in msgs)


def test_nothing_recorded_means_nothing_removed(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    installed = raw(home)
    os.remove(str(ctx.manifest_path))
    msgs = kimi.uninstall(ctx)
    assert raw(home) == installed and msgs == ["Kimi Code: nothing to remove"]


def test_a_config_that_is_gone_is_not_an_error(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    config(home).unlink()
    msgs = kimi.uninstall(ctx)
    assert not config(home).exists() and not (home / SKILL).exists()
    assert not any("config.toml" in m for m in msgs)


def test_the_config_path_comes_from_the_code_not_the_manifest(home, ctx, tmp_path):
    put_config(home, ORIGINALS["plain"])
    victim = tmp_path / "victim.toml"
    victim.write_text("keep = 1\n" + BEGIN_LINE + "\n" + END_LINE + "\n")
    kimi.install(ctx, KIT)
    m = manifest(ctx)
    m["agents"]["kimi"]["toml"] = {"../../victim.toml": {"lead": ""}}
    kit_setup.save_manifest(ctx, m)
    installed = raw(home)
    kimi.uninstall(ctx)
    assert victim.read_text() == "keep = 1\n" + BEGIN_LINE + "\n" + END_LINE + "\n" and raw(home) == installed


def test_a_tampered_line_break_in_the_manifest_cannot_cut_text_out_of_the_file(home, ctx):
    original = ORIGINALS["no table, no newline at the end"]
    put_config(home, original)
    kimi.install(ctx, KIT)
    m = manifest(ctx)
    m["agents"]["kimi"]["toml"][CONFIG]["lead"] = '"\n'
    kit_setup.save_manifest(ctx, m)
    kimi.uninstall(ctx)
    assert raw(home) == original + "\n"                                     # a line break stays; no text is lost


def test_the_shared_skill_stays_while_another_agent_uses_it(home, ctx):
    put_config(home, ORIGINALS["plain"])
    agents.acquire_shared_skill(ctx, "codex", KIT)
    kimi.install(ctx, KIT)
    assert manifest(ctx)["shared_skill"]["users"] == ["codex", "kimi"]
    kimi.uninstall(ctx)
    assert (home / SKILL).is_file() and manifest(ctx)["shared_skill"]["users"] == ["codex"]
    assert raw(home) == ORIGINALS["plain"]
    agents.release_shared_skill(ctx, "codex")
    assert not (home / SKILL).exists()


# ================================================================ a home folder with odd characters

def test_a_home_with_spaces_quotes_and_backslashes_still_gives_a_working_command(tmp_path):
    odd_home = tmp_path / "my home" / 'say "hi" it\'s a\\b'
    c = kit_setup.Ctx(odd_home)
    put_config(odd_home, ORIGINALS["plain"])
    kimi.install(c, KIT)
    text = raw(odd_home)
    _, block, _ = split_block(text)
    command = read_block(block)["hooks"][0]["command"]
    assert shlex.split(command) == ["sh", str(odd_home / ".config" / "bertie" / "hook.sh"), "--agent", "kimi"]
    command_line = next(l for l in block.split("\n") if l.startswith("command = "))
    assert '\\"hi\\"' in command_line and "\\\\b" in command_line            # quote and backslash are escaped
    if tomllib:
        assert tomllib.loads(text)["hooks"][0]["command"] == command
    kimi.uninstall(c)
    assert raw(odd_home) == ORIGINALS["plain"]


# ================================================================ TOML strings

@pytest.mark.parametrize("value,literal", [
    ("plain", '"plain"'),
    ('a"b', '"a\\"b"'),
    ("a\\b", '"a\\\\b"'),
    ("a\nb\tc\r", '"a\\nb\\tc\\r"'),
    ("\x00\x01\x1f\x7f", '"\\u0000\\u0001\\u001F\\u007F"'),
    ("\b\f", '"\\b\\f"'),
    ("a" + LS + "b" + PS + "c" + BOM + "d" + NEL + "e",
     '"a' + BS + 'u2028b' + BS + 'u2029c' + BS + 'uFEFFd' + BS + 'u0085e"'),
    ("caf" + E_ACUTE + " " + SNOWMAN, '"caf' + E_ACUTE + " " + SNOWMAN + '"'),
    ("", '""'),
])
def test_toml_strings_are_escaped(value, literal):
    assert kimi.toml_string(value) == literal
    assert unquote(literal) == value


def test_a_lone_surrogate_cannot_be_written():
    with pytest.raises(kit_setup.SetupError):
        kimi.toml_string("a\udcffb")


def test_toml_strings_round_trip_for_every_character_and_for_random_text():
    chars = [chr(i) for i in range(0x10000) if not 0xD800 <= i <= 0xDFFF] + [chr(0x1F600), chr(0x10FFFF)]
    chunks = ["".join(chars[i:i + 200]) for i in range(0, len(chars), 200)]
    rng = random.Random(7)
    alphabet = ['"', "\\", " ", "a", "b", "'", "\n", "\r", "\t", "\x00", "\x7f", LS, E_ACUTE, "$", "{", "/", "u", "n"]
    chunks += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))) for _ in range(300)]
    for chunk in chunks:
        lit = kimi.toml_string(chunk)
        assert unquote(lit) == chunk
        assert "\n" not in lit and "\r" not in lit
        if tomllib:
            assert tomllib.loads("k = " + lit)["k"] == chunk


# ================================================================ rules_cover

COVERED = [
    "bertie-mail send", "bertie-mail send r1", "bertie-mail trash 18c0", "bertie-mail untrash 18c0",
    "bertie-calendar create --title x", "bertie-calendar move e1", "bertie-calendar cancel e1",
    "bertie-chat send spaces/AAA hello", "bertie-chat dm someone@test.invalid hi",
    "bertie-drive upload ./f.pdf", "bertie-drive update ID ../f.pdf", "bertie-drive share ID a@test.invalid --role reader",
    "bertie logout", "bertie uninstall --yes",
    "bertie-mail send r1 && ls", "bertie-mail send r1 ; ls", "bertie-mail send r1 | cat", "bertie-mail send r1 $(ls)",
    "bertie-mail send 'a.b' .hidden", "bertie-mail send  two-spaces",
    # round 2 (R6): Kimi applies no rules, so a dot folder or a backslash no longer makes a plain send "uncovered"
    "bertie-drive upload ./.hidden/f", "bertie-mail send a/.b", "bertie-drive upload x/../y", "bertie-chat send /.x",
    "bertie-mail send r\\1", "bertie-mail send r1/.x", "bertie-drive upload ~/.config/x.pdf",
]


@pytest.mark.parametrize("cmd", COVERED)
def test_rules_cover_accepts_a_command_that_starts_with_a_gated_action(cmd):
    assert kimi.rules_cover(cmd) is True


NOT_COVERED = [
    # a line break anywhere (round 2 keeps this one: a second command can hide behind it)
    "bertie-drive upload a/b\nc", "bertie-drive upload a/b\r", "bertie-drive upload a/b" + LS + "c",
    "bertie-drive upload a/b" + PS + "c", "bertie-mail send r1\nls /tmp", "bertie-mail send r1\r\nls /tmp",
    "bertie-mail send r1\nls", "bertie-mail send r1\r\nls", "bertie-mail send r1" + LS + "ls",
    # a backslash that leaves the action word itself, or joins lines
    "bertie-mail send\\ r1", "bertie-mail send r1 \\\n x",
    # case counts
    "BERTIE-MAIL send r1", "Bertie-mail send r1", "bertie-mail SEND r1", "bertie-mail Send r1", "Bertie logout",
    # the command must start with the tool
    " bertie-mail send r1", "\tbertie-mail send r1", "echo hi; bertie-mail send r1", "ls && bertie-mail send r1",
    "cd /tmp && bertie-mail send r1", "env X=1 bertie-mail send r1", "~/.local/bin/bertie-mail send r1",
    "sh -c 'bertie-mail send r1'", "(bertie-mail send r1)", "bertie-mail --json send r1",
    # exactly one space, then the whole action word
    "bertie-mail  send r1", "bertie-mail\tsend r1", "bertie-mail sendx", "bertie-mail send/x", "bertie-mail send;ls",
    "bertie-mail sen", "bertie-mail trashed", "bertie-mail", "bertie", "bertie-mail send".upper(),
    # not gated
    "bertie-mail search x", "bertie-mail whoami", "bertie-calendar agenda", "bertie login", "ls", "",
]


@pytest.mark.parametrize("cmd", NOT_COVERED)
def test_rules_cover_refuses_anything_the_rules_might_miss(cmd):
    assert kimi.rules_cover(cmd) is False


@pytest.mark.parametrize("value", [None, 5, b"bertie-mail send", ["bertie-mail send"], {"command": "bertie-mail send"}])
def test_rules_cover_refuses_what_is_not_text(value):
    assert kimi.rules_cover(value) is False


def test_rules_cover_is_a_strict_true_or_false():
    assert {kimi.rules_cover(c) for c in COVERED + NOT_COVERED} == {True, False}


def test_every_gated_action_is_covered_when_it_stands_alone_and_with_an_argument():
    for tool, acts in registry.gated_actions().items():
        for act in acts:
            assert kimi.rules_cover(f"{tool} {act}") is True and kimi.rules_cover(f"{tool} {act} x") is True
            assert kimi.rules_cover(f"{tool}  {act}") is False and kimi.rules_cover(f"{tool} {act}x") is False
            assert kimi.rules_cover(f"{tool} {act.upper()} x") is False


def test_every_character_after_the_action_is_covered_unless_it_is_a_line_break():
    """Round 2 (R6): Kimi applies no rules, so the old slash, dot and backslash tests are gone; of all
    the characters only a line break (a second command can hide behind one) keeps a plain send from
    being covered, with or without a "/" beside it."""
    breaks = ("\n", "\r", LS, PS)
    for code in range(0x10000):
        if 0xD800 <= code <= 0xDFFF:
            continue
        c = chr(code)
        expect = c not in breaks
        assert kimi.rules_cover(f"bertie-mail send x{c}y") is expect, repr(c)
        assert kimi.rules_cover(f"bertie-mail send x/{c}y") is expect, repr(c)
        assert kimi.rules_cover(f"bertie-mail send {c}/x") is expect, repr(c)


# ================================================================ the hook: what Kimi sends and what it is answered

def test_the_hook_reads_kimis_snake_case_input():
    assert kimi.hook_input(bash("ls -la")) == ("command", "ls -la")
    assert kimi.hook_input(file_tool("Write", path="/x", content="hello")) == ("file", "/x\nhello")
    assert kimi.hook_input(file_tool("Edit", path="/x", old_string="a", new_string="b")) == ("file", "/x\na\nb")
    assert kimi.hook_input({"tool_name": "Read", "tool_input": {"path": "/x"}}) == ("none", "")
    assert kimi.hook_input({"tool_name": "Bash", "tool_input": {"command": ""}}) == ("command", "")
    assert kimi.hook_input({"tool_name": "Bash", "tool_input": {"command": "  "}}) == ("command", "  ")


@pytest.mark.parametrize("data", [
    {}, {"hook_event_name": "PreToolUse"}, {"tool_name": None}, {"tool_name": 5, "tool_input": {"command": "ls"}},
    {"tool_name": "Bash"}, {"tool_name": "Bash", "tool_input": None}, {"tool_name": "Bash", "tool_input": "ls"},
    {"tool_name": "Bash", "tool_input": {}}, {"tool_name": "Bash", "tool_input": {"cmd": "ls"}},
    {"tool_name": "Bash", "tool_input": {"command": 5}}, {"tool_name": "Bash", "tool_input": {"command": None}},
    {"tool_name": "Bash", "tool_input": {"command": ["ls"]}},
    {"tool_name": "Write", "tool_input": "x"}, {"tool_name": "Edit"}, {"tool_name": "Write", "tool_input": ["a"]},
    {"toolName": "Bash", "toolInput": {"command": "bertie-mail send r1"}},                 # camelCase is not Kimi's
])
def test_input_it_cannot_read_is_an_error_not_a_pass(data):
    with pytest.raises(ValueError):
        kimi.hook_input(data)


def test_the_answers():
    assert kimi.hook_answer(None) == ("", 0) and kimi.hook_answer("") == ("", 0)
    out, code = kimi.hook_answer("why")
    assert code == 0 and out.endswith("\n") and denied(out) == "why"
    out, code = kimi.hook_failure("broken")
    assert code == 0 and denied(out) == "broken"
    assert json.loads(out) == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                      "permissionDecisionReason": "broken"}}


# ---- through hook.py --agent kimi, in a process of its own

def test_a_plain_gated_send_gets_no_opinion_so_the_mac_window_asks():
    p = kimi_hook(bash("bertie-mail send r1 --body-file /tmp/body.txt"))
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")


def test_a_hidden_gated_send_is_denied_with_the_instruction():
    p = kimi_hook(bash("echo hi; bertie-mail send r1"))
    assert p.returncode == 0 and p.stderr == ""
    reason = denied(p.stdout)
    assert reason.startswith("Bertie safety check: ")
    assert reason.endswith("Run the Bertie command on its own, starting the line, e.g. `bertie-mail send ...` "
                           "(use --body-file for text with special characters), so the Mac window asks you first.")


@pytest.mark.parametrize("cmd", [
    "cd /tmp && bertie-mail send r1", "BERTIE-MAIL send r1", "bertie-mail --json send r1", 'bertie-mail "se""nd" r1',
    " bertie-mail send r1", "bertie-mail send r1\nls /tmp", "bertie-mail send r1" + LS + "/tmp",
    "bertie-mail send r1 \\\n x", "bertie-mail send r1 && ~/.config/bertie/.venv/bin/python x",
    "bertie-mail send r1; security find-generic-password -s bertie -w",
])
def test_a_gated_send_the_rule_would_miss_or_with_anything_else_wrong_is_denied(cmd):
    out, code = answer(bash(cmd))
    assert code == 0 and "Run the Bertie command on its own" in denied(out), cmd


@pytest.mark.parametrize("cmd", GATED + ["bertie-mail send r1 && ls", "bertie-drive upload ./f.pdf",
                                          "bertie-mail send r1/.x", "bertie-drive upload ~/.config/x.pdf"])
def test_every_plain_gated_command_is_left_to_the_rule(cmd):
    assert answer(bash(cmd)) == ("", 0)
    assert kimi.rules_cover(cmd) is True


@pytest.mark.parametrize("cmd", ATTACKS)
def test_no_attack_string_gets_past_the_kimi_hook(cmd):
    """Denied, unless it is a gated action on its own that the rules surely cover."""
    out, code = answer(bash(cmd))
    assert code == 0
    if out:
        # the send advice only when a send is one of the reasons (round 5, H2)
        if hook.decide_parts(cmd)[0]:
            assert denied(out).endswith("so the Mac window asks you first."), cmd
        else:
            assert denied(out).endswith(NEUTRAL), cmd
        return
    gated, other = hook.decide_parts(cmd)
    assert gated and not other and kimi.rules_cover(cmd) is True, cmd


def test_the_attack_strings_in_separate_processes_are_denied_unless_plain_and_covered():
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(lambda cmd: (cmd, kimi_hook(bash(cmd))), ATTACKS))
    passed = []
    for cmd, p in runs:
        assert p.returncode == 0 and p.stderr == "", cmd
        if p.stdout:
            if hook.decide_parts(cmd)[0]:
                assert "so the Mac window asks you first." in denied(p.stdout), cmd
            else:
                assert denied(p.stdout).endswith(NEUTRAL), cmd
        else:
            gated, other = hook.decide_parts(cmd)
            assert gated and not other and kimi.rules_cover(cmd) is True, cmd
            passed.append(cmd)
    assert set(GATED) <= set(passed) and len(passed) < len(ATTACKS) / 2


@pytest.mark.parametrize("tool,tool_input", [
    ("Write", {"path": "/tmp/a.sh", "content": "cp x ~/.config/bertie/bertie-approve"}),
    ("Write", {"path": "/tmp/a.py", "content": "import svc_mail"}),
    ("Write", {"path": "~/.config/bertie/hook.py", "content": "pass"}),
    ("Write", {"path": "/tmp/k.sh", "content": "security find-generic-password -s bertie -w"}),
    ("Edit", {"path": "/tmp/a", "old_string": "a", "new_string": "approve.sha256"}),
    ("Edit", {"path": "/tmp/a", "old_string": "x = 1", "new_string": "importlib.import_module('svc_'+'mail')"}),
])
def test_writing_text_that_reaches_the_tools_is_denied(tool, tool_input):
    p = kimi_hook({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input})
    assert p.returncode == 0 and p.stderr == ""
    reason = denied(p.stdout)
    assert "written to a file" in reason and "Run the Bertie command" not in reason


@pytest.mark.parametrize("tool,tool_input", [
    ("Write", {"path": "/tmp/a.py", "content": "print('hi')"}),
    ("Write", {"path": "notes.md", "content": "Use bertie-mail send to send."}),
    ("Edit", {"path": "/tmp/a.py", "old_string": "a", "new_string": "b"}),
    ("Read", {"path": "/tmp/a"}),
    ("Grep", {"pattern": "bertie-mail send"}),
])
def test_ordinary_writes_and_other_tools_get_no_opinion(tool, tool_input):
    p = kimi_hook({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input})
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")


@pytest.mark.parametrize("cmd", [c for c in FREE if c.strip()])
def test_harmless_commands_get_no_opinion(cmd):
    assert answer(bash(cmd)) == ("", 0), cmd


def test_a_blank_command_is_nothing_to_check():
    assert answer(bash("")) == ("", 0) and answer(bash("   ")) == ("", 0)


BAD_INPUT = [
    "not json", "{", "[]", "5", '"x"', "null", "", "{}",
    json.dumps({"hook_event_name": "PreToolUse"}),
    json.dumps({"tool_name": "Bash"}),
    json.dumps({"tool_name": "Bash", "tool_input": "ls"}),
    json.dumps({"tool_name": "Bash", "tool_input": {"command": 5}}),
    json.dumps({"tool_name": "Bash", "tool_input": {"cmd": "bertie-mail send r1"}}),
    json.dumps({"tool_name": "Write", "tool_input": "x"}),
    json.dumps({"toolName": "Bash", "toolInput": {"command": "bertie-mail send r1"}}),
]


@pytest.mark.parametrize("raw_text", BAD_INPUT)
def test_input_that_cannot_be_read_is_denied(raw_text):
    p = kimi_hook(raw_text=raw_text)
    assert p.returncode == 0 and p.stderr == ""
    assert "could not read this command" in denied(p.stdout) and "blocking to be safe" in denied(p.stdout)


def test_input_that_is_not_even_text_is_denied():
    p = subprocess.run([sys.executable, "-I", str(TOOLS / "hook.py"), "--agent", "kimi"],
                       input=b"\xff\xfe\x00", capture_output=True, timeout=30)
    assert p.returncode == 0 and "deny" in p.stdout.decode()


# ---- hook.sh

@pytest.mark.parametrize("args", [["--agent", "kimi"], ["--agent=kimi"]])
def test_hook_sh_without_python_blocks_kimi_with_exit_2(lonely, home, tmp_path, args):
    r = run_sh(lonely, args, json.dumps(bash("bertie-mail send r1")), home, tmp_path)
    assert r.returncode == 2 and r.stdout == ""
    assert r.stderr.strip() == "Safety check could not start (no working Python); blocking to be safe"


@pytest.fixture
def real_kit(tmp_path):
    """tools/ as it is installed, real adapters and all, with a private Python that is this one."""
    d = tmp_path / "realkit"
    shutil.copytree(TOOLS, d, ignore=shutil.ignore_patterns("__pycache__"))
    py = d / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable)
    py.chmod(0o755)
    return d


def test_hook_sh_end_to_end_with_the_real_kimi_adapter(real_kit, home, tmp_path):
    r = run_sh(real_kit, ["--agent", "kimi"], json.dumps(bash("bertie-mail send r1")), home, tmp_path)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    r = run_sh(real_kit, ["--agent", "kimi"], json.dumps(bash("true && bertie-mail send r1")), home, tmp_path)
    assert r.returncode == 0 and "so the Mac window asks you first." in denied(r.stdout)
    r = run_sh(real_kit, ["--agent", "kimi"], json.dumps(file_tool("Write", path="/x", content="import svc_mail")),
               home, tmp_path)
    assert r.returncode == 0 and "written to a file" in denied(r.stdout)
    r = run_sh(real_kit, ["--agent", "kimi"], "garbage", home, tmp_path)
    assert r.returncode == 0 and "blocking to be safe" in denied(r.stdout)


def test_a_missing_kimi_adapter_file_is_still_answered_with_a_deny(real_kit, home, tmp_path):
    (real_kit / "agents" / "kimi.py").unlink()
    r = run_sh(real_kit, ["--agent", "kimi"], json.dumps(bash("bertie-mail send r1")), home, tmp_path)
    assert r.returncode == 0 and "could not load its kimi adapter" in denied(r.stdout)


# ================================================================ the installer around it

def test_kit_setup_sets_kimi_up_after_claude_code(home):
    put_config(home, ORIGINALS["plain"])
    c = kit_setup.Ctx(home)
    msgs = kit_setup.install(c, KIT, False, ["claude", "kimi"])
    assert msgs.index("installed the skill 'bertie'") < msgs.index("Kimi Code: installed the skill in ~/.agents/skills/bertie")
    check_block(raw(home), c)
    assert (home / ".claude" / "settings.json").is_file() and (home / SKILL).is_file()
    done = kit_setup.uninstall_local(c)
    assert raw(home) == ORIGINALS["plain"] and not (home / SKILL).exists()
    assert any("Kimi Code: removed the ask rules and safety hook" in m for m in done)


def test_kimi_alone_leaves_claude_code_alone(home):
    put_config(home, ORIGINALS["plain"])
    kit_setup.install(kit_setup.Ctx(home), KIT, False, ["kimi"])
    assert not (home / ".claude").exists() and (home / ".kimi-code").is_dir()


def test_install_sh_sets_kimi_up_end_to_end_and_uninstall_takes_it_away(home, tmp_path, keychain, monkeypatch, dialog):
    import auth
    import common
    (home / ".zshrc").write_text("alias ll='ls -l'\n")
    put_config(home, ORIGINALS["no newline at the end"])
    before, before_dirs = tree(home), dirs(home)
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login", "--agent", "kimi")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Done. Open a NEW terminal, start Kimi Code, and say:" in r.stdout
    assert "  Kimi Code: installed the skill in ~/.agents/skills/bertie" in r.stdout
    assert "  Kimi Code: ask rules and safety hook added to ~/.kimi-code/config.toml" in r.stdout
    assert not (home / ".claude").exists()
    inst = home / ".config" / "bertie"
    text = raw(home)
    c = kit_setup.Ctx(home, inst)
    check_block(text, c)
    command = read_block(split_block(text)[1])["hooks"][0]["command"]
    assert shlex.split(command) == ["sh", str(inst / "hook.sh"), "--agent", "kimi"]
    assert (home / SKILL).is_file() and (inst / "agents" / "kimi.py").is_file()
    # the installed hook, run the way Kimi runs it: the command line from the config, through a shell
    env = env_for(home, tmp_path)

    def run_hook(payload):
        return subprocess.run(command, shell=True, env=env, capture_output=True, text=True, timeout=60,
                              input=json.dumps(payload))

    p = run_hook(bash("bertie-mail send r1"))
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    p = run_hook(bash("echo hi && bertie-mail send r1"))
    assert p.returncode == 0 and "so the Mac window asks you first." in denied(p.stdout)
    p = run_hook(file_tool("Edit", path="/x", old_string="a", new_string="~/.config/bertie/auth.py"))
    assert p.returncode == 0 and "written to a file" in denied(p.stdout)
    # a second run changes nothing
    snapshot = tree(home)
    assert run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login", "--agent", "kimi").returncode == 0
    assert tree(home) == snapshot
    # and the uninstall gives the home back
    monkeypatch.setattr(common, "HOME", inst)
    monkeypatch.setattr(auth, "_post", lambda url, data: (200, "{}"))
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps({"refresh_token": "R", "scopes": []}))
    google_cli.main(["uninstall", "--yes"])
    assert "what the installer set up for Kimi Code" in dialog.shown[-1]["message"]
    assert tree(home) == before and without_claude_leftovers(dirs(home)) == before_dirs
    assert raw(home) == ORIGINALS["no newline at the end"]


def test_install_sh_sets_kimi_up_by_default_when_its_folder_is_there(home, tmp_path):
    put_config(home, ORIGINALS["plain"])
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "start Claude Code or Kimi Code, and say:" in r.stdout
    assert (home / ".claude" / "settings.json").is_file()
    check_block(raw(home), kit_setup.Ctx(home, home / ".config" / "bertie"))


def test_install_sh_without_a_config_file_says_so_and_still_succeeds(home, tmp_path):
    (home / ".kimi-code").mkdir()
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login", "--agent", "kimi")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Kimi Code: ~/.kimi-code/config.toml was not found" in r.stdout and "NOT added" in r.stdout
    assert not config(home).exists() and (home / SKILL).is_file()


def test_install_sh_stops_with_a_clear_message_when_the_config_clashes(home, tmp_path):
    put_config(home, "[hooks]\nPreToolUse = []\n")
    r = run_install(home, tmp_path, ACCOUNT, "--skip-deps", "--skip-login", "--agent", "kimi")
    assert r.returncode == 1
    assert "already defines the hooks or the permission rules" in r.stderr and "line 1" in r.stderr
    assert raw(home) == "[hooks]\nPreToolUse = []\n" and not (home / SKILL).exists()


# ================================================================ review fixes (K1 to K11)

def kimi_bash(cmd, **extra):
    payload = bash(cmd)
    payload["tool_input"].update(extra)
    return payload


def installed(home, ctx, text=None):
    """The adapter installed over a config file; returns the messages."""
    put_config(home, ORIGINALS["plain"] if text is None else text)
    return kimi.install(ctx, KIT)


# ---- K1: Kimi does not apply the rules, so nothing may say it asks

def test_k1_the_block_message_names_the_mac_window_not_kimi():
    assert kimi.ask_clause() == "the Mac window asks you"
    out, code = answer(bash("echo hi; bertie-mail send r1"))
    reason = denied(out)
    assert code == 0 and reason.endswith("so the Mac window asks you first.") and "Kimi Code asks" not in reason


def test_k1_no_install_or_hook_text_claims_kimi_asks(home, ctx):
    text = "\n".join(installed(home, ctx))
    assert "Kimi Code asks" not in text and "Kimi asks before" not in text.replace("in its default mode it asks before", "")
    assert "Kimi checks with you" not in text
    # the rules are still written
    patterns = {r["pattern"] for r in read_block(split_block(raw(home))[1])["permission"]["rules"]}
    assert "Bash(bertie-mail send*)" in patterns


# ---- K2: the folder the command runs in

def test_k2_the_scan_text_puts_the_folder_in_front_of_the_command():
    # round 2: the folder is put in front only where it can matter (hook.cwd_prefix); an ordinary one is not
    assert kimi.hook_scan_text(kimi_bash("ls", cwd="/tmp/a b/.config/bertie"), "ls") == "cd '/tmp/a b/.config/bertie' && ls"
    assert kimi.hook_scan_text(kimi_bash("ls", cwd="/tmp/a b"), "ls") is None
    assert kimi.hook_scan_text(bash("ls"), "ls") is None
    assert kimi.hook_scan_text(kimi_bash("ls", cwd=""), "ls") is None
    assert kimi.hook_scan_text(file_tool("Write", path="/x", cwd="/y"), "") is None
    with pytest.raises(ValueError):
        kimi.hook_scan_text(kimi_bash("ls", cwd=5), "ls")


INSTALL_DIR = "/srv/me/.config/bertie"
BIN_DIR = "/srv/me/.local/bin"


@pytest.mark.parametrize("cmd,cwd", [
    ("ln -sf /usr/bin/true .venv/bin/python", INSTALL_DIR),
    ("mv hook.sh h", INSTALL_DIR),
    ("sed -i s/a/b/ common.py", INSTALL_DIR),
    ("./??????????? send r1", BIN_DIR),
    ("rm -f hook.sh", INSTALL_DIR),
    ("cp x kimi.py", INSTALL_DIR + "/agents"),
])
def test_k2_a_command_that_only_does_harm_in_the_right_folder_is_denied(cmd, cwd):
    assert answer(bash(cmd)) == ("", 0)                          # the same words, no folder: nothing to see
    out, code = answer(kimi_bash(cmd, cwd=cwd))
    assert code == 0 and denied(out).startswith("Bertie safety check: "), (cmd, cwd)


def test_k2_a_folder_that_names_a_tool_is_denied_in_a_process_of_its_own():
    p = kimi_hook(kimi_bash("ln -sf /usr/bin/true .venv/bin/python", cwd=INSTALL_DIR))
    assert p.returncode == 0 and p.stderr == "" and denied(p.stdout)


@pytest.mark.parametrize("cwd", ["/tmp", "/srv/work/a b", "", None])
def test_k2_a_plain_gated_send_in_a_clean_folder_gets_no_opinion(cwd):
    extra = {} if cwd is None else {"cwd": cwd}
    assert answer(kimi_bash("bertie-mail send r1", **extra)) == ("", 0)
    assert answer(kimi_bash("ls", **extra)) == ("", 0)


def test_k2_a_folder_that_is_not_text_is_denied():
    out, code = answer(kimi_bash("ls", cwd=5))
    assert code == 0 and "could not read" in denied(out)
    out, code = answer(kimi_bash("ls", cwd=["/tmp"]))
    assert code == 0 and "could not read" in denied(out)


def test_k2_a_hidden_send_is_still_denied_with_a_folder():
    out, code = answer(kimi_bash("cd /tmp && bertie-mail send r1", cwd="/srv/work"))
    assert code == 0 and "Run the Bertie command on its own" in denied(out)


# ---- K3: Kimi throws away every hook if one has a key it does not know

def test_k3_a_hook_with_a_key_kimi_does_not_accept_stops_the_install(home, ctx):
    original = '[[hooks]]\nevent = "PreToolUse"\ncommand = "echo mine"\nname = "mine"\n'
    put_config(home, original)
    with pytest.raises(kit_setup.SetupError, match="key 'name'.*does not accept"):
        kimi.install(ctx, KIT)
    assert raw(home) == original and not (home / SKILL).exists()


def test_k3_a_hook_with_an_event_kimi_does_not_know_stops_the_install(home, ctx):
    original = '[ui]\ntheme = "dark"\n\n[[hooks]]\nevent = "BeforeTool"\ncommand = "echo mine"\n'
    put_config(home, original)
    with pytest.raises(kit_setup.SetupError, match="event 'BeforeTool'.*does not know"):
        kimi.install(ctx, KIT)
    assert raw(home) == original and not (home / SKILL).exists()


@pytest.mark.parametrize("event", sorted(kimi.HOOK_EVENTS))
def test_k3_every_event_kimi_knows_is_accepted(home, ctx, event):
    original = f'[[hooks]]\nevent = "{event}"\ncommand = "echo mine"\ntimeout = 5\nmatcher = "x"\n'
    put_config(home, original)
    kimi.install(ctx, KIT)
    assert check_block(raw(home), ctx) == ("", original)             # round 2: in front of their first table


def test_k3_other_tables_may_hold_any_keys(home, ctx):
    original = '[ui]\nname = "x"\nevent = "nothing"\n\n[[models]]\nname = "m"\n'
    put_config(home, original)
    kimi.install(ctx, KIT)
    assert check_block(raw(home), ctx) == ("", original)


def test_k3_a_setting_added_after_our_block_stops_a_reinstall(home, ctx):
    installed(home, ctx, 'model = "x"\n')                             # no table: the block is at the end
    with_block = raw(home)
    put_config(home, with_block + 'theme = "light"\n')
    with pytest.raises(kit_setup.SetupError, match="right after Bertie's block.*Move that line above"):
        kimi.install(ctx, KIT)
    assert raw(home) == with_block + 'theme = "light"\n'
    put_config(home, with_block + '[ui]\ntheme = "light"\n')           # under a header of its own it is fine
    kimi.install(ctx, KIT)
    # round 2: and the block, now that the file has a table, moves to just in front of it
    _, block, _ = split_block(with_block)
    assert raw(home) == 'model = "x"\n' + block + '[ui]\ntheme = "light"\n'


def test_k3_a_foreign_line_inside_our_block_stops_install_and_uninstall(home, ctx):
    installed(home, ctx)
    text = raw(home)
    mixed = text.replace(END_LINE, 'theme = "light"\n' + END_LINE)
    put_config(home, mixed)
    with pytest.raises(kit_setup.SetupError, match="not Bertie's inside Bertie's block"):
        kimi.install(ctx, KIT)
    assert raw(home) == mixed
    lines = kimi.uninstall(ctx)
    assert raw(home) == mixed
    assert any("left exactly as it is" in m for m in lines) and any("to finish by hand" in m for m in lines)
    left = agents.hook_leftovers(ctx)                                    # the block is still there, so the folder is kept
    assert [(label, where) for label, where, _ in left] == [("Kimi Code", "~/.kimi-code/config.toml")]


# ---- K8: our lines without the marker lines

@pytest.mark.parametrize("which", ["rules", "hook"])
def test_k8_our_lines_without_the_markers_stop_the_install(home, ctx, which):
    installed(home, ctx)
    before, block, after = split_block(raw(home))
    lines = block.splitlines(True)
    body = [l for l in lines if not l.startswith("# ")]
    if which == "rules":                                           # keep only the rule tables
        keep = body[:body.index("[[hooks]]\n")]
    else:                                                          # keep only the hook table
        keep = body[body.index("[[hooks]]\n"):]
    stripped = before + "".join(keep) + after
    put_config(home, stripped)
    with pytest.raises(kit_setup.SetupError, match="start and end lines"):
        kimi.install(ctx, KIT)
    assert raw(home) == stripped
    assert any("left exactly as it is" in m or "to finish by hand" in m for m in kimi.uninstall(ctx))
    assert raw(home) == stripped


# ---- K4: KIMI_CODE_HOME

def test_k4_a_kimi_code_home_folder_is_used_and_recorded(home, ctx, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "config.toml").write_text('[ui]\ntheme = "dark"\n')
    monkeypatch.setenv("KIMI_CODE_HOME", str(other))
    assert kimi.detect(home) is True
    msgs = kimi.install(ctx, KIT)
    assert split_block((other / "config.toml").read_text())[1]
    assert not (home / ".kimi-code").exists()
    assert f"ask rules and safety hook added to {other / 'config.toml'}" in "\n".join(msgs)
    assert manifest(ctx)["agents"]["kimi"]["toml"] == {str(other / "config.toml"): {"lead": ""}}
    assert kimi.hook_files(ctx)[-1] == other / "config.toml"
    # the recorded file is what uninstall uses, even when the variable is gone by then
    monkeypatch.delenv("KIMI_CODE_HOME")
    lines = kimi.uninstall(ctx)
    assert (other / "config.toml").read_text() == '[ui]\ntheme = "dark"\n'
    assert any(f"removed the ask rules and safety hook from {other / 'config.toml'}" in m for m in lines)
    assert "kimi" not in manifest(ctx).get("agents", {}) and not (home / SKILL).exists()


def test_k4_without_the_variable_the_default_folder_is_used(home, ctx):
    assert kimi.detect(home) is False
    (home / ".kimi-code").mkdir()
    assert kimi.detect(home) is True


def test_k4_a_folder_that_is_not_there_is_not_detected(home, tmp_path, monkeypatch):
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "nothing-here"))
    (home / ".kimi-code").mkdir()
    assert kimi.detect(home) is False


def test_k4_a_relative_kimi_code_home_is_refused_plainly(home, ctx, monkeypatch):
    monkeypatch.setenv("KIMI_CODE_HOME", "relative/dir")
    assert kimi.detect(home) is False
    with pytest.raises(kit_setup.SetupError, match="not a full path"):
        kimi.install(ctx, KIT)
    assert not (home / SKILL).exists()


def test_k4_a_doctored_record_cannot_point_uninstall_elsewhere(home, ctx, tmp_path):
    victim = tmp_path / "notes.txt"
    victim.write_text(f"{BEGIN_LINE}\nx\n{END_LINE}\n")
    installed(home, ctx)
    m = manifest(ctx)
    m["agents"]["kimi"]["toml"] = {str(victim): {"lead": ""}, "../notes.txt": {"lead": ""}, 7: {}}
    kit_setup.save_manifest(ctx, m)
    kimi.uninstall(ctx)
    assert victim.read_text() == f"{BEGIN_LINE}\nx\n{END_LINE}\n"


# ---- K9 and K7: the files edited, and an uninstall that cannot finish

def test_k9_the_edited_files_are_recorded_for_the_backup_note(home, ctx):
    installed(home, ctx)
    sec = manifest(ctx)["agents"]["kimi"]
    assert sec["edited"] == [".kimi-code/config.toml"] and len(sec["backups"]) == 1
    assert ".kimi-code/config.toml" in agents.edited_files(manifest(ctx))


def test_k7_an_uninstall_that_cannot_edit_the_config_says_how_to_finish_and_still_removes_the_skill(home, ctx):
    installed(home, ctx)
    config(home).write_bytes(b"\xff\xfe not text")                     # no longer readable as text
    lines = kimi.uninstall(ctx)
    text = "\n".join(lines)
    assert "could not remove everything" in text and "~/.kimi-code/config.toml" in text
    assert 'delete the block from the line starting "# >>> bertie" to the line starting "# <<< bertie"' in text
    assert not (home / SKILL).exists()                                  # the other step still ran
    assert manifest(ctx)["agents"]["kimi"]["toml"]                       # and what is left stays on record
    assert agents.hook_leftovers(ctx) == []                              # nothing readable there names the hook


def test_k7_preflight_refuses_before_the_skill_is_written(home, ctx):
    put_config(home, "[hooks]\nPreToolUse = []\n")
    with pytest.raises(kit_setup.SetupError):
        kimi.preflight(ctx, KIT)
    assert not (home / SKILL).exists()
    put_config(home, ORIGINALS["plain"])
    kimi.preflight(ctx, KIT)
    assert not (home / SKILL).exists() and raw(home) == ORIGINALS["plain"]


def test_k7_the_adapter_lists_its_hook_file_and_how_to_clear_it(home, ctx):
    assert kimi.hook_files(ctx) == [home / ".kimi-code" / "config.toml"]
    assert kimi.HAND_HINT.startswith('delete the block from the line starting "# >>> bertie"')
    assert kimi.EMPTY_INPUT_IS_FAILURE is True


# ---- K11: the rules against picomatch 4.0.4, the matcher Kimi bundles, as fixed data

PICOMATCH = json.loads((TOOLS.parent / "tests" / "data" / "kimi_picomatch.json").read_text())


def test_k11_the_data_is_for_the_rules_written_today():
    import registry
    pairs = [list(kimi.patterns(t, a)) for t, acts in registry.gated_actions().items() for a in acts]
    assert PICOMATCH["picomatch"] == "4.0.4" and PICOMATCH["patterns"] == pairs
    assert len(PICOMATCH["cases"]) > 2000


def test_k11_rules_cover_is_true_where_picomatch_says_the_rules_miss_only_for_a_dot_folder_or_a_backslash():
    """Round 2 (R6): Kimi 0.8.2 applies no rules, so rules_cover no longer tries to match the glob. The
    only commands it accepts that the glob would miss are the ones with "/." or a backslash in them."""
    wrong = [cmd for cmd, matched in PICOMATCH["cases"] if kimi.rules_cover(cmd) and not matched]
    assert wrong and all("/." in cmd or "\\" in cmd for cmd in wrong)


def test_k11_rules_cover_is_true_for_most_plain_commands_picomatch_matches():
    matched = [cmd for cmd, m in PICOMATCH["cases"] if m]
    covered = [cmd for cmd in matched if kimi.rules_cover(cmd)]
    assert len(matched) > 1500 and len(covered) / len(matched) > 0.7
    for cmd in ("bertie-mail send r1", "bertie-drive share F a@test.invalid --role writer"):
        assert kimi.rules_cover(cmd)
