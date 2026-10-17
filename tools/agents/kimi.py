"""Kimi Code: the shared skill in ~/.agents/skills and, in ~/.kimi-code/config.toml, one marked
block holding the ask rules and the safety hook.

Kimi's hook can only allow or block. The block holds ask rules too, but Kimi Code 0.8.2 reads
[[permission.rules]] from config.toml and never applies them, so Kimi itself does not ask before
a send (in its default mode it asks before every command); the Mac window and Touch ID guard
every send, and nothing here may say otherwise. The rules are written anyway: they start working
if a later Kimi applies them. The hook blocks the ways round the window (hook.decide_block, with
rules_cover below), and leaves a plain gated command alone. Each gated action gets two rules,
`Bash(<tool> <action>*)` and `Bash(<tool> <action>*/**)`: Kimi matches the pattern as a glob
over the whole command string, where `*` does not cross a "/" and `**` does.

There is no TOML writer in the standard library, so the file is edited as text. The block sits
between two marker comments. It goes just before the file's first table (`[name]` or `[[name]]`),
so a setting added to the end of the file later is the user's own and not part of the last
`[[hooks]]` entry, where Kimi would throw away every hook; a file with no table gets it at the
end, and its end marker says to add settings above it. A re-install puts the block back where it
belongs, and uninstall cuts out exactly it and gives back the bytes the file had before. The file
is only touched if Kimi has made it already, and never if it defines `hooks` or `permission.rules`
in a form a block of array-of-tables entries would clash with.

Kimi keeps its files in ~/.kimi-code, or in the folder named by KIMI_CODE_HOME when that is set
(a full path); the skill stays in ~/.agents/skills, which Kimi reads from the real home folder.

The manifest keeps, under manifest["agents"]["kimi"]: "toml" -> {config path: {"lead": the line
break this installer added so the file ended with one ("" if none)}} (the path is relative to home
for ~/.kimi-code and a full path for another folder), "edited" (the same paths, for the backup
note), "backups", and "skipped" when there was no config file to put the block in.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

import agents
import kit_setup
import registry

SetupError = kit_setup.SetupError

KEY = "kimi"
LABEL = "Kimi Code"
HOOK_STYLE = "block"

FOLDER = ".kimi-code"
CONFIG_REL = FOLDER + "/config.toml"
CONFIG_SHOWN = "~/" + CONFIG_REL
SKILL_SHOWN = "~/" + str(Path(agents.SHARED_SKILL_REL).parent)
BEGIN = (f"# >>> {registry.NAME}: added by the {registry.NAME} installer; "
         f"`{registry.GOOGLE_COMMAND} uninstall` removes it >>>")
END = f"# <<< {registry.NAME}: add your own settings above this block <<<"
OLD_END = f"# <<< {registry.NAME} <<<"            # what an early build wrote; still found and removed
HOOK_MATCHER = "^(Bash|Write|Edit)$"
HOOK_TIMEOUT = 30
HAND_HINT = f'delete the block from the line starting "# >>> {registry.NAME}" to the line starting "# <<< {registry.NAME}"'
EMPTY_INPUT_IS_FAILURE = True            # Kimi always sends something; silence is not a pass

# What Kimi accepts in a [[hooks]] entry (its schema is strict: one other key and it throws away the
# whole hooks section, ours with it) and the events it knows.
HOOK_KEYS = ("event", "matcher", "command", "timeout")
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionResult",
               "UserPromptSubmit", "UserPromptQueued", "TurnStarted", "Stop", "StopFailure", "Interrupt",
               "SessionStart", "SessionEnd", "SessionHeartbeat", "SubagentStart", "SubagentStop", "TaskStarted",
               "PreCompact", "PostCompact", "Notification")

_SAFE_WORD = re.compile(r"[A-Za-z0-9_-]+")        # a name that means itself in a glob pattern


def kimi_home(home) -> Path:
    """Kimi's own folder: $KIMI_CODE_HOME when it is set (Kimi reads it too), else ~/.kimi-code.
    SetupError for a relative KIMI_CODE_HOME: Kimi would read it from wherever it is started."""
    env = os.environ.get("KIMI_CODE_HOME", "").strip()
    if not env:
        return Path(home) / FOLDER
    if not os.path.isabs(env):
        raise SetupError("KIMI_CODE_HOME is not a full path, so Bertie cannot tell which folder Kimi Code uses. "
                         "Set it to a full path (starting with /) and run the installer again.")
    return Path(env)


def detect(home: Path) -> bool:
    try:
        return kimi_home(home).is_dir()
    except SetupError:
        return False


# ---------------------------------------------------------------- TOML text

_NAMED = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def toml_string(value: str) -> str:
    """`value` as a TOML basic string: quote and backslash escaped, and every control character
    (plus the line and paragraph separators, the byte order mark and the C1 controls, which
    some readers treat as line breaks) written as an escape."""
    out = []
    for ch in value:
        n = ord(ch)
        if ch in _NAMED:
            out.append(_NAMED[ch])
        elif n < 0x20 or 0x7F <= n <= 0x9F or n in (0x2028, 0x2029, 0xFEFF):
            out.append("\\u%04X" % n)
        elif 0xD800 <= n <= 0xDFFF:
            raise SetupError("a path with bytes that are not text cannot be written into a TOML file")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _gated() -> List[Tuple[str, str, str]]:
    """(tool, action, what it does) for every gated action, in registry order."""
    return [(tool, act, what) for tool, acts in registry.gated_actions().items() for act, what in acts.items()]


def _glob_safe(tool: str, action: str) -> bool:
    return bool(_SAFE_WORD.fullmatch(tool) and _SAFE_WORD.fullmatch(action))


def patterns(tool: str, action: str) -> Tuple[str, str]:
    """The two rule patterns for one gated action."""
    return f"Bash({tool} {action}*)", f"Bash({tool} {action}*/**)"


def block_lines(hook_cmd: str) -> List[str]:
    """Our block, marker to marker, one entry per line."""
    lines = [BEGIN]
    for tool, action, what in _gated():
        if not _glob_safe(tool, action):
            raise SetupError(f"'{tool} {action}' has characters that mean something in Kimi's rule patterns; "
                             "agents/kimi.py needs a rewording for it")
        for pattern in patterns(tool, action):
            lines += ["[[permission.rules]]",
                      'decision = "ask"',
                      "pattern = " + toml_string(pattern),
                      "reason = " + toml_string(f"{registry.DISPLAY_NAME}: {what}"),
                      ""]
    lines += ["[[hooks]]",
              'event = "PreToolUse"',
              "matcher = " + toml_string(HOOK_MATCHER),
              "command = " + toml_string(hook_cmd),
              f"timeout = {HOOK_TIMEOUT}",
              END]
    return lines


# ---------------------------------------------------------------- finding things in the file

_BEGIN_RE = re.compile("^" + chr(0xFEFF) + "?(" + re.escape(BEGIN) + r")[ \t]*\r?$", re.M)    # a byte order mark may come first
_END_RE = re.compile("^(?:" + re.escape(END) + "|" + re.escape(OLD_END) + r")[ \t]*\r?$", re.M)
# the hook line however the install folder is spelled: sh '<dir>/hook.sh' --agent kimi
_HOOK_LINE = re.compile(r"hook\.sh\S*\s+--agent[ =]" + KEY + r"\b")


def _find_block(text: str) -> Tuple[Optional[Tuple[int, int]], bool]:
    """(span, damaged). The span runs from the start of the begin marker's line to just after the
    line break that ends the end marker's. No marker at all: (None, False). One missing, one of
    them twice, or the end before the begin: (None, True)."""
    begins, ends = list(_BEGIN_RE.finditer(text)), list(_END_RE.finditer(text))
    if not begins and not ends:
        return None, False
    if len(begins) == 1 and len(ends) == 1 and begins[0].start() < ends[0].start():
        end = ends[0].end()
        if text.startswith("\n", end):
            end += 1
        return (begins[0].start(1), end), False
    return None, True


def _q(name: str) -> str:
    return '(?:"%s"|\'%s\'|%s)' % (name, name, name)


_HOOKS, _RULES, _PERM = _q("hooks"), _q("rules"), _q("permission")
_LEAD = "^[" + chr(0xFEFF) + r" \t]*"
# the ways a file can already define hooks or permission.rules so that appending
# [[hooks]] and [[permission.rules]] entries would clash with it
_CONFLICTS = tuple(re.compile(p, re.M) for p in (
    _LEAD + _HOOKS + r"[ \t]*[=.]",                                     # hooks = ...   hooks.x = ...
    _LEAD + _RULES + r"[ \t]*[=.]",                                     # rules = ...   (inside [permission])
    _LEAD + _PERM + r"[ \t]*=",                                         # permission = { ... }
    _LEAD + _PERM + r"[ \t]*\.[ \t]*" + _RULES + r"[ \t]*[=.]",          # permission.rules = ...
    _LEAD + r"\[(?!\[)[ \t]*" + _HOOKS + r"[ \t]*[\].]",                 # [hooks]   [hooks.x]
    _LEAD + r"\[(?!\[)[ \t]*" + _PERM + r"[ \t]*\.[ \t]*" + _RULES + r"[ \t]*[\].]",   # [permission.rules]
    _LEAD + r"\[\[[ \t]*" + _HOOKS + r"[ \t]*\.",                       # [[hooks.x]]
    _LEAD + r"\[\[[ \t]*" + _PERM + r"[ \t]*\.[ \t]*" + _RULES + r"[ \t]*\.",          # [[permission.rules.x]]
))


def _conflict(text: str) -> Optional[Tuple[int, str]]:
    """(line number, line) of the first line that defines hooks or permission.rules in a form our
    block would clash with, else None."""
    first = None
    for rx in _CONFLICTS:
        m = rx.search(text)
        if m and (first is None or m.start() < first):
            first = m.start()
    if first is None:
        return None
    n = text.count("\n", 0, first) + 1
    return n, text.split("\n")[n - 1].rstrip("\r")


def _ours(text: str, hook_cmd: str) -> Tuple[List[Tuple[int, str]], List[Tuple[int, str]], List[Tuple[int, str]]]:
    """(marker lines, hook lines, rule lines) as (line number, line) pairs: the lines that are
    Bertie's. Lines are split at "\\n" only, so no character in a command can move a line number."""
    escaped = toml_string(hook_cmd)[1:-1]
    quoted = {toml_string(p) for tool, act, _ in _gated() for p in patterns(tool, act)}
    marks: List[Tuple[int, str]] = []
    hooks: List[Tuple[int, str]] = []
    rules: List[Tuple[int, str]] = []
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if line.strip(" \t" + chr(0xFEFF)) in (BEGIN, END, OLD_END):
            marks.append((n, line))
        elif hook_cmd in line or escaped in line or _HOOK_LINE.search(line):
            hooks.append((n, line))
        elif any(q in line for q in quoted):
            rules.append((n, line))
    return marks, hooks, rules


