"""The PreToolUse hook: what it asks about, what it leaves alone, what it prints."""
import importlib
import json
import subprocess
import sys

import pytest

import hook
import registry
from conftest import TOOLS

GATED = [
    "bertie-mail send r123",
    "bertie-mail trash 18c0",
    "bertie-mail untrash 18c0",
    "bertie-calendar create --title x --start 2026-10-15T10:00 --end 2026-10-15T11:00",
    "bertie-calendar move ev1 --start a --end b",
    "bertie-calendar cancel ev1",
    "bertie-chat send spaces/AAA hello",
    "bertie-chat dm someone@test.invalid hello",
    "bertie-drive upload ./f.pdf",
    "bertie-drive update FILEID ./f.pdf",
    "bertie-drive share FILEID someone@test.invalid --role reader",
    "bertie logout",
    "bertie uninstall --yes",
]

COMPOUND = [
    "cd /tmp && bertie-mail send r123",
    "echo hi; bertie-mail send r123",
    "ls | xargs bertie-chat dm a@test.invalid hi",
    "echo $(bertie-calendar cancel ev1)",
    "echo `bertie-calendar cancel ev1`",
    "sh -c 'bertie-mail trash 123'",
    'bash -lc "bertie-drive share F a@test.invalid --role writer"',
    "env X=1 bertie-calendar cancel ev1",
    "FOO=bar bertie-chat send spaces/A hi",
    "true || bertie-mail send r1",
    "(bertie-mail send r1)",
    "bertie-mail --json send r1",
    "~/.local/bin/bertie-chat send spaces/A hi",
    "$HOME/.local/bin/bertie-drive upload x",
    "command bertie-mail send r1",
    'bertie-mail "se""nd" r1',
    "bertie-mail 'send' r1",
    "bertie-mail se\\nd r1",
    "bertie-mail \\\n send r1",
    "nohup bertie-calendar create --title t --start a --end b &",
    # the review's bypass strings
    "BERTIE-MAIL send r-1",
    "Bertie-Mail send r-1",
    "bertie-mail SEND r-1",
    "bertie-mail 2>/dev/null send r-1",
    "bertie-mail </dev/null send r-1",
    "bertie-mail >out send r-1",
    "bertie-mail 2>&1 send r-1",
    "bertie-mail &>log send r-1",
    "bertie-chat 1>/dev/null dm a@test.invalid hi",
    "echo send r-1 | xargs bertie-mail",
    "echo r-1 | xargs -n1 bertie-mail send",
    "find . -name x -exec bertie-mail send r-1 \\;",
    "parallel bertie-mail ::: send",
]

UNREADABLE = [
    "bertie-mail $ACTION r1",
    "bertie-mail $(echo send) r1",
    "bertie-drive ${A} f",
    "bertie-mail",
    "bertie-chat 2>&1",
    "bertie-drive >/dev/null",
]

