"""Fix round 4, from the fourth review of the other-agent work.

G1 the regex that joins the pieces of a path in text going into a file no longer takes twice as long with
every `/ ` (a line of `/ / / ...` held the hook past the agent's time-out, and three of the agents let a
call that times out go), the home-folder backstop is read under the same clock as everything else, and a
line too long to read asks. G2 a bare `.cursor` run from a project folder is the project's own only until
the command changes folder (it may have made a link to the home folder a moment before); the folders above
the home folder, and a link to it, are folders that matter when the agent says a command runs there; a name
with a folder in front of it counts in a command. G3 a folder kept only for the variable or the pattern in
it (`cd $ROOT`, `cd build*`) is no longer copied by every cd that follows, and too many folders to follow
says so in its own words.

HOME is a temporary folder everywhere (the autouse fixture in conftest.py). The folder the home folder is in
and the home folder's name are read from it: no real path is written into a test."""
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time

import pytest

import hook
from agents import antigravity, codex, cursor, kimi
from conftest import KIT, TOOLS
from test_fix_round3 import (AGENTS, PROJECT, SAFE, SHELL_AGENTS, assert_caught, assert_free, caught,
                             command_call, file_call, run_process, timed, verdict)


# ---------------------------------------------------------------- one process, many calls

# The cases that used to take minutes run in a separate process with a time limit, so that a regression
# fails this test instead of hanging the whole run.
BATCH = r'''
import io, json, sys, time
sys.path.insert(0, sys.argv[1])
import hook
jobs = json.loads(sys.stdin.read())
out = []
for key, payload in jobs:
    start = time.monotonic()
    text, code = hook.agent_answer(key, io.StringIO(json.dumps(payload)))
    out.append([key, time.monotonic() - start, text])
print(json.dumps(out))
'''


def run_batch(home, script, jobs, limit=120):
    """Run `script` in a fresh Python with the tools folder on its path; the jobs go in as JSON, the answer
    comes back as JSON."""
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TZ": "UTC"}
    done = subprocess.run([sys.executable, "-I", "-c", script, str(TOOLS)], input=json.dumps(jobs),
                          capture_output=True, text=True, env=env, timeout=limit)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def stopped(key, text):
    """Whether the answer one agent's hook printed stops the call (asks, denies or force-asks)."""
    if not text.strip():
        return False
    answer = json.loads(text)
    if key == "cursor":
        return answer["permission"] != "allow"
    if key == "antigravity":
        return answer["decision"] != "allow"
    return answer["hookSpecificOutput"]["permissionDecision"] in ("deny", "ask")


# ================================================================ G1: no text makes the hook slow

def redos(k):
    """The text that made the hook take 2**k steps: an agent folder's name after a mention of home, then
    `/ ` k times, then something that is not the file's name."""
    return '{"version": 1, "hooks": {}}\n// ~ .cursor' + "/ " * k + "x\n"


@pytest.mark.parametrize("k", [22, 30, 200, 2000])
def test_g1_the_text_that_held_the_hook_is_refused_in_under_a_second_for_every_agent(home, k):
    """Content written to the hooks file itself: a Cursor Write and a Kimi Write are denied, a Codex patch
    is denied, and Antigravity force-asks."""
    path = str(home / ".cursor" / "hooks.json")
    jobs = [[key, file_call(key, path, redos(k))] for key in AGENTS]
    for key, seconds, text in run_batch(home, BATCH, jobs):
        assert stopped(key, text), (key, k, text)
        assert seconds < 1.0, (key, k, seconds)


@pytest.mark.parametrize("k", [22, 30, 200, 2000])
def test_g1_the_same_text_in_an_ordinary_file_is_free_and_quick(home, k):
    jobs = [[key, file_call(key, "/x/p/notes.txt", redos(k))] for key in AGENTS]
    for key, seconds, text in run_batch(home, BATCH, jobs):
        assert not stopped(key, text), (key, k, text)
        assert seconds < 1.0, (key, k, seconds)


@pytest.mark.parametrize("key", AGENTS)
def test_g1_the_slow_text_is_also_quick_in_this_process(key, home):
    (quiet, reason), seconds = timed(verdict, key, file_call(key, str(home / ".cursor" / "hooks.json"), redos(22)))
    assert not quiet and "safety hook is set up" in reason and seconds < 1.0


