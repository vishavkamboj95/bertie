"""The Codex adapter: the rules file, the hooks file, the shared skill, what rules_cover says,
and the hook as Codex runs it (`hook.py --agent codex`, through hook.sh, from install.sh).

Every test runs in a temporary HOME. Starlark has no parser in the standard library, so the rules
file is read back with Python's own parser (a prefix_rule(...) call with keyword arguments is
valid Python) and checked against a small model of how Codex matches a command to its rules."""
import argparse
import ast
import concurrent.futures
import io
import json
import os
import random
import shlex
import stat
import subprocess
import sys

import pytest

import agents
import auth
import common
import google_cli
import hook
import kit_setup
import registry
from conftest import ACCOUNT, KIT, TOOLS
from test_agents_core import (ARGS, ATTACKS, backups, blocked, dirs, lonely, run_sh, tree,
                              without_claude_leftovers)
from test_hook import BYPASS, COMPOUND, FREE, GATED, UNREADABLE
from test_installer import env_for, run_install

codex = agents.get("codex")

RULES = ".codex/rules/bertie.rules"
HOOKS = ".codex/hooks.json"
SHARED = agents.SHARED_SKILL_REL
TAIL = ("Run the Bertie command on its own, starting the line, e.g. `bertie-mail send ...` "
        "(use --body-file for text with special characters), so Codex asks you first.")
# a block with no send among its reasons ends with this instead (round 5, H2)
NEUTRAL = "This touches Bertie's safety settings; ask the user to do it by hand if it is needed."


def tail_for(cmd):
    """The line a block message ends with: the send advice only when a send is one of the reasons."""
    return TAIL if hook.decide_parts(cmd)[0] else NEUTRAL


@pytest.fixture
def ctx(home, tmp_path):
    return kit_setup.Ctx(home, tmp_path / "inst")


def install(ctx, sends=None):
    if sends:
        agents.set_options(ctx, codex_sends=sends)
    return codex.install(ctx, KIT)


def rules_file(home):
    return (home / RULES).read_text()


def manifest(ctx):
    return kit_setup.load_manifest(ctx)


# ---------------------------------------------------------------- reading the rules file back

def parse_rules(text):
    """The prefix_rule(...) calls in a rules file, as dicts of their keyword arguments."""
    rules = []
    for node in ast.parse(text).body:
        assert isinstance(node, ast.Expr) and isinstance(node.value, ast.Call), ast.dump(node)
        assert node.value.func.id == "prefix_rule" and not node.value.args
        kws = [kw.arg for kw in node.value.keywords]
        assert len(kws) == len(set(kws)) and set(kws) <= {"pattern", "decision", "justification", "match"}, kws
        rules.append({kw.arg: ast.literal_eval(kw.value) for kw in node.value.keywords})
    return rules


RESTRICTIVE = {"allow": 0, "prompt": 1, "forbidden": 2}


def matches(rule, argv):
    """Codex's prefix match: each pattern item is one word, or a list of words that may stand there."""
    pattern = rule["pattern"]
    return len(argv) >= len(pattern) and all(a == p if isinstance(p, str) else a in p for a, p in zip(argv, pattern))


def codex_decision(rules, argv):
    """What Codex decides for an argv: the most restrictive of the rules whose pattern is a
    prefix of it; None when no rule matches."""
    found = [r["decision"] for r in rules if matches(r, argv)]
    return max(found, key=RESTRICTIVE.get) if found else None


def check_as_codex_loads_it(rules):
    """The checks Codex makes when it loads the file: known decisions, a non-empty pattern and
    justification, and every `match` example starting with its own rule's pattern."""
    assert rules
    for r in rules:
        assert r["pattern"] and all((isinstance(p, str) and p) or (isinstance(p, list) and p and all(isinstance(w, str) and w for w in p))
                                    for p in r["pattern"])
        assert r["decision"] in RESTRICTIVE
        assert "justification" not in r or r["justification"].strip()
        assert r["match"]
        for example in r["match"]:
            assert matches(r, shlex.split(example)), (r["pattern"], example)


def gated_pairs():
    return [(tool, act) for tool, acts in registry.gated_actions().items() for act in acts]


# ================================================================ the adapter's face

def test_the_adapter_says_what_it_is():
    assert (codex.KEY, codex.LABEL, codex.HOOK_STYLE) == ("codex", "Codex", "block")
    assert agents.get("codex") is codex
    for name in ("detect", "install", "uninstall", "hook_input", "hook_answer", "hook_failure", "rules_cover"):
        assert callable(getattr(codex, name)), name


def test_it_is_found_by_its_config_folder_alone(home, tmp_path, monkeypatch):
    assert codex.detect(home) is False
    (home / ".codex").mkdir()
    assert codex.detect(home) is True
    assert agents.detect_present(home) == ["codex"]
    assert agents.default_selection(home) == ["claude", "codex"]


def test_a_codex_program_on_the_path_does_not_count(home, tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "codex"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake.parent) + os.pathsep + os.environ["PATH"])
    assert codex.detect(home) is False
    (home / ".codex").write_text("a file, not a folder")
    assert codex.detect(home) is False


def test_the_module_imports_hook_only_inside_hook_input():
    tree_ = ast.parse((TOOLS / "agents" / "codex.py").read_text())
    top = [n for n in tree_.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top for a in n.names} | {n.module for n in top if isinstance(n, ast.ImportFrom)}
    assert "hook" not in names


# ================================================================ the rules file text

def test_starlark_strings_read_back_as_the_text_they_were_made_from():
    for text in ["plain", "", 'say "hi"', "back\\slash", "line\nbreak", "tab\there", "return\rhere", "nul\x00byte",
                 "del\x7f", "cafe\u00e9", "emoji \U0001f600", "100% $HOME `ls` #not-a-comment", "it's",
                 '\\"', "\\n is two characters", "a" * 500]:
        literal = codex.starlark_string(text)
        assert ast.literal_eval(literal) == text, text
        assert "\n" not in literal and literal.isascii() and literal[0] == literal[-1] == '"'


def test_the_rules_file_text_for_one_action_is_exactly_this():
    expected = (
        "# SENDS an email from the signed-in account\n"
        "prefix_rule(\n"
        '    pattern = ["bertie-mail", "send"],\n'
        '    decision = "prompt",\n'
        '    justification = "Bertie: SENDS an email from the signed-in account",\n'
        "    match = [\n"
        '        "bertie-mail send",\n'
        '        "bertie-mail send --dry-run",\n'
        "    ],\n"
        ")\n")
    assert expected in codex.rules_text("prompt")
    allow = (
        "prefix_rule(\n"
        '    pattern = ["bertie-chat", ["read", "spaces", "--help"]],\n'
        '    decision = "allow",\n'
        "    match = [\n"
        '        "bertie-chat read",\n'
        '        "bertie-chat spaces",\n'
        '        "bertie-chat --help",\n'
        "    ],\n"
        ")\n")
    assert allow in codex.rules_text("prompt")


def test_the_first_line_is_the_ownership_marker_and_nothing_names_a_person_or_a_path():
    text = codex.rules_text()
    assert text.splitlines()[0] == codex.RULES_MARKER and text.endswith(")\n")
    assert "/Use" + "rs/" not in text and chr(0x2014) not in text and text.isascii()


