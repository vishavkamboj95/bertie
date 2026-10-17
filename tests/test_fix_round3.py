"""Fix round 3, from the third review of the other-agent work.

F1 following `cd` is cheap and cannot drop the folder that matters, F2 a check that runs out of
time asks (Codex, Kimi Code and Cursor's Write let a call that times out go), F3 a backstop for a
path built up at run time (only where it points at the home folder), F4 a Codex patch's added
lines are read without their leading `+`, F5 a project's own `.cursor`/`.codex`/`.gemini`/`.agents`
folder is left alone when the agent said where the command runs, F6 Antigravity's Cwd is read as
a folder, F7 a linked file holding another spelling of our hook is refused before sign-out.

What stays an accepted limit (a path that needs the shell to be run to be read) is listed at the
end of this file. The Claude Code path (hook.py with no arguments) is not changed by any of it.

HOME is a temporary folder everywhere (the autouse fixture in conftest.py)."""
import io
import json
import os
import subprocess
import sys
import time

import pytest

import agents
import hook
import kit_setup
from agents import antigravity, codex, cursor, kimi
from conftest import KIT
from test_agents_core import ctx  # noqa: F401
from test_core_fixes import HOOKS, put

SHELL_AGENTS = ("codex", "cursor", "kimi")
AGENTS = SHELL_AGENTS + ("antigravity",)
SAFE = "asking to be safe"
QUICK = 2.0                      # seconds: the cases that took minutes (or 16 s) are measured in milliseconds


# ---------------------------------------------------------------- one call, in each agent's own format

def command_call(key, command, cwd=None):
    """A shell command in the agent's format. cwd=None leaves the folder out."""
    if key == "antigravity":
        args = {"CommandLine": command}
        if cwd is not None:
            args["Cwd"] = cwd
        return {"toolCall": {"name": "run_command", "args": args}, "hookEventName": "PreToolUse"}
    if key == "cursor":
        data = {"hook_event_name": "beforeShellExecution", "command": command}
    else:
        data = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        data["cwd"] = cwd
    return data