BYPASS = [
    "$HOME/.config/bertie/.venv/bin/python $HOME/.config/bertie/svc_mail.py send r1",
    "~/.config/bertie/.venv/bin/python -c 'print(1)'",
    "python3 ~/.config/bertie/svc_drive.py share F a@test.invalid --role reader",
    "cd ~/.config/bertie && ./.venv/bin/python svc_mail.py send r1",
    "cd ~/.config/bertie; python3 -c 'import auth'",
    "python3 svc_calendar.py cancel e1",
    "python3 google_cli.py logout",
    "cat ~/.config/bertie/client_secret.json",
    "ls ~/.config/bertie",
    "rm -rf ~/.config/bertie",
    "cat $HOME/.config/bertie/hook.py",
    # the review's module-use strings
    "python3 -c \"import sys; sys.path.insert(0, '/x'); import svc_mail; svc_mail.main(['send', 'r-1'])\"",
    "$HOME/.config/bertie/.venv/bin/python -m svc_mail send r-1",
    ".venv/bin/python -m svc_mail send r-1",
    "python3 -m svc_drive share F a@test.invalid --role reader",
    "python3 -c 'import auth; auth.credentials()'",
    "python3 -c 'import registry'",
    "python3 -m google_cli logout",
    "python3 -c 'from common import confirm'",
    "ls ~/.CONFIG/BERTIE",
    "cat ~/.config/berti*/client_secret.json",
    "ls ~/.config/b*/*",
    "ls ~/.config/*/approve.sha256",
    "rm -rf ~/.config/bertie",
    "cd ~/.config && cat bertie/client_secret.json",
    "cd ~/.config; ls berti*/",
    "find ~ -path *bertie/* -exec cat {} +",
    "ls ~/.config/*/common.py",
    "chmod u+w ~/.c*/bertie/common.py",
    "cp /tmp/c.py ~/.c*/bertie/common.py",
    "echo x >> ~/.c*/bertie/hook.py",
    "sed -i s/x/y/ $HOME/.[c]onfig/bertie/common.py",
    "chmod u+w ~/.conf?g/bertie/common.py",
    "cat $XDG_CONFIG_HOME/bertie/common.py",
    "cat ./bertie/client_secret.json",
    "ln -s ~/.c*/bertie /tmp/k",
    "{bertie,logout}",
    "{bertie,uninstall} --yes",
    "{,bertie} logout",
    "~/.local/bin/berti? logout",
    "~/.local/bin/[b]ertie uninstall --yes",
    "~/.local/bin/bertie-ma? send d1",
    "skills=~/.config; chmod u+w $skills/bertie/common.py",
    "skills=$HOME/.config; cat $skills/bertie/client_secret.json",
    "skills=~/.config; ln -s $skills/bertie /tmp/k",
    "cd ~/.config && ln -s . skills && chmod u+w skills/bertie/common.py",
    "cd ~/.config && cat skills/bertie/client_secret.json",
    "mkdir -p /tmp/x/.claude && ln -s ~/.config /tmp/x/.claude/skills && cp /tmp/c.py /tmp/x/.claude/skills/bertie/common.py",
    "~/.local/bin/*ie uninstall --yes",
    "~/.local/bin/b*e logout",
    "~/.local/bin/b????? logout",
    "be{r..r}tie uninstall --yes",
    "~/.local/bin/[^a]ertie logout",
    "~/.local/bin/[[:alpha:]]ertie logout",
    "X=1 ~/.local/bin/b* logout",
    "env ~/.local/bin/b????? logout",
    "exec ~/.local/bin/b????? logout",
    "sudo ~/.local/bin/b????? logout",
    "if true; then ~/.local/bin/b????? logout; fi",
    ">/dev/null ~/.local/bin/b????? logout",
    "for f in ~/.local/bin/b?????; do $f logout; done",
    "bash -c ~/.local/bin/b?????",
    "~/.local/bin/[[:alpha:][:digit:]]ertie logout",
    "~/.local/bin/[b[:digit:]]ertie logout",
    "~/.local/bin/[![:digit:]]ertie logout",
    "~/.local/bin/{b{e,x},y}rtie logout",
    "~/.local/bin/b@(e)rtie logout",
    "~/.local/bin/b?(e)rtie logout",
    "{~/.local/bin/b?????,logout}",
    "~/.local/bin/[[.b.]]ertie logout",
    "~/.local/bin/[[=b=]]ertie logout",
    "~/.local/bin/b@(e@(r))tie logout",
    "~/.local/bin/[{b,x}]e* logout",
    "~/.local/bin/(b)ertie-mail send r1",
    "bertie-ma(i)l send r1",
    "~/.local/bin/b(e)rtie logout",
    "echo {bertie,logout}",
    "env ~/.local/bin/* logout",
    "ls b*",
    'y=$(~/.local/bin/b[e\\ xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx]rtie-mail send r1)',
    'echo [ ; ~/.local/bin/b[e\\ xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx]rtie-mail send r1',
    'y=$(~/.local/bin/(b|xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx)ertie-mail send r1)',
    'y=$(~/.local/bin/@(b|xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx)ertie-mail send r1)',
    "echo '[' ; ~/.local/bin/(b|xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx)ertie-mail send r1 # ]",
    'echo [ ; ~/.local/bin/b[]e\\ ]rtie-mail send r1',
    'cd ~/.l?cal/b?n$(true) && ./?????? logout',
    'cd ~/.l?cal/b?$(true)n && ./?????? logout',
    'cd ~/.l?cal/b?n`true` && ./?????? logout',
    'cd $(true)~/.l?cal/b?n && ./?????? logout',
    'find ~/.local/bin -exec env {} logout \\;',
    'find ~/.local/bin -exec /usr/bin/env {} logout \\;',
    'find ~/.local/bin -type f -exec nohup {} logout \\;',
    'find ~/.local/bin -exec sh {} logout \\;',
    'find ~/.local/bin -exec {,}{} logout \\;',
    "~/.local/bin/bertie-mail s$''end r1",
    "~/.local/bin/b?rti$''e-mail send r1",
    '~/.local/bin/bertie-mail s$""end r1',
    "$'\\x62'ertie logout",
    'y=$(~/.local/bin/b[e\\ ]rtie-mail send r1); echo "$y"',
    'echo x$(~/.local/bin/b[e\\ ]rtie-mail send r1)',
    "echo '[' ; ~/.local/bin/b[e\\ ]rtie-mail send r1",
    'echo [ ; ~/.local/bin/b["e "]rtie-mail send r1',
    'y=$(~/.local/bin/(b|q)ertie-mail send r1); echo "$y"',
    'echo x$(~/.local/bin/(b|q)ertie-mail send r1)',
    "echo '[' ; ~/.local/bin/(b|q)ertie-mail send r1 # ]",
    'cd -P ~/.l?cal/b?n && ./?????? logout',
    'cd -- ~/.l?cal/b?n && ./?????? logout',
    'cd -L ~/.l?cal/b?n; ./?????? logout',
    'pushd -q ~/.l?cal/b?n && ./?????? logout',
    'chdir ~/.l?cal/b?n && ./?????? logout',
    'find ~/.local/bin -exec {} logout \\;',
    'find ~/.l?cal/b?n -exec {} logout \\;',
    "find ~/.l?cal/b?n -name '??????' -exec {} logout \\;",
    'ls ~/.l?cal/b?n | xargs -I{} {} logout',
    '~/.local/b?n/?????? logout',
    '~/.local/*/?????? logout',
    '~/.l*/b*/?????? logout',
    'cd ~/.local/b?n && ./?????? logout',
    'cd ~/.l*/b?n && ./?????? logout',
    'x=$({~/.local/bin/b?????-mail,send,r1})',
    'echo $({~/.local/bin/b?????-mail,send,r1})',
    '$(~/.local/bin/b?????-mail)',
    '~/.local/bin/@(b|q)ertie-mail send r1',
    '~/.local/bin/(b|q)ertie-mail send r1',
    '~/.local/bin/(zz|b?rt|qq)ie-mail send r1',
    '~/.local/bin/b[{e]rtie-m{a..a}il send r1',
    '~/.local/bin/b[e\\ ]rtie-mail send r1',
    '~/.local/bin/b["e "]rtie-mail send r1',
    "~/.local/bin/b[e'|']rtie-mail send r1",
    "~/.local/bin/b[e'<']rtie-mail send r1",
]