def test_one_prompt_rule_per_gated_action_and_one_named_reads_rule_per_tool():
    rules = parse_rules(codex.rules_text("prompt"))
    check_as_codex_loads_it(rules)
    prompts = [r for r in rules if r["decision"] == "prompt"]
    allows = [r for r in rules if r["decision"] == "allow"]
    assert [tuple(r["pattern"]) for r in prompts] == gated_pairs() and len(rules) == len(prompts) + len(allows)
    assert [r["pattern"] for r in allows] == [[t, list(codex.READS[t]) + ["--help"]] for t in registry.tool_names()]
    for r in prompts:
        tool, act = r["pattern"]
        assert r["justification"] == "Bertie: " + registry.gated_actions()[tool][act]
        assert r["match"] == [f"{tool} {act}", f"{tool} {act} --dry-run"]
    assert all("justification" not in r for r in allows)


def test_what_codex_would_decide_for_each_kind_of_command():
    rules = parse_rules(codex.rules_text("prompt"))
    for tool, act in gated_pairs():
        assert codex_decision(rules, [tool, act]) == "prompt"
        assert codex_decision(rules, [tool, act, "x", "--to", "a@test.invalid"]) == "prompt"
    for tool in registry.tool_names():
        for read in codex.READS[tool] + ("--help",):
            assert codex_decision(rules, [tool, read, "x"]) == "allow", (tool, read)
        for other in ("nonsense", "--version", "-h"):
            assert codex_decision(rules, [tool, other]) is None, (tool, other)
    assert codex_decision(rules, ["bertie-mail", "untrash", "x"]) == "prompt"       # not caught by 'trash'
    assert codex_decision(rules, ["bertie-mail-x", "send"]) is None
    assert codex_decision(rules, ["ls"]) is None and codex_decision(rules, ["BERTIE-MAIL", "send"]) is None
    # the rules cannot see an action that is not the second word: this is what the hook is for. Such a
    # line is not allowed outright either: it goes the way Codex sends everything it has no rule for
    assert codex_decision(rules, ["bertie-mail", "--json", "send", "r1"]) is None
    assert codex_decision(rules, ["bertie-mail", "--", "send", "r1"]) is None
    assert not codex.rules_cover("bertie-mail --json send r1")


def test_with_sends_allowed_the_gated_rules_say_allow_and_carry_no_justification():
    text = codex.rules_text("allow")
    rules = parse_rules(text)
    check_as_codex_loads_it(rules)
    assert [r["decision"] for r in rules] == ["allow"] * len(rules)
    assert [tuple(r["pattern"]) for r in rules if all(isinstance(p, str) for p in r["pattern"])] == gated_pairs()
    assert all("justification" not in r for r in rules) and 'decision = "prompt"' not in text
    assert text.startswith(codex.RULES_MARKER + "\n") and "SENDS an email from the signed-in account" in text


def test_a_choice_that_is_neither_prompt_nor_allow_is_refused():
    with pytest.raises(kit_setup.SetupError, match="prompt or allow"):
        codex.rules_text("maybe")


# ================================================================ install

def test_install_puts_each_file_where_codex_reads_it(ctx, home):
    msgs = install(ctx)
    assert (home / RULES).read_text() == codex.rules_text("prompt")
    assert stat.S_IMODE((home / RULES).stat().st_mode) == 0o644
    assert (home / SHARED).read_text() == agents.neutral_skill_text(agents.kit_skill_text(KIT))
    hooks = json.loads((home / HOOKS).read_text())
    cmd = "sh " + shlex.quote(str(ctx.install_dir / "hook.sh")) + " --agent codex"
    assert hooks == {"hooks": {"PreToolUse": [
        {"matcher": "^(Bash|apply_patch)$", "hooks": [{"type": "command", "command": cmd, "timeout": 30}]}]}}
    assert shlex.split(cmd) == ["sh", str(ctx.install_dir / "hook.sh"), "--agent", "codex"]
    assert not (home / ".claude").exists() and not (home / ".config").exists()
    assert all(m.startswith("Codex: ") for m in msgs)


def test_install_records_every_file_and_edit_and_the_choice(ctx, home):
    install(ctx)
    m = manifest(ctx)
    rec = m["agents"]["codex"]
    assert list(rec["files"]) == [RULES] and rec["files"][RULES]["created_dirs"] == [".codex", ".codex/rules"]
    assert list(rec["json"]) == [HOOKS] and rec["json"][HOOKS]["created_file"] is True
    assert rec["sends"] == "prompt" and m["shared_skill"]["users"] == ["codex"]


def test_the_report_says_to_trust_the_hook_and_that_it_is_untried(ctx, home):
    msgs = install(ctx)
    assert msgs[0].startswith("Codex: skill added to ~/.agents/skills/bertie/SKILL.md")
    assert msgs[1].startswith("Codex: ask rules written to ~/.codex/rules/bertie.rules")
    assert msgs[2].startswith("Codex: safety hook added to ~/.codex/hooks.json")
    last = msgs[-1]
    assert last.startswith("Codex: protected by ask rules") and "run /hooks" in last
    assert "not yet been tried by hand in Codex" in last and "the hook cannot ask, the rules do" in last
    assert len(msgs) == 4 and all(m.isascii() for m in msgs)


def test_installing_again_changes_nothing_and_says_already_in(ctx, home):
    install(ctx)
    before, saved = tree(home), ctx.manifest_path.read_bytes()
    msgs = install(ctx)
    assert tree(home) == before and ctx.manifest_path.read_bytes() == saved
    assert [m.split(":")[1].split(" already in")[0] for m in msgs[:3]] == [" skill", " ask rules", " safety hook"]
    assert all("already in" in m for m in msgs[:3])
    assert backups(home / ".codex") == []


def test_a_second_install_does_not_double_the_hook_in_a_users_file(ctx, home):
    (home / ".codex").mkdir()
    (home / HOOKS).write_text(json.dumps({"model": "x"}, indent=2) + "\n")
    install(ctx)
    install(ctx)
    assert len(json.loads((home / HOOKS).read_text())["hooks"]["PreToolUse"]) == 1
    assert len(backups(home / ".codex")) == 1                              # only the first edit made a backup


USER_HOOKS = {
    "model": "o-test",
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "theirs --check"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "bye"}]}],
    },
}


def seed(home, variant):
    if variant == "nothing":
        return
    (home / ".codex").mkdir()
    if variant == "empty folder":
        return
    (home / ".codex" / "rules").mkdir()
    (home / ".codex" / "config.toml").write_text('model = "o-test"\n')
    (home / ".codex" / "rules" / "default.rules").write_text('prefix_rule(pattern = ["ls"], decision = "allow")\n')
    (home / ".codex" / "hooks.json").write_text(json.dumps(USER_HOOKS, indent=4))          # no final newline
    (home / ".agents" / "skills" / "other").mkdir(parents=True)
    (home / ".agents" / "skills" / "other" / "SKILL.md").write_text("---\nname: other\n---\nmine\n")


@pytest.mark.parametrize("variant", ["nothing", "empty folder", "user files"])
def test_uninstall_leaves_the_home_exactly_as_it_was(ctx, home, variant):
    seed(home, variant)
    before, before_dirs = tree(home), dirs(home)
    install(ctx)
    assert tree(home) != before
    msgs = codex.uninstall(ctx)
    assert tree(home) == before and dirs(home) == before_dirs
    assert msgs[0] == "Codex: removed the safety hook from ~/.codex/hooks.json"
    assert "Codex: removed ~/.codex/rules/bertie.rules" in msgs
    assert "Codex: removed the skill ~/.agents/skills/bertie/SKILL.md" in msgs
    assert "agents" not in manifest(ctx) and "shared_skill" not in manifest(ctx)