def file_call(key, path, content):
    """Text going into a file, in the agent's format (a Codex patch adds every line with a `+`)."""
    if key == "codex":
        patch = "*** Begin Patch\n*** Add File: " + path + "\n" + "\n".join("+" + l for l in content.split("\n")) + "\n*** End Patch"
        return {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_input": {"command": patch}}
    if key == "cursor":
        return {"hook_event_name": "preToolUse", "tool_name": "Write", "tool_input": {"file_path": path, "content": content}}
    if key == "kimi":
        return {"hook_event_name": "PreToolUse", "tool_name": "Write", "tool_input": {"file_path": path, "content": content}}
    return {"toolCall": {"name": "write_to_file", "args": {"TargetFile": path, "CodeContent": content}}}


def verdict(key, payload):
    """(no opinion?, the reason or None) for one call."""
    out, code = hook.agent_answer(key, io.StringIO(json.dumps(payload)))
    assert code == 0
    if key == "cursor":
        d = json.loads(out)
        return d["permission"] == "allow", d.get("user_message")
    if key == "antigravity":
        d = json.loads(out)
        return d["decision"] == "allow", d.get("reason")
    if not out:
        return True, None
    return False, json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


def caught(key, command, cwd=None):
    """Whether the hook stops this command, and its reason."""
    quiet, reason = verdict(key, command_call(key, command, cwd))
    return not quiet, reason


def assert_caught(key, command, cwd=None):
    stopped, reason = caught(key, command, cwd)
    assert stopped and "safety check" in reason, (key, command, cwd)
    return reason


def assert_free(key, command, cwd=None):
    stopped, reason = caught(key, command, cwd)
    assert not stopped, (key, command, cwd, reason)


def timed(fn, *args, **kwargs):
    start = time.monotonic()
    out = fn(*args, **kwargs)
    return out, time.monotonic() - start


def at_home(text, home):
    return text.replace("@H", str(home))


def run_process(home, key, payload):
    """(stdout, seconds) for the real hook.py --agent <key>, as a separate process."""
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TZ": "UTC"}
    start = time.monotonic()
    done = subprocess.run([sys.executable, "-I", str(KIT / "tools" / "hook.py"), "--agent", key],
                          input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60)
    return done.stdout.strip(), time.monotonic() - start


# ================================================================ F1: cd is followed cheaply, and nothing is dropped

CD_NUMBERS = " ".join(str(i) for i in range(31))


@pytest.mark.parametrize("key", AGENTS)
def test_f1_cd_with_thirty_words_after_it_is_quick_and_the_file_after_it_is_still_caught(key):
    command = "cd " + CD_NUMBERS + "; rm ~/.cursor/hooks.json"
    (stopped, reason), seconds = timed(caught, key, command)
    assert stopped and "Cursor's hooks file" in reason
    assert seconds < QUICK


@pytest.mark.parametrize("key", SHELL_AGENTS)
def test_f1_the_real_process_answers_the_cd_thirty_case_well_inside_its_time_out(key, home):
    out, seconds = run_process(home, key, command_call(key, "cd " + CD_NUMBERS + "; rm ~/.cursor/hooks.json"))
    assert ("deny" in out or "ask" in out) and "Cursor's hooks file" in out
    assert seconds < QUICK


@pytest.mark.parametrize("command", [
    "cd 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24; rm ~/.cursor/hooks.json",     # the 24-word case
    "cd {a,b}{a,b}{a,b}{a,b}{a,b}; rm ~/.cursor/hooks.json",
    "cd {a,b}{a,b}{a,b}{a,b}{a,b}{a,b}{a,b}{a,b} x y z; rm ~/.cursor/hooks.json",
    "pushd " + CD_NUMBERS + "; rm ~/.cursor/hooks.json",
])
@pytest.mark.parametrize("key", SHELL_AGENTS)
def test_f1_the_commands_that_took_minutes_are_quick_now(key, command):
    (stopped, reason), seconds = timed(caught, key, command)
    assert stopped and seconds < QUICK, (command, reason)


@pytest.mark.parametrize("key", SHELL_AGENTS)
@pytest.mark.parametrize("command", [
    "cd a; cd b; cd c; cd d; cd e; cd f; cd g; cd h; cd ~/.cursor; rm hooks.json",
    "cd a b c d e f g h; cd ~/.cursor && rm hooks.json",
    "cd a; cd b; cd c; cd d; cd e; cd f; cd g; cd h; cd i; cd j; cd ~/.codex; vi config.toml",
    "; ".join("cd d%d" % i for i in range(300)) + "; cd ~/.cursor && rm hooks.json",
])
def test_f1_a_long_run_of_other_folders_does_not_push_out_the_one_that_matters(key, command):
    assert_caught(key, command)


def test_f1_only_the_first_argument_of_cd_is_a_folder():
    homes, known = [("~",)], {".cursor", "hooks.json"}
    states = []
    hook._follow_cd(["cd", "a", "b", "c"], "cd", states, homes, known)
    assert states == []                                       # `a` does not matter and `b`, `c` are not folders
    hook._follow_cd(["cd", "~/.cursor", "b", "c"], "cd", states, homes, known)
    assert states == [["~", ".cursor"]]
    hook._follow_cd(["cd", "-P", "--", "~/.codex"], "cd", states, homes, known | {".codex"})
    assert states == [["~", ".cursor"], ["~", ".codex"]]       # flags are skipped
    hook._follow_cd(["cd", "-"], "cd", states, homes, known)
    hook._follow_cd(["pushd"], "pushd", states, homes, known)
    hook._follow_cd(["cd", "."], "cd", states, homes, known)
    assert states == [["~", ".cursor"], ["~", ".codex"]]       # `cd -`, a bare pushd and `cd .` go nowhere new


def test_f1_cd_alone_is_the_home_folder():
    states = []
    hook._follow_cd(["cd"], "cd", states, [("~",)], set())
    assert states == [["~"]]
    for key in SHELL_AGENTS:
        assert_caught(key, "cd; rm -rf .cursor", "/x/proj")
        assert_caught(key, "cd -P && rm -rf .codex", "/x/proj")


def test_f1_new_folders_are_built_from_the_ones_before_the_command_not_the_ones_it_just_made():
    states = []
    homes, known = [("~",)], {".cursor"}
    hook._follow_cd(["cd", "~/{.cursor,x}/{a,b}"], "cd", states, homes, known)
    assert sorted(states) == [["~", ".cursor", "a"], ["~", ".cursor", "b"]]        # 2 folders, not a pile of them
    hook._follow_cd(["cd", "~/.cursor/{a,b}"], "cd", states, homes, known)
    assert sorted(states) == [["~", ".cursor", "a"], ["~", ".cursor", "b"]]        # a folder already known is kept once


def test_f1_a_folder_is_kept_only_if_it_can_matter():
    homes = [("~",), ("$home",), ("users", "me")]
    known = {".cursor", "config", "rules"}
    matters = lambda *pieces: hook._folder_matters(list(pieces), homes, known)      # noqa: E731
    assert matters("~") and matters("$home") and matters("users", "me") and matters("users") and matters()
    assert matters(".cursor") and matters("x", "config") and matters("a", ".cursor", "b")        # an agent folder name
    assert matters("$dir") and matters("a*") and matters("[ab]", "c") and matters("x?")           # could be any
    assert matters(*[".."] * 16) and not matters(*[".."] * 17)                                    # a plain climb out
    assert not matters("step1") and not matters("src", "app") and not matters("..", "step1")
    assert not matters("users", "me", "proj")


@pytest.mark.parametrize("key", SHELL_AGENTS)
def test_f1_the_same_folder_over_and_over_is_kept_once(key):
    command = "; ".join(["cd ~/.cursor"] * 500) + "; rm hooks.json"
    (stopped, reason), seconds = timed(caught, key, command)
    assert stopped and SAFE not in reason and seconds < QUICK


@pytest.mark.parametrize("key", SHELL_AGENTS)
def test_f1_more_folders_than_can_be_followed_ask_instead_of_dropping_the_newest(key):
    just_inside = "; ".join("cd ~/.cursor/d%d" % i for i in range(hook._MAX_FOLDER_STATES))
    over = "; ".join("cd ~/.cursor/d%d" % i for i in range(hook._MAX_FOLDER_STATES + 1))
    assert_free(key, just_inside)                                                       # a full list is not a reason
    assert SAFE in assert_caught(key, over)
    assert SAFE in assert_caught(key, over + "; ls")
    assert SAFE in assert_caught(key, "cd ~/.cursor/x; " + over)                       # whatever else the text holds


def test_f1_the_cap_is_64():
    assert hook._MAX_FOLDER_STATES == 64


@pytest.mark.parametrize("key", SHELL_AGENTS + ("antigravity",))
@pytest.mark.parametrize("flags", ["-P", "-L", "--", "-P --"])
def test_f1_flags_before_the_folder_are_skipped(key, flags):
    assert_caught(key, f"cd {flags} ~/.cursor && rm hooks.json")


# ---- plain text and prose: the deadline is looked at on every word, and cd in prose costs nothing

PROSE = ("cd to the project root first, then run make, then open the browser and check that the page "
         "loads without any errors in the console window")
TUTORIAL = "\n".join("cd step%d\nmake build\nEdit the rules and config files, then config.toml and settings.json; "
                     "cd .. when done." % i for i in range(300))


@pytest.mark.parametrize("key", AGENTS)
def test_f1_a_readme_line_that_starts_with_cd_and_has_twenty_five_more_words_is_quick_and_clean(key):
    assert len(PROSE.split()) - 1 >= 25
    write = file_call(key, "/x/p/README.md", "## Setup\n\n" + PROSE + "\n")
    (quiet, reason), seconds = timed(verdict, key, write)
    assert quiet and reason is None and seconds < QUICK


@pytest.mark.parametrize("key", AGENTS)
def test_f1_a_tutorial_full_of_cd_steps_and_cd_up_is_clean_and_has_no_time_out_reason(key):
    (quiet, reason), seconds = timed(verdict, key, file_call(key, "/x/p/TUTORIAL.md", TUTORIAL))
    assert quiet and reason is None and seconds < QUICK
    assert hook.agent_config_hits(hook._normalise_lines(TUTORIAL), command=False) == []
    assert hook.agent_config_hits(hook._normalise_lines(TUTORIAL)) == []              # and read as a command, too


def test_f1_plain_text_of_seventy_two_kilobytes_is_quick_and_the_deadline_is_looked_at():
    text = "word " * 14500
    assert len(text) > 72000
    reason, seconds = timed(hook.decide_agent_file, text)
    assert reason is None and seconds < QUICK
    hook._deadline[0] = 0.0                                                  # a deadline long gone
    with pytest.raises(hook._OutOfTime):
        hook._agent_path_hits(text, False)


def test_f1_a_big_readme_with_many_agent_words_is_quick():
    text = "This project keeps its notes next to the code. config rules hooks.json .cursor .codex\n" * 800
    reason, seconds = timed(hook.decide_agent_file, text)
    assert reason is None and seconds < QUICK


# ================================================================ F2: out of time asks

ZERO_BUDGET_INPUTS = ["ls", "echo hi", "rm ~/.cursor/hooks.json", "cd a", "git status", "cat README.md",
                      "bertie-mail send --to a@example.com --subject hi", "x"]


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", ZERO_BUDGET_INPUTS)
def test_f2_with_no_time_left_every_command_asks_to_be_safe(monkeypatch, key, command):
    monkeypatch.setattr(hook, "_PATTERN_SECONDS", 0)
    quiet, reason = verdict(key, command_call(key, command))
    assert not quiet and SAFE in reason, (key, command, reason)


@pytest.mark.parametrize("key", AGENTS)
def test_f2_with_no_time_left_text_going_into_a_file_asks_too(monkeypatch, key):
    monkeypatch.setattr(hook, "_PATTERN_SECONDS", 0)
    quiet, reason = verdict(key, file_call(key, "/x/p/notes.md", "Nothing here."))
    assert not quiet and SAFE in reason, (key, reason)


def test_f2_with_no_time_left_even_an_empty_text_asks():
    hook._deadline[0] = 0.0
    assert any(SAFE in h for h in _config_hits_with_budget(0.0, ""))
    assert any(SAFE in h for h in _config_hits_with_budget(0.0, "ls", command=False))
    assert any(SAFE in h for h in _config_hits_with_budget(-1.0, "ls; rm ~/.cursor/hooks.json"))


def _config_hits_with_budget(seconds, text, command=True):
    saved = hook._PATTERN_SECONDS
    hook._PATTERN_SECONDS = seconds
    try:
        return hook.agent_config_hits(hook._normalise_lines(text), command=command)
    finally:
        hook._PATTERN_SECONDS = saved


@pytest.mark.parametrize("key", SHELL_AGENTS)
def test_f2_a_busy_command_cannot_get_a_later_removal_through(monkeypatch, key):
    """The old check gave up quietly with what it had found so far, so a plain `rm` after enough
    busy work passed. A reason has to come back whatever the clock does."""
    busy = " ".join("".join("{rules,config}/" for _ in range(7)) + "*x?" for _ in range(4))
    command = "cd " + "".join("{a,b}" for _ in range(4)) + " d1 d2; ls " + busy + "; rm ~/.cursor/hooks.json"
    for budget in (0, 0.001, 0.02, 3.0):
        monkeypatch.setattr(hook, "_PATTERN_SECONDS", budget)
        (stopped, reason), seconds = timed(caught, key, command)
        assert stopped, (budget, reason)
        assert seconds < 10


def test_f2_out_of_time_inside_expanding_a_word_is_not_swallowed(monkeypatch):
    real = hook._expand

    def slow(word):
        if word == "boom":
            raise hook._OutOfTime("took too long")
        return real(word)

    monkeypatch.setattr(hook, "_expand", slow)
    for text in ("ls boom; rm ~/.cursor/hooks.json", "cd boom; rm ~/.cursor/hooks.json"):
        hits = hook.agent_config_hits(hook._normalise_lines(text))
        assert any(SAFE in h for h in hits), text
        file_hits = hook.agent_config_hits(hook._normalise_lines(text), command=False)
        assert any(SAFE in h for h in file_hits), text


def test_f2_a_word_too_big_to_expand_is_still_just_skipped(monkeypatch):
    real = hook._expand

    def big(word):
        if word == "big":
            raise ValueError("too big")
        return real(word)

    monkeypatch.setattr(hook, "_expand", big)
    hits = hook.agent_config_hits(hook._normalise_lines("ls big; cd big; rm ~/.cursor/hooks.json"))
    assert any("Cursor's hooks file" in h for h in hits) and not any(SAFE in h for h in hits)


def test_f2_the_reason_comes_from_agent_config_hits_and_is_only_given_once():
    hits = _config_hits_with_budget(0.0, "ls; ls; ls")
    assert len([h for h in hits if SAFE in h]) == 1


@pytest.mark.parametrize("key", SHELL_AGENTS)
def test_f2_a_time_out_denies_and_does_not_let_the_call_through(monkeypatch, key):
    monkeypatch.setattr(hook, "_PATTERN_SECONDS", 0)
    quiet, reason = verdict(key, command_call(key, "echo hello"))
    assert not quiet


# ================================================================ F3: a path built up at run time

# Each of these names a file only after the shell, or the script, has put it together. Every one has to
# stop a Codex/Kimi call and make Cursor and Antigravity ask. (Not here, accepted: see the end of the file.)
BUILT_UP = [
    """python3 -c "import os; os.remove(os.path.join(os.path.expanduser('~'), '.cursor', 'hooks.json'))" """,
    """python3 -c "import pathlib; pathlib.Path.home().joinpath('.codex','hooks.json').unlink()" """,
    """node -e "require('fs').unlinkSync(require('path').join(require('os').homedir(),'.cursor','hooks.json'))" """,
    """python3 -c "import shutil,os; shutil.rmtree(os.path.expanduser('~/.cursor'))" """,
    """rm "$(echo ~/.cursor)/hooks.json" """,
    """echo ~/.cursor | xargs -I{} rm {}/hooks.json""",
    """cd ~/.cursor; rm -rf "$(pwd)" """,
    """rm -rf ${KIMI_CODE_HOME:-~/.kimi-code}/config.toml""",
    "cd $(echo ~/.cursor) && rm hooks.json",
    "cd `echo ~/.codex` && rm hooks.json",
    """cd "$(dirname ~/.cursor/x)" && rm hooks.json""",
    "f=hooks.json; rm ~/.cursor/$f",
    "printf '%s/hooks.json' ~/.cursor | xargs rm",
    'D=$HOME/.cursor; rm -f "$D/hooks.json"',
    "export D=~/.cursor && rm -f $D/hooks.json",
    "cd ~ && cd .cursor && rm hooks.json",
    """python3 -c "import os;os.remove(os.path.expanduser('~/.cursor')+'/hooks.json')" """,
    "G=${HOME}/.gemini; mv $G/config /tmp/g; echo hooks.json",
    "rm $HOME/.codex/rules/../hooks.json",
]


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", BUILT_UP)
def test_f3_a_path_built_up_from_the_home_folder_is_caught(key, command):
    assert_caught(key, command)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", BUILT_UP[:8])
def test_f3_the_same_with_a_project_folder_given(key, command):
    assert_caught(key, command, "/x/proj")


FILE_BUILT_UP = [
    "import os\nos.remove(os.path.join(os.path.expanduser('~'),'.cursor','hooks.json'))\n",
    "import os\nos.remove(os.path.join(os.path.expanduser('~'), '.cursor', 'hooks.json'))\n",
    "from pathlib import Path\nPath.home()/'.codex'/'hooks.json'\n",
    "from pathlib import Path\n(Path.home() / '.codex' / 'hooks.json').unlink()\n",
    "p = os.path.join(os.environ['HOME'] + '/.codex', 'config.toml')\n",
    "const p = path.join(os.homedir(), '.codex', 'rules');\n",
    "p = os.path.join(os.path.expanduser('~'), '.gemini', 'config', 'hooks.json')\n",
    "p = os.path.join(os.path.expanduser('~'), '.gemini', 'antigravity-cli', 'settings.json')\n",
    "p = Path.home() / '.agents' / 'skills' / 'bertie'\n",
    "p = os.path.join(os.environ.get('KIMI_CODE_HOME'), 'config.toml')\n",
    "p = os.path.join(os.path.expanduser(\"~\"), \".kimi-code\", \"config.toml\")\n",
]


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("content", FILE_BUILT_UP)
def test_f3_text_that_builds_up_the_path_from_the_home_folder_is_caught_in_a_file(key, content):
    quiet, reason = verdict(key, file_call(key, "/x/p/clean.py", content))
    assert not quiet and "safety check" in reason, content


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("content", [
    "import os\np = os.path.join(root, '.cursor', 'hooks.json')\n",                  # a project's file: no home
    "p = Path('.codex') / 'rules'\nq = Path.home() / 'notes' / 'x'\n",                  # the home and the folder on different lines' paths
    "import os\nhome = os.path.expanduser('~')\nname = os.path.join('.cursor', 'hooks.json')\n",   # the home on another line
    "x = os.path.expanduser('~')  # see .cursor docs\ny = 'hooks.json'\n",
    "Cursor reads .cursor; hooks.json is separate; the home folder is ~\n",              # not joined by punctuation
])
def test_f3_the_home_folder_must_be_on_the_same_line_and_the_names_joined_by_path_punctuation(key, content):
    assert hook.loose_file_hits(content) == [], content
    quiet, reason = verdict(key, file_call(key, "/x/p/gen.py", content))
    assert quiet and reason is None, (content, reason)


def test_f3_every_agent_path_has_a_joined_form_for_files():
    """A line that builds each whole path from the home folder out of its pieces is caught."""
    for spec, what in hook._AGENT_PATHS:
        pieces = [names[0].lstrip("${").rstrip("}") for names in spec]
        line = "p = Path.home() / " + " / ".join(f"'{p}'" for p in pieces)
        reasons = hook.loose_file_hits(line)
        assert any(what in r for r in reasons), (what, line)


def test_f3_the_loose_rows_name_the_same_things_as_the_path_table():
    whats = {what for _, what in hook._AGENT_PATHS}
    assert {what for _, _, what in hook._LOOSE_PAIRS} == whats


@pytest.mark.parametrize("command", [
    "cat ~/.cursor/hooks.json.bak",                      # the names are words of their own only
    "ls ~/.cursor; cat hooks.json",
    "echo ~ .cursor hooks.json",                         # every command only looks
    "ls -d ~/.cursor ~/.codex; cat notes-hooks.json",
    "cd ~/.cursor && ls",
])
def test_f3_a_command_that_only_looks_is_not_a_reason_for_the_backstop(command):
    assert hook.loose_command_hits(command) == [], command


@pytest.mark.parametrize("command", [
    "git commit -am 'Update .cursor hooks.json'",         # no home folder in it
    "git add .cursor hooks.json",
    "cp template/hooks.json .cursor/",
    "sed -i s/a/b/ .codex/notes.md rules.txt",
    "python3 tools/gen.py --rules rules/ --out .codex-cache",
    "rm -f .cursorignore hooks.json.bak",
])
def test_f3_without_a_mention_of_the_home_folder_the_backstop_says_nothing(command):
    assert hook.loose_command_hits(command) == [], command


def test_f3_the_backstop_reads_the_command_alone_not_the_folder_it_runs_in(home):
    """Run in ~/.gemini, `git log -- ~/notes hooks.json` mentions the home folder and a hooks.json, and the
    folder's name would supply the third part of the old pairing. Only the command is read."""
    command = "git log -- ~/notes hooks.json"
    assert hook.loose_command_hits(command) == []
    assert hook.loose_command_hits("git log -- ~/.gemini/notes hooks.json")                   # named in the command: a reason
    cwd = str(home / ".gemini")
    assert hook.cwd_prefix(cwd, command)                                                      # the folder is put in front
    for key in AGENTS:
        assert_free(key, command, cwd)


def test_f3_the_home_folder_is_known_by_its_real_path_too(home):
    command = f"python3 -c \"open('{home}/' + '.cursor' + '/hooks.json')\""
    assert hook.loose_command_hits(command)


def test_f3_the_kimi_folder_from_the_environment_is_a_home_mention_and_a_folder(monkeypatch, tmp_path):
    kimi_dir = tmp_path / "kimi-data"
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_dir))
    command = f"D={kimi_dir}; rm $D/'config.toml'"
    reasons = hook.loose_command_hits(command)
    assert any("Kimi Code's settings" in r for r in reasons)
    assert hook.loose_command_hits("ls -la; rm /x/other/config.toml") == []


