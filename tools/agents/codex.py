"""Codex: the skill in ~/.agents/skills, ask rules in ~/.codex/rules/bertie.rules and the
PreToolUse hook in ~/.codex/hooks.json.

Codex's hook can only allow or block, never ask (HOOK_STYLE "block"). The asking is done by
Codex's own rules: a prefix rule with decision "prompt" for every gated action, so Codex asks
before it runs, and the Mac window and Touch ID follow. The hook blocks the ways round those
rules, and leaves a plain gated command alone only when the rules surely match it (rules_cover).

The rules file also allows the commands that only read, each by name: they need the Keychain,
which Codex's sandbox very likely blocks. An action that is not named there (a download, which
writes a file, or anything with a flag in front of the action) gets no allow rule and goes the
way Codex sends everything else. Codex takes the most restrictive of the rules that match a
command, so a gated action still prompts. If a prompt rule turns out unable to leave the sandbox,
`--codex-sends allow` writes "allow" for the gated actions as well, and the Mac window and
Touch ID are then the only gate for a send.
"""
from __future__ import annotations

import json
import re
import shlex
import string
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import agents
import kit_setup
import registry
from kit_setup import SetupError

KEY = "codex"
LABEL = "Codex"
HOOK_STYLE = "block"

RULES_REL = ".codex/rules/" + registry.NAME + ".rules"
HOOKS_REL = ".codex/hooks.json"
HOOKS_PATH = ["hooks", "PreToolUse"]
HOOK_MATCHER = "^(Bash|apply_patch)$"
HOOK_TIMEOUT = 30
SENDS = ("prompt", "allow")               # --codex-sends: what Codex does before a gated action

RULES_MARKER = ("# " + registry.NAME + ": Codex rules written by the " + registry.NAME
                + " installer; `" + registry.NAME + " uninstall` removes this file")
_is_ours = agents.marker_first_line(RULES_MARKER)

# The actions of each command that only read and print. Not here on purpose: download and
# attachments (they write files to any folder), draft (--attach reads any file), login, and
# every gated action. A test holds this list against the real command parsers, so a new action
# has to be put on one side or the other.
READS: Dict[str, Tuple[str, ...]] = {
    "bertie-mail": ("drafts", "read", "search", "show-draft", "thread", "whoami"),
    "bertie-calendar": ("agenda", "event", "freebusy"),
    "bertie-chat": ("read", "spaces"),
    "bertie-drive": ("list", "ls"),
    "bertie": ("check", "log", "whoami"),
}
NOT_READS = ("attachments", "download", "draft", "login")


# A hook call with no input at all is a failure here; Codex always sends something.
EMPTY_INPUT_IS_FAILURE = True


def detect(home: Path) -> bool:
    return (Path(home) / ".codex").is_dir()


def hook_files(ctx) -> List[Path]:
    return [Path(ctx.home) / HOOKS_REL]


# ---------------------------------------------------------------- the rules file

def starlark_string(text: str) -> str:
    """A double-quoted Starlark string literal that reads back as `text`. Plain ASCII out:
    anything else, and every control character, is escaped."""
    out = ['"']
    for ch in text:
        n = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif n < 0x20 or n == 0x7F:
            out.append("\\x%02x" % n)
        elif n > 0x7E:
            out.append("\\u%04x" % n if n <= 0xFFFF else "\\U%08x" % n)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _comment(text: str) -> str:
    """One comment line from text that may hold anything: a control character or a line break
    would end the comment early, and non-ASCII text is written as escapes like everywhere else."""
    return "# " + "".join(c if 0x20 <= ord(c) < 0x7F else (" " if ord(c) < 0x20 or ord(c) == 0x7F else
                                                          "\\u%04x" % ord(c) if ord(c) <= 0xFFFF else "\\U%08x" % ord(c))
                          for c in text)


def _pattern(items: List[Union[str, List[str]]]) -> str:
    """A pattern list; an item that is itself a list is a choice between words (Codex accepts
    that at any place in a pattern)."""
    parts = [("[" + ", ".join(starlark_string(w) for w in p) + "]") if isinstance(p, list) else starlark_string(p)
             for p in items]
    return "[" + ", ".join(parts) + "]"


def _prefix_rule(pattern: List[Union[str, List[str]]], decision: str, justification: Optional[str],
                 examples: List[str]) -> List[str]:
    """One prefix_rule(...) call. Codex checks the `match` examples when it loads the file, so
    each one must start with the pattern."""
    assert decision in ("allow", "prompt") and examples
    lines = ["prefix_rule(",
             "    pattern = " + _pattern(pattern) + ",",
             f'    decision = "{decision}",']
    if justification:
        lines.append("    justification = " + starlark_string(justification) + ",")
    lines.append("    match = [")
    lines += ["        " + starlark_string(e) + "," for e in examples]
    lines += ["    ],", ")"]
    return lines