# Every separator that can sit between two pieces of a path, one or many times, after the first piece of
# each agent path (and after its first two, for the paths of three pieces), then something that is not
# the next piece: the shape that can make a regex try every way of reading a run.
AUDIT = r'''
import json, random, re, sys, time
sys.path.insert(0, sys.argv[1])
import hook

worst, count = {}, [0]


def clock(label, fn, text):
    start = time.monotonic()
    fn(text)
    seconds = time.monotonic() - start
    count[0] += 1
    if seconds > worst.get(label, 0.0):
        worst[label] = seconds


SEPS = ["/ ", ", ", "+ ", ") ", "} ", "' ", '" ', "` ", "/ ' ", "  ", "/", ",", "+", ")", "}", "'", '"', "`", "/'",
        "/ '' ", "/ \t", "\t", " /", "/ \" ", ",' ", ") ' ", "/ / ", "} } "]
HEADS = []
for spec, what in hook._AGENT_PATHS:
    names = [n[0].lstrip("${").rstrip("}") for n in spec]
    for m in range(1, len(names)):
        HEADS.append("/".join(names[:m]))
TAILS = ["x", "", " x", "'x'", "hooks", "hooks.jso"]
lines = []
for head in HEADS:
    for sep in SEPS:
        for k in (22, 30, 200):
            for tail in TAILS:
                lines.append("~ " + head + sep * k + tail)
for line in lines:
    clock("loose_file_hits(structured)", hook.loose_file_hits, line)

rnd = random.Random(4)
atoms = [".cursor", ".codex", ".gemini", "config", "hooks.json", "config.toml", "rules", "antigravity-cli", "settings.json",
         ".kimi-code", ".agents", "skills", "bertie", "~", "$HOME", "x", "kimi_code_home"]
punct = list("/,+)}'\"` \t([")
for _ in range(300):
    parts = ["~ "]
    for _ in range(rnd.randint(40, 500)):
        parts.append(rnd.choice(atoms) if rnd.random() < 0.12 else rnd.choice(punct))
    clock("loose_file_hits(random)", hook.loose_file_hits, "".join(parts))

# every regex added in rounds 2 and 3, on long runs
regexes = [("_JOINED_PATHS[%d]" % i, p) for i, (p, _) in enumerate(hook._JOINED_PATHS)]
for i, (folder, name, _) in enumerate(hook._LOOSE_PAIRS):
    regexes += [("_LOOSE_PAIRS[%d].folder" % i, folder), ("_LOOSE_PAIRS[%d].name" % i, name)]
regexes += [("_home_regex", hook._home_regex()), ("_AGENT_SEGMENTS", hook._AGENT_SEGMENTS),
            ("_AGENT_WORD_BREAK", hook._AGENT_WORD_BREAK)]
for label, rx in regexes:
    for line in lines[::7]:
        clock("regex " + label, rx.search, line)
for text in ("a" * 20000, "a=" * 10000, "A_" + "b." * 10000, "hook.sh" * 3000):
    clock("regex _ASSIGNMENT", hook._ASSIGNMENT.match, text)
print(json.dumps({"worst": worst, "count": count[0]}))
'''


def test_g1_no_line_of_separators_and_no_random_punctuation_takes_a_second(home):
    answer = run_batch(home, AUDIT, [])
    assert answer["count"] > 5000
    slow = {label: seconds for label, seconds in answer["worst"].items() if seconds >= 1.0}
    assert not slow, slow
    assert {"loose_file_hits(structured)", "loose_file_hits(random)"} <= set(answer["worst"])


OLD_JOIN = r"""['"`]?\s*(?:[,/+)}]\s*['"`]?\s*)+"""         # round 3's: the same texts, read in many ways at once


def _joined_with(join):
    return [re.compile(join.join([hook._path_pieces_regex(n) for n in spec[:-1]]
                                 + [hook._path_pieces_regex(spec[-1]) + r"(?![\w-])"]), re.I)
            for spec, _ in hook._AGENT_PATHS]


def test_g1_the_new_join_matches_exactly_what_the_old_one_did():
    """The fix makes the reading unambiguous without changing which texts are joined (so no line that was free
    is stopped now, and none that was stopped is free). Random short texts built from the real names, so the
    old one is not slow and a good share of them are joined."""
    old, new = _joined_with(OLD_JOIN), _joined_with(hook._JOIN)
    rnd = random.Random(11)
    punct = ["/", ",", "+", ")", "}", "'", '"', "`", " ", "  ", "\t", "(", "[", ".", "-", "~"]
    texts = matched = different = 0
    for _ in range(40000):
        spec, _ = rnd.choice(hook._AGENT_PATHS)
        names = [rnd.choice(alternatives) for alternatives in spec]
        if rnd.random() < 0.15:
            names[rnd.randrange(len(names))] = "x"                         # a piece that is not the name
        parts = []
        for name in names:
            parts.append(name)
            parts += [rnd.choice(punct) for _ in range(rnd.randint(0, 4))]
        text = "".join(parts)
        texts += 1
        for before, after in zip(old, new):
            found = bool(before.search(text))
            matched += found
            different += found != bool(after.search(text))
    assert texts == 40000 and matched > 3000
    assert different == 0