# ---- the FIXES2 lists still behave

FIXES2_CATCH = [
    "rm ~/.cu*/hooks.json", "mv ~/.cursor/hooks.jso? /tmp/", "rm -rf ~/.cursor", "cd ~ && mv .cursor .c2",
    "rm -rf ~/.codex", "mv ~/.codex/rul* /tmp", "rm -rf ~/.kimi-code", "mv ~/.gemini/config /tmp/g",
    "rm ~/.c?dex/hooks.json", "rm ~/.codex/hooks.*", "cd ~/.codex && rm -r r*", "mv ~/.agents/skills /tmp/s",
    "rm -rf ~/.cursor/", "rm -rf $HOME/.cursor", "rm -rf ${HOME}/.cursor", "rm -rf ~/.{cursor,x}", "rm -r ~/.gemini",
    "mv ~/.gemini/antigravity-cli /tmp/a", "rm ~/.gemini/antigravity-cli/settings.json", "rm -rf ~/.agents",
    "rm -rf ~/.agents/skills/bertie", "rm -rf ~/.agents/skills/b*", "cd ~/.agents/skills && rm -rf bertie",
    "cd ~/.gemini && mv config c2", "pushd ~/.codex && rm config.toml", "cd ~/.codex; cd rules; rm *",
    "sudo rm -rf ~/.cursor", "env X=1 rm -rf ~/.cursor", "trash ~/.cursor", "rm -rf ~/.Cursor",
    "rm -rf ~/.cursor/../.cursor", "cd ~/.cursor/.. && rm -rf .cursor", "rm ~/.cursor/hooks.json\\ ",
    "mv ~/.kimi-code/config.toml /tmp", "cd ~/.kimi-code && sed -i '' 1d config.toml",
    "cd ~/.cursor && sed -i x hooks.json", "ln -sf /dev/null ~/.cursor/hooks.json", "rm ~/'.cursor'/hooks.json",
    "rm ~/.cursor/[h]ooks.json", "rm ~/.cursor/@(hooks).json", "rm ~/.curs\"\"or/hooks.json",
    "chmod 000 ~/.cursor", "chflags uchg ~/.codex/hooks.json", "perl -e 'unlink glob(\"~/.cursor/hooks.json\")'",
    "cp /dev/null ~/.codex/hooks.json", "rsync -a --delete /tmp/empty/ ~/.cursor/",
]
FIXES2_CWD_CATCH = [
    ("mv hooks.json h", "@H/.cursor"), ("rm -rf rules", "@H/.codex"), ("rm -rf config", "@H/.gemini"),
    ("rm -rf bertie", "@H/.agents/skills"), ("rm config.toml", "@H/.kimi-code"), ("rm ../hooks.json", "@H/.cursor/x"),
    ("mv hook.sh h", "@H/.config/bertie"), ("mv bertie old", "@H/.config"), ("rm -rf ../../hooks.json", "@H/.codex/rules/a"),
    ("rm hooks.json", "@H/.gemini/config"), ("rm settings.json", "@H/.gemini/antigravity-cli"),
    ("rm -rf .cursor", "@H"), ("mv .codex x", "@H"), ("rm -f *", "@H/.local/bin"),
]
FIXES2_FREE = [
    ("ls -la", "/x/src/bertie"), ("ls -la", "/x/src/python-keyring"), ("rm -f *", "/x/proj/bin"),
    ("git status", "/x/work/svc_payments"), ("git status", "/x/osascript-tools"), ("git status", "/x/approve.swift-demo"),
    ("cat package.json; ls rules", "@H/.codex/worktrees/abc/proj"),
    ("cargo build; cat Cargo.toml config.toml", "@H/.codex/worktrees/abc/proj"),
    ("npm test", "@H/.codex/worktrees/abc/proj"), ("rm -rf rules", "@H/.codex/worktrees/abc/proj"),
    ("ls ~/.cursor", None), ("ls -la ~/.agents/skills", None),
    ("cd ../ && npm i", "@H/.codex/worktrees/abc/proj"), ("cat ../README.md", "@H/.codex/worktrees/abc/proj"),
    ("git log", "/x/proj/.config"), ("ls", "@H"),
]


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", FIXES2_CATCH)
def test_f3_every_round_two_must_catch_command_is_still_caught(key, command):
    assert_caught(key, command)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command,cwd", FIXES2_CWD_CATCH)