def test_a_users_hooks_file_survives_the_round_trip_and_keeps_its_own_entries(ctx, home):
    seed(home, "user files")
    original = (home / HOOKS).read_bytes()
    install(ctx)
    got = json.loads((home / HOOKS).read_text())
    assert got["model"] == "o-test" and got["hooks"]["Stop"] == USER_HOOKS["hooks"]["Stop"]
    assert got["hooks"]["PreToolUse"][0] == USER_HOOKS["hooks"]["PreToolUse"][0]
    assert len(got["hooks"]["PreToolUse"]) == 2 and (home / ".codex" / "rules" / "default.rules").is_file()
    (b,) = backups(home / ".codex")
    assert (home / ".codex" / b).read_bytes() == original
    codex.uninstall(ctx)
    assert (home / HOOKS).read_bytes() == original


def test_uninstall_removes_the_folders_it_made_and_only_those(ctx, home):
    install(ctx)
    assert (home / ".codex" / "rules").is_dir()
    codex.uninstall(ctx)
    assert not (home / ".codex").exists() and not (home / ".agents").exists()
    seed(home, "empty folder")
    install(ctx)
    codex.uninstall(ctx)
    assert (home / ".codex").is_dir() and list((home / ".codex").iterdir()) == []


def test_the_skill_stays_while_another_agent_still_uses_it(ctx, home):
    agents.acquire_shared_skill(ctx, "kimi", KIT)
    install(ctx)
    msgs = codex.uninstall(ctx)
    assert (home / SHARED).is_file() and not any("skill" in m for m in msgs)
    assert manifest(ctx)["shared_skill"]["users"] == ["kimi"]


def test_a_rules_file_the_user_has_since_replaced_is_left_alone(ctx, home):
    install(ctx)
    (home / RULES).write_text("# my own rules now\n")
    msgs = codex.uninstall(ctx)
    assert (home / RULES).read_text() == "# my own rules now\n"
    assert not any("rules" in m for m in msgs)
    assert not (home / HOOKS).exists()


def test_uninstall_without_an_install_removes_nothing(ctx, home):
    (home / ".codex").mkdir()
    (home / ".codex" / "hooks.json").write_text(json.dumps(USER_HOOKS) + "\n")
    before = tree(home)
    assert codex.uninstall(ctx) == ["Codex: nothing to remove"]
    assert tree(home) == before


def test_a_hooks_file_broken_since_is_reported_by_hand_and_the_rest_still_goes(ctx, home):
    seed(home, "user files")
    install(ctx)
    (home / HOOKS).write_text("{broken")
    msgs = codex.uninstall(ctx)
    assert (home / HOOKS).read_text() == "{broken"
    assert msgs[0].startswith("Codex: could not edit ~/.codex/hooks.json") and "by hand" in msgs[0]
    assert shlex.quote(str(ctx.install_dir / "hook.sh")) in msgs[0]
    assert not (home / RULES).exists()                                       # the rules file went regardless
    assert HOOKS in manifest(ctx)["agents"]["codex"]["json"]                 # and the record stays for a retry
    (home / HOOKS).write_text(json.dumps({"model": "o-test", "hooks": {}}) + "\n")
    assert codex.uninstall(ctx)                                              # nothing left to do, no error


def test_a_hooks_file_that_is_not_text_is_a_report_not_a_crash(ctx, home):
    install(ctx)
    (home / HOOKS).write_bytes(b"\xff\xfe\x00bad")
    msgs = codex.uninstall(ctx)
    assert msgs[0].startswith("Codex: could not edit") and (home / HOOKS).read_bytes() == b"\xff\xfe\x00bad"


# ---------------------------------------------------------------- --codex-sends

def test_sends_allow_writes_allow_for_the_gated_actions_and_records_it(ctx, home):
    msgs = install(ctx, "allow")
    rules = parse_rules(rules_file(home))
    assert {r["decision"] for r in rules} == {"allow"}
    assert manifest(ctx)["agents"]["codex"]["sends"] == "allow"
    assert any("will not ask before a send" in m and "--codex-sends allow" in m for m in msgs)
    assert "ask rules" not in msgs[-1] and "run /hooks" in msgs[-1]
    codex.uninstall(ctx)
    assert not (home / ".codex").exists()


def test_the_choice_can_be_changed_by_installing_again(ctx, home):
    install(ctx)
    assert {r["decision"] for r in parse_rules(rules_file(home))} == {"allow", "prompt"}
    msgs = install(ctx, "allow")
    assert msgs[1].startswith("Codex: ask rules written to") and {r["decision"] for r in parse_rules(rules_file(home))} == {"allow"}
    install(ctx, "prompt")
    assert rules_file(home) == codex.rules_text("prompt") and manifest(ctx)["agents"]["codex"]["sends"] == "prompt"


def test_a_bad_choice_stops_the_install_before_anything_is_written(ctx, home):
    with pytest.raises(kit_setup.SetupError, match="prompt or allow"):
        install(ctx, "maybe")
    assert tree(home) == {} and not ctx.manifest_path.exists()


# ---------------------------------------------------------------- refusals

def snapshot(home):
    return tree(home), dirs(home), {str(p.relative_to(home)): os.readlink(p) for p in home.rglob("*") if p.is_symlink()}


def refused(ctx, home, match="already exists and was not made by this installer"):
    before = snapshot(home)
    with pytest.raises(kit_setup.SetupError, match=match):
        install(ctx)
    assert snapshot(home) == before                          # nothing of ours was written anywhere
    assert not ctx.manifest_path.exists() and backups(home / ".codex") == []


def test_a_rules_file_without_our_marker_is_never_overwritten(ctx, home):
    (home / ".codex" / "rules").mkdir(parents=True)
    (home / RULES).write_text('prefix_rule(pattern = ["ls"], decision = "allow")\n')
    refused(ctx, home)
    assert (home / RULES).read_text() == 'prefix_rule(pattern = ["ls"], decision = "allow")\n'


def test_our_marker_must_be_the_whole_first_line(ctx, home):
    (home / ".codex" / "rules").mkdir(parents=True)
    (home / RULES).write_text("# bertie: someone else's notes\n" + codex.RULES_MARKER + "\n")
    refused(ctx, home)
    (home / RULES).write_text("\n" + codex.RULES_MARKER + "\n")
    refused(ctx, home)


def test_a_rules_file_that_is_a_link_is_refused(ctx, home, tmp_path):
    (home / ".codex" / "rules").mkdir(parents=True)
    target = tmp_path / "elsewhere.rules"
    target.write_text(codex.RULES_MARKER + "\n")
    os.symlink(target, home / RULES)
    refused(ctx, home)
    assert target.read_text() == codex.RULES_MARKER + "\n"


def test_a_skill_that_is_not_bertie_s_is_never_overwritten(ctx, home):
    (home / SHARED).parent.mkdir(parents=True)
    (home / SHARED).write_text("---\nname: another-tool\n---\nhis\n")
    refused(ctx, home)
    assert (home / SHARED).read_text() == "---\nname: another-tool\n---\nhis\n"


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]", '"text"', '{"hooks": []}', '{"hooks": null}',
                                 '{"hooks": {"PreToolUse": {}}}', '{"hooks": {"PreToolUse": null}}'])