@pytest.mark.parametrize("line", [
    "p = os.path.join(root, '.cursor', 'hooks.json')  # same layout as ~/.cursor",     # round 3, by design: stays stopped
    "x = Path.home() / '.codex' / 'hooks.json'",
    "p = os.path.join(os.path.expanduser('~'), '.gemini', 'config', 'hooks.json')",
    "q = \"~\" + \"/.agents\" + \"/skills/\" + 'bertie'",
    "p = Path('.cursor') / 'hooks.json'; home = os.path.expanduser('~')",       # round 3 F3, by design: a home on the line is enough
])
def test_g1_the_joined_forms_that_were_stopped_still_are(line):
    assert hook.loose_file_hits(line)


@pytest.mark.parametrize("line", [
    "Cursor reads .cursor; hooks.json is separate; the home folder is ~",
    "# see ~/.cursor for more\nname = os.path.join('.cursor', 'hooks.json')",     # the home folder is on another line
    "p = os.path.join(root, '.cursor', 'hooks.json')",
])
def test_g1_the_lines_that_were_free_still_are(line):
    assert hook.loose_file_hits(line) == []


# ---- the backstop is read under the clock

class Slow:
    """A pattern that takes its time, to show that the clock is looked at between lines."""

    def search(self, line):
        time.sleep(0.02)


def test_g1_the_file_backstop_looks_at_the_clock_on_every_line(monkeypatch):
    monkeypatch.setattr(hook, "_JOINED_PATHS", ((Slow(), "slow"),))
    monkeypatch.setattr(hook, "_PATTERN_SECONDS", 0.1)
    hits, seconds = timed(hook.loose_file_hits, "\n".join(["~ .cursor/x"] * 500))        # 10 s if it were not watched
    assert hits == [hook._LOOSE_SLOW] and SAFE in hits[0]
    assert seconds < 1.0


@pytest.mark.parametrize("text", ["", "ls", "Nothing here.", "~ .cursor/hooks.json"])
def test_g1_with_no_time_left_the_file_backstop_asks(monkeypatch, text):
    monkeypatch.setattr(hook, "_PATTERN_SECONDS", 0)
    assert hook.loose_file_hits(text) == [hook._LOOSE_SLOW]


@pytest.mark.parametrize("command", ["ls", "echo ~", "rm -rf ~/.cursor", "git status"])
def test_g1_with_no_time_left_the_command_backstop_asks(monkeypatch, command):
    monkeypatch.setattr(hook, "_PATTERN_SECONDS", 0)
    assert hook.loose_command_hits(command) == [hook._LOOSE_SLOW]


def test_g1_the_command_backstop_looks_at_the_clock_between_its_pairs(monkeypatch):
    monkeypatch.setattr(hook, "_LOOSE_PAIRS", tuple((Slow(), Slow(), "slow") for _ in range(100)))
    monkeypatch.setattr(hook, "_PATTERN_SECONDS", 0.1)
    hits, seconds = timed(hook.loose_command_hits, "rm ~/x")
    assert hits == [hook._LOOSE_SLOW] and seconds < 1.0


def test_g1_with_time_to_spare_the_backstops_say_what_they_said_before(monkeypatch):
    assert hook.loose_file_hits("p = Path.home() / '.codex' / 'hooks.json'")
    assert hook.loose_command_hits("rm $HOME/.codex/hooks.json")
    assert hook.loose_command_hits("ls ~/.cursor") == []
    assert hook.loose_file_hits("nothing about home here") == []


# ---- a line too long to read asks

LONG = hook._MAX_COMMAND


@pytest.mark.parametrize("key", AGENTS)
def test_g1_a_line_over_the_limit_that_mentions_home_asks_to_be_safe(key):
    line = "~ " + "x" * LONG
    assert len(line) > LONG
    quiet, reason = verdict(key, file_call(key, "/x/p/big.txt", "before\n" + line + "\nafter"))
    assert not quiet and SAFE in reason and "too long to check" in reason and str(LONG) in reason