def _where(text: str, hook_cmd: str) -> str:
    """Where Bertie's lines are in the file, in words, for the person who has to remove them."""
    marks, hooks, rules = _ours(text, hook_cmd)
    parts = []
    if marks:
        parts.append("marker lines at " + ", ".join(f"line {n}" for n, _ in marks))
    if hooks:
        parts.append("the hook command at " + ", ".join(f"line {n}" for n, _ in hooks)
                     + f" ({hooks[0][1].strip()[:60]})")
    if rules:
        parts.append(f"{len(rules)} rule pattern lines at "
                     + (", ".join(f"line {n}" for n, _ in rules) if len(rules) <= 6
                        else f"lines {rules[0][0]} to {rules[-1][0]}"))
    return "; ".join(parts)


def _parses(text: str) -> Optional[bool]:
    """Whether Python's own TOML reader accepts the text; None where Python has none (before 3.11)."""
    try:
        import tomllib
    except ImportError:
        return None
    try:
        tomllib.loads(text)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- reading and writing the file

def _target(ctx) -> Tuple[str, Path, str]:
    """(the manifest key, the path, how to show it) of the config file for this install: under
    ~/.kimi-code the key is the path from home, under another KIMI_CODE_HOME it is the full path."""
    folder = kimi_home(ctx.home)
    if folder == Path(ctx.home) / FOLDER:
        return CONFIG_REL, Path(ctx.home) / CONFIG_REL, CONFIG_SHOWN
    path = folder / "config.toml"
    return str(path), path, str(path)