KEYCHAIN = [
    "security find-generic-password -s bertie -w",
    'security find-generic-password -s "bertie" -a x@test.invalid -w',
    "python3 -c \"import keyring; print(keyring.get_password('bertie','x'))\"",
    "security delete-generic-password -s bertie",
    "security find-generic-password -s bertie-goo -w",
    "security find-generic-password -s $(echo bert)ie -a x -w; echo bertie",
    "security find-generic-password -s BERTIE -w",
    "python3 -c \"import keyring; print(keyring.get_password('ber'+'tie','x'))\"",
    "python3 -c 'import keyring'",
]

FREE = [
    "ls -la",
    "git status",
    "echo hello",
    "ls *.py",
    "find . -name '*.pyc' -exec rm {} +",
    "find . -name '*.pyc' -exec rm {} \\;",
    'ls | xargs -I{} echo {}',
    "awk '{print}' file.txt",
    'python3 -c "d = {}; print(d)"',
    'f() { echo hi; }',
    'echo *',
    'ls src/*',
    'grep -rn TODO *',
    'for f in *; do echo $f; done',
    'git stash show stash@{0}',
    'echo ${HOME}',
    'cp a{,.bak}',
    '[[ -f x ]] && echo ok',
    'echo $(date)',
    'find . -exec grep -l x {} +',
    'cd $(git rev-parse --show-toplevel) && ls *',
    'cd "$(dirname "$0")" && grep -n foo *',
    'ls -la ~/.cache/*',
    'find . -type f -exec chmod 644 {} \\;',
    'python3 -m pytest -q tests 2>&1 | tail -5',
    'echo hi > out.txt 2>&1',
    "git log --format='%h %s' -5",
    'echo $"hello"',
    "bertie-mail whoami",
    "bertie-mail search 'from:aws is:unread' --max 5",
    "bertie-mail read 18c0",
    "bertie-mail thread T1",
    "bertie-mail drafts",
    "bertie-mail show-draft r123",
    "bertie-mail attachments 18c0 --out /tmp/x",
    "bertie-mail draft --to a@test.invalid --subject s --body b",
    "bertie-mail search \"send\"",
    "bertie-mail --json search send",
    "bertie-calendar agenda",
    "bertie-calendar freebusy 2026-10-15 2026-10-16",
    "bertie-calendar event e1",
    "bertie-chat spaces",
    "bertie-chat read spaces/A",
    "bertie-drive list send",
    "bertie-drive ls FOLDERID",
    "bertie-drive download FILEID --out /tmp",
    "bertie whoami",
    "bertie login",
    "bertie log",
    "bertie check",
    "cat ~/.config/bertie/actions.log",
    "mybertie-mail send",
    "bertie-mail --help",
    "bertie-mail -h",
    "which bertie-mail",
    "ls -l ~/.local/bin/bertie-mail",
    "cat ~/.local/bin/bertie-drive",
    "bertie-mail search x 2>/dev/null",
    "bertie-calendar agenda > /tmp/out.txt",
    "bertie-drive list x </dev/null",
    "bertie-chat read spaces/A 2>&1 | head",
    "python3 -m pytest tests",
    "",
]