def test_a_hooks_file_that_cannot_be_merged_safely_is_left_untouched(ctx, home, bad):
    (home / ".codex").mkdir()
    (home / HOOKS).write_text(bad)
    refused(ctx, home, "left untouched")
    assert (home / HOOKS).read_text() == bad


def test_a_hooks_file_that_is_not_text_is_refused_not_a_crash(ctx, home):
    (home / ".codex").mkdir()
    (home / HOOKS).write_bytes(b"\xff\xfe\x00")
    refused(ctx, home, "could not be read")


@pytest.mark.parametrize("rel", [".codex", ".codex/rules", ".agents", ".agents/skills"])
def test_a_config_folder_that_is_really_a_file_is_refused(ctx, home, rel):
    (home / rel).parent.mkdir(parents=True, exist_ok=True)
    (home / rel).write_text("not a folder")
    refused(ctx, home, "is a file, not a folder")


def test_a_kit_without_its_skill_is_refused_before_anything_is_written(ctx, home, tmp_path):
    with pytest.raises(kit_setup.SetupError, match="missing"):
        codex.install(ctx, tmp_path / "empty-kit")
    assert tree(home) == {} and not ctx.manifest_path.exists()


# ================================================================ rules_cover

PLAIN_SENDS = GATED + [
    "bertie-mail send",
    "bertie-mail send r1 --to a@test.invalid --subject \"Hi, there (friend)\" --body \"Lunch at 1, bring the (red) file\"",
    "bertie-mail send r1 --body 'see https://x.test/a?b=1&c=2#frag for details, (all of them)'",
    "bertie-mail send r1 --body \"a; b | c > d && e (f) <g>\"",
    "bertie-chat send spaces/AAA 'Hello, it\"s (fine) and costs 5%'",
    "bertie-chat dm someone@test.invalid \"hi there, (really)\"",
    "bertie-chat dm someone@test.invalid hi",
    "bertie-drive upload /srv/share/Downloads/f.pdf",
    "bertie-drive share FILEID someone@test.invalid --role reader",
    "bertie-calendar create --title \"Standup (daily), room 4\" --start 2026-10-15T10:00 --end 2026-10-15T11:00",
    "bertie-mail send\tr1",
    "bertie-mail send r1 --body-file /tmp/body.txt   ",
    "bertie-mail send r1 --body \"caf\u00e9, na\u00efve\"",
    "bertie-mail send r1 --body \"\"",
    "bertie-mail send r1 --body 'it said \"stop\"'",
    "bertie-mail send r1 --body \"it's fine\"",
    "bertie logout",
    "bertie uninstall --yes",
]

HIDDEN = [
    # something else on the same line
    "bertie-mail send r1 && ls", "bertie-mail send r1 || ls", "bertie-mail send r1; ls", "bertie-mail send r1 ;ls",
    "bertie-mail send r1 | cat", "bertie-mail send r1 |& cat", "bertie-mail send r1 &", "bertie-mail send r1 & ls",
    "bertie-mail send r1\nls", "bertie-mail send r1\r\nls", "bertie-mail send r1\rls", "bertie-mail send\n",
    "ls && bertie-mail send r1", "echo hi; bertie-mail send r1", "cd /tmp && bertie-chat send spaces/A hi",
    "echo hi | bertie-mail send r1", "(bertie-mail send r1)", "{ bertie-mail send r1; }",
    # run-time text
    "bertie-mail send $(echo r1)", "bertie-mail send `echo r1`", "bertie-mail send $ID", "bertie-mail send ${ID}",
    "bertie-mail send \"$ID\"", "bertie-mail send \"$(ls)\"", "bertie-mail send \"`ls`\"", "bertie-mail send '$ID'",
    "bertie-mail send r1 --body \"costs $5\"", "bertie-mail send $'r\\x31'", "bertie-mail send r1 --body \"a\\nb\"",
    "bertie-mail send r\\1", "bertie-mail send r1\\\n--body x", "bertie-mail $ACTION r1", "$TOOL send r1",
    # redirections
    "bertie-mail send r1 > out", "bertie-mail send r1 >out", "bertie-mail send r1 2>/dev/null",
    "bertie-mail send r1 2>&1", "bertie-mail send r1 &>log", "bertie-mail send r1 < in", "bertie-mail send r1 <<EOF",
    "bertie-mail send r1 <<<x", "bertie-mail send r1 >> log", ">out bertie-mail send r1", "bertie-mail 2>/dev/null send r1",
    # not starting with the tool and the action, plainly
    "X=1 bertie-mail send r1", "FOO=bar bertie-chat send spaces/A hi", "env X=1 bertie-mail send r1",
    "env bertie-mail send r1", "command bertie-mail send r1", "exec bertie-mail send r1", "nohup bertie-mail send r1",
    "sudo bertie-mail send r1", " bertie-mail send r1", "\tbertie-mail send r1", "  bertie-mail send r1",
    "BERTIE-MAIL send r1", "Bertie-Mail send r1", "bertie-mail SEND r1", "bertie-mail Send r1",
    "bash -c 'bertie-mail send r1'", 'bash -lc "bertie-mail send r1"', "sh -c 'bertie-mail send r1'", "zsh -c bertie-mail",
    "~/.local/bin/bertie-mail send r1", "/usr/local/bin/bertie-mail send r1", "./bertie-mail send r1",
    "bertie-mail --json send r1", "bertie-mail -v send r1", "bertie-mail search send", "bertie-mail search r1 send",
    "bertie-mail draft --to a@test.invalid send", "bertie-chat read send", "bertie-mail  send r1", "bertie-mail  send",
    "bertie-mail 'send' r1", 'bertie-mail "send" r1', 'bertie-mail "se""nd" r1', "'bertie-mail' send r1", '"bertie-mail" send r1',
    "bertie-mail s''end r1", "bertie-mail sendx r1", "bertie-mail send-all", "bertie-mail sen", "bertie-mail", "bertie-mail ",
    "bertie logoutnow", "bertie-mails send r1", "xbertie-mail send r1", "my-bertie-mail send r1", "",
    # wildcards, braces, comments, history: Codex reads these as one opaque script
    "bertie-mail send *", "bertie-mail send r?", "bertie-mail send [ab]", "bertie-mail send r[1-3]", "bertie-mail send {a,b}",
    "bertie-mail send {1..3}", "bertie-mail send r1 # comment", "bertie-mail send r1 #", "bertie-mail send #r1",
    "bertie-mail send !r1", "bertie-mail send ^r1", "bertie-mail send r1 *.txt", "bertie-mail send ~/*.pdf",
    "bertie-mail send @(a|b)", "bertie-mail send +(a)", "bertie-mail send x(y)", "bertie-mail send (x)",
    # quotes that never close, or that hide punctuation as a word of its own
    "bertie-mail send \"unbalanced", "bertie-mail send 'unbalanced", "bertie-mail send r1 --body \"it's",
    "bertie-mail send r1 \"", "bertie-mail send r1 '", "bertie-mail send don't", "bertie-mail send r1 ';'",
    "bertie-mail send r1 \">\"", "bertie-mail send r1 \"&&\"", "bertie-mail send r1 \"(\"",
    # characters that are not plain
    "bertie-mail send r1\x00", "bertie-mail send r1\x0b", "bertie-mail send r1\x0c", "bertie-mail send r1\x1b[0m",
    "bertie-mail send r1\x7f", "bertie-mail send r1\u00a0--to x", "bertie-mail send caf\u00e9",
    # a quote that touches the text beside it, and characters that are only plain inside quotes
    'bertie-mail send r1 --subject="a b"', "bertie-mail send r1 --subject='a b'", "bertie-mail send r1 'a''b'",
    "bertie-mail send r1 'a'b", "bertie-mail send r1 a'b'", 'bertie-mail send r1 "a"\'b\'', "bertie-mail send r1 --subject=Hello",
    "bertie-mail send r1 --subject=Hello --max=5", "bertie-drive upload ~/Downloads/f.pdf", "bertie-mail send ~r1",
    "bertie-mail send r1 100%", "bertie-mail send r1 a+b", "bertie-mail send r1 a,b", "bertie-mail send a=b",
    # the same inside quotes: a newline or a control character in a body is not plain text to Codex's reader
    "bertie-mail send r1 --body \"line one\nline two\"", "bertie-mail send r1 --body 'a\nb'",
    "bertie-mail send r1 --body \"a\rb\"", "bertie-mail send r1 --body \"a\x00b\"",
    "bertie-mail send r1 --body \"a\x01b\"", "bertie-mail send r1 --body 'a\x1bb'", "bertie-mail send r1 --body \"a\x7fb\"",
]