def _recorded(ctx) -> List[Tuple[str, Path, str]]:
    """(key, path, shown) for every config file the manifest says we put a block in. The path is
    rebuilt from the key by the code's own rules: a key that climbs out of home, or a full path
    that is not a config.toml, is ignored, so a doctored record cannot point anywhere else."""
    recs = agents.peek(kit_setup.load_manifest(ctx), KEY).get("toml")
    out: List[Tuple[str, Path, str]] = []
    for key in (recs if isinstance(recs, dict) else {}):
        if not isinstance(key, str):
            continue
        if os.path.isabs(key):
            if Path(key).name != "config.toml":
                continue
            out.append((key, Path(key), key))
        else:
            try:
                path = agents._under_home(ctx, key)
            except SetupError:
                continue
            out.append((key, path, "~/" + key))
    return out


def hook_files(ctx) -> List[Path]:
    """Where Kimi's config may hold our hook: the default file and every recorded one."""
    paths = [Path(ctx.home) / CONFIG_REL]
    try:
        paths.append(_target(ctx)[1])
    except SetupError:
        pass
    paths += [p for _, p, _ in _recorded(ctx)]
    return list(dict.fromkeys(paths))


def _read(path: Path) -> str:
    """The file's text exactly as it is (no change to line endings). SetupError if unreadable."""
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise SetupError(f"{path} is not UTF-8 text; left untouched.")
    except OSError as e:
        raise SetupError(f"{path} cannot be read ({e.strerror}); left untouched.")