def test_f3_every_round_two_folder_case_is_still_caught(key, home, command, cwd):
    assert_caught(key, at_home(command, home), at_home(cwd, home))


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command,cwd", FIXES2_FREE)
def test_f3_every_round_two_must_not_flag_case_is_still_free(key, home, command, cwd):
    assert_free(key, at_home(command, home), at_home(cwd, home) if cwd else None)


# ================================================================ F4: a Codex patch

def patch(*added, path="/x/p/clean.sh"):
    return {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_input": {
        "command": "*** Begin Patch\n*** Add File: " + path + "\n" + "\n".join("+" + l for l in added) + "\n*** End Patch"}}


@pytest.mark.parametrize("lines", [
    ["#!/bin/sh", "rm -rf ~/.cursor"],
    ["#!/bin/sh", "mv ~/.gemini/config /tmp/g"],
    ["#!/bin/sh", "rm -rf ~/.codex"],
    ["#!/bin/sh", "cd ~/.cursor", "rm -f hooks.json"],
    ["#!/bin/sh", "cd ~", "mv .cursor .c2"],
    ["rm -f ~/.cursor/hooks.json"],
])
def test_f4_a_patch_that_adds_a_removal_is_caught(lines):
    quiet, reason = verdict("codex", patch(*lines))
    assert not quiet and "safety hook is set up" in reason, lines