@pytest.mark.parametrize("cmd", PLAIN_SENDS)
def test_a_plain_gated_command_is_covered_by_the_rules(cmd):
    assert codex.rules_cover(cmd) is True
    rules = parse_rules(codex.rules_text("prompt"))
    assert codex_decision(rules, shlex.split(cmd)) == "prompt"       # what Codex will do with it


@pytest.mark.parametrize("cmd", HIDDEN)
def test_every_hidden_form_is_not_covered(cmd):
    assert codex.rules_cover(cmd) is False


def test_every_command_the_hook_tests_call_compound_or_unreadable_is_not_covered():
    for cmd in COMPOUND + UNREADABLE + BYPASS:
        assert codex.rules_cover(cmd) is False, cmd


def test_only_the_plain_gated_attack_strings_are_covered_and_the_hook_still_blocks_the_rest():
    covered = [c for c in ATTACKS if codex.rules_cover(c)]
    assert set(GATED) <= set(covered)
    for cmd in covered:
        gated, other = hook.decide_parts(cmd)
        assert gated and not other, cmd


@pytest.mark.parametrize("value", [None, 5, b"bertie-mail send r1", ["bertie-mail", "send"], 1.5, object()])
def test_something_that_is_not_text_is_not_covered_and_never_raises(value):
    assert codex.rules_cover(value) is False


def test_a_rule_that_covers_a_command_means_codex_would_prompt_for_it_whatever_the_text():
    """A seeded fuzz: random lines that start like a send. Whenever rules_cover says yes, the
    words Codex would see begin with the pattern of a prompt rule and nothing but plain words and
    quoted text is on the line. It never raises."""
    pieces = [" ", " ", "r1", "--to", "a@test.invalid", "'", '"', "$", "`", "\\", ";", "&", "&&", "|", "(", ")", "<", ">", "#",
              "*", "?", "[", "]", "{", "}", "!", "~", "=", ",", "\n", "\t", "x y", "\"a b\"", "'c d'", "caf\u00e9", "%", "@", "^"]
    rules = parse_rules(codex.rules_text("prompt"))
    rng = random.Random(20261016)
    said_yes = 0
    for _ in range(4000):
        cmd = "bertie-mail send " + "".join(rng.choice(pieces) for _ in range(rng.randint(0, 7)))
        if not codex.rules_cover(cmd):
            continue
        said_yes += 1
        words = shlex.split(cmd)
        assert codex_decision(rules, words) == "prompt", cmd
        unquoted = ""
        quote = ""
        for c in cmd:
            if quote:
                quote = "" if c == quote else quote
            elif c in "'\"":
                quote = c
            else:
                unquoted += c
        assert all(c.isascii() and (c.isalnum() or c in "_-./:@=+,%~ \t") for c in unquoted), cmd
        assert not any(c in cmd for c in "$`\\\n\r"), cmd
    assert said_yes > 100                                        # the fuzz did reach the interesting cases


# ================================================================ the hook, as Codex calls it

def bash(command):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "t1", "cwd": "/tmp",
            "tool_input": {"command": command}}


def patch(text):
    return {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_input": {"command": text}}


def run_hook(payload, raw=None):
    return subprocess.run([sys.executable, "-I", str(TOOLS / "hook.py"), "--agent", "codex"],
                          input=raw if raw is not None else json.dumps(payload),
                          capture_output=True, text=True, timeout=60)


def in_process(payload):
    return hook.agent_answer("codex", io.StringIO(json.dumps(payload)))


def test_hook_input_reads_what_codex_sends():
    assert codex.hook_input(bash("bertie-mail send r1")) == ("command", "bertie-mail send r1")
    assert codex.hook_input(bash("")) == ("command", "")
    assert codex.hook_input(patch("*** Begin Patch\n*** End Patch")) == ("file", "*** Begin Patch\n*** End Patch")
    assert codex.hook_input({"tool_name": "apply_patch", "tool_input": {"command": "a", "x": ["b"]}}) == ("file", "a\nb")
    assert codex.hook_input({"tool_name": "mcp__x__y", "tool_input": {"a": "bertie-mail send"}}) == ("none", "")
    assert codex.hook_input({}) == ("none", "")


@pytest.mark.parametrize("data", [
    {"tool_name": "Bash"}, {"tool_name": "Bash", "tool_input": None}, {"tool_name": "Bash", "tool_input": "ls"},
    {"tool_name": "Bash", "tool_input": {}}, {"tool_name": "Bash", "tool_input": {"command": None}},
    {"tool_name": "Bash", "tool_input": {"command": ["ls"]}}, {"tool_name": "Bash", "tool_input": {"cmd": "ls"}},
    {"tool_name": "apply_patch"}, {"tool_name": "apply_patch", "tool_input": {}}, {"tool_name": "apply_patch", "tool_input": []},
])
def test_hook_input_refuses_what_it_cannot_read(data):
    with pytest.raises(ValueError):
        codex.hook_input(data)


def test_the_answers_are_the_deny_json_or_nothing():
    assert codex.hook_answer(None) == ("", 0) and codex.hook_answer("") == ("", 0)
    text, code = codex.hook_answer("because")
    assert code == 0 and text.endswith("\n") and json.loads(text) == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "because"}}
    assert codex.hook_failure("it broke") == (agents.pretooluse_answer("deny", "it broke"), 0)


@pytest.mark.parametrize("cmd", PLAIN_SENDS[:8] + ["bertie-calendar cancel ev1", "bertie logout"])
def test_a_plain_gated_command_gets_no_opinion_from_the_hook_so_codexs_own_rule_asks(cmd):
    p = run_hook(bash(cmd))
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    assert codex.rules_cover(cmd)