@pytest.mark.parametrize("cmd", GATED + COMPOUND)
def test_gated_commands_ask(cmd):
    reason = hook.decide(cmd)
    assert reason, cmd


@pytest.mark.parametrize("cmd", UNREADABLE)
def test_commands_whose_action_cannot_be_read_ask(cmd):
    assert hook.decide(cmd), cmd


@pytest.mark.parametrize("cmd", BYPASS)
def test_direct_file_or_venv_use_asks(cmd):
    assert hook.decide(cmd), cmd


@pytest.mark.parametrize("cmd", KEYCHAIN)
def test_keychain_service_mention_asks(cmd):
    assert hook.decide(cmd), cmd


@pytest.mark.parametrize("cmd", FREE)
def test_harmless_and_free_commands_have_no_opinion(cmd):
    assert hook.decide(cmd) is None, cmd


def test_reason_names_the_action():
    r = hook.decide("cd /x && bertie-drive share F a@test.invalid --role writer")
    assert "bertie-drive share" in r
    r = hook.decide("bertie-mail send d1")
    assert "bertie-mail send" in r and "SENDS" in r


def test_every_registered_gated_action_is_caught():
    for tool, acts in registry.gated_actions().items():
        for act in acts:
            assert hook.decide(f"{tool} {act} x y"), (tool, act)
            assert hook.decide(f"echo 1 && {tool} --json {act} x"), (tool, act)


def run_hook(payload, raw=None):
    """Run hook.py exactly as Claude Code would: JSON on stdin."""
    data = raw if raw is not None else json.dumps(payload)
    p = subprocess.run([sys.executable, "-I", str(TOOLS / "hook.py")], input=data,
                       capture_output=True, text=True, timeout=30)
    return p


def test_output_follows_the_claude_code_schema():
    p = run_hook({"tool_name": "Bash", "tool_input": {"command": "bertie-mail send r1"}})
    assert p.returncode == 0
    out = json.loads(p.stdout)
    assert list(out) == ["hookSpecificOutput"]
    h = out["hookSpecificOutput"]
    assert set(h) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}
    assert h["hookEventName"] == "PreToolUse"
    assert h["permissionDecision"] == "ask"
    assert "bertie-mail send" in h["permissionDecisionReason"]
    assert len(h["permissionDecisionReason"]) <= 2000          # Claude Code truncates beyond this


def test_free_command_prints_nothing_and_exits_zero():
    p = run_hook({"tool_name": "Bash", "tool_input": {"command": "ls -la"}})
    assert p.returncode == 0 and p.stdout == ""