def _write(path: Path, text: str) -> None:
    """Replace the file's content, keeping its mode. A link is followed, so a config kept
    in a dotfiles folder stays a link."""
    try:
        real = Path(os.path.realpath(str(path)))
        agents.write_text(real, text, real.stat().st_mode & 0o777)
    except OSError as e:
        raise SetupError(f"{path} cannot be written ({e.strerror}); left untouched.")


def _line_break(text: str) -> str:
    crlf = text.count("\r\n")
    return "\r\n" if crlf > text.count("\n") - crlf else "\n"


def _recorded_lead(ctx, key: str = CONFIG_REL) -> str:
    recs = agents.peek(kit_setup.load_manifest(ctx), KEY).get("toml")
    rec = recs.get(key) if isinstance(recs, dict) else None
    lead = rec.get("lead") if isinstance(rec, dict) else None
    return lead if lead in ("\n", "\r\n") else ""


_HOOKS_HEADER = re.compile(r"^[ \t]*\[\[[ \t]*" + _HOOKS + r"[ \t]*\]\][ \t]*(?:#.*)?$")
_ANY_HEADER = re.compile(r"^[ \t]*\[")
_KEY_LINE = re.compile(r"""^[ \t]*("[^"]*"|'[^']*'|[A-Za-z0-9_.-]+)[ \t]*=[ \t]*(.*)$""")
_EVENT_VALUE = re.compile(r"""^(?:"([^"]*)"|'([^']*)')[ \t]*(?:#.*)?$""")


def _bad_hook_entry(text: str) -> Optional[Tuple[int, str, str]]:
    """(line number, line, why) of the first [[hooks]] entry Kimi would not accept: a key other
    than event, matcher, command or timeout, or an event it does not know. Kimi throws away the
    whole hooks section for one such entry, which would take Bertie's hook with it."""
    inside = False
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if _HOOKS_HEADER.match(line):
            inside = True
        elif _ANY_HEADER.match(line):
            inside = False
        elif inside:
            m = _KEY_LINE.match(line)
            if not m:
                continue
            key = m.group(1).strip("\"'")
            if key not in HOOK_KEYS:
                return n, line.strip(), f"has the key '{key}', which Kimi does not accept in a hook"
            if key == "event":
                ev = _EVENT_VALUE.match(m.group(2).strip())
                name = (ev.group(1) if ev.group(1) is not None else ev.group(2)) if ev else None
                if name is not None and name not in HOOK_EVENTS:
                    return n, line.strip(), f"names the event '{name}', which Kimi does not know"
    return None