HIDDEN_GATED = [
    "echo hi; bertie-mail send r1", "cd /tmp && bertie-chat send spaces/A hi", "echo hi | bertie-mail send r1",
    "echo $(bertie-calendar cancel ev1)", "echo `bertie-calendar cancel ev1`", "bertie-mail send $ID",
    "bertie-mail send r1 > out", "bertie-mail send r1 2>&1", "bertie-mail send r1\nls", "env X=1 bertie-mail send r1",
    "X=1 bertie-mail send r1", " bertie-mail send r1", "BERTIE-MAIL send r1", "bash -c 'bertie-mail send r1'",
    "bertie-mail --json send r1", "bertie-mail send r1 --body \"unbalanced", "bertie-mail send r1 --body \"it's",
    "bertie-mail send r1 &", "bertie-drive share F a@test.invalid --role writer #", "bertie-mail send *",
    "bertie-mail send r1 --body \"costs $5\"", "bertie-mail 'send' r1",
]


@pytest.mark.parametrize("cmd", HIDDEN_GATED)
def test_a_gated_command_the_rules_would_miss_is_denied_with_the_instruction(cmd):
    p = run_hook(bash(cmd))
    assert p.returncode == 0 and p.stderr == ""
    reason = blocked(p.stdout)
    assert reason.startswith("Bertie safety check: ") and reason.endswith(TAIL), cmd
    assert not codex.rules_cover(cmd)


@pytest.mark.parametrize("cmd", [c for c in FREE if c.strip()][:12] + ["bertie-mail search send", "bertie-mail read 18c0"])
def test_a_harmless_command_gets_no_opinion(cmd):
    p = run_hook(bash(cmd))
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")


def test_every_harmless_command_gets_no_opinion_in_process():
    for cmd in [c for c in FREE if c.strip()]:
        assert in_process(bash(cmd)) == ("", 0), cmd


def test_every_attack_string_is_denied_unless_it_is_a_plain_gated_command_the_rules_cover():
    for cmd in ATTACKS:
        out, code = in_process(bash(cmd))
        assert code == 0
        if out:
            assert blocked(out).endswith(tail_for(cmd)), cmd
        else:
            gated, other = hook.decide_parts(cmd)
            assert gated and not other and codex.rules_cover(cmd) is True, cmd


def test_every_attack_string_is_denied_when_codex_runs_the_hook_as_a_process():
    def one(cmd):
        p = run_hook(bash(cmd))
        return cmd, p.returncode, p.stdout, p.stderr
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, ATTACKS))
    wrong = []
    for cmd, code, out, err in results:
        if code != 0 or err:
            wrong.append((cmd, "exit/stderr", code, err))
        elif out:
            if not blocked(out).endswith(tail_for(cmd)):
                wrong.append((cmd, "not the instruction"))
        elif not codex.rules_cover(cmd) or hook.decide_parts(cmd)[1]:
            wrong.append((cmd, "let through"))
    assert not wrong, wrong[:5]
    assert sum(1 for _, _, out, _ in results if out) > len(ATTACKS) // 2


def test_a_gated_command_with_anything_else_wrong_is_denied_even_when_it_starts_plainly():
    for cmd in ("bertie-mail send r1 /h/.config/bertie/account", "bertie-mail send r1 --body \"cat ~/.config/bertie/x\"",
                "bertie-chat send spaces/A security-find-generic-password-s-bertie", "bertie-mail send r1 osascript",
                "bertie-drive upload /h/.config/bertie/client_secret.json", "bertie-mail send r1 python3 svc_mail.py"):
        assert codex.rules_cover(cmd) is True and hook.decide_parts(cmd)[1], cmd     # the rules would ask; the hook says no
        out, _ = in_process(bash(cmd))
        assert out and blocked(out).endswith(TAIL), cmd


def test_a_command_that_is_not_gated_but_goes_near_the_install_folder_is_denied():
    for cmd in BYPASS[:6]:
        p = run_hook(bash(cmd))
        assert p.returncode == 0 and blocked(p.stdout).endswith(tail_for(cmd)), cmd


def test_a_patch_that_writes_the_bypass_text_is_denied_and_a_harmless_one_is_not():
    text = "*** Begin Patch\n*** Add File: run.sh\n+python3 ~/.config/bertie/svc_mail.py send r1\n*** End Patch\n"
    p = run_hook(patch(text))
    assert p.returncode == 0 and p.stderr == ""
    reason = blocked(p.stdout)
    assert "written to a file" in reason and "Run the Bertie command" not in reason
    ok = "*** Begin Patch\n*** Add File: notes.md\n+hello, a note about bertie-mail send in prose\n*** End Patch\n"
    assert run_hook(patch(ok)).stdout == ""
    assert run_hook(patch("")).stdout == ""
    for hostile in ("security find-generic-password -s bertie -w", "cat $HOME/.config/bertie/client_secret.json",
                    "import auth; auth.credentials()"):
        assert blocked(run_hook(patch("+" + hostile)).stdout)


def test_another_tool_is_nobody_s_business_and_a_blank_command_is_nothing_to_check():
    assert run_hook({"tool_name": "mcp__docs__search", "tool_input": {"q": "bertie-mail send"}}).stdout == ""
    assert run_hook(bash("")).stdout == "" and run_hook(bash("   ")).stdout == ""
    assert run_hook({}).stdout == ""                                            # an empty object: nothing to check


@pytest.mark.parametrize("raw", ["not json", "{", "[]", "5", '"x"', "null",
                                 json.dumps({"tool_name": "Bash"}), json.dumps({"tool_name": "Bash", "tool_input": {"command": 5}}),
                                 json.dumps({"tool_name": "apply_patch", "tool_input": {}})])
def test_input_that_cannot_be_read_is_denied_with_exit_zero(raw):
    p = run_hook(None, raw=raw)
    d = json.loads(p.stdout)["hookSpecificOutput"]
    assert p.returncode == 0 and p.stderr == "" and d["permissionDecision"] == "deny"
    assert d["permissionDecisionReason"].startswith("Bertie safety check could not read this command")


def test_a_command_that_is_far_too_long_is_denied_not_checked():
    p = run_hook(bash("bertie-mail send r1 " + "a" * 30000))
    assert "too long to check" in blocked(p.stdout)


def test_hook_sh_without_a_working_python_blocks_codex_with_exit_2(lonely, home, tmp_path):
    r = run_sh(lonely, ["--agent", "codex"], json.dumps(bash("bertie-mail send r1")), home, tmp_path)
    assert r.returncode == 2 and r.stdout == ""
    assert r.stderr.strip() == "Safety check could not start (no working Python); blocking to be safe"
    r = run_sh(lonely, ["--agent=codex"], json.dumps(bash("ls")), home, tmp_path)
    assert r.returncode == 2 and "blocking to be safe" in r.stderr


