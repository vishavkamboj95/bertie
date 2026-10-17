"""Fix round 2, from the second review of the other-agent work: the folder a command runs in is
read only where it matters, an agent's hook file is recognised by the shape of its path, a
recorded file that became a link no longer stops an uninstall, Kimi's block goes before the
first table, plain sends are not blocked for no reason, and the smaller things (what the
uninstall prints, which hook path counts as ours).

HOME is a temporary folder everywhere (the autouse fixture in conftest.py)."""
import io
import json
import os
import random
import shlex

import pytest

import agents
import hook
import kit_setup
from agents import antigravity, codex, cursor, kimi
from conftest import KIT
from test_agent_kimi import (BEGIN_LINE, CONFIG, END_LINE, OLD_END_LINE, ORIGINALS, BLOCK_BEFORE, check_block,
                             config, expected_place, manifest, put_config, raw, split_block, tomllib)
from test_agents_core import backups, ctx, dirs, tree  # noqa: F401
from test_core_fixes import ARGS, HOOKS, needs_user, put, uninstall
from test_installer import run_install


# ---------------------------------------------------------------- the three agents that send a folder

def codex_call(command, cwd="/tmp", **tool_input):
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": cwd,
            "tool_input": dict(command=command, **tool_input)}


def cursor_call(command, cwd="/work/project"):
    return {"hook_event_name": "beforeShellExecution", "command": command, "cwd": cwd}


def kimi_call(command, cwd=None, top=None, **tool_input):
    """cwd is tool_input.cwd; top is the session's own folder, at the top level."""
    if cwd is not None:
        tool_input["cwd"] = cwd
    data = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": dict(command=command, **tool_input)}
    if top is not None:
        data["cwd"] = top
    return data


def ask(key, payload):
    """(no opinion?, the reason or None) for one call, in the agent's own format."""
    out, code = hook.agent_answer(key, io.StringIO(json.dumps(payload)))
    assert code == 0
    if key == "cursor":
        d = json.loads(out)
        return d["permission"] == "allow", d.get("user_message")
    if not out:
        return True, None
    return False, json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


def call_for(key, command, cwd):
    return {"codex": codex_call, "cursor": cursor_call, "kimi": lambda c, d: kimi_call(c, cwd=d)}[key](command, cwd)


AGENTS = ("codex", "cursor", "kimi")


# ================================================================ R1: the folder only where it matters

@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command,cwd", [
    ("ls -la", "/x/src/bertie"),                          # a clone of this repository
    ("ls -la", "/x/src/bertie-publish"),
    ("ls -la", "/x/src/python-keyring"),                  # a folder with "keyring" in its name
    ("rm -f *", "/x/proj/bin"),                           # a bin folder that is not ~/.local/bin
    ("git status", "/x/work/svc_payments"),               # a folder named like a tool module
    ("git status", "/x/osascript-tools"),
    ("git status", "/x/approve.swift-demo"),
    ("cargo build", "/x/work/.config/other"),
    ("ls", "/x/work/bertie/.git"),
])
def test_r1_an_ordinary_folder_does_not_trip_the_checks(key, command, cwd):
    assert ask(key, call_for(key, command, cwd)) == (True, None), (key, command, cwd)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command,cwd", [
    ("mv hook.sh h", "/h/.config/bertie"),
    ("ln -sf /usr/bin/true .venv/bin/python", "/h/.config/bertie"),
    ("sed -i s/a/b/ common.py", "/h/.config/bertie/agents"),
    ("rm -f svc_mail.py", "/h/.config/bertie/.venv/bin"),
    ("./??????????? send r1", "/h/.local/bin"),
    ("mv bertie old", "/h/.config"),
])
def test_r1_a_command_that_only_does_harm_in_the_install_bin_or_config_folder_is_still_caught(key, command, cwd):
    quiet, reason = ask(key, call_for(key, command, cwd))
    assert not quiet and "safety check" in reason, (key, command, cwd)


@pytest.mark.parametrize("cwd", ["/h/.config", "/h/.local", "/h/.local/bin", "/h/.config/bertie", "/h/.config/bertie/x/y"])
def test_r1_the_install_bin_and_config_folders_are_read(cwd):
    assert hook.cwd_prefix(cwd, "ls") == "cd " + cwd + " && "


@pytest.mark.parametrize("cwd", ["/h/.cursor", "/h/.codex", "/h/.codex/rules", "/h/.gemini", "/h/.gemini/config",
                                 "/h/.gemini/antigravity-cli", "/h/.kimi-code", "/h/.agents", "/h/.agents/skills",
                                 "/h/.agents/skills/bertie"])
def test_r1_exactly_a_folder_that_holds_an_agent_file_is_read(cwd):
    command = "ls"
    assert hook.cwd_prefix(cwd, command) == "cd " + cwd + " && "
    assert hook.cwd_prefix(cwd + "/deeper", command) == ""               # deeper inside it is not, without ".."
    assert hook.cwd_prefix(cwd + "/deeper", "cat ../x") == "cd " + cwd + "/deeper && "   # with ".." it is


def test_r1_a_folder_is_judged_by_where_it_really_is(tmp_path):
    install = tmp_path / "home" / ".config" / "bertie"
    install.mkdir(parents=True)
    (tmp_path / "work").mkdir()
    link = tmp_path / "work" / "proj"
    link.symlink_to(install)
    # round 4: a folder that matters only because of where the link leads is put in as that real place
    # (a link to the home folder is the home folder), not as the name of the link
    assert hook.cwd_prefix(str(link), "ls") == "cd " + shlex.quote(os.path.realpath(install)) + " && "
    # and a link in the other direction: a name that looks like the install folder but is not
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fake = tmp_path / "x" / ".config"
    fake.mkdir(parents=True)
    (fake / "bertie").symlink_to(elsewhere)
    assert hook.cwd_prefix(str(fake / "bertie"), "ls") != ""             # the name counts too: it is the safe side
    assert hook.cwd_prefix(str(elsewhere), "ls") == ""


@pytest.mark.parametrize("cwd", ["work/proj", "./x", "~", "$HOME/proj", "proj\0x", "/x/\0"])
def test_r1_a_folder_that_is_relative_or_cannot_be_resolved_is_read(cwd):
    assert hook.cwd_prefix(cwd, "ls") == "cd " + shlex.quote(cwd) + " && "


def test_r1_no_folder_is_no_prefix():
    assert hook.cwd_prefix("", "ls") == ""


@pytest.mark.parametrize("cwd", [5, ["/x"], {"a": 1}, b"/x", False])
def test_r1_a_folder_that_is_not_text_is_an_error(cwd):
    with pytest.raises(ValueError):
        hook.cwd_prefix(cwd, "ls")


def test_r1_the_kimi_folder_from_the_environment_counts(monkeypatch, tmp_path):
    kimi_dir = tmp_path / "kimi data"
    kimi_dir.mkdir()
    assert hook.cwd_prefix(str(kimi_dir), "ls") == ""
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_dir))
    assert hook.cwd_prefix(str(kimi_dir), "ls") != ""
    assert hook.cwd_prefix(str(kimi_dir / "sub"), "ls") == ""
    assert hook.cwd_prefix(str(kimi_dir / "sub"), "ls ..") != ""


