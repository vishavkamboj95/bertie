#!/usr/bin/env python3
"""Claude Code PreToolUse hook: the SECOND approval layer.

The first, and the one that cannot be talked round, is the confirmation window
inside the tools themselves (common.confirm): no command phrasing gets past a
Mac window that only a click can answer. This hook adds a prompt in Claude Code
for the common ways of reaching those tools, and watches for the ways round them.

Claude Code feeds the hook a JSON object on stdin:
    {"tool_name": "Bash", "tool_input": {"command": "..."}, ...}
and the hook may answer on stdout with:
    {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                            "permissionDecision": "ask",
                            "permissionDecisionReason": "..."}}
No output and exit 0 means "no opinion": Claude Code carries on as usual.

The permission rules in settings.json match the START of a command, and are case
sensitive. This hook looks at the WHOLE command text, case-blind, so it also
catches a gated action hidden after `&&`, `;` or `|`, inside `$(...)` or
`sh -c '...'`, after `env X=1`, behind redirections (`2>/dev/null`), fed by
`xargs`, or written with odd quoting. It asks whenever the text

  * runs a gated subcommand of any of the tools, or a tool with no
    readable subcommand,
  * touches the install folder, imports or runs the tool modules or their Python
    (the way round the commands), or
  * goes near the Keychain entry that holds the Google token,
  * writes a tool name or action with wildcards or braces, or builds a module
    name at run time (importlib, __import__, exec), or
  * touches the fingerprint/password helper (<name>-approve, approve.swift, its
    recorded fingerprint) or runs swiftc,
  * scripts the Mac to answer the confirmation window (osascript, System Events,
    keystrokes, cliclick, button clicks).

It always asks before a computer-use (screen control) tool, and it reads text
written to files (Write, Edit) for the same bypass phrases.

This is a deny-list, so it will always have holes (a command built at run time
with variables, eval or encoded text). It is a net, not the gate. Standard
library only.

Other agents: `hook.py --agent <key>` makes the same checks for another coding agent,
reading and answering in that agent's own format (tools/agents/<key>.py). An agent whose
hook can only block gets the block-only policy (see decide_block). It always exits 0 and
says "block" in JSON, so hook.sh never retries a block with another Python. Run with no
arguments, this file behaves exactly as described above.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402

# Run as a script this file is `__main__`; let the adapters' `import hook` find this module, not a second copy.
sys.modules.setdefault("hook", sys.modules[__name__])

COMMAND_TOOLS = ("Bash", "mcp__terminal__run_in_terminal")
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SCREEN_TOOL_PREFIX = "mcp__computer-use__"     # tools that look at and click the screen

_TOOLS = "|".join(re.escape(t) for t in sorted(registry.tool_names(), key=len, reverse=True))
_N = re.escape(registry.NAME)
# a tool name as a command word: not glued to other word characters, so
# "my{name}-mail" and "{name}-mail-x" do not count, "~/.local/bin/{name}-mail" does.
_TOOL_RE = re.compile(r"(?<![\w.-])(" + _TOOLS + r")(?![\w-])", re.I)
_SEGMENT_END = re.compile(r"[;&|()<>`\n]")
# redirections: 2>/dev/null  >out  </dev/null  &>log  2>&1  <<EOF
_REDIRECT = re.compile(r"(?:(?<!\d)\d+|&)?[<>]+&?\s*[^\s;&|()<>`]*")
_RUNNERS = re.compile(r"(?:^|\s)(?:xargs|parallel|-exec|-execdir)(?:\s|$)")
_READ_ONLY_LEADS = {"which", "type", "ls", "cat", "file", "stat", "head", "tail", "wc", "grep", "man", "less"}

_INSTALL_DIR = [re.compile(p, re.I) for p in (
    r"\.config/+" + _N + r"(?![\w-])",                        # the install folder, any case
    r"\.config/+" + _N[:-1] + r"[\w-]*[*?\[]",                # a glob into it: .config/berti*
    r"\.config/+(?:[*?\[]|" + _N[0] + r"[\w-]*[*?\[])",       # a glob over ~/.config
    # any path piece that is the folder's name, however the path before it is written:
    # `~/.c*/bertie/x`, `$XDG_CONFIG_HOME/bertie`, `./bertie/x`, `ln -s ~/.c*/bertie /tmp/k`.
    # This also asks for ~/.claude/skills/bertie: harmless to read, but an exemption for it
    # was found to hide real paths into the install folder, so there is none.
    r"(?<![\w.-])" + _N + r"/",
    r"/" + _N + r"(?![\w-])",
    r"(?<![\w.-])" + _N[:-1] + r"[\w-]*[*?\[][^\s]*/",   # or a glob such as berti*/
)]
# from inside ~/.config the folder is just its bare name: `cd ~/.config; mv bertie old`
_CONFIG_WORD = re.compile(r"\.config(?![\w-])", re.I)
_BARE_NAME = re.compile(r"(?<![\w.-])" + _N + r"(?![\w-])", re.I)
# module names with no other likely owner, with or without .py
_TOOL_FILES = re.compile(r"(?<![\w.-])(?:svc_[a-z0-9_]+|google_cli|kit_setup)(?:\.py)?(?![\w-])", re.I)
# importing or running the shared modules, e.g. python -c "import auth", python -m registry
_MODULE_USE = re.compile(
    r"(?:\bimport|\bfrom|\s-m)\s+(?:auth|common|registry|hook|kit_setup|google_cli|svc_\w+)(?![\w-])", re.I)
_KEYCHAIN_WORDS = re.compile(r"\bsecurity\b|keyring|keychain", re.I)
# a name pieced together or looked up at run time, e.g. import_module('svc_'+'mail')
_DYNAMIC_IMPORT = re.compile(r"importlib|__import__|import_module|runpy|(?<![\w-])svc_", re.I)
# running text as code; only a worry where Python is involved (or in a file being written)
_RUN_TEXT = re.compile(r"(?<![\w.])(?:exec|compile|eval)\s*\(", re.I)
_PYTHON_WORD = re.compile(r"(?<![\w-])(?:python[\d.]*|py|ipython|uv\s+run|pytest)(?![\w-])", re.I)
# the fingerprint/password helper and its recorded fingerprint, or building Swift: the way round the check
_APPROVAL_FILES = re.compile(re.escape(registry.HELPER_NAME) + r"|approve\.swift|approve\.sha|swiftc", re.I)
# a tool name with a pattern character next to it: {name}-mai? {name}-* {{name}-mail,x}
_TOOL_PATTERN = re.compile(_N + r"[\w-]*[{*?\[]|[{*?\[][\w,.-]*" + _N + r"-", re.I)
# driving the confirmation window from a script: the click has to be a person's
_WINDOW_DRIVING = re.compile(
    r"osascript|system\s+events|keystroke|key\s+code|cliclick|click\s+button|axpress", re.I)
_LABEL = registry.DISPLAY_NAME + " safety check"


def normalise(command: str) -> str:
    """Drop quotes and backslashes (so `bertie-mail "se""nd"` reads as send)
    and squeeze whitespace."""
    s = command.replace("\\\n", " ")
    s = re.sub(r"\$(?=['\"])", "", s)              # $'...' and $"..." are quotes too
    s = re.sub(r"[\\'\"]", "", s)
    return re.sub(r"\s+", " ", s)


def gated_hits(norm: str) -> List[str]:
    """Reasons for every gated (or unreadable) tool subcommand in the string."""
    gated = registry.gated_actions()
    text = _REDIRECT.sub(" ", norm)
    reasons: List[str] = []
    for m in _TOOL_RE.finditer(text):
        tool = m.group(1).lower()
        before = text[: m.start()]
        lead = before[max(before.rfind(c) for c in ";&|\n(`") + 1:]
        if _RUNNERS.search(lead):
            reasons.append(f"{tool} run through xargs, parallel or find -exec; cannot tell which action it runs")
            continue
        rest = text[m.end():]
        cut = _SEGMENT_END.search(rest)
        seg = rest[: cut.start()] if cut else rest
        # a `$(` right after the tool name is cut at the `(`, leaving the `$`
        if cut and cut.group(0) == "(" and seg.endswith("$"):
            reasons.append(f"{tool} followed by a command substitution; cannot tell which action it runs")
            continue
        tokens = [t for t in seg.split(" ") if t]
        sub = next((t for t in tokens if not t.startswith("-")), "")
        if not sub:
            lead_words = lead.split()
            if any(t in ("-h", "--help") for t in tokens):
                continue
            if lead_words and lead_words[0].lower() in _READ_ONLY_LEADS:
                continue                      # `cat ~/.local/bin/bertie-mail` runs nothing
            reasons.append(f"{tool} with no readable subcommand; cannot tell which action it runs")
        elif sub.startswith("$"):
            reasons.append(f"{tool} followed by a variable; cannot tell which action it runs")
        elif any(c in sub for c in "{*?["):
            reasons.append(f"{tool} followed by a pattern; cannot tell which action it runs")
        elif sub.lower() in gated.get(tool, {}):
            reasons.append(f"{tool} {sub.lower()} {gated[tool][sub.lower()]}")
    return reasons


_MAX_COMMAND = 20000          # longer commands ask without being checked, so the hook stays fast
_MAX_ALTERNATIVES = 256       # a brace list that grows past this asks
_MAX_BRACE_STEPS = 200000     # so does brace text that takes too long to expand
_MAX_NESTING = 50             # groups nested deeper than this are all treated as '*'


def _brace_alternatives(word: str) -> List[str]:
    """Expand braces the way the shell does: {a,b} and ranges {1..9} (a range becomes
    '*'). A brace with no top-level comma or '..' ({}, {print}) or no closing brace
    stays as written, and expansion carries on after it. Raises ValueError when the
    expansion grows too large or takes too many steps."""
    out, todo, steps = [], [word], 0
    while todo:
        if time.monotonic() > _deadline[0]:
            raise _OutOfTime("pattern check took too long")
        w = todo.pop()
        found = False
        for i, c in enumerate(w):
            if c != "{":
                continue
            depth, commas = 0, []
            for j in range(i, len(w)):
                steps += 1
                if steps > _MAX_BRACE_STEPS:
                    raise ValueError("brace text too slow to expand")
                if w[j] == "{":
                    depth += 1
                elif w[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                elif w[j] == "," and depth == 1:
                    commas.append(j)
            else:
                continue                                # no closing brace: this one is literal
            inner = w[i + 1:j]
            if commas:
                cuts = [i] + commas + [j]
                todo += [w[:i] + w[a + 1:b] + w[j + 1:] for a, b in zip(cuts, cuts[1:])]
            elif ".." in inner and "{" not in inner:
                todo.append(w[:i] + "*" + w[j + 1:])
            else:
                continue                                # {} or {print}: not an expansion
            found = True
            break
        if len(out) + len(todo) > _MAX_ALTERNATIVES:
            raise ValueError("brace text too large to expand")
        if not found:
            out.append(w)
    return out


def _brackets_to_any(text: str) -> str:
    """Each [...] becomes '?', in one pass: a '[' closes at the next ']' on the same
    line (a ']' straight after '[', '[!' or '[^' is part of the set), else it is literal."""
    n = len(text)
    next_close, next_nl = [n] * (n + 1), [n] * (n + 1)
    for k in range(n - 1, -1, -1):
        next_close[k] = k if text[k] == "]" else next_close[k + 1]
        next_nl[k] = k if text[k] == "\n" else next_nl[k + 1]
    out, i = [], 0
    while i < n:
        if text[i] == "[":
            j = i + 1
            if j < n and text[j] in "!^":
                j += 1
            if j < n and text[j] == "]":
                j += 1
            end = next_close[j] if j <= n else n
            if end < n and end < next_nl[i]:
                out.append("?")
                i = end + 1
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _collapse(text: str) -> str:
    """Loose fnmatch form: any [...] (with classes such as [[:alpha:]], [[.b.]],
    [[=b=]], or a quoted space or symbol inside) becomes '?', and any group, bash
    extglob @(x|y) ?(x) +(x) *(x) !(x), zsh (x|y) or $(...), becomes '*'."""
    p = re.sub(r"\[([.=:])[^\]\[\n]{0,40}\1\]", "\0", text)    # [:class:] [.b.] [=b=]
    p = _brackets_to_any(p).replace("\0", "?")
    p = re.sub(r"`[^`]*`", "*", p)                                   # `...` is run-time text
    for _ in range(_MAX_NESTING):
        q = re.sub(r"[@?+*!$]?\([^()]*\)", "*", p)
        if q == p:
            break
        p = q
    return p.replace("(", "*").replace(")", "*")


_PATTERN_SECONDS = 3.0        # the pattern check asks if it has not finished by then


class _OutOfTime(ValueError):
    pass


class _TooManyFolders(_OutOfTime):
    """The text changes into more folders than can be followed (it stops the check like running out of time,
    but says so in its own words)."""


_deadline = [0.0]


def _matches(name: str, pat: str) -> bool:
    """fnmatch on a pattern already through _collapse: any '[' left is a plain
    character (and fnmatch is slow on a long run of unclosed '['). A pattern with
    more non-'*' characters than the name cannot match it, so fnmatch never has to
    compile a long one."""
    if time.monotonic() > _deadline[0]:
        raise _OutOfTime("pattern check took too long")
    if len(pat) - pat.count("*") > len(name):
        return False
    return fnmatch.fnmatchcase(name, pat.replace("[", "[[]"))


def _could_be_bin(piece: str) -> bool:
    if len(piece) - piece.count("*") > 200:
        return True                                     # too long to read cheaply: assume it could
    pat = _collapse(piece.strip("\"'"))
    return any(c in pat for c in "*?") and _matches("bin", pat)


def _mentions_bin(norm: str, words: List[str]) -> bool:
    """A letterless pattern (`*`, `??????`) can only run a tool from inside the bin
    folder. True when the command names that folder, plainly or as a pattern:
    ~/.local/b?n/..., ~/.local/*/..., cd ~/.l*/b*"""
    if re.search(r"\bbin\b|\.local\b", norm, re.I):
        return True
    for word in words:
        for alt in _brace_alternatives(word.lower()):
            if any(_could_be_bin(p) for p in alt.split("/")[:-1]):
                return True
    # every argument of cd, pushd or chdir, flags or not; a $(...) there is run-time
    # text (an accepted limit), not a pattern
    for m in re.finditer(r"(?<![\w.-])(?:cd|pushd|chdir)\s+([^;&|\n]*)", norm, re.I):
        args = re.sub(r"(?<!\S)(?:\$\([^()]*\)|`[^`]*`)(?!\S)", "x", m.group(1))
        for word in args.split():
            for alt in _brace_alternatives(word.lower()):
                if any(_could_be_bin(p) for p in alt.split("/")):
                    return True
    return False


_SHAPE_TOKEN = re.compile(
    r"(?:[\w.~/{},-]|[?*]|\[[!^]?\]?[^\]\[\n]*\]|[@?+*!]?\([^()\n]*\))+")
_SHAPE_TOKEN_NO_GROUPS = re.compile(r"(?:[\w.~/{},-]|[?*]|\[[!^]?\]?[^\]\[\n]*\])+")
# a stray '[' far from its ']' must not swallow a tool name: brackets with no spaces
_SHAPE_TOKEN_TIGHT = re.compile(
    r"(?:[\w.~/{},-]|[?*]|\[[!^]?\]?[^\]\[\s;&|]*\]|[@?+*!]?\([^()\n]*\))+")


def _shape_hits(norm: str, mentions_bin: bool) -> bool:
    """One check over the whole text, whatever the word splitting: any run that looks
    like a tool name written with ?, *, [...], (...) or braces, wherever it sits
    (after `x=$(`, after a stray '[', inside a group). Read twice: once with groups as
    single pieces, once with '(' and ')' as breaks, once with brackets that hold no space."""
    text = re.sub(r"\[([.=:])[^\]\[\n]{0,40}\1\]", "?", norm)   # [:class:] [.b.] [=b=]
    for finder in (_SHAPE_TOKEN, _SHAPE_TOKEN_NO_GROUPS, _SHAPE_TOKEN_TIGHT):
        for m in finder.finditer(text):
            tok = m.group(0)
            if any(c in tok for c in "?*[({") and _word_could_be_a_tool(tok, mentions_bin):
                return True
    return False


_RUNS_FOUND_FILES = re.compile(
    r"-(?:exec|execdir|ok|okdir)\s+\{\}|\bxargs\b[^;&|\n]{0,200}?-I\s*(\S{1,20})\s+\1(?:\s|$)", re.I)
# {} anywhere in an -exec command (`-exec env {} logout`): asks when the bin folder is in view
_EXEC_WITH_FOUND = re.compile(r"-(?:exec|execdir|ok|okdir)\s[^;+\n]{0,500}?\{\}", re.I)


def _word_could_be_a_tool(word: str, mentions_bin: bool) -> bool:
    """A word the shell could expand to a tool name. Any word counts, not only the
    first of a command: `env`, `exec`, `sudo`, `then`, `bash -c` or a redirection in
    front must not hide it. A pattern with no letters at all (`*`, `src/*`) asks only
    when the command could reach the bin folder, so `echo *` stays free."""
    word = word.strip("\"'\\")
    if not any(c in word for c in "*?[{("):
        return False
    tools = [t.lower() for t in registry.tool_names()]
    for alt in _brace_alternatives(word.lower()):
        base = _collapse(alt).rsplit("/", 1)[-1]
        if base in tools:
            return True
        if not any(c in base for c in "*?"):
            continue
        if not base.strip("*?") and not mentions_bin:
            continue
        if any(_matches(t, base) for t in tools):
            return True
    return False


def pattern_hits(norm: str) -> List[str]:
    reason = [f"a {registry.DISPLAY_NAME} tool name written with a pattern (wildcard or braces); "
              "cannot tell which command it runs"]
    if _TOOL_PATTERN.search(norm):
        return reason
    # two word lists: brackets and groups read across the whole command first (so a
    # quoted space or a `|` inside one cannot split it), and the plain split on
    # brackets, `$(` and `=` (so a brace list inside `$(...)` or after `x=` is seen)
    collapsed = [w for w in re.split(r"[\s;&|<>`]+", _collapse(norm)) if w]
    plain = [w for w in re.split(r"[\s;&|<>`()=]+|\$\(", norm) if w]
    found_files = ["find -exec {} or xargs -I runs whatever files it finds as commands; "
                   "cannot tell which command it runs"]
    if _RUNS_FOUND_FILES.search(norm):
        return found_files
    _deadline[0] = time.monotonic() + _PATTERN_SECONDS
    try:
        mentions_bin = _mentions_bin(norm, collapsed) or _mentions_bin(norm, plain)
        if mentions_bin and _EXEC_WITH_FOUND.search(norm):
            return found_files
        if any(_word_could_be_a_tool(w, mentions_bin) for w in collapsed + plain) or _shape_hits(norm, mentions_bin):
            return reason
    except ValueError:
        return reason
    return []


def approval_hits(norm: str) -> List[str]:
    if _APPROVAL_FILES.search(norm):
        return ["this touches the fingerprint/password approval helper or builds Swift code, "
                "which is the way round the approval check"]
    return []


def window_hits(norm: str) -> List[str]:
    if _WINDOW_DRIVING.search(norm):
        return ["this scripts the Mac (osascript, System Events, keystrokes or clicks); "
                "the confirmation window must be answered by a person, never by a script"]
    return []


def bypass_hits(norm: str, python_involved: bool = True) -> List[str]:
    """The way round the commands is to run the .py files, import the modules or
    use the private Python directly."""
    # reading the action log is the one harmless thing in the install folder
    stripped = re.sub(_N + r"/+actions\.log\b", "", norm, flags=re.I)
    if any(p.search(stripped) for p in _INSTALL_DIR) or (
            _CONFIG_WORD.search(stripped) and _BARE_NAME.search(stripped)):
        return [f"this touches the {registry.DISPLAY_NAME} install folder (its Python files, environment or "
                "token), which is the way round the approval rules"]
    if _TOOL_FILES.search(norm) or _MODULE_USE.search(norm) or _DYNAMIC_IMPORT.search(norm):
        return [f"this runs or imports a {registry.DISPLAY_NAME} tool module directly, "
                "which is the way round the approval rules"]
    if python_involved and _RUN_TEXT.search(norm):
        return ["this runs text as Python code (exec, eval or compile), which could reach the "
                f"{registry.DISPLAY_NAME} tool modules; asking to be safe"]
    return []


def keychain_hits(norm: str) -> List[str]:
    svc = registry.KEYCHAIN_SERVICE
    low = norm.lower()
    # the name with the Keychain tools catches the entry and longer names built from it
    if (registry.NAME in low and _KEYCHAIN_WORDS.search(low)) or "keyring" in low:
        return [f'this goes near the Keychain, where the entry "{svc}" holds the Google sign-in token']
    return []


def _finish(reasons: List[str]) -> Optional[str]:
    if not reasons:
        return None
    seen: List[str] = []
    for r in reasons:
        if r not in seen:
            seen.append(r)
    return _LABEL + ": " + "; ".join(seen[:4])[:1500]


def decide(command: str) -> Optional[str]:
    """For a command line. None = no opinion, else the reason shown in the prompt."""
    norm = normalise(command)
    if re.search(r"\$'[^']*\\", command):
        return _finish(["$'...' quoting with backslash escapes can spell any name; cannot read it, asking to be safe"])
    if len(norm) > _MAX_COMMAND:
        return _finish([f"a command longer than {_MAX_COMMAND} characters; too long to check, asking to be safe"])
    return _finish(gated_hits(norm) + pattern_hits(norm) + window_hits(norm) + approval_hits(norm)
                   + bypass_hits(norm, bool(_PYTHON_WORD.search(norm))) + keychain_hits(norm))


def decide_parts(command: str) -> Tuple[List[str], List[str]]:
    """The same pieces decide() joins, kept apart: (the reasons that come from gated
    subcommands, every other reason). _finish(gated + other) is exactly decide(command); a
    test holds the two together."""
    norm = normalise(command)
    if re.search(r"\$'[^']*\\", command):
        return [], ["$'...' quoting with backslash escapes can spell any name; cannot read it, asking to be safe"]
    if len(norm) > _MAX_COMMAND:
        return [], [f"a command longer than {_MAX_COMMAND} characters; too long to check, asking to be safe"]
    return gated_hits(norm), (pattern_hits(norm) + window_hits(norm) + approval_hits(norm)
                              + bypass_hits(norm, bool(_PYTHON_WORD.search(norm))) + keychain_hits(norm))


def decide_file_text(text: str) -> Optional[str]:
    """For text being written to a file: only the bypass phrases matter."""
    norm = normalise(text)
    reasons = bypass_hits(norm) + keychain_hits(norm) + approval_hits(norm)
    return _finish([r + " (in text being written to a file)" for r in reasons])


def strings_in(obj: Any) -> List[str]:
    """Every string value anywhere inside tool_input (the field name of a
    terminal tool is not something to guess)."""
    out: List[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out += strings_in(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out += strings_in(v)
    return out


def decide_tool(tool_name: str, tool_input: Any) -> Optional[str]:
    if isinstance(tool_name, str) and tool_name.startswith(SCREEN_TOOL_PREFIX):
        return (_LABEL + ": a screen-control tool is being used; the Mac confirmation "
                "window must only ever be answered by a person, never by Claude")
    if tool_name in COMMAND_TOOLS:
        return decide("\n".join(strings_in(tool_input)))
    if tool_name in FILE_TOOLS:
        return decide_file_text("\n".join(strings_in(tool_input)))
    return None


def ask(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": reason,
    }}


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        if not isinstance(data, dict):
            raise ValueError("hook input is not an object")
        tool = data.get("tool_name")
        tool_input = data.get("tool_input")
        if tool in COMMAND_TOOLS and not isinstance(tool_input, dict):
            raise ValueError("tool input is not an object")
        if tool in COMMAND_TOOLS and not strings_in(tool_input):
            raise ValueError("no command text")
        reason = decide_tool(tool, tool_input)
    except Exception as e:  # never fail open on a bug in this file
        reason = f"{_LABEL} could not read this command ({e.__class__.__name__}); asking to be safe"
    if reason:
        sys.stdout.write(json.dumps(ask(reason)) + "\n")


# ---------------------------------------------------------------- other agents

_BLOCK_KEYS = ("codex", "kimi")           # hooks that can only deny, never ask

# --agent mode only: the files other agents keep Bertie's hook, rules and skill in. A command or a file
# write that reaches one is a way to switch the safety hook off (a missing hooks.json means no hook at
# all). Each row is a path as pieces, each piece the names it may have, and what to call it. A path in
# a command is read piece by piece, wildcards and braces included (`~/.cu*/hooks.jso?`,
# `~/.{cursor,codex}/hooks.json`), and matched against these whole paths, so `.gemini/settings.json`
# (Gemini CLI's own project file) and a README that happens to say ".codex" and "rules" are not
# mistaken for them. See _agent_path_hits.
_AGENT_PATHS = (
    (((".cursor",), ("hooks.json",)), "Cursor's hooks file (~/.cursor/hooks.json)"),
    (((".codex",), ("hooks.json",)), "Codex's hooks file (~/.codex/hooks.json)"),
    (((".codex",), ("config.toml",)), "Codex's settings (~/.codex/config.toml), where one line turns hooks off"),
    (((".codex",), ("rules",)), "Codex's rules folder (~/.codex/rules)"),
    (((".gemini",), ("config",), ("hooks.json",)), "Antigravity's hooks file (~/.gemini/config/hooks.json)"),
    (((".gemini",), ("antigravity-cli",), ("settings.json",)),
     "Antigravity's settings (~/.gemini/antigravity-cli/settings.json)"),
    (((".kimi-code", "$kimi_code_home", "${kimi_code_home}"), ("config.toml",)), "Kimi Code's settings (config.toml)"),
    (((".agents",), ("skills",), (registry.NAME,)), "the shared skill (~/.agents/skills/" + registry.NAME + ")"),
)

# The folder a command runs in, for the agents that send it (Codex, Cursor, Kimi Code). The agent's
# adapter puts `cd <folder> &&` in front of the text the checks read, so a command that only does
# harm in the right folder (`mv hook.sh h` inside the install folder) is read as it would run. But
# every check then reads the folder's name too, and an ordinary folder called `bertie` (a clone of
# this repository), `python-keyring` or `svc_payments` would trip the tool-name, Keychain and module
# checks for every command run in it. So the folder is added only where it can change what a command
# does to Bertie: the install folder or anywhere in it, ~/.config, ~/.local, ~/.local/bin, exactly a
# folder that holds an agent file Bertie installed or sits on the way to one (~/.gemini, ~/.agents),
# the home folder itself (`mv .cursor x` there is `mv ~/.cursor x`) and every folder above it (from `/`,
# `rm -rf Users/me/.cursor` is `rm -rf ~/.cursor`), and (when the command climbs out with `..`)
# anywhere deeper inside any of those. Judged by the shape of the path as given and of the real path,
# with links followed.
_INSTALL_SHAPE = tuple(registry.INSTALL_REL.lower().split("/"))
_CWD_SHAPES = tuple(tuple(s.split("/")) for s in (
    ".config", ".local", ".local/bin", ".cursor", ".codex", ".codex/rules", ".gemini", ".gemini/config",
    ".gemini/antigravity-cli", ".kimi-code", ".agents", ".agents/skills", ".agents/skills/" + registry.NAME))


def _path_pieces(path: str) -> List[str]:
    return [p for p in path.lower().split("/") if p and p != "."]


def _home_pieces() -> List[Tuple[str, ...]]:
    """The home folder as pieces, as it is written and as it really is (links followed)."""
    home = os.path.expanduser("~")
    out = [tuple(_path_pieces(home))]
    try:
        out.append(tuple(_path_pieces(os.path.realpath(home))))
    except (OSError, ValueError, RuntimeError):
        pass
    return list(dict.fromkeys(out))


def _cwd_folder(cwd: str, command: str) -> Optional[str]:
    """The folder to put in `cd <folder> &&` in front of `command`, or None when the folder `cwd` cannot
    change what the command does to Bertie (see above). It is `cwd` as it was written when that is what
    matters, else the real folder when only that does (a link to the home folder is the home folder: a
    bare `.cursor` run in it is ~/.cursor). A folder that is not a full path, or cannot be resolved,
    counts as it is: it could be anywhere."""
    if not cwd.startswith("/") or "\x00" in cwd:
        return cwd                                        # a NUL cannot be looked up (some Pythons skip the check)
    try:
        real = os.path.realpath(cwd)
        norm = os.path.normpath(cwd)
        kimi = os.environ.get("KIMI_CODE_HOME", "").strip()
        kimi_real = _path_pieces(os.path.realpath(kimi)) if kimi.startswith("/") else None
    except (OSError, ValueError, RuntimeError):
        return cwd
    climbs = ".." in command
    homes = _home_pieces()

    def matters(path: str) -> bool:
        pieces = _path_pieces(path)
        if any(h[:len(pieces)] == tuple(pieces) for h in homes):
            return True                                   # the home folder, or one above it: `.cursor` or `me/.cursor` reaches ~/.cursor
        n = len(_INSTALL_SHAPE)
        if any(tuple(pieces[i:i + n]) == _INSTALL_SHAPE for i in range(len(pieces))):
            return True                                   # the install folder, or anywhere inside it
        for shape in _CWD_SHAPES:
            k = len(shape)
            if tuple(pieces[-k:]) == shape:
                return True                               # exactly such a folder
            if climbs and any(tuple(pieces[i - k:i]) == shape for i in range(k, len(pieces))):
                return True                               # deeper inside one, and the command can climb out
        return kimi_real is not None and (pieces == kimi_real or (climbs and pieces[:len(kimi_real)] == kimi_real))

    if matters(norm):
        return cwd                                        # the name it was given counts, even if the link leads elsewhere
    if real != norm and matters(real):
        return real                                       # where it really is
    return None


def _cwd_matters(cwd: str, command: str) -> bool:
    """Whether the folder `cwd` can change what `command` does to Bertie (see above)."""
    return _cwd_folder(cwd, command) is not None


def cwd_prefix(cwd: Any, command: str) -> str:
    """`cd <folder> && ` to put in front of the text the checks read, or "" when the folder cannot
    matter. The folder is the one that matters: the real one when `cwd` is a link to it. ValueError for
    a folder that is not text (the hook then answers with its failure answer)."""
    if not isinstance(cwd, str):
        raise ValueError("working folder is not text")
    folder = _cwd_folder(cwd, command) if cwd else None
    return "cd " + shlex.quote(folder) + " && " if folder is not None else ""


def folder_given(*folders: Any) -> bool:
    """Whether the agent said, as a full path, the folder a command runs in. Then a bare `.cursor` in
    the command is that folder's own (a project's, say), not the one in the home folder, until the
    command changes folder itself; with no full path given it could be either. An adapter answers
    this for hook_cwd_known with the folders it passes to cwd_prefix."""
    return any(isinstance(f, str) and f.startswith("/") for f in folders)


# What a command can do with a folder without moving, removing or replacing it: look, or make it. A word
# that ends at an agent's folder (`rm -rf ~/.cursor`) is not a reason when the command is one of these.
_AGENT_READ_ONLY = _READ_ONLY_LEADS | {
    "echo", "printf", "pwd", "test", "[", "du", "df", "tree", "realpath", "readlink", "basename", "dirname",
    "rg", "egrep", "fgrep", "more", "bat", "diff", "cmp", "cd", "pushd", "popd", "chdir", "mkdir", "touch"}
# In text being written to a file, a word that ends at an agent's folder counts only after one of these:
# prose and code comments say ".codex" often, and the script that does the damage starts with the command.
_AGENT_MUTATING = frozenset((
    "rm", "mv", "cp", "ln", "rmdir", "chmod", "chown", "chgrp", "chflags", "truncate", "tee", "sed", "dd", "unlink",
    "rsync", "ditto", "shred", "trash", "xargs", "git", "tar", "zip", "unzip"))
_AGENT_WRAPPERS = frozenset(("env", "sudo", "command", "exec", "nohup", "time", "builtin", "then", "do", "else",
                             "elif", "if", "while", "until", "!", "{", "}", "nice", "doas"))
_AGENT_CD = ("cd", "pushd", "chdir")
_AGENT_SEGMENTS = re.compile(r"[;&|()`\n]+")          # where one command ends and the next starts
_AGENT_WORD_BREAK = re.compile(r"[\s<>=]+")
_ASSIGNMENT = re.compile(r"[A-Za-z_][\w.-]*=")
# A redirection: an optional file number, the operator, and the word it names (`>/dev/null`, `2>err`, `<<< x`,
# `>> log`). Taken out of a command before its first word or its cd target is chosen: `cd >/dev/null .cursor`
# changes into .cursor, not into /dev/null. The operators that hold an `&` or a `|` (`&>`, `>&`, `>|`) are
# first made plain `>` (_plain_redirects), and a file-number copy (`2>&1`) names no file, so it goes.
_FD_COPY = re.compile(r"(?<!\S)\d*[<>]&(?:\d+|-)(?![\w./~$-])")
_AMP_REDIRECT = re.compile(r"&>>?|[<>]&|>\|")
_CMD_REDIRECT = re.compile(r"(?:(?<!\S)\d+)?(?:<<<|<<-?|>>|<>|>|<)\s*[^\s<>]*")
_MAX_FOLDER_STATES = 64       # folders followed through cd; a text that changes into more asks
_MAX_CLIMB = 16               # a run of `..` longer than this is not followed as a folder


def _plain_redirects(text: str) -> str:
    """`text` with a file-number copy (`2>&1`) taken out and `&>`, `>&` and `>|` written as `>`, so that the `&`
    and `|` in them do not end the command in the middle of it."""
    return _AMP_REDIRECT.sub(">", _FD_COPY.sub(" ", text))


def _without_redirects(seg: str) -> str:
    """One command (_AGENT_SEGMENTS has split it off) without its redirections."""
    return _CMD_REDIRECT.sub(" ", seg)


def _agent_lead(tokens: List[str]) -> str:
    """The command a segment runs: its first word, past `env`, `sudo`, `X=1` and the like."""
    wrapped = False
    for tok in tokens:
        low = tok.lower()
        if low in _AGENT_WRAPPERS or _ASSIGNMENT.match(tok):
            wrapped = True
            continue
        if wrapped and low.startswith("-"):
            continue
        return os.path.basename(low)
    return ""


def _squash(pieces: List[str]) -> List[str]:
    """The pieces with each `..` taken against the piece before it."""
    out: List[str] = []
    for p in pieces:
        if p == ".." and out and out[-1] != "..":
            out.pop()
        else:
            out.append(p)
    return out


def _piece_ok(piece: str, names: Tuple[str, ...]) -> bool:
    """Whether a piece of a path in a command (it may hold wildcards) can be one of `names`. As in
    the shell, a piece that starts with `*` never matches a name that starts with a dot."""
    if piece in names:
        return True
    if not any(c in piece for c in "*?["):
        return False
    return any(not (piece.startswith("*") and name.startswith(".")) and _matches(name, piece) for name in names)


def _has_letter(piece: str) -> bool:
    return bool(re.search(r"[a-z0-9]", re.sub(r"[*?\[\]]", "", piece)))


def _window(pieces: List[str], start: int, spec: Tuple[Tuple[str, ...], ...]) -> bool:
    if start < 0 or start + len(spec) > len(pieces):
        return False
    run = pieces[start:start + len(spec)]
    return all(_piece_ok(p, names) for p, names in zip(run, spec)) and any(_has_letter(p) for p in run)


def _agent_specs() -> List[Tuple[Tuple[Tuple[str, ...], ...], str, int, str]]:
    """(path, what, the shortest folder of it that counts for `ends at`, how to show that folder) for
    every row, and for the folder KIMI_CODE_HOME names if it is set."""
    rows = [(spec, what, 1, "") for spec, what in _AGENT_PATHS]
    kimi = os.environ.get("KIMI_CODE_HOME", "").strip().rstrip("/")
    if kimi.startswith("/"):
        try:
            folders = list(dict.fromkeys((kimi, os.path.realpath(kimi))))
        except (OSError, ValueError, RuntimeError):
            folders = [kimi]
        for folder in folders:
            pieces = _path_pieces(folder)
            if pieces:
                rows.append((tuple((p,) for p in pieces) + (("config.toml",),),
                             "Kimi Code's settings (config.toml)", len(pieces), folder))
    return rows


def _expand(word: str) -> List[str]:
    """The word lower-cased, with its braces expanded the way the shell does. _OutOfTime stops the
    whole check; ValueError (too big to expand) is for the caller to skip the word."""
    low = word.lower()
    return _brace_alternatives(low) if "{" in low else [low]


def _check_time() -> None:
    """Stop the agent-file check once its time is up. agent_config_hits then asks, so a text built to keep
    the check busy cannot get a later command through (two of the agents let a call that times out go)."""
    if time.monotonic() >= _deadline[0]:
        raise _OutOfTime("agent check took too long")


def _folder_matters(pieces: List[str], homes: list, known: set) -> bool:
    """Whether a folder a text has changed into can turn a later bare name into an agent file: the home
    folder or one above it, a folder with an agent name in its path, one written with a pattern or a
    variable (it could be any), or a plain climb out of a folder (`..`, `../..`) of _MAX_CLIMB levels or
    fewer. Any other folder (`cd step1`) is left out, so a long text cannot fill the list."""
    here = tuple(pieces)
    if any(h[:len(here)] == here for h in homes):
        return True
    if pieces and all(p == ".." for p in pieces):
        return len(pieces) <= _MAX_CLIMB
    return any(p in known or p.startswith("$") or any(c in p for c in "*?[") for p in pieces)


def _kept_for_a_pattern_only(pieces: List[str], homes: list, known: set) -> bool:
    """Whether a folder _folder_matters keeps is kept for nothing but the variable or the pattern in it
    (`$ROOT`, `build*`, `$HOME/proj`): not the home folder or one above it, not a climb, and no agent name
    in its path."""
    here = tuple(pieces)
    if any(h[:len(here)] == here for h in homes):
        return False
    if pieces and all(p == ".." for p in pieces):
        return False
    return not any(p in known for p in pieces)


def _could_name_an_agent_folder(piece: str, known: set) -> bool:
    """Whether a piece of a cd target is `..`, an agent folder's name, or could stand for one (a variable,
    or a pattern one of the names fits)."""
    if piece == ".." or piece in known or piece.startswith("$"):
        return True
    return any(c in piece for c in "*?[") and _piece_ok(piece, tuple(known))


def _follow_cd(words: List[str], lead: str, states: List[List[str]], homes: list, known: set) -> bool:
    """After a cd, pushd or chdir the folder is the new one, from the next command on. Only its first
    argument that is not a flag is the folder (`cd a b` is an error in bash, and the second word is
    not a folder), and `cd` alone is the home folder. The new folders are built from the ones known
    before this command, and kept only if they can matter (_folder_matters); a folder kept only for the
    variable or pattern in it (`cd $ROOT`) takes a later relative piece only when that is `..` or could be
    an agent folder, so it is not copied by every cd that follows. _TooManyFolders when that would be
    more than _MAX_FOLDER_STATES. True when the command goes somewhere (not `cd .`, `cd -`, a bare
    pushd), whether or not that folder is kept."""
    after = next((i for i, w in enumerate(words) if os.path.basename(w.lower()) == lead), -1) + 1
    target = next((w for w in words[after:] if not (len(w) > 1 and w.startswith("-"))), None)
    if target == "-" or (target is None and lead == "pushd"):
        return False                                # back to the folder before, or a swap: nowhere new
    if target is None:
        target = "~"
    try:
        alts = _expand(target)
    except _OutOfTime:
        raise
    except ValueError:
        return True                                 # too big to expand: the checks on the whole command ask already
    moved = False
    before = list(states)
    for alt in alts:
        _check_time()
        raw = [p for p in alt.split("/") if p and p != "."]
        absolute = alt[:1] in ("/", "~", "$")
        if not raw and not absolute:
            continue                                # `cd .` stays where it is
        moved = True
        reaches = any(_could_name_an_agent_folder(p, known) for p in raw)
        for base in [[]] if absolute else [[]] + before:
            if base and not reaches and _kept_for_a_pattern_only(base, homes, known):
                continue                            # `cd $ROOT; cd src`: src is no agent folder, the pattern stays as it is
            new = _squash(base + raw)
            if new not in states and _folder_matters(new, homes, known):
                if len(states) >= _MAX_FOLDER_STATES:
                    raise _TooManyFolders("too many folders to follow")
                states.append(new)
    return moved


def _agent_path_hits(text: str, command: bool, cwd_known: bool = False) -> List[str]:
    """Reasons for every agent file `text` reaches, read word by word (see _AGENT_PATHS):
      * a word that holds a whole agent path somewhere in it (`~/.cursor/hooks.json`, `.cu*/hooks.jso?`);
      * a word that ends at an agent's folder, or at a folder on the way to one of the files, so that
        the file goes with it (`rm -rf ~/.cursor`, `mv ~/.gemini/config /tmp/x`), unless the command
        only looks at it (`ls ~/.cursor`); in text going into a file only after a command that
        changes things (`rm`, `mv` ...), so prose is left alone;
      * a bare name taken against a folder the text has changed into (`cd ~/.codex && vi config.toml`,
        `cd ~ && mv .cursor x`), which includes the folder the agent runs the command in when
        cwd_prefix put it in front. A bare name with no folder before it (`mv .cursor x`) is read as
        the one in the home folder only in a command, and only when the agent gave no full path for
        the folder it runs in (cwd_known): a project has `.cursor` and `.codex` folders of its own.
        That freedom ends at the first cd, pushd or chdir (to any folder: it may be a link to home,
        made a moment before) and never covers a name with `..` in front (`mv ../.cursor x`), or, in
        a command, a name with a folder in front of it (`mv x/.cursor y`).
    _OutOfTime when the time is up, _TooManyFolders when the text changes into more folders than can
    be followed."""
    _check_time()
    specs = _agent_specs()
    known = {n for spec, _, _, _ in specs for names in spec for n in names}
    homes = [("~",), ("$home",), ("${home}",)] + _home_pieces()
    bare_counts = command and not cwd_known
    hits: List[str] = []
    states: List[List[str]] = []                    # the folders it has changed into so far, as pieces
    after_cd = False                                # has it changed folder, wherever to?
    for seg in _AGENT_SEGMENTS.split(_plain_redirects(text)):
        tokens = _without_redirects(seg).split()          # a redirection in front (`>/dev/null cd x`) is not the command
        if not tokens:
            if seg.split():
                tokens = seg.split()                      # only a redirection: its target is read below as before
            else:
                continue
        lead = _agent_lead(tokens)
        ends_count = lead not in _AGENT_READ_ONLY if command else lead in _AGENT_MUTATING
        words = [w for w in _AGENT_WORD_BREAK.split(seg) if w]
        for word in words:
            _check_time()
            try:
                alts = _expand(word)
            except _OutOfTime:
                raise
            except ValueError:
                continue                            # too big to expand: the checks on the whole command ask already
            for alt in alts:
                _check_time()
                raw = [p for p in alt.split("/") if p and p != "."]
                if raw and "*" not in alt and "?" not in alt and "[" not in alt and ".." not in raw \
                        and not any(p in known for p in raw):
                    continue                        # nothing in it can be part of an agent path
                shapes = [raw, _squash(raw)] if ".." in raw else [raw]
                absolute = alt[:1] in ("/", "~", "$")
                for base in [[]] if absolute else [[]] + states:
                    counts = ends_count and bool(base or absolute or ".." in raw or bare_counts or after_cd)
                    for shape in shapes:
                        _check_pieces(_squash(base + shape) if base else shape, specs, counts, hits, homes,
                                      ends_count and command)
        if lead in _AGENT_CD:
            cd_words = [w for w in _AGENT_WORD_BREAK.split(_without_redirects(seg)) if w]
            if _follow_cd(cd_words, lead, states, homes, known):
                after_cd = True
        elif any(os.path.basename(w.lower()) in _AGENT_CD for w in words):
            after_cd = True                         # `sh -c 'cd /tmp/x; rm -rf .cursor'`: a cd inside a wrapped command
    return hits


def _check_pieces(pieces: List[str], specs: list, ends_count: bool, hits: List[str], homes: list,
                  nested_counts: bool = False) -> None:
    """Add a reason for every agent path in `pieces`. A path that ends at an agent's folder is a reason when
    `ends_count`, and (`nested_counts`) also when it has a folder in front of the folder's name: `x/.cursor`
    could be a link to the home folder made a moment ago, which a bare `.cursor` in a project cannot."""
    first = next((i for i, p in enumerate(pieces) if p != ".."), len(pieces))   # no name starts with `..`
    for spec, what, least, shown in specs:
        n = len(spec)
        if any(_window(pieces, i, spec) for i in range(first, len(pieces) - n + 1)):
            hits.append(f"it names {what}, where {registry.DISPLAY_NAME}'s safety hook is set up")
        if ends_count or nested_counts:
            for m in range(max(least, 1), n):
                if _window(pieces, len(pieces) - m, spec[:m]) and (ends_count or len(pieces) - m > first):
                    # "~/" only where the folder is the home folder's: a bare `.cursor` may be a project's
                    at_home = tuple(pieces[:len(pieces) - m]) in homes
                    folder = shown or ("~/" if at_home else "") + "/".join(names[0] for names in spec[:m])
                    hits.append(f"it moves, removes or changes {folder}, the folder where "
                                f"{registry.DISPLAY_NAME}'s safety hook is set up ({what})")
                    break


def _normalise_lines(text: str) -> str:
    """normalise() that keeps the line breaks, which end one command and start the next."""
    s = text.replace("\\\n", " ")
    s = re.sub(r"\$(?=['\"])", "", s)
    s = re.sub(r"[\\'\"]", "", s)
    return re.sub(r"[^\S\n]+", " ", s)


def agent_config_hits(norm: str, command: bool = True, cwd_known: bool = False) -> List[str]:
    """Reasons for every agent file the (normalised, line breaks kept) text reaches: see
    _agent_path_hits. `command` is False for text being written to a file; `cwd_known` is True when
    the agent gave a full path for the folder the command runs in (folder_given). A check that cannot
    finish in time, or that finds more folders to follow than it can, says so as a reason to ask:
    stopping quietly would let the call through."""
    hits: List[str] = []
    _deadline[0] = time.monotonic() + _PATTERN_SECONDS
    variants = [norm]
    if len(norm) <= _MAX_COMMAND:
        variants.append(_collapse(norm))            # [..] and (..) groups read as ? and *, quoted spaces included
    for text in variants:
        try:
            hits += _agent_path_hits(text, command, cwd_known)
        except _TooManyFolders:
            hits.append("too many folders to follow; asking to be safe")
            break
        except _OutOfTime:
            hits.append("the agent files check could not finish in time; asking to be safe")
            break
        except ValueError:
            continue
    return list(dict.fromkeys(hits))


# A backstop for a path put together at run time (`rm "$(echo ~/.cursor)/hooks.json"`, `f=hooks.json;
# rm ~/.cursor/$f`, `os.path.join(os.path.expanduser('~'), '.cursor', 'hooks.json')`), which the
# word-by-word reading above cannot see whole. It is the old reading, a folder's name and a file's
# name anywhere in the text, kept to where it points at the home folder and does harm:
#   * in a command: the text mentions the home folder, at least one command in it is not read-only,
#     and only the command is read (never the folder the agent runs it in, which would make every
#     command run in ~/.codex "name" rules);
#   * in text going into a file: the folder and the file name are joined by path punctuation
#     (a comma, `/`, `+`, a bracket or quotes) and the home folder is mentioned on the same line.
# A project's own `.cursor` and a README's "hooks.json" never mention the home folder, so they stay free.
# Both read under the same clock as the rest (_check_time: out of time asks), and in a file a line over
# _MAX_COMMAND characters that mentions the home folder asks instead of being read.
# Rows: the folder's name, the file's name, and what to call it (as in _AGENT_PATHS).
_LOOSE_PAIRS = tuple((re.compile(folder, re.I), re.compile(name, re.I), what) for folder, name, what in (
    (r"\.cursor(?![\w-])", r"(?<![\w-])hooks\.json", "Cursor's hooks file (~/.cursor/hooks.json)"),
    (r"\.codex(?![\w-])", r"(?<![\w-])hooks\.json", "Codex's hooks file (~/.codex/hooks.json)"),
    (r"\.codex(?![\w-])", r"(?<![\w-])config\.toml", "Codex's settings (~/.codex/config.toml), where one line turns hooks off"),
    (r"\.codex(?![\w-])", r"(?<![\w-])rules(?![\w-])", "Codex's rules folder (~/.codex/rules)"),
    (r"\.gemini(?![\w-])", r"(?<![\w-])hooks\.json", "Antigravity's hooks file (~/.gemini/config/hooks.json)"),
    (r"antigravity-cli(?![\w-])", r"(?<![\w-])settings\.json", "Antigravity's settings (~/.gemini/antigravity-cli/settings.json)"),
    (r"\.kimi-code(?![\w-])|kimi_code_home", r"(?<![\w-])config\.toml", "Kimi Code's settings (config.toml)"),
    (r"\.agents(?![\w-])", r"(?<![\w-])skills/+" + _N + r"(?![\w-])", "the shared skill (~/.agents/skills/" + registry.NAME + ")"),
))
# Between two pieces of a path: an optional quote, then one or more of `, / + ) }`, each followed by
# optional spaces, an optional quote and optional spaces. Written so that there is only one way to read any
# text (each round starts at a punctuation mark, and a space or a quote belongs to one place only): a
# match that fails then takes time in proportion to the text. The easier `\s*['"`]?\s*` after the mark
# can split a run of spaces two ways, and a line of `/ / / ...` took twice as long with every `/ `.
_JOIN = r"""['"`]?\s*(?:[,/+)}](?:\s*['"`])?\s*)+"""


def _joined(*pieces: str) -> "re.Pattern":
    return re.compile(_JOIN.join(pieces), re.I)


def _path_pieces_regex(names: Tuple[str, ...]) -> str:
    alternatives = [re.escape(n.lstrip("${").rstrip("}")) for n in names]      # `$kimi_code_home` is written bare in code
    return "(?:" + "|".join(dict.fromkeys(alternatives)) + ")"


# every row of _AGENT_PATHS with its pieces joined by path punctuation, for text going into a file
_JOINED_PATHS = tuple(
    (_joined(*[_path_pieces_regex(names) for names in spec[:-1]] + [_path_pieces_regex(spec[-1]) + r"(?![\w-])"]), what)
    for spec, what in _AGENT_PATHS)
# words that say "the home folder": ~ , $HOME, ${HOME}, and the calls code makes to find it
# (`os.environ['HOME']`, `process.env.HOME` too)
_HOME_WORD = (r"(?<![\w.-])~(?![\w-])|\$\{?home\b|expanduser|homedir|path\.home|kimi_code_home"
              r"|['\"`]home['\"`]|\benv\.home\b")


def _home_regex() -> "re.Pattern":
    """Matches a mention of the home folder: any of _HOME_WORD, or the real home folder's path (as
    written and with links followed), or the folder KIMI_CODE_HOME names if it is set."""
    folders = [os.path.expanduser("~"), os.environ.get("KIMI_CODE_HOME", "").strip()]
    try:
        folders.append(os.path.realpath(folders[0]))
    except (OSError, ValueError, RuntimeError):
        pass
    parts = [_HOME_WORD] + [re.escape(f.rstrip("/")) + r"(?![\w.-])" for f in dict.fromkeys(folders)
                            if f.startswith("/") and len(f.rstrip("/")) > 1]
    return re.compile("|".join(parts), re.I)


_LOOSE_SLOW = "the check for a path built up from the home folder could not finish in time; asking to be safe"


def loose_command_hits(command: str, anywhere: bool = False) -> List[str]:
    """Reasons from the backstop above for a command (the command alone, not the folder it runs in). Out of
    time, it says so as a reason to ask. `anywhere` is True when the command runs in the home folder, a folder
    above it or a folder nobody named in full: then a bare `.cursor/$f` is the home folder's own, and the
    command need not mention the home folder (a project's own `.cursor` is left alone, as it was)."""
    _deadline[0] = time.monotonic() + _PATTERN_SECONDS
    try:
        _check_time()
        if not anywhere and not _home_regex().search(command):
            return []                                    # only a path from the home folder can be one of Bertie's
        norm = _normalise_lines(command)
        leads = [_agent_lead(seg.split()) for seg in _AGENT_SEGMENTS.split(norm) if seg.split()]
        if all(lead in _AGENT_READ_ONLY for lead in leads):
            return []                                    # it only looks, or makes
        pairs = list(_LOOSE_PAIRS)
        kimi = os.environ.get("KIMI_CODE_HOME", "").strip().rstrip("/")
        if kimi.startswith("/"):
            pairs.append((re.compile(re.escape(kimi) + r"(?![\w.-])", re.I), re.compile(r"(?<![\w-])config\.toml", re.I),
                          "Kimi Code's settings (config.toml)"))
        hits = []
        for folder, name, what in pairs:
            _check_time()
            if folder.search(norm) and name.search(norm):
                hits.append(f"it names {what}, where {registry.DISPLAY_NAME}'s safety hook is set up")
        return hits
    except _OutOfTime:
        return [_LOOSE_SLOW]


def loose_file_hits(text: str) -> List[str]:
    """Reasons from the backstop above for text going into a file. The time is watched on every line, a line
    that mentions the home folder and is longer than _MAX_COMMAND characters is not read (it asks, like a
    command that long), and out of time it asks too."""
    home = _home_regex()
    hits: List[str] = []
    _deadline[0] = time.monotonic() + _PATTERN_SECONDS
    try:
        for line in text.split("\n"):
            _check_time()
            if not home.search(line):
                continue
            if len(line) > _MAX_COMMAND:
                hits.append(f"a line longer than {_MAX_COMMAND} characters that mentions the home folder; "
                            "too long to check, asking to be safe")
                continue
            line = re.sub(r"[^\S\n]+", " ", line)
            for pattern, what in _JOINED_PATHS:
                _check_time()
                if pattern.search(line):
                    hits.append(f"it names {what}, where {registry.DISPLAY_NAME}'s safety hook is set up")
    except _OutOfTime:
        hits.append(_LOOSE_SLOW)
    return hits


def _with_hits(reason: Optional[str], hits: List[str]) -> Optional[str]:
    """`reason` (what decide() or decide_file_text() said, or None) with more reasons added."""
    if not hits:
        return reason
    return _finish(hits) if not reason else reason + "; " + "; ".join(hits)


def _runs_at_home(command: str, checked: Optional[str], cwd_known: bool) -> bool:
    """Whether a bare folder name in the command could be one in the home folder: the agent did not give the
    folder it runs in as a full path, or it did and cwd_prefix put that folder in front of the command
    because it is the home folder, one above it or an agent's own (`checked` is then not the command), or the
    command climbs with `..`, which can leave a project inside the home folder for the home folder itself."""
    return not cwd_known or (checked is not None and checked != command) or ".." in command


def decide_agent_command(command: str, checked: Optional[str] = None, cwd_known: bool = False) -> Optional[str]:
    """decide() for a command from another agent: the same reasons, plus any agent hook file it names.
    `checked` is the text the checks run on when the agent says more than the command (the folder it
    runs in, say); `cwd_known` is True when it gave that folder as a full path (see folder_given)."""
    scan = command if checked is None else checked
    hits = agent_config_hits(_normalise_lines(scan), cwd_known=cwd_known) \
        + loose_command_hits(command, _runs_at_home(command, checked, cwd_known))
    return _with_hits(decide(scan), list(dict.fromkeys(hits)))


_MAX_FILE_BYTES = 256 * 1024     # more text than this going into a file by another agent asks unread
_MAX_LONG_WORDS = 4              # about this many runs of _MAX_COMMAND characters without a space, and no more


def file_too_big(text: str) -> Optional[str]:
    """The reason when text going into a file is too big to read in the time an agent allows (None if it can
    be read): over _MAX_FILE_BYTES in all, or a run without any space longer than _MAX_COMMAND, or runs
    without a space that together would take as long to read as _MAX_LONG_WORDS of the longest allowed (the
    install-folder pattern takes the square of a run's length, and an agent lets a call that times out go).
    One pass over the words; no pattern is used, so this cannot be slowed."""
    if len(text) > _MAX_FILE_BYTES // 4 and len(text.encode("utf-8", "replace")) > _MAX_FILE_BYTES:
        return "the file is too large to check (over 256 KB); asking to be safe"
    work = 0
    for word in text.split():
        n = len(word)
        if n > _MAX_COMMAND:
            return "a line is too long to check; asking to be safe"
        work += n * n if n > 1000 else 0
    if work > _MAX_LONG_WORDS * _MAX_COMMAND * _MAX_COMMAND:
        return "a line is too long to check; asking to be safe"
    return None


def decide_agent_file(text: str) -> Optional[str]:
    """decide_file_text() for text going into a file (or a path being deleted) for another
    agent: the same reasons, plus any agent hook file it names."""
    hits = agent_config_hits(_normalise_lines(text), command=False) + loose_file_hits(text)
    return _with_hits(decide_file_text(text),
                      [h + " (in text being written to a file)" for h in dict.fromkeys(hits)])


def agent_from_argv(argv: List[str]) -> Optional[str]:
    """The key after --agent (or in --agent=KEY); "" if it has none; None if --agent is absent."""
    for i, a in enumerate(argv):
        if a == "--agent":
            return argv[i + 1] if i + 1 < len(argv) else ""
        if a.startswith("--agent="):
            return a[len("--agent="):]
    return None


def block_tail(label: str, asker: Optional[str] = None) -> str:
    """Ends every reason a block-only agent is refused with. `asker` is who asks the person
    when the command stands alone ("Codex asks you" unless the adapter says otherwise)."""
    return (f"Run the {registry.DISPLAY_NAME} command on its own, starting the line, e.g. "
            f"`{registry.command_for('mail')} send ...` (use --body-file for text with special "
            f"characters), so {asker or label + ' asks you'} first.")


_SETTINGS_TAIL = f"This touches {registry.DISPLAY_NAME}'s safety settings; ask the user to do it by hand if it is needed."


def decide_block(adapter: Any, command: str, checked: Optional[str] = None, cwd_known: bool = False) -> Optional[str]:
    """The policy for an agent whose hook cannot ask. A gated action on its own, which the
    agent's own ask rule surely matches (adapter.rules_cover), is left to that rule: it
    prompts, and the Mac window and Touch ID follow. Any other reason blocks. When a send is the
    only reason and the rule would miss it, an adapter's cover_hint (if it has one) says why.
    `checked` is the text the checks run on when the agent says more than the command (the
    folder it runs in, say); rules_cover is always asked about the command alone. `cwd_known` is True
    when the agent gave that folder as a full path (see folder_given)."""
    scan = command if checked is None else checked
    gated, other = decide_parts(scan)
    other = other + agent_config_hits(_normalise_lines(scan), cwd_known=cwd_known) \
        + loose_command_hits(command, _runs_at_home(command, checked, cwd_known))
    if not gated and not other:
        return None
    if gated and not other and adapter.rules_cover(command) is True:
        return None
    asker = getattr(adapter, "ask_clause", None)
    who = asker() if asker is not None else None
    if gated and not other:                                   # only a send, and the ask rule would not match it: say why
        hint = getattr(adapter, "cover_hint", None)
        why = hint(command) if hint is not None else None
        if why:
            return _finish(gated) + ". " + why + f", so {who or adapter.LABEL + ' asks you'} first."
    if not gated:                                             # nothing to send here: the advice about a send would mislead
        return _finish(other) + ". " + _SETTINGS_TAIL
    return _finish(gated + other) + ". " + block_tail(adapter.LABEL, who)


def agent_reason(adapter: Any, data: dict) -> Optional[str]:
    """None for no opinion, else the reason, for one hook call in the agent's format."""
    style = adapter.HOOK_STYLE
    if style not in ("ask", "block"):
        raise ValueError(f"unknown hook style {style!r}")
    kind, text = adapter.hook_input(data)
    if kind == "none":
        return None
    if not isinstance(text, str):
        raise ValueError("hook input gave no text")
    if kind == "ask":
        if not text:
            raise ValueError("an ask without a reason")
        return text
    if kind == "file":
        if adapter.KEY == "claude":
            return decide_file_text(text)
        too_big = file_too_big(text)                          # before anything reads the text: see file_too_big
        return _finish([too_big]) if too_big else decide_agent_file(text)
    if kind == "command":
        scan = getattr(adapter, "hook_scan_text", None)      # more than the command to check, if the agent sends more
        checked = scan(data, text) if scan is not None else None
        if checked is not None and not isinstance(checked, str):
            raise ValueError("hook input gave no text")
        given = getattr(adapter, "hook_cwd_known", None)     # did the agent say, in full, the folder it runs in?
        known = bool(given(data)) if given is not None else False
        if style == "block":
            return decide_block(adapter, text, checked, known)
        if adapter.KEY == "claude":
            return decide(text)
        return decide_agent_command(text, checked, known)
    raise ValueError(f"unknown input kind {kind!r}")


def fallback_answer(key: str, reason: str) -> str:
    """The safe answer when the adapter cannot be used at all (missing file, a bug in it).
    The same answers hook.sh gives when no Python can start."""
    if key in _BLOCK_KEYS:
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": reason}}) + "\n"
    if key == "cursor":
        return json.dumps({"permission": "deny", "user_message": reason, "agent_message": reason}) + "\n"
    if key == "antigravity":
        return json.dumps({"decision": "force_ask", "reason": reason}) + "\n"
    return json.dumps(ask(reason)) + "\n"


def agent_answer(key: str, stdin: Any) -> Tuple[str, int]:
    """(stdout text, exit code) for one hook call from agent `key`, JSON read from `stdin`.
    Never raises, and the exit code is always 0."""
    adapter = None
    try:
        import agents                        # the adapter package next to this file
        adapter = agents.get(key)
        raw = stdin.read()
        if not raw.strip() and getattr(adapter, "EMPTY_INPUT_IS_FAILURE", False):
            raise ValueError("no hook input")                # an agent that always sends something: silence is not a pass
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("hook input is not an object")
        text, code = adapter.hook_answer(agent_reason(adapter, data))
        if code != 0 or not isinstance(text, str):
            raise ValueError("adapter gave an answer hook.py cannot pass on")
        return text, 0
    except Exception as e:  # never fail open on a bug in this file or in an adapter
        if adapter is None:
            blocking = key in _BLOCK_KEYS or key == "cursor"
        else:
            says = getattr(adapter, "failure_blocks", None)      # the answer hook_failure will give, said ahead
            blocking = says() if says is not None else getattr(adapter, "HOOK_STYLE", None) == "block"
        what = "read this command" if adapter is not None else f"load its {key or 'agent'} adapter"
        reason = f"{_LABEL} could not {what} ({e.__class__.__name__}); {'blocking' if blocking else 'asking'} to be safe"
        try:
            if adapter is None:
                raise LookupError("no adapter")
            text, code = adapter.hook_failure(reason)
            if code == 0 and isinstance(text, str):
                return text, 0
        except Exception:
            pass
        return fallback_answer(key, reason), 0


def main_agent(key: str) -> None:
    text, _ = agent_answer(key, sys.stdin)
    if text:
        sys.stdout.write(text)


if __name__ == "__main__":
    agent = agent_from_argv(sys.argv[1:])
    try:
        if agent is None:
            main()
        else:
            main_agent(agent)
    except Exception:
        blocks = agent in _BLOCK_KEYS or agent == "cursor"
        reason = _LABEL + " failed; " + ("blocking" if blocks else "asking") + " to be safe"
        sys.stdout.write(json.dumps(ask(reason)) + "\n" if agent is None else fallback_answer(agent, reason))
    sys.exit(0)