def test_hook_py_that_cannot_load_the_adapter_denies_in_codexs_format(tmp_path):
    only = tmp_path / "only"
    only.mkdir()
    for name in ("hook.py", "registry.py", "kit_setup.py"):
        (only / name).write_bytes((TOOLS / name).read_bytes())
    p = subprocess.run([sys.executable, "-I", str(only / "hook.py"), "--agent", "codex"],
                       input=json.dumps(bash("ls")), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ================================================================ install.sh --agent codex, end to end

def installed_hook(home):
    """The hook command Codex would run, read back from the hooks file install.sh wrote."""
    group = json.loads((home / HOOKS).read_text())["hooks"]["PreToolUse"][-1]
    return shlex.split(group["hooks"][0]["command"])


def run_installed_hook(home, tmp_path, command):
    return subprocess.run(installed_hook(home), input=json.dumps(bash(command)), capture_output=True, text=True,
                          timeout=60, env=env_for(home, tmp_path))


def test_install_sh_sets_up_codex_alone_and_the_installed_hook_works(home, tmp_path):
    (home / ".codex").mkdir()
    (home / HOOKS).write_text(json.dumps(USER_HOOKS, indent=2) + "\n")
    r = run_install(home, tmp_path, *ARGS, "--agent", "codex")
    assert r.returncode == 0, r.stdout + r.stderr
    for line in ("  Codex: skill added to ~/.agents/skills/bertie/SKILL.md", "  Codex: ask rules written to ~/.codex/rules/bertie.rules",
                 "  Codex: safety hook added to ~/.codex/hooks.json"):
        assert line in r.stdout
    assert "run /hooks" in r.stdout and "has not yet been tried by hand in Codex" in r.stdout
    assert "Done. Open a NEW terminal, start Codex, and say:" in r.stdout
    assert not (home / ".claude").exists() and (home / RULES).read_text() == codex.rules_text("prompt")
    d = home / ".config" / "bertie"
    assert (d / "agents" / "codex.py").is_file() and stat.S_IMODE((d / "agents" / "codex.py").stat().st_mode) == 0o400
    cmd = installed_hook(home)
    assert cmd == ["sh", str(d / "hook.sh"), "--agent", "codex"]
    pre = json.loads((home / HOOKS).read_text())["hooks"]["PreToolUse"]
    assert pre[0] == USER_HOOKS["hooks"]["PreToolUse"][0] and len(pre) == 2
    # what Codex does with the hook, through hook.sh and the installed copy of hook.py
    plain = run_installed_hook(home, tmp_path, 'bertie-mail send r1 --body "Hi, (all)"')
    assert (plain.returncode, plain.stdout, plain.stderr) == (0, "", "")
    hidden = run_installed_hook(home, tmp_path, "echo hi; bertie-mail send r1")
    assert hidden.returncode == 0 and blocked(hidden.stdout).endswith(TAIL)
    # running the installer again changes nothing
    before = tree(home)
    assert run_install(home, tmp_path, *ARGS, "--agent", "codex").returncode == 0
    assert tree(home) == before


def test_install_sh_sends_allow_reaches_the_rules_file(home, tmp_path):
    r = run_install(home, tmp_path, *ARGS, "--agent", "codex", "--codex-sends", "allow")
    assert r.returncode == 0, r.stdout + r.stderr
    assert {x["decision"] for x in parse_rules(rules_file(home))} == {"allow"}
    assert "will not ask before a send" in r.stdout and not (home / ".claude").exists()
    m = json.loads((home / ".config" / "bertie" / "install-manifest.json").read_text())
    assert m["agents"]["codex"]["sends"] == "allow"
    assert "Done. Open a NEW terminal, start Codex, and say:" in r.stdout


def test_install_sh_finds_codex_by_its_folder_and_uninstall_leaves_the_home_as_it_was(home, tmp_path, keychain,
                                                                                      monkeypatch, dialog):
    (home / ".zshrc").write_text("alias ll='ls -l'\n")
    seed(home, "user files")
    before, before_dirs = tree(home), dirs(home)
    r = run_install(home, tmp_path, *ARGS)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Done. Open a NEW terminal, start Claude Code or Codex, and say:" in r.stdout
    assert (home / ".claude" / "settings.json").is_file() and (home / RULES).is_file() and (home / SHARED).is_file()
    assert tree(home) != before
    monkeypatch.setattr(common, "HOME", home / ".config" / "bertie")
    monkeypatch.setattr(auth, "_post", lambda url, data: (200, "{}"))
    keychain.set_password(registry.KEYCHAIN_SERVICE, ACCOUNT, json.dumps({"refresh_token": "R", "scopes": []}))
    google_cli.main(["uninstall", "--yes"])
    assert tree(home) == before and without_claude_leftovers(dirs(home)) == before_dirs
    assert "Codex" in dialog.shown[-1]["message"]


def test_install_sh_stops_with_a_clear_message_for_a_rules_file_that_is_not_ours(home, tmp_path):
    (home / ".codex" / "rules").mkdir(parents=True)
    (home / RULES).write_text("# somebody else's rules\n")
    r = run_install(home, tmp_path, *ARGS, "--agent", "codex")
    assert r.returncode == 1 and "already exists and was not made by this installer" in r.stderr
    assert (home / RULES).read_text() == "# somebody else's rules\n"
    assert not (home / HOOKS).exists() and not (home / SHARED).exists()


# ================================================================ the review round (X1-X9, C-items)

def parser_actions():
    import svc_calendar, svc_chat, svc_drive, svc_mail
    out = {}
    for tool, mod in (("bertie-mail", svc_mail), ("bertie-calendar", svc_calendar), ("bertie-chat", svc_chat),
                      ("bertie-drive", svc_drive), ("bertie", google_cli)):
        sub = next(a for a in mod.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        out[tool] = set(sub.choices)
    return out


def test_every_action_of_every_command_is_a_named_read_a_gated_action_or_a_known_non_read():
    real = parser_actions()
    assert set(real) == set(registry.tool_names()) == set(codex.READS)
    for tool, actions in real.items():
        gated = set(registry.gated_actions()[tool])
        reads = set(codex.READS[tool])
        assert reads <= actions and not reads & gated and not reads & set(codex.NOT_READS), tool
        assert actions == reads | gated | (actions & set(codex.NOT_READS)), (tool, actions - reads - gated)


def test_a_download_a_draft_or_a_login_is_not_allowed_outright():
    rules = parse_rules(codex.rules_text("prompt"))
    for argv in (["bertie-mail", "attachments", "m1"], ["bertie-mail", "draft", "--attach", "/etc/hosts"],
                 ["bertie-drive", "download", "f1"], ["bertie", "login"]):
        assert codex_decision(rules, argv) is None, argv
    for argv in (["bertie", "logout"], ["bertie", "uninstall"], ["bertie-mail", "send", "d1"]):
        assert codex_decision(rules, argv) == "prompt", argv


def test_no_rule_allows_a_whole_command_any_more():
    for r in parse_rules(codex.rules_text("prompt")):
        if r["decision"] == "allow":
            assert len(r["pattern"]) == 2 and isinstance(r["pattern"][1], list), r["pattern"]


def test_a_hostile_description_cannot_break_out_of_its_comment_line(monkeypatch):
    nasty = {"bertie-mail": {"send": "sends\nprefix_rule(pattern = [\"ls\"], decision = \"allow\")\r\x00\x1b café"}}
    monkeypatch.setattr(registry, "gated_actions", lambda: nasty)
    text = codex.rules_text("prompt")
    assert text.isascii() and "\r" not in text and "\x00" not in text
    rules = parse_rules(text)
    assert [r["pattern"] for r in rules if r["pattern"][0] == "ls"] == []
    assert all(line.startswith("#") or not line.startswith("prefix_rule(pattern") for line in text.splitlines())


@pytest.mark.parametrize("cmd", [
    "bertie-mail send r1 --subject=\"a b\"", "bertie-mail send r1 --subject='a b'", "bertie-mail send r1 'a''b'",
    "bertie-mail send r1 --subject=Hello", "bertie-drive upload ~/f.pdf", "bertie-mail send r1 a,b", "bertie-mail send r1 a+b",
])
def test_a_quote_touching_text_and_unquoted_tilde_equals_percent_plus_comma_are_not_covered(cmd):
    assert codex.rules_cover(cmd) is False
    message = blocked(run_hook(bash(cmd)).stdout)
    if cmd in ("bertie-mail send r1 --subject=Hello", "bertie-drive upload ~/f.pdf", "bertie-mail send r1 a,b"):
        # round 2 (R10): when the only problem is an unquoted , = or ~, the message says exactly that
        assert message.endswith("so Codex asks you first.") and "quote the argument with , = or ~" in message
    else:
        assert message.endswith(TAIL)


@pytest.mark.parametrize("cmd", ["bertie-mail send r1 --subject \"a b\"", "bertie-mail send r1 \"a=b, 100%, ~/x +1\"",
                                 "bertie-mail send r1 'a' 'b'"])
def test_those_same_characters_inside_closed_quotes_are_still_covered(cmd):
    assert codex.rules_cover(cmd) is True


def test_a_reinstall_without_the_flag_keeps_the_recorded_choice(ctx, home):
    install(ctx, "allow")
    msgs = codex.install(ctx, KIT)                                            # no codex_sends given this time
    assert {r["decision"] for r in parse_rules(rules_file(home))} == {"allow"}
    assert manifest(ctx)["agents"]["codex"]["sends"] == "allow"
    assert any("will not ask before a send" in m for m in msgs)
    ctx2 = kit_setup.Ctx(home, ctx.install_dir)
    assert codex._sends(ctx2) == "allow"
    install(ctx, "prompt")                                                    # said out loud: that wins
    assert "prompt" in {r["decision"] for r in parse_rules(rules_file(home))}
    assert manifest(ctx)["agents"]["codex"]["sends"] == "prompt"
    codex.install(ctx, KIT)
    assert manifest(ctx)["agents"]["codex"]["sends"] == "prompt"


def test_the_block_message_says_the_mac_window_asks_when_codex_does_not(tmp_path, monkeypatch):
    manifest_path = tmp_path / "install-manifest.json"
    manifest_path.write_text(json.dumps({"agents": {"codex": {"sends": "allow"}}}))
    monkeypatch.setattr(codex, "__file__", str(tmp_path / "agents" / "codex.py"))
    assert codex.ask_clause() == "the Mac window asks you"
    reason = hook.decide_block(codex, "bertie-mail --json send r1")
    assert reason.endswith("so the Mac window asks you first.") and "Codex asks" not in reason
    manifest_path.write_text(json.dumps({"agents": {"codex": {"sends": "prompt"}}}))
    assert codex.ask_clause() == "Codex asks you" and hook.decide_block(codex, "bertie-mail --json send r1").endswith(TAIL)
    manifest_path.write_text("not json")
    assert codex.ask_clause() == "Codex asks you"


def test_the_folder_a_command_runs_in_is_checked_and_the_ask_rule_is_still_judged_on_the_command():
    # round 2: a folder is put in front only where it can matter (hook.cwd_prefix), so /w is no longer
    # written in; the two folders below are the bin folder and the install folder
    assert codex.hook_scan_text({"cwd": "/h/.local/bin", "tool_input": {"command": "ls", "workdir": "/h/.config/bertie"}},
                                "ls") == "cd /h/.local/bin && cd /h/.config/bertie && ls"
    assert codex.hook_scan_text({"cwd": "/w", "tool_input": {"command": "ls", "workdir": "/h/.config/bertie"}}, "ls") \
        == "cd /h/.config/bertie && ls"
    # round 2: justification was one of the fields read; it is prose for the person now, so a field Codex
    # does not document stands in for "any other text field"
    assert codex.hook_scan_text({"tool_input": {"command": "ls", "note": "why"}}, "ls") == "ls\nwhy"
    with pytest.raises(ValueError):
        codex.hook_scan_text({"tool_input": {"command": "ls", "workdir": 5}}, "ls")
    with pytest.raises(ValueError):
        codex.hook_scan_text({"cwd": ["x"], "tool_input": {"command": "ls"}}, "ls")


@pytest.mark.parametrize("command,workdir", [
    ("ln -sf /usr/bin/true .venv/bin/python", "/h/.config/bertie"),
    ("mv hook.sh h", "/h/.config/bertie"),
    ("./??????????? send r1", "/h/.local/bin"),
    ("sed -i s/a/b/ common.py", "/h/.config/bertie"),
])
def test_a_command_that_only_does_harm_in_the_right_folder_is_blocked(command, workdir):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": "/tmp",
               "tool_input": {"command": command, "workdir": workdir}}
    assert blocked(run_hook(payload).stdout), (command, workdir)


def test_a_plain_send_in_an_ordinary_folder_is_still_left_to_the_ask_rule():
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": "/tmp",
               "tool_input": {"command": "bertie-mail send r1", "workdir": "/work/project"}}
    assert run_hook(payload).stdout == ""
    payload["tool_input"]["workdir"] = "/h/.config/bertie"
    assert blocked(run_hook(payload).stdout)