def test_r1_the_round_one_bypass_cases_still_hold_for_every_agent():
    for key in AGENTS:
        for command, cwd in (("ln -sf /usr/bin/true .venv/bin/python", "/srv/me/.config/bertie"),
                             ("mv hook.sh h", "/srv/me/.config/bertie"),
                             ("sed -i s/a/b/ common.py", "/srv/me/.config/bertie"),
                             ("./??????????? send r1", "/srv/me/.local/bin"),
                             ("rm -f hook.sh", "/srv/me/.config/bertie"),
                             ("cp x kimi.py", "/srv/me/.config/bertie/agents")):
            assert ask(key, call_for(key, command, cwd))[0] is False, (key, command, cwd)


@pytest.mark.parametrize("key", AGENTS)
def test_r1_a_plain_send_in_an_ordinary_folder_still_gets_the_answer_it_always_did(key):
    quiet, _ = ask(key, call_for(key, "bertie-mail send r1", "/x/src/bertie"))
    if key == "cursor":
        assert quiet is False                                    # Cursor asks about every gated action itself
    else:
        assert quiet is True                                     # the ask rule / the Mac window take it from here


# ================================================================ R9: Kimi's folder is tool_input.cwd, else the session's

def test_r9_kimi_reads_the_session_folder_when_tool_input_has_none():
    command = "./??????????? send r1"
    assert ask("kimi", kimi_call(command, top="/srv/me/.local/bin"))[0] is False
    assert ask("kimi", kimi_call(command, top="/srv/work"))[0] is True


def test_r9_tool_input_cwd_wins_over_the_session_folder():
    command = "./??????????? send r1"
    assert ask("kimi", kimi_call(command, cwd="/srv/work", top="/srv/me/.local/bin"))[0] is True
    assert ask("kimi", kimi_call(command, cwd="/srv/me/.local/bin", top="/srv/work"))[0] is False


def test_r9_an_empty_tool_input_cwd_falls_back_to_the_session_folder():
    from agents import kimi
    data = kimi_call("ls", cwd="", top="/srv/me/.config/bertie")
    assert kimi.hook_scan_text(data, "ls") == "cd /srv/me/.config/bertie && ls"
    assert kimi.hook_scan_text(kimi_call("ls", cwd="", top=""), "ls") is None
    assert kimi.hook_scan_text(kimi_call("ls"), "ls") is None


@pytest.mark.parametrize("data", [kimi_call("ls", top=5), kimi_call("ls", top=["/x"]), kimi_call("ls", cwd=5, top="/x")])
def test_r9_a_folder_that_is_not_text_is_denied_as_before(data):
    quiet, reason = ask("kimi", data)
    assert not quiet and "could not read" in reason


def test_r9_a_good_tool_input_cwd_hides_a_bad_session_folder_as_kimi_itself_would():
    assert ask("kimi", kimi_call("ls", cwd="/x", top=5)) == (True, None)


# ================================================================ R13a: prose fields of a Codex call are not read

@pytest.mark.parametrize("field", ["description", "justification"])
def test_r13a_a_prose_field_that_talks_about_the_keychain_is_not_a_reason(field):
    call = codex_call("git status", **{field: "check status before editing the keyring module"})
    assert ask("codex", call) == (True, None)


@pytest.mark.parametrize("field", ["note", "reason", "args", "anything_else"])
def test_r13a_any_other_field_is_still_read(field):
    call = codex_call("git status", **{field: "import svc_mail"})
    quiet, reason = ask("codex", call)
    assert not quiet and "safety check" in reason


def test_r13a_the_command_itself_is_read_whatever_the_prose_says():
    call = codex_call("python -c 'import svc_mail'", description="harmless", justification="just looking")
    assert ask("codex", call)[0] is False
    call = codex_call("echo hi; bertie-mail send r1", description="a read")
    assert ask("codex", call)[0] is False


# ================================================================ R2+R3: an agent's files by the shape of their path

R23_MUST_CATCH = [
    "rm ~/.cu*/hooks.json",
    "mv ~/.cursor/hooks.jso? /tmp/",
    "rm -rf ~/.cursor",
    "cd ~ && mv .cursor .c2",
    "rm -rf ~/.codex",
    "mv ~/.codex/rul* /tmp",
    "rm -rf ~/.kimi-code",
    "mv ~/.gemini/config /tmp/g",
    "rm ~/.c?dex/hooks.json",
    "rm ~/.codex/hooks.*",
    "cd ~/.codex && rm -r r*",
    "mv ~/.agents/skills /tmp/s",
]

R23_MORE_CATCH = [
    "rm ~/.{cursor,codex}/hooks.json",                    # braces
    "rm ~/.cursor/hooks.{json,bak}",
    "rm ~/.c[u]rsor/hooks.json",                          # brackets
    "rm ~/.cursor/hooks.js[o]n",
    "rm '~/.cursor/hooks.json'",                          # quotes
    'rm "$HOME/.cursor/hooks.json"',
    "rm ~/.*/hooks.json",                                 # a wildcard that could be any hidden folder
    "rm -rf ~/.gemini/*",
    "rm -rf ~/.agents",
    "echo x > ~/.cursor/hooks.json",
    "tee ~/.codex/config.toml",
    "mv ~/.gemini/antigravity-cli /tmp/a",
    "rm ~/.gemini/antigravity-cli/settings.json",
    "rm ~/.kimi-code/config.toml",
    "rm -rf ~/.agents/skills/bertie",
    "rm -rf ~/.gemini/config/../config",                  # climbing back in
    "cd ~/.cursor/sub && rm ../hooks.json",
    "git -C ~/.codex checkout config.toml",
    "rm ~/.codex/hooks.json; ls",                         # not the only command
    "ls && rm ~/.codex/hooks.json",
    "echo hi | tee ~/.cursor/hooks.json",
    "sudo rm -rf ~/.codex",
    "env X=1 mv ~/.gemini/config /tmp/g",
]

R23_CD_PAIRING = [
    "cd ~/.cursor && sed -i x hooks.json",
    "cd ~/.cursor; sed -i x hooks.json",
    "cd ~/.cursor\nrm hooks.json",
    "pushd ~/.codex && rm config.toml",
    "cd ~/.codex && rm -r rules",
    "cd ~/.gemini && mv config x",
    "cd ~/.gemini; cd config; rm hooks.json",
    "cd ~/.cursor && rm -rf .",
    "cd ~ && cd .gemini/config && rm hooks.json",
    "cd /h/.gemini/antigravity-cli && sed -i x settings.json",
]

R23_MUST_NOT_FLAG = [
    "cat ~/.cursorrc",                                    # another file that starts the same
    "cat ~/.cursor-backup/hooks.json",
    "vi ~/.codex/notes.txt",
    "ls ~/.agents/skills/other",
    "git commit -m hooks.json",
    "cat ~/.gemini/GEMINI.md",
    "vi my.codex/rulesfile",
    "ls ~/.cursor",                                       # only looking
    "ls -la ~/.codex",
    "cat package.json; ls rules",
    "cargo build; cat Cargo.toml config.toml",
    "vi /x/p/.gemini/settings.json",                      # a project's own file, not the Antigravity one
    "rm -rf node_modules/.cache",
    "echo .*",
    "ls ~/.*",
    "cd ~/.cursor",
    "ls ~/.gemini/config/skills",
    "grep -r foo ~/.codex",
    "cd ~/.codex && ls",
    "mkdir -p ~/.cursor",                                 # making it is not removing it
    "touch ~/.codex",
    "cd /x/proj && rm hooks.json",
    "rm /x/proj/hooks.json",
    "rm /x/proj/.cursorignore",
]