def _key_after_block(tail: str) -> Optional[Tuple[int, str]]:
    """(line number within the text after our block, line) of the first `key = value` line that
    follows the block before any table header. TOML would file it under our last [[hooks]] table,
    and Kimi throws away the whole hooks section for a hook with a key it does not know."""
    for n, raw in enumerate(tail.split("\n"), 1):
        line = raw.rstrip("\r")
        if _ANY_HEADER.match(line):
            return None
        if _KEY_LINE.match(line):
            return n, line
    return None


def _first_table(text: str) -> Optional[int]:
    """Where the block goes: the offset of the line that starts the file's first table (`[name]` or
    `[[name]]`), moved up over the comment lines stuck to it. None when the file has no table, or
    is too broken to find one (a string that never ends); the block then goes at the end.

    The text is read the way TOML is: a line that starts with `[` is a table only outside a string
    and outside an array or inline table that goes over several lines, so `["a", "b"],` inside a
    multi-line array is not taken for one. Only a `#` that really starts a comment counts as a comment."""
    n, i, depth, line_start, at_start = len(text), 0, 0, 0, True
    comments = set()                                      # offsets of lines that are only a comment
    while i < n:
        c = text[i]
        if c == "\n":
            i += 1
            line_start, at_start = i, True
            continue
        if c in " \t\r" or (i == 0 and c == chr(0xFEFF)):
            i += 1
            continue
        if at_start and c == "[" and depth == 0:
            at = line_start
            while at > 0:
                before = text.rfind("\n", 0, at - 1) + 1  # the start of the line above
                if before not in comments:
                    break
                at = before
            return at
        if at_start and c == "#":
            comments.add(line_start)
        at_start = False
        if c == "#":
            end = text.find("\n", i)
            if end < 0:
                return None
            i = end
        elif c in "\"'":
            triple = c * 3
            if text.startswith(triple, i):
                j = i + 3
                while j < n:
                    if c == '"' and text[j] == "\\":
                        j += 2
                    elif text.startswith(triple, j):
                        j += 3
                        for _ in range(2):                # a quote or two just before the end belong to the string
                            if text.startswith(c, j):
                                j += 1
                        break
                    else:
                        j += 1
                else:
                    return None
                i = j
            else:
                j = i + 1
                while j < n and text[j] not in (c, "\n"):
                    j += 2 if (c == '"' and text[j] == "\\") else 1
                if j >= n or text[j] != c:
                    return None
                i = j + 1
        else:
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth = max(0, depth - 1)
            i += 1
    return None


_OUR_LINE = re.compile(
    r'^(?:\[\[(?:permission\.rules|hooks)\]\]|(?:decision|pattern|reason|event|matcher|command) = ".*"'
    r"|timeout = \d+|#.*)?$")


def _foreign_in_block(block: str) -> Optional[Tuple[int, str]]:
    """(line number within the block, line) of the first line inside our markers that is not one
    of the shapes this installer writes: something of the user's (or Kimi's) that landed in it."""
    for n, raw in enumerate(block.split("\n"), 1):
        line = raw.rstrip("\r")
        if not _OUR_LINE.match(line.strip()):
            return n, line
    return None