@pytest.mark.parametrize("key", AGENTS)
def test_g1_a_line_at_the_limit_is_read_and_a_long_line_with_no_home_is_not_a_reason(key):
    at_limit = "~ " + "x" * (LONG - 2)
    assert len(at_limit) == LONG
    assert verdict(key, file_call(key, "/x/p/big.txt", at_limit)) == (True, None)
    assert verdict(key, file_call(key, "/x/p/big.txt", "x " * (LONG * 2))) == (True, None)


def test_g1_a_long_line_is_one_reason_and_the_short_lines_round_it_are_still_read():
    long_line = "~ " + "x" * (LONG + 5)
    hits = hook.loose_file_hits("\n".join([long_line, "p = Path.home() / '.codex' / 'hooks.json'", long_line]))
    assert len([h for h in hits if "too long" in h]) == 2 and any("Codex's hooks file" in h for h in hits)


@pytest.mark.parametrize("key", AGENTS)
def test_g1_the_slow_text_with_a_long_tail_is_quick(key):
    text = "// ~ .cursor" + "/ " * (LONG // 2 - 10) + "x"
    (quiet, reason), seconds = timed(verdict, key, file_call(key, "/x/p/notes.txt", text))
    assert quiet and seconds < 1.0


def test_g1_the_claude_code_path_does_not_run_the_backstops(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("the Claude Code path must not run the backstops")

    monkeypatch.setattr(hook, "loose_file_hits", fail)
    monkeypatch.setattr(hook, "loose_command_hits", fail)
    assert hook.decide_tool("Write", {"file_path": "/x/a.py", "content": redos(30)}) is None
    assert hook.decide_tool("Bash", {"command": "rm -rf ~/.cursor"}) is None


# ================================================================ G2: a cd may have gone to the home folder

def parent_of(home):
    return str(home.parent)


def user_of(home):
    return home.name


@pytest.fixture
def link_to_home(tmp_path, home):
    link = tmp_path / "shortcut"
    link.symlink_to(home)
    return str(link)


# `ln -sfn ~ /tmp/hh` makes a link to the home folder; the cd that follows goes there, and a bare name after it
# is the home folder's. The project folder was given as a full path, which used to make every bare name the
# project's own, whatever the command had done before.
LINK_THEN_CD = [
    "ln -sfn ~ /tmp/hh && cd /tmp/hh && rm -rf .cursor",
    "ln -sfn ~ /tmp/hh && cd /tmp/hh/. && rm -rf .codex",
    "ln -sfn ~ /tmp/hh; cd /tmp/hh; mv .gemini/config /tmp/g",
    "ln -sfn ~ /tmp/hh; cd /tmp/hh; rm -rf .agents/skills",
    "ln -sfn $HOME /tmp/hh && pushd /tmp/hh && rm -rf .cursor",
    "ln -sfn ~ /tmp/hh && chdir /tmp/hh && rm -rf .codex",
    "ln -s ~ hh && cd hh && rm -rf .cursor",
    "cd src && rm -rf .cursor",
    "cd ../other && mv .codex x",
    "cd -P /tmp/hh && rm -rf .gemini",
    "(cd /tmp/hh && rm -rf .cursor)",
    "sh -c 'cd /tmp/hh; rm -rf .cursor'",
    "cd /tmp/hh\nrm -rf .cursor",
    "cd /tmp/hh && git rm -r --cached .codex",
    "cd /tmp/hh && rm -rf .cursor/hooks.json",
]


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", LINK_THEN_CD)
def test_g2_a_name_after_a_cd_is_judged_as_before_the_project_folder_was_given(key, command):
    assert_caught(key, command, PROJECT)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", LINK_THEN_CD[:4])
def test_g2_the_same_with_no_folder_given_is_still_caught(key, command):
    assert_caught(key, command)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("content", [
    "#!/bin/sh\nln -sfn ~ /tmp/hh\ncd /tmp/hh\nrm -rf .cursor\n",
    "ln -sfn ~ /tmp/hh && cd /tmp/hh && rm -rf .codex\n",
    "ln -sfn ~ /tmp/hh; cd /tmp/hh/.; mv .gemini/config /tmp/g\n",
    "cd src\nrm -rf .cursor\n",
    "cd /tmp/hh\ngit rm -r --cached .codex\n",
])
def test_g2_the_same_in_text_going_into_a_file(key, content):
    quiet, reason = verdict(key, file_call(key, "/x/p/s.sh", content))
    assert not quiet and "safety check" in reason, (key, content, reason)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", ["cd . ; rm -rf .cursor", "cd - ; rm -rf .codex", "cd .; cd ./.; git add .cursor",
                                     "cd -P . && rm -rf .gemini", "pushd; rm -rf .cursor", "popd; mv .codex x"])