@pytest.mark.parametrize("command", R23_MUST_CATCH + R23_MORE_CATCH + R23_CD_PAIRING)
@pytest.mark.parametrize("key", AGENTS)
def test_r23_a_command_that_reaches_an_agent_hook_file_is_caught(key, command):
    quiet, reason = ask(key, call_for(key, command, "/x/work"))
    assert not quiet and "safety hook is set up" in reason, (key, command, reason)


@pytest.mark.parametrize("command", ["ls src/*/*", "cp -r src/*/* dist/", "echo .*", "ls ~/.*", "ls ~/.*/*"])
def test_r23_a_wildcard_is_not_a_hidden_folder(command):
    """In the shell a leading * never matches a name that starts with a dot, so src/*/* is not .cursor/hooks.json."""
    assert hook.agent_config_hits(hook._normalise_lines(command)) == []


@pytest.mark.parametrize("command", R23_MUST_NOT_FLAG)
@pytest.mark.parametrize("key", AGENTS)
def test_r23_an_ordinary_command_is_not_mistaken_for_one(key, command):
    assert ask(key, call_for(key, command, "/x/work")) == (True, None), (key, command)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command,cwd", [
    ("cat package.json; ls rules", "/h/.codex/worktrees/abc/proj"),       # a Codex worktree under ~/.codex
    ("cargo build; cat Cargo.toml config.toml", "/h/.codex/worktrees/abc/proj"),
    ("ls rules", "/h/.codex/worktrees/abc/proj"),
    ("vi config.toml", "/h/.codex/worktrees/abc/proj"),
    ("cat hooks.json", "/h/.cursor/projects/x"),
    ("ls rules; ls config", "/x/work/.gemini/foo"),
])
def test_r23_a_codex_worktree_and_other_folders_deeper_inside_an_agent_folder_are_left_alone(key, command, cwd):
    assert ask(key, call_for(key, command, cwd)) == (True, None), (key, command, cwd)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command,cwd", [
    ("sed -i s/a/b/ hooks.json", "/h/.cursor"),                           # the folder the command runs in counts
    ("rm hooks.json", "/h/.codex"),
    ("rm -r rules", "/h/.codex"),
    ("mv config.toml x", "/h/.codex"),
    ("rm config/hooks.json", "/h/.gemini"),
    ("rm hooks.json", "/h/.gemini/config"),
    ("rm settings.json", "/h/.gemini/antigravity-cli"),
    ("rm config.toml", "/h/.kimi-code"),
    ("mv bertie x", "/h/.agents/skills"),
    ("mv config x", "/h/.gemini"),
    ("mv skills x", "/h/.agents"),
])
def test_r23_the_folder_a_command_runs_in_pairs_with_a_bare_file_name(key, command, cwd):
    quiet, reason = ask(key, call_for(key, command, cwd))
    assert not quiet and "safety hook is set up" in reason, (key, command, cwd)


def test_r23_the_reason_names_the_file_or_folder_and_says_what_it_is():
    reason = hook.decide_agent_command("rm ~/.cursor/hooks.json")
    assert "Cursor's hooks file" in reason and "safety hook is set up" in reason
    reason = hook.decide_agent_command("rm -rf ~/.cursor")
    assert "moves, removes or changes ~/.cursor" in reason
    reason = hook.decide_agent_command("mv ~/.agents/skills /tmp/s")
    assert "~/.agents/skills" in reason


def test_r23_a_reason_is_given_once_however_many_times_the_path_is_named():
    reason = hook.decide_agent_command("rm ~/.cursor/hooks.json ~/.cursor/hooks.json")
    assert reason.count("Cursor's hooks file") == 1


# ---- text going into a file: only a command that does the damage counts, not words about it

R23_FILE_NOT_FLAGGED = [
    "Config lives in .codex; see the rules section below.",
    "Cursor reads .cursor/rules; hooks.json is separate",
    "Add .codex to your .gitignore\n## Lint rules",
    ".codex\n.cursor/\n.gemini\n.kimi-code",
    "/x/p/.gemini/settings.json",
    "# Remove .codex when you are done\n# Then see rules and config.toml",
    "Do not rm the .codex folder by hand.",
    "The hooks file is hooks.json; the folder is .cursor.",
    "ls ~/.cursor && cat ~/.codex/notes.txt",
]

R23_FILE_FLAGGED = [
    "rm ~/.codex/hooks.json",
    "#!/bin/sh\nrm -rf ~/.cursor",
    "cd ~/.codex\nrm config.toml",
    "/h/.cursor/hooks.json",                              # a whole path to the hooks file is a reason on its own
    "mv ~/.agents/skills /tmp/s",
    "echo done\nmv ~/.gemini/config /tmp/g",
    "rm ~/.codex/hooks.json  # tidy up",
]


@pytest.mark.parametrize("text", R23_FILE_NOT_FLAGGED)
def test_r23_prose_and_notes_in_a_file_are_not_flagged(text):
    assert hook.decide_agent_file(text) is None, text
    write = {"hook_event_name": "preToolUse", "tool_name": "Write",
             "tool_input": {"file_path": "/x/work/README.md", "content": text}}
    assert ask("cursor", write) == (True, None)


@pytest.mark.parametrize("text", R23_FILE_FLAGGED)
def test_r23_text_that_does_the_damage_is_flagged_in_a_file(text):
    reason = hook.decide_agent_file(text)
    assert reason and "in text being written to a file" in reason, text
    write = {"hook_event_name": "preToolUse", "tool_name": "Write",
             "tool_input": {"file_path": "/x/work/run.sh", "content": text}}
    quiet, why = ask("cursor", write)
    assert not quiet and "safety hook is set up" in why


def test_r23_a_project_gemini_settings_file_can_be_written():
    write = {"hook_event_name": "preToolUse", "tool_name": "Write",
             "tool_input": {"file_path": "/x/p/.gemini/settings.json", "content": "{}"}}
    assert ask("cursor", write) == (True, None)
    hit = {"hook_event_name": "preToolUse", "tool_name": "Write",
           "tool_input": {"file_path": "/h/.gemini/antigravity-cli/settings.json", "content": "{}"}}
    quiet, reason = ask("cursor", hit)
    assert not quiet and "Antigravity's settings" in reason


def test_r23_a_file_path_that_names_an_agent_file_is_still_refused_for_write_and_delete():
    for tool in ("Write", "Delete"):
        call = {"hook_event_name": "preToolUse", "tool_name": tool, "tool_input": {"file_path": "/h/.cursor/hooks.json"}}
        quiet, reason = ask("cursor", call)
        assert not quiet and "Cursor's hooks file" in reason


# ---- the Kimi settings folder named by KIMI_CODE_HOME

