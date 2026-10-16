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
"""
from __future__ import annotations

import fnmatch
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402

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


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.stdout.write(json.dumps(ask(_LABEL + " failed; asking to be safe")) + "\n")
    sys.exit(0)