def test_g2_a_cd_that_goes_nowhere_leaves_the_projects_own_names_alone(key, command):
    assert_free(key, command, PROJECT)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", [
    "git add .cursor", "rm -rf .gemini", "tar czf cfg.tgz .cursor .codex", "git rm -r --cached .codex",
    "mkdir -p .cursor && cp template/hooks.json .cursor/", "ls cd .cursor",
])
def test_g2_with_no_cd_in_the_command_the_projects_own_names_stay_free(key, command):
    assert_free(key, command, PROJECT)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", ["rm -rf x/.cursor", "mv sub/.codex /tmp/x", "rm -rf a/b/.gemini/config", "mv x/.agents/skills y"])
def test_g2_a_relative_name_with_a_folder_in_front_counts_in_a_command(key, command):
    assert_caught(key, command, PROJECT)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("command", ["ls x/.cursor", "cat x/.codex/notes.md", "grep -r hooks x/.cursor", "echo x/.gemini"])
def test_g2_a_relative_name_with_a_folder_in_front_that_only_looks_is_free(key, command):
    assert_free(key, command, PROJECT)


@pytest.mark.parametrize("key", AGENTS)
def test_g2_a_relative_name_with_a_folder_in_front_in_a_file_is_prose_and_free(key):
    quiet, reason = verdict(key, file_call(key, "/x/p/setup.sh", "#!/bin/sh\ncp -r template/.cursor .\nrm -rf build/.codex\n"))
    assert quiet, reason


# ---- the folder the agent says the command runs in

@pytest.mark.parametrize("key", AGENTS)
def test_g2_the_folder_above_home_makes_the_user_folder_the_home_folder(key, home):
    assert_caught(key, "rm -rf " + user_of(home) + "/.cursor", parent_of(home))
    assert_caught(key, "mv " + user_of(home) + "/.codex /tmp/x", parent_of(home))
    assert_caught(key, "rm -rf .cursor", str(home))
    assert_caught(key, "cd " + user_of(home) + " && rm -rf .cursor", parent_of(home))


@pytest.mark.parametrize("key", AGENTS)
def test_g2_from_the_root_the_whole_path_down_to_home_is_the_home_folder(key, home):
    assert_caught(key, "rm -rf " + str(home).lstrip("/") + "/.cursor", "/")
    assert_caught(key, "mv " + str(home).lstrip("/") + "/.gemini/config /tmp/g", "/")
    assert_caught(key, "rm -rf " + str(home.parent).lstrip("/") + "/" + user_of(home) + "/.agents/skills", "/")


@pytest.mark.parametrize("key", AGENTS)
def test_g2_a_link_to_the_home_folder_is_the_home_folder(key, link_to_home):
    assert_caught(key, "rm -rf .cursor", link_to_home)
    assert_caught(key, "mv .codex /tmp/x", link_to_home)
    assert_caught(key, "rm -rf .gemini/config", link_to_home)
    assert_caught(key, "mv .agents/skills /tmp/s", link_to_home)


@pytest.mark.parametrize("key", AGENTS)
def test_g2_a_link_to_the_home_folder_in_a_folder_given_with_dots(key, link_to_home):
    assert_caught(key, "rm -rf .cursor", link_to_home + "/.")
    assert_caught(key, "rm -rf .cursor", link_to_home + "/")


@pytest.mark.parametrize("key", AGENTS)
def test_g2_a_name_that_only_looks_is_free_in_the_folders_that_matter(key, home, link_to_home):
    for cwd in (str(home), parent_of(home), "/", link_to_home):
        assert_free(key, "ls .cursor", cwd)
        assert_free(key, "cat .codex/config.toml.bak", cwd)