def test_f4_one_leading_plus_is_taken_off_each_line_and_no_more():
    text = ("*** Begin Patch\n*** Update File: /x/a.sh\n@@\n+rm -rf ~/.cursor\n++two\n+a+b\n context\n-removed\n+\n"
            "*** End Patch")
    kind, got = codex.hook_input({"tool_name": "apply_patch", "tool_input": {"command": text}})
    assert kind == "file"
    assert got == ("*** Begin Patch\n*** Update File: /x/a.sh\n@@\nrm -rf ~/.cursor\n+two\na+b\n context\n-removed\n\n"
                   "*** End Patch")


def test_f4_other_fields_of_the_patch_input_are_read_the_same_way():
    kind, got = codex.hook_input({"tool_name": "apply_patch", "tool_input": {"command": "+a", "note": "+b\n+c"}})
    assert (kind, got) == ("file", "a\nb\nc")


def test_f4_a_patch_of_plain_prose_is_still_free():
    quiet, _ = verdict("codex", patch("# Setup", "Config lives in .codex; see the rules section below.", ".cursor", ".codex"))
    assert quiet


# ================================================================ F5: a project's own folders

PROJECT = "/x/proj"
# `cp -r ../template/.cursor .` (a `..` in front always counts), and the three lines that name a whole
# agent path (`.codex/config.toml`, `.agents/skills/bertie`) are blocked as they were at 8a86dee: not here.
PROJECT_FREE = [
    "git add .cursor",
    "git add .cursor/ .gemini/",
    "git status .codex",
    "git rm -r --cached .codex",
    "rm -rf .gemini",
    "rsync -a .agents/ /tmp/backup/",
    "tar czf cfg.tgz .cursor .codex",
    "zip -r rules.zip .cursor",
    "find .cursor -name '*.mdc'",
    "code .cursor",
    "vim .gemini/settings.json",
    "git diff -- .cursor/rules",
    'git commit -am "Update .cursor hooks.json"',
    'git commit -m "move rules into .codex"',
    "mkdir -p .cursor && cp template/hooks.json .cursor/",
    "sed -i s/a/b/ .codex/notes.md rules.txt",
    "cat ~/.cursor",
    "ls ~/.cursor && cat hooks.json",
    "python3 tools/gen.py --rules rules/ --out .codex-cache",
    "cd . ; rm -rf .cursor",
]
# round 4: "cd src && rm -rf .cursor" used to be free here. It is not any more: once a command has gone into
# another folder (src could be a link to the home folder made a moment before) a bare name could be the
# home folder's. It is held as blocked in test_fix_round4.py, and `cd .` (which goes nowhere) stays free.


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", PROJECT_FREE)
def test_f5_commands_on_a_projects_own_agent_folders_are_free_when_the_folder_is_given(key, command):
    assert_free(key, command, PROJECT)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", [
    "mv .cursor .c2", "rm -rf .codex", "mv .gemini/config /tmp/g", "mv .agents/skills /tmp/s", "git rm -r --cached .codex",
    "rm -rf .gemini", "mv ../.cursor x", "cd .. && rm -rf .cursor",
])
def test_f5_with_no_folder_given_a_bare_name_could_be_the_one_in_the_home_folder(key, command):
    assert_caught(key, command)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command,cwd", [
    ("mv .cursor .c2", "@H"), ("rm -rf .codex", "@H"), ("mv .gemini/config /tmp/g", "@H"), ("mv .agents/skills /tmp/s", "@H"),
    ("rm hooks.json", "@H/.cursor"), ("sed -i x config.toml", "@H/.codex"), ("rm -r r*", "@H/.codex"),
    ("mv ../.cursor /tmp/x", "@H/proj"), ("cd .. && rm -rf .cursor", "@H/proj"),
    ("rm ~/.cu*/hooks.json", PROJECT), ("rm -rf ~/.cursor", PROJECT), ("cd ~ && mv .cursor .c2", PROJECT),
    ("rm -rf @H/.cursor", PROJECT), ("cd @H && rm -rf .cursor", PROJECT), ("cd $HOME && rm -rf .cursor", PROJECT),
    ("cd ${HOME}; rm -rf .codex", PROJECT), ("cd ~/ && rm -rf .agents/skills", PROJECT),
])
def test_f5_the_home_folder_and_the_way_up_to_it_still_count(key, home, command, cwd):
    assert_caught(key, at_home(command, home), at_home(cwd, home))