def _plan(text: str, hook_cmd: str, recorded_lead: str, shown: str = CONFIG_SHOWN) -> Tuple[str, str]:
    """(the file's new text, the line break added in front of the block) for an install. SetupError,
    with nothing changed, if the file cannot take the block."""
    span, damaged = _find_block(text)
    if damaged:
        raise SetupError(f"{shown} has only part of the start and end lines Bertie puts round its block, so "
                         "it was left as it is. Delete the lines that look like Bertie's, then run the installer "
                         f"again. These are at: {_where(text, hook_cmd)}")
    if span is None:
        marks, hooks, rules = _ours(text, hook_cmd)
        if hooks or rules:
            raise SetupError(f"{shown} has Bertie's {'safety hook' if hooks else 'ask rules'} but not the start and "
                             "end lines Bertie puts round its block (Kimi may have rewritten the file), so it was "
                             "left as it is. Delete the [[hooks]] table that holds the hook and the Bertie "
                             f"[[permission.rules]] tables, then run the installer again. These are at: "
                             f"{_where(text, hook_cmd)}")
        base = text
    else:
        stray = _foreign_in_block(text[span[0]:span[1]])
        if stray:
            raise SetupError(f"{shown} has a line that is not Bertie's inside Bertie's block (line "
                             f"{text.count(chr(10), 0, span[0]) + stray[0]}: {stray[1].strip()[:60]}), so it was left "
                             "as it is. Move your own settings above the block, then run the installer again.")
        loose = _key_after_block(text[span[1]:])
        if loose:
            raise SetupError(f"{shown} has a setting right after Bertie's block ({loose[1].strip()[:60]}), which "
                             "Kimi would read as part of Bertie's safety hook and then throw every hook away. "
                             "Move that line above Bertie's block, then run the installer again.")
        before, after = text[:span[0]], text[span[1]:]
        if recorded_lead and not after and before.endswith(recorded_lead):
            before = before[:-len(recorded_lead)]         # the line break an earlier install added so the file ended with one
        base = before + after
    hit = _conflict(base)
    if hit:
        raise SetupError(f"{shown} line {hit[0]} ({hit[1].strip()[:60]}) already defines the hooks or the "
                         "permission rules in a form Bertie's block cannot sit beside, so it was left as it is.")
    bad = _bad_hook_entry(base)
    if bad:
        raise SetupError(f"{shown} line {bad[0]} ({bad[1][:60]}) {bad[2]}. Kimi throws away every hook in a file "
                         "with a hook like that, Bertie's safety hook too, so nothing was added. Fix that hook, "
                         "then run the installer again.")
    nl = _line_break(base)
    block = nl.join(block_lines(hook_cmd)) + nl
    at = _first_table(base)
    if at is None:                                            # no table in the file: the end, with a line break before it if needed
        lead = nl if base and not base.endswith("\n") else ""
        new = base + lead + block
    else:                                                     # just before the first table, which starts a line
        if base.startswith(chr(0xFEFF)):
            at = max(at, 1)
        lead = ""
        new = base[:at] + block + base[at:]
    if _parses(base) is True and _parses(new) is False:
        raise SetupError(f"{shown} could not be read as TOML once Bertie's block was added (some of its "
                         "hooks or permission entries clash with it), so it was left as it is.")
    return new, lead


# ---------------------------------------------------------------- install and uninstall

def preflight(ctx, kit_dir: Path) -> None:
    """Every refusal, before anything is written: a skill that is not ours, a folder on the way
    that is a link, a config file the block cannot go into."""
    agents.preflight_skill(ctx, agents.SHARED_SKILL_REL, kit_dir)
    _, path, shown = _target(ctx)
    if path.is_file():
        key = _target(ctx)[0]
        _plan(_read(path), agents.hook_command(ctx, KEY), _recorded_lead(ctx, key), shown)