def test_g2_codex_reads_the_folder_from_the_call_and_from_the_tool_input(home, link_to_home):
    for fields in ({"workdir": user_of(home)}, {"cwd": user_of(home)}):
        payload = command_call("codex", "rm -rf .cursor", parent_of(home))
        payload["tool_input"].update(fields)
        quiet, reason = verdict("codex", payload)
        assert not quiet and "safety check" in reason, (fields, reason)
    payload = command_call("codex", "rm -rf .cursor")
    payload["tool_input"]["workdir"] = link_to_home
    assert not verdict("codex", payload)[0]
    payload = command_call("codex", "git add .cursor")
    payload["tool_input"]["workdir"] = PROJECT
    assert verdict("codex", payload)[0]


def test_g2_codex_a_workdir_that_is_a_relative_name_could_be_anywhere(home):
    payload = command_call("codex", "rm -rf .cursor", PROJECT)
    payload["tool_input"]["workdir"] = "sub"
    assert not verdict("codex", payload)[0]


def test_g2_kimi_reads_its_own_folder_field_and_the_sessions(home, link_to_home):
    payload = command_call("kimi", "rm -rf .cursor", PROJECT)
    payload["tool_input"]["cwd"] = link_to_home
    assert not verdict("kimi", payload)[0]
    payload = command_call("kimi", "rm -rf " + user_of(home) + "/.codex", parent_of(home))
    assert not verdict("kimi", payload)[0]
    payload = command_call("kimi", "git add .cursor", PROJECT)
    payload["tool_input"]["cwd"] = PROJECT
    assert verdict("kimi", payload)[0]


# ---- cwd_prefix and the folders behind it

def test_g2_the_folders_above_home_are_folders_that_matter(home):
    for folder in (str(home), parent_of(home), "/"):
        assert hook._cwd_matters(folder, "ls"), folder
        assert hook.cwd_prefix(folder, "ls") == "cd " + shlex.quote(folder) + " && "
    assert hook.cwd_prefix(PROJECT, "ls") == ""
    assert hook.cwd_prefix(str(home / "proj"), "ls") == ""
    assert hook.cwd_prefix(str(home / "proj"), "rm -rf ../.cursor") == ""      # `..` is read in the command itself


def test_g2_a_link_to_home_is_put_in_front_as_the_folder_it_leads_to(home, link_to_home):
    assert hook._cwd_folder(link_to_home, "ls") == os.path.realpath(home)
    assert hook.cwd_prefix(link_to_home, "rm -rf .cursor") == "cd " + shlex.quote(os.path.realpath(home)) + " && "
    assert hook._cwd_folder(str(home), "ls") == str(home)                  # a folder that matters as written is kept as written
    assert hook._cwd_folder(PROJECT, "ls") is None


def test_g2_a_folder_that_is_not_a_full_path_or_not_text_is_as_before(home):
    assert hook._cwd_folder("sub", "ls") == "sub"
    assert hook.cwd_prefix("sub", "ls") == "cd sub && "
    assert hook.cwd_prefix("", "ls") == ""
    with pytest.raises(ValueError):
        hook.cwd_prefix(7, "ls")


def test_g2_a_folder_with_spaces_or_quotes_is_quoted_in_the_prefix(tmp_path):
    odd = tmp_path / "it's mine" / "home2"
    odd.mkdir(parents=True)
    link = tmp_path / "my link"
    link.symlink_to(os.environ["HOME"])
    prefix = hook.cwd_prefix(str(link), "ls")
    assert prefix.startswith("cd ") and prefix.endswith(" && ")
    assert shlex.split(prefix[:-4])[1] == os.path.realpath(os.environ["HOME"])


def test_g2_folder_given_and_hook_cwd_known_say_so_only_until_the_first_cd():
    assert hook.folder_given(PROJECT) and not hook.folder_given("sub", None, 3)
    text = "cd src\nrm -rf .cursor"
    assert hook.agent_config_hits(text, True, True)                       # cwd_known, but a cd came first
    assert hook.agent_config_hits("rm -rf .cursor", True, True) == []
    assert hook.agent_config_hits("rm -rf .cursor", True, False)
    assert hook.agent_config_hits("cd .; rm -rf .cursor", True, True) == []


def test_g2_follow_cd_says_whether_it_went_anywhere():
    homes, known = _homes_and_known()
    for words, moved in ((["cd", "src"], True), (["cd", "."], False), (["cd", "-"], False), (["pushd"], False),
                         (["cd"], True), (["cd", "-P", "."], False), (["cd", "-P", "x"], True), (["pushd", "x"], True),
                         (["cd", "~"], True), (["cd", "$X"], True)):
        _arm()
        assert hook._follow_cd(words, words[0], [], homes, known) is moved, words


def _arm():
    hook._deadline[0] = time.monotonic() + 5