def test_f5_the_home_folder_is_a_folder_that_matters():
    home = os.path.expanduser("~")
    assert hook.cwd_prefix(home, "ls") == "cd " + home + " && "
    assert hook.cwd_prefix(home + "/proj", "ls") == ""
    assert hook.cwd_prefix(home + "/proj", "mv ../.cursor x") == ""            # `..` is read in the command itself
    assert hook.cwd_prefix(os.path.realpath(home), "ls") != ""


def test_f5_a_name_ending_at_a_folder_is_not_called_home_when_it_is_not():
    reasons = {key: assert_caught(key, "mv ../.cursor x") for key in SHELL_AGENTS}
    for key, reason in reasons.items():
        assert "changes .cursor," in reason and "changes ~/.cursor" not in reason, (key, reason)
    assert "changes ~/.cursor," in assert_caught("codex", "rm -rf ~/.cursor")
    assert "changes ~/.cursor," in assert_caught("codex", "rm -rf $HOME/.cursor")
    assert "changes ~/.cursor," in assert_caught("codex", "mv .cursor x", os.path.expanduser("~"))
    assert "changes ~/.gemini/config," in assert_caught("cursor", "cd ~ && mv .gemini/config /tmp/g")
    assert "changes .kimi-code," in assert_caught("codex", "rm -rf .kimi-code")             # no ~/ for what was not ~/


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("path,content", [
    ("/x/p/README.md", "Config lives in .codex; see the rules section below."),
    ("/x/p/README.md", "Cursor reads .cursor/rules; hooks.json is separate"),
    ("/x/p/.gemini/settings.json", '{"theme": "dark"}'),
    ("/x/p/.gitignore", ".cursor\n.codex\n.gemini\n.agents\n"),
    ("/x/p/Makefile", "clean:\n\trm -rf build .cursor-cache\n"),
    ("/x/p/setup.sh", "#!/bin/sh\ncp -r template/.cursor .\ngit add .cursor\n"),
    ("/x/p/README.md", "To share rules, commit them: `git add .cursor`.\n"),
    ("/x/p/README.md", "Then git tracks .codex and .gemini like any folder.\n"),
    ("/x/p/docs.md", "Run `mv .gemini/settings.json .gemini/settings.bak` to reset Gemini CLI.\n"),
    ("/x/p/a.py", "SKIP = {'.git', '.cursor', '.codex', '.gemini'}\nshutil.rmtree(tmp)\n"),
    ("/x/p/a.py", "for d in ('.cursor', '.codex'):\n    os.makedirs(os.path.join(root, d), exist_ok=True)\n"),
    ("/x/p/a.ts", "const dirs = ['.cursor', '.vscode'];\n"),
    ("/x/p/README.md", "Kimi keeps settings in ~/.kimi-code (see config.toml)."),
    ("/x/p/gen.py", "import os\np = os.path.join(root, '.cursor', 'hooks.json')\n"),
    ("/x/p/clean.sh", "#!/bin/sh\nrm -rf .cursor\nmv .codex x\n"),
])
def test_f5_prose_and_ordinary_project_code_written_to_a_file_is_free(key, path, content):
    quiet, reason = verdict(key, file_call(key, path, content))
    assert quiet, (key, path, content, reason)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("content", [
    "cd ~\nrm -rf .cursor\n", "cd ~/\nmv .codex x\n", "cd ..\nrm -rf .cursor\n", "cd $HOME\nrm -rf .gemini/config\n",
    "rm -rf ~/.cursor\n", "cd ~/.cursor\nrm -f hooks.json\n",
])
def test_f5_in_a_file_a_bare_name_counts_only_after_a_cd_that_could_lead_home(key, content):
    quiet, reason = verdict(key, file_call(key, "/x/p/clean.sh", content))
    assert not quiet and "safety hook is set up" in reason, content