def test_other_tools_are_ignored():
    p = run_hook({"tool_name": "Read", "tool_input": {"command": "bertie-mail send r1"}})
    assert p.returncode == 0 and p.stdout == ""


@pytest.mark.parametrize("raw", ["not json", "[]", '{"tool_name": "Bash", "tool_input": {"command": 5}}'])
def test_unreadable_input_asks_instead_of_failing_open(raw):
    p = run_hook(None, raw=raw)
    assert p.returncode == 0
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_empty_stdin_is_no_opinion():
    p = run_hook(None, raw="")
    assert p.returncode == 0 and p.stdout == ""


def test_a_new_service_is_picked_up_from_the_registry(monkeypatch):
    new = registry.Service(key="docs", command="bertie-docs", module="svc_docs", label="Docs",
                           api_name="Google Docs API", scopes=("https://www.googleapis.com/auth/documents",),
                           gated={"create": "creates a document"})
    monkeypatch.setattr(registry, "SERVICES", registry.SERVICES + (new,))
    assert "Bash(bertie-docs create:*)" in registry.ask_rules()
    assert "https://www.googleapis.com/auth/documents" in registry.all_scopes()
    try:
        reloaded = importlib.reload(hook)
        assert reloaded.decide("bertie-docs create --title x")
        assert reloaded.decide("bertie-docs read d1") is None
    finally:
        monkeypatch.undo()
        importlib.reload(hook)


# ------------------------------------------------------------------ other tools

def tool_run(tool, tool_input):
    return run_hook({"tool_name": tool, "tool_input": tool_input})


@pytest.mark.parametrize("field", ["command", "cmd", "text", "input"])
def test_ide_terminal_tool_is_read_whatever_its_field_is_called(field):
    p = tool_run("mcp__terminal__run_in_terminal", {field: "bertie-mail send r1", "other": 1})
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    p = tool_run("mcp__terminal__run_in_terminal", {field: "ls -la"})
    assert p.returncode == 0 and p.stdout == ""