def test_r23_the_kimi_folder_from_the_environment_is_an_agent_folder(monkeypatch):
    for command in ("rm /x/kimi-data/config.toml", "rm -rf /x/kimi-data", "mv /x/kimi-data /tmp/k",
                    "cd /x/kimi-data && rm config.toml", "rm /x/kimi-data/conf*.toml"):
        assert hook.decide_agent_command(command) is None, command            # not set yet: just a folder
    monkeypatch.setenv("KIMI_CODE_HOME", "/x/kimi-data")
    for command in ("rm /x/kimi-data/config.toml", "rm -rf /x/kimi-data", "mv /x/kimi-data /tmp/k",
                    "cd /x/kimi-data && rm config.toml", "rm /x/kimi-data/conf*.toml", "rm /x/kimi-data/config.toml/"):
        reason = hook.decide_agent_command(command)
        assert reason and "safety hook is set up" in reason, command
    for command in ("ls /x/kimi-data", "cat /x/kimi-data/notes.txt", "rm /x/kimi-data/other.toml", "rm /x/kimi-data2/config.toml"):
        assert hook.decide_agent_command(command) is None, command


def test_r23_the_kimi_folder_variable_itself_is_always_read():
    for command in ("rm $KIMI_CODE_HOME/config.toml", "rm ${KIMI_CODE_HOME}/config.toml", 'rm "$KIMI_CODE_HOME/config.toml"'):
        assert hook.decide_agent_command(command), command


def test_r23_a_kimi_folder_that_is_a_link_counts_where_it_really_is(monkeypatch, tmp_path):
    real = tmp_path / "real-kimi"
    real.mkdir()
    link = tmp_path / "kimi-link"
    link.symlink_to(real)
    monkeypatch.setenv("KIMI_CODE_HOME", str(link))
    for target in (link, real):
        assert hook.decide_agent_command(f"rm {target}/config.toml"), target


@pytest.mark.parametrize("value", ["", "   ", "relative/kimi", "~/kimi"])
def test_r23_a_kimi_folder_that_is_not_a_full_path_adds_nothing(monkeypatch, value):
    monkeypatch.setenv("KIMI_CODE_HOME", value)
    assert hook.decide_agent_command("rm kimi/config.toml") is None


# ---- it stays quick and does not fall over on odd or huge text

def test_r23_a_very_large_brace_word_does_not_hang_or_raise():
    command = "echo " + "{a,b}" * 40
    hook.agent_config_hits(hook._normalise_lines(command))                   # must simply return
    hook.decide_agent_command(command)


def test_r23_many_changes_of_folder_stay_quick():
    command = " && ".join(["cd sub"] * 800) + " && rm hooks.json"
    hook.decide_agent_command(command)


def test_r23_a_long_text_with_nothing_in_it_is_quick_and_clean():
    text = ("This project keeps its notes next to the code. Nothing here is special.\n" * 20000)
    import time
    start = time.monotonic()
    assert hook.decide_agent_file(text) is None
    assert time.monotonic() - start < 20


def test_r23_the_command_check_for_claude_code_is_not_the_agent_check():
    """hook.decide (the Claude Code path) does not know about agent files at all."""
    assert hook.decide("rm ~/.cursor/hooks.json") is None
    assert hook.decide("rm -rf ~/.codex") is None


# ---- the line breaks that end one command and start the next are kept

def test_r23_normalise_lines_keeps_the_breaks_and_drops_quotes_and_backslashes():
    assert hook._normalise_lines("rm 'a b'\n  cd   x") == "rm a b\n cd x"
    assert hook._normalise_lines("rm \\\nfoo") == "rm foo"
    assert hook._normalise_lines("rm $'a\\tb'") == "rm atb"


# ================================================================ R5: Kimi's block goes before the first table

@pytest.mark.parametrize("name", sorted(ORIGINALS))
def test_r5_the_block_goes_before_the_first_table_or_at_the_end_when_there_is_none(home, ctx, name):
    put_config(home, ORIGINALS[name])
    kimi.install(ctx, KIT)
    before, after = check_block(raw(home), ctx)
    want_before, want_after, want_lead = expected_place(name)
    assert (before, after) == (want_before, want_after)
    assert manifest(ctx)["agents"]["kimi"]["toml"][CONFIG]["lead"] == want_lead
    if tomllib:                                               # and the file still means what it meant
        assert tomllib.loads(before + after) == tomllib.loads(ORIGINALS[name])


@pytest.mark.parametrize("name", sorted(ORIGINALS))
def test_r5_a_second_install_changes_nothing_and_the_uninstall_gives_back_the_original(home, ctx, name):
    put_config(home, ORIGINALS[name])
    kimi.install(ctx, KIT)
    first = raw(home)
    msgs = kimi.install(ctx, KIT)
    assert raw(home) == first and any("already in" in m for m in msgs)
    assert len(backups(home / ".kimi-code")) == 1             # no copy of an unchanged file
    kimi.uninstall(ctx)
    assert raw(home) == ORIGINALS[name]


def test_r5_the_end_line_says_where_the_users_own_settings_go():
    assert END_LINE == "# <<< bertie: add your own settings above this block <<<"
    assert kimi.END == END_LINE and "add your own settings above this block" in kimi.block_lines("x")[-1]


def test_r5_top_level_settings_stay_top_level(home, ctx):
    put_config(home, 'model = "k"\nquiet = true\n\n[ui]\ntheme = "dark"\n')
    kimi.install(ctx, KIT)
    before, after = check_block(raw(home), ctx)
    assert before == 'model = "k"\nquiet = true\n\n' and after == '[ui]\ntheme = "dark"\n'
    if tomllib:
        full = tomllib.loads(raw(home))
        assert full["model"] == "k" and full["quiet"] is True and full["ui"] == {"theme": "dark"}
        assert all(set(h) <= set(kimi.HOOK_KEYS) for h in full["hooks"])


def test_r5_a_setting_added_to_the_end_later_is_the_users_own_not_part_of_our_hook(home, ctx):
    put_config(home, ORIGINALS["plain"])
    kimi.install(ctx, KIT)
    config(home).write_bytes((raw(home) + 'extra = 1\n').encode())        # what Kimi or the user does next
    if tomllib:
        full = tomllib.loads(raw(home))
        assert full["ui"]["extra"] == 1                                  # it lands in their table
        assert all(set(h) <= set(kimi.HOOK_KEYS) for h in full["hooks"])  # and our hook keeps only its four keys
    kimi.install(ctx, KIT)                                               # a reinstall has nothing to complain about
    kimi.uninstall(ctx)
    assert raw(home) == ORIGINALS["plain"] + 'extra = 1\n'


def test_r5_a_file_with_no_table_still_gets_the_block_at_the_end_and_a_later_setting_is_refused(home, ctx):
    put_config(home, 'model = "k"\n')
    kimi.install(ctx, KIT)
    assert raw(home).startswith('model = "k"\n' + BEGIN_LINE) and raw(home).endswith(END_LINE + "\n")
    with_key = raw(home) + 'extra = 1\n'
    put_config(home, with_key)
    with pytest.raises(kit_setup.SetupError, match="right after Bertie's block.*Move that line above"):
        kimi.install(ctx, KIT)
    assert raw(home) == with_key