def rules_text(sends: str = "prompt") -> str:
    """The whole of ~/.codex/rules/bertie.rules. `sends` is what Codex does before a gated
    action: "prompt" (it asks) or "allow" (it does not; see --codex-sends)."""
    if sends not in SENDS:
        raise SetupError(f"--codex-sends must be prompt or allow, not {sends!r}")
    if sends == "prompt":
        intro = ['# Codex asks before each action under "Ask first" (decision "prompt"); the Mac confirmation',
                 "# window and Touch ID follow."]
    else:
        intro = ['# --codex-sends allow: Codex does NOT ask before the actions under "Ask first" (decision',
                 '# "allow"); the Mac confirmation window and Touch ID are the only gate for them.']
    lines = [
        RULES_MARKER,
        "#",
        *intro,
        "# The commands that only read are allowed by name: they need the Keychain, which Codex's",
        "# sandbox probably blocks. Nothing else is allowed, so a download or a flag in front of an",
        "# action goes the usual way. When several rules match a command Codex applies the most",
        "# restrictive one. The installer rewrites this file each time it runs; edit nothing here.",
        "",
        "# Reads",
    ]
    for tool in registry.tool_names():
        words = list(READS.get(tool, ())) + ["--help"]
        lines += ["", *_prefix_rule([tool, words], "allow", None, [f"{tool} {w}" for w in words])]
    lines += ["", "# Ask first"]
    for tool, acts in registry.gated_actions().items():
        for act, desc in acts.items():
            why = f"{registry.DISPLAY_NAME}: {desc}"
            lines += ["", _comment(desc),
                      *_prefix_rule([tool, act], sends, why if sends == "prompt" else None,
                                    [f"{tool} {act}", f"{tool} {act} --dry-run"])]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- the hook

def hook_group(ctx) -> Dict[str, Any]:
    return {"matcher": HOOK_MATCHER,
            "hooks": [{"type": "command", "command": agents.hook_command(ctx, KEY), "timeout": HOOK_TIMEOUT}]}


def hook_input(data: dict) -> Tuple[str, str]:
    """{"tool_name": "Bash", "tool_input": {"command": "..."}} is a command; apply_patch (whose
    "command" is the patch text, each added line starting with "+") is a file being written; any other
    tool is not ours to watch."""
    import hook
    tool, tool_input = data.get("tool_name"), data.get("tool_input")
    if tool == "Bash":
        if not isinstance(tool_input, dict):
            raise ValueError("tool input is not an object")
        command = tool_input.get("command")
        if not isinstance(command, str):
            raise ValueError("no command text")
        return "command", command
    if tool == "apply_patch":
        if not isinstance(tool_input, dict) or not hook.strings_in(tool_input):
            raise ValueError("no patch text")
        # every line a patch adds starts with "+", which would make `+rm` the command word: take one off
        return "file", re.sub(r"(?m)^\+", "", "\n".join(hook.strings_in(tool_input)))
    return "none", ""


# tool_input fields hook_scan_text does not read as text to check: the folders (read as folders) and the
# prose that explains a request to the person. Any other field, known or not, is read.
_NOT_SCANNED = ("command", "workdir", "cwd", "description", "justification")


def hook_scan_text(data: dict, command: str) -> Optional[str]:
    """What the checks read for a Bash call: the folder it runs in (the session's cwd and any
    workdir or cwd in tool_input; each only where it can matter, see hook.cwd_prefix), then the
    command, then every other text field in tool_input, so none of them is a way round the
    checks. The fields that only explain a request to the person (description, justification) are
    not read: they run nothing. The ask rule is still judged on the command alone (see rules_cover)."""
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    import hook
    folders = [data.get("cwd"), tool_input.get("workdir"), tool_input.get("cwd")]
    if any(f is not None and not isinstance(f, str) for f in folders):
        raise ValueError("working folder is not text")
    text = "".join(hook.cwd_prefix(f, command) for f in folders if f) + command
    rest = [s for k, v in tool_input.items() if k not in _NOT_SCANNED for s in hook.strings_in(v)]
    return "\n".join([text] + rest)


def hook_cwd_known(data: dict) -> bool:
    """Whether a full path was given for the folder the command runs in (see hook.folder_given)."""
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    import hook
    return hook.folder_given(data.get("cwd"), tool_input.get("workdir"), tool_input.get("cwd"))