def install(ctx, kit_dir: Path) -> List[str]:
    key, path, shown = _target(ctx)
    hook_cmd = agents.hook_command(ctx, KEY)
    preflight(ctx, kit_dir)                                         # may refuse, before anything is written
    present = path.is_file()
    text = new = lead = None
    if present:
        text = _read(path)
        new, lead = _plan(text, hook_cmd, _recorded_lead(ctx, key), shown)
    skill_changed = agents.acquire_shared_skill(ctx, KEY, kit_dir)
    msgs = [f"{LABEL}: " + (f"installed the skill in {SKILL_SHOWN}" if skill_changed
                            else f"the skill is already in {SKILL_SHOWN}")]
    if not present:
        manifest = kit_setup.load_manifest(ctx)
        agents.section(manifest, KEY)["skipped"] = f"no {key}"
        kit_setup.save_manifest(ctx, manifest)
        msgs += [f"{LABEL}: {shown} was not found (Kimi makes it the first time it runs), so the safety hook "
                 "and the ask rules were NOT added.",
                 f"{LABEL}: start Kimi Code once, then run the installer again to add them. Until then only the "
                 "Mac confirmation window and Touch ID guard each send."]
        return msgs
    if new != text:
        had_block = _find_block(text)[0] is not None
        try:
            agents.backup_file(ctx, KEY, path)
        except OSError as e:
            raise SetupError(f"could not save a copy of {path} first ({e.strerror}); left untouched.")
        _write(path, new)
        msgs.append(f"{LABEL}: ask rules and safety hook {'updated in' if had_block else 'added to'} {shown} "
                    "(a copy of the file was saved beside it first)")
    else:
        msgs.append(f"{LABEL}: ask rules and safety hook already in {shown}")
    manifest = kit_setup.load_manifest(ctx)
    sec = agents.section(manifest, KEY)
    sec.pop("skipped", None)
    sec.setdefault("toml", {})[key] = {"lead": lead}
    sec["edited"] = list(sec["toml"])
    kit_setup.save_manifest(ctx, manifest)
    msgs += [f"{LABEL}: Kimi Code does not yet apply ask rules from its settings, so Kimi itself does not ask "
             "before a send (in its default mode it asks before every command). The Mac confirmation window and "
             "Touch ID guard every send. The rules are written anyway, in case a later Kimi applies them.",
             f"{LABEL}: in --auto mode, and after you choose \"approve for this session\", Kimi asks nothing at "
             "all, so then only the Mac confirmation window and Touch ID guard each send.",
             f"{LABEL}: the safety hook can only block, never ask: it blocks a command that would slip a Bertie "
             "action past the window. Start a new Kimi Code session to use the new settings."]
    return msgs


def _remove_block(ctx, key: str, path: Path, shown: str) -> List[str]:
    """Cut our block out of one config file, if the manifest says we put one there."""
    if not path.is_file():
        return []
    text = _read(path)
    hook_cmd = agents.hook_command(ctx, KEY)
    span, damaged = _find_block(text)
    if span is None:
        if not any(_ours(text, hook_cmd)):
            return []                                    # nothing of ours left in it
        why = ("has only part of the start and end lines Bertie put round its block" if damaged
               else "no longer has the start and end lines Bertie put round its block (Kimi may have rewritten it)")
        return [f"{LABEL}: {shown} {why}, so it was left exactly as it is.",
                f"{LABEL}: to finish by hand, open it and delete the [[hooks]] table and the [[permission.rules]] "
                f"tables that belong to Bertie. These lines look like Bertie's: {_where(text, hook_cmd)}"]
    stray = _foreign_in_block(text[span[0]:span[1]])
    if stray:
        return [f"{LABEL}: {shown} has a line that is not Bertie's inside Bertie's block (line "
                f"{text.count(chr(10), 0, span[0]) + stray[0]}: {stray[1].strip()[:60]}), so it was left exactly as it is.",
                f"{LABEL}: to finish by hand, open it and delete the lines from the one starting "
                f"\"# >>> {registry.NAME}\" to the line starting \"# <<< {registry.NAME}\", keeping your own line."]
    before, after = text[:span[0]], text[span[1]:]
    lead = _recorded_lead(ctx, key)
    if lead and not after and before.endswith(lead):
        before = before[:-len(lead)]                     # the line break we added so the file ended with one
    try:
        backup = kit_setup._backup_file(path)
    except OSError as e:
        raise SetupError(f"could not save a copy of {path} first ({e.strerror}); left as it is")
    _write(path, before + after)
    return [f"{LABEL}: removed the ask rules and safety hook from {shown} "
            f"(a copy of the file as it was before is left beside it, named {backup.name})"]