def test_r5_comment_lines_stuck_to_the_first_table_stay_with_it(home, ctx):
    original = 'a = 1\n\n# the ui settings\n[ui]\nx = 1\n'
    put_config(home, original)
    kimi.install(ctx, KIT)
    before, after = check_block(raw(home), ctx)
    assert before == "a = 1\n\n" and after == "# the ui settings\n[ui]\nx = 1\n"
    put_config(home, 'a = 1\n# about a\n\n[ui]\nx = 1\n')            # a blank line in between: not stuck
    kimi.install(ctx, KIT)
    assert check_block(raw(home), ctx) == ('a = 1\n# about a\n\n', '[ui]\nx = 1\n')


def test_r5_a_line_that_looks_like_a_table_inside_a_value_is_not_one(home, ctx):
    original = 'list = [\n  ["a"],\n  ["b"],\n]\nnote = """\n[fake]\n"""\nlit = \'\'\'\n[fake2]\n\'\'\'\ninline = { a = [1, 2] }\n[real]\nx = 1\n'
    put_config(home, original)
    kimi.install(ctx, KIT)
    before, after = check_block(raw(home), ctx)
    assert after == "[real]\nx = 1\n" and before == original[:-len("[real]\nx = 1\n")]
    if tomllib:
        assert tomllib.loads(before + after) == tomllib.loads(original)


@pytest.mark.parametrize("original", ['text = "never ends\n[ui]\nx = 1\n', "text = \'\'\'\n[ui]\nx = 1\n", 'text = """\n[ui]\n'])
def test_r5_a_file_the_reader_cannot_follow_gets_the_block_at_the_end(home, ctx, original):
    put_config(home, original)
    kimi.install(ctx, KIT)
    before, block, after = split_block(raw(home))                 # not valid TOML to begin with, so no parser here
    assert (before, after) == (original + ("" if original.endswith("\n") else "\n"), "")
    assert block.startswith(BEGIN_LINE) and block.endswith(END_LINE + "\n")
    kimi.uninstall(ctx)
    assert raw(home) == original


def test_r5_a_byte_order_mark_stays_first(home, ctx):
    original = chr(0xFEFF) + '[ui]\nx = 1\n'
    put_config(home, original)
    kimi.install(ctx, KIT)
    text = raw(home)
    assert text.startswith(chr(0xFEFF) + BEGIN_LINE) and text.endswith("[ui]\nx = 1\n") and chr(0xFEFF) not in text[1:]
    kimi.uninstall(ctx)
    assert raw(home) == original


def test_r5_line_endings_of_the_file_are_kept(home, ctx):
    original = 'a = 1\r\n\r\n[ui]\r\nx = 1\r\n'
    put_config(home, original)
    kimi.install(ctx, KIT)
    text = raw(home)
    assert text.count("\r\n") == text.count("\n")                # no bare line feed anywhere
    assert text.startswith('a = 1\r\n\r\n' + BEGIN_LINE + "\r\n") and text.endswith(END_LINE + "\r\n[ui]\r\nx = 1\r\n")
    kimi.uninstall(ctx)
    assert raw(home) == original


# ---- a block an earlier build wrote (at the end, with the old end line)

def _old_style(original, ctx, lead):
    """The file as an early build left it: the same block, the old end line, at the end of the file."""
    put_config(home_of(ctx), original)
    kimi.install(ctx, KIT)
    text = raw(home_of(ctx))
    _, block, after = split_block(text)
    block = block.replace(END_LINE, OLD_END_LINE)
    old = original + lead + block
    put_config(home_of(ctx), old)
    m = manifest(ctx)
    m["agents"]["kimi"]["toml"][CONFIG]["lead"] = lead
    kit_setup.save_manifest(ctx, m)
    return old


def home_of(ctx):
    return ctx.home


@pytest.mark.parametrize("original,lead", [('a = 1\n[ui]\nx = 1\n', ""), ('a = 1\n[ui]\nx = 1', "\n"), ('a = 1\n', "")])
def test_r5_an_early_block_is_still_found_and_removed_by_the_uninstall(home, ctx, original, lead):
    _old_style(original, ctx, lead)
    assert OLD_END_LINE in raw(home)
    msgs = kimi.uninstall(ctx)
    assert raw(home) == original and any("removed the ask rules and safety hook" in m for m in msgs)


@pytest.mark.parametrize("original,lead", [('a = 1\n[ui]\nx = 1\n', ""), ('a = 1\n[ui]\nx = 1', "\n"), ('a = 1\n', "")])
def test_r5_a_reinstall_moves_an_early_block_to_where_it_belongs_and_uninstall_is_still_exact(home, ctx, original, lead):
    _old_style(original, ctx, lead)
    kimi.install(ctx, KIT)
    text = raw(home)
    assert OLD_END_LINE not in text.replace(END_LINE, "") and text.count(END_LINE) == 1
    want_before, want_after, want_lead = _place(original)
    assert check_block(text, ctx) == (want_before, want_after)
    assert manifest(ctx)["agents"]["kimi"]["toml"][CONFIG]["lead"] == want_lead
    kimi.install(ctx, KIT)
    assert raw(home) == text                                              # and now it stays put
    kimi.uninstall(ctx)
    assert raw(home) == original


def _place(original):
    """(before, after, lead) for a sample written out in the test above: block before `[ui]`, else at the end."""
    if "[ui]" in original:
        at = original.index("[ui]")
        return original[:at], original[at:], ""
    return original, "", ""


def test_r5_the_hand_hint_names_the_block_by_its_start_of_line_so_it_fits_either_end_line():
    assert kimi.HAND_HINT == 'delete the block from the line starting "# >>> bertie" to the line starting "# <<< bertie"'
    assert kimi.HAND_HINT.split("to the line starting ")[1].strip('"') + " " in OLD_END_LINE + " "
    assert OLD_END_LINE.startswith("# <<< bertie") and END_LINE.startswith("# <<< bertie")


# ---- the reader that finds the first table, against a real TOML reader

_STRING_BODIES = ["a", "[x]", "# y", "a b", "back\\\\slash", 'quote\\"d']
_COMMENT_BODIES = ["[ui]", "about", " [[x]]", ""]


def _piece(rng):
    """One top-level entry as text that is valid TOML, with lines that try to look like tables or comments."""
    kind = rng.randrange(9)
    key = "k" + str(rng.randrange(10 ** 6))
    if kind == 0:
        return key + ' = "' + rng.choice(_STRING_BODIES) + '"\n'
    if kind == 1:
        return key + " = " + str(rng.randrange(100)) + "\n"
    if kind == 2:
        return key + ' = [\n  ["a", "b"],\n  [ "c" ], # [d]\n  [],\n]\n'
    if kind == 3:
        return key + ' = """\n[table]\n# not a comment\n  [[nor]]\n\\"""[more]\n"""\n'
    if kind == 4:
        return key + " = \'\'\'\n[table]\n\'\'\'\n"
    if kind == 5:
        return key + ' = { a = [1, 2], b = { c = "[" } }\n'
    if kind == 6:
        return "# " + rng.choice(_COMMENT_BODIES) + "\n"
    if kind == 7:
        return rng.choice(["\n", "   \n", "\t\n"])
    return '"' + key + ' [x]" = \'[q]\'\n'


def _header(rng):
    return rng.choice(["[t%d]\n", "[[t%d]]\n", "  [t%d]  # c\n", "[ t%d . sub ]\n", "[[ t%d ]]\n"]) % rng.randrange(100)