def test_nested_values_are_scanned_too():
    p = tool_run("mcp__terminal__run_in_terminal", {"args": [{"line": "echo hi; BERTIE-DRIVE share F x --role reader"}]})
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("tool,payload", [
    ("Write", {"file_path": "/tmp/run.sh", "content": "#!/bin/sh\n$HOME/.config/bertie/.venv/bin/python x.py\n"}),
    ("Write", {"file_path": "/tmp/run.py", "content": "import svc_mail\nsvc_mail.main(['send', 'r1'])\n"}),
    ("Edit", {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "import keyring\nkeyring.get_password('bertie', 'x')"}),
    ("Write", {"file_path": "/home/x/.config/bertie/hook.py", "content": "pass"}),
    ("Edit", {"file_path": "/tmp/x", "old_string": "a", "new_string": "security find-generic-password -s bertie -w"}),
])
def test_writing_text_that_reaches_the_tools_asks(tool, payload):
    p = tool_run(tool, payload)
    out = json.loads(p.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask" and "written to a file" in out["permissionDecisionReason"]


@pytest.mark.parametrize("tool,payload", [
    ("Write", {"file_path": "/tmp/notes.md", "content": "meeting notes\nship the report"}),
    ("Edit", {"file_path": "/tmp/a.py", "old_string": "x", "new_string": "print('hello')"}),
    ("Write", {"file_path": "/tmp/a.sh", "content": "bertie-mail search x"}),
])
def test_ordinary_file_writes_have_no_opinion(tool, payload):
    p = tool_run(tool, payload)
    assert p.returncode == 0 and p.stdout == ""


# ------------------------------------------------------------------ round 2: the window, patterns, run-time names

WINDOW_DRIVING = [
    """osascript -e 'tell application "System Events" to click button "Send 4821" of window 1 of process "osascript"'""",
    """osascript -e 'tell application "System Events" to keystroke return'""",
    """osascript -e 'tell application "System Events" to key code 36'""",
    "OSASCRIPT -e 'beep'",
    "cliclick c:100,200",
    "CliClick m:5,5",
    "echo tell application System Events",
    "echo keystroke return",
    "echo KEY   CODE 36",
    "echo click   button OK",
    "echo AXPress",
    "echo axpress",
    "python3 -c \"import subprocess; subprocess.run(['/usr/bin/osascript', '-e', 'beep'])\"",
    "sh -c 'o''sascript -e beep'",
]


@pytest.mark.parametrize("cmd", WINDOW_DRIVING)
def test_scripting_the_mac_to_answer_the_window_asks(cmd):
    reason = hook.decide(cmd)
    assert reason and "person" in reason, cmd


@pytest.mark.parametrize("tool", ["Bash", "mcp__terminal__run_in_terminal"])
def test_window_driving_is_seen_in_both_command_tools(tool):
    p = tool_run(tool, {"command": "osascript -e 'tell application \"System Events\" to key code 36'"})
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("tool", [
    "mcp__computer-use__screenshot", "mcp__computer-use__left_click", "mcp__computer-use__type",
    "mcp__computer-use__key", "mcp__computer-use__request_access",
])
@pytest.mark.parametrize("payload", [{}, {"coordinate": [10, 20]}, {"text": "hello"}])
def test_every_computer_use_tool_always_asks(tool, payload):
    p = tool_run(tool, payload)
    out = json.loads(p.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask" and "person" in out["permissionDecisionReason"]


def test_other_mcp_tools_are_still_ignored():
    p = tool_run("mcp__other__thing", {"command": "hello"})
    assert p.returncode == 0 and p.stdout == ""


PATTERNS = [
    "bash -c 'bertie-mail {send,} r-1'",
    "bertie-mail {send,} r-1",
    "bertie-mai? send r-1",
    "bertie-m* send r-1",
    "bertie-[m]ail send r-1",
    "bertie-{mail,chat} send x",
    "{bertie-mail,true} send r1",
    "bertie-mail s?nd r-1",
    "bertie-mail s* r-1",
    "bertie-mail [s]end r-1",
    "bertie-mail{,} send r-1",
    "bertie-mail* send r-1",
    "BERTIE-MAI? SEND r-1",
]


@pytest.mark.parametrize("cmd", PATTERNS)
def test_a_pattern_next_to_a_tool_name_or_its_action_asks(cmd):
    assert hook.decide(cmd), cmd


RUN_TIME_NAMES = [
    "python3 -c \"import importlib; importlib.import_module('svc_'+'mail').main(['send', 'r-1'])\"",
    "python3 -c \"import importlib; m = importlib.import_module('s'+'vc_mail')\"",
    "python3 -c \"__import__('sv'+'c_mail')\"",
    "python3 -c \"import runpy; runpy.run_path('x')\"",
    "python3 -m runpy something",
    "python3 -c \"exec(open('x').read())\"",
    "python3 -c \"exec(compile(s, 'f', 'exec'))\"",
    "/usr/bin/python3 -c 'eval(s)'",
    "python -c \"compile('1', 'f', 'eval')\"",
    "uv run python -c 'exec(s)'",
    "PYTHON3 -c 'EXEC (s)'",
]


@pytest.mark.parametrize("cmd", RUN_TIME_NAMES)
def test_building_a_module_name_or_running_text_as_code_asks(cmd):
    assert hook.decide(cmd), cmd


@pytest.mark.parametrize("tool,payload", [
    ("Write", {"file_path": "/tmp/a.py", "content": "import importlib\nm = importlib.import_module('svc_'+'mail')\n"}),
    ("Edit", {"file_path": "/tmp/a.py", "old_string": "a", "new_string": "m = import_module('svc_'+'mail')"}),
    ("Write", {"file_path": "/tmp/a.py", "content": "m = __import__('sv' + 'c_mail')"}),
    ("Write", {"file_path": "/tmp/a.py", "content": "import runpy\nrunpy.run_module('x')"}),
    ("Write", {"file_path": "/tmp/a.py", "content": "exec(open('/x').read())"}),
    ("Write", {"file_path": "/tmp/a.py", "content": "code = compile(src, 'f', 'exec')"}),
])
def test_a_file_that_builds_the_module_name_or_runs_text_asks(tool, payload):
    p = tool_run(tool, payload)
    out = json.loads(p.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask" and "written to a file" in out["permissionDecisionReason"]


@pytest.mark.parametrize("cmd", [
    "python3 -c 'print(1)'",
    "python3 -m pytest tests",
    "echo compile( and exec( are words",
    "node -e 'console.log(1)'",
    "bertie-mail search 'from:a*'",
    "ls src/*.py",
    "grep -rn 'x?' .",
])
def test_round_two_additions_leave_ordinary_commands_alone(cmd):
    assert hook.decide(cmd) is None, cmd


def test_ordinary_regex_use_in_a_file_is_left_alone():
    p = tool_run("Write", {"file_path": "/tmp/a.py", "content": "import re\nx = re.compile(r'a+')\nprint(x)"})
    assert p.returncode == 0 and p.stdout == ""


# ------------------------------------------------------------------ the fingerprint / password helper

HELPER_TAMPERING = [
    "chmod 700 ~/.config/bertie/bertie-approve",
    "chmod u+w $HOME/.config/bertie/approve.sha256",
    "cp /tmp/other ~/.config/bertie/bertie-approve",
    "mv /tmp/x ~/.config/bertie/approve.sha256",
    "ln -sf /usr/bin/true ~/.config/bertie/bertie-approve",
    "echo abc | tee ~/.config/bertie/approve.sha256",
    "echo abc > ~/.config/bertie/approve.sha256",
    "swiftc -O -o /tmp/x /tmp/y.swift",
    "/Library/Developer/CommandLineTools/usr/bin/swiftc -o x approve.swift",
    "SWIFTC -O x.swift",
    "cat approve.swift",
    "ls bertie-approve",
    "shasum -a 256 approve.sha256",
    "BERTIE-APPROVE 'anything'",
    "echo x >> ~/.config/BERTIE/hook.sh",
    "cd ~/.config/bertie; chmod 755 hook.py",
    "cd ~/.config; mv bertie bertie-old",
    "cd ~/.config && mv Bertie /tmp/x",
    "ln -s /tmp/evil ~/.config/bertie/svc_mail.py",
]


@pytest.mark.parametrize("cmd", HELPER_TAMPERING)
def test_touching_the_approval_helper_or_the_install_folder_asks(cmd):
    assert hook.decide(cmd), cmd


@pytest.mark.parametrize("tool,payload", [
    ("Write", {"file_path": "/home/x/tools/approve.swift", "content": "exit(0)"}),
    ("Write", {"file_path": "/tmp/a.sh", "content": "swiftc -o /tmp/bertie-approve x.swift"}),
    ("Edit", {"file_path": "/tmp/a", "old_string": "a", "new_string": "approve.sha256"}),
    ("Write", {"file_path": "/tmp/a.sh", "content": "cp x ~/.config/bertie/bertie-approve"}),
])
def test_writing_text_that_names_the_helper_asks(tool, payload):
    p = tool_run(tool, payload)
    out = json.loads(p.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask" and "written to a file" in out["permissionDecisionReason"]


def test_very_long_command_asks():
    assert hook.decide("echo " + "a" * 30000)
    assert hook.decide("{" * 40000 + "}" * 40000)


@pytest.mark.parametrize("cmd", [
    "bertie-mail send r1; : " + "{" * 2500 + "}" * 2500 + "{a,b}" * 9,
    "echo " + "[" * 19900 + "*",
    "echo " + "[:" * 9900,
    "{" * 9900 + "}" * 9900,
    "{" * 19900,
    "a{" * 6000 + "b,c" + "}" * 6000,
    "(" * 9000 + ")" * 9000,
    "{a,{" * 3000 + "}" * 3000,
    "cd " + "{a,b}" * 8 + "*X" * 9970 + "=",
    "cd " + "{a,b}" * 8 + "*X" * 9940 + "=; bertie-mail send r1",
    "{a,b}" * 8 + "*x" * 9800,
    "a?b " * 4900,
    "(a|b)" * 3900,
    "~/.local/bin/b?rtie-mail send r1; : " + "-xargs-I" * 2490,
    "echo " + "1" * 19990,
    "echo " + "[x" * 9900,
    "find -exec " + "x " * 9900,
])
def test_hostile_input_just_under_the_length_limit_is_fast(cmd):
    import time
    start = time.time()
    hook.decide(cmd)
    assert time.time() - start < 5, "the hook must answer well inside Claude Code's hook timeout"