def _recorded_sends() -> str:
    """What the installer recorded for --codex-sends, read from the manifest beside this file's
    install folder; "" if it cannot be read."""
    try:
        manifest = json.loads((Path(__file__).resolve().parent.parent / kit_setup.MANIFEST_NAME).read_text())
        sends = manifest["agents"][KEY]["sends"]
        return sends if sends in SENDS else ""
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def ask_clause() -> str:
    """Who asks the person when a send stands alone, for the end of a block message: Codex,
    unless the rules were written for --codex-sends allow, when only the Mac window asks."""
    return "the Mac window asks you" if _recorded_sends() == "allow" else "Codex asks you"


def hook_answer(reason: Optional[str]) -> Tuple[str, int]:
    if not reason:
        return "", 0
    return agents.pretooluse_answer("deny", reason), 0


def hook_failure(reason: str) -> Tuple[str, int]:
    return agents.pretooluse_answer("deny", reason), 0


# What can stand in a word outside quotes. Anything else (a wildcard, a brace, #, !, ^, $, a
# backtick, a backslash, ~, = % + , ; & | ( ) < >) may make Codex read the line as one opaque
# script that its rules do not match, so such a line is not "covered". (= % + , ~ are fine
# inside quotes; outside they can be an assignment, a tilde expansion or a pattern.)
_PLAIN = frozenset(string.ascii_letters + string.digits + "_-./:@")
_PUNCTUATION = frozenset("();<>|&")


def _plain_words(command: str) -> bool:
    """Words made only of _PLAIN characters, blanks and closed quotes, where a quote never
    touches other text (`--x="a b"` and `'a''b'` are one word made of pieces). Inside quotes
    anything goes (the caller has already refused $, backtick, backslash and control characters)."""
    quote = ""
    for i, c in enumerate(command):
        if quote:
            if c == quote:
                quote = ""
                if i + 1 < len(command) and command[i + 1] not in " \t":
                    return False                            # a quote closing against more text
        elif c in "'\"":
            if i > 0 and command[i - 1] not in " \t":
                return False                                # a quote opening against text before it
            quote = c
        elif c not in _PLAIN and c not in " \t":
            return False
    return not quote


def rules_cover(command: str) -> bool:
    """True only when the ask rules surely match this exact command string, so Codex will
    prompt. Conservative: a doubt is False. The command must start with a gated `<tool>
    <action>` written plainly (no leading blank, quotes or upper case in those two words), and
    the rest may only be plain words and quoted text: no newline, $, backtick or backslash
    anywhere, no unquoted ; & | ( ) < > ~ = % + , or wildcard, brace, # or !, no unbalanced
    quote, no quote that touches the text beside it."""
    try:
        return _rules_cover(command)
    except Exception:                       # shlex on input it dislikes: not sure, so not covered
        return False


def _rules_cover(command: str) -> bool:
    if not isinstance(command, str) or any(c in command for c in "$`\\"):
        return False
    if any(ord(c) < 0x20 and c != "\t" or ord(c) == 0x7F for c in command):    # newline, return, ...
        return False
    pair = next(((tool, act) for tool, acts in registry.gated_actions().items() for act in acts
                 if command == f"{tool} {act}" or command.startswith((f"{tool} {act} ", f"{tool} {act}\t"))), None)
    if pair is None or not _plain_words(command):
        return False
    if shlex.split(command)[:2] != list(pair):
        return False
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    return not any(tok and all(c in _PUNCTUATION for c in tok) for tok in lex)


_QUOTE_FIRST = frozenset(",=~")             # plain inside quotes, but outside them Codex does not match the rule


def _swap_unquoted(command: str, chars: frozenset) -> str:
    """The command with each of `chars` that stands outside quotes replaced by a letter."""
    out, quote = [], ""
    for c in command:
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c in chars:
            c = "x"
        out.append(c)
    return "".join(out)


def cover_hint(command: str) -> Optional[str]:
    """Why a send that starts the line the right way is still not left to Codex's ask rule, when
    the only reason is an unquoted , = or ~ in it (the line is then read as a script the rule does
    not match); else None. A clause for the end of a block message, in place of the usual tail."""
    if not isinstance(command, str) or rules_cover(command):
        return None
    swapped = _swap_unquoted(command, _QUOTE_FIRST)
    if swapped == command or not rules_cover(swapped):
        return None
    return ("Codex's ask rule does not match a send with a , = or ~ outside quotes, so this one would not be asked "
            "about: quote the argument with , = or ~ (e.g. --to 'a@example.com,b@example.com') or write the full path")


# ---------------------------------------------------------------- install and uninstall