def _homes_and_known():
    specs = hook._agent_specs()
    known = {n for spec, _, _, _ in specs for names in spec for n in names}
    return [("~",), ("$home",), ("${home}",)] + hook._home_pieces(), known


def test_g2_the_words_after_a_wrapped_cd_count_too():
    """`sh -c 'cd /tmp/x; rm -rf .cursor'` is one segment's words, not a cd of its own."""
    assert hook.agent_config_hits("sh -c cd /tmp/hh; rm -rf .cursor", True, True)
    assert hook.agent_config_hits("echo hello; rm -rf .cursor", True, True) == []


# ================================================================ G3: a kept-for-its-pattern folder is not copied

README_CD = "# Project\n\n```\ncd $PROJECT\n```\n\n" + "".join(
    "## Step %d\n\n```\ncd step%d\nmake\ncd ..\n```\n\n" % (i, i) for i in range(12))


@pytest.mark.parametrize("key", AGENTS)
def test_g3_a_readme_with_a_variable_cd_and_twelve_sections_of_cds_is_free(key):
    quiet, reason = verdict(key, file_call(key, "/x/p/README.md", README_CD))
    assert quiet, (key, reason)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("head", ["cd $ROOT", "cd build*", "cd .codex-tools*", "cd $HOME/proj", "cd ${ROOT}/src", "cd ./out?"])
@pytest.mark.parametrize("cwd", [None, PROJECT])
def test_g3_eight_plain_cds_after_a_variable_or_a_pattern_are_free(key, head, cwd):
    command = head + "; " + "; ".join("cd d%d" % i for i in range(8)) + "; make"
    assert_free(key, command, cwd)


@pytest.mark.parametrize("key", AGENTS)
def test_g3_a_much_longer_run_of_cds_after_a_variable_is_free_too(key):
    assert_free(key, "cd $ROOT; " + "; ".join("cd d%d && make && cd .." % i for i in range(60)), PROJECT)


@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("cwd", [None, PROJECT])
@pytest.mark.parametrize("command", [
    "cd $X; cd .cursor && rm hooks.json",
    "cd $X; cd .codex; sed -i x config.toml",
    "cd build*; cd .gemini/config; rm hooks.json",
    "cd $X; cd ..; rm -rf .cursor",
    "cd $X; cd src; cd .cursor; rm hooks.json",
    "cd $ROOT/.cursor; rm hooks.json",
    "cd $ROOT; cd .agents/skills; rm -rf bertie",
    "cd $X; cd .c*; rm hooks.json",
])
def test_g3_a_variable_or_pattern_followed_by_a_cd_that_could_name_an_agent_folder_is_still_caught(key, cwd, command):
    assert_caught(key, command, cwd)


def test_g3_the_folder_kept_for_its_pattern_picks_up_only_a_climb_or_an_agent_name():
    homes, known = _homes_and_known()

    def states_after(*commands):
        states = []
        for command in commands:
            _arm()
            words = command.split()
            hook._follow_cd(words, words[0], states, homes, known)
        return states

    assert states_after("cd $ROOT", "cd d0", "cd d1", "cd d2", "cd d3") == [["$root"]]
    assert states_after("cd build*", "cd d0", "cd d1") == [["build*"]]
    after_up = states_after("cd $ROOT", "cd ..")
    assert ["$root"] in after_up and [".."] in after_up
    after_name = states_after("cd $ROOT", "cd .cursor")
    assert ["$root"] in after_name and ["$root", ".cursor"] in after_name and [".cursor"] in after_name
    assert ["$root", ".c*"] in states_after("cd $ROOT", "cd .c*")
    assert ["$root", "src", ".codex"] in states_after("cd $ROOT", "cd src/.codex")


def test_g3_a_folder_with_an_agent_name_in_it_is_still_copied_by_a_later_cd():
    """Only the folders kept for their pattern are left alone: ~/.cursor and the folders above home keep working."""
    homes, known = _homes_and_known()
    states = []
    for command in ("cd ~/.cursor", "cd d0", "cd d1"):
        _arm()
        words = command.split()
        hook._follow_cd(words, words[0], states, homes, known)
    assert ["~", ".cursor", "d0", "d1"] in states
    assert hook.agent_config_hits("cd ~/.cursor; cd d0; cd d1; rm hooks.json", True, True)