def test_f5_cwd_known_is_a_real_argument_not_a_flag_left_behind():
    text = hook._normalise_lines("git add .cursor")
    assert hook.agent_config_hits(text)                                  # nothing said about the folder: it could be home
    assert hook.agent_config_hits(text, cwd_known=True) == []
    assert hook.agent_config_hits(text, True, False)
    assert hook.agent_config_hits(hook._normalise_lines("mv ../.cursor x"), cwd_known=True)
    assert hook.agent_config_hits(hook._normalise_lines("cd ~; mv .cursor x"), cwd_known=True)
    # one call after another: nothing is left over from the call before
    for known in (True, False, True, False):
        assert bool(hook.agent_config_hits(text, cwd_known=known)) is (not known)


def test_f5_nothing_is_left_behind_between_calls_in_one_process():
    project = command_call("codex", "git add .cursor", PROJECT)
    bare = command_call("codex", "git add .cursor")
    for _ in range(2):
        assert verdict("codex", project)[0] is True
        assert verdict("codex", bare)[0] is False


def test_f5_folder_given_means_a_full_path():
    assert hook.folder_given("/x/proj") and hook.folder_given(None, "rel", "/x")
    assert not hook.folder_given(None) and not hook.folder_given("") and not hook.folder_given("proj", ".", "~", "$HOME")
    assert not hook.folder_given(5, ["/x"])
    assert not hook.folder_given()