@pytest.mark.parametrize("seed", range(250))
def test_r5_the_first_table_is_found_the_way_a_real_toml_reader_would(seed):
    rng = random.Random(seed)
    head = "".join(_piece(rng) for _ in range(rng.randrange(0, 7)))
    stuck = "".join("# " + rng.choice(["note", "[x]", ""]) + "\n" for _ in range(rng.randrange(0, 3)))
    body = "".join(_piece(rng) for _ in range(rng.randrange(0, 4)))
    tail = _header(rng) + "x = 1\n" + "".join(_piece(rng) for _ in range(rng.randrange(0, 4)))
    if rng.randrange(4) == 0:
        doc = head + body                                            # no table at all
        assert kimi._first_table(doc) is None, doc
        return
    doc = head + body + stuck + tail
    at = kimi._first_table(doc)
    lines = doc.split("\n")
    starts = [sum(len(l) + 1 for l in lines[:i]) for i in range(len(lines))]
    k = starts.index(len(head + body + stuck))                       # the header's line, then up over the comment lines stuck to it
    while k > 0 and lines[k - 1].lstrip().startswith("#"):
        k -= 1
    assert at == starts[k], (at, starts[k], doc)                    # never into the middle of a value
    if tomllib:
        assert tomllib.loads(doc) is not None
        placed = doc[:at] + "# marker\n[[hooks]]\nevent = 'PreToolUse'\n" + doc[at:]
        full, plain = tomllib.loads(placed), tomllib.loads(doc)
        assert full.pop("hooks") == [{"event": "PreToolUse"}] and full == plain


# ================================================================ R4: a recorded file that became a link

OTHER_INSTALL = "sh /srv/other/.config/bertie/hook.sh --agent cursor"