def test_any_other_text_field_in_the_tool_input_is_checked_too():
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": "/tmp",
               "tool_input": {"command": "ls", "note": "import svc_mail"}}
    assert blocked(run_hook(payload).stdout)


@pytest.mark.parametrize("cmd", [
    "sed -i s/a/b/ ~/.codex/hooks.json", "rm ~/.codex/rules/bertie.rules", "echo 'hooks = false' >> ~/.codex/config.toml",
    "cd ~/.codex && mv hooks.json x", "cat x > $HOME/.codex/rules/default.rules",
])
def test_a_command_naming_codexs_hook_rule_or_settings_files_is_blocked(cmd):
    reason = blocked(run_hook(bash(cmd)).stdout)
    assert "safety hook is set up" in reason and reason.endswith(NEUTRAL)     # no send among the reasons


@pytest.mark.parametrize("path", [".codex/hooks.json", ".codex/rules/bertie.rules", ".codex/config.toml",
                                  ".gemini/config/hooks.json", ".cursor/hooks.json"])
def test_a_patch_to_an_agents_hook_files_is_blocked(path):
    text = f"*** Begin Patch\n*** Update File: /h/{path}\n@@\n-a\n+b\n*** End Patch\n"
    reason = blocked(run_hook(patch(text)).stdout)
    assert "safety hook is set up" in reason and "written to a file" in reason


@pytest.mark.parametrize("raw", ["", "  ", "\n"])
def test_input_with_nothing_in_it_is_blocked_not_let_through(raw):
    assert "could not read this command" in blocked(run_hook(None, raw=raw).stdout)


def test_a_hooks_file_that_is_a_link_is_refused_before_anything_is_written(ctx, home, tmp_path):
    real = tmp_path / "dot" / "hooks.json"
    real.parent.mkdir()
    real.write_text("{}\n")
    (home / ".codex").mkdir()
    (home / HOOKS).symlink_to(real)
    before = tree(home)
    with pytest.raises(kit_setup.SetupError, match="link"):
        install(ctx)
    assert tree(home) == before and real.read_text() == "{}\n" and not (home / RULES).exists()


def test_a_hooks_file_that_is_not_text_is_refused_up_front_in_plain_words(ctx, home):
    (home / ".codex").mkdir()
    (home / HOOKS).write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(kit_setup.SetupError, match="could not be read"):
        install(ctx)
    assert not (home / RULES).exists() and not (home / SHARED).exists()
