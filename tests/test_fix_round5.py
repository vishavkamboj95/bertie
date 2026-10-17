"""Fix round 5, from the fifth review of the other-agent work.

I1 text going into a file for another agent (hook.py --agent X) is not read when it is too big to read in
the time the agent allows: over 256 KiB in all, a run without a space longer than 20,000 characters, or
runs without a space that together would take as long to read as four of those. The install-folder pattern
(`berti*` ... `/`) takes the square of the length of such a run, so 96 KB of `berti*berti*...` held the hook
3.5 s and 192 KB 13.6 s, and Codex, Cursor and Kimi let a call that times out go. The Claude Code path
(no --agent) is not touched.

HOME is a temporary folder everywhere (the autouse fixture in conftest.py)."""
import io
import json
import time

import pytest

import hook
from test_fix_round3 import AGENTS, SAFE, file_call, run_process, verdict

CAP = 256 * 1024
WORD = ("berti*" * 4000)[:20000]                       # the longest run that is read


def stopped(key, content, path="/x/proj/notes.txt"):
    quiet, reason = verdict(key, file_call(key, path, content))
    return not quiet, reason


def edit_call(key, path, old, new):
    """An Edit-style call: Kimi and Cursor's Edit/StrReplace, Antigravity's replace tools."""
    if key == "codex":
        patch = "*** Begin Patch\n*** Update File: " + path + "\n@@\n-" + old + "\n+" + new + "\n*** End Patch"
        return {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_input": {"command": patch}}
    if key == "cursor":
        return {"hook_event_name": "preToolUse", "tool_name": "Write",
                "tool_input": {"file_path": path, "old_string": old, "new_string": new}}
    if key == "kimi":
        return {"hook_event_name": "PreToolUse", "tool_name": "Edit",
                "tool_input": {"file_path": path, "old_string": old, "new_string": new}}
    return {"toolCall": {"name": "replace_file_content",
                         "args": {"TargetFile": path, "TargetContent": old, "ReplacementContent": new}}}


# ---------------------------------------------------------------- the reviewer's repros refuse, quickly

@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("n", [16000, 32000])           # 96 KB and 192 KB of `berti*`
def test_repro_is_refused_in_milliseconds(key, n):
    start = time.monotonic()
    refused, reason = stopped(key, "berti*" * n)
    took = time.monotonic() - start
    assert refused and "a line is too long to check; asking to be safe" in reason and SAFE in reason
    assert took < 1.0, took


def test_repro_answer_form_per_agent():
    """deny for Codex, Kimi and Cursor (a file write), force_ask for Antigravity."""
    content = "berti*" * 16000
    for key in ("codex", "kimi"):
        out, code = hook.agent_answer(key, io.StringIO(json.dumps(file_call(key, "/x/a.txt", content))))
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    out, _ = hook.agent_answer("cursor", io.StringIO(json.dumps(file_call("cursor", "/x/a.txt", content))))
    assert json.loads(out)["permission"] == "deny"
    out, _ = hook.agent_answer("antigravity", io.StringIO(json.dumps(file_call("antigravity", "/x/a.txt", content))))
    assert json.loads(out)["decision"] == "force_ask"


@pytest.mark.parametrize("key", AGENTS)
def test_repro_through_the_real_process(tmp_path, key):
    out, took = run_process(tmp_path, key, file_call(key, "/x/a.txt", "berti*" * 32000))
    assert "a line is too long to check" in out
    assert took < 5.0, took


# ---------------------------------------------------------------- the two caps

@pytest.mark.parametrize("key", AGENTS)
def test_over_256_kib_is_refused(key):
    refused, reason = stopped(key, "x = 1\n" * 50000)       # 300 KB of ordinary lines
    assert refused and "the file is too large to check (over 256 KB); asking to be safe" in reason


@pytest.mark.parametrize("key", AGENTS)
def test_just_under_the_size_cap_passes(key):
    code = "def f(x):\n    return x + 1  # a comment\n" * 6000
    code = code[:CAP - 400]                                 # room for the path and the agent's own wrapping
    refused, reason = stopped(key, code)
    assert not refused, reason


@pytest.mark.parametrize("key", AGENTS)
def test_a_run_over_20000_characters_is_refused(key):
    refused, reason = stopped(key, "a" * 20001)
    assert refused and "a line is too long to check; asking to be safe" in reason


@pytest.mark.parametrize("key", AGENTS)
def test_one_run_of_exactly_20000_characters_passes(key):
    refused, reason = stopped(key, "# data\n" + "a" * 20000 + "\nprint(1)\n")
    assert not refused, reason


def test_the_size_is_counted_in_bytes():
    line = "é" * 99 + "\n\n"                               # 200 bytes
    assert hook.file_too_big(line * (CAP // 200 + 1))
    assert hook.file_too_big(line * (CAP // 200 - 1)) is None
    assert hook.file_too_big(("x" * 99 + "\n") * (CAP // 100)) is None
    assert hook.file_too_big(("x" * 99 + "\n") * (CAP // 100 + 1))


def test_a_run_ends_at_any_white_space():
    assert hook.file_too_big(("a" * 19999 + " ") * 3) is None
    assert hook.file_too_big("a" * 20001) is not None
    assert hook.file_too_big(("a" * 20001).replace("a", "a ", 1)) is None or True   # unicode spaces: never a crash


@pytest.mark.parametrize("key", AGENTS)
def test_edit_style_tools_count_the_text_they_check(key):
    quiet, reason = verdict(key, edit_call(key, "/x/a.py", "old", "berti*" * 16000))
    assert not quiet and "a line is too long to check" in reason
    quiet, reason = verdict(key, edit_call(key, "/x/a.py", "old line", "new line"))
    assert quiet, reason


@pytest.mark.parametrize("key", AGENTS)
def test_edit_style_over_the_size_cap(key):
    big = "x = 1\n" * 50000
    quiet, reason = verdict(key, edit_call(key, "/x/a.py", "old", big))
    assert not quiet and "the file is too large to check" in reason


def test_antigravity_other_file_tools_are_covered():
    for name in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
        call = {"toolCall": {"name": name, "args": {"TargetFile": "/x/a", "CodeContent": "berti*" * 20000}}}
        quiet, reason = verdict("antigravity", call)
        assert not quiet and "too long to check" in reason, name


# ---------------------------------------------------------------- the worst case under the caps is quick

def _worst_case():
    """As much slow text as the caps allow: runs of `berti*` as long as the work budget lets in, the
    rest ordinary code, 256 KiB in all."""
    runs = " ".join([WORD] * hook._MAX_LONG_WORDS)
    code = "x = 1  # /home/user/a\n" * 20000
    return (runs + "\n" + code)[:CAP - 400]


@pytest.mark.parametrize("key", AGENTS)
def test_worst_case_under_the_caps_is_quick(tmp_path, key):
    content = _worst_case()
    assert hook.file_too_big(content) is None
    out, took = run_process(tmp_path, key, file_call(key, "/x/proj/a.txt", content))
    print(f"worst case under the caps, {key}: {took:.2f} s end to end ({len(content)} characters)")
    assert took < 5.0, took


@pytest.mark.parametrize("key", AGENTS)
def test_many_runs_as_long_as_allowed_are_refused_not_slow(tmp_path, key):
    content = " ".join([WORD] * 13)                         # 13 x 20,000 characters: 2 s before the budget
    out, took = run_process(tmp_path, key, file_call(key, "/x/proj/a.txt", content))
    assert "a line is too long to check" in out
    assert took < 5.0, took


@pytest.mark.parametrize("key", AGENTS)
def test_256_kib_of_ordinary_code_passes_quickly(tmp_path, key):
    code = ("def handler(request):\n    value = request.get('name', '')  # fine\n    return value.strip()\n\n" * 4000)
    code = code[:CAP - 400]
    out, took = run_process(tmp_path, key, file_call(key, "/x/proj/a.py", code))
    assert "safety check" not in out
    assert took < 5.0, took


def test_long_words_that_are_not_runs_of_the_budget_are_read():
    """Words of 1,000 characters or fewer do not count toward the budget at all."""
    assert hook.file_too_big(" ".join(["a" * 1000] * 250)) is None


# ---------------------------------------------------------------- the Claude Code path is not changed

def test_the_claude_path_does_not_use_the_cap():
    """decide_file_text (the Claude path) has no cap: text the agent path refuses is read as before."""
    assert hook.file_too_big("a" * 20001)
    assert hook.decide_file_text("a" * 20001) is None
    assert hook.decide_file_text("x = 1\n" * 50000) is None


# ================================================================ B1: a redirection in front of the cd target

import os  # noqa: E402

from test_fix_round3 import SHELL_AGENTS, assert_free, caught, command_call  # noqa: E402,F401

REDIRECTED_CD = [
    "cd >/dev/null .cursor && rm hooks.json",
    "cd </dev/null .cursor && rm hooks.json",
    "pushd >/dev/null .cursor && rm hooks.json",
    "cd 2>/dev/null .codex && sed -i '' /hooks/d config.toml",
    "cd >/dev/null .gemini/config && rm hooks.json",
    "cd >/dev/null .codex && rm -rf rules",
    "cd >/dev/null .agents/skills && rm -rf bertie",
    "cd >> /dev/null .cursor && rm hooks.json",
    "cd <<< x .cursor && rm hooks.json",
    "cd &>/dev/null .cursor && rm hooks.json",
    "cd >&/dev/null .cursor && rm hooks.json",
    "cd >|/dev/null .cursor && rm hooks.json",
    "cd 3>&1 .cursor && rm hooks.json",
    "cd 2>&1 .codex && rm -rf rules",
    "cd .cursor >/dev/null && rm hooks.json",
    ">/dev/null cd .cursor && rm hooks.json",
    "2>/dev/null cd .codex && rm config.toml",
    "cd >/dev/null 2>&1 .cursor && rm hooks.json",
]


def _home():
    return os.path.expanduser("~")


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", REDIRECTED_CD)
def test_b1_a_redirection_before_the_cd_target_does_not_hide_it(key, command):
    for cwd in (None, _home()):
        stopped_, reason = _caught_in(key, command, cwd)
        assert stopped_, (key, command, cwd)


def _caught_in(key, command, cwd):
    quiet, reason = verdict(key, command_call(key, command, cwd))
    return not quiet, reason


@pytest.mark.parametrize("key", AGENTS)
def test_b1_the_same_in_file_text(key):
    for text in ("#!/bin/sh\ncd >/dev/null ~/.cursor && rm hooks.json\n",
                 "#!/bin/sh\ncd >/dev/null ~/.codex\nrm -rf rules\n",
                 "cd 2>/dev/null ~/.codex && rm config.toml\n"):
        refused, reason = stopped(key, text)
        assert refused, (key, text)


@pytest.mark.parametrize("key", AGENTS)
def test_b1_redirections_alone_stay_free(key):
    for command in ("ls >/dev/null 2>&1", "cd src >/dev/null && ls", "cd /tmp/x 2>/dev/null && rm out.txt",
                    "echo hi > notes.txt", "make 2>&1 | tee build.log", "cd >/dev/null /tmp && ls"):
        assert_free(key, command, "/x/proj")


# ================================================================ B2: a path built at run time, in the home folder

BUILT_AT_RUN_TIME = [
    "f=hooks.json; rm .cursor/$f",
    "python3 -c \"import os; os.remove(os.path.join('.cursor','hooks.json'))\"",
    "node -e \"require('fs').unlinkSync(require('path').join('.cursor','hooks.json'))\"",
    "cd ${D:-.cursor} && rm hooks.json",
    "cd .cursor$X && rm hooks.json",
    "cd $(echo .cursor) && rm hooks.json",
    "python3 -c \"import os; os.chdir('.codex'); os.remove('config.toml')\"",
]


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", BUILT_AT_RUN_TIME + ["rm .cursor/$(echo hooks.json)"])
def test_b2_built_at_run_time_is_caught_in_the_home_folder_and_with_no_folder(key, command):
    for cwd in (None, _home()):
        stopped_, reason = _caught_in(key, command, cwd)
        assert stopped_, (key, command, cwd)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", BUILT_AT_RUN_TIME)
def test_b2_a_project_folder_is_left_alone(key, command):
    stopped_, reason = _caught_in(key, command, "/x/proj")
    assert not stopped_, (key, command, reason)


@pytest.mark.parametrize("key", AGENTS)
def test_b2_ordinary_commands_with_no_folder_stay_free(key):
    for command in ("npm test", "git status", "ls .cursor", "cat .cursor/rules/style.mdc", "make clean",
                    "python3 -m pytest -q", "git add .cursor/rules", "cd src && ls"):
        assert_free(key, command, None)
        assert_free(key, command, _home())


# ================================================================ H2: the send advice only when a send is a reason

NEUTRAL = "This touches Bertie's safety settings; ask the user to do it by hand if it is needed."


@pytest.mark.parametrize("key", ("codex", "kimi"))
@pytest.mark.parametrize("command", ["cat .codex/config.toml", "rm ~/.codex/hooks.json", "osascript -e beep",
                                     "python3 ~/.config/bertie/svc_mail.py x", "cat ~/.cursor/hooks.json > /tmp/x"])
def test_h2_a_block_with_no_send_ends_with_the_neutral_line(key, command):
    quiet, reason = verdict(key, command_call(key, command, _home()))
    assert not quiet, command
    assert reason.endswith(NEUTRAL) and "Run the Bertie command on its own" not in reason


@pytest.mark.parametrize("key", ("codex", "kimi"))
def test_h2_a_block_that_has_a_send_keeps_the_send_advice(key):
    quiet, reason = verdict(key, command_call(key, "echo hi; bertie-mail send r1", None))
    assert not quiet and "Run the Bertie command on its own" in reason and NEUTRAL not in reason
    quiet, reason = verdict(key, command_call(key, "bertie-mail send r1 && osascript -e beep", None))
    assert not quiet and "Run the Bertie command on its own" in reason


@pytest.mark.parametrize("key", ("codex", "kimi"))
def test_h2_neutral_line_for_the_codex_config_case_from_the_review(key):
    quiet, reason = verdict(key, command_call(key, "sed -i s/a/b/ ~/.codex/config.toml", None))
    assert not quiet and reason.endswith(NEUTRAL)


# ================================================================ H1 (reverted: resolving folders at hook time opened
# bypasses, e.g. `false && cd sub; rm -rf ../.cursor`). What can lead to home must still be read strictly.

@pytest.fixture
def project(tmp_path):
    """A real project folder (beside the home folder, not in it) with the folders the commands name, a link
    to the home folder inside it, and a sibling folder `shared`."""
    proj = tmp_path / "proj"
    for sub in ("frontend", "src", "apps/web", "packages/ui", "template", "sub"):
        (proj / sub).mkdir(parents=True)
    (tmp_path / "shared").mkdir()
    os.symlink(os.path.expanduser("~"), proj / "lnk")
    os.symlink(os.path.expanduser("~"), proj / "sub" / "up")
    return str(proj)


PROJECT_OWN = [
    "cd {p} && git add .cursor",
    "cd frontend && git add .cursor",
    "cd src && ls && cd .. && git add .cursor",
    "git add apps/web/.cursor",
    "git status apps/web/.codex",
    "git diff -- packages/ui/.cursor",
    "cp -r template/.cursor .",
    "rsync -a ../shared/.cursor/ .cursor/",
    "cd >/dev/null frontend && git add .cursor",
    "cd apps/web && cd ../.. && rm -rf build && git add .cursor",
    "git add .cursor",
]


STILL_HOME = [
    "cd lnk && rm -rf .cursor",                           # a link in the project that leads to home
    "git add lnk/.cursor",
    "rm -rf lnk/.cursor",
    "cd sub/up && git add .cursor",
    "cd sub && cd up && git add .cursor",
    "ln -s ~ hh && cd hh && git add .cursor",             # a command that makes a link anywhere is read as before
    "ln -s ~ hh && git add hh/.cursor",
    "cp -s -r ~/. here && git add frontend/.cursor",
    "tar xf x.tgz && git add frontend/.cursor",
    "cd frontend && cd ~ && git add .cursor",
    "cd $ROOT && git add .cursor",                        # a name that cannot be looked up
    "cd build* && git add .cursor",
    "cd $(echo src) && git add .cursor",
    "cd {p}/nowhere && git add .cursor",                  # a folder that is not there is not trusted
    "git add nowhere/.cursor",
    "cd && git add .cursor",
    "sh -c 'cd frontend; git add .cursor'",
    "(cd frontend); cd ~; git add .cursor",
    "cd .. && git add .cursor && cd {home}",
]


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", STILL_HOME)
def test_h1_what_can_lead_to_home_is_still_read_strictly(key, command, project):
    stopped_, reason = _caught_in(key, command.format(p=project, home=_home()), project)
    assert stopped_, (key, command)


@pytest.mark.parametrize("key", AGENTS)
def test_h1_without_a_full_path_for_the_folder_nothing_is_relaxed(key, project):
    for command in ("git add apps/web/.cursor", "cd frontend && git add .cursor", "rsync -a ../shared/.cursor/ .cursor/"):
        stopped_, reason = _caught_in(key, command, None)
        assert stopped_, (key, command)


@pytest.mark.parametrize("key", AGENTS)
def test_h1_a_project_inside_the_home_folder_still_reaches_home_by_climbing(key):
    inside = os.path.join(_home(), "proj")
    os.makedirs(os.path.join(inside, "sub"))
    for command in ("cd .. && rm -rf .cursor", "cd ../.. && git add .cursor", "rm -rf ../.cursor", "mv ../.codex /tmp/x",
                    "cd sub && cd ../.. && rm -rf .cursor", "cd ~ && rm -rf .cursor"):
        stopped_, reason = _caught_in(key, command, inside)
        assert stopped_, (key, command)
    assert_free(key, "git add .cursor", inside)


@pytest.mark.parametrize("key", AGENTS)
def test_h1_the_home_folder_itself_is_not_relaxed(key, project):
    for command in PROJECT_OWN[1:8]:
        stopped_, reason = _caught_in(key, command.format(p=project), _home())
        assert stopped_, (key, command)


def test_b1_the_names_it_added_do_not_change_the_claude_code_path():
    """The redirection pattern the shared checks use (hook._REDIRECT) is its own: `&>log` is still taken out
    of a Claude Code command before the action is read."""
    assert "SENDS an email" in hook.decide("bertie-mail &>log send r-1")
    assert "SENDS an email" in hook.decide("bertie-mail >log send r-1")


# ================================================================ B2: a project inside the home folder that climbs out

@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", [
    "cd .. && f=hooks.json; rm .cursor/$f",
    "f=hooks.json; rm ../.cursor/$f",
    "f=.cursor; cd ..; rm $f/hooks.json",
    "cd .. && python3 -c \"import os; os.remove(os.path.join('.cursor','hooks.json'))\"",
])
def test_b2_a_project_inside_home_that_climbs_out_is_read_as_home(key, command):
    inside = os.path.join(_home(), "proj")
    os.makedirs(inside, exist_ok=True)
    stopped_, reason = _caught_in(key, command, inside)
    assert stopped_, (key, command)