def test_g3_the_helpers_say_what_a_piece_could_be():
    homes, known = _homes_and_known()
    assert hook._could_name_an_agent_folder("..", known)
    assert hook._could_name_an_agent_folder(".cursor", known)
    assert hook._could_name_an_agent_folder("$root", known)
    assert hook._could_name_an_agent_folder(".c*", known)
    assert hook._could_name_an_agent_folder("*", known)
    assert hook._could_name_an_agent_folder(".cu?sor", known)
    assert not hook._could_name_an_agent_folder("src", known)
    assert not hook._could_name_an_agent_folder("build*", known)
    assert not hook._could_name_an_agent_folder(".codex-tools*", known)
    assert hook._kept_for_a_pattern_only(["$root"], homes, known)
    assert hook._kept_for_a_pattern_only(["build*"], homes, known)
    assert not hook._kept_for_a_pattern_only(["~"], homes, known)
    assert not hook._kept_for_a_pattern_only([".."], homes, known)
    assert not hook._kept_for_a_pattern_only(["$root", ".cursor"], homes, known)


# ---- the cap says what it is

DEEP = "cd ~/.cursor; " + "; ".join("cd d%d" % i for i in range(10))


@pytest.mark.parametrize("key", AGENTS)
def test_g3_more_folders_than_can_be_followed_ask_with_their_own_reason(key):
    quiet, reason = verdict(key, command_call(key, DEEP))
    assert not quiet and "too many folders to follow; asking to be safe" in reason, (key, reason)
    assert "could not finish in time" not in reason


def test_g3_the_cap_reason_is_given_once_and_is_not_the_time_reason():
    hits = hook.agent_config_hits(DEEP, True, False)
    assert hits.count("too many folders to follow; asking to be safe") == 1
    assert not any("in time" in h for h in hits)
    assert issubclass(hook._TooManyFolders, hook._OutOfTime)
    assert hook._MAX_FOLDER_STATES == 64


@pytest.mark.parametrize("key", AGENTS)
def test_g3_running_out_of_time_still_says_so(monkeypatch, key):
    monkeypatch.setattr(hook, "_PATTERN_SECONDS", 0)
    quiet, reason = verdict(key, command_call(key, "ls"))
    assert not quiet and "could not finish in time" in reason and "too many folders" not in reason


# ================================================================ G4: the false blocks of round 3 stay

@pytest.mark.parametrize("key", AGENTS)
@pytest.mark.parametrize("content", [
    "p = os.path.join(root, '.cursor', 'hooks.json')  # same layout as ~/.cursor\n",
    "q = Path('.codex') / 'config.toml'  # ~ is where the real one is\n",
])
def test_g4_a_path_built_up_on_a_line_that_names_home_is_still_stopped(key, content):
    quiet, reason = verdict(key, file_call(key, "/x/p/gen.py", content))
    assert not quiet and "safety hook is set up" in reason, (key, reason)


@pytest.mark.parametrize("key", AGENTS)
def test_g4_the_same_path_with_no_mention_of_home_is_free(key):
    quiet, reason = verdict(key, file_call(key, "/x/p/gen.py", "p = os.path.join(root, '.cursor', 'hooks.json')\n"))
    assert quiet, reason


# ================================================================ the Claude Code path is not touched

CLAUDE_CASES_4 = ["ls", "git add .cursor", "rm -rf ~/.cursor", "cd $ROOT; cd .cursor && rm hooks.json", "cd src && rm -rf .cursor",
                  "ln -sfn ~ /tmp/hh && cd /tmp/hh && rm -rf .codex", "cd ~/.cursor; " + "; ".join("cd d%d" % i for i in range(10))]


def test_g_claude_code_path_never_runs_the_new_agent_helpers(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("the Claude Code path must not run the agent checks")

    for name in ("agent_config_hits", "loose_command_hits", "loose_file_hits", "_agent_path_hits", "_follow_cd",
                 "_cwd_folder", "_cwd_matters", "cwd_prefix", "_kept_for_a_pattern_only", "_could_name_an_agent_folder"):
        monkeypatch.setattr(hook, name, fail)
    for command in CLAUDE_CASES_4:
        assert hook.decide_tool("Bash", {"command": command}) is None, command


@pytest.mark.parametrize("command", CLAUDE_CASES_4)
def test_g_claude_code_path_as_a_process_with_no_arguments(home, command):
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TZ": "UTC"}
    done = subprocess.run([sys.executable, "-I", str(KIT / "tools" / "hook.py")], capture_output=True, text=True, env=env, timeout=60,
                          input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))
    assert done.returncode == 0 and done.stdout.strip() == ""