def preflight(ctx, kit_dir: Path) -> None:
    """Every refusal, before anything is written: a file that is not Bertie's, a link, a hooks
    file that cannot be merged safely, a folder that is really a file."""
    home = Path(ctx.home)
    for rel in (".codex", ".codex/rules", ".agents", ".agents/skills", ".agents/skills/" + registry.NAME):
        if (home / rel).exists() and not (home / rel).is_dir():
            raise SetupError(f"{home / rel} is a file, not a folder. Move it out of the way and run the installer again.")
    rules = home / RULES_REL
    if rules.exists() or rules.is_symlink():
        try:
            current = rules.read_text(encoding="utf-8") if rules.is_file() and not rules.is_symlink() else None
        except (OSError, UnicodeDecodeError):
            current = None
        if current is None or not _is_ours(current):
            raise SetupError(f"{rules} already exists and was not made by this installer. "
                             "Move it out of the way and run the installer again.")
    agents.preflight_skill(ctx, agents.SHARED_SKILL_REL, kit_dir)
    agents.preflight_json(ctx, HOOKS_REL, [agents.Append(HOOKS_PATH, [hook_group(ctx)])], KEY)
    rules_text(_sends(ctx))                                    # a choice that is neither prompt nor allow


def _sends(ctx) -> str:
    """--codex-sends if it was given this time, else what the last install recorded, else prompt.
    A reinstall never quietly turns the choice back to prompt (or to allow)."""
    given = agents.option(ctx, "codex_sends", "")
    if given:
        return given
    recorded = agents.peek(kit_setup.load_manifest(ctx), KEY).get("sends")
    return recorded if recorded in SENDS else "prompt"


def install(ctx, kit_dir: Path) -> List[str]:
    sends = _sends(ctx)
    rules = rules_text(sends)                                  # SetupError for a choice that is neither
    group = hook_group(ctx)
    preflight(ctx, kit_dir)
    # the rules file goes first: it makes ~/.codex if it is missing, and uninstall removes it last
    rules_changed = agents.write_owned_file(ctx, KEY, RULES_REL, rules, _is_ours)
    skill_changed = agents.acquire_shared_skill(ctx, KEY, kit_dir)
    hook_changed = agents.json_merge(ctx, KEY, HOOKS_REL, [agents.Append(HOOKS_PATH, [group])])
    manifest = kit_setup.load_manifest(ctx)
    agents.section(manifest, KEY)["sends"] = sends
    kit_setup.save_manifest(ctx, manifest)
    msgs = [
        f"{LABEL}: skill " + ("added to" if skill_changed else "already in") + f" ~/{agents.SHARED_SKILL_REL}",
        f"{LABEL}: ask rules " + ("written to" if rules_changed else "already in") + f" ~/{RULES_REL}",
        f"{LABEL}: safety hook " + ("added to" if hook_changed else "already in") + f" ~/{HOOKS_REL}",
    ]
    if sends == "allow":
        msgs.append(f"{LABEL}: sends are set to allow (--codex-sends allow), so {LABEL} will not ask before a send, "
                    "delete or share; the Mac window and Touch ID still guard each one")
        guard = f"{LABEL}: protected by a safety hook that blocks the ways round the rules."
    else:
        guard = (f"{LABEL}: protected by ask rules ({LABEL} asks before each send, delete or share) and a "
                 "safety hook that blocks the ways round them; the hook cannot ask, the rules do.")
    msgs.append(guard + f" Open {LABEL} and run /hooks to trust {registry.DISPLAY_NAME}'s safety hook "
                f"({LABEL} skips it until you do). This setup follows {LABEL}'s documentation and has not "
                f"yet been tried by hand in {LABEL}.")
    return msgs


def uninstall(ctx) -> List[str]:
    """Undo what the manifest records: our entry in hooks.json, then our rules file (and the
    folders we made), then our claim on the shared skill. A step that fails does not stop the
    others; what is left stays on record."""
    u = agents.Undo(ctx, KEY, LABEL)
    try:
        if agents.json_unmerge(ctx, KEY, HOOKS_REL):
            u.done.append(f"{LABEL}: removed the safety hook from ~/{HOOKS_REL}")
    except (SetupError, OSError, ValueError) as e:       # a file made unreadable since: the record stays
        u.keep = True
        u.first.append(f"{LABEL}: could not edit ~/{HOOKS_REL} ({e}); left as it is. Remove the entry that runs "
                       f"`{agents.hook_command(ctx, KEY)}` by hand")
    if u.run(f"the rules file ~/{RULES_REL}", agents.remove_owned_file, ctx, KEY, RULES_REL, _is_ours):
        u.done.append(f"{LABEL}: removed ~/{RULES_REL}")
    if u.run(f"the skill ~/{agents.SHARED_SKILL_REL}", agents.release_shared_skill, ctx, KEY):
        u.done.append(f"{LABEL}: removed the skill ~/{agents.SHARED_SKILL_REL}")
    return u.finish()