def _cursor_installed(home, ctx):
    put(home, HOOKS, json.dumps({"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}) + "\n")
    cursor.install(ctx, KIT)
    return home / HOOKS


def _replace_with_link(home, target):
    p = home / HOOKS
    p.unlink()
    p.symlink_to(target)
    return p


def test_r4_a_link_that_leads_nowhere_no_longer_stops_the_uninstall(home, ctx, tmp_path):
    _cursor_installed(home, ctx)
    link = _replace_with_link(home, tmp_path / "gone.json")
    assert agents.uninstall_problems(ctx) == []
    lines = cursor.uninstall(ctx)
    assert link.is_symlink() and not (tmp_path / "gone.json").exists()            # never followed, never deleted
    assert not (home / ".agents" / "skills" / "bertie").exists()                  # and the rest of the uninstall ran
    assert "cursor" not in kit_setup.load_manifest(ctx).get("agents", {})
    assert not any("could not remove everything" in l for l in lines)


@pytest.mark.parametrize("content", [
    '{"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}\n',           # a dotfiles copy without our entry
    "",                                                                            # an empty file
    "{broken",                                                                     # not JSON, and no hook command in it
    '{"hooks": {"beforeShellExecution": [{"command": "' + OTHER_INSTALL + '"}]}}\n',   # another install's hook, not ours
], ids=["other hooks", "empty", "broken", "another install"])
def test_r4_a_link_whose_target_no_longer_runs_our_hook_is_dropped_and_the_target_untouched(home, ctx, tmp_path, content):
    _cursor_installed(home, ctx)
    target = tmp_path / "dotfiles.json"
    target.write_text(content)
    link = _replace_with_link(home, target)
    assert agents.uninstall_problems(ctx) == []
    cursor.uninstall(ctx)
    assert link.is_symlink() and os.readlink(link) == str(target) and target.read_text() == content
    assert "cursor" not in kit_setup.load_manifest(ctx).get("agents", {})


def test_r4_a_link_to_a_folder_or_a_loop_is_dropped_too(home, ctx, tmp_path):
    _cursor_installed(home, ctx)
    (tmp_path / "somewhere").mkdir()
    link = _replace_with_link(home, tmp_path / "somewhere")
    assert agents.uninstall_problems(ctx) == []
    cursor.uninstall(ctx)
    assert link.is_symlink() and (tmp_path / "somewhere").is_dir()
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    assert agents.link_problem(ctx, "cursor", tmp_path / "a") is None


def test_r4_a_link_to_a_file_that_still_holds_our_hook_is_refused_with_the_real_target_named(home, ctx, tmp_path):
    _cursor_installed(home, ctx)
    target = tmp_path / "dotfiles" / "hooks.json"
    target.parent.mkdir()
    (home / HOOKS).rename(target)
    link = _replace_with_link_to_existing(home, target)
    snapshot = target.read_bytes()
    problems = agents.uninstall_problems(ctx)
    assert len(problems) == 1
    assert str(target) in problems[0] and "remove the entry there, or replace the link with a plain file" in problems[0]
    lines = cursor.uninstall(ctx)
    assert target.read_bytes() == snapshot and link.is_symlink()                    # nothing edited through the link
    assert "cursor" in kit_setup.load_manifest(ctx)["agents"]                       # the record stays
    assert any("could not remove everything" in l and str(target) in l for l in lines)
    assert not (home / ".agents" / "skills" / "bertie").exists()                    # the skill still went
    # once the person replaces the link with a plain file the uninstall finishes
    link.unlink()
    target.rename(link)
    cursor.uninstall(ctx)
    assert json.loads(link.read_text()) == {"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}
    assert "cursor" not in kit_setup.load_manifest(ctx).get("agents", {})


def _replace_with_link_to_existing(home, target):
    p = home / HOOKS
    p.symlink_to(target)
    return p


def test_r4_the_command_is_found_in_its_json_escaped_form_too(home, ctx, tmp_path):
    _cursor_installed(home, ctx)
    target = tmp_path / "t.json"
    target.write_text(json.dumps({"x": agents.hook_command(ctx, "cursor")}, ensure_ascii=True))
    _replace_with_link(home, target)
    assert len(agents.uninstall_problems(ctx)) == 1


@needs_user
def test_r4_a_link_to_a_file_that_cannot_be_read_is_refused_because_nobody_can_tell(home, ctx, tmp_path):
    _cursor_installed(home, ctx)
    target = tmp_path / "locked.json"
    target.write_text("{}")
    target.chmod(0)
    try:
        _replace_with_link(home, target)
        problems = agents.uninstall_problems(ctx)
        assert len(problems) == 1 and "could not be read" in problems[0] and "replace the link with a plain file" in problems[0]
    finally:
        target.chmod(0o644)


def test_r4_antigravity_and_the_install_still_refuse_links(home, ctx, tmp_path):
    """Installing never edits through a link: only the uninstall of a record whose link is harmless changed."""
    real = tmp_path / "dotfiles" / "hooks.json"
    real.parent.mkdir()
    real.write_text("{}\n")
    (home / ".gemini" / "config").mkdir(parents=True)
    (home / ".gemini" / "config" / "hooks.json").symlink_to(real)
    with pytest.raises(kit_setup.SetupError, match="link"):
        antigravity.install(ctx, KIT)
    with pytest.raises(kit_setup.SetupError, match="link"):
        agents.json_merge(ctx, "antigravity", ".gemini/config/hooks.json", [agents.Require(["x"], 1)])


def test_r4_link_problem_is_none_for_a_plain_file_or_a_missing_one(home, ctx):
    put(home, "cfg/a.json", "{}")
    assert agents.link_problem(ctx, "cursor", home / "cfg" / "a.json") is None
    assert agents.link_problem(ctx, "cursor", home / "cfg" / "missing.json") is None


def test_r4_the_whole_uninstall_goes_through_when_a_recorded_file_is_a_dangling_link(home, tmp_path, keychain, monkeypatch,
                                                                                      dialog, capsys):
    assert run_install(home, tmp_path, *ARGS, "--agent", "cursor").returncode == 0
    hooks = home / HOOKS
    hooks.unlink()
    hooks.symlink_to(tmp_path / "gone.json")
    capsys.readouterr()
    uninstall(home, keychain, monkeypatch)                                          # no "nothing was removed" stop
    out = capsys.readouterr().out
    assert "removed the install folder ~/.config/bertie" in out and "KEPT" not in out
    assert hooks.is_symlink() and not (home / ".config" / "bertie").exists()
    assert not (home / ".local" / "bin" / "bertie").exists()


# ================================================================ R11: which hook path is ours, and the backups line

def _ctx_at(home, install):
    from pathlib import Path
    return kit_setup.Ctx(Path(home), Path(install))


def test_r11_this_installs_own_hook_command_is_ours_in_any_file_format(ctx):
    for key in ("cursor", "codex", "kimi", "antigravity"):
        command = agents.hook_command(ctx, key)
        assert agents._names_our_hook(ctx, command)
        assert agents._names_our_hook(ctx, json.dumps({"command": command}))
        assert agents._names_our_hook(ctx, "command = " + kimi.toml_string(command))


def test_r11_a_home_with_spaces_and_quotes_is_recognised_in_its_quoted_and_escaped_forms(tmp_path):
    odd = tmp_path / "my home 'x' \"y\""
    c = _ctx_at(odd, odd / ".config" / "bertie")
    command = agents.hook_command(c, "cursor")
    assert "'" in command                                                         # it really is quoted
    for text in (command, json.dumps({"command": command}), "command = " + kimi.toml_string(command)):
        assert agents._names_our_hook(c, text), text


def test_r11_the_tilde_and_home_spellings_are_ours_too(ctx):
    for text in ("sh ~/.config/bertie/hook.sh --agent codex", "sh $HOME/.config/bertie/hook.sh --agent codex",
                 'sh "${HOME}/.config/bertie/hook.sh" --agent codex', "sh '~/.config/bertie/hook.sh' --agent kimi"):
        assert agents._names_our_hook(ctx, text), text


@pytest.mark.parametrize("text", [
    "sh /srv/other/.config/bertie/hook.sh --agent cursor",                       # another copy with the same folder name
    "sh /srv/bertie/hook.sh --agent kimi",
    "sh ~/work/bertie/hook.sh --agent codex",
    "sh $HOME/dev/bertie/hook.sh --agent codex",
    "bertie/hook.sh --agent cursor",
    "sh /x/bertie-old/hook.sh --agent cursor",
    "",
])
def test_r11_a_hook_in_another_folder_of_the_same_name_is_not_ours(home, text):
    real = _ctx_at(home, home / ".config" / "bertie")                             # the folder really is called bertie
    assert agents._names_our_hook(real, text) is False
    assert agents._names_our_hook(real, "sh " + str(real.install_dir / "hook.sh") + " --agent cursor") is True


def test_r11_our_path_without_an_agent_flag_is_not_a_hook_line(ctx):
    assert agents._names_our_hook(ctx, "# see " + str(ctx.install_dir / "hook.sh")) is False


def test_r11_a_home_reached_through_a_link_is_still_recognised(tmp_path):
    real = tmp_path / "real-home"
    real.mkdir()
    via = tmp_path / "linked-home"
    via.symlink_to(real)
    c = _ctx_at(real, real / ".config" / "bertie")
    other_spelling = "sh " + str(via / ".config" / "bertie" / "hook.sh") + " --agent cursor"
    assert agents._names_our_hook(c, other_spelling) is False                    # a path through a link nobody installed with
    c2 = _ctx_at(via, via / ".config" / "bertie")
    assert agents._names_our_hook(c2, "sh " + str(real / ".config" / "bertie" / "hook.sh") + " --agent cursor")


def test_r11_the_leftovers_list_names_only_files_that_run_this_installs_hook(home):
    ctx = _ctx_at(home, home / ".config" / "bertie")
    put(home, HOOKS, json.dumps({"version": 1, "hooks": {"beforeShellExecution": [{"command": OTHER_INSTALL}]}}))
    assert agents.hook_leftovers(ctx) == []                                       # someone else's copy of Bertie
    put(home, HOOKS, json.dumps({"version": 1, "hooks": {"beforeShellExecution": [
        {"command": OTHER_INSTALL}, {"command": agents.hook_command(ctx, "cursor")}]}}))
    left = agents.hook_leftovers(ctx)
    assert [(label, where) for label, where, _ in left] == [("Cursor", "~/" + HOOKS)]


def test_r11_the_backups_line_is_printed_when_the_install_folder_is_kept(home, tmp_path, keychain, monkeypatch, dialog, capsys):
    put(home, HOOKS, json.dumps({"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}) + "\n")
    put(home, ".kimi-code/config.toml", '[ui]\ntheme = "dark"\n')
    assert run_install(home, tmp_path, *ARGS, "--agent", "cursor,kimi").returncode == 0
    ctx = kit_setup.Ctx(home, home / ".config" / "bertie")
    m = kit_setup.load_manifest(ctx)
    m["agents"].pop("cursor")                                                     # its hook entry stays behind: the folder is kept
    kit_setup.save_manifest(ctx, m)
    capsys.readouterr()
    uninstall(home, keychain, monkeypatch)
    out = capsys.readouterr().out
    assert "was KEPT" in out and (home / ".config" / "bertie" / "hook.sh").is_file()
    lines = out.splitlines()
    claude = [i for i, l in enumerate(lines) if "settings.json.bertie-backup-* were left in ~/.claude" in l]
    beside = [i for i, l in enumerate(lines) if "also left beside the other agents' files" in l]
    done = [i for i, l in enumerate(lines) if l.startswith("done, apart from the steps above")]
    assert claude and beside and done and max(claude + beside) < done[0]           # said before the early finish
    assert "~/.kimi-code/config.toml" in lines[beside[0]]
    assert not any(l.startswith("done. ") for l in lines)                          # and the other ending was not printed too


def test_r11_the_ordinary_ending_is_unchanged(home, tmp_path, keychain, monkeypatch, dialog, capsys):
    assert run_install(home, tmp_path, *ARGS).returncode == 0
    capsys.readouterr()
    uninstall(home, keychain, monkeypatch)
    lines = capsys.readouterr().out.splitlines()
    assert ("done. Settings backups named settings.json.bertie-backup-* were left in ~/.claude "
            "for you to delete when you like.") in lines
    assert lines.index("removed the install folder ~/.config/bertie") < lines.index(
        "done. Settings backups named settings.json.bertie-backup-* were left in ~/.claude for you to delete when you like.")


# ================================================================ R6: Kimi's plain sends are not blocked for their paths

@pytest.mark.parametrize("command", [
    "bertie-drive upload ./.hidden/f", "bertie-mail send r1 --attach ~/.config/notes.pdf", "bertie-drive upload x/../y.pdf",
    "bertie-chat send /.x hi", "bertie-mail send r1 --body a\\ b", "bertie-drive upload ./f.pdf",
    "bertie-calendar create --title 'a/.b'", "bertie-mail send r1 && ls",
])
def test_r6_a_plain_send_with_a_dot_folder_or_a_backslash_gets_no_opinion(command):
    assert ask("kimi", kimi_call(command)) == (True, None), command
    assert kimi.rules_cover(command) is True


@pytest.mark.parametrize("command", [
    "bertie-mail send r1\nls", "bertie-mail send r1\r\nls", "bertie-mail send r1 ls", "bertie-drive upload a/b\nc",
    "bertie-mail send r1 \\\n x",                                    # a line continuation is a line break
    "cd /tmp && bertie-mail send r1", "echo hi; bertie-mail send r1", " bertie-mail send r1", "BERTIE-MAIL send r1",
    "bertie-mail --json send r1", "bertie-mail send  r1 \\\nbertie-mail send r2",
])
def test_r6_a_send_that_is_not_plain_is_still_denied_with_the_instruction(command):
    quiet, reason = ask("kimi", kimi_call(command))
    assert not quiet and "Run the Bertie command on its own" in reason, command


@pytest.mark.parametrize("command", [
    "bertie-mail send r1 && b\\ertie-approve x",                      # the backslash does not hide a tool of ours
    "bertie-mail send r1; security find-generic-password -s bertie -w",
    "bertie-mail send r1 && ~/.config/bertie/.venv/bin/python x",
    "bertie-mail send r1 && sed -i s/a/b/ ~/.cursor/hooks.json",
    "bertie-mail send r1; import\\ svc_mail",
    "bertie-mail send r1 $(cat ~/.config/bertie/.approve\\.sha256)",
])
def test_r6_a_send_with_something_else_wrong_in_it_is_denied_whatever_the_backslashes(command):
    quiet, reason = ask("kimi", kimi_call(command))
    assert not quiet and "safety check" in reason, command


# ================================================================ R10: Codex says why a send would not be asked about

HINT = "quote the argument with , = or ~ (e.g. --to 'a@example.com,b@example.com') or write the full path"


@pytest.mark.parametrize("command", [
    "bertie-mail send r1 a@example.com,b@example.com", "bertie-mail send r1 --subject=Hello",
    "bertie-drive upload ~/f.pdf", "bertie-mail send --to a@example.com,b@example.com --subject hi",
    "bertie-calendar create --title x --when=tomorrow",
])
def test_r10_codex_names_the_unquoted_character_when_that_is_the_only_problem(command):
    quiet, reason = ask("codex", codex_call(command))
    assert not quiet and HINT in reason and reason.endswith("so Codex asks you first."), command
    assert "Run the Bertie command on its own" not in reason
    assert reason.startswith("Bertie safety check: ")
    assert codex.cover_hint(command) is not None


@pytest.mark.parametrize("command", [
    "bertie-mail send --to 'a@example.com,b@example.com' --subject hi", "bertie-mail send r1 --subject \"a=b, ~/x\"",
    "bertie-drive upload /x/docs/f.pdf",
])
def test_r10_the_fix_the_message_suggests_is_left_to_the_rule(command):
    assert codex.rules_cover(command) is True
    assert ask("codex", codex_call(command)) == (True, None)
    assert codex.cover_hint(command) is None


@pytest.mark.parametrize("command", [
    "cd /tmp && bertie-mail send r1 a,b",                             # not at the start of the line: the old advice fits
    "echo hi; bertie-mail send r1 a,b",
    "bertie-mail send r1 a,b; rm ~/.cursor/hooks.json",               # something else is wrong too
    "bertie-mail send r1 a,b && ~/.config/bertie/.venv/bin/python x",
    "bertie-mail send r1 a,b ; ls",                                   # an unquoted ; is a problem the hint would not fix
    "bertie-mail send r1 a+b",                                        # + is not one of the three the message names
    "bertie-mail send r1 --subject=\"a b\"",                          # a quote touching text
    "bertie-mail send r1 $HOME",
])
def test_r10_other_problems_keep_the_old_advice(command):
    quiet, reason = ask("codex", codex_call(command))
    assert not quiet and HINT not in reason and "Run the Bertie command on its own" in reason, command
    assert codex.cover_hint(command) is None


@pytest.mark.parametrize("value", [None, 5, b"bertie-mail send r1 a,b", ["x"]])
def test_r10_the_hint_is_none_for_anything_that_is_not_text(value):
    assert codex.cover_hint(value) is None


def test_r10_when_the_rules_allow_sends_the_message_names_the_mac_window(monkeypatch):
    monkeypatch.setattr(codex, "_recorded_sends", lambda: "allow")
    reason = hook.decide_block(codex, "bertie-mail send r1 a,b")
    assert HINT in reason and reason.endswith("so the Mac window asks you first.")


def test_r10_kimi_and_the_blocked_commands_of_other_adapters_have_no_hint():
    assert getattr(kimi, "cover_hint", None) is None
    reason = hook.decide_block(kimi, "echo hi; bertie-mail send r1")
    assert "quote the argument" not in reason and "Run the Bertie command on its own" in reason


def test_r10_claude_codes_own_hook_is_unchanged():
    """hook.decide (no --agent) never goes through decide_block: nothing it says mentions the hint."""
    assert "quote the argument with ," not in (hook.decide("bertie-mail send r1 a,b") or "")


# ================================================================ R8: Antigravity's CLI folder when the note about it is gone

def _forget_cli_before(ctx):
    m = kit_setup.load_manifest(ctx)
    del m["agents"]["antigravity"]["cli_before"]
    kit_setup.save_manifest(ctx, m)


def test_r8_a_folder_our_own_skill_copy_made_is_not_taken_for_the_clis_when_the_note_is_lost(home, ctx):
    (home / ".gemini" / "config").mkdir(parents=True)                           # the App's folder only
    antigravity.install(ctx, KIT)
    assert kit_setup.load_manifest(ctx)["agents"]["antigravity"]["cli_before"] is False
    assert (home / antigravity.CLI_DIR_REL).is_dir()                            # made by our skill copy
    _forget_cli_before(ctx)
    assert antigravity._cli_wanted(ctx) is False
    antigravity.install(ctx, KIT)                                                # and a reinstall adds no CLI rules
    assert not (home / antigravity.SETTINGS_REL).exists()
    assert kit_setup.load_manifest(ctx)["agents"]["antigravity"]["cli_before"] is False


def test_r8_a_cli_folder_that_was_there_first_still_counts_when_the_note_is_lost(home, ctx):
    (home / antigravity.CLI_DIR_REL).mkdir(parents=True)                        # the CLI is in use
    antigravity.install(ctx, KIT)
    assert kit_setup.load_manifest(ctx)["agents"]["antigravity"]["cli_before"] is True
    _forget_cli_before(ctx)
    assert antigravity._cli_wanted(ctx) is True


def test_r8_with_no_record_at_all_the_folder_on_disk_decides_as_before(home, ctx):
    assert antigravity._cli_wanted(ctx) is False
    (home / antigravity.CLI_DIR_REL).mkdir(parents=True)
    assert antigravity._cli_wanted(ctx) is True
    assert "antigravity" not in kit_setup.load_manifest(ctx).get("agents", {})   # asking writes nothing down