def test_f5_each_agent_says_whether_it_gave_a_full_path():
    assert codex.hook_cwd_known({"cwd": "/x", "tool_input": {"command": "ls"}})
    assert codex.hook_cwd_known({"tool_input": {"command": "ls", "workdir": "/x"}})
    assert codex.hook_cwd_known({"tool_input": {"command": "ls", "cwd": "/x"}})
    assert not codex.hook_cwd_known({"tool_input": {"command": "ls"}})
    assert not codex.hook_cwd_known({"cwd": "rel", "tool_input": {"command": "ls", "workdir": ".cursor"}})
    assert not codex.hook_cwd_known({"cwd": "/x", "tool_input": "ls"})
    assert cursor.hook_cwd_known({"command": "ls", "cwd": "/x"})
    assert not cursor.hook_cwd_known({"command": "ls"}) and not cursor.hook_cwd_known({"command": "ls", "cwd": ""})
    assert not cursor.hook_cwd_known({"command": "ls", "tool_name": "Shell", "cwd": "/x"})     # that call leaves the folder out
    kimi_call = {"tool_name": "Bash", "tool_input": {"command": "ls", "cwd": "/x"}, "cwd": "rel"}
    assert kimi.hook_cwd_known(kimi_call)
    assert not kimi.hook_cwd_known({"tool_name": "Bash", "tool_input": {"command": "ls", "cwd": "rel"}, "cwd": "/x"})
    assert kimi.hook_cwd_known({"tool_name": "Bash", "tool_input": {"command": "ls", "cwd": ""}, "cwd": "/x"})
    assert not kimi.hook_cwd_known({"tool_name": "Write", "tool_input": {"cwd": "/x"}, "cwd": "/x"})
    assert not kimi.hook_cwd_known({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert antigravity.hook_cwd_known(command_call("antigravity", "ls", "/x"))
    assert not antigravity.hook_cwd_known(command_call("antigravity", "ls"))
    assert not antigravity.hook_cwd_known(command_call("antigravity", "ls", "rel"))
    assert not antigravity.hook_cwd_known({"toolCall": {"name": "write_to_file", "args": {"Cwd": "/x"}}})


def test_f5_an_adapter_without_hook_cwd_known_is_read_as_no_folder_given():
    class Old:
        KEY, LABEL, HOOK_STYLE = "old", "Old", "ask"

        @staticmethod
        def hook_input(data):
            return "command", data["c"]

        @staticmethod
        def hook_answer(reason):
            return (json.dumps({"r": reason}) + "\n", 0) if reason else ("", 0)

    assert hook.agent_reason(Old, {"c": "git add .cursor"})
    assert hook.agent_reason(Old, {"c": "git status"}) is None


# ================================================================ F6: Antigravity's Cwd

def antigravity_run(command, cwd=None, **more):
    args = {"CommandLine": command, **more}
    if cwd is not None:
        args["Cwd"] = cwd
    return {"toolCall": {"name": "run_command", "args": args}, "hookEventName": "PreToolUse"}


@pytest.mark.parametrize("cwd", ["/x/src/bertie", "/x/src/python-keyring", "/x/work/svc_payments", "/x/osascript-tools",
                                 "/x/approve.swift-demo", "/x/proj/.cursor", "/x/proj", "/x/proj/.codex/worktrees/abc/proj"])
def test_f6_an_ordinary_folder_does_not_trip_the_checks(cwd):
    assert verdict("antigravity", antigravity_run("git status", cwd)) == (True, None), cwd


@pytest.mark.parametrize("command,cwd", [
    ("mv .cursor x", "@H"), ("rm -rf .codex", "@H"), ("rm hooks.json", "@H/.cursor"), ("mv hook.sh h", "@H/.config/bertie"),
    ("mv bertie old", "@H/.config"), ("sed -i x config.toml", "@H/.codex"), ("ln -sf /usr/bin/true .venv/bin/python", "@H/.config/bertie"),
])
def test_f6_the_folder_that_matters_is_read_as_the_folder(home, command, cwd):
    quiet, reason = verdict("antigravity", antigravity_run(at_home(command, home), at_home(cwd, home)))
    assert not quiet and "safety check" in reason, (command, cwd)


def test_f6_cwd_is_a_folder_not_a_line_of_text():
    kind, text = antigravity.hook_input(antigravity_run("ls -la", "/tmp"))
    assert (kind, text) == ("command", "ls -la")
    kind, text = antigravity.hook_input(antigravity_run("ls -la", "/tmp", Blocking=True, Note="why"))
    assert (kind, text) == ("command", "ls -la\nwhy")
    assert antigravity.hook_scan_text(antigravity_run("ls", "/tmp"), "ls") is None
    assert antigravity.hook_scan_text(antigravity_run("ls"), "ls") is None
    assert antigravity.hook_scan_text(antigravity_run("ls", ""), "ls") is None
    assert antigravity.hook_scan_text(antigravity_run("ls", "/h/.config/bertie"), "ls") == "cd /h/.config/bertie && ls"
    assert antigravity.hook_scan_text({"toolCall": {"name": "write_to_file", "args": {"Cwd": "/h/.config/bertie"}}}, "x") is None


@pytest.mark.parametrize("cwd", [5, ["/x"], {"a": "/x"}, True])
def test_f6_a_cwd_that_is_not_text_asks(cwd):
    quiet, reason = verdict("antigravity", antigravity_run("ls", cwd))
    assert not quiet and "could not read" in reason


def test_f6_the_other_fields_of_the_call_are_still_read():
    quiet, reason = verdict("antigravity", antigravity_run("ls", "/x/proj", Note="rm ~/.cursor/hooks.json"))
    assert not quiet and "Cursor's hooks file" in reason
    quiet, _ = verdict("antigravity", antigravity_run("ls", "/x/osascript-tools", Note="fine"))
    assert quiet


def test_f6_a_text_typed_into_a_running_terminal_has_no_folder():
    call = {"toolCall": {"name": "manage_task", "args": {"Action": "send_input", "Input": "rm ~/.cursor/hooks.json"}}}
    quiet, reason = verdict("antigravity", call)
    assert not quiet and "Cursor's hooks file" in reason


# ================================================================ F7: a link to another spelling of our hook

def test_f7_a_link_to_a_file_with_the_portable_spelling_of_our_hook_is_refused(home, ctx, tmp_path):
    put(home, HOOKS, json.dumps({"version": 1, "hooks": {"stop": [{"command": "./mine.sh"}]}}) + "\n")
    cursor.install(ctx, KIT)
    link = home / HOOKS
    dotfiles = tmp_path / "dotfiles" / "hooks.json"
    dotfiles.parent.mkdir()
    text = link.read_text().replace(str(ctx.install_dir / "hook.sh"), "~/.config/bertie/hook.sh")
    assert "~/.config/bertie/hook.sh" in text and agents.hook_command(ctx, "cursor") not in text
    dotfiles.write_text(text)
    link.unlink()
    link.symlink_to(dotfiles)
    snapshot = dotfiles.read_bytes()
    problems = agents.uninstall_problems(ctx)
    assert len(problems) == 1 and str(dotfiles) in problems[0]
    assert "remove the entry there, or replace the link with a plain file" in problems[0]
    cursor.uninstall(ctx)
    assert dotfiles.read_bytes() == snapshot and link.is_symlink()
    assert "cursor" in kit_setup.load_manifest(ctx)["agents"]                      # the record stays until it is dealt with


@pytest.mark.parametrize("spelling", ["$HOME/.config/bertie/hook.sh", "${HOME}/.config/bertie/hook.sh",
                                      "'~/.config/bertie/hook.sh'"])
def test_f7_the_other_spellings_count_too(home, ctx, tmp_path, spelling):
    put(home, HOOKS, "{}\n")
    target = tmp_path / "t.json"
    target.write_text(json.dumps({"hooks": {"beforeShellExecution": [{"command": f"sh {spelling} --agent cursor"}]}}))
    (home / HOOKS).unlink()
    (home / HOOKS).symlink_to(target)
    assert agents.link_problem(ctx, "cursor", home / HOOKS)


def test_f7_a_link_to_another_installs_hook_is_still_dropped(home, ctx, tmp_path):
    put(home, HOOKS, "{}\n")
    target = tmp_path / "t.json"
    target.write_text(json.dumps({"hooks": {"beforeShellExecution": [{"command": "sh /srv/other/.config/bertie/hook.sh --agent cursor"}]}}))
    (home / HOOKS).unlink()
    (home / HOOKS).symlink_to(target)
    assert agents.link_problem(ctx, "cursor", home / HOOKS) is None


# ================================================================ the Claude Code path is not touched

CLAUDE_CASES = ["ls", "git status", "rm ~/.cursor/hooks.json", "rm -rf ~/.cursor", "git add .cursor", "cd " + CD_NUMBERS + "; rm ~/.cursor/hooks.json",
                "mv .cursor .c2", "python3 -c \"import os; os.remove(os.path.join(os.path.expanduser('~'), '.cursor', 'hooks.json'))\"",
                "cat ~/.codex/config.toml", "echo hello"]


def test_claude_code_path_never_asks_the_agent_file_checks(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("the Claude Code path must not run the agent checks")

    for name in ("agent_config_hits", "loose_command_hits", "loose_file_hits", "_agent_path_hits", "_follow_cd"):
        monkeypatch.setattr(hook, name, fail)
    for command in CLAUDE_CASES:
        assert hook.decide_tool("Bash", {"command": command}) is None, command
    assert hook.decide_tool("Write", {"file_path": "/x/a.py", "content": "os.remove(os.path.join(os.path.expanduser('~'), '.cursor', 'hooks.json'))"}) is None
    assert hook.decide_tool("Bash", {"command": "bertie-mail send --to a@example.com"})


@pytest.mark.parametrize("command,asks", [("ls", False), ("rm ~/.cursor/hooks.json", False), ("cd " + CD_NUMBERS + "; rm -rf ~/.codex", False),
                                          ("bertie-mail send --to a@example.com", True), ("osascript -e 'x'", True)])
def test_claude_code_path_as_a_process_with_no_arguments(home, command, asks):
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TZ": "UTC"}
    done = subprocess.run([sys.executable, "-I", str(KIT / "tools" / "hook.py")], capture_output=True, text=True, env=env, timeout=60,
                          input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))
    assert done.returncode == 0
    if asks:
        assert json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    else:
        assert done.stdout.strip() == ""


# ================================================================ accepted limits
#
# These stay open, on purpose: reading them needs the shell, or a script, to be run.
#   * the output of one command used as the path of the next:  `echo ~/.cursor | xargs rm -rf`,
#     `ls -d ~/.gemini/config | xargs -I@ mv @ /tmp/g`, `test -d ~/.cursor && rm -rf $_`,
#     `cd ~/.cursor && rm -rf "$PWD"`;
#   * a pattern the shell can be told to widen: `shopt -s dotglob; rm -rf ~/*/hooks.json`,
#     `setopt globdots; rm ~/*/hooks.json`, `rm -rf ~/.*/*`, `find ~ -maxdepth 2 -name hooks.json -delete`;
#   * in text going into a file: `D=$HOME/.cursor` and then `$D/hooks.json` on a later line, and a
#     for-loop over folder names (`for f in .cursor .codex; do rm -rf ~/$f; done`);
#   * in text going into a file: a path split over several lines and joined only when the program runs
#     (`path = "~/.cursor/" +` on one line and `"hooks.json"` on the next, or a folder and a file name
#     put together across a line break);
#   * a Codex worktree under ~/.codex, climbing out with "$PWD" and dirname;
#   * zsh's two-argument `cd old new`;
#   * `cp -r ../template/.cursor .` is still stopped (a `..` in front always counts), as are the lines
#     that name a whole agent path (`git add .agents/skills/bertie`, `rm .codex/config.toml`).