def uninstall(ctx) -> List[str]:
    u = agents.Undo(ctx, KEY, LABEL)
    for key, path, shown in _recorded(ctx):
        try:
            u.done += _remove_block(ctx, key, path, shown)
        except (SetupError, OSError, ValueError) as e:
            u.left.append(f"{shown}: {e}")
            u.done.append(f"{LABEL}: to finish by hand, open {shown} and {HAND_HINT}")
    if u.run(f"the skill in {SKILL_SHOWN}", agents.release_shared_skill, ctx, KEY):
        u.done.append(f"{LABEL}: removed the skill from {SKILL_SHOWN}")
    return u.finish()


# ---------------------------------------------------------------- the hook

def hook_input(data: dict) -> Tuple[str, str]:
    """{"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "..."}}.
    Write and Edit carry a path and text (content, or old_string and new_string)."""
    import hook
    tool = data.get("tool_name")
    if not isinstance(tool, str):
        raise ValueError("no tool name")
    tool_input = data.get("tool_input")
    if tool == "Bash":
        if not isinstance(tool_input, dict):
            raise ValueError("tool input is not an object")
        command = tool_input.get("command")
        if not isinstance(command, str):
            raise ValueError("no command text")
        return "command", command
    if tool in ("Write", "Edit"):
        if not isinstance(tool_input, dict):
            raise ValueError("tool input is not an object")
        return "file", "\n".join(hook.strings_in(tool_input))
    return "none", ""


def hook_scan_text(data: dict, command: str) -> Optional[str]:
    """What the checks read for a Bash call: the folder it runs in first (only where it can matter,
    see hook.cwd_prefix), then the command. Kimi runs the command in tool_input.cwd, else in the
    session's own folder (the top-level cwd). rules_cover is still asked about the command alone."""
    tool_input = data.get("tool_input")
    if data.get("tool_name") != "Bash" or not isinstance(tool_input, dict):
        return None
    cwd = tool_input.get("cwd")
    if cwd is None or cwd == "":
        cwd = data.get("cwd")
    if cwd is None:
        return None
    import hook
    prefix = hook.cwd_prefix(cwd, command)               # ValueError for a folder that is not text
    return prefix + command if prefix else None


def hook_cwd_known(data: dict) -> bool:
    """Whether a full path was given for the folder the command runs in (see hook.folder_given): the
    folder hook_scan_text reads, tool_input.cwd or else the session's own."""
    tool_input = data.get("tool_input")
    if data.get("tool_name") != "Bash" or not isinstance(tool_input, dict):
        return False
    cwd = tool_input.get("cwd")
    if cwd is None or cwd == "":
        cwd = data.get("cwd")
    import hook
    return hook.folder_given(cwd)


def ask_clause() -> str:
    """Who asks the person when a send stands alone, for the end of a block message. Not Kimi:
    it does not apply the ask rules, so the Mac window is what asks."""
    return "the Mac window asks you"


def hook_answer(reason: Optional[str]) -> Tuple[str, int]:
    if not reason:
        return "", 0
    return agents.pretooluse_answer("deny", reason), 0


def hook_failure(reason: str) -> Tuple[str, int]:
    return agents.pretooluse_answer("deny", reason), 0


_LINE_ENDS = ("\n", "\r", chr(0x2028), chr(0x2029))


def rules_cover(command: str) -> bool:
    """True when the command starts with a gated `<tool> <action>` and then a space or the end of
    the text (case counts, and a leading space does not match) and is on one line. Then it is a
    plain gated command: the hook leaves it alone and the Mac window asks. Kimi 0.8.2 applies no
    rules at all (see the top of this file), so how Kimi's glob matcher would read the rest of the
    line no longer decides anything, and a path with "/." or a backslash in it is as plain as any.
    Anything that hides another command, or is not the first thing on the line, is not covered."""
    if not isinstance(command, str) or any(c in command for c in _LINE_ENDS):
        return False
    for tool, act, _ in _gated():
        if _glob_safe(tool, act):
            start = f"{tool} {act}"
            if command == start or command.startswith(start + " "):
                return True
    return False
